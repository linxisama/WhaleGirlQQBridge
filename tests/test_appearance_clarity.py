"""背景按设备像素比缩放（避免 150% 缩放发虚）+ 文件对话框默认走系统原生。"""
import unittest

from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

from src.gui.config import DEFAULTS


class AppearanceClarityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_scaled_from_uses_device_ratio(self):
        from qq_gui import BackgroundFrame
        pixmap = QPixmap(100, 100)
        scaled = BackgroundFrame._scaled_from(pixmap, 200, 100, 2.0)
        self.assertGreaterEqual(max(scaled.width(), scaled.height()), 380)

    def test_scaled_pixmap_caches_per_ratio(self):
        from qq_gui import BackgroundFrame
        frame = BackgroundFrame.__new__(BackgroundFrame)
        frame._pixmap = QPixmap(400, 400)
        frame._scaled = None
        frame._scaled_size = None
        frame.devicePixelRatioF = lambda: 1.5
        first = frame._scaled_pixmap(200, 200)
        self.assertAlmostEqual(first.devicePixelRatio(), 1.5, places=2)
        again = frame._scaled_pixmap(200, 200)
        self.assertIs(first, again, '同尺寸同比例必须命中缓存')

    def test_native_dialogs_default_on(self):
        self.assertTrue(DEFAULTS.get('native_file_dialogs', True))


if __name__ == '__main__':
    unittest.main()
