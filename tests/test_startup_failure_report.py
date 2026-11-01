"""启动失败路径：无人值守入口不许弹模态框；坏库/坏环境变量要给能照做的错误。"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gui_launcher
from src import compat_console as compat
from src import desktop_runtime as desktop


class _TempProfile(unittest.TestCase):
    """每个用例把 LOCALAPPDATA 指到临时目录，绝不碰用户真实日志。"""

    def setUp(self):
        # 坏库路径上 sqlite 连接可能还持有文件句柄，清理失败不该算用例失败。
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        patcher = patch.dict(os.environ, LOCALAPPDATA=str(self.base / 'profile'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = self.base / 'profile/WhaleGirlQQ/logs'


class InteractiveGateTests(unittest.TestCase):
    def test_unattended_entries_are_never_interactive(self):
        for argv in (['app', '--backend-only'],
                     ['app', '--self-test', 'out.json'],
                     ['app', '--native-self-test', 'out.json']):
            with self.subTest(argv=argv):
                self.assertFalse(desktop.interactive_session(argv))

    def test_service_window_station_is_not_interactive(self):
        # 服务会话/计划任务拿不到 WinSta0：在那里弹框没有人能点，只会把启动挂住。
        with patch.object(desktop, 'desktop_station', return_value='Service-0x0-3e7$'):
            self.assertFalse(desktop.interactive_session(['app']))

    def test_console_desktop_station_is_interactive(self):
        with patch.object(desktop, 'desktop_station', return_value='WinSta0'):
            self.assertTrue(desktop.interactive_session(['app']))

    def test_unreadable_window_station_stays_interactive(self):
        # 查不到窗口站时按交互处理：宁可多弹一次，也不能吞掉正常桌面启动的提示。
        with patch.object(desktop, 'desktop_station', return_value=None):
            self.assertTrue(desktop.interactive_session(['app']))

    def test_non_windows_never_shows_a_modal(self):
        with patch.object(desktop, 'desktop_station', return_value='WinSta0'):
            self.assertFalse(desktop.interactive_session(['app'], windows=False))

    def test_env_kill_switch_disables_the_modal(self):
        with patch.dict(os.environ, WHALEGIRL_NO_MODAL='1'), \
             patch.object(desktop, 'desktop_station', return_value='WinSta0'):
            self.assertFalse(desktop.interactive_session(['app']))


class HeadlessFailureTests(_TempProfile):
    def test_backend_only_failure_skips_the_modal_and_names_the_database(self):
        box = Mock()
        stream = io.StringIO()
        target = self.base / 'root/data/qq-only/messages.sqlite3'
        reason = '数据库文件无法读取：%s' % target
        with patch.object(desktop, 'retry_in_safe_mode') as retry:
            code = desktop.failed(reason, messagebox=box, argv=['app', '--backend-only'],
                                  stream=stream)
        self.assertEqual(code, 1)
        self.assertEqual(box.call_count, 0)
        retry.assert_not_called()
        self.assertIn(str(target), stream.getvalue())
        self.assertIn(str(target), (self.logs / 'startup.log').read_text(encoding='utf-8'))

    def test_interactive_failure_still_shows_exactly_one_modal(self):
        box = Mock(return_value=7)          # IDNO
        with patch.object(desktop, 'retry_in_safe_mode') as retry:
            code = desktop.failed('桌面窗口未能正常启动', messagebox=box, interactive=True)
        self.assertEqual(code, 1)
        self.assertEqual(box.call_count, 1)
        retry.assert_not_called()

    def test_interactive_yes_answer_still_retries_in_safe_mode(self):
        with patch.object(desktop, 'retry_in_safe_mode') as retry:
            self.assertEqual(desktop.failed('启动失败', messagebox=Mock(return_value=6),
                                            interactive=True), 1)
        retry.assert_called_once_with()

    def test_gui_entry_resolves_interactivity_from_the_window_station(self):
        box = Mock(return_value=7)
        with patch.object(desktop, 'desktop_station', return_value='WinSta0'), \
             patch.object(desktop, 'retry_in_safe_mode'):
            desktop.failed('启动失败', messagebox=box, argv=['app'])
        self.assertEqual(box.call_count, 1)

    @unittest.skipUnless(os.name == 'nt', '原生模态框只在 Windows 上')
    def test_compat_inform_follows_the_same_rule(self):
        box = Mock()
        compat.inform('后台已启动，但浏览器未能自动打开。', messagebox=box, interactive=True,
                      stream=io.StringIO())
        self.assertEqual(box.call_count, 1)
        stream = io.StringIO()
        compat.inform('请关闭旧控制台后重开。', messagebox=box, interactive=False, stream=stream)
        self.assertEqual(box.call_count, 1)          # 非交互不弹框，只落 stderr + 日志
        self.assertIn('请关闭旧控制台后重开', stream.getvalue())


class CorruptDatabaseTests(_TempProfile):
    def _corrupt_root(self):
        root = self.base / 'root'
        target = root / 'data/qq-only/messages.sqlite3'
        target.parent.mkdir(parents=True, exist_ok=True)
        junk = b'this is definitely not a sqlite database\n'
        target.write_bytes(junk)
        return root, target, junk

    def test_prepare_app_names_the_file_and_never_touches_user_data(self):
        root, target, junk = self._corrupt_root()
        with self.assertRaises(compat.StartupError) as caught:
            compat.prepare_app(root)
        message = str(caught.exception)
        self.assertIn(str(target), message)
        self.assertIn('备份', message)
        self.assertIn('不会自动删除', message)
        self.assertEqual(target.read_bytes(), junk)               # 原文件逐字节不变
        for suffix in ('.bak', '.old', '.orig', '.1'):
            self.assertFalse(Path(str(target) + suffix).exists())  # 不改名、不重建

    def test_backend_entry_exits_nonzero_without_modal_and_keeps_the_file(self):
        root, target, junk = self._corrupt_root()
        box = Mock()
        stream = io.StringIO()
        with patch.object(compat, 'runtime_paths', return_value=(root, root)), \
             patch.object(compat, 'probe', return_value=None), \
             patch.object(desktop, 'message_box', box):
            code = compat.main(open_browser=False, stream=stream)
        self.assertNotEqual(code, 0)
        box.assert_not_called()
        self.assertIn(str(target), stream.getvalue())
        self.assertIn('数据库文件无法读取', stream.getvalue())
        self.assertEqual(target.read_bytes(), junk)


class EnvironmentPathTests(_TempProfile):
    def test_unusable_localappdata_is_named_in_the_error(self):
        with patch.dict(os.environ, LOCALAPPDATA='Z:\\'):
            with self.assertRaises(compat.StartupError) as caught:
                compat.runtime_paths(frozen=True)
        message = str(caught.exception)
        self.assertIn('LOCALAPPDATA', message)
        self.assertIn('Z:\\', message)
        self.assertNotIsInstance(caught.exception, FileNotFoundError)

    def test_backend_entry_reports_unusable_localappdata_without_modal(self):
        box = Mock()
        stream = io.StringIO()
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, '_MEIPASS', str(self.base / 'bundle'), create=True), \
             patch.dict(os.environ, LOCALAPPDATA='Z:\\'), \
             patch.object(compat, 'probe', return_value=None), \
             patch.object(desktop, 'message_box', box):
            code = compat.main(open_browser=False, stream=stream)
        self.assertNotEqual(code, 0)
        box.assert_not_called()
        self.assertIn('LOCALAPPDATA', stream.getvalue())
        self.assertIn('Z:\\', stream.getvalue())


class LauncherBackendOnlyTests(_TempProfile):
    def test_backend_only_startup_failure_never_pops_a_modal(self):
        root = self.base / 'root'
        target = root / 'data/qq-only/messages.sqlite3'
        target.parent.mkdir(parents=True)
        junk = b'not a database\n'
        target.write_bytes(junk)
        self.logs.mkdir(parents=True, exist_ok=True)
        box = Mock()
        stream = io.StringIO()
        with patch.object(gui_launcher, '_logs_dir', return_value=self.logs), \
             patch.object(compat, 'runtime_paths', return_value=(root, root)), \
             patch.object(compat, 'probe', return_value=None), \
             patch.object(desktop, 'message_box', box), \
             patch.object(sys, 'argv', ['app', '--backend-only']), \
             patch.object(sys, 'stderr', stream):
            code = gui_launcher.main()
        self.assertNotEqual(code, 0)
        self.assertEqual(box.call_count, 0)
        self.assertIn(str(target), stream.getvalue())
        self.assertEqual(target.read_bytes(), junk)
        self.assertIn(str(target), (self.logs / 'startup.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
