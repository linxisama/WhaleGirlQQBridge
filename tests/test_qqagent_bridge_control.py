"""能力桥开箱状态/启用控制的单元测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.qqagent_bridge_control import bridge_status, set_bridge_enabled


class FakeBridge:
    def __init__(self, profiles, report=None):
        self.profiles = profiles
        self.report = report

    def _profiles(self, include_disabled=False):
        return list(self.profiles)

    def profile_report(self):
        if self.report is None:
            raise AttributeError("profile_report")
        return dict(self.report)


class FakeManager:
    def __init__(self, settings, profiles=(), report=None):
        self._plugins = {"qqagent_bridge": FakeBridge(profiles, report)}
        self.settings = dict(settings)
        self.updates = []

    def get_settings(self, plugin_id):
        assert plugin_id == "qqagent_bridge"
        return dict(self.settings)

    def update_settings(self, plugin_id, patch):
        assert plugin_id == "qqagent_bridge"
        self.updates.append(dict(patch))
        self.settings.update(patch)


class BridgeControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = self.root / "host-one.mjs"
        self.host.write_text("// host", encoding="utf-8")
        self.source = self.root / "source"
        self.source.mkdir()
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text("[]", encoding="utf-8")
        self.extensions = self.root / "extensions"
        self.extensions.mkdir()
        self.profile = {
            "provider": "message.owner-check",
            "hostScript": str(self.host),
            "sourceDir": str(self.source),
            "extensionPath": str(self.extensions / "x.zip"),
            "event": "on_prepare",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, **changes):
        value = {
            "enabled": False,
            "hostScript": str(self.host),
            "sourceDir": str(self.source),
            "catalogPath": str(self.catalog),
            "extensionsDir": str(self.extensions),
            "ownerOnly": True,
            "ownerIds": "2019117802",
        }
        value.update(changes)
        return value

    def test_status_reports_missing_and_ready(self):
        manager = FakeManager(self.settings(catalogPath=""), [self.profile])
        status = bridge_status(manager)
        self.assertTrue(status["loaded"])
        self.assertFalse(status["ready"])
        self.assertIn("扩展 catalog", status["missing"])

        manager = FakeManager(self.settings(), [self.profile])
        status = bridge_status(manager)
        self.assertTrue(status["ready"])
        self.assertEqual(status["profiles"], 1)
        self.assertEqual(status["providers"], ["message.owner-check"])

    def test_enable_needs_confirmation_and_complete_config(self):
        manager = FakeManager(self.settings(), [self.profile])
        with self.assertRaisesRegex(ValueError, "显式确认"):
            set_bridge_enabled(manager, True)
        self.assertFalse(manager.updates)

        broken = FakeManager(self.settings(ownerIds=""), [self.profile])
        with self.assertRaisesRegex(ValueError, "缺少"):
            set_bridge_enabled(broken, True, confirmed=True)
        self.assertFalse(broken.updates)

    def test_enable_and_disable_roundtrip(self):
        manager = FakeManager(self.settings(), [self.profile])
        status = set_bridge_enabled(manager, True, confirmed=True)
        self.assertTrue(status["enabled"])
        self.assertTrue(manager.settings["enabled"])
        self.assertEqual(manager.updates, [{"enabled": True}])

        status = set_bridge_enabled(manager, False)
        self.assertFalse(status["enabled"])
        self.assertFalse(manager.settings["enabled"])

    def test_unloaded_bridge_is_reported(self):
        class Empty:
            _plugins = {}
        status = bridge_status(Empty())
        self.assertFalse(status["loaded"])
        self.assertEqual(status["missing"], ["qqagent_bridge"])

    def test_status_route_accepts_get_and_post(self):
        """GUI 走 run_api_async 一律 POST，路由必须同时收 GET/POST，否则 405。"""
        from types import SimpleNamespace

        from fastapi.testclient import TestClient

        from src.qq_app import create_app

        manager = FakeManager(self.settings(), [self.profile])
        app = create_app()
        app.state.bot = SimpleNamespace(plugins=manager)
        client = TestClient(app)

        posted = client.post("/api/qqagent/bridge/status", json={})
        self.assertEqual(posted.status_code, 200, posted.text)
        body = posted.json()
        self.assertTrue(body["success"])
        self.assertNotIn("detail", body)

        got = client.get("/api/qqagent/bridge/status")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertTrue(got.json()["success"])

    def test_all_disabled_profiles_are_reported_not_ready(self):
        report = {
            "active": [],
            "disabled": [
                {"id": "ext-a", "provider": "message.owner-check",
                 "reason": "扩展管理里未启用"},
            ],
        }
        manager = FakeManager(self.settings(), [self.profile], report=report)
        status = bridge_status(manager)
        self.assertEqual(status["profiles"], 1)
        self.assertEqual(status["activeProfiles"], 0)
        self.assertEqual(status["disabledProfiles"], 1)
        self.assertEqual(status["disabledReasons"][0]["reason"], "扩展管理里未启用")
        self.assertFalse(status["ready"])
        self.assertIn("1 个已配置，但全部被停用", "".join(status["missing"]))
        with self.assertRaisesRegex(ValueError, "全部被停用"):
            set_bridge_enabled(manager, True, confirmed=True)
        self.assertFalse(manager.updates)

    def test_profiles_without_provider_do_not_count(self):
        manager = FakeManager(self.settings(), [{"provider": "", "hookName": ""}])
        status = bridge_status(manager)
        self.assertEqual(status["profiles"], 0)
        self.assertIn("可执行扩展（白名单 provider）", status["missing"])


if __name__ == "__main__":
    unittest.main()
