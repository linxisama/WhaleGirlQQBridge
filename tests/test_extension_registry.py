"""扩展启用态存储（src/extension_registry.py）与能力桥启用态过滤的测试。

这里不启动 Node、不联网：桥的工具枚举用 asyncio.create_subprocess_exec 的假替身
盯着，确认"全停"时一个进程都不会起。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import extension_registry
from src.plugins import qqagent_bridge


def _zip(path: Path) -> Path:
    """一个只有 end-of-central-directory 的空 zip：存在即可，不需要真能解开。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    return path


class _FakeManager:
    """桥只需要 data_dir + get_settings；不构造真实插件管理器（那会读全部插件）。"""

    def __init__(self, data_dir, settings=None):
        self.data_dir = Path(data_dir)
        self.custom_plugins_dir = self.data_dir / "qq_plugins"
        self._settings = {qqagent_bridge.PLUGIN_ID: dict(settings or {})}

    def get_settings(self, plugin_id):
        merged = dict(qqagent_bridge.QqAgentBridge.META.default_settings)
        merged.update(self._settings.get(plugin_id) or {})
        return merged


class RegistryStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        (self.data / "qq-only").mkdir(parents=True)
        extension_registry.invalidate()
        self.addCleanup(extension_registry.invalidate)

    def _state_file(self) -> Path:
        return self.data / "qq-only" / "qqagent_extensions.json"

    def test_missing_state_file_means_everything_disabled(self):
        self.assertEqual(extension_registry.load(self.data),
                         {"version": 1, "extensions": {}})
        self.assertFalse(extension_registry.is_enabled("anything", self.data))
        self.assertEqual(extension_registry.enabled_ids(self.data), [])
        self.assertFalse(self._state_file().exists(), "读状态不能顺手写盘")

    def test_set_enabled_persists_expected_shape(self):
        entry = extension_registry.set_enabled("demo-ext", True, self.data)
        self.assertEqual(entry["id"], "demo-ext")
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["source"], "market")
        self.assertRegex(entry["updatedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        payload = json.loads(self._state_file().read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(set(payload["extensions"]["demo-ext"]),
                         {"enabled", "updatedAt", "source", "note"})
        self.assertTrue(extension_registry.is_enabled("demo-ext", self.data))
        self.assertEqual(extension_registry.enabled_ids(self.data), ["demo-ext"])
        extension_registry.set_enabled("demo-ext", False, self.data)
        self.assertFalse(extension_registry.is_enabled("demo-ext", self.data))
        self.assertEqual(extension_registry.disabled_ids(self.data), ["demo-ext"])
        self.assertTrue(self._state_file().with_suffix(".json.bak").exists(),
                        "第二次保存要留下备份，坏文件才有得回滚")

    def test_blank_id_is_rejected(self):
        with self.assertRaises(ValueError):
            extension_registry.set_enabled("  ", True, self.data)

    def test_corrupt_state_degrades_to_empty(self):
        self._state_file().write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(extension_registry.load(self.data),
                         {"version": 1, "extensions": {}})
        self.assertFalse(extension_registry.is_enabled("demo-ext", self.data))
        items = extension_registry.list_extensions(self.data)
        self.assertIsInstance(items, list)
        empty_market = self.root / "empty-market"
        empty_market.mkdir()
        (empty_market / "all-plugins-v4.json").write_text("[]", encoding="utf-8")
        with mock.patch.dict(os.environ, {"QQAGENT_MANIFEST_DIR": str(empty_market)}):
            extension_registry.invalidate()
            self.assertEqual(extension_registry.summary(self.data)["total"], 0)

    def test_set_many_is_idempotent(self):
        first = extension_registry.set_many(["a", "b", "a"], True, self.data)
        self.assertEqual(first["count"], 2
        )
        self.assertTrue(first["ok"])
        second = extension_registry.set_many(["a", "b"], True, self.data)
        self.assertEqual(second["count"], 2)
        self.assertEqual(extension_registry.enabled_ids(self.data), ["a", "b"])
        third = extension_registry.set_many(["a", "b"], False, self.data)
        self.assertFalse(third["enabled"])
        self.assertEqual(extension_registry.enabled_ids(self.data), [])
        self.assertEqual(extension_registry.disabled_ids(self.data), ["a", "b"])

    def test_empty_batch_touches_nothing(self):
        result = extension_registry.set_many([], True, self.data)
        self.assertEqual(result["count"], 0)
        self.assertFalse(self._state_file().exists())


class RegistryDiscoveryTests(unittest.TestCase):
    """市场清单层：用临时目录 + QQAGENT_MANIFEST_DIR 覆盖，不碰用户 data。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        (self.data / "qq-only").mkdir(parents=True)
        self.market = self.root / "market"
        self.market.mkdir()
        zip_a = _zip(self.market / "ext-a.zip")
        rows = [
            {"id": "ext-a", "name": "扩展甲", "file": str(zip_a), "ok": True,
             "extension": {"tools": ["ext-a__ping", "ext-a__pong"],
                           "hooks": ["before-context", "after-response"],
                           "providers": ["message.owner-check"]}},
            {"id": "ext-b", "name": "扩展乙", "file": str(self.market / "missing.zip"),
             "ok": True, "extension": {"tools": [], "hooks": [], "providers": []}},
        ]
        (self.market / "all-plugins-v4.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        self._env = mock.patch.dict(
            os.environ, {"QQAGENT_MANIFEST_DIR": str(self.market)})
        self._env.start()
        self.addCleanup(self._env.stop)
        extension_registry.invalidate()
        self.addCleanup(extension_registry.invalidate)

    def test_list_extensions_merges_manifest_and_state(self):
        items = extension_registry.list_extensions(self.data)
        by_id = {item["id"]: item for item in items}
        self.assertEqual(set(by_id), {"ext-a", "ext-b"})
        first = by_id["ext-a"]
        self.assertEqual(first["layer"], "catalog")
        self.assertEqual(first["name"], "扩展甲")
        self.assertFalse(first["enabled"], "市场扩展默认必须停用")
        self.assertEqual(first["toolCount"], 2)
        self.assertEqual(first["hookCount"], 2)
        self.assertEqual(first["tier"], "T1-ready")
        self.assertEqual(first["blockers"], [])
        self.assertEqual(by_id["ext-b"]["blockers"], ["zip-missing"])
        self.assertEqual(by_id["ext-b"]["tier"], "T6-disabled")
        extension_registry.set_enabled("ext-a", True, self.data)
        extension_registry.invalidate()
        enabled = [item for item in extension_registry.list_extensions(self.data)
                   if item["enabled"]]
        self.assertEqual([item["id"] for item in enabled], ["ext-a"])

    def test_filters_and_summary(self):
        self.assertEqual(len(extension_registry.list_extensions(self.data, False, "catalog")), 2)
        self.assertEqual(extension_registry.list_extensions(self.data, False, "builtin"), [])
        self.assertEqual(
            [item["id"] for item in extension_registry.list_extensions(self.data, False, "", "扩展乙")],
            ["ext-b"])
        extension_registry.set_enabled("ext-a", True, self.data)
        extension_registry.invalidate()
        summary = extension_registry.summary(self.data)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["enabled"], 1)
        self.assertEqual(summary["layers"], {"catalog": 2})
        self.assertEqual(summary["enabledToolCount"], 2)
        self.assertEqual(summary["enabledHookCount"], 2)
        self.assertTrue(summary["statePath"].endswith("qqagent_extensions.json"))
        self.assertEqual(summary["error"], "")

    def test_scan_returns_items_and_summary(self):
        snapshot = extension_registry.scan(self.data, True)
        self.assertEqual(len(snapshot["items"]), 2)
        self.assertEqual(snapshot["summary"]["total"], 2)


class BridgeRespectsRegistryTests(unittest.TestCase):
    """桥必须把 registry 当唯一权威：全停就不枚举、不起进程、拒绝调用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        (self.data / "qq-only").mkdir(parents=True)
        self.ext_dir = self.root / "extensions"
        self.ext_dir.mkdir()
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.host_script = self.root / "host-one.mjs"
        self.host_script.write_text("// stub\n", encoding="utf-8")
        self.catalog = self.root / "catalog.json"
        rows = []
        for ident in ("ext-a", "ext-b"):
            archive = _zip(self.ext_dir / (ident + ".zip"))
            rows.append({"id": ident, "kind": "plugin", "name": ident,
                         "file": str(archive),
                         "capabilities": ["message.owner-check"],
                         "host_imports": [], "tools": [ident + "__do"]})
        self.catalog.write_text(json.dumps(rows), encoding="utf-8")
        # 让 ext-a/ext-b 出现在市场清单里 = "归扩展管理管"，于是默认停用；
        # 手写 profiles 里的历史 id 不在此列，桥上继续放行（兼容语义）。
        market = self.root / "market"
        market.mkdir()
        manifest = [{"id": ident, "name": ident, "file": str(self.ext_dir / (ident + ".zip")),
                     "ok": True, "extension": {"tools": [ident + "__do"], "hooks": [],
                                               "providers": []}}
                    for ident in ("ext-a", "ext-b")]
        (market / "all-plugins-v4.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {"QQAGENT_MANIFEST_DIR": str(market)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.manager = _FakeManager(self.data, {
            "enabled": True, "autoDiscover": True, "maxProfiles": 4,
            "hostScript": str(self.host_script), "sourceDir": str(self.source_dir),
            "catalogPath": str(self.catalog), "extensionsDir": str(self.ext_dir)})
        extension_registry.invalidate()
        self.addCleanup(extension_registry.invalidate)

    def _bridge(self):
        return qqagent_bridge.QqAgentBridge(self.manager)

    def test_all_disabled_yields_no_tools_and_no_node_process(self):
        bridge = self._bridge()
        with mock.patch("asyncio.create_subprocess_exec") as spawn:
            tools = bridge.list_tools()
            self.assertEqual(tools, [])
            self.assertEqual(spawn.call_count, 0, "全停时一个 node 进程都不该起")
        self.assertEqual(bridge._profiles()[0].get("extensionPath") or "", "")

    def test_enabling_one_extension_brings_its_profile_back(self):
        bridge = self._bridge()
        self.assertEqual(
            [item.get("extensionId") for item in bridge._profiles()
             if item.get("extensionId")], [])
        extension_registry.set_enabled("ext-a", True, self.data)
        discovered = bridge._profiles()
        # 一条扩展可能同时命中 provider 与 tool 两条车道，故按集合比对。
        self.assertEqual({item.get("extensionId") for item in discovered}, {"ext-a"})
        extension_registry.set_enabled("ext-b", True, self.data)
        both = {item.get("extensionId") for item in bridge._profiles()}
        self.assertEqual(both, {"ext-a", "ext-b"})

    def test_cached_tools_of_disabled_extension_are_hidden(self):
        cache = self.data / "qqagent_bridge_tools.json"
        cache.write_text(json.dumps({"extensions": {
            "ext-a": [{"id": "ext-a__do", "description": "x", "parameters": {}}],
            "ext-b": [{"id": "ext-b__do", "description": "y", "parameters": {}}],
        }}), encoding="utf-8")
        extension_registry.set_enabled("ext-b", True, self.data)
        bridge = self._bridge()
        with mock.patch("asyncio.create_subprocess_exec") as spawn:
            tools = bridge.list_tools()
        self.assertEqual([tool["name"] for tool in tools], ["ext-b__do"])
        self.assertEqual(spawn.call_count, 0)

    def test_call_tool_refuses_disabled_extension_without_starting_node(self):
        bridge = self._bridge()
        with mock.patch("asyncio.create_subprocess_exec") as spawn:
            result = bridge.call_tool("ext-a__do", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["kind"], "disabled")
        self.assertIn("ext-a", result["error"]["message"])
        self.assertEqual(spawn.call_count, 0)

    def test_unknown_extensions_keep_legacy_behaviour(self):
        # 手写 profiles 里的扩展不在 registry 视野内：默认不是"停用"，桥上照常放行，
        # 这样升级不会把已有配置一键打死。
        bridge = self._bridge()
        self.assertFalse(extension_registry.is_known("legacy-ext", self.data))
        self.assertFalse(bridge._registry_disabled("legacy-ext"))
        self.assertTrue(extension_registry.is_known("ext-a", self.data))
        self.assertTrue(bridge._registry_disabled("ext-a"))

    def test_registry_gate_follows_enable_state(self):
        # 闸门本身：启用前拦、启用后放行、空 id 不拦（handled by 既有行为）。
        bridge = self._bridge()
        self.assertTrue(bridge._registry_disabled("ext-a"))
        extension_registry.set_enabled("ext-a", True, self.data)
        self.assertFalse(bridge._registry_disabled("ext-a"))
        self.assertTrue(bridge._registry_disabled("ext-b"))
        self.assertFalse(bridge._registry_disabled(""))


class TierPolicyTests(unittest.TestCase):
    """host_imports 只在 zip 层阻断：装进宿主根后 ../../src 已经能解析。"""

    def test_zip_layer_with_host_imports_stays_t4(self):
        tier, blockers, _flags = extension_registry._tier(
            {"id": "zip-ext", "host_imports": ["../../src/config.js"],
             "source": "catalog", "installed": True, "ok": True})
        self.assertEqual(tier, "T4-host-source")
        self.assertIn("host-source", blockers)

    def test_host_layer_with_host_imports_is_not_blocked(self):
        tier, blockers, _flags = extension_registry._tier(
            {"id": "host-ext", "host_imports": ["../../src/config.js"],
             "source": "host", "installed": True, "ok": True})
        self.assertNotEqual(tier, "T4-host-source")
        self.assertNotIn("host-source", blockers)

    def test_discover_keeps_source_so_host_layer_is_not_t4(self):
        """discover 合并时必须把 record 的 source 带到 item 上，_tier 才分得清宿主层。"""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            (data / "qq-only").mkdir(parents=True)
            extension_registry.invalidate()
            self.addCleanup(extension_registry.invalidate)
            record = {"id": "host-ext", "name": "宿主扩展",
                      "path": str(Path(tmp) / "plugins" / "host-ext"),
                      "tools": [], "hooks": [], "providers": [],
                      "host_imports": ["../../src/config.js"],
                      "source": "host", "fileExists": True}
            with mock.patch.object(extension_registry, "_installed_records",
                                   return_value={"host-ext": record}), \
                    mock.patch.object(extension_registry, "manifest_dir",
                                      return_value=Path(tmp) / "manifest"), \
                    mock.patch.object(extension_registry, "_manifest_index",
                                      return_value={}), \
                    mock.patch.object(extension_registry, "_catalog_index",
                                      return_value={}):
                payload = extension_registry.discover(data, refresh=True)
            item = {row["id"]: row for row in payload["items"]}["host-ext"]
            self.assertEqual(item["source"], "host")
            self.assertNotEqual(item["tier"], "T4-host-source")
            self.assertNotIn("host-source", item["blockers"])


if __name__ == "__main__":
    unittest.main()