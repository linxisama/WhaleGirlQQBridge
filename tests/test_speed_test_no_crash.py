"""模型测速不能再因为把输入框当组合框用而崩（PyQt 槽里异常会 abort）。"""
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
import qq_gui  # noqa: E402


class SpeedTestTests(unittest.TestCase):
    def setUp(self):
        self.window = qq_gui.MainWindow()
        self.addCleanup(self.window.close)
        self.tab = self.window.settings_tab
        self.tab.populate({'model': 'glm-5.3-flashx', 'api_url': 'https://x', 'api_key_set': True})

    def test_speed_test_uses_picker_and_does_not_raise(self):
        self.tab.model_pick.addItems(['glm-5.3-flashx', 'deepseek-chat'])
        self.tab.model_pick.show()
        captured = {}

        def fake_start(path, payload, button):
            captured['path'] = path
            captured['models'] = payload.get('models')
            return False          # 不再真的起线程

        with patch.object(self.window, '_start_probe', fake_start):
            self.window._speed_test_ai()      # 以前这里 AttributeError → 闪退
        self.assertEqual(captured['path'], 'ai/model-speed')
        self.assertIn('deepseek-chat', captured['models'])

    def test_speed_ready_handles_empty_results(self):
        self.window._speed_ready({'results': [], 'message': '都超时了'})
        self.assertIn('超时', self.tab.probe_lbl.text())


if __name__ == '__main__':
    unittest.main()
