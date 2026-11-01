# -*- coding: utf-8 -*-
"""离线 UI 集成测试：更新卡 / Windows 沙盒页 / 本地插件包 / DSH 连接体检。"""
import copy
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QProgressBar, QTextEdit  # noqa: E402

import qq_gui  # noqa: E402
from qq_gui import AboutTab, PluginPacksDialog  # noqa: E402
from src.gui.agent_panel import AgentBackendPanel  # noqa: E402
from src.gui.sandbox_page import SandboxPage  # noqa: E402

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


STATUS_DISABLED = {
    "success": True,
    "status": "disabled",
    "supported": True,
    "ready": False,
    "detail": "Windows 沙盒可用但尚未启用。",
    "alternative": "可以使用 Hyper-V 虚拟机",
    "detection": {"windows_version": "11", "edition": "Pro", "build": "22631"},
}
CHANNEL_DEFAULT = {
    "success": True,
    "channel": {
        "name": "default",
        "exists": True,
        "wsb_path": "C:/sandbox/default.wsb",
        "host_folder": "C:/sandbox/host",
        "sandbox_folder": "C:/sandbox",
        "ready": True,
        "ready_marker_path": "C:/sandbox/.ready",
    },
}


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


class SandboxPageTests(unittest.TestCase):
    def _page(self, **kwargs):
        fake = FakeApi({
            "sandbox/status": STATUS_DISABLED,
            "sandbox/channel": CHANNEL_DEFAULT,
        }, {
            "sandbox/channel": {"success": True, "channel": CHANNEL_DEFAULT["channel"]},
            "sandbox/enable": {"success": True, "status": "enabled", "ready": True},
            "sandbox/disable": {"success": True, "status": "disabled", "ready": False},
        }, **kwargs)
        page = SandboxPage(fake.get, fake.post)
        self.addCleanup(page.close)
        return page, fake

    def test_refresh_shows_chinese_status_and_windows_info(self):
        page, fake = self._page()
        self.assertTrue(page.refresh())
        self.assertTrue(pump_until(lambda: not page.busy))
        self.assertEqual(page.status_value.text(), "未启用")
        self.assertIn("Windows 11", page.windows_info.text())
        self.assertIn("Pro", page.windows_info.text())
        self.assertIn("22631", page.windows_info.text())
        self.assertIn("替代方案", page.alternative.text())
        self.assertTrue(any(path == "sandbox/status" for path, _ in fake.gets))

    def test_create_channel_sends_confirmed_true(self):
        page, fake = self._page()
        page.channel_host.setText("D:/sandbox-host")
        self.assertTrue(page.create_channel())
        self.assertTrue(pump_until(lambda: bool(fake.posts)))
        path, body, _ = fake.posts[0]
        self.assertEqual(path, "sandbox/channel")
        self.assertEqual(body["name"], "default")
        self.assertEqual(body["host_folder"], "D:/sandbox-host")
        self.assertIs(body["confirmed"], True)
        self.assertIs(body["launch"], False)
        self.assertIs(body["networking"], False)
        self.assertIs(body["clipboard"], False)

    def test_unsupported_disables_enable_and_busy_ignores_repeat(self):
        page, fake = self._page(post_delay=0.25)
        page.apply_status({"success": True, "status": "unsupported", "supported": False})
        self.assertFalse(page.btn_enable.isEnabled())
        page.apply_status(STATUS_DISABLED)
        self.assertTrue(page.btn_enable.isEnabled())
        # 先让 apply_status 触发的 channel 读取结束，再测重复点击。
        self.assertTrue(pump_until(lambda: not page.busy))
        self.assertTrue(page.create_channel())
        self.assertTrue(pump_until(lambda: len(fake.posts) == 1))
        self.assertFalse(page.create_channel())
        self.assertTrue(pump_until(lambda: not page.busy))
        self.assertEqual(len(fake.posts), 1)


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
