"""扩展管理 5+1 个接口的测试（TestClient，不启动真实运行时/不联网）。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from src import extension_registry, plugin_installer


def _zip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    return path


class _FakeManager:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.custom_plugins_dir = self.data_dir / "qq_plugins"
        self._plugins = {}


class _FakeBridge:
    """只实现接口层用得到的三个成员。"""

    def __init__(self, enabled=True, fail=False):
        self.enabled = enabled
        self.fail = fail
        self.refreshed = 0

    def bridge_status(self):
        if self.fail:
            raise RuntimeError("boom")
        return {"enabled_extensions": ["ext-a"], "calls": 3,
                "tool_calls": 2, "capabilities": [
                    {"extensionId": "ext-a", "kind": "tool",
                     "capability": "ext-a__ping", "event": "", "deadInHost": False}]}

    async def refresh_tools(self):
        self.refreshed += 1
        return {"ok": True, "tools": ["ext-a__ping"], "error": ""}


class ExtensionsApiTests(unittest.TestCase):
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
             "extension": {"tools": ["ext-a__ping"], "hooks": ["after-response"],
                           "providers": []}},
            {"id": "ext-b", "name": "扩展乙", "file": str(self.market / "gone.zip"),
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
        self.manager = _FakeManager(self.data)
        # 装进宿主根是真复制 + 真跑 node，另一层在 tests/test_qqagent_host_install.py 里验；
        # 这里只钉"装成功"，让本文件的用例继续只关心启用态与文案。
        self._host_install = mock.patch(
            "src.qqagent_host.install_market_extension",
            side_effect=self._fake_host_install)
        self._host_install.start()
        self.addCleanup(self._host_install.stop)

    def _fake_host_install(self, data_dir, zip_path, extension_id="", force=False,
                           **kwargs):
        ident = str(extension_id or "")
        root = Path(data_dir) / "qq-only" / "qqagent_host"
        return {"ok": True, "id": ident, "hostRoot": str(root),
                "path": str(root / "plugins" / ident) if ident else "",
                "manifest": {}, "tools": [], "hooks": [], "probe": {}}

    def _client(self, bridge=None):
        from src.qq_app import create_app

        if bridge is not None:
            self.manager._plugins["qqagent_bridge"] = bridge
        app = create_app()
        app.state.bot = SimpleNamespace(plugins=self.manager)
        return TestClient(app)

    def _state_file(self) -> Path:
        return self.data / "qq-only" / "qqagent_extensions.json"

    # ── GET /api/extensions ────────────────────────────────────────────
    def test_list_returns_items_and_summary(self):
        body = self._client().get("/api/extensions").json()
        self.assertTrue(body["success"])
        self.assertTrue(body["ok"])
        self.assertEqual([item["id"] for item in body["items"]], ["ext-a", "ext-b"])
        self.assertFalse(any(item["enabled"] for item in body["items"]),
                         "市场扩展默认必须停用")
        self.assertEqual(body["summary"]["total"], 2)
        self.assertEqual(body["summary"]["enabled"], 0)

    def test_list_supports_layer_and_q_filters(self):
        client = self._client()
        self.assertEqual(len(client.get("/api/extensions?layer=catalog").json()["items"]), 2)
        self.assertEqual(client.get("/api/extensions?layer=builtin").json()["items"], [])
        hit = client.get("/api/extensions?q=扩展乙").json()["items"]
        self.assertEqual([item["id"] for item in hit], ["ext-b"])
        posted = client.post("/api/extensions", json={"q": "甲"}).json()["items"]
        self.assertEqual([item["id"] for item in posted], ["ext-a"])

    # ── set-enabled / batch-enabled ────────────────────────────────────
    def test_set_enabled_round_trip(self):
        client = self._client()
        body = client.post("/api/extensions/set-enabled",
                           json={"id": "ext-a", "enabled": True}).json()
        self.assertTrue(body["success"])
        self.assertTrue(body["item"]["enabled"])
        self.assertEqual(body["item"]["id"], "ext-a")
        self.assertEqual(body["message"], "已启用 ext-a")
        payload = json.loads(self._state_file().read_text(encoding="utf-8"))
        self.assertEqual(payload["extensions"]["ext-a"]["enabled"], True)
        listed = client.get("/api/extensions").json()
        self.assertEqual(listed["summary"]["enabled"], 1)
        self.assertTrue(listed["items"][0]["enabled"], "启用项排在最前")

    def test_set_enabled_validates_input(self):
        client = self._client()
        self.assertEqual(client.post("/api/extensions/set-enabled",
                                     json={"enabled": True}).status_code, 400)
        self.assertEqual(client.post("/api/extensions/set-enabled",
                                     json={"id": "ext-a"}).status_code, 400)
        self.assertEqual(client.post("/api/extensions/set-enabled",
                                     json={"id": "ext-a", "enabled": "yes"}).status_code, 400)

    def test_batch_enabled_is_idempotent(self):
        client = self._client()
        first = client.post("/api/extensions/batch-enabled",
                            json={"ids": ["ext-a", "ext-b"], "enabled": True}).json()
        self.assertEqual(first["count"], 2)
        self.assertTrue(first["ok"])
        second = client.post("/api/extensions/batch-enabled",
                             json={"ids": ["ext-a", "ext-b"], "enabled": True}).json()
        self.assertEqual(second["count"], 2)
        enabled = extension_registry.enabled_ids(self.data)
        self.assertEqual(enabled, ["ext-a", "ext-b"])
        off = client.post("/api/extensions/batch-enabled",
                          json={"ids": ["ext-a", "ext-b"], "enabled": False}).json()
        self.assertFalse(off["enabled"])
        self.assertEqual(extension_registry.enabled_ids(self.data), [])
        self.assertEqual(client.post("/api/extensions/batch-enabled",
                                     json={"ids": "ext-a", "enabled": True}).status_code, 400)

    # ── status ─────────────────────────────────────────────────────────
    def test_status_without_bridge_is_not_an_error(self):
        body = self._client().get("/api/extensions/status").json()
        self.assertTrue(body["success"])
        self.assertFalse(body["available"])
        self.assertFalse(body["loaded"])
        self.assertIn("未加载", body["message"])
        self.assertTrue(body["log_path"].endswith("qqagent_bridge_calls.jsonl"))

    def test_status_passes_bridge_state_through(self):
        body = self._client(_FakeBridge()).get("/api/extensions/status").json()
        self.assertTrue(body["available"])
        self.assertEqual(body["calls"], 3)
        self.assertEqual(body["enabled_extensions"], ["ext-a"])
        self.assertEqual(body["capabilities"][0]["extensionId"], "ext-a")

    def test_status_survives_bridge_error(self):
        body = self._client(_FakeBridge(fail=True)).get("/api/extensions/status").json()
        self.assertFalse(body["available"])
        self.assertIn("boom", body["message"])

    # ── scan ───────────────────────────────────────────────────────────
    def test_scan_without_refresh_skips_bridge(self):
        bridge = _FakeBridge()
        body = self._client(bridge).post("/api/extensions/scan", json={}).json()
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["summary"]["total"], 2)
        self.assertIsNone(body["bridge"])
        self.assertEqual(bridge.refreshed, 0)

    def test_scan_with_refresh_calls_bridge_refresh_tools(self):
        bridge = _FakeBridge()
        body = self._client(bridge).post(
            "/api/extensions/scan", json={"refresh": True}).json()
        self.assertEqual(bridge.refreshed, 1)
        self.assertEqual(body["bridge"]["tools"], ["ext-a__ping"])
        self.assertTrue(body["ok"])

    # ── install ────────────────────────────────────────────────────────
    def test_install_requires_confirmation(self):
        client = self._client()
        self.assertEqual(client.post("/api/extensions/install",
                                     json={"id": "ext-a"}).status_code, 400)
        self.assertEqual(client.post("/api/extensions/install",
                                     json={"id": "ext-a", "confirmed": False}).status_code, 400)
        self.assertEqual(client.post("/api/extensions/install",
                                     json={"confirmed": True}).status_code, 400)

    def test_install_reuses_market_installer_and_registers_disabled(self):
        client = self._client()
        catalog = {"items": [{"id": "ext-a", "name": "扩展甲"}]}
        installed = {"ok": True, "path": str(self.data / "qqagent_extensions" / "ext-a")}
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value=catalog) as load_catalog, \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed) as install:
            body = client.post("/api/extensions/install",
                               json={"id": "ext-a", "confirmed": True}).json()
        self.assertTrue(body["success"])
        self.assertEqual(body["extension"], installed)
        self.assertFalse(body["item"]["enabled"], "安装后默认停用")
        self.assertEqual(body["item"]["note"], "市场安装，默认停用")
        self.assertTrue(load_catalog.called)
        self.assertEqual(install.call_args.args[1], {"id": "ext-a", "name": "扩展甲"})
        self.assertFalse(extension_registry.is_enabled("ext-a", self.data))

    def test_install_unknown_id_is_rejected(self):
        client = self._client()
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": []}):
            response = client.post("/api/extensions/install",
                                   json={"id": "nope", "confirmed": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("市场清单里没有", response.json()["detail"])


    # ── 安装即启用（enable 字段）────────────────────────────────────────
    def test_install_enable_true_switches_on_ready_extension(self):
        client = self._client()
        installed = {"ok": True, "path": str(self.data / "qqagent_extensions" / "ext-a")}
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": [{"id": "ext-a", "name": "扩展甲"}]}), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed):
            body = client.post("/api/extensions/install",
                               json={"id": "ext-a", "confirmed": True,
                                     "enable": True}).json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["enabled"])
        self.assertTrue(body["item"]["enabled"])
        self.assertEqual(body["item"]["note"], "市场安装并启用")
        self.assertIn("并在能力桥启用", body["message"])
        self.assertTrue(extension_registry.is_enabled("ext-a", self.data))

    def test_install_enable_true_refuses_blocked_extension(self):
        client = self._client()
        installed = {"ok": True, "path": str(self.data / "qqagent_extensions" / "ext-b")}
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": [{"id": "ext-b", "name": "扩展乙"}]}), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed):
            body = client.post("/api/extensions/install",
                               json={"id": "ext-b", "confirmed": True,
                                     "enable": True}).json()
        # ext-b 的 zip 在清单里指向不存在的文件 => T6 + zip-missing：装上也不启用。
        self.assertTrue(body["ok"], "安装本身必须成功")
        self.assertFalse(body["enabled"])
        self.assertFalse(body["item"]["enabled"])
        self.assertIn("未启用", body["message"])
        self.assertIn("T6-disabled", body["message"])
        self.assertIn("zip-missing", body["message"])
        self.assertTrue(body["item"]["note"].startswith("市场安装，未启用："))
        self.assertFalse(extension_registry.is_enabled("ext-b", self.data))

    def test_install_without_enable_keeps_disabled_and_says_so(self):
        client = self._client()
        installed = {"ok": True, "path": str(self.data / "qqagent_extensions" / "ext-a")}
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": [{"id": "ext-a", "name": "扩展甲"}]}), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed):
            body = client.post("/api/extensions/install",
                               json={"id": "ext-a", "confirmed": True}).json()
        self.assertFalse(body["enabled"])
        self.assertFalse(body["item"]["enabled"])
        self.assertIn("默认停用", body["message"])
        self.assertFalse(extension_registry.is_enabled("ext-a", self.data))

    # ── 市场对话框走的那条端点也要认得 enable ───────────────────────────
    def test_market_install_forwards_enable_to_registry(self):
        client = self._client()
        installed = {"id": "ext-a", "ok": True, "path": "/tmp/ext-a"}
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": [{"id": "ext-a", "name": "扩展甲"}]}), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed):
            body = client.post("/api/qqagent/market/install",
                               json={"id": "ext-a", "enable": True}).json()
        self.assertTrue(body["success"])
        self.assertTrue(body["enabled"])
        self.assertEqual(body["item"]["id"], "ext-a")
        self.assertEqual(body["item"]["source"], "market")
        self.assertIn("并在能力桥启用", body["message"])
        self.assertTrue(extension_registry.is_enabled("ext-a", self.data))

    def test_market_install_without_enable_stays_disabled(self):
        client = self._client()
        installed = {"id": "ext-a", "ok": True, "path": "/tmp/ext-a"}
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": [{"id": "ext-a", "name": "扩展甲"}]}), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed):
            body = client.post("/api/qqagent/market/install",
                               json={"id": "ext-a"}).json()
        self.assertFalse(body["enabled"])
        self.assertFalse(body["item"]["enabled"])
        self.assertIn("默认停用", body["message"])
        self.assertFalse(extension_registry.is_enabled("ext-a", self.data))

    # ── enable 三态：省略 = 不动已登记状态（install 只负责安装）────────
    def test_reinstall_without_enable_keeps_enabled_state(self):
        client = self._client()
        installed = {"id": "ext-a", "ok": True, "path": "/tmp/ext-a"}
        catalog = {"items": [{"id": "ext-a", "name": "扩展甲"}]}
        with mock.patch("src.qqagent_market.load_catalog", return_value=catalog), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed), \
                mock.patch("src.extension_registry._now",
                           return_value="2020-01-01T00:00:00"):
            first = client.post("/api/extensions/install",
                                json={"id": "ext-a", "confirmed": True,
                                      "enable": True}).json()
        self.assertTrue(first["item"]["enabled"])
        self.assertEqual(first["item"]["note"], "市场安装并启用")
        with mock.patch("src.qqagent_market.load_catalog", return_value=catalog), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed), \
                mock.patch("src.extension_registry._now",
                           return_value="2026-01-01T00:00:00"):
            second = client.post("/api/extensions/install",
                                 json={"id": "ext-a", "confirmed": True}).json()
        self.assertEqual(second["item"], first["item"], "省略 enable 时条目必须原样保留")
        self.assertTrue(second["item"]["enabled"])
        self.assertEqual(second["item"]["note"], "市场安装并启用",
                         "不能把 note 改写成默认停用")
        self.assertEqual(second["item"]["updatedAt"], "2020-01-01T00:00:00",
                         "重装没改动状态就不该刷新 updatedAt")
        self.assertEqual(second["message"], "已安装 ext-a，启用状态保持为：已启用")
        self.assertTrue(extension_registry.is_enabled("ext-a", self.data))

    def test_explicit_disable_overrides_previous_enabled_state(self):
        client = self._client()
        installed = {"id": "ext-a", "ok": True, "path": "/tmp/ext-a"}
        catalog = {"items": [{"id": "ext-a", "name": "扩展甲"}]}
        with mock.patch("src.qqagent_market.load_catalog", return_value=catalog), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed):
            client.post("/api/extensions/install",
                        json={"id": "ext-a", "confirmed": True, "enable": True})
            body = client.post("/api/extensions/install",
                               json={"id": "ext-a", "confirmed": True,
                                     "enable": False}).json()
        self.assertFalse(body["enabled"], "显式传 false 就是意图，别拦")
        self.assertFalse(body["item"]["enabled"])
        self.assertEqual(body["message"], "已安装 ext-a，已按你的选择停用")
        self.assertEqual(body["item"]["note"], "市场安装，按你的选择停用")
        self.assertFalse(extension_registry.is_enabled("ext-a", self.data))

    def test_market_install_keeps_enabled_state_when_body_omits_enable(self):
        client = self._client()
        installed = {"id": "ext-a", "ok": True, "path": "/tmp/ext-a"}
        catalog = {"items": [{"id": "ext-a", "name": "扩展甲"}]}
        with mock.patch("src.qqagent_market.load_catalog", return_value=catalog), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value=installed):
            client.post("/api/qqagent/market/install",
                        json={"id": "ext-a", "enable": True})
            # bot_control_api 只发 id：这条路径重装后不能丢掉启用状态。
            body = client.post("/api/qqagent/market/install",
                               json={"id": "ext-a"}).json()
        self.assertTrue(body["enabled"])
        self.assertTrue(body["item"]["enabled"])
        self.assertEqual(body["message"], "已安装 ext-a，启用状态保持为：已启用")
        self.assertTrue(extension_registry.is_enabled("ext-a", self.data))

    def test_enable_must_be_boolean(self):
        client = self._client()
        for path, payload in (
                ("/api/extensions/install", {"id": "ext-a", "confirmed": True}),
                ("/api/qqagent/market/install", {"id": "ext-a"})):
            response = client.post(path, json={**payload, "enable": "yes"})
            self.assertEqual(response.status_code, 400, path)
            self.assertIn("布尔值", response.json()["detail"])




    # ── /api/qqagent/extensions 兼容层（外部控制 API 用）────────────────
    def test_qqagent_extensions_get_includes_enabled_state(self):
        body = self._client().get("/api/qqagent/extensions").json()
        self.assertTrue(body["success"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 2)
        items = {item["id"]: item for item in body["items"]}
        self.assertIn("enabled", items["ext-a"])
        self.assertIn("known", items["ext-a"])
        self.assertFalse(items["ext-a"]["enabled"])
        self.assertIn("summary", body)

    def test_qqagent_extensions_post_enable_toggle_and_state(self):
        client = self._client()
        posted = client.post("/api/qqagent/extensions/ext-a/enable")
        self.assertEqual(posted.status_code, 200, posted.text)
        self.assertTrue(posted.json()["enabled"])
        state = json.loads(self._state_file().read_text(encoding="utf-8"))
        self.assertTrue(state["extensions"]["ext-a"]["enabled"])

        toggled = client.post("/api/qqagent/extensions/ext-a/toggle")
        self.assertEqual(toggled.status_code, 200, toggled.text)
        self.assertFalse(toggled.json()["enabled"])

        stated = client.post("/api/extensions/ext-b", json={"state": "启用"})
        self.assertEqual(stated.status_code, 200, stated.text)
        self.assertTrue(stated.json()["enabled"])
        detail = client.get("/api/qqagent/extensions/ext-b").json()
        self.assertTrue(detail["item"]["enabled"])

class InstallLocalBatchPluginsDirTests(unittest.TestCase):
    """回归：install-local-batch 曾漏传 plugins_dir，existing 恒 False、覆盖提示永不出现。"""

    SOURCE = (
        "from src.plugins.manager import PluginBase, PluginMeta\n"
        "\n"
        "class Demo(PluginBase):\n"
        "    META = PluginMeta(id=\"demo\", name=\"演示\", version=\"1.0\")\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        (self.data / "qq-only").mkdir(parents=True)
        self.manager = _FakeManager(self.data)
        self.manager.custom_plugins_dir.mkdir(parents=True)
        self.plugin = self.root / "demo.py"
        self.plugin.write_text(self.SOURCE, encoding="utf-8")

    def _client(self):
        from src.qq_app import create_app

        app = create_app()
        app.state.bot = SimpleNamespace(plugins=self.manager)
        return TestClient(app)

    def test_install_local_batch_passes_plugins_dir(self):
        real = plugin_installer.inspect_local
        seen = {}

        def spy(*args, **kwargs):
            seen["args"] = args
            return real(*args, **kwargs)

        with mock.patch("src.qq_app.plugin_installer.inspect_local", side_effect=spy), \
                mock.patch("src.qq_app.plugin_installer.install_from_file",
                           return_value={"plugin_id": "demo"}), \
                mock.patch("src.qq_app.plugin_install_service.finish_batch",
                           return_value={"success": True, "items": []}):
            response = self._client().post(
                "/api/plugins/install-local-batch",
                json={"paths": [str(self.plugin)], "confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(seen["args"],
                         (str(self.plugin), "", self.manager.custom_plugins_dir))

    def test_existing_only_appears_when_plugins_dir_is_passed(self):
        (self.manager.custom_plugins_dir / "demo.py").write_text(self.SOURCE, encoding="utf-8")
        without = plugin_installer.inspect_local(str(self.plugin))
        with_dir = plugin_installer.inspect_local(
            str(self.plugin), "", self.manager.custom_plugins_dir)
        self.assertEqual(with_dir["plugin_id"], "demo")
        self.assertFalse(without["existing"], "漏传 plugins_dir 时永远看不到已安装")
        self.assertTrue(with_dir["existing"])


if __name__ == "__main__":
    unittest.main()
