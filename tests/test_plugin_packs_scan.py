"""src/plugin_packs.py 的只读扫描口径：目录、清单、版本、entry、ZIP、重复 id、隔离。

夹具全部在 tempfile 里现造；真实的 plugin_packages/ 只读，不写仓库 data/，不 import 任何
包的入口代码——唯一的挂载验证（test_broken_pack_does_not_break_manager 与
test_real_pack_mounts_in_manager）把包复制进临时目录，交给 PluginManager 在临时根里跑。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest
import zipfile

from src import plugin_packs
from src.plugins.manager import PluginManager

HOST = "5.2.100"


def manifest(**overrides) -> dict:
    data = {
        "id": "demo-pack",
        "name": "演示包",
        "version": "1.0.0",
        "api_version": 1,
        "entry": "demo.py",
        "host": "WhaleGirlQQ",
        "min_host_version": "5.2.0",
        "permissions": ["read_message"],
        "description": "测试用包",
    }
    data.update(overrides)
    return data


class PackScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.packs = self.root / "plugin_packages"
        self.packs.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    # ── 夹具 ────────────────────────────────────────────────────────────────
    def add_pack(self, name="demo-pack", data=None, raw=None,
                 entry_name="demo.py", entry_code="PLUGIN = None\n"):
        folder = self.packs / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "README.md").write_text("# demo\n", encoding="utf-8")
        if entry_name:
            (folder / entry_name).write_text(entry_code, encoding="utf-8")
        if raw is not None:
            (folder / "plugin.json").write_text(raw, encoding="utf-8")
        else:
            payload = manifest(id=name) if data is None else data
            (folder / "plugin.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return folder

    def add_zip(self, zip_name="demo.zip", entries=None):
        path = self.packs / zip_name
        with zipfile.ZipFile(path, "w") as archive:
            for name, text in (entries or {}).items():
                archive.writestr(name, text)
        return path

    def scan(self):
        return plugin_packs.scan(self.packs, host_version_text=HOST)

    def only(self, report):
        self.assertEqual(len(report["packs"]), 1, report["packs"])
        return report["packs"][0]

    @staticmethod
    def codes(pack):
        return [issue["code"] for issue in pack["issues"]]

    # ── A. 真实包 ───────────────────────────────────────────────────────────
    def test_repo_packs_are_valid(self):
        host = plugin_packs.host_version()
        self.assertTrue(host, "读不到宿主版本（version.txt / runtime_identity）")
        report = plugin_packs.scan(plugin_packs.default_packs_dir(),
                                   host_version_text=host)
        self.assertTrue(report["ok"], report["issues"])
        by_id = {pack["id"]: pack for pack in report["packs"]}
        for expected in ("pack-repeat-guard", "pack-burst-merge", "pack-sleep-quiet",
                         "voice_console"):
            self.assertIn(expected, by_id)
        for pack in report["packs"]:
            self.assertTrue(pack["ok"], (pack["id"], pack["issues"]))
            self.assertEqual(pack["source"], "dir")
            self.assertTrue(pack["entry"], pack["id"])
            self.assertTrue(pack["permissions"], pack["id"])
            self.assertTrue(pack["description"], pack["id"])
        for pack_id in ("pack-repeat-guard", "pack-burst-merge", "pack-sleep-quiet"):
            pack = by_id[pack_id]
            self.assertEqual(pack["host"], "WhaleGirlQQ")
            self.assertEqual(pack["api_version"], plugin_packs.MOD_API_VERSION)
            self.assertTrue(pack["min_host_version"])
            self.assertNotIn("host-too-old", self.codes(pack))
        self.assertEqual(report["counts"]["bad"], 0)

    def test_repo_packs_are_under_the_declared_dir(self):
        self.assertEqual(plugin_packs.default_packs_dir().name, "plugin_packages")
        self.assertTrue((plugin_packs.default_packs_dir()
                         / "pack-repeat-guard" / "plugin.json").is_file())

    def test_repo_packs_ship_readme_and_example_config(self):
        root = plugin_packs.default_packs_dir()
        for pack_id in ("pack-repeat-guard", "pack-burst-merge", "pack-sleep-quiet"):
            folder = root / pack_id
            self.assertTrue((folder / "README.md").is_file(), pack_id)
            example = folder / "config.example.json"
            self.assertTrue(example.is_file(), pack_id)
            data = json.loads(example.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict, pack_id)
            self.assertIn("enabled", data, pack_id)

    # ── B. 清单问题 ─────────────────────────────────────────────────────────
    def test_missing_field_is_reported(self):
        data = manifest()
        del data["version"]
        self.add_pack("demo-pack", data=data)
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("missing-field", self.codes(pack))
        missing = [i for i in pack["issues"] if i["code"] == "missing-field"]
        self.assertEqual(missing[0]["field"], "version")

    def test_bad_json_is_reported(self):
        self.add_pack("demo-pack", raw="{ not json")
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("bad-json", self.codes(pack))

    def test_manifest_not_object_is_reported(self):
        self.add_pack("demo-pack", raw="[]")
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("bad-manifest", self.codes(pack))

    def test_missing_manifest_is_reported(self):
        folder = self.packs / "empty-pack"
        folder.mkdir()
        (folder / "notes.txt").write_text("x", encoding="utf-8")
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("missing-manifest", self.codes(pack))

    def test_manifest_too_large_is_rejected(self):
        data = manifest(description="x" * (plugin_packs.MAX_MANIFEST_BYTES + 10))
        self.add_pack("demo-pack", data=data)
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("manifest-too-large", self.codes(pack))

    def test_id_mismatch_is_reported(self):
        self.add_pack("wrong-name", data=manifest(id="demo-pack"))
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("id-mismatch", self.codes(pack))

    def test_bad_permissions_and_api_version(self):
        self.add_pack("demo-pack", data=manifest(permissions="read_message",
                                                 api_version=2))
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("bad-permissions", self.codes(pack))
        self.assertIn("api-version-unsupported", self.codes(pack))

    def test_unknown_permission_is_only_a_warning(self):
        self.add_pack("demo-pack", data=manifest(permissions=["read_message",
                                                              "loopback_tts"]))
        pack = self.only(self.scan())
        self.assertTrue(pack["ok"], pack["issues"])
        self.assertIn("unknown-permission", self.codes(pack))
        warning = [i for i in pack["issues"] if i["code"] == "unknown-permission"][0]
        self.assertEqual(warning["severity"], plugin_packs.SEVERITY_WARNING)

    # ── C. 版本与入口 ───────────────────────────────────────────────────────
    def test_host_too_old_is_rejected(self):
        self.add_pack("demo-pack", data=manifest(min_host_version="9.9.9"))
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("host-too-old", self.codes(pack))

    def test_host_requirement_satisfied_is_accepted(self):
        self.add_pack("demo-pack", data=manifest(min_host_version="5.1.2"))
        pack = self.only(self.scan())
        self.assertTrue(pack["ok"], pack["issues"])

    def test_entry_missing_is_rejected(self):
        self.add_pack("demo-pack", data=manifest(entry="nope.py"))
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("entry-missing", self.codes(pack))

    def test_entry_escape_is_rejected(self):
        self.add_pack("demo-pack", data=manifest(entry="../escape.py"))
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("entry-escape", self.codes(pack))

    def test_entry_with_function_suffix_checks_file_only(self):
        self.add_pack("demo-pack", data=manifest(entry="demo.py:setup"))
        pack = self.only(self.scan())
        self.assertTrue(pack["ok"], pack["issues"])

    # ── D. ZIP 包 ───────────────────────────────────────────────────────────
    def test_zip_with_top_directory(self):
        self.add_zip("demo.zip", {
            "demo-pack/plugin.json": json.dumps(manifest(id="demo-pack")),
            "demo-pack/demo.py": "PLUGIN = None\n",
        })
        pack = self.only(self.scan())
        self.assertTrue(pack["ok"], pack["issues"])
        self.assertEqual(pack["source"], "zip")
        self.assertEqual(pack["id"], "demo-pack")
        self.assertEqual(pack["files"], 2)

    def test_zip_manifest_at_root(self):
        self.add_zip("root-pack.zip", {
            "plugin.json": json.dumps(manifest(id="root-pack")),
            "demo.py": "PLUGIN = None\n",
        })
        pack = self.only(self.scan())
        self.assertTrue(pack["ok"], pack["issues"])
        self.assertEqual(pack["id"], "root-pack")

    def test_zip_entry_missing(self):
        self.add_zip("demo.zip", {
            "demo-pack/plugin.json": json.dumps(manifest(id="demo-pack")),
        })
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("entry-missing", self.codes(pack))

    def test_zip_unsafe_path(self):
        self.add_zip("demo.zip", {
            "../evil/plugin.json": json.dumps(manifest(id="evil")),
        })
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("zip-unsafe-path", self.codes(pack))

    def test_bad_zip_is_reported(self):
        (self.packs / "broken.zip").write_bytes(b"not a zip at all")
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("bad-zip", self.codes(pack))

    def test_zip_multiple_packs_is_rejected(self):
        self.add_zip("two.zip", {
            "a/plugin.json": json.dumps(manifest(id="a")),
            "b/plugin.json": json.dumps(manifest(id="b")),
        })
        pack = self.only(self.scan())
        self.assertFalse(pack["ok"])
        self.assertIn("zip-multiple-packs", self.codes(pack))

    # ── E. 重复 id 与隔离 ───────────────────────────────────────────────────
    def test_duplicate_id_between_dir_and_zip(self):
        self.add_pack("demo-pack", data=manifest(id="demo-pack"))
        self.add_zip("demo-pack-copy.zip", {
            "plugin.json": json.dumps(manifest(id="demo-pack")),
            "demo.py": "PLUGIN = None\n",
        })
        report = self.scan()
        self.assertEqual(len(report["packs"]), 2)
        dir_pack = next(p for p in report["packs"] if p["source"] == "dir")
        zip_pack = next(p for p in report["packs"] if p["source"] == "zip")
        self.assertTrue(dir_pack["ok"], dir_pack["issues"])
        self.assertFalse(zip_pack["ok"])
        self.assertIn("duplicate-id", self.codes(zip_pack))
        self.assertIn("duplicate-id", [i["code"] for i in report["issues"]])
        self.assertFalse(report["ok"])

    def test_bad_pack_does_not_affect_others(self):
        self.add_pack("good-pack", data=manifest(id="good-pack"))
        self.add_pack("bad-pack", raw="{ broken")
        report = self.scan()
        by_id = {pack["id"]: pack for pack in report["packs"]}
        self.assertTrue(by_id["good-pack"]["ok"], by_id["good-pack"]["issues"])
        self.assertFalse(by_id["bad-pack"]["ok"])
        self.assertEqual(report["counts"], {"packs": 2, "ok": 1, "bad": 1})
        self.assertFalse(report["ok"])

    def test_pycache_and_hidden_dirs_are_skipped(self):
        (self.packs / "__pycache__").mkdir()
        (self.packs / ".tmp").mkdir()
        self.add_pack("demo-pack")
        report = self.scan()
        self.assertEqual([pack["id"] for pack in report["packs"]], ["demo-pack"])

    def test_missing_directory_is_reported(self):
        report = plugin_packs.scan(self.root / "absent", host_version_text=HOST)
        self.assertFalse(report["ok"])
        self.assertEqual(report["packs"], [])
        self.assertEqual([i["code"] for i in report["issues"]], ["dir-missing"])

    # ── F. 挂载隔离（真实 PluginManager）────────────────────────────────────
    def test_real_pack_mounts_in_manager(self):
        site = self.root / "custom_plugins"
        site.mkdir()
        shutil.copytree(plugin_packs.default_packs_dir() / "pack-repeat-guard",
                        site / "pack-repeat-guard")
        manager = PluginManager(self.root)
        self.assertIn("pack-repeat-guard", manager._mod_entries)
        self.assertIn("pack_repeat_guard", manager._plugins)

    def test_broken_pack_does_not_break_manager(self):
        site = self.root / "custom_plugins"
        site.mkdir()
        shutil.copytree(plugin_packs.default_packs_dir() / "pack-sleep-quiet",
                        site / "pack-sleep-quiet")
        broken = site / "broken-pack"
        broken.mkdir()
        (broken / "plugin.json").write_text(json.dumps(
            {"id": "broken-pack", "api_version": 1, "entry": "broken.py"}), "utf-8")
        (broken / "broken.py").write_text("raise RuntimeError('boom')\n", "utf-8")
        manager = PluginManager(self.root)
        self.assertIn("pack_sleep_quiet", manager._plugins)
        problems = "\n".join(manager.take_load_problems())
        self.assertIn("broken-pack", problems)


if __name__ == "__main__":
    unittest.main()
