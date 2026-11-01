"""插件市场界面的测试：安装弹窗的安全闸门 + 删除按钮的授权边界。

安全侧重点：
- 「确认安装」只有在预检通过后才可用（检查不通过时用户无法绕过）；
- 点安装必须再弹一次二次确认；
- 删除按钮**只对自定义插件**出现（内置插件连按钮都没有）。
"""

import copy
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QPushButton
    import qq_gui as gui

    APP = QApplication.instance() or QApplication([])
    HAS_QT = True
except Exception:
    HAS_QT = False


def wait_api(widget, timeout: float = 5.0) -> bool:
    """等后台接口任务跑完（这些按钮走 QThread，避免主线程被下载/安装卡死）。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        APP.processEvents()
        if not getattr(widget, "_api_tasks", []):
            APP.processEvents()
            return True
        time.sleep(0.01)
    return False


def plugin(plugin_id, name="插件", enabled=False, schema=None):
    return {"id": plugin_id, "name": name, "description": "说明",
            "category": "utility", "enabled": enabled,
            "settings": {"enabled": enabled}, "defaults": {"enabled": False},
            "schema": schema if schema is not None else {"enabled": {"type": "bool", "label": "启用"}}}


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class InstallDialogGateTests(unittest.TestCase):
    def setUp(self):
        self.dialog = gui.PluginInstallDialog()

    def tearDown(self):
        self.dialog.deleteLater()
        APP.processEvents()

    def test_install_disabled_before_check(self):
        # 没检查过就不给装
        self.assertFalse(self.dialog.btn_install.isEnabled())
        self.dialog._install()
        wait_api(self.dialog)
        self.assertFalse(self.dialog.installed)

    def test_check_failure_keeps_install_disabled(self):
        with mock.patch.object(gui, "api_post",
                               return_value={"success": True, "ok": False,
                                             "message": "只允许 https 地址"}):
            self.dialog.url.setText("http://x/a.py")
            self.dialog._check()
            wait_api(self.dialog)
        self.assertFalse(self.dialog.btn_install.isEnabled())
        self.assertIn("https", self.dialog.result.text())

    def test_forbidden_source_keeps_install_disabled(self):
        # 预检通过 ok=true 但带 problems → 仍不许装
        with mock.patch.object(gui, "api_post",
                               return_value={"success": True, "ok": True,
                                             "plugin_id": "evil", "kind": "file",
                                             "bytes": 100, "lines": 5,
                                             "problems": ["含被禁止的用法：动态执行代码 eval"],
                                             "preview": "eval('x')"}):
            self.dialog.url.setText("https://x/evil.py")
            self.dialog._check()
            wait_api(self.dialog)
        self.assertFalse(self.dialog.btn_install.isEnabled())
        self.assertIn("未通过", self.dialog.result.text())

    def test_clean_source_enables_install(self):
        with mock.patch.object(gui, "api_post",
                               return_value={"success": True, "ok": True,
                                             "plugin_id": "good_one", "kind": "file",
                                             "bytes": 2048, "lines": 30,
                                             "problems": [], "resolved_url": "https://x/good.py",
                                             "preview": "from src.plugins import PluginBase"}):
            self.dialog.url.setText("https://x/good.py")
            self.dialog._check()
            wait_api(self.dialog)
        self.assertTrue(self.dialog.btn_install.isEnabled())
        self.assertEqual(self.dialog.plugin_id.text(), "good_one", "应自动填入插件标识")

    def test_preview_shown_after_check(self):
        with mock.patch.object(gui, "api_post",
                               return_value={"success": True, "ok": True, "plugin_id": "p",
                                             "kind": "file", "bytes": 1, "lines": 1,
                                             "problems": [], "preview": "SOME_CODE_HERE"}):
            self.dialog.url.setText("https://x/p.py")
            self.dialog._check()
            wait_api(self.dialog)
        self.assertIn("SOME_CODE_HERE", self.dialog.preview.toPlainText())

    def test_install_needs_second_confirmation(self):
        self.dialog.url.setText("https://x/p.py")
        self.dialog._inspection = {"plugin_id": "p"}
        # 用户在二次确认里点「否」
        with mock.patch.object(gui, "ask_confirm", return_value=False):
            with mock.patch.object(gui, "api_post") as posted:
                self.dialog._install()
        posted.assert_not_called()
        self.assertFalse(self.dialog.installed)

    def test_install_sends_confirmed_true(self):
        self.dialog.url.setText("https://x/p.py")
        self.dialog._inspection = {"plugin_id": "p"}
        captured = {}

        def fake_post(path, data=None, timeout=10):
            captured["path"] = path
            captured["data"] = data
            return {"success": True, "message": "已安装"}

        with mock.patch.object(gui, "ask_confirm", return_value=True), \
             mock.patch.object(gui, "tell"), \
             mock.patch.object(gui, "api_post", side_effect=fake_post):
            self.dialog._install()
            wait_api(self.dialog)

        self.assertEqual(captured["path"], "plugins/install")
        self.assertTrue(captured["data"]["confirmed"], "必须显式确认")
        self.assertTrue(self.dialog.installed)

    def test_failed_install_reports_and_stays_open(self):
        self.dialog.url.setText("https://x/p.py")
        self.dialog._inspection = {"plugin_id": "p"}
        with mock.patch.object(gui, "ask_confirm", return_value=True), \
             mock.patch.object(gui, "api_post",
                               return_value={"success": False, "message": "安全检查未通过"}):
            self.dialog._install()
            wait_api(self.dialog)
        self.assertFalse(self.dialog.installed)
        self.assertIn("安全检查未通过", self.dialog.result.text())


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class DialogStylingTests(unittest.TestCase):
    """弹窗必须显式套样式表。

    踩过的坑：``STYLESHEET`` 只应用在 MainWindow 上，靠**继承**生效；
    而 ``QDialog`` 是独立顶层窗口、不是 MainWindow 的子控件，**继承不到** ——
    表现为"主界面正常、弹窗是系统默认黑底 + 白输入框"。
    所以每个弹窗都要自己 setStyleSheet。
    """

    def test_install_dialog_has_stylesheet(self):
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.styleSheet(), "安装弹窗必须套样式表")
        self.assertIn("QDialog", dialog.styleSheet())
        self.assertIn(gui.BG, dialog.styleSheet())

    def test_install_dialog_has_auto_enable_and_folder(self):
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.auto_enable.isChecked())
        self.assertTrue(dialog.btn_folder.isEnabled())
        self.assertTrue(dialog.acceptDrops())

    def test_config_dialog_has_stylesheet(self):
        dialog = gui.PluginConfigDialog({"id": "x", "name": "x", "schema": {},
                                         "settings": {}, "defaults": {}})
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.styleSheet(), "插件设置弹窗必须套样式表")
        self.assertIn(gui.BG, dialog.styleSheet())

    def test_dialog_stylesheet_covers_inputs(self):
        # 输入框必须是浅色 FIELD，而不是系统默认的深色
        sheet = gui.DIALOG_STYLESHEET
        self.assertIn("QDialog", sheet)
        self.assertIn(f"background: {gui.FIELD}", sheet)

    def test_warning_uses_html_not_markdown(self):
        """警告文字用 HTML 加粗。

        QLabel 不解析 Markdown，写成 ``**外部代码**`` 会把星号原样显示出来。
        """
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.deleteLater)
        labels = [w.text() for w in dialog.findChildren(gui.QLabel)]
        warned = [t for t in labels if "外部代码" in t]
        self.assertTrue(warned, "应能找到警告文字")
        for text in warned:
            self.assertNotIn("**", text, "不能把 Markdown 星号显示给用户")
            self.assertIn("<b>", text)

    def test_result_label_uses_plain_text_for_backend_messages(self):
        """后端返回的消息按纯文本显示，避免 < > 被当 HTML 解析。"""
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.deleteLater)
        with mock.patch.object(gui, "api_post",
                               return_value={"success": True, "ok": False,
                                             "detail": "地址不合法 <bad>"}):
            dialog.url.setText("https://x/a.py")
            dialog._check()
            wait_api(dialog)
        self.assertEqual(dialog.result.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("<bad>", dialog.result.text())


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class DeleteAuthorisationTests(unittest.TestCase):
    """删除按钮只应出现在自定义插件上。"""

    def setUp(self):
        self.tab = gui.PluginTab()

    def tearDown(self):
        self.tab.close()
        self.tab.deleteLater()
        APP.processEvents()

    def _load(self, plugins, custom_ids):
        def fake_get(path, timeout=3):
            if path == "plugins/custom":
                return {"success": True, "plugins": [{"id": i} for i in custom_ids]}
            return copy.deepcopy(plugins)

        with mock.patch.object(gui, "api_get", side_effect=fake_get):
            self.tab.refresh()

    def _buttons(self, plugin_id):
        return [b.text() for b in self.tab._rows[plugin_id]["frame"].findChildren(QPushButton)]

    def test_custom_plugin_has_delete(self):
        self._load([plugin("mine")], ["mine"])
        self.assertIn("删除", self._buttons("mine"))

    def test_builtin_plugin_has_no_delete(self):
        self._load([plugin("sleep_mode")], [])
        self.assertNotIn("删除", self._buttons("sleep_mode"))

    def test_only_custom_rows_get_delete(self):
        self._load([plugin("mine"), plugin("sleep_mode")], ["mine"])
        self.assertIn("删除", self._buttons("mine"))
        self.assertNotIn("删除", self._buttons("sleep_mode"))

    def test_custom_endpoint_failure_means_no_delete_buttons(self):
        # 拿不到自定义清单时宁可不给删除按钮，也不能误删内置插件
        with mock.patch.object(gui, "api_get",
                               side_effect=lambda path, timeout=3: (
                                   {} if path == "plugins/custom" else [plugin("sleep_mode")])):
            self.tab.refresh()
        self.assertNotIn("删除", self._buttons("sleep_mode"))

    def test_delete_calls_api_and_refreshes(self):
        self._load([plugin("mine")], ["mine"])
        calls = []

        def fake_delete(path, timeout=15):
            calls.append(path)
            return {"success": True, "message": "已删除"}

        with mock.patch.object(gui, "ask_confirm", return_value=True), \
             mock.patch.object(gui, "api_delete", side_effect=fake_delete), \
             mock.patch.object(self.tab, "refresh") as refreshed:
            self.tab._delete_plugin("mine", plugin("mine"))

        self.assertEqual(calls, ["plugins/mine"])
        self.assertTrue(refreshed.called)

    def test_delete_cancelled_does_not_call_api(self):
        self._load([plugin("mine")], ["mine"])
        with mock.patch.object(gui, "ask_confirm", return_value=False), \
             mock.patch.object(gui, "api_delete") as deleted:
            self.tab._delete_plugin("mine", plugin("mine"))
        deleted.assert_not_called()

    def test_add_button_opens_dialog(self):
        with mock.patch.object(gui.PluginInstallDialog, "exec", return_value=0) as opened:
            self.tab._add_plugin()
        self.assertTrue(opened.called)


if __name__ == "__main__":
    unittest.main()
