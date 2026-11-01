"""兼容模式（安全模式）与原生崩溃自救的回归测试。

真机背景（2026-10-15，另一台电脑）：
Win10 + RDP 远程桌面 + Parsec/UU/微软三个虚拟显示适配器上，
程序**每次双击必崩**：`QtCore.pyd` 里 `0xC0000005` 访问违规，
崩在 GUI 初始化阶段（窗口还没出来），用户看到的是"双击完全没反应"。

原生崩溃 Python 抓不住，所以策略是：崩了要留痕、要能自动换安全模式重开、
下次启动要记得上次崩过。这里把这些逻辑钉住。
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui_launcher as launcher  # noqa: E402

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False


class CrashRecoveryPlanTests(unittest.TestCase):
    """退出码 → 要不要用安全模式重开。"""

    def test_normal_exits_do_not_relaunch(self):
        for code in (0, None, 1, 3, "0"):
            self.assertEqual(launcher.crash_recovery_plan(code, False), 'none', code)

    def test_native_crash_codes_relaunch_in_safe_mode(self):
        # 真机日志里的就是 0xC0000005（访问违规），Windows 有时给无符号、有时给负数
        for code in (0xC0000005, 3221225477, -1073741819, 0xC0000409, 0xC0000374, 0xC00000FD):
            self.assertEqual(launcher.crash_recovery_plan(code, False), 'relaunch-safe', code)

    def test_never_loops_when_already_in_safe_mode(self):
        self.assertEqual(launcher.crash_recovery_plan(0xC0000005, True), 'none')


class LaunchMarkerTests(unittest.TestCase):
    """「上次没退干净」的判定：标记文件还在 + 那个进程已经没了 = 崩过。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_marker_round_trip(self):
        self.assertEqual(launcher.read_marker(), {})
        self.assertFalse(launcher.previous_launch_crashed())
        launcher.write_marker(False)
        data = launcher.read_marker()
        self.assertEqual(data.get("pid"), os.getpid())
        self.assertIs(data.get("safe_mode"), False)
        # 当前进程还活着 → 不算崩（正常情况：另一个实例正在跑）
        self.assertFalse(launcher.previous_launch_crashed())
        launcher.clear_marker()
        self.assertEqual(launcher.read_marker(), {})

    def test_dead_process_in_marker_means_previous_crash(self):
        launcher.write_marker(False)
        marker = launcher.read_marker()
        marker["pid"] = 999999          # 不可能存在的进程
        launcher.marker_path().write_text(json.dumps(marker), encoding="utf-8")
        self.assertTrue(launcher.previous_launch_crashed())

    def test_broken_marker_is_treated_as_crash(self):
        launcher.marker_path().write_text("不是 json", encoding="utf-8")
        self.assertFalse(launcher.previous_launch_crashed(), "读不出来的标记按没有处理更安全")
        launcher.marker_path().write_text('{"pid": "abc"}', encoding="utf-8")
        self.assertTrue(launcher.previous_launch_crashed())

    def test_config_safe_mode_reads_gui_config(self):
        target = pathlib.Path(self._tmp.name) / "WhaleGirlQQ" / "data"
        target.mkdir(parents=True, exist_ok=True)
        (target / "gui_config.json").write_text('{"safe_mode": true}', encoding="utf-8")
        self.assertTrue(launcher.config_safe_mode())
        (target / "gui_config.json").write_text('{"safe_mode": false}', encoding="utf-8")
        self.assertFalse(launcher.config_safe_mode())
        (target / "gui_config.json").write_text("坏文件", encoding="utf-8")
        self.assertFalse(launcher.config_safe_mode())


class MemeWorkerIsolationTests(unittest.TestCase):
    def test_worker_never_reads_or_writes_main_window_state(self):
        with mock.patch('sys.argv', ['WhaleGirlQQ.exe', '--meme-worker', 'input.json', 'output.json']), \
             mock.patch('src.meme_worker.main') as worker, \
             mock.patch.object(launcher, '_logs_dir', side_effect=AssertionError('不能读主窗口状态')):
            self.assertEqual(launcher.main(), 0)
            worker.assert_called_once_with('input.json', 'output.json')

    def test_failed_worker_does_not_enter_gui_recovery(self):
        with mock.patch('sys.argv', ['WhaleGirlQQ.exe', '--meme-worker', 'input.json', 'output.json']), \
             mock.patch('src.meme_worker.main', side_effect=ValueError('输入损坏')), \
             mock.patch.object(launcher, '_logs_dir', side_effect=AssertionError('不能读主窗口状态')), \
             mock.patch.object(launcher, '_relaunch_in_safe_mode') as relaunch:
            with self.assertRaisesRegex(ValueError, '输入损坏'):
                launcher.main()
            relaunch.assert_not_called()


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class SafeModeWindowTests(unittest.TestCase):
    """安全模式下的窗口：系统边框、不透明、不碰 DWM/云母。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, safe: bool):
        import qq_gui
        # 每次都用临时配置文件：界面测试绝不能读写仓库/用户的真实外观配置
        cfg = pathlib.Path(self._tmp.name) / "gui_config.json"
        with mock.patch.object(qq_gui, 'SAFE_MODE', safe), \
             mock.patch.object(qq_gui.MainWindow, '_prepare_backend', lambda self: None), \
             mock.patch.object(qq_gui, 'apply_mica') as mica:
            window = qq_gui.MainWindow(config_path=cfg)
            window._mica_spy = mica
            return qq_gui, window

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def _close(self, window):
        try:
            window.hang_watchdog._stop = True
            window.hang_watchdog._timer.stop()
        except Exception:
            pass
        window.close()
        window.deleteLater()
        self.app.processEvents()

    def tearDown(self):
        self._tmp.cleanup()

    def test_safe_mode_uses_a_plain_framed_window(self):
        gui, window = self._window(True)
        try:
            self.assertTrue(window.safe_mode)
            flags = window.windowFlags()
            self.assertFalse(bool(flags & Qt.WindowType.FramelessWindowHint), "安全模式不该无边框")
            self.assertFalse(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground),
                             "安全模式不该要半透明背景")
        finally:
            self._close(window)

    def test_safe_mode_never_touches_dwm(self):
        gui, window = self._window(True)
        try:
            window.show()
            self.app.processEvents()
            window._apply_backdrop()
            window._mica_spy.assert_not_called()
        finally:
            self._close(window)

    def test_only_one_title_bar_in_both_modes(self):
        for safe in (False, True):
            gui, window = self._window(safe)
            try:
                window.show()
                self.app.processEvents()
                self.assertEqual(window.title_bar.isHidden(), safe)
                self.assertIn(gui.APP_VERSION, window.windowTitle())
                # 边距现在跟随"窗口是否真的透明"：不透明时归零（否则那 6px 就是一圈白框，
                # 真机上用户报过"白框框"），透明时才留 6px 给圆角/阴影。
                margin = window.layout().contentsMargins().top()
                if safe:
                    self.assertEqual(margin, 0)
                else:
                    expected = 0 if window.windowOpacity() >= 0.995 else 6
                    self.assertEqual(margin, expected,
                                     f"不透明时边距应为 0、透明时为 6（opacity={window.windowOpacity()}）")
            finally:
                self._close(window)

    def test_safe_mode_native_messages_do_not_enter_custom_hit_testing(self):
        gui, window = self._window(True)
        try:
            with mock.patch.object(gui.ctypes, 'WinDLL', side_effect=AssertionError('系统边框不做自定义命中')):
                window.nativeEvent(b'windows_generic_MSG', 0)
        finally:
            self._close(window)

    def test_normal_mode_still_keeps_the_custom_look(self):
        gui, window = self._window(False)
        try:
            self.assertFalse(window.safe_mode)
            flags = window.windowFlags()
            self.assertTrue(bool(flags & Qt.WindowType.FramelessWindowHint))
            self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        finally:
            self._close(window)

    def test_safe_mode_notice_is_visible_to_the_user(self):
        gui, window = self._window(True)
        try:
            window.show()
            self.app.processEvents()
            self.assertIn('兼容模式', window.console_tab.notice.text())
        finally:
            self._close(window)

    def test_appearance_checkbox_persists_safe_mode(self):
        gui, window = self._window(False)
        try:
            window.appearance_tab.safe_mode_box.setChecked(True)
            with mock.patch.object(gui, 'save_gui_config') as save:
                window.appearance_tab._apply()
            self.assertTrue(window._cfg.get('safe_mode'))
            save.assert_called()
            self.assertIn('重新打开', window.console_tab.notice.text())
        finally:
            self._close(window)

    def test_glossary_of_safe_mode_flag_is_parsed(self):
        import importlib
        import qq_gui
        with mock.patch.object(__import__('sys'), 'argv', ['qq_gui.py', '--safe-mode']):
            reloaded = importlib.reload(qq_gui)
            try:
                self.assertTrue(reloaded.SAFE_MODE)
                self.assertEqual(os.environ.get('QT_OPENGL'), 'software')
            finally:
                with mock.patch.object(__import__('sys'), 'argv', ['qq_gui.py']):
                    importlib.reload(qq_gui)


if __name__ == "__main__":
    unittest.main()
