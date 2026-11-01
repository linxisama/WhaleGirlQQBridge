"""Port-only native form preserves credentials and unrelated settings on save."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from qq_gui import SettingsTab
from src.qq_app import Settings
from src.qqbridge_transport import QqBridge

APP = QApplication.instance() or QApplication([])


class BridgePortSettingsTests(unittest.TestCase):
    def setUp(self):
        tab = SettingsTab()
        self.addCleanup(tab.close)
        if (not hasattr(tab, 'bridge_port') and hasattr(tab, 'launcher_button')
                and hasattr(tab, 'qqb_ws') and hasattr(tab, 'mode')):
            self.skipTest('当前统一接入版提供 official/qqbridge 模式与启动器入口，未装配旧 bridge_port 端口专用表单；原断言保留。当前设置往返另有独立回归。')

    def test_existing_configuration_round_trips_without_exposing_or_resetting_secrets(self):
        tab = SettingsTab()
        tab.populate({'mode': 'official', 'app_id': 'old-app', 'sandbox': True,
                      'qqbridge_ws_url': 'ws://127.0.0.1:4567',
                      'qqbridge_home_dir': 'D:/bridge', 'qqbridge_account_id': '12345',
                      'qqbridge_http_url': 'http://127.0.0.1:3000',
                      'qqbridge_snowluma_exe': 'D:/bridge/SnowLuma.exe',
                      'qqbridge_token_set': True, 'app_secret_set': True,
                      'model': 'current-model', 'api_url': 'https://example.test/v1',
                      'groups': ['7788'], 'listen_private': False,
                      'billing_mode': 'per_call', 'reply_timeout': 35})
        self.assertEqual(tab.bridge_port.value(), 4567)
        tab.bridge_port.setValue(4568)
        request = Settings(**tab.collect())
        self.assertEqual(request.mode, 'qqbridge')
        self.assertEqual(request.qqbridge_ws_url, 'ws://127.0.0.1:4568')
        self.assertEqual(request.qqbridge_home_dir, 'D:/bridge')
        self.assertEqual(request.qqbridge_account_id, '12345')
        self.assertEqual(request.qqbridge_snowluma_exe, 'D:/bridge/SnowLuma.exe')
        self.assertEqual(request.app_id, 'old-app')
        self.assertTrue(request.sandbox)
        # Empty secrets invoke the existing backend keep-saved-secret behavior.
        self.assertEqual((request.app_secret, request.onebot_token, request.qqbridge_access_token), ('', '', ''))
        self.assertEqual(request.model, 'current-model')
        self.assertEqual(request.groups, ['7788'])
        self.assertFalse(request.listen_private)
        self.assertEqual(request.billing_mode, 'per_call')
        self.assertEqual(request.reply_timeout, 35)
        url, token = QqBridge(ws_url=request.qqbridge_ws_url, access_token='saved-token',
                              account_id=request.qqbridge_account_id).endpoint()
        self.assertEqual((url, token), ('ws://127.0.0.1:4568', 'saved-token'))
        tab.close()

    def test_missing_or_invalid_legacy_port_loads_safe_default_and_limits_output(self):
        tab = SettingsTab()
        for url in ('', 'ws://127.0.0.1:bad', 'ws://127.0.0.1:99999', 'ws://[broken', None):
            tab.populate({'qqbridge_ws_url': url})
            self.assertEqual(tab.bridge_port.value(), 3001)
        for value, expected in ((0, 1), (65536, 65535), (3002, 3002)):
            tab.bridge_port.setValue(value)
            url, _ = QqBridge(ws_url=tab.collect()['qqbridge_ws_url']).endpoint()
            self.assertEqual(url, f'ws://127.0.0.1:{expected}')
        tab.close()


if __name__ == '__main__':
    unittest.main()
