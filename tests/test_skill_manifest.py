"""src/skill_manifest.py 单测：分层口径、清单构建、原子写、ensure_manifest 降级、CLI。

夹具全部在 tempfile 里生成：不读真实 Skill 市场、不写真实 <data> 目录，不联网、不起 node。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import skill_manifest, skill_tools

TIERS = (
    "T1-ready",
    "T2-web-fetch",
    "T3-extra-api",
    "T4-host-source",
    "T5-depends-other",
    "T6-disabled",
)
REQUIRED_SKILL_KEYS = {
    "id",
    "name",
    "version",
    "tier",
    "tierReason",
    "tools",
    "usage",
    "requires",
    "hostImports",
    "source",
}
# 入口写一行 registerTool，保证不会被判成 T6 的 no-tools。
ENTRY_OK = "registerTool({name:'ping', description:'p', parameters:{type:'object'}});\n"


def declaration(skill_id, **extra):
    data = {"id": skill_id, "name": skill_id.upper(), "version": "1.0.0"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


def write_tree(root, files):
    root = Path(root)
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def make_zip(path, files, prefix=""):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(prefix + name, text)
    return path


def classify_path(path):
    path = Path(path)
    reader = skill_tools._DirReader(path) if path.is_dir() else skill_tools._ZipReader(path)
    try:
        return skill_manifest.classify(reader)
    finally:
        skill_manifest._close(reader)


def package_files(skill_id, entry=ENTRY_OK, **manifest_extra):
    return {"skill.json": declaration(skill_id, **manifest_extra), "index.js": entry}


class ManifestTestCase(unittest.TestCase):
    """每个用例一个临时目录，所有夹具都在里面。"""

    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="skill-manifest-test-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.packages = self.tmp / "pkgs"

    def package(self, name, files):
        return write_tree(self.packages / name, files)

    def market(self, packages):
        """packages: {zip 文件名（不含 .zip）: 包内文件}。"""
        market = self.tmp / "market"
        market.mkdir(exist_ok=True)
        for stem, files in packages.items():
            make_zip(market / (stem + ".zip"), files)
        return market

    def empty_dir(self, name):
        path = self.tmp / name
        path.mkdir(exist_ok=True)
        return path


class ClassifyTierTests(ManifestTestCase):
    def test_t1_ready_clean(self):
        info = classify_path(self.package("clean", package_files("clean")))
        self.assertEqual(info["tier"], "T1-ready")
        self.assertEqual(info["tierReason"], "clean")
        self.assertEqual(info["hostImports"], [])
        self.assertEqual(info["external"], [])

    def test_t2_web_fetch_permission(self):
        files = package_files("fetcher", permissions=["web_fetch"])
        info = classify_path(self.package("fetcher", files))
        self.assertEqual(info["tier"], "T2-web-fetch")
        self.assertIn("web_fetch", info["tierReason"])

    def test_t2_external_host_from_code(self):
        entry = ENTRY_OK + "const endpoint = 'https://api.example.com/v1/items';\n"
        info = classify_path(self.package("remote", package_files("remote", entry=entry)))
        self.assertEqual(info["tier"], "T2-web-fetch")
        self.assertEqual(info["external"], ["api.example.com"])
        self.assertIn("api.example.com", info["tierReason"])

    def test_t3_extra_api_permission(self):
        for permission in ("onebot", "qq_sticker"):
            with self.subTest(permission=permission):
                info = classify_path(
                    self.package(permission, package_files(permission, permissions=[permission]))
                )
                self.assertEqual(info["tier"], "T3-extra-api")
                self.assertIn(permission, info["tierReason"])

    def test_t4_host_source_import(self):
        entry = "import {cfg} from '../../src/config.js';\n" + ENTRY_OK
        info = classify_path(self.package("hosty", package_files("hosty", entry=entry)))
        self.assertEqual(info["tier"], "T4-host-source")
        self.assertEqual(info["hostImports"], ["../../src/config.js"])

    def test_t5_depends_other_requires(self):
        files = package_files("needy", requires=["currency.v1"])
        info = classify_path(self.package("needy", files))
        self.assertEqual(info["tier"], "T5-depends-other")
        self.assertEqual(info["requires"], ["currency.v1"])
        self.assertTrue(info["tierReason"].startswith("requires:"))

    def test_t6_no_manifest(self):
        info = classify_path(self.package("nomani", {"index.js": ENTRY_OK}))
        self.assertEqual(info["tier"], "T6-disabled")
        self.assertEqual(info["tierReason"], "no-manifest")

    def test_t6_no_entry(self):
        files = {"skill.json": declaration("noentry"), "util.js": ENTRY_OK}
        info = classify_path(self.package("noentry", files))
        self.assertEqual(info["tier"], "T6-disabled")
        self.assertEqual(info["tierReason"], "no-entry")

    def test_t6_no_tools(self):
        entry = "export const onlyData = 1;\n"
        info = classify_path(self.package("notools", package_files("notools", entry=entry)))
        self.assertEqual(info["tier"], "T6-disabled")
        self.assertEqual(info["tierReason"], "no-tools")

    def test_t6_escape_run_root(self):
        entry = "import helper from '../../../vendor/helper.js';\n" + ENTRY_OK
        info = classify_path(self.package("escaper", package_files("escaper", entry=entry)))
        self.assertEqual(info["tier"], "T6-disabled")
        self.assertTrue(info["tierReason"].startswith("escape-run-root:"))

    def test_t6_load_failed_missing_relative_import(self):
        # 包根在子目录里，入口又 import 到包外不存在的同级模块 → 加载期必失败。
        files = {
            "pkg/skill.json": declaration("nested"),
            "pkg/index.js": "import sibling from '../sibling/util.js';\n" + ENTRY_OK,
        }
        info = classify_path(self.package("nested", files))
        self.assertEqual(info["tier"], "T6-disabled")
        self.assertTrue(info["tierReason"].startswith("load-failed:"))

    def test_empty_requires_container_is_not_t5(self):
        # 真实市场里 记忆宫殿-palace 声明 "requires": {}（空对象 = 没有依赖），
        # 之前会被 str({}) 变成真值 '{}' 而误判 T5。
        for raw in ({}, [], None, ""):
            with self.subTest(requires=raw):
                files = package_files("emptyreq", requires=raw)
                info = classify_path(self.package("emptyreq", files))
                self.assertEqual(info["requires"], [])
                self.assertEqual(info["tier"], "T1-ready")

    def test_requires_declared_as_object_uses_keys(self):
        files = package_files("objreq", requires={"currency.v1": ">=1"})
        info = classify_path(self.package("objreq", files))
        self.assertEqual(info["requires"], ["currency.v1"])
        self.assertEqual(info["tier"], "T5-depends-other")

    def test_host_import_beats_requires(self):
        files = package_files(
            "both",
            entry="import {x} from '../../src/store.js';\n" + ENTRY_OK,
            requires=["rapport.v1"],
        )
        info = classify_path(self.package("both", files))
        self.assertEqual(info["tier"], "T4-host-source")

    def test_escape_from_non_entry_file_is_not_fatal(self):
        # 只有入口自己逃出运行目录才是加载失败；子文件往上 import 仍算依赖宿主源码。
        files = {
            "skill.json": declaration("sub"),
            "index.js": "import './lib/remote.js';\n" + ENTRY_OK,
            "lib/remote.js": "import deep from '../../../../outside/deep.js';\n",
        }
        info = classify_path(self.package("sub", files))
        self.assertEqual(info["tier"], "T4-host-source")
        self.assertIn("../../../../outside/deep.js", info["hostImports"])

    def test_register_tool_inside_comment_is_ignored(self):
        entry = "// registerTool({name:'ghost'});\n/* registerTool({name:'ghost2'}); */\nexport const x = 1;\n"
        info = classify_path(self.package("commented", package_files("commented", entry=entry)))
        self.assertEqual(info["tier"], "T6-disabled")
        self.assertEqual(info["tierReason"], "no-tools")


class ClassifyExtractionTests(ManifestTestCase):
    def test_payload_keys(self):
        info = classify_path(self.package("shape", package_files("shape")))
        self.assertTrue({"tier", "tierReason", "tools", "usage", "requires", "hostImports"} <= set(info))
        self.assertIn(info["tier"], TIERS)
        self.assertIsInstance(info["tools"], list)
        self.assertIsInstance(info["usage"], list)
        self.assertIsInstance(info["requires"], list)
        self.assertIsInstance(info["hostImports"], list)

    def test_usage_from_ctx_members(self):
        entry = ENTRY_OK + "ctx.send_group(1);\napi.ctx.sticker(2);\n"
        info = classify_path(self.package("usage", package_files("usage", entry=entry)))
        self.assertEqual(info["usage"], ["send_group", "sticker"])

    def test_requires_and_host_imports_lists(self):
        files = package_files(
            "lists",
            entry="import {a} from '../../src/a.js';\nimport b from '../../src/b.js';\n" + ENTRY_OK,
            requires=["android.play.request"],
        )
        info = classify_path(self.package("lists", files))
        self.assertEqual(info["requires"], ["android.play.request"])
        self.assertEqual(sorted(info["hostImports"]), ["../../src/a.js", "../../src/b.js"])

    def test_zip_reader_matches_dir_reader(self):
        files = package_files(
            "same",
            entry="import {cfg} from '../../src/config.js';\n" + ENTRY_OK + "ctx.reply('x');\n",
            permissions=["web_fetch"],
            requires=["currency.v1"],
        )
        from_dir = classify_path(self.package("same", files))
        from_zip = classify_path(make_zip(self.tmp / "same.zip", files))
        for key in ("tier", "tierReason", "usage", "requires", "hostImports", "entry", "external"):
            self.assertEqual(from_dir[key], from_zip[key], key)

    def test_import_inside_comment_still_marks_host_source(self):
        # from / require 是对**原文**做的静态扫描（不剥注释），这是既有清单的口径；
        # 注释里的 registerTool 则会被剥掉（见 ClassifyTierTests.test_register_tool_inside_comment_is_ignored）。
        entry = "// import ghost from '../../src/ghost.js';\n" + ENTRY_OK
        info = classify_path(self.package("ghost", package_files("ghost", entry=entry)))
        self.assertEqual(info["hostImports"], ["../../src/ghost.js"])
        self.assertEqual(info["tier"], "T4-host-source")

    def test_classify_bad_zip_returns_t6(self):
        broken = self.tmp / "broken.zip"
        broken.write_bytes(b"this is not a zip archive")
        info = classify_path(broken)
        self.assertEqual(info["tier"], "T6-disabled")
        self.assertEqual(info["tierReason"], "no-manifest")


class BuildManifestTests(ManifestTestCase):
    def test_shape_and_stats(self):
        market = self.market(
            {
                "alpha": package_files("alpha"),
                "beta": package_files("beta", permissions=["web_fetch"]),
            }
        )
        manifest = skill_manifest.build_manifest(
            market_dir=market, extensions_root=self.empty_dir("ext")
        )
        self.assertEqual(set(manifest), {"meta", "stats", "skills"})
        self.assertIsInstance(manifest["meta"]["writtenAt"], int)
        self.assertGreaterEqual(manifest["meta"]["elapsedMs"], 0)
        self.assertEqual(manifest["meta"]["marketDir"], str(market))
        stats = manifest["stats"]
        self.assertEqual(stats["skillsTotal"], 2)
        self.assertEqual(stats["zipsInMarketDir"], 2)
        self.assertEqual(set(stats["tierCounts"]), set(TIERS))
        self.assertEqual(stats["tierCounts"]["T1-ready"], 1)
        self.assertEqual(stats["tierCounts"]["T2-web-fetch"], 1)
        by_id = {skill["id"]: skill for skill in manifest["skills"]}
        self.assertEqual(set(by_id), {"alpha", "beta"})
        for skill in manifest["skills"]:
            self.assertTrue(REQUIRED_SKILL_KEYS <= set(skill), sorted(skill))
            self.assertEqual(skill["source"]["kind"], "market")
            self.assertTrue(skill["source"]["path"].endswith(".zip"))
            self.assertEqual(skill["name"], skill["id"].upper())
            self.assertEqual(skill["version"], "1.0.0")

    def test_installed_dir_overrides_same_id_zip(self):
        market = self.market({"alpha": package_files("alpha")})
        installed = self.empty_dir("ext")
        write_tree(installed / "alpha", {"skill.json": declaration("alpha"), "index.js": "export const x = 1;\n"})
        manifest = skill_manifest.build_manifest(market_dir=market, extensions_root=installed)
        self.assertEqual(len(manifest["skills"]), 1)
        skill = manifest["skills"][0]
        self.assertEqual(skill["id"], "alpha")
        self.assertEqual(skill["source"]["kind"], "installed")
        self.assertEqual(skill["tier"], "T6-disabled")  # 被已安装版本覆盖：它没有 registerTool
        self.assertEqual(manifest["meta"]["overrides"], ["alpha"])

    def test_broken_zip_is_skipped_without_raising(self):
        market = self.market({"omega": package_files("omega")})
        (market / "broken.zip").write_bytes(b"PK\x03\x04 truncated garbage")
        manifest = skill_manifest.build_manifest(
            market_dir=market, extensions_root=self.empty_dir("ext")
        )
        self.assertEqual([skill["id"] for skill in manifest["skills"]], ["omega"])
        self.assertEqual(manifest["stats"]["zipsInMarketDir"], 1)

    def test_nested_zip_manifest_tools_are_extracted(self):
        # 包根多一层的 zip：清单里声明的 tools 也必须被采到（tool_schema 只读根目录清单）。
        files = {
            "nested/skill.json": json.dumps(
                {
                    "id": "nested",
                    "name": "NESTED",
                    "version": "1.0.0",
                    "tools": [{"name": "declared_tool", "description": "d"}],
                },
                ensure_ascii=False,
            ),
            "nested/index.js": ENTRY_OK,
        }
        market = self.market({"nested": files})
        manifest = skill_manifest.build_manifest(
            market_dir=market, extensions_root=self.empty_dir("ext")
        )
        skill = manifest["skills"][0]
        names = [tool["name"] for tool in skill["tools"]]
        self.assertTrue(any(name.endswith("declared_tool") for name in names), names)
        self.assertTrue(any(name.endswith("ping") for name in names), names)

    def test_empty_dirs_give_empty_manifest(self):
        manifest = skill_manifest.build_manifest(
            market_dir=self.empty_dir("nomanifest"), extensions_root=self.empty_dir("ext")
        )
        self.assertEqual(manifest["skills"], [])
        self.assertEqual(manifest["stats"]["skillsTotal"], 0)


class WriteAndStampTests(ManifestTestCase):
    def setUp(self):
        super().setUp()
        self.market = self.market(
            {
                "alpha": package_files("alpha"),
                "beta": package_files("beta", requires=["currency.v1"]),
            }
        )
        self.ext = self.empty_dir("ext")

    def test_write_manifest_atomic_and_idempotent(self):
        target = self.tmp / "out" / "skill-tools.json"
        first = skill_manifest.write_manifest(
            target, market_dir=self.market, extensions_root=self.ext
        )
        self.assertTrue(first["ok"])
        self.assertEqual(first["skills"], 2)
        self.assertGreaterEqual(first["elapsedMs"], 0)
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(first["counts"], on_disk["stats"]["tierCounts"])
        self.assertEqual(first["stamp"], skill_manifest.stamp(target))
        second = skill_manifest.write_manifest(
            target, market_dir=self.market, extensions_root=self.ext
        )
        self.assertEqual(first["stamp"], second["stamp"])  # 内容指纹幂等
        self.assertEqual(
            sorted(item.name for item in target.parent.iterdir()), ["skill-tools.json"]
        )  # 原子写不留 .tmp

    def test_write_manifest_creates_parent_directory(self):
        target = self.tmp / "deep" / "deeper" / "skill-tools.json"
        self.assertFalse(target.parent.exists())
        result = skill_manifest.write_manifest(
            target, market_dir=self.market, extensions_root=self.ext
        )
        self.assertTrue(result["ok"])
        self.assertTrue(target.is_file())

    def test_stamp_is_stable_and_ignores_volatile_meta(self):
        target = self.tmp / "stamped.json"
        payload = {
            "meta": {"version": 1, "writtenAt": 100, "elapsedMs": 7},
            "stats": {"tierCounts": {"T1-ready": 1}},
            "skills": [{"id": "alpha"}],
        }
        target.write_text(json.dumps(payload), encoding="utf-8")
        first = skill_manifest.stamp(target)
        payload["meta"]["writtenAt"] = 999
        payload["meta"]["elapsedMs"] = 4321
        target.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(first, skill_manifest.stamp(target))
        payload["skills"][0]["id"] = "beta"
        target.write_text(json.dumps(payload), encoding="utf-8")
        self.assertNotEqual(first, skill_manifest.stamp(target))

    def test_stamp_missing_or_invalid_returns_none(self):
        self.assertIsNone(skill_manifest.stamp(self.tmp / "absent.json"))
        broken = self.tmp / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(skill_manifest.stamp(broken))


class EnsureManifestTests(ManifestTestCase):
    def setUp(self):
        super().setUp()
        self.market = self.market({"alpha": package_files("alpha")})
        self.ext = self.empty_dir("ext")
        self.runtime = types.SimpleNamespace(data_root=self.tmp / "runtime")
        self.target = Path(skill_tools.data_dir(self.runtime)) / skill_manifest.MANIFEST_FILE

    def ensure(self, **kwargs):
        kwargs.setdefault("market_dir", self.market)
        kwargs.setdefault("extensions_root", self.ext)
        return skill_manifest.ensure_manifest(runtime=self.runtime, **kwargs)

    def test_ensure_generates_when_manifest_missing(self):
        self.assertFalse(self.target.exists())
        result = self.ensure()
        self.assertTrue(result["ok"])
        self.assertTrue(result["rebuilt"])
        self.assertFalse(result["reused"])
        self.assertEqual(result["skills"], 1)
        self.assertEqual(result["path"], str(self.target))
        on_disk = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual([skill["id"] for skill in on_disk["skills"]], ["alpha"])

    def test_ensure_reuses_fresh_manifest(self):
        first = self.ensure()
        before = self.target.read_text(encoding="utf-8")
        second = self.ensure()
        self.assertTrue(second["ok"])
        self.assertTrue(second["reused"])
        self.assertFalse(second["rebuilt"])
        self.assertEqual(second["skills"], 1)
        self.assertGreaterEqual(second["ageSeconds"], 0)
        self.assertEqual(second["stamp"], first["stamp"])
        self.assertEqual(before, self.target.read_text(encoding="utf-8"))  # 未重写

    def test_ensure_force_rebuilds(self):
        self.ensure()
        before = self.target.read_text(encoding="utf-8")
        forced = self.ensure(force=True)
        self.assertTrue(forced["ok"])
        self.assertTrue(forced["rebuilt"])
        self.assertFalse(forced["reused"])
        self.assertEqual(forced["skills"], 1)
        self.assertTrue(before.strip())  # 旧清单本来就在
        self.assertTrue(json.loads(self.target.read_text(encoding="utf-8"))["skills"])

    def test_ensure_recovers_from_corrupt_manifest(self):
        self.ensure()
        self.target.write_text("{ this is not json", encoding="utf-8")
        result = self.ensure()
        self.assertTrue(result["ok"])
        self.assertTrue(result["rebuilt"])
        on_disk = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual([skill["id"] for skill in on_disk["skills"]], ["alpha"])

    def test_ensure_degrades_to_ok_false_on_error(self):
        self.ensure()
        before = self.target.read_bytes()
        with mock.patch.object(skill_manifest, "build_manifest", side_effect=RuntimeError("boom")):
            result = self.ensure(force=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "RuntimeError")
        self.assertIn("boom", result["error"]["message"])
        self.assertTrue(result["keptOld"])
        self.assertEqual(before, self.target.read_bytes())  # 绝不删旧清单

    def test_ensure_keeps_old_manifest_when_scan_empty(self):
        self.ensure()
        before = self.target.read_bytes()
        result = self.ensure(
            force=True,
            market_dir=self.empty_dir("empty-market"),
            extensions_root=self.empty_dir("empty-ext"),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "EmptyManifest")
        self.assertTrue(result["keptOld"])
        self.assertEqual(before, self.target.read_bytes())


class CliAndPurityTests(ManifestTestCase):
    def test_cli_writes_manifest_for_market_dir(self):
        market = self.market({"alpha": package_files("alpha")})
        target = self.tmp / "cli" / "skill-tools.json"
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = skill_manifest.main(
                ["--market", str(market), "--out", str(target), "--extensions", str(self.empty_dir("ext"))]
            )
        self.assertEqual(code, 0)
        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["skills"], 1)
        self.assertTrue(target.is_file())

    def test_cli_rejects_unknown_flag(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream), contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit):
                skill_manifest.main(["--nope"])

    def test_module_is_pure_stdlib(self):
        source = Path(skill_manifest.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported.add(node.module.split(".")[0])
        self.assertTrue(imported)
        third_party = sorted(imported - set(sys.stdlib_module_names) - {"src"})
        self.assertEqual(third_party, [], "只允许纯标准库（外加包内 src）")
        forbidden = imported & {"subprocess", "socket", "urllib", "http", "ssl", "asyncio"}
        self.assertEqual(sorted(forbidden), [], "不联网、不起子进程")


if __name__ == "__main__":
    unittest.main()
