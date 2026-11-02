# -*- coding: utf-8 -*-
"""离线 UI 集成测试：更新卡 / 本地插件包 / DSH 连接体检。"""
import copy
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QProgressBar, QTextEdit  # noqa: E402

import qq_gui  # noqa: E402
from qq_gui import AboutTab, PluginCoreDialog, PluginPacksDialog  # noqa: E402
from src.gui.agent_panel import AgentBackendPanel  # noqa: E402

_APP = None


def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def pump_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    app().processEvents()
    while time.time() < deadline:
        if predicate():
            return True
        app().processEvents()
        time.sleep(0.01)
    app().processEvents()
    return bool(predicate())


class FakeApi:
    """按路径前缀返回假数据，同时记录每次调用的参数。"""

    def __init__(self, get_routes=None, post_routes=None, get_delay=0.0, post_delay=0.0):
        self.get_routes = get_routes or {}
        self.post_routes = post_routes or {}
        self.get_delay = get_delay
        self.post_delay = post_delay
        self.gets = []
        self.posts = []

    @staticmethod
    def _match(routes, path):
        for key, value in routes.items():
            if str(path) == key or str(path).startswith(key):
                return copy.deepcopy(value)
        return {"success": True}

    def get(self, path, timeout=0, **kwargs):
        self.gets.append((path, dict(kwargs, timeout=timeout)))
        if self.get_delay:
            time.sleep(self.get_delay)
        return self._match(self.get_routes, path)

    def post(self, path, data=None, timeout=0, **kwargs):
        self.posts.append((path, copy.deepcopy(data), dict(kwargs, timeout=timeout)))
        if self.post_delay:
            time.sleep(self.post_delay)
        return self._match(self.post_routes, path)


class AboutUpdateCardTests(unittest.TestCase):
    def setUp(self):
        app()
        self.tab = AboutTab()

    def tearDown(self):
        self.tab.close()
        self.tab.deleteLater()
        app().processEvents()

    def test_notes_and_progress_widgets_exist(self):
        self.assertIsNotNone(self.tab.findChild(QTextEdit, "about_update_notes"))
        self.assertIsNotNone(self.tab.findChild(QProgressBar, "about_update_progress"))
        self.assertIsNotNone(self.tab.findChild(QTextEdit, "about_update_notes"))
        self.assertTrue(self.tab.update_progress.isHidden())

    def test_notes_are_truncated_and_empty_text_is_readable(self):
        long_notes = "说明" * 4000
        text = self.tab.format_update_notes(long_notes)
        self.assertLessEqual(len(text), self.tab.UPDATE_NOTES_LIMIT)
        self.assertIn("截断", text)
        self.assertEqual(self.tab.format_update_notes(""), "此版本没有附更新说明")

    def test_progress_text_and_check_result(self):
        line = self.tab.format_update_progress(
            {"percent": 45, "bytesPerSecond": 1.5 * 1024 * 1024, "message": "正在下载"})
        self.assertIn("45%", line)
        self.assertIn("1.5 MB/s", line)
        self.assertIn("正在下载", line)
        self.tab._show_update_progress(
            {"percent": 45, "bytesPerSecond": 1.5 * 1024 * 1024, "message": "正在下载"})
        self.assertEqual(self.tab.update_progress.value(), 45)
        self.assertIn("1.5 MB/s", self.tab.update_progress_text.text())
        self.tab._update_checked({
            "success": True, "hasUpdate": True, "latest": "5.2.0",
            "current": "5.1.0", "published_at": "2026-10-01",
            "notes": "完整更新说明第一行\n第二行",
        })
        self.assertIn("5.2.0", self.tab.update_state.text())
        self.assertIn("5.1.0", self.tab.update_state.text())
        self.assertIn("2026-10-01", self.tab.update_state.text())
        self.assertIn("完整更新说明第一行", self.tab.update_notes.toPlainText())
        self.assertFalse(self.tab.update_notes.isHidden())


class PluginPacksDialogTests(unittest.TestCase):
    def test_lists_packs_and_installs_confirmed(self):
        fake = FakeApi({
            "plugins/packs": {
                "success": True,
                "directory": "C:/packs",
                "packs": [{"id": "demo", "name": "演示包", "version": "1.0",
                           "description": "演示说明", "ok": True, "installed": False}],
                "issues": [],
            },
        }, {
            "plugins/packs/install": {"success": True, "id": "demo", "target": "C:/plugins/demo",
                                      "installed": True},
        })
        dialog = PluginPacksDialog(api_get=fake.get, api_post=fake.post)
        self.addCleanup(dialog.close)
        self.assertTrue(pump_until(lambda: dialog.pack_list.count() == 1))
        self.assertIn("演示包", dialog.pack_list.item(0).text())
        self.assertIn("1.0", dialog.pack_list.item(0).text())
        dialog.pack_list.setCurrentRow(0)
        self.assertTrue(dialog.install_selected())
        self.assertTrue(pump_until(lambda: bool(fake.posts)))
        path, body, _ = fake.posts[0]
        self.assertEqual(path, "plugins/packs/install")
        self.assertEqual(body["id"], "demo")
        self.assertIs(body["confirmed"], True)
        self.assertFalse(body["overwrite"])
        self.assertTrue(body["enable"])

    def test_install_result_message_and_enabled_list_are_shown(self):
        fake = FakeApi({
            "plugins/packs": {
                "success": True,
                "directory": "C:/packs",
                "packs": [{"id": "demo", "name": "演示包", "version": "1.0",
                           "description": "演示说明", "ok": True, "installed": False}],
                "issues": [],
            },
        }, {
            "plugins/packs/install": {
                "success": True, "id": "demo", "target": "C:/plugins/demo",
                "installed": True, "enabled": ["demo_plugin"],
                "message": "插件包已安装；已启用：demo_plugin",
            },
        })
        dialog = PluginPacksDialog(api_get=fake.get, api_post=fake.post)
        self.addCleanup(dialog.close)
        self.assertTrue(pump_until(lambda: dialog.pack_list.count() == 1))
        dialog.pack_list.setCurrentRow(0)
        self.assertTrue(dialog.install_selected())
        self.assertTrue(pump_until(lambda: not dialog.requests.busy))
        self.assertIn("已启用", dialog.notice.text())
        self.assertIn("demo_plugin", dialog.notice.text())

    def test_installed_pack_is_marked_and_install_blocked(self):
        fake = FakeApi({
            "plugins/packs": {
                "success": True,
                "directory": "C:/packs",
                "packs": [{"id": "demo", "name": "演示包", "version": "1.0",
                           "description": "演示说明", "ok": True, "installed": True}],
                "issues": [],
            },
        })
        dialog = PluginPacksDialog(api_get=fake.get, api_post=fake.post)
        self.addCleanup(dialog.close)
        self.assertTrue(pump_until(lambda: dialog.pack_list.count() == 1))
        self.assertIn("已安装", dialog.pack_list.item(0).text())
        dialog.pack_list.setCurrentRow(0)
        self.assertFalse(dialog.install_selected())
        self.assertIn("已安装", dialog.notice.text())


class PluginTabCoreEntryTests(unittest.TestCase):
    def test_plugin_tab_exposes_core_plugins_button(self):
        app()
        tab = qq_gui.PluginTab()
        self.addCleanup(tab.close)
        self.assertEqual(tab.btn_plugin_core.text(), "内核插件")
        self.assertEqual(tab.btn_plugin_core.objectName(), "btn_plugin_core")


class PluginCoreDialogTests(unittest.TestCase):
    def _dialog(self, status, post_routes=None):
        fake = FakeApi({"plugin-core/status": status}, post_routes or {})
        dialog = PluginCoreDialog(api_get=fake.get, api_post=fake.post)
        self.addCleanup(dialog.close)
        self.assertTrue(pump_until(lambda: fake.gets))
        return dialog, fake

    def test_lists_core_plugins_and_installs_with_overwrite(self):
        dialog, fake = self._dialog({
            "success": True, "available": True, "lastError": "",
            "plugins": [{"id": "hello", "name": "问候", "version": "1.0"}],
        }, {
            "plugin-core/install": {"success": True, "plugin": {"id": "hello"}},
        })
        self.assertTrue(pump_until(lambda: dialog.plugin_list.count() == 1))
        self.assertIn("问候", dialog.plugin_list.item(0).text())
        self.assertIn("hello", dialog.plugin_list.item(0).text())
        self.assertIn("内核已启动", dialog.summary.text())
        dialog.path.setText("C:/plugins/hello")
        dialog.overwrite.setChecked(True)
        self.assertTrue(dialog.install_plugin())
        self.assertTrue(pump_until(lambda: bool(fake.posts)))
        path, body, kwargs = fake.posts[0]
        self.assertEqual(path, "plugin-core/install")
        self.assertEqual(body, {"path": "C:/plugins/hello", "overwrite": True})
        self.assertEqual(kwargs.get("timeout"), 120)

    def test_uninstall_requires_confirmation_and_sends_selected_id(self):
        dialog, fake = self._dialog({
            "success": True, "available": True,
            "plugins": [{"id": "hello", "name": "问候", "version": "1.0"}],
        }, {
            "plugin-core/uninstall": {"success": True, "id": "hello"},
        })
        self.assertTrue(pump_until(lambda: dialog.plugin_list.count() == 1))
        dialog.plugin_list.setCurrentRow(0)
        with mock.patch.object(
                qq_gui.QMessageBox, "question",
                return_value=qq_gui.QMessageBox.StandardButton.Yes):
            self.assertTrue(dialog.uninstall_selected())
        self.assertTrue(pump_until(lambda: bool(fake.posts)))
        path, body, kwargs = fake.posts[0]
        self.assertEqual(path, "plugin-core/uninstall")
        self.assertEqual(body, {"id": "hello"})
        self.assertEqual(kwargs.get("timeout"), 30)

    def test_uninstall_cancelled_does_not_call_api(self):
        dialog, fake = self._dialog({
            "success": True, "available": True,
            "plugins": [{"id": "hello", "name": "问候", "version": "1.0"}],
        })
        self.assertTrue(pump_until(lambda: dialog.plugin_list.count() == 1))
        dialog.plugin_list.setCurrentRow(0)
        with mock.patch.object(
                qq_gui.QMessageBox, "question",
                return_value=qq_gui.QMessageBox.StandardButton.No):
            self.assertFalse(dialog.uninstall_selected())
        app().processEvents()
        self.assertFalse(fake.posts)

    def test_empty_path_is_rejected_locally(self):
        dialog, fake = self._dialog({"success": True, "available": False, "plugins": []})
        self.assertFalse(dialog.install_plugin())
        self.assertIn("请先选择", dialog.notice.text())
        self.assertFalse(fake.posts)


class DshConnectionReportTests(unittest.TestCase):
    def _panel(self):
        report = {
            "ok": True, "profile": "desktop", "installed": ["memory", "control"],
            "missing": ["tools"], "new_session_ready": False, "restart_required": True,
            "issues": [{"code": "mcp_missing", "message": "缺少工具桥"}],
            "hint": "请重启 DSH 后重新打开会话。",
        }
        fake = FakeApi({
            "agent/backends/dsh/connection-report": report,
            "agent/backends": {"success": True, "backends": []},
        })
        panel = AgentBackendPanel(fake.get, fake.post, fake.post)
        self.addCleanup(panel.close)
        self.assertTrue(pump_until(lambda: not panel.requests.busy))
        return panel, fake

    def test_apply_report_shows_session_missing_issues_and_hint(self):
        panel, _fake = self._panel()
        panel.apply_dsh_report({
            "ok": True, "profile": "desktop", "installed": ["memory", "control"],
            "missing": ["tools"], "new_session_ready": False, "restart_required": True,
            "issues": [{"code": "mcp_missing", "message": "缺少工具桥"}],
            "hint": "请重启 DSH 后重新打开会话。",
        })
        text = panel.dsh_report.text()
        self.assertIn("新会话不可用", text)
        self.assertIn("缺少 MCP：tools", text)
        self.assertIn("缺少工具桥", text)
        self.assertIn("需要重启：是", text)
        self.assertIn("请重启 DSH 后重新打开会话。", text)

    def test_report_action_and_profile_switch_clear(self):
        panel, fake = self._panel()
        self.assertTrue(panel.action_dsh_report())
        self.assertTrue(pump_until(lambda: not panel.requests.busy))
        self.assertIn("新会话不可用", panel.dsh_report.text())
        self.assertTrue(any("connection-report" in path and "profile=desktop" in path
                            for path, _ in fake.gets))
        panel.set_dsh_profiles(["desktop", "headless"])
        panel.dsh_profile.setCurrentIndex(1)
        self.assertEqual(panel.dsh_report.text(), "")

    def test_ready_report_uses_available_wording(self):
        panel, _fake = self._panel()
        panel.apply_dsh_report({"ok": True, "profile": "desktop", "installed": ["a"],
                                "missing": [], "new_session_ready": True,
                                "restart_required": False, "issues": [], "hint": ""})
        self.assertIn("新会话可用", panel.dsh_report.text())
        self.assertIn("需要重启：否", panel.dsh_report.text())


if __name__ == "__main__":
    unittest.main()
