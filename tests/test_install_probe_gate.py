"""装上 ≠ 能跑：三条安装路由的加载探测门禁（probe gate）。

市场端(/api/qqagent/market/install)、扩展管理端(/api/extensions/install)、本地 zip
(/api/extensions/install-local) 共用 src/qq_app.py 的 _enable_after_probe：
探测没过就按停用登记，绝不留下"每次调用都失败"的白名单。
"""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from src import extension_registry
from src.qq_app import _enable_after_probe, _probe_failure_text, create_app

BAD_JS = "throw new Error('gate-boom 加载即失败');\n"


def build_zip(folder, ident, js):
    path = Path(folder) / ("%s.zip" % ident)
    manifest = {"id": ident, "name": "演示包 " + ident, "version": "1.0.0",
                "apiVersion": 1, "entry": "index.js"}
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("%s/plugin.json" % ident, json.dumps(manifest, ensure_ascii=False))
        archive.writestr("%s/index.js" % ident, js)
    return path


class _FakeManager:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.custom_plugins_dir = self.data_dir / "custom_plugins"
        self._plugins = {}


class EnableAfterProbeTests(unittest.TestCase):
    """纯函数契约：只有拿到"探测失败"这条证据才改判启用状态。"""

    def test_probe_ok_keeps_enable(self):
        self.assertEqual(_enable_after_probe(True, {"probe": {"ok": True}}), (True, ""))

    def test_probe_failed_downgrades_to_disabled(self):
        host = {"probe": {"ok": False, "error": {"code": "load-failed",
                                                 "message": "入口文件不存在：nope.js"}}}
        self.assertEqual(_enable_after_probe(True, host),
                         (False, "入口文件不存在：nope.js"))

    def test_no_probe_evidence_does_not_change_state(self):
        # legacy 只下载 / 调用方注入的 host：没有探测信息就没有证据。
        self.assertEqual(_enable_after_probe(True, {}), (True, ""))
        self.assertEqual(_enable_after_probe(True, {"probe": {}}), (True, ""))

    def test_disabled_request_stays_disabled(self):
        host = {"probe": {"ok": False, "error": {"message": "boom"}}}
        self.assertEqual(_enable_after_probe(False, host), (False, ""))

    def test_omitted_enable_is_not_rewritten(self):
        self.assertEqual(_enable_after_probe(None, {"probe": {"ok": False}}), (None, ""))

    def test_failure_text_sources(self):
        self.assertEqual(_probe_failure_text({"error": {"detail": "d"}}), "d")
        self.assertEqual(_probe_failure_text({"error": {"message": ""},
                                              "loadError": "load-x"}), "load-x")
        self.assertEqual(_probe_failure_text({"error": "plain"}), "plain")
        self.assertEqual(_probe_failure_text({}), "加载探测失败")


class RouteProbeGateTests(unittest.TestCase):
    """两条市场侧路由：装进宿主但探测失败时不许登记成启用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        (self.data / "qq-only").mkdir(parents=True)
        self.market = self.root / "market"
        self.market.mkdir()
        # 清单里的 zip 要真存在，否则 registry 把扩展判成 T6（zip-missing）而挡住启用，
        # 这条用例就测不到 probe 门禁了。
        (self.market / "ext-a.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        (self.market / "all-plugins-v4.json").write_text(
            json.dumps([{"id": "ext-a", "name": "扩展甲", "ok": True,
                         "file": str(self.market / "ext-a.zip"),
                         "extension": {"tools": [], "hooks": [], "providers": []}}],
                       ensure_ascii=False), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"QQAGENT_MANIFEST_DIR": str(self.market)})
        env.start()
        self.addCleanup(env.stop)
        extension_registry.invalidate()
        self.addCleanup(extension_registry.invalidate)
        app = create_app()
        app.state.bot = SimpleNamespace(plugins=_FakeManager(self.data))
        self.client = TestClient(app)

    def _host(self, probe):
        root = self.data / "qq-only" / "qqagent_host"
        return {"ok": True, "id": "ext-a", "hostRoot": str(root),
                "path": str(root / "plugins" / "ext-a"), "manifest": {},
                "tools": [], "hooks": [], "probe": probe}

    def _post_market(self, probe, body=None):
        payload = {"id": "ext-a", "enable": True}
        payload.update(body or {})
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": [{"id": "ext-a", "name": "扩展甲"}]}), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value={"id": "ext-a", "ok": True}), \
                mock.patch("src.qq_app._install_market_into_host",
                           return_value=self._host(probe)):
            return self.client.post("/api/qqagent/market/install", json=payload)

    def test_market_install_probe_failure_keeps_disabled(self):
        response = self._post_market(
            {"ok": False, "error": {"code": "load-failed", "message": "加载即失败"}})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"] if "ok" in body else body["success"])
        self.assertFalse(body["enabled"])
        self.assertFalse(extension_registry.is_enabled("ext-a", self.data))
        self.assertIn("宿主加载探测失败", body["message"])
        self.assertIn("加载即失败", body["message"])
        self.assertIn("宿主加载探测失败", body["item"]["note"])
        stored = extension_registry.load(self.data)["extensions"]["ext-a"]
        # 落库的说明也得说实话：用户请求的是启用，是探测把它拦下的。
        self.assertIn("宿主加载探测失败", stored["note"])

    def test_market_install_probe_ok_enables(self):
        response = self._post_market({"ok": True})
        body = response.json()
        self.assertTrue(body["enabled"])
        self.assertTrue(extension_registry.is_enabled("ext-a", self.data))
        self.assertNotIn("探测失败", body["message"])

    def test_extensions_install_probe_failure_keeps_disabled(self):
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": [{"id": "ext-a", "name": "扩展甲"}]}), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value={"id": "ext-a", "ok": True}), \
                mock.patch("src.qq_app._install_market_into_host",
                           return_value=self._host({"ok": False,
                                                    "loadError": "boom-load"})):
            response = self.client.post("/api/extensions/install",
                                        json={"id": "ext-a", "confirmed": True,
                                              "enable": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["enabled"])
        self.assertFalse(extension_registry.is_enabled("ext-a", self.data))
        self.assertIn("boom-load", body["message"])

    def test_install_echoes_host_side_switches(self):
        # 宿主 config.json 里写的 enabled/主人号码以前只落盘不回显，用户装完看不到；
        # 三条安装路由都必须把这两块原样带回来。
        host = self._host({"ok": True})
        host["hostEnabled"] = {"ok": True, "id": "ext-a", "enabled": True, "path": "cfg"}
        host["ownerSeed"] = {"ok": True, "id": "ext-a", "keys": ["ids"], "ids": ["10001"]}
        catalog = {"items": [{"id": "ext-a", "name": "扩展甲"}]}
        with mock.patch("src.qqagent_market.load_catalog", return_value=catalog), \
                mock.patch("src.qqagent_market.install_extension",
                           return_value={"id": "ext-a", "ok": True}), \
                mock.patch("src.qq_app._install_market_into_host", return_value=host):
            market = self.client.post("/api/qqagent/market/install",
                                      json={"id": "ext-a", "enable": True})
            self.assertEqual(market.status_code, 200)
            body = market.json()
            self.assertTrue(body["hostEnabled"]["enabled"])
            self.assertEqual(body["ownerSeed"]["ids"], ["10001"])
            other = self.client.post("/api/extensions/install",
                                     json={"id": "ext-a", "confirmed": True, "enable": True})
            self.assertEqual(other.status_code, 200)
            body2 = other.json()
            self.assertTrue(body2["hostEnabled"]["enabled"])
            self.assertEqual(body2["ownerSeed"]["keys"], ["ids"])

    def test_install_local_probe_failure_keeps_disabled(self):
        zip_path = build_zip(self.root, "gate-demo", BAD_JS)
        response = self.client.post("/api/extensions/install-local",
                                    json={"zipPath": str(zip_path), "confirmed": True,
                                          "enable": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"] is False)
        self.assertFalse(body["enabled"])
        self.assertFalse(extension_registry.is_enabled("gate-demo", self.data))
        self.assertIn("宿主加载探测失败", body["message"])
        self.assertIn("宿主加载探测失败", body["item"]["note"])
        stored = extension_registry.load(self.data)["extensions"]["gate-demo"]
        self.assertIn("宿主加载探测失败", stored["note"])


if __name__ == "__main__":
    unittest.main()
