"""原生崩溃接管与无桌面窗口入口；不连接 QQ、不改真实配置。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gui_launcher
from fastapi.testclient import TestClient
from src import compat_console as compat
from src import startup_supervisor as monitor
from src.runtime_identity import APP_ID, APP_VERSION


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.object(monitor, 'stage')
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def run_plan(self, results, safe=False):
        attempt = Mock(side_effect=results)
        fallback = Mock(return_value=7)
        result = monitor.supervise(attempt, fallback, safe_mode=safe,
            is_native_crash=lambda code: gui_launcher.crash_recovery_plan(code, False) == 'relaunch-safe')
        return result, attempt, fallback

    def test_normal_close_does_not_restart_or_open_browser(self):
        result, attempt, fallback = self.run_plan([monitor.GuiResult(0, True)])
        self.assertEqual(result, 0)
        attempt.assert_called_once_with(False)
        fallback.assert_not_called()

    def test_native_crash_retries_safe_mode_once(self):
        result, attempt, fallback = self.run_plan([
            monitor.GuiResult(0xC0000005, False), monitor.GuiResult(0, True)])
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in attempt.call_args_list], [False, True])
        fallback.assert_not_called()

    def test_two_native_failures_call_terminal_failure_without_loop(self):
        result, attempt, fallback = self.run_plan([monitor.GuiResult(0xC0000005, True)] * 2)
        self.assertEqual(result, 7)
        self.assertEqual(attempt.call_count, 2)
        fallback.assert_called_once()

    def test_already_safe_mode_reports_after_one_failure(self):
        result, attempt, fallback = self.run_plan([monitor.GuiResult(-1073741819, False)], True)
        self.assertEqual(result, 7)
        attempt.assert_called_once_with(True)
        fallback.assert_called_once()

    def test_hung_startup_is_bounded(self):
        result, attempt, fallback = self.run_plan([monitor.GuiResult(1, False, True)] * 2)
        self.assertEqual(attempt.call_count, 2)
        fallback.assert_called_once()

    def test_python_error_does_not_repeat_same_configuration(self):
        _, attempt, fallback = self.run_plan([monitor.GuiResult(1, False)])
        attempt.assert_called_once()
        fallback.assert_called_once()

    def test_real_child_native_exit_code_can_be_observed_outside_process(self):
        with tempfile.TemporaryDirectory() as folder:
            command = ([sys.executable, '-c', 'import ctypes; ctypes.windll.kernel32.ExitProcess(0xC0000005)']
                       if os.name == 'nt' else [sys.executable, '-c', 'import os; os._exit(42)'])
            result = monitor.watch_gui(command, Path(folder) / 'ready', timeout=5)
            self.assertEqual(result.code, 0xC0000005 if os.name == 'nt' else 42)
            self.assertFalse(result.ready)

    def test_real_hung_child_is_terminated(self):
        with tempfile.TemporaryDirectory() as folder:
            result = monitor.watch_gui([sys.executable, '-c', 'import time; time.sleep(60)'],
                                       Path(folder) / 'ready', timeout=.2)
            self.assertTrue(result.timed_out)
            self.assertNotEqual(result.code, 0)

    def test_real_ready_child_exits_cleanly_and_marker_is_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / 'ready'
            code = ('import os,time; from pathlib import Path; '
                    'Path(os.environ["WHALEGIRL_GUI_READY_FILE"]).write_text("ready"); time.sleep(.3)')
            result = monitor.watch_gui([sys.executable, '-c', code], ready, timeout=5)
            self.assertEqual(result.code, 0)
            self.assertTrue(result.ready)
            self.assertFalse(ready.exists())

    def test_ready_window_is_not_killed_when_startup_deadline_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / 'ready'
            proc = Mock()
            proc.poll.side_effect = [None, None, 0]
            def start(*args, **kwargs):
                ready.write_text('ready')
                return proc
            terminate = Mock()
            result = monitor.watch_gui(['fake'], ready, timeout=1, popen=start,
                monotonic=Mock(side_effect=[0, 60, 120]), sleep=Mock(), terminate=terminate)
            self.assertTrue(result.ready)
            terminate.assert_not_called()


class CompatEntryTests(unittest.TestCase):
    def test_incomplete_history_cannot_break_status_or_change_stored_data(self):
        with tempfile.TemporaryDirectory() as folder:
            app = compat.prepare_app(Path(folder))
            with TestClient(app) as client:
                bot = app.state.bot
                payloads = ('{}', '[]', 'not-valid-json')
                for index, payload in enumerate(payloads):
                    bot.db.execute("INSERT INTO inbox(key,mode,account,room,payload,state,reply,error,created) VALUES(?,?,?,?,?,'sent','keep-reply','',?)",
                                   (str(index), 'official', 'fixture', 'private:u', payload, index))
                bot.db.commit()
                response = client.get('/api/status')
                self.assertEqual(response.status_code, 200)
                messages = response.json()['messages']
                self.assertEqual(len(messages), 3)
                self.assertTrue(all(item['sender'] == '未知发送者' for item in messages))
                self.assertTrue(all(item['reply'] == 'keep-reply' for item in messages))
                stored = tuple(row[0] for row in bot.db.execute('SELECT payload FROM inbox ORDER BY created'))
                self.assertEqual(stored, payloads)

    def test_backend_entry_does_not_start_gui_supervisor(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(gui_launcher, '_logs_dir', return_value=Path(folder)), \
             patch.object(sys, 'argv', ['app', '--backend-only']), \
             patch('src.compat_console.main', return_value=0) as main, \
             patch('src.startup_supervisor.supervise', side_effect=AssertionError('不应启动 Qt')):
            self.assertEqual(gui_launcher.main(), 0)
            main.assert_called_once_with(open_browser=False)

    def test_legacy_browser_entry_now_uses_desktop_supervisor(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(gui_launcher, '_logs_dir', return_value=Path(folder)), \
             patch.object(sys, 'argv', ['app', '--browser-mode']), \
             patch('src.compat_console.main', side_effect=AssertionError('不能打开网页')) as main, \
             patch('src.startup_supervisor.supervise', return_value=0) as supervisor:
            gui_launcher.main()
            main.assert_not_called()
            supervisor.assert_called_once()
            self.assertNotIn('--browser-mode', sys.argv)

    def test_existing_matching_backend_is_reused_without_second_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            identity = dict(app=APP_ID, version=APP_VERSION, data_root=str(root))
            with patch.object(compat, 'runtime_paths', return_value=(root, root)), \
                 patch.object(compat, 'probe', return_value=identity), \
                 patch.object(compat.webbrowser, 'open', return_value=True) as opened, \
                 patch.object(compat, 'prepare_app') as build:
                self.assertEqual(compat.main(), 0)
                opened.assert_called_once_with(compat.URL)
                build.assert_not_called()

    def test_foreign_backend_is_not_opened_or_terminated(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(compat, 'runtime_paths', return_value=(Path(folder), Path(folder))), \
             patch.object(compat, 'probe', return_value={'app': 'OtherApp'}), \
             patch.object(compat, 'inform') as inform, \
             patch.object(compat.webbrowser, 'open') as opened, \
             patch.object(compat, 'prepare_app') as build:
            self.assertEqual(compat.main(), 1)
            inform.assert_called_once()
            opened.assert_not_called()
            build.assert_not_called()

    def test_frozen_resources_seed_missing_only_and_keep_existing_persona(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            bundle = base / 'bundle'
            (bundle / 'styles').mkdir(parents=True)
            (bundle / 'styles/default.yaml').write_text('new bundled default')
            root = base / 'profile/WhaleGirlQQ'
            (root / 'styles').mkdir(parents=True)
            (root / 'styles/default.yaml').write_text('existing custom content')
            with patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, '_MEIPASS', str(bundle), create=True), \
                 patch.dict(os.environ, LOCALAPPDATA=str(base / 'profile')):
                actual, _ = compat.runtime_paths()
            self.assertEqual(actual, root)
            self.assertEqual((root / 'styles/default.yaml').read_text(), 'existing custom content')

    def test_fresh_compat_backend_import_does_not_import_qt_or_create_application(self):
        repo = Path(__file__).resolve().parents[1]
        code = '''
import sys, tempfile
from pathlib import Path
from src.compat_console import prepare_app
from fastapi.testclient import TestClient
with tempfile.TemporaryDirectory() as folder:
    app=prepare_app(Path(folder))
    assert not any(name.startswith('PyQt6') for name in sys.modules), [n for n in sys.modules if n.startswith('PyQt6')]
    with TestClient(app) as client:
        data=client.get('/api/runtime').json()
        assert data['console_mode']=='browser'
        assert client.get('/').status_code==200
        assert client.get('/qq.js').status_code==200
        assert client.get('/api/plugins').status_code==200
        assert client.get('/api/status').status_code==200
        assert not any(name.startswith('PyQt6') for name in sys.modules)
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=repo, capture_output=True,
                                timeout=30, creationflags=0x08000000 if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')[-2000:])


if __name__ == '__main__':
    unittest.main()
