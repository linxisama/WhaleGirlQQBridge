"""插件外部链接 / 本地 ZIP 导入：只校验落盘，不执行。"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import qqagent_market


def make_zip(root, ident="demo-plugin"):
    path = Path(root) / (ident + ".zip")
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr(ident + "/plugin.json", json.dumps({
            "id": ident, "name": "演示插件", "version": "1.2.3",
            "apiVersion": 1, "entry": "index.js"}, ensure_ascii=False))
        archive.writestr(ident + "/index.js", "export function setup(api) {}")
    return path


class ExternalUrlTests(unittest.TestCase):
    def test_only_external_https(self):
        self.assertEqual(qqagent_market._external_url(
            "https://example.com/a.zip"), "https://example.com/a.zip")
        for value in ("http://example.com/a.zip", "https://localhost/a.zip",
                      "https://127.0.0.1/a.zip", "https://192.168.1.2/a.zip",
                      "https://user:pass@example.com/a.zip", "ftp://example.com/a.zip"):
            self.assertEqual(qqagent_market._external_url(value), "", value)

    def test_download_rejects_bad_url_without_network(self):
        with self.assertRaises(ValueError):
            qqagent_market._download_raw("http://localhost/a.zip")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="wp-market-import-"))

    def test_install_zip_writes_archive_and_registry(self):
        source = make_zip(self.data)
        record = qqagent_market.install_zip(self.data, source)
        self.assertEqual(record["id"], "demo-plugin")
        self.assertEqual(record["source"], "local")
        target = Path(record["path"])
        self.assertTrue(target.is_file())
        self.assertEqual(target.parent.name, "qqagent_extensions")
        registry = json.loads((target.parent / "extensions.json").read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in registry], ["demo-plugin"])

    def test_reimport_replaces_same_id_instead_of_duplicating(self):
        source = make_zip(self.data)
        qqagent_market.install_zip(self.data, source)
        record = qqagent_market.install_zip(self.data, source)
        registry = json.loads((Path(record["path"]).parent / "extensions.json").read_text(encoding="utf-8"))
        self.assertEqual(len(registry), 1)

    def test_install_url_uses_downloaded_bytes(self):
        source = make_zip(self.data)
        raw = source.read_bytes()
        with mock.patch("src.qqagent_market._download_raw", return_value=raw) as download:
            record = qqagent_market.install_url(self.data, "https://example.com/demo.zip")
        download.assert_called_once()
        self.assertEqual(record["source"], "url")
        self.assertEqual(record["name"], "演示插件")
        self.assertTrue(Path(record["path"]).is_file())

    def test_install_zip_rejects_missing_file(self):
        with self.assertRaises(ValueError):
            qqagent_market.install_zip(self.data, self.data / "missing.zip")

    def test_install_zip_rejects_non_zip(self):
        bad = self.data / "bad.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(ValueError):
            qqagent_market.install_zip(self.data, bad)


if __name__ == "__main__":
    unittest.main()
