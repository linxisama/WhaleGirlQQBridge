"""QQ Agent 能力桥的清单筛选与白名单测试（不启动 Node）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.plugins import qqagent_bridge


class CatalogProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ext = self.root / "extensions"
        self.ext.mkdir()
        self.catalog = self.root / "catalog.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, name, caps, imports=None, kind="plugin", tools=None):
        path = self.ext / f"{name}.zip"
        path.write_bytes(b"PK\x05\x06" + b"\0" * 18)
        return {"id": name, "kind": kind, "file": str(path),
                "capabilities": caps, "host_imports": imports or [],
                "tools": tools or []}

    def test_only_allowed_plugins_are_selected_and_limit_applies(self):
        entries = [
            self._entry("owner-check", ["message.owner-check"]),
            self._entry("llm-params", [{"name": "llm.request-params"}]),
            self._entry("tool-only", ["tool.guard"]),
            self._entry("skill", ["message.gate"], kind="skill"),
        ]
        self.catalog.write_text(json.dumps(entries), encoding="utf-8")
        result = qqagent_bridge.catalog_profiles(self.catalog, self.ext, limit=2)
        self.assertEqual([item["provider"] for item in result],
                         ["message.owner-check", "llm.request-params"])
        self.assertEqual([item["event"] for item in result], ["on_prepare", "on_pre_ai"])
        self.assertTrue(all(item["enabled"] for item in result))

    def test_imports_are_deprioritized_but_still_usable(self):
        entries = [
            self._entry("imported", ["message.gate"], imports=["../../src/config.js"]),
            self._entry("plain", ["message.identity"]),
        ]
        self.catalog.write_text(json.dumps(entries), encoding="utf-8")
        result = qqagent_bridge.catalog_profiles(self.catalog, self.ext, limit=2)
        self.assertEqual(result[0]["provider"], "message.identity")

    def test_missing_and_outside_extensions_are_skipped(self):
        good = self._entry("good", ["message.gate"])
        missing = dict(good, id="missing", file=str(self.ext / "missing.zip"))
        outside = self.ext.parent / "outside.zip"
        outside.write_bytes(b"x")
        escaped = dict(good, id="escaped", file=str(outside))
        self.catalog.write_text(json.dumps([missing, escaped, good]), encoding="utf-8")
        result = qqagent_bridge.catalog_profiles(self.catalog, self.ext, limit=2)
        self.assertEqual([Path(item["extensionPath"]).name for item in result], ["good.zip"])

    def test_broken_catalog_is_empty_not_an_exception(self):
        self.catalog.write_text("{not json", encoding="utf-8")
        self.assertEqual(qqagent_bridge.catalog_profiles(self.catalog), [])


class BridgeRegistrationTests(unittest.TestCase):
    def test_bridge_is_part_of_basic_plugin_set(self):
        from src.plugin_catalog import BASIC_PLUGINS
        from src.plugins.manager import PLUGIN_MODULES
        self.assertIn("qqagent_bridge", BASIC_PLUGINS)
        self.assertIn("qqagent_bridge", PLUGIN_MODULES)
        self.assertTrue((Path(__file__).resolve().parents[1] / "src" / "plugins"
                         / "qqagent_bridge.py").is_file())

    def test_auto_discovery_fields_are_exposed(self):
        schema = qqagent_bridge.QqAgentBridge.META.config_schema
        for key in ("catalogPath", "extensionsDir", "autoDiscover", "maxProfiles"):
            self.assertIn(key, schema)
        self.assertFalse(qqagent_bridge.QqAgentBridge.META.default_settings["autoDiscover"])

    def test_bridge_is_owner_only_and_off_by_default(self):
        meta = qqagent_bridge.QqAgentBridge.META
        self.assertFalse(meta.enabled_by_default)
        self.assertTrue(meta.default_settings["ownerOnly"])

        class Manager:
            def get_settings(self, plugin_id):
                return {}

        plugin = qqagent_bridge.QqAgentBridge(Manager())
        self.assertEqual(plugin._event_profiles({"sender_id": "stranger"}, "on_prepare"), [])


if __name__ == "__main__":
    unittest.main()
