"""背景图入口：只保留系统文件框（应用内画廊已下线）。"""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton  # noqa: E402

APP = QApplication.instance() or QApplication([])


class BackgroundPickerTests(unittest.TestCase):
    def test_pick_image_uses_native_file_dialog(self):
        import qq_gui
        tab = qq_gui.AppearanceTab()
        with mock.patch.object(qq_gui.file_dialogs, "open_file",
                               return_value=("", "")) as open_file:
            tab._pick_image()
        open_file.assert_called_once()
        self.assertIs(open_file.call_args.kwargs.get("force_light"), False,
                      "所有文件导入默认使用系统文件框")
        self.assertTrue(str(open_file.call_args.args[2]).endswith("Pictures"))

    def test_gallery_entry_is_gone(self):
        import qq_gui
        tab = qq_gui.AppearanceTab()
        labels = [btn.text() for btn in tab.findChildren(QPushButton)]
        self.assertIn("选择其他图片…", labels, "系统文件框入口必须保留")
        self.assertIn("清除背景图", labels, "清除背景图必须保留")
        self.assertFalse(hasattr(tab, "_pick_gallery"), "应用内画廊入口方法已下线")
        self.assertEqual([t for t in labels if "图片" in t], ["选择其他图片…"],
                         "背景图入口只剩系统文件框这一个")


if __name__ == "__main__":
    unittest.main()
