"""启动监护的取证链：子进程输出、崩溃报告、失败弹窗文案。不连 QQ、不弹真窗。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gui_launcher
from src import desktop_runtime as desktop
from src import startup_supervisor as monitor


class _TempLogs(unittest.TestCase):
    """每个用例把 LOCALAPPDATA 指到临时目录，绝不碰用户真实日志。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        patcher = patch.dict(os.environ, LOCALAPPDATA=str(self.base))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = monitor.logs_dir()

    def read_json(self):
        return json.loads((self.logs / 'desktop-crash.json').read_text(encoding='utf-8'))


class ChildOutputTests(_TempLogs):
    def test_child_stderr_lands_in_gui_child_log(self):
        command = [sys.executable, '-u', '-c',
                   'import sys; sys.stderr.write("BOOM-CRASH-ORIGINAL-LINE\\n"); sys.stderr.flush()']
        with tempfile.TemporaryDirectory() as folder:
            result = monitor.watch_gui(command, Path(folder) / 'ready', timeout=20)
        self.assertEqual(result.code, 0)
        raw = (self.logs / 'gui-child.log').read_bytes()
        self.assertIn(b'BOOM-CRASH-ORIGINAL-LINE', raw)   # 原文逐字节保留，不转码

    def test_child_log_path_can_be_overridden_and_stale_content_kept(self):
        target = self.base / 'custom' / 'child.log'
        command = [sys.executable, '-u', '-c', 'import sys; print("second-run", flush=True)']
        with tempfile.TemporaryDirectory() as folder:
            monitor.watch_gui(command, Path(folder) / 'ready', timeout=20, child_log=target)
        self.assertIn('second-run', target.read_text(encoding='utf-8', errors='replace'))

    def test_log_rotation_keeps_one_backup(self):
        target = self.base / 'rot' / 'gui-child.log'
        target.parent.mkdir(parents=True)
        target.write_text('x' * 32, encoding='utf-8')
        self.assertTrue(monitor.rotate_log(target, limit=16))
        self.assertTrue(Path(str(target) + '.1').exists())
        self.assertFalse(target.exists())
        # 没超限时不动文件
        target.write_text('small', encoding='utf-8')
        self.assertFalse(monitor.rotate_log(target, limit=16))

    def test_unwritable_child_log_degrades_silently(self):
        blocker = self.base / 'not-a-dir'
        blocker.write_text('file', encoding='utf-8')
        self.assertIsNone(monitor.open_child_log(blocker / 'gui-child.log'))
        command = [sys.executable, '-u', '-c', 'print("still-works", flush=True)']
        with tempfile.TemporaryDirectory() as folder:
            result = monitor.watch_gui(command, Path(folder) / 'ready', timeout=20,
                                       child_log=blocker / 'gui-child.log')
        self.assertEqual(result.code, 0)


class CrashReportTests(_TempLogs):
    def test_nonzero_exit_writes_report_and_stage(self):
        command = [sys.executable, '-u', '-c',
                   'import sys; sys.stderr.write("kaboom\\n"); sys.exit(3)']
        with tempfile.TemporaryDirectory() as folder:
            result = monitor.watch_gui(command, Path(folder) / 'ready', timeout=20)
        self.assertEqual(result.code, 3)
        data = self.read_json()
        self.assertEqual(data['code'], 3)
        self.assertEqual(data['code_hex'], '0x00000003')
        self.assertFalse(data['ready'])
        self.assertIsNone(data['safe_mode'])          # 由 supervise 回填
        self.assertIsInstance(data['pid'], int)
        self.assertGreater(data['pid'], 0)          # 崩掉的是子进程的 pid
        self.assertTrue(data['gui_child_log'].endswith('gui-child.log'))
        self.assertTrue(data['qt_messages_log'].endswith('qt-messages.log'))
        self.assertIsInstance(data['stages'], list)
        self.assertTrue(data['time'] > 0)
        stages = (self.logs / 'startup-stages.jsonl').read_text(encoding='utf-8')
        self.assertIn('"stage": "gui-crash"', stages)
        self.assertIn('kaboom', (self.logs / 'gui-child.log').read_text(encoding='utf-8'))

    def test_zero_exit_writes_no_report(self):
        command = [sys.executable, '-u', '-c', 'print("ok")']
        with tempfile.TemporaryDirectory() as folder:
            monitor.watch_gui(command, Path(folder) / 'ready', timeout=20)
        self.assertFalse((self.logs / 'desktop-crash.json').exists())

    def test_safe_mode_is_filled_in_by_the_caller(self):
        monitor.record_gui_crash(0xC0000409 & 0xFFFFFFFF, pid=1234, ready=True)
        self.assertIsNone(self.read_json()['safe_mode'])
        self.assertTrue(monitor.stamp_crash_safe_mode(True))
        data = self.read_json()
        self.assertTrue(data['safe_mode'])
        self.assertEqual(data['code_hex'], '0xC0000409')

    def test_stale_report_is_not_rewritten(self):
        monitor.record_gui_crash(3, pid=1, ready=False)
        data = self.read_json()
        data['time'] = 0
        (self.logs / 'desktop-crash.json').write_text(json.dumps(data), encoding='utf-8')
        self.assertFalse(monitor.stamp_crash_safe_mode(True))
        self.assertFalse(self.read_json()['safe_mode'])

    def test_supervise_stamps_the_report_it_just_produced(self):
        monitor.record_gui_crash(0xC0000409 & 0xFFFFFFFF, pid=1, ready=True)
        attempt = Mock(return_value=monitor.GuiResult(0xC0000409, True))
        fallback = Mock(return_value=9)
        with patch.object(monitor, 'stage'):
            self.assertEqual(monitor.supervise(attempt, fallback, safe_mode=True,
                                               is_native_crash=lambda code: True), 9)
        self.assertTrue(self.read_json()['safe_mode'])

    def test_report_is_silent_when_directory_is_unwritable(self):
        blocker = self.base / 'blocked'
        blocker.write_text('file', encoding='utf-8')
        payload = monitor.record_gui_crash(5, pid=9, directory=blocker)
        self.assertEqual(payload['code'], 5)          # 返回值仍然可用，不抛异常


class QtMessageHandlerTests(_TempLogs):
    def setUp(self):
        super().setUp()
        try:
            from PyQt6.QtCore import qInstallMessageHandler
        except Exception:                        # pragma: no cover
            self.skipTest('环境缺 PyQt6')
        self.addCleanup(qInstallMessageHandler, None)      # 先加的后跑：最后恢复 Qt 默认
        self.addCleanup(self._close_stream)

    @staticmethod
    def _close_stream():
        stream = gui_launcher._QT_LOG_STREAM
        try:
            if hasattr(stream, 'close'):
                stream.close()
        except Exception:
            pass
        gui_launcher._QT_LOG_STREAM = None

    def test_qt_warning_and_fatal_text_are_appended_with_level(self):
        self.assertTrue(gui_launcher.install_qt_message_handler(self.logs))
        from PyQt6.QtCore import QtMsgType, qWarning
        qWarning('PROBE-WARNING-LINE')
        handler = gui_launcher._QT_HANDLER
        handler(QtMsgType.QtFatalMsg, None, 'PROBE-FATAL-LINE')
        text = (self.logs / 'qt-messages.log').read_text(encoding='utf-8')
        self.assertIn('[Qt/WARNING] PROBE-WARNING-LINE', text)
        self.assertIn('[Qt/FATAL] PROBE-FATAL-LINE', text)

    def test_handler_swallows_its_own_write_errors(self):
        self.assertTrue(gui_launcher.install_qt_message_handler(self.logs))
        self._close_stream()
        broken = Mock()
        broken.write.side_effect = OSError('disk full')
        gui_launcher._QT_LOG_STREAM = broken
        from PyQt6.QtCore import QtMsgType
        gui_launcher._QT_HANDLER(QtMsgType.QtFatalMsg, None, 'x')   # 不许抛出去

    def test_logfile_directory_failure_returns_false(self):
        blocker = self.base / 'blocked'
        blocker.write_text('file', encoding='utf-8')
        self.assertFalse(gui_launcher.install_qt_message_handler(blocker))


class FallbackMessageTests(_TempLogs):
    def test_crash_explanation_is_chinese_and_exact(self):
        self.assertEqual(desktop.crash_explanation(0xC0000409),
                         '界面组件异常退出（多半是线程或绘制相关的原生错误）')
        self.assertEqual(desktop.crash_explanation(3221226505),
                         '界面组件异常退出（多半是线程或绘制相关的原生错误）')
        self.assertIn('非正常', desktop.crash_explanation(3))
        self.assertEqual(desktop.crash_explanation(0), '正常退出')
        self.assertEqual(desktop.hex_code(3221226505), '0xC0000409')

    def test_text_carries_code_log_paths_and_next_step(self):
        details = {'code': 0xC0000409 & 0xFFFFFFFF,
                   'gui_child_log': r'C:\logs\gui-child.log',
                   'qt_messages_log': r'C:\logs\qt-messages.log'}
        text = desktop.startup_failure_text('桌面窗口未能正常启动', details=details, logs=self.logs)
        self.assertIn('0xC0000409', text)
        self.assertIn('界面组件异常退出', text)
        self.assertIn(r'C:\logs\gui-child.log', text)
        self.assertIn(r'C:\logs\qt-messages.log', text)
        self.assertIn('以兼容模式重试', text)

    def test_text_without_report_still_names_the_log_files(self):
        text = desktop.startup_failure_text('桌面窗口未能正常启动', details={}, logs=self.logs)
        self.assertIn('没有拿到窗口进程的退出码', text)
        self.assertIn(str(self.logs / 'gui-child.log'), text)
        self.assertIn(str(self.logs / 'qt-messages.log'), text)

    def test_yes_answer_retries_in_safe_mode_and_no_answer_does_not(self):
        with patch.object(desktop, 'stage'), \
             patch.object(desktop, 'retry_in_safe_mode') as retry:
            self.assertEqual(desktop.failed('启动失败', messagebox=Mock(return_value=6)), 1)
            retry.assert_called_once_with()
            retry.reset_mock()
            self.assertEqual(desktop.failed('启动失败', messagebox=Mock(return_value=7)), 1)
            retry.assert_not_called()

    def test_message_box_failure_never_breaks_the_return_value(self):
        def boom(text):
            raise OSError('no user32')
        with patch.object(desktop, 'stage'):
            self.assertEqual(desktop.failed('启动失败', messagebox=boom), 1)

    def test_retry_command_uses_safe_mode(self):
        with patch('subprocess.Popen') as popen:
            self.assertTrue(desktop.retry_in_safe_mode())
            command = popen.call_args.args[0]
            self.assertIn('--safe-mode', command)
            self.assertEqual(command[0], sys.executable)
        with patch('subprocess.Popen', side_effect=OSError('nope')):
            self.assertFalse(desktop.retry_in_safe_mode())


if __name__ == '__main__':
    unittest.main()
