"""src/pack_import.py 的第六种家族 mod-py（目录 mod）：识别 / 预检 / 安装三段同形。

夹具全部在 tempfile 里生成：不碰仓库 data/、不读真实插件市场、不联网、不 import 任何
mod 代码（安装只做落盘，挂载是 PluginManager 的事）。

进程内唯一的替身是 test_overwrite_failure_restores_old 里的 os.replace：让它对目标位的
一次替换抛 OSError，模拟「落盘那一刻目标目录被占用」，用来验证 _Aside 把旧版本挪回。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import import_api, pack_import
from src.plugins.manager import PluginManager

SCHEMA_KEYS = {
    "ok", "kind", "id", "name", "version", "entry", "files", "bytes", "landed",
    "target_dir", "tools", "hooks", "providers", "problems", "notices", "load",
    "installed_but_disabled", "actions", "duration_ms",
}

# 与 tests/test_plugin_mod_mount.py 的 MOD_SRC 同形，假 manager 挂载时能真注册。
MOD_SRC = '''from . import PluginBase, PluginMeta


class Demo(PluginBase):
    META = PluginMeta(id="{pid}", name="{name}", category="utility",
                      description="测试用 mod")

    def on_ingest(self, message, runtime):
        return None
'''


def codes(report, key="problems") -> list:
    return [str(one.get("code") or "") for one in report[key]]


def messages(report, key="problems") -> str:
    return "\n".join(str(one.get("message") or "") for one in report[key])


class ModCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / "src-pkgs"
        self.src.mkdir()
        self.zip_dir = self.root / "zips"
        self.zip_dir.mkdir()
        self.data_dir = self.root / "data"
        self.custom = self.data_dir / "custom_plugins"

    def tearDown(self):
        self._tmp.cleanup()

    # ── 夹具 ───────────────────────────────────────────────────────────────
    def build_dir(self, name, manifest=None, entry_name="main.py",
                  entry_code=None, files=None, raw_manifest=None,
                  api_version=1, base=None):
        folder = (base or self.src) / name
        if folder.exists():
            shutil.rmtree(folder)      # 同一个测试里改装新版本：同名包重建
        folder.mkdir(parents=True)
        if raw_manifest is not None:
            (folder / "plugin.json").write_text(raw_manifest, encoding="utf-8")
        else:
            data = {"id": name, "entry": entry_name}
            if api_version is not None:
                data["api_version"] = api_version
            data.update(manifest or {})
            (folder / "plugin.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if entry_name:
            (folder / entry_name).write_text(
                entry_code or MOD_SRC.format(pid=name, name=name), encoding="utf-8")
        for rel, text in (files or {}).items():
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return folder

    def build_zip(self, folder, name=None):
        name = name or folder.name
        path = self.zip_dir / (name + ".zip")
        with zipfile.ZipFile(path, "w") as zf:
            for item in sorted(folder.rglob("*")):
                if item.is_file():
                    rel = item.relative_to(folder).as_posix()
                    zf.writestr("%s/%s" % (name, rel),
                                item.read_bytes())
        return path

    def tree(self, folder: Path) -> dict:
        folder = Path(folder)
        out = {}
        for item in sorted(folder.rglob("*")):
            rel = item.relative_to(folder).as_posix()
            out[rel] = "" if item.is_dir() else item.read_text("utf-8", "replace")
        return out

    def mod_digest(self, folder: Path) -> dict:
        return {rel: text for rel, text in self.tree(folder).items()
                if not rel.startswith(".old-") and not rel.startswith(".install-")}

    def leftovers(self) -> list:
        """custom_plugins 下的名字；正常只该有装成的 <id>，不该有 .install-*/.old-*。"""
        if not self.custom.is_dir():
            return []
        return sorted(item.name for item in self.custom.iterdir())

    def assertNoResidue(self):
        stray = [name for name in self.leftovers()
                 if name.startswith(".install-") or name.startswith(".old-")]
        self.assertEqual(stray, [], "落点里留下了暂存/备份目录：%r" % (stray,))

    def install(self, path, **kwargs):
        kwargs.setdefault("data_dir", str(self.data_dir))
        return pack_import.install(path, confirmed=True, **kwargs)


class ModDetectTest(ModCase):
    def test_dir_mod_is_mod_py(self):
        report = pack_import.detect(self.build_dir("demo_mod"))
        self.assertEqual(report["kind"], "mod-py")
        self.assertEqual(report["dialect"], "mod-json")
        self.assertEqual(report["manifest_name"], "plugin.json")
        self.assertAlmostEqual(report["confidence"], 0.96)

    def test_camel_api_version_does_not_claim_mod(self):
        folder = self.build_dir("legacy_js", api_version=None, manifest={
            "apiVersion": "1.0", "entry": "index.js"}, entry_name="index.js",
            entry_code="export function register() {}\n")
        report = pack_import.detect(folder)
        self.assertEqual(report["kind"], "extension-js")
        self.assertNotIn("mod-py", str(report))

    def test_plugin_py_without_api_version_stays_plugin_py(self):
        folder = self.build_dir("plain_py", api_version=None, manifest={
            "format": "whale-plugin", "name": "p", "version": "1",
            "entry": "demo.py"}, entry_name="demo.py",
            entry_code="from src.plugins import PluginBase\n")
        self.assertEqual(pack_import.detect(folder)["kind"], "plugin-py")

    def test_kinds_tuple_has_mod_py(self):
        self.assertIn("mod-py", pack_import.KINDS)
        self.assertEqual(pack_import.KINDS[-1], "unknown")


class ModInspectTest(ModCase):
    def test_inspect_dir_mod_promises_dir_target(self):
        folder = self.build_dir("demo_mod", files={"lib/helper.py": "VALUE = 1\n"})
        report = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertEqual(set(report), SCHEMA_KEYS)
        self.assertEqual(report["kind"], "mod-py")
        self.assertEqual(report["id"], "demo_mod")
        self.assertEqual(report["entry"], "main.py")
        self.assertEqual(report["target_dir"], str(self.custom / "demo_mod"))
        self.assertEqual(report["problems"], [])
        self.assertTrue(report["ok"])
        self.assertEqual(report["files"], 3)
        self.assertEqual(report["load"]["code"], "not-loaded")
        self.assertEqual(report["tools"], [])

    def test_inspect_does_not_write_into_target(self):
        folder = self.build_dir("demo_mod")
        pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertFalse(self.custom.exists())

    def test_inspect_without_dirs_reports_no_target(self):
        folder = self.build_dir("demo_mod")
        report = pack_import.inspect(str(folder))
        self.assertEqual(codes(report), ["no-target"])
        self.assertEqual(report["target_dir"], "")
        self.assertFalse(report["ok"])

    def test_inspect_uses_explicit_custom_plugins_dir(self):
        folder = self.build_dir("demo_mod")
        other = self.root / "elsewhere"
        report = pack_import.inspect(str(folder), custom_plugins_dir=str(other),
                                     data_dir=str(self.data_dir))
        self.assertEqual(report["target_dir"], str(other / "demo_mod"))
        self.assertEqual(report["problems"], [])

    def test_zip_and_dir_inspect_agree(self):
        folder = self.build_dir("demo_mod", files={"lib/helper.py": "VALUE = 1\n"})
        as_dir = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        as_zip = pack_import.inspect(str(self.build_zip(folder)),
                                     data_dir=str(self.data_dir))
        for key in ("kind", "id", "entry", "target_dir", "files", "bytes"):
            self.assertEqual(as_dir[key], as_zip[key], key)
        self.assertEqual(as_zip["problems"], [])

    def test_inspect_is_deterministic(self):
        folder = self.build_dir("demo_mod")
        one = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        two = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        for key in ("kind", "id", "entry", "target_dir", "files", "bytes"):
            self.assertEqual(one[key], two[key], key)
        self.assertEqual(one["problems"], two["problems"])

    def test_id_that_differs_from_folder_name_is_refused(self):
        folder = self.build_dir("demo_mod")
        (folder / "plugin.json").write_text(json.dumps(
            {"id": "other_id", "api_version": 1, "entry": "main.py"}),
            encoding="utf-8")
        report = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertEqual(codes(report), ["id_mismatch"])
        self.assertIn("other_id", messages(report))
        self.assertFalse((self.custom / "other_id").exists())

    def test_id_that_is_not_directory_safe_is_refused(self):
        folder = self.build_dir("demo_mod")
        (folder / "plugin.json").write_text(json.dumps(
            {"id": "我的 mod", "api_version": 1, "entry": "main.py"},
            ensure_ascii=False), encoding="utf-8")
        report = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertEqual(codes(report), ["bad-mod-id"])
        self.assertEqual(report["target_dir"], "")
        self.assertFalse(report["ok"])

    def test_forbidden_import_in_entry_is_refused(self):
        folder = self.build_dir(
            "demo_mod", entry_code="import subprocess\n\nVALUE = 1\n")
        report = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertEqual(codes(report), ["security-forbidden"])
        self.assertIn("subprocess", messages(report))
        self.assertIn("main.py", messages(report))
        self.assertFalse((self.custom / "demo_mod").exists())

    def test_forbidden_import_in_extra_module_is_refused(self):
        folder = self.build_dir("demo_mod", files={
            "lib/evil.py": "import ctypes\nVALUE = 1\n"})
        report = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertEqual(codes(report), ["security-forbidden"])
        self.assertIn("lib/evil.py", messages(report))

    def test_plugin_base_is_not_required(self):
        folder = self.build_dir("demo_mod", entry_code="VALUE = 1\n")
        report = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertEqual(report["problems"], [])

    def test_existing_target_only_warns(self):
        folder = self.build_dir("demo_mod")
        (self.custom / "demo_mod").mkdir(parents=True)
        report = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertEqual(report["problems"], [])
        self.assertEqual([one["code"] for one in report["notices"]],
                         ["overwrite-required"])

    def test_enabled_by_default_false_is_flagged(self):
        folder = self.build_dir("demo_mod", manifest={"enabled_by_default": False})
        report = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        self.assertTrue(report["installed_but_disabled"])
        self.assertIn("disabled-by-default",
                      [one["code"] for one in report["notices"]])


class ModManifestCodeTest(ModCase):
    """manager 的 13 个拒载码原样进 problems（不是另写一份清单校验）。"""

    CASES = (
        ("missing_id", {"id": None}, None, "missing_id"),
        ("missing_id_blank", {"id": "  "}, None, "missing_id"),
        ("bad_api_version", {"api_version": "1"}, None, "bad_api_version"),
        ("api_version_unsupported", {"api_version": 99}, None,
         "api_version_unsupported"),
        ("missing_entry", {"entry": ""}, None, "missing_entry"),
        ("entry_escape", {"entry": "../evil.py"}, None, "entry_escape"),
        ("entry_missing", {"entry": "nope.py"}, None, "entry_missing"),
        ("bad_permissions", {"permissions": "all"}, None, "bad_permissions"),
        ("bad_permissions_unknown", {"permissions": ["root"]}, None,
         "bad_permissions"),
        ("bad_field", {"name": 1}, None, "bad_field"),
        ("bad_depends", {"depends": "x"}, None, "bad_depends"),
        ("dependency_self", {"depends": ["demo_mod"]}, None, "dependency-self"),
    )

    def test_codes_pass_through_and_nothing_is_written(self):
        for label, override, raw, expected in self.CASES:
            with self.subTest(label):
                box = Path(tempfile.mkdtemp(prefix="mod-codes-"))
                self.addCleanup(shutil.rmtree, box, True)
                pkgs = box / "pkgs"
                pkgs.mkdir()
                data = box / "data"
                folder = self.build_dir("demo_mod", manifest=override,
                                        raw_manifest=raw, base=pkgs)
                report = pack_import.inspect(str(folder), data_dir=str(data))
                self.assertEqual(codes(report), [expected])
                self.assertFalse(report["ok"])
                self.assertTrue(messages(report).strip())
                self.assertFalse((data / "custom_plugins" / "demo_mod").exists())

    def test_unparsable_manifest_is_not_claimed_as_mod(self):
        """plugin.json 语法坏了就没有 api_version 可读：这一族认不出来，交给旧家族兜底。

        不在这里抢判，是为了守住语料零位移（根 plugin.json 共 566 个，但凡有写坏的，
        改判就会让它们从原家族跳到 mod-py）。兜底家族同样装不上或装成单文件，见报告。
        """
        for raw in ("{not json", "[]"):
            with self.subTest(raw):
                box = Path(tempfile.mkdtemp(prefix="mod-bad-json-"))
                self.addCleanup(shutil.rmtree, box, True)
                pkgs = box / "pkgs"
                pkgs.mkdir()
                folder = self.build_dir("demo_mod", raw_manifest=raw, base=pkgs)
                kind = pack_import.detect(folder)["kind"]
                self.assertNotEqual(kind, "mod-py")


class ModInstallTest(ModCase):
    def test_install_dir_mod_lands_directory(self):
        folder = self.build_dir("demo_mod", files={"lib/helper.py": "VALUE = 1\n"})
        report = self.install(folder)
        self.assertEqual(report["problems"], [])
        self.assertTrue(report["ok"])
        target = self.custom / "demo_mod"
        self.assertEqual(report["landed"], str(target))
        self.assertEqual(report["target_dir"], str(target))
        self.assertEqual(sorted(self.tree(target)),
                         ["lib", "lib/helper.py", "main.py", "plugin.json"])
        self.assertEqual(self.leftovers(), ["demo_mod"])
        self.assertNoResidue()
        self.assertEqual([one["code"] for one in report["notices"]],
                         ["mod-mount-pending"])
        self.assertIn("mod-py: %s" % target, report["actions"])
        self.assertEqual(report["load"]["code"], "not-loaded")

    def test_install_does_not_import_mod_code(self):
        folder = self.build_dir("demo_mod")
        before = set(sys.modules)
        report = self.install(folder)
        self.assertEqual(report["problems"], [])
        self.assertEqual(set(sys.modules) - before, set())

    def test_install_zip_matches_dir_install(self):
        folder = self.build_dir("demo_mod", files={"lib/helper.py": "VALUE = 1\n"})
        as_dir = self.install(folder)
        dir_tree = self.mod_digest(self.custom / "demo_mod")
        shutil.rmtree(self.custom)
        as_zip = self.install(self.build_zip(folder))
        self.assertEqual(as_zip["problems"], [])
        self.assertEqual(as_zip["landed"], as_dir["landed"])
        self.assertEqual(as_zip["id"], as_dir["id"])
        self.assertEqual(as_zip["entry"], as_dir["entry"])
        self.assertEqual(self.mod_digest(self.custom / "demo_mod"), dir_tree)
        self.assertNoResidue()

    def test_install_is_mounted_by_a_real_manager(self):
        folder = self.build_dir("demo_mod")
        self.install(folder)
        manager = PluginManager(self.data_dir)
        self.assertEqual(manager.take_load_problems(), [])
        self.assertIn("demo_mod", manager._plugins)
        self.assertEqual(manager._mod_plugins.get("demo_mod"), "demo_mod")

    def test_install_requires_confirmed(self):
        folder = self.build_dir("demo_mod")
        with self.assertRaises(pack_import.PackImportError):
            pack_import.install(str(folder), data_dir=str(self.data_dir))
        self.assertFalse(self.custom.exists())

    def test_second_install_without_overwrite_is_refused(self):
        folder = self.build_dir("demo_mod")
        first = self.install(folder)
        self.assertEqual(first["problems"], [])
        again = self.install(folder)
        self.assertEqual(codes(again), ["exists"])
        self.assertIn(str(self.custom / "demo_mod"), messages(again))
        self.assertNoResidue()

    def test_overwrite_replaces_old_version(self):
        folder = self.build_dir("demo_mod")
        first = self.install(folder)
        target = Path(first["landed"])
        (target / "stale.txt").write_text("old\n", encoding="utf-8")
        newer = self.build_dir("demo_mod", files={"lib/helper.py": "VALUE = 2\n"})
        report = self.install(newer, overwrite=True)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["landed"], str(target))
        self.assertNotIn("stale.txt", self.tree(target))
        self.assertIn("lib/helper.py", self.tree(target))
        self.assertNoResidue()

    def test_overwrite_failure_restores_old(self):
        folder = self.build_dir("demo_mod")
        first = self.install(folder)
        target = Path(first["landed"])
        (target / "marker.txt").write_text("old\n", encoding="utf-8")
        before = self.mod_digest(target)
        listing_before = self.leftovers()
        newer = self.build_dir("demo_mod", files={"lib/helper.py": "VALUE = 2\n"})

        real_replace = os.replace
        hits = []

        def flaky(src, dst):
            if str(dst) == str(target) and not hits:
                hits.append(str(src))
                raise OSError(13, "模拟目标目录被占用")
            return real_replace(src, dst)

        with mock.patch("os.replace", flaky):
            report = self.install(newer, overwrite=True)
        self.assertEqual(hits and True, True)
        self.assertEqual(codes(report), ["install-failed"])
        self.assertIn("目录落盘失败", messages(report))
        self.assertIn("旧版本已挪回", str(report["problems"][0].get("hint") or ""))
        self.assertEqual(self.mod_digest(target), before)
        self.assertEqual(self.leftovers(), listing_before)
        self.assertNoResidue()

    def test_overwrite_with_forbidden_payload_keeps_old(self):
        folder = self.build_dir("demo_mod")
        first = self.install(folder)
        target = Path(first["landed"])
        before = self.mod_digest(target)
        evil = self.build_dir("demo_mod", entry_code="import subprocess\nVALUE = 1\n")
        report = self.install(evil, overwrite=True)
        self.assertEqual(codes(report), ["security-forbidden"])
        self.assertEqual(self.mod_digest(target), before)
        self.assertEqual(self.leftovers(), ["demo_mod"])

    def test_rejected_install_leaves_no_target(self):
        folder = self.build_dir("demo_mod", entry_code="import subprocess\nVALUE = 1\n")
        report = self.install(folder)
        self.assertEqual(codes(report), ["security-forbidden"])
        self.assertFalse((self.custom / "demo_mod").exists())
        self.assertNoResidue()

    def test_install_agrees_with_inspect(self):
        folder = self.build_dir("demo_mod", files={"lib/helper.py": "VALUE = 1\n"})
        seen = pack_import.inspect(str(folder), data_dir=str(self.data_dir))
        done = self.install(folder)
        for key in ("kind", "id", "entry", "target_dir", "files", "bytes"):
            self.assertEqual(done[key], seen[key], key)
        self.assertEqual(done["landed"], seen["target_dir"])


class ImportApiWiringTest(ModCase):
    """两条真端点：custom_plugins_dir 反射 manager 的值，请求体可覆盖，缺了就退 data_dir。"""

    def make_client(self, plugins=None):
        app = FastAPI()
        bot = SimpleNamespace(root=str(self.data_dir))
        if plugins is not None:
            bot.plugins = plugins
        app.state.bot = bot
        import_api.register(app)
        return TestClient(app)

    def reflect(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(custom_plugins_dir=str(folder))

    def test_inspect_uses_manager_custom_plugins_dir(self):
        folder = self.build_dir("demo_mod")
        elsewhere = self.root / "elsewhere"
        seen = self.make_client(self.reflect(elsewhere)).post(
            "/api/import/inspect", json={"path": str(folder)}).json()
        self.assertEqual(seen["kind"], "mod-py")
        self.assertEqual(seen["target_dir"], str(elsewhere / "demo_mod"))
        self.assertFalse((elsewhere / "demo_mod").exists())   # 预检不写盘
        self.assertFalse(self.custom.exists())

    def test_install_lands_under_manager_custom_plugins_dir(self):
        folder = self.build_dir("demo_mod", files={"lib/helper.py": "VALUE = 1\n"})
        elsewhere = self.root / "elsewhere"
        done = self.make_client(self.reflect(elsewhere)).post(
            "/api/import/install",
            json={"confirmed": True, "path": str(folder)}).json()
        self.assertTrue(done["ok"], done["problems"])
        target = elsewhere / "demo_mod"
        self.assertEqual(done["landed"], str(target))
        files = sorted(rel for rel in self.mod_digest(target)
                       if (target / rel).is_file())
        self.assertEqual(files, ["lib/helper.py", "main.py", "plugin.json"])
        self.assertEqual(sorted(item.name for item in elsewhere.iterdir()), ["demo_mod"])

    def test_body_field_overrides_custom_plugins_dir(self):
        folder = self.build_dir("demo_mod")
        client = self.make_client(self.reflect(self.root / "elsewhere"))
        alt = self.root / "alt-root"
        seen = client.post("/api/import/inspect",
                           json={"path": str(folder), "custom_plugins_dir": str(alt)}).json()
        self.assertEqual(seen["target_dir"], str(alt / "demo_mod"))
        done = client.post("/api/import/install",
                           json={"confirmed": True, "path": str(folder),
                                 "custom_plugins_dir": str(alt)}).json()
        self.assertTrue(done["ok"], done["problems"])
        self.assertEqual(done["landed"], str(alt / "demo_mod"))
        self.assertFalse((self.root / "elsewhere" / "demo_mod").exists())

    def test_falls_back_to_data_dir_without_manager_dir(self):
        folder = self.build_dir("demo_mod")
        seen = self.make_client().post(
            "/api/import/inspect", json={"path": str(folder)}).json()
        self.assertEqual(seen["target_dir"], str(self.custom / "demo_mod"))


if __name__ == "__main__":
    unittest.main()
