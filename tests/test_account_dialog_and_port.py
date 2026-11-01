"""新账号窗口 + 端口 27499 + 弹窗换肤的回归测试。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication, QDialog, QLabel  # noqa: E402

APP = QApplication.instance() or QApplication([])

import qq_gui  # noqa: E402


class AccountDialogTests(unittest.TestCase):
    def test_defaults(self):
        dialog = qq_gui.AccountCreateDialog([{'id': 'default', 'label': '默认账号'}])
        self.assertEqual(dialog.label_edit.text(), '新账号')
        self.assertEqual(dialog.source_combo.currentData(), 'default')
        self.assertTrue(dialog.switch_check.isChecked())
        dialog.close()

    def test_values_carry_clone_source(self):
        dialog = qq_gui.AccountCreateDialog([
            {'id': 'default', 'label': '默认账号'}, {'id': 'abc', 'label': '小号'}])
        dialog.label_edit.setText('测试号')
        dialog.source_combo.setCurrentIndex(1)
        dialog.switch_check.setChecked(False)
        self.assertEqual(dialog.values(),
                         {'label': '测试号', 'clone_from': 'abc', 'switch': False})
        dialog.close()


class PortTests(unittest.TestCase):
    """PORT 是导入期由 resolve_port() 算出来的，所以不写死 27499：
    只跟"同一条解析链在同一个环境里再算一次"的结果比，shell 里 export 了
    WHALEGIRL_PORT 也不会红。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.empty_config = Path(tmp.name) / 'config.qq.yaml'
        self.empty_config.write_text('', encoding='utf-8')

    def test_module_port_and_url_follow_resolver(self):
        self.assertEqual(qq_gui.PORT,
                         qq_gui.resolve_port(argv=[], environ=os.environ,
                                             config_path=qq_gui.ROOT / 'config.qq.yaml'))
        self.assertEqual(qq_gui.BASE_URL, 'http://127.0.0.1:%d' % qq_gui.PORT)

    def test_documented_default_is_27499_when_nothing_set(self):
        self.assertEqual(qq_gui.DEFAULT_CONSOLE_PORT, 27499)
        self.assertEqual(qq_gui.resolve_port(argv=[], environ={}, config_path=self.empty_config),
                         27499)

    def test_onebot_hint_shows_console_port(self):
        # 提示文案在设置页（qq_gui.py:1431），不在 ConsoleTab；以前扫 ConsoleTab 的按钮
        # 永远扫不到 onebot，条件不成立就静默空过了。
        tab = qq_gui.SettingsTab()
        self.addCleanup(tab.close)
        joined = ' '.join(widget.text() for widget in tab.findChildren(QLabel))
        self.assertIn('/qq/onebot', joined)
        self.assertIn('ws://127.0.0.1:%d/qq/onebot' % qq_gui.PORT, joined)


class AddAccountFlowTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.window = qq_gui.MainWindow()
        self.addCleanup(self.window.close)

    def test_dialog_cancel_does_not_post(self):
        def fake_hub(method, path, payload=None, timeout=60):
            self.calls.append((method, path, payload))
            return {'success': True, 'items': [{'id': 'default', 'label': '默认账号'}]}

        self.window._hub_request = fake_hub
        with patch.object(qq_gui.AccountCreateDialog, 'exec',
                          return_value=QDialog.DialogCode.Rejected):
            self.window._add_account()
        self.assertEqual([call for call in self.calls if call[0] == 'post'], [])

    def test_dialog_accept_posts_with_clone_from(self):
        posted = []

        def fake_hub(method, path, payload=None, timeout=60):
            if method == 'post':
                posted.append((path, payload))
                return {'success': True, 'item': {'id': 'new1', 'label': '测试'}}
            return {'items': [{'id': 'default', 'label': '默认账号'}]}

        self.window._hub_request = fake_hub
        self.window._reload_accounts = lambda: None
        self.window._on_account_chosen = lambda *args: None
        with patch.object(qq_gui.AccountCreateDialog, 'exec',
                          return_value=QDialog.DialogCode.Accepted), \
             patch.object(qq_gui.AccountCreateDialog, 'values',
                          return_value={'label': '测试', 'clone_from': 'default', 'switch': True}):
            self.window._add_account()
        self.assertEqual(posted[0][0], 'accounts')
        self.assertEqual(posted[0][1]['clone_from'], 'default')


if __name__ == '__main__':
    unittest.main()
