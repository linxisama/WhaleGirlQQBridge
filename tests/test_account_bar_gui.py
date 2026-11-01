"""多账号切片 1 的 GUI 测试：账号栏渲染 / 切换调用 / 新建走接口。

用离屏 + 猴补 api_get/api_post，不启动后台、不碰真实用户数据。
"""
import os
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import qq_gui  # noqa: E402


class AccountBarTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.accounts = {'items': [{'id': 'default', 'label': '默认账号'},
                                   {'id': 'abc123', 'label': '小号'}],
                         'active': 'default'}
        self._orig_get, self._orig_post = qq_gui.api_get, qq_gui.api_post

        def fake_get(path, *args, **kwargs):
            self.calls.append(('get', path))
            if path == 'accounts':
                return dict(self.accounts)
            return {}

        def fake_post(path, payload=None, *args, **kwargs):
            self.calls.append(('post', path, payload))
            if path == 'accounts/select':
                return {'success': True, 'active': (payload or {}).get('id')}
            if path == 'accounts':
                return {'success': True, 'item': {'id': 'new1', 'label': '新账号'}}
            return {'success': True}

        qq_gui.api_get, qq_gui.api_post = fake_get, fake_post
        self.addCleanup(setattr, qq_gui, 'api_get', self._orig_get)
        self.addCleanup(setattr, qq_gui, 'api_post', self._orig_post)
        self.window = qq_gui.MainWindow()
        self.addCleanup(self.window.close)
        # 切片 2：切换会通过 8090 启动该账号后台并切端口。测试里不打真网络：
        self.window._hub_request = lambda method, path, payload=None, timeout=60: (
            self.calls.append((method, path, payload)) or {'success': True, 'server': {'port': 8090}})
        self.window._point_at = lambda account_id, port: True

    def test_account_bar_lists_accounts(self):
        self.window._reload_accounts()
        self.assertEqual(self.window.account_combo.count(), 2)
        self.assertEqual(self.window.account_combo.currentData(), 'default')
        self.assertTrue(self.window.account_remove.isEnabled())

    def test_single_account_cannot_be_deleted(self):
        self.accounts = {'items': [{'id': 'default', 'label': '默认账号'}], 'active': 'default'}
        self.window._reload_accounts()
        self.assertFalse(self.window.account_remove.isEnabled())

    def test_switch_calls_select_and_refreshes(self):
        self.window._reload_accounts()
        index = self.window.account_combo.findData('abc123')
        self.window.account_combo.setCurrentIndex(index)
        self.window._on_account_chosen(index)
        self.assertIn(('post', 'accounts/select', {'id': 'abc123'}), self.calls)
        self.assertIn('已切换到', self.window.account_hint.text())


if __name__ == '__main__':
    unittest.main()
