"""导入边界回归：家族误判 / 落盘坏掉 / 编码坏掉（导入报错修复卡）。

每条用例都对应一处真实误判，断言直接盯用户看得见的字段（kind / problems[].code /
notices[].code / landed），不读私有中间量。夹具全部在 tempfile 里现造，不读插件市场、
不联网、不碰仓库 data/。

修复前 → 修复后的对照（都在本文件里被钉住）：
1. plugin.json 写 apiVersion + entry=main.py：以前判 extension-js 0.97 并当 JS 扩展落盘，
   装完是跑不起来的目录；现在按 .py 入口判 plugin-py。
2. skill.json 坏 JSON 但同目录有合法 plugin.json：以前咬定 skill 且预检 ok=True，真装才
   失败；现在与 plugin.json 一样出 manifest-broken，并且点名是 skill.json。
3. 清单名大小写变体 Plugin.JSON：以前当"无清单"按单文件猜，id/name/version 全丢；
   现在按清单识别，并多一条 manifest-name-case 告知。
4. 一个 zip 两个顶层包：以前 unknown + "没有 pack.json / skill.json / plugin.json"（假话）；
   现在 multiple-packages 说清有几个包、分别是哪几个目录。
5. 入口 import 了包内兄弟模块：以前只装一个 .py，兄弟模块被丢掉 → 加载即 ImportError；
   现在 py-multi-file 预检拦下并给两条出路（合并入口 / 改目录 mod）。
6. 包里带 README 等副文件：以前无声丢文件；现在 extra-files-not-installed 明说不会装进去。
7. GBK 源码：以前 plugin_installer._decode 直接 InstallError('文件不是 UTF-8 文本')；
   现在按 GB18030 解出来（并要求解开仍是合法 Python），中文原样落进 <id>.py。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src import pack_import, plugin_installer, skill_tools  # noqa: E402

PY_ENTRY = '''from src.plugins import PluginBase, PluginMeta


class EdgePlugin(PluginBase):
    META = PluginMeta(id="edge_plugin", name="边界插件", description="用例")


def handle(message):
    return message
'''


def codes(result) -> list:
    return [str(one.get("code") or "") for one in result.get("problems") or []]


def notice_codes(result) -> list:
    return [str(one.get("code") or "") for one in result.get("notices") or []]


def problem_text(result) -> str:
    return " ".join(str(one.get("message") or "") for one in result.get("problems") or [])


def notice_text(result) -> str:
    return " ".join(str(one.get("message") or "") for one in result.get("notices") or [])


class PackCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_zip(self, name: str, members: dict) -> Path:
        target = self.root / name
        with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as archive:
            for member, data in members.items():
                archive.writestr(member, data.encode("utf-8") if isinstance(data, str) else data)
        return target

    def manifest(self, **fields) -> str:
        return json.dumps(fields, ensure_ascii=False)


class ApiVersionWithPyEntry(PackCase):
    def test_py_entry_wins_over_api_version(self):
        """plugin.json 同时写了 apiVersion 与 entry=main.py：入口语言说了算。"""
        pack = self.make_zip("py-entry.zip", {
            "pkg/plugin.json": self.manifest(apiVersion=1, id="edge_py", name="边界",
                                            version="1.0.0", entry="main.py"),
            "pkg/main.py": PY_ENTRY,
        })
        got = pack_import.inspect(pack)
        self.assertEqual(got["kind"], "plugin-py", got["problems"])
        self.assertEqual(got["entry"], "main.py")
        self.assertEqual(got["id"], "edge_py")
        self.assertEqual(codes(got), [])

    def test_install_lands_as_single_py_not_as_js_extension(self):
        pack = self.make_zip("py-entry-install.zip", {
            "pkg/plugin.json": self.manifest(apiVersion=1, id="edge_py2", name="边界",
                                            version="1.0.0", entry="main.py"),
            "pkg/main.py": PY_ENTRY,
        })
        plugins = self.root / "plugins"
        plugins.mkdir()
        got = pack_import.install(pack, confirmed=True, plugins_dir=plugins)
        self.assertTrue(got["ok"], got["problems"])
        self.assertEqual(got["kind"], "plugin-py")
        self.assertTrue(Path(str(got["landed"])).is_file(), got["landed"])
        self.assertEqual(Path(str(got["landed"])).name, "edge_py2.py")
        self.assertFalse((self.root / "custom_plugins").exists())


class BrokenSkillManifest(PackCase):
    def test_broken_skill_json_blocks_preflight(self):
        pack = self.make_zip("bad-skill.zip", {
            "pkg/skill.json": '{"id": "edge_skill", ',
            "pkg/plugin.json": self.manifest(apiVersion=1, id="edge_skill",
                                            entry="index.js"),
            "pkg/index.js": "export async function setup(api) {}\n",
        })
        got = pack_import.inspect(pack)
        self.assertEqual(codes(got), ["manifest-broken"], got["notices"])
        self.assertFalse(got["ok"])
        self.assertTrue(problem_text(got).startswith("skill.json"), problem_text(got))

    def test_broken_plugin_json_says_plugin_json(self):
        """历史文案不能漂：既有用例断言这两句原文。"""
        pack = self.make_zip("bad-plugin.zip", {
            "pkg/plugin.json": '{"id": "edge_js", ',
            "pkg/main.py": PY_ENTRY,
        })
        got = pack_import.inspect(pack)
        self.assertEqual(codes(got), ["manifest-broken"])
        self.assertIn("plugin.json 不是合法 JSON", problem_text(got))


class ManifestNameCase(PackCase):
    def test_uppercase_plugin_json_is_recognized(self):
        pack = self.make_zip("case.zip", {
            "pkg/Plugin.JSON": self.manifest(apiVersion=1, id="edge_case", entry="index.js"),
            "pkg/index.js": "export async function setup(api) {}\n",
        })
        got = pack_import.inspect(pack)
        self.assertEqual(got["kind"], "extension-js", got["problems"])
        self.assertEqual(got["id"], "edge_case")
        self.assertIn("manifest-name-case", notice_codes(got))
        self.assertIn("Plugin.JSON", notice_text(got))

    def test_canonical_names_get_no_case_notice(self):
        pack = self.make_zip("plain.zip", {
            "pkg/plugin.json": self.manifest(apiVersion=1, id="edge_plain", entry="index.js"),
            "pkg/index.js": "export async function setup(api) {}\n",
        })
        got = pack_import.inspect(pack)
        self.assertNotIn("manifest-name-case", notice_codes(got))


class MultiplePackages(PackCase):
    def test_two_top_level_packages_are_refused_with_real_reason(self):
        pack = self.make_zip("two-packs.zip", {
            "p1/plugin.json": self.manifest(apiVersion=1, id="edge_p1", entry="index.js"),
            "p1/index.js": "export async function setup(api) {}\n",
            "p2/plugin.json": self.manifest(id="edge_p2", api_version=1, entry="main.py"),
            "p2/main.py": PY_ENTRY,
        })
        got = pack_import.inspect(pack)
        self.assertEqual(got["kind"], "unknown")
        self.assertEqual(codes(got), ["multiple-packages"])
        text = problem_text(got)
        self.assertIn("2 个包", text)
        self.assertIn("p1", text)
        self.assertIn("p2", text)
        self.assertNotIn("没有 pack.json", text)


class ImportedSiblingModules(PackCase):
    def test_entry_importing_sibling_is_refused(self):
        pack = self.make_zip("multi-py.zip", {
            "pkg/plugin.json": self.manifest(id="edge_multi", name="多文件", version="1.0.0",
                                            entry="main.py"),
            "pkg/main.py": PY_ENTRY + "\nimport helper\n",
            "pkg/helper.py": "VALUE = 1\n",
        })
        got = pack_import.inspect(pack)
        self.assertEqual(codes(got), ["py-multi-file"])
        text = problem_text(got)
        self.assertIn("helper.py", text)
        self.assertIn("api_version", " ".join(str(one.get("hint") or "")
                                             for one in got["problems"]))

    def test_from_import_and_relative_import_are_caught_too(self):
        """from helper import X / from . import helper 与 import helper 同一判据。

        旧正则把 from X import Y 的 X 吞进了前置的 (.*) 组，X 不会进候选集：
        这种入口在 pack_import.inspect 里只落一条 extra-files-not-installed notice，
        预检放过去、装完才在加载时 ImportError。
        """
        for name, line in (("from-import.zip", "from helper import VALUE"),
                           ("relative-import.zip", "from . import helper")):
            with self.subTest(line=line):
                pack = self.make_zip(name, {
                    "pkg/plugin.json": self.manifest(id="edge_multi3", name="多文件",
                                                     version="1.0.0", entry="main.py"),
                    "pkg/main.py": PY_ENTRY + "\n" + line + "\n",
                    "pkg/helper.py": "VALUE = 1\n",
                })
                got = pack_import.inspect(pack)
                self.assertEqual(codes(got), ["py-multi-file"], got["notices"])
                self.assertIn("helper.py", problem_text(got))

    def test_installer_precheck_reports_the_dropped_module(self):
        pack = self.make_zip("multi-py2.zip", {
            "pkg/plugin.json": self.manifest(id="edge_multi2", name="多文件", version="1.0.0",
                                            entry="main.py"),
            "pkg/main.py": PY_ENTRY + "\nfrom helper import VALUE\n",
            "pkg/helper.py": "VALUE = 1\n",
        })
        info = plugin_installer.inspect_local(pack)
        self.assertFalse(info["ok"])
        joined = " ".join(str(one) for one in info["problems"])
        self.assertIn("helper.py", joined)

    def test_unimported_sibling_is_only_a_notice(self):
        """既有的装法不能倒退：pkg/main.py + 没被 import 的 helper.py 仍可安装。"""
        pack = self.make_zip("extra-sibling.zip", {
            "pkg/plugin.json": self.manifest(id="edge_extra", name="副文件", version="1.0.0",
                                            entry="main.py"),
            "pkg/main.py": PY_ENTRY,
            "pkg/helper.py": "VALUE = 1\n",
            "pkg/README.md": "# 说明\n",
        })
        got = pack_import.inspect(pack)
        self.assertTrue(got["ok"], got["problems"])
        self.assertEqual(codes(got), [])
        self.assertIn("extra-files-not-installed", notice_codes(got))
        self.assertIn("helper.py", notice_text(got))
        self.assertIn("README.md", notice_text(got))


class GbkSource(PackCase):
    def test_gbk_source_installs_with_chinese_intact(self):
        source = "# 中文说明：这个插件用 GBK 保存\n" + PY_ENTRY
        pack = self.make_zip("gbk.zip", {
            "pkg/plugin.json": self.manifest(id="edge_gbk", name="编码", version="1.0.0",
                                            entry="main.py"),
            "pkg/main.py": source.encode("gbk"),
        })
        got = pack_import.inspect(pack)
        self.assertTrue(got["ok"], got["problems"])
        plugins = self.root / "plugins"
        plugins.mkdir()
        landed = pack_import.install(pack, confirmed=True, plugins_dir=plugins)
        self.assertTrue(landed["ok"], landed["problems"])
        text = Path(str(landed["landed"])).read_text(encoding="utf-8")
        self.assertIn("中文说明", text)
        self.assertIn("PluginBase", text)


JS_REGISTER = "registerTool({name:'ping', description:'p', parameters:{type:'object'}});\n"


class StaticLoadStage(PackCase):
    """load 的 stage / checked / warnings：静态分层结论与运行期放行闸同一口径。

    ok 不再各自比对 "T1-ready"，而是引用 skill_tools.DEFAULT_TIERS，所以 T2-web-fetch
    也必须 ok=True —— 预检、安装、候选列表三处结论一致才叫「统一」。
    """

    def skill_zip(self, name: str, skill_id: str, **fields) -> Path:
        manifest = {"id": skill_id, "name": skill_id, "version": "1.0.0"}
        manifest.update(fields)
        return self.make_zip(name, {"pkg/skill.json": self.manifest(**manifest),
                                    "pkg/index.js": JS_REGISTER})

    def runtime(self):
        return SimpleNamespace(data_root=str(self.root / "rt"))

    def test_t2_tier_agrees_across_inspect_install_and_eligible(self):
        pack = self.skill_zip("t2.zip", "edge_t2", permissions=["web_fetch"])
        got = pack_import.inspect(pack)
        self.assertEqual(got["kind"], "skill")
        self.assertTrue(got["load"]["ok"], got["load"])
        self.assertEqual(got["load"]["code"], "T2-web-fetch")
        self.assertEqual(got["load"]["stage"], "static-registerable")
        self.assertEqual(got["load"]["checked"], "static-classify")

        runtime = self.runtime()
        landed = pack_import.install(pack, confirmed=True, skills_runtime=runtime)
        self.assertTrue(landed["ok"], landed["problems"])
        self.assertTrue(landed["load"]["ok"], landed["load"])
        self.assertEqual(landed["load"]["code"], got["load"]["code"])
        self.assertEqual(landed["load"]["stage"], got["load"]["stage"])

        market = self.root / "market"
        market.mkdir()
        manifest = {"skills": [{"id": "edge_t2", "tier": landed["load"]["code"], "name": "T2",
                                "tools": [{"name": "edge_t2__ping", "description": "p"}]}]}
        chosen = skill_tools.eligible_skills(manifest=manifest, runtime=runtime, market=market)
        self.assertEqual([one["id"] for one in chosen["eligible"]], ["edge_t2"], chosen["skipped"])

    def test_missing_ctx_is_a_warning_not_a_block(self):
        pack = self.make_zip("t2-ctx.zip", {
            "pkg/skill.json": self.manifest(id="edge_ctx", name="缺 ctx", version="1.0.0",
                                            permissions=["web_fetch"]),
            "pkg/index.js": JS_REGISTER + "ctx.sender.sendImage('x');\n",
        })
        got = pack_import.inspect(pack)
        self.assertTrue(got["load"]["ok"], got["load"])
        self.assertEqual(got["load"]["stage"], "static-registerable")
        self.assertTrue(any("sender" in one for one in got["load"]["warnings"]), got["load"])

    def test_t6_tier_blocks_with_reason(self):
        pack = self.make_zip("t6.zip", {
            "pkg/skill.json": self.manifest(id="edge_t6", name="缺入口", version="1.0.0"),
        })
        got = pack_import.inspect(pack)
        self.assertEqual(got["kind"], "skill")
        self.assertFalse(got["load"]["ok"], got["load"])
        self.assertEqual(got["load"]["code"], "T6-disabled")
        self.assertEqual(got["load"]["stage"], "static-blocked")
        self.assertEqual(got["load"]["error"], "no-entry")

    def test_t6_install_error_names_the_structural_reason(self):
        """装也装得住，但 load.error 必须是分层原因，不能拿「默认停用」顶掉。"""
        pack = self.make_zip("t6-install.zip", {
            "pkg/skill.json": self.manifest(id="edge_t6i", name="缺入口", version="1.0.0"),
        })
        landed = pack_import.install(pack, confirmed=True, skills_runtime=self.runtime())
        self.assertTrue(landed["ok"], landed["problems"])
        self.assertFalse(landed["load"]["ok"], landed["load"])
        self.assertEqual(landed["load"]["stage"], "static-blocked")
        self.assertIn("结构性不可用", landed["load"]["error"])
        self.assertNotIn("默认停用", landed["load"]["error"])
        self.assertTrue(landed["installed_but_disabled"])

    def test_load_keys_and_top_level_schema_hold(self):
        got = pack_import.inspect(self.skill_zip("keys.zip", "edge_keys"))
        self.assertEqual(len(got), 19, sorted(got))
        self.assertEqual(sorted(got["load"]),
                         ["checked", "code", "error", "ok", "stage", "warnings"])
        self.assertIsInstance(got["load"]["ok"], bool)
        self.assertIsInstance(got["load"]["error"], str)
        self.assertIsInstance(got["load"]["code"], str)
        self.assertIsInstance(got["load"]["stage"], str)
        self.assertIsInstance(got["load"]["checked"], str)
        self.assertIsInstance(got["load"]["warnings"], list)


if __name__ == "__main__":
    unittest.main()
