"""LOCALAPPDATA 不可用时启动器必须给出可照做的消息，而不是 WinError 栈。"""
import contextlib
import io
import os
import unittest
from unittest import mock


class LauncherEnvironmentErrorTest(unittest.TestCase):
    def test_unusable_localappdata_reports_variable_not_traceback(self):
        import gui_launcher

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': 'Z:\\'}, clear=False), \
                mock.patch.object(gui_launcher.sys, 'argv', ['gui_launcher.py', '--backend-only']), \
                mock.patch('src.desktop_runtime.interactive_session', return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gui_launcher.main()
        text = err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn('LOCALAPPDATA', text)
        self.assertIn('Z:\\', text)
        self.assertNotIn('Traceback', text)

    def test_gui_mode_still_reports_through_message_box(self):
        import gui_launcher

        shown = []
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': 'Z:\\'}, clear=False), \
                mock.patch.object(gui_launcher.sys, 'argv', ['gui_launcher.py']), \
                mock.patch('src.desktop_runtime.interactive_session', return_value=True), \
                mock.patch('src.desktop_runtime.message_box', lambda text, *a, **k: shown.append(text) or 7), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = gui_launcher.main()
        self.assertEqual(code, 1)
        self.assertEqual(len(shown), 1)
        self.assertIn('LOCALAPPDATA', shown[0])


if __name__ == '__main__':
    unittest.main()
