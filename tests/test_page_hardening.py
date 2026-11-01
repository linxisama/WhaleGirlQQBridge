"""把本轮页面加固写成回归测试（坏数据只提示、正常数据照常显示）。"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import qq_gui  # noqa: E402
from src.gui import diagnostics  # noqa: E402
from src.gui.conversation import PcTab  # noqa: E402
from src.gui.stickers import StickerLibraryDialog  # noqa: E402

APP = QApplication.instance() or QApplication([])


class PageHardeningTests(unittest.TestCase):
    """第三轮审计（页面）发现的问题：坏数据不能让整块空白/弹窗，正常数据必须能显示。"""

    @classmethod
    def setUpClass(cls):
        cls.app = APP
        diagnostics.install(qq_gui._logs_dir(), lambda title, message: None)

    # ── 控制台 ─────────────────────────────────────────────────────────────
    def test_console_shows_normal_data(self):
        from unittest import mock
        with mock.patch.object(qq_gui.MainWindow, '_prepare_backend', lambda self: None):
            window = qq_gui.MainWindow()
        try:
            window.console_tab.refresh({
                "running": True, "mode": "onebot", "model": "TM ·DeepSeek-V4",
                "onebot": {"online": True, "self_id": "10001", "error": ""},
                "messages": [{"time": 1760000000, "state": "sent", "sender": "西莲寺紫",
                              "text": "在吗", "reply": "在的", "kind": "group", "target": "123456"}],
                "events": [{"time": 1760000000, "text": "机器人已启动"}],
            })
            self.assertIn("在吗", window.console_tab.msg_view.toPlainText())
            self.assertIn("机器人已启动", window.console_tab.evt_view.toPlainText())
        finally:
            window.hang_watchdog._stop = True
            window.hang_watchdog._timer.stop()
            window.close()

    def test_console_survives_bad_shapes(self):
        from unittest import mock
        with mock.patch.object(qq_gui.MainWindow, '_prepare_backend', lambda self: None):
            window = qq_gui.MainWindow()
        try:
            for bad in ([], {}, {"messages": None, "events": None},
                        {"official": None, "mode": "official"},
                        {"messages": [{"state": {"x": 1}}], "events": ["字符串"]},
                        {"messages": [{"time": "不是时间", "text": None, "sender": None}]}):
                window.console_tab.refresh(bad)          # 不抛异常即通过
        finally:
            window.hang_watchdog._stop = True
            window.hang_watchdog._timer.stop()
            window.close()

    # ── 设置页 ─────────────────────────────────────────────────────────────
    def test_settings_shows_normal_data_and_survives_bad(self):
        tab = qq_gui.SettingsTab()
        tab.populate({"mode": "onebot", "groups": ["123", " 456 "], "reply_timeout": 25,
                      "model": "m", "api_url": "https://x.test/v1/chat/completions"})
        self.assertEqual(len(tab.groups.toPlainText().splitlines()), 2)
        self.assertEqual(tab.sp_reply_timeout.value(), 25)
        for bad in (None, [], {}, {"groups": None, "reply_timeout": "abc"},
                    {"groups": [1, 2], "model": {"a": 1}}, {"api_url": 123, "reply_timeout": [20]}):
            tab.populate(bad)

    # ── 机器人管理页 ───────────────────────────────────────────────────────
    def test_pc_tab_shows_normal_data_and_survives_bad(self):
        tab = PcTab(lambda path: {}, lambda path, payload: {"success": True}, lambda pid: None)
        tab.get = lambda path: {
            "success": True, "admin": {"password_set": True, "minutes": 30, "active": 1},
            "robot": {"running": True, "queue": {"pending": 2}, "plugins": [{"enabled": True}]},
            "jobs": [{"id": "j1", "action": "pause", "arg": "", "state": "ready", "result": ""}],
            "candidates": [{"id": "10001", "name": "西莲寺紫"}]}
        tab.refresh()
        self.assertEqual(tab.table.rowCount(), 1)
        self.assertIn("运行中", tab.summary.text())
        for bad in (None, [], {}, {"success": True, "robot": None},
                    {"success": True, "jobs": None, "candidates": None,
                     "admin": {"minutes": "abc"}},
                    {"success": True, "robot": {"queue": None, "plugins": None}}):
            tab.get = lambda path, payload=bad: payload
            tab.refresh()

    # ── 表情库对话框 ───────────────────────────────────────────────────────
    def test_sticker_dialog_survives_bad_and_shows_normal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dialog = StickerLibraryDialog(tmp, lambda path: {"items": [], "pages": 1, "total": 0},
                                          lambda path, payload: {"success": True}, lambda *args: None)
            for bad in ([], {}, {"items": None}, {"items": [{"label": "x"}]},
                        {"items": ["字符串"], "pages": "x"},
                        {"items": [{"label": "ok", "thumbnail": 123}], "total": "x"}):
                dialog.get = lambda path, payload=bad: payload
                dialog.refresh()                     # 以前 :70 会因"判空后又 .get"二次崩
            dialog.get = lambda path: {"items": [{"id": "1", "label": "猫猫", "uses": 3,
                                                  "thumbnail": ""}], "pages": 2, "total": 5}
            dialog.refresh()
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertIn("2 页", dialog.page_label.text())


if __name__ == "__main__":
    unittest.main()
