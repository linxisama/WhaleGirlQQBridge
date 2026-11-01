"""设置页要能选 OneBot，且保存时不能把已选模式改写掉（以前 onebot 会被存成 qqbridge）。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from qq_gui import SettingsTab
from src.qq_app import Settings

APP = QApplication.instance() or QApplication([])


class SettingsOneBotModeTests(unittest.TestCase):
    def setUp(self):
        self.tab = SettingsTab()
        self.addCleanup(self.tab.close)

    def test_combo_offers_onebot(self):
        self.assertGreaterEqual(self.tab.mode.findData('onebot'), 0)

    def test_populate_keeps_onebot_selected(self):
        self.tab.populate({'mode': 'onebot', 'onebot_token_set': True})
        self.assertEqual(self.tab.mode.currentData(), 'onebot')
        self.assertTrue(self.tab.onebot_frame.isVisibleTo(self.tab))
        self.assertFalse(self.tab.official_frame.isVisibleTo(self.tab))

    def test_populate_qqbridge_hides_onebot_panel(self):
        self.tab.populate({'mode': 'qqbridge'})
        self.assertEqual(self.tab.mode.currentData(), 'qqbridge')
        self.assertFalse(self.tab.onebot_frame.isVisibleTo(self.tab))

    def test_collect_sends_onebot_mode_and_token(self):
        self.tab.populate({'mode': 'onebot'})
        self.tab.onebot_token.setText('  relay-token-123  ')
        request = Settings(**self.tab.collect())
        self.assertEqual(request.mode, 'onebot')
        self.assertEqual(request.onebot_token, 'relay-token-123')

    def test_unknown_mode_falls_back_to_official(self):
        self.tab.populate({'mode': 'bogus-mode'})
        self.assertEqual(self.tab.mode.currentData(), 'official')


if __name__ == '__main__':
    unittest.main()
