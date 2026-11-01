"""QQ Agent 市集清单/扩展包落盘的离线测试。"""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import qqagent_market


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/plugin.json", json.dumps({
            "id": "demo", "name": "演示扩展", "version": "1.2.3",
            "description": "测试用", "capabilities": ["message.owner-check"],
        }, ensure_ascii=False))
        archive.writestr("demo/index.js", "export const hooks = {};\n")
    return buffer.getvalue()


class _StreamResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield self._payload


class _Client:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        return _StreamResponse(self.payload)


class MarketCatalogTests(unittest.TestCase):
    def test_normalize_filters_bad_ids_and_marks_installable(self):
        data = qqagent_market.normalize({"categories": {"utility": "实用工具"}, "items": [
            {"id": "ok", "name": "可用", "downloadUrl": "/api/community/plugins/ok/download",
             "status": "approved", "downloads": 3},
            {"id": "../bad", "name": "坏 ID"},
            {"id": "pending", "name": "待审核", "status": "pending"},
            {"id": "external", "name": "外链",
             "downloadUrl": "https://evil.example/api/community/plugins/x/download"},
        ]})
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["installable"], 1)
        item = next(x for x in data["items"] if x["id"] == "ok")
        self.assertTrue(item["downloadUrl"].startswith("https://kondius.cn/"))

    def test_fetch_failure_falls_back_to_cached_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            cached = qqagent_market.save_catalog(data_dir, {"items": [
                {"id": "cached", "name": "缓存", "status": "approved"}
            ]})
            self.assertEqual(cached["count"], 1)
            with mock.patch.object(qqagent_market, "fetch_catalog",
                                   side_effect=RuntimeError("network down")):
                loaded = qqagent_market.load_catalog(data_dir, refresh=True)
            self.assertEqual(loaded["items"][0]["id"], "cached")
            self.assertTrue(loaded["stale"])
            self.assertIn("network down", loaded["error"])

    def test_install_writes_validated_zip_and_record(self):
        item = {"id": "demo", "name": "演示扩展", "version": "1.2.3",
                "downloadUrl": "/api/community/plugins/demo/download"}
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            with mock.patch.object(qqagent_market.httpx, "Client",
                                   return_value=_Client(_zip_bytes())):
                record = qqagent_market.install_extension(data_dir, item)
            self.assertEqual(record["id"], "demo")
            self.assertTrue(Path(record["path"]).is_file())
            self.assertEqual(len(record["sha256"]), 64)
            records = qqagent_market.list_extensions(data_dir)
            self.assertEqual(records[0]["id"], "demo")

    def test_install_rejects_path_traversal(self):
        item = {"id": "bad", "name": "坏包",
                "downloadUrl": "/api/community/plugins/bad/download"}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../evil.py", "print(1)\n")
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            with mock.patch.object(qqagent_market.httpx, "Client",
                                   return_value=_Client(buffer.getvalue())):
                with self.assertRaises(ValueError):
                    qqagent_market.install_extension(data_dir, item)
            self.assertFalse((data_dir / "qqagent_extensions" / "bad.zip").exists())

    def test_failed_replacement_keeps_previous_extension(self):
        item = {"id": "demo", "name": "演示扩展",
                "downloadUrl": "/api/community/plugins/demo/download"}
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            target = data_dir / "qqagent_extensions" / "demo.zip"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"previous-good-package")
            invalid = io.BytesIO()
            with zipfile.ZipFile(invalid, "w") as archive:
                archive.writestr("../escape.py", "print(1)\n")
            with mock.patch.object(qqagent_market.httpx, "Client",
                                   return_value=_Client(invalid.getvalue())):
                with self.assertRaises(ValueError):
                    qqagent_market.install_extension(data_dir, item)
            self.assertEqual(target.read_bytes(), b"previous-good-package")

    def test_build_bridge_catalog_uses_installed_manifest(self):
        item = {"id": "demo", "name": "演示扩展",
                "downloadUrl": "/api/community/plugins/demo/download"}
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            with mock.patch.object(qqagent_market.httpx, "Client",
                                   return_value=_Client(_zip_bytes())):
                record = qqagent_market.install_extension(data_dir, item)
            self.assertEqual(record["capabilities"], ["message.owner-check"])
            info = qqagent_market.build_bridge_catalog(data_dir)
            self.assertEqual(info["count"], 1)
            catalog = json.loads(Path(info["path"]).read_text(encoding="utf-8"))
            entry = catalog["plugins"][0]
            self.assertEqual(entry["id"], "demo")
            self.assertEqual(entry["file"], record["path"])
            self.assertEqual(entry["capabilities"], ["message.owner-check"])
            # zip 条目照样产出，只多一个来源标记；没有宿主扩展时不算被遮蔽。
            self.assertEqual(entry["source"], "zip")
            self.assertNotIn("shadowed", entry)
            self.assertEqual(info["hostCount"], 0)

    def test_find_runtime_prefers_explicit_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host = root / "QQAgent兼容层" / "host-one.mjs"
            host.parent.mkdir(parents=True)
            host.write_text("// host\n", encoding="utf-8")
            source = root / "qq-agent-run"
            (source / "src").mkdir(parents=True)
            (source / "src" / "plugin-loader.js").write_text("// loader\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"QQ_AGENT_HOME": str(root)}):
                found_host, found_source = qqagent_market.find_runtime()
            self.assertEqual(found_host, str(host))
            self.assertEqual(found_source, str(source))


if __name__ == "__main__":
    unittest.main()
