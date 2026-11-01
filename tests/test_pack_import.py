"""src/pack_import.py 单测：决策表 7 条、目录与 zip 两种形态、双嵌套下钻、同名跨家族、
B1 方言、CJS、未知包、异常输入永不抛、统一 schema 键完整性。

夹具全部在 tempfile 里生成：不读真实市场目录、不写 data/、不联网、不起 node。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from src import pack_import

SCHEMA_KEYS = {
    "ok",
    "kind",
    "id",
    "name",
    "version",
    "entry",
    "files",
    "bytes",
    "landed",
    "target_dir",
    "tools",
    "hooks",
    "providers",
    "problems",
    "notices",
    "load",
    "installed_but_disabled",
    "actions",
    "duration_ms",
}

JS_ESM = "export async function setup(api) { api.log('ok'); }\n"
# 与 tests/test_skill_manifest.py 同口径：入口写一行 registerTool，保证不是 T6 no-tools。
JS_REGISTER = "registerTool({name:'ping', description:'p', parameters:{type:'object'}});\n"
# CommonJS：有 module.exports，且没有任何 ESM import/export。
JS_CJS = "const api = require('../../src/api');\nmodule.exports = { setup() { return api; } };\n"
# 需要控制台 UI 的真实形状（external-market/install-by-code.zip 等）：
# 源码里写死 ui/index.html 并做存在性检查，包内却没有这个文件。
JS_UI = (
    "const UI_INDEX = 'ui/index.html';\n"
    "if (!fs.existsSync(UI_INDEX)) throw new Error('找不到 ui/index.html');\n"
)
PY_SOURCE = "from host import PluginBase\n\n\nclass Demo(PluginBase):\n    pass\n"


def plugin_js(**extra) -> str:
    data = {"id": "demo-js", "name": "Demo JS", "version": "1.0.0", "entry": "index.js"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, indent=2)


def plugin_py(**extra) -> str:
    data = {"id": "demo_py", "name": "Demo PY", "version": "1.0.0", "entry": "demo_py.py"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, indent=2)


def skill_manifest(skill_id: str = "demo-skill", **extra) -> str:
    data = {"id": skill_id, "name": skill_id.upper(), "version": "1.0.0"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, indent=2)


def write_tree(root, files) -> Path:
    root = Path(root)
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def make_zip(path, files) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


class ImportTestCase(unittest.TestCase):
    """公共夹具：一个临时根目录。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pack-import-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def package(self, name, files) -> Path:
        return write_tree(self.tmp / "pkg" / name, files)

    def package_zip(self, name, files) -> Path:
        return make_zip(self.tmp / "zips" / (name + ".zip"), files)

    def codes(self, report, key="problems"):
        return [item["code"] for item in report[key]]


class DecisionTableTests(ImportTestCase):
    """决策表 7 条，每条至少一例。"""

    def test_rule1_feature_pack_dir(self):
        found = pack_import.detect(self.package("pack", {
            "pack.json": json.dumps({"format": "whale-feature-pack", "version": 1,
                                     "plugins": ["a", "b"], "skills": ["c"]}),
        }))
        self.assertEqual(found["kind"], "feature-pack")
        self.assertEqual(found["manifest_name"], "pack.json")
        self.assertEqual(found["dialect"], "whale-feature-pack-v1")
        self.assertGreaterEqual(found["confidence"], 0.9)

    def test_rule1_feature_pack_needs_version_1(self):
        found = pack_import.detect(self.package("pack2", {
            "pack.json": json.dumps({"format": "whale-feature-pack", "version": 2}),
        }))
        self.assertEqual(found["kind"], "unknown")

    def test_rule2_skill_dir(self):
        found = pack_import.detect(self.package("skill", {
            "skill.json": skill_manifest(), "index.js": JS_REGISTER,
        }))
        self.assertEqual(found["kind"], "skill")
        self.assertEqual(found["manifest_name"], "skill.json")

    def test_rule2_skill_zip(self):
        found = pack_import.detect(self.package_zip("skillzip", {
            "demo-skill/skill.json": skill_manifest(), "demo-skill/index.js": JS_REGISTER,
        }))
        self.assertEqual(found["kind"], "skill")
        self.assertEqual(found["root"], "demo-skill")

    def test_rule2_skill_beats_plugin_json(self):
        found = pack_import.detect(self.package("both", {
            "skill.json": skill_manifest(), "plugin.json": plugin_js(), "index.js": JS_ESM,
        }))
        self.assertEqual(found["kind"], "skill")
        self.assertEqual(found["manifest_name"], "skill.json")

    def test_rule3_extension_js_by_index_js(self):
        found = pack_import.detect(self.package("ext", {
            "plugin.json": plugin_js(), "index.js": JS_ESM,
        }))
        self.assertEqual(found["kind"], "extension-js")
        self.assertEqual(found["dialect"], "qqagent-0.4")

    def test_rule3_extension_js_by_api_version(self):
        found = pack_import.detect(self.package("ext2", {
            "plugin.json": plugin_js(apiVersion=1), "main.js": JS_ESM,
        }))
        self.assertEqual(found["kind"], "extension-js")
        self.assertTrue(any("apiVersion" in line for line in found["evidence"]))

    def test_rule3_extension_js_by_mjs_entry(self):
        found = pack_import.detect(self.package("ext3", {
            "plugin.json": plugin_js(entry="main.mjs"), "main.mjs": JS_ESM,
        }))
        self.assertEqual(found["kind"], "extension-js")

    def test_rule4_plugin_py_by_format(self):
        found = pack_import.detect(self.package("py1", {
            "plugin.json": plugin_py(format="whale-plugin"), "demo_py.py": PY_SOURCE,
        }))
        self.assertEqual(found["kind"], "plugin-py")
        self.assertEqual(found["dialect"], "whale-plugin-v1")

    def test_rule4_plugin_py_by_host(self):
        found = pack_import.detect(self.package("py2", {
            "plugin.json": plugin_py(host="WhaleGirlQQ", entry="demo_py.py", min_host_version="1.0.0"),
            "demo_py.py": PY_SOURCE,
        }))
        self.assertEqual(found["kind"], "plugin-py")
        self.assertEqual(found["dialect"], "whalegirlqq-host")

    def test_rule5_plugin_py_without_manifest(self):
        found = pack_import.detect(self.package("py3", {"solo.py": PY_SOURCE}))
        self.assertEqual(found["kind"], "plugin-py")
        self.assertEqual(found["manifest_name"], "")
        self.assertIn("no-manifest", self.codes(pack_import.inspect(self.tmp / "pkg" / "py3"), "notices"))

    def test_rule6_extension_js_without_manifest(self):
        found = pack_import.detect(self.package("js3", {"solo.js": JS_ESM}))
        self.assertEqual(found["kind"], "extension-js")
        self.assertEqual(found["dialect"], "js-no-manifest")

    def test_rule7_unknown_empty_dir(self):
        found = pack_import.detect(self.package("nothing", {"README.md": "hi"}))
        self.assertEqual(found["kind"], "unknown")
        self.assertEqual(found["confidence"], 0.0)

    def test_rule7_unknown_zip_without_manifest(self):
        found = pack_import.detect(self.package_zip("junk", {"readme.txt": "hi"}))
        self.assertEqual(found["kind"], "unknown")
        self.assertEqual(found["manifest_name"], "")


class DrillTests(ImportTestCase):
    """下钻对齐 host-one.mjs:60-68 的 extensionRoot()。"""

    def test_double_nested_package(self):
        found = pack_import.detect(self.package_zip("owner-pause", {
            "owner-pause/owner-pause/plugin.json": plugin_js(),
            "owner-pause/owner-pause/index.js": JS_ESM,
        }))
        self.assertEqual(found["kind"], "extension-js")
        self.assertEqual(found["root"], "owner-pause/owner-pause")

    def test_drill_into_only_subtree_with_manifest(self):
        found = pack_import.detect(self.package_zip("mixed", {
            "pkg/README.md": "hi",
            "pkg/assets/a.txt": "a",
            "pkg/real/plugin.json": plugin_js(),
            "pkg/real/index.js": JS_ESM,
        }))
        self.assertEqual(found["kind"], "extension-js")
        self.assertEqual(found["root"], "pkg/real")

    def test_drill_stops_at_depth_limit(self):
        deep = "/".join(["d%d" % index for index in range(1, 6)])
        found = pack_import.detect(self.package_zip("deep", {
            deep + "/plugin.json": plugin_js(), deep + "/index.js": JS_ESM,
        }))
        # host-one 最多下钻 4 层，第 5 层里的清单认不到。
        self.assertEqual(found["kind"], "unknown")


class CrossFamilyTests(ImportTestCase):
    """同名跨家族：只按包内容判，不按 id 或目录猜。"""

    def test_same_id_two_families(self):
        js_dir = self.package("js-copy", {"plugin.json": plugin_js(id="long_image_reader"),
                                          "index.js": JS_ESM})
        py_dir = self.package("py-copy", {"plugin.json": plugin_py(id="long_image_reader"),
                                          "demo_py.py": PY_SOURCE})
        self.assertEqual(pack_import.detect(js_dir)["kind"], "extension-js")
        self.assertEqual(pack_import.detect(py_dir)["kind"], "plugin-py")
        self.assertEqual(pack_import.inspect(js_dir)["id"], "long_image_reader")
        self.assertEqual(pack_import.inspect(py_dir)["id"], "long_image_reader")


class InspectProblemTests(ImportTestCase):
    """inspect() 的特殊标记与统一 schema。"""

    def test_b1_dialect_normalized(self):
        path = self.package("b1", {
            "plugin.json": json.dumps({"id": "old_py", "name": "老方言", "format": "whale-plugin",
                                       "version": 1, "app_version": "2.3.4"}),
            "old_py.py": PY_SOURCE,
        })
        report = pack_import.inspect(path)
        self.assertIn("b1-dialect-normalized", self.codes(report, "notices"))
        self.assertEqual(report["version"], "2.3.4")
        self.assertEqual(report["entry"], "old_py.py")
        self.assertTrue(report["ok"])

    def test_b2_dialect_not_flagged(self):
        path = self.package("b2", {
            "plugin.json": plugin_py(host="WhaleGirlQQ", min_host_version="1.0.0"),
            "demo_py.py": PY_SOURCE,
        })
        report = pack_import.inspect(path)
        self.assertNotIn("b1-dialect-normalized", self.codes(report, "notices"))
        self.assertTrue(report["ok"])

    def test_cjs_unsupported(self):
        path = self.package("cjs", {"plugin.json": plugin_js(), "index.js": JS_CJS})
        report = pack_import.inspect(path)
        self.assertIn("cjs-unsupported", self.codes(report))

    def test_esm_entry_not_flagged_as_cjs(self):
        path = self.package("esm", {"plugin.json": plugin_js(), "index.js": JS_ESM})
        self.assertNotIn("cjs-unsupported", self.codes(pack_import.inspect(path)))

    def test_needs_console_ui_when_missing(self):
        path = self.package("ui", {"plugin.json": plugin_js(), "index.js": JS_UI})
        report = pack_import.inspect(path)
        self.assertIn("needs-console-ui", self.codes(report, "notices"))
        self.assertNotIn("extension-unavailable", self.codes(report))
        self.assertTrue(report["ok"])

    def test_console_ui_present_is_fine(self):
        path = self.package("ui2", {"plugin.json": plugin_js(), "index.js": JS_ESM,
                                    "ui/index.html": "<html></html>"})
        self.assertNotIn("needs-console-ui", self.codes(pack_import.inspect(path), "notices"))

    def test_enabled_by_default_false_notice(self):
        path = self.package("disabled", {
            "plugin.json": plugin_js(enabledByDefault=False), "index.js": JS_ESM,
        })
        report = pack_import.inspect(path)
        self.assertIn("enabled-by-default-false", self.codes(report, "notices"))
        self.assertTrue(report["ok"])

    def test_skill_tier_goes_into_load_and_notices(self):
        path = self.package("sk", {"skill.json": skill_manifest(), "index.js": JS_REGISTER})
        report = pack_import.inspect(path)
        self.assertEqual(report["kind"], "skill")
        self.assertTrue(report["load"]["code"])
        self.assertIn("skill-tier", self.codes(report, "notices"))

    def test_target_dir_is_planned_not_created(self):
        py_path = self.package("land1", {"plugin.json": plugin_py(), "demo_py.py": PY_SOURCE})
        plugins_dir = self.tmp / "plugins"
        report = pack_import.inspect(py_path, plugins_dir=plugins_dir)
        self.assertEqual(report["target_dir"], str(plugins_dir / "demo_py.py"))
        self.assertFalse(plugins_dir.exists())

        skill_path = self.package("land2", {"skill.json": skill_manifest(), "index.js": JS_REGISTER})
        skills_runtime = self.tmp / "runtime"
        report = pack_import.inspect(skill_path, skills_runtime=skills_runtime)
        self.assertEqual(report["target_dir"], str(skills_runtime / "skills" / "demo-skill"))
        self.assertFalse(skills_runtime.exists())

    def test_unrecognized_problem_for_unknown(self):
        report = pack_import.inspect(self.package("nope", {"readme.txt": "hi"}))
        self.assertEqual(report["kind"], "unknown")
        self.assertIn("unrecognized", self.codes(report))
        self.assertFalse(report["ok"])

    def test_read_only_no_side_effects(self):
        path = self.package("clean", {"plugin.json": plugin_js(), "index.js": JS_ESM})
        before = sorted(item.name for item in path.rglob("*"))
        pack_import.inspect(path)
        pack_import.detect(path)
        after = sorted(item.name for item in path.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual([item.name for item in self.tmp.iterdir()], ["pkg"])


class NeverRaiseTests(ImportTestCase):
    """detect()/inspect() 对任何垃圾输入都不抛。"""

    def test_detect_missing_path(self):
        found = pack_import.detect(self.tmp / "does-not-exist.zip")
        self.assertEqual(found["kind"], "unknown")
        self.assertTrue(found["evidence"])

    def test_detect_empty_bytes(self):
        self.assertEqual(pack_import.detect(b"")["kind"], "unknown")

    def test_detect_corrupt_zip_bytes(self):
        self.assertEqual(pack_import.detect(b"PK\x03\x04 not really a zip")["kind"], "unknown")

    def test_detect_binary_garbage(self):
        self.assertEqual(pack_import.detect(bytes(range(256)) * 8)["kind"], "unknown")

    def test_detect_empty_file(self):
        target = self.tmp / "empty.zip"
        target.write_bytes(b"")
        self.assertEqual(pack_import.detect(target)["kind"], "unknown")

    def test_detect_none_does_not_raise(self):
        self.assertEqual(pack_import.detect(None)["kind"], "unknown")

    def test_inspect_missing_path(self):
        report = pack_import.inspect(self.tmp / "nothing-here")
        self.assertIn("path-missing", self.codes(report))
        self.assertEqual(set(report), SCHEMA_KEYS)

    def test_inspect_corrupt_zip_file(self):
        target = self.tmp / "broken.zip"
        target.write_bytes(b"not a zip at all")
        report = pack_import.inspect(target)
        self.assertIn("zip-invalid", self.codes(report))

    def test_inspect_broken_manifest_json(self):
        path = self.package("badjson", {"plugin.json": "{not json", "index.js": JS_ESM})
        report = pack_import.inspect(path)
        self.assertEqual(set(report), SCHEMA_KEYS)


class SchemaTests(ImportTestCase):
    """统一 schema：所有分支键集合一致（GUI 只渲染一套）。"""

    def test_all_branches_share_one_key_set(self):
        samples = [
            self.package("s-feature", {"pack.json": json.dumps(
                {"format": "whale-feature-pack", "version": 1, "plugins": ["a"], "skills": [],
                 "preferences": [{"key": "k", "value": 1}]})}),
            self.package("s-skill", {"skill.json": skill_manifest(), "index.js": JS_REGISTER}),
            self.package("s-js", {"plugin.json": plugin_js(), "index.js": JS_ESM}),
            self.package("s-py", {"plugin.json": plugin_py(), "demo_py.py": PY_SOURCE}),
            self.package("s-unknown", {"readme.txt": "hi"}),
            self.tmp / "s-missing",
        ]
        reports = [pack_import.inspect(item) for item in samples]
        for report in reports:
            self.assertEqual(set(report), SCHEMA_KEYS)
            self.assertIsInstance(report["problems"], list)
            self.assertIsInstance(report["notices"], list)
            self.assertIsInstance(report["tools"], list)
            self.assertIn("ok", report["load"])
            self.assertIsInstance(report["duration_ms"], int)

    def test_kinds_constant_matches_detected_kinds(self):
        detected = {
            pack_import.detect(self.package("k-feature", {"pack.json": json.dumps(
                {"format": "whale-feature-pack", "version": 1})}))["kind"],
            pack_import.detect(self.package("k-skill", {"skill.json": skill_manifest()}))["kind"],
            pack_import.detect(self.package("k-js", {"plugin.json": plugin_js(),
                                                     "index.js": JS_ESM}))["kind"],
            pack_import.detect(self.package("k-py", {"plugin.json": plugin_py(),
                                                     "demo_py.py": PY_SOURCE}))["kind"],
            pack_import.detect(self.package("k-mcp", {"mcp.json": json.dumps(
                {"mcpServers": {"demo": {"command": "node", "args": ["server.js"]}}})}))["kind"],
            pack_import.detect(self.package("k-mod", {"plugin.json": json.dumps(
                {"id": "k-mod", "api_version": 1, "entry": "main.py"}),
                "main.py": "VALUE = 1\n"}))["kind"],
            pack_import.detect(self.package("k-unknown", {"x.txt": "x"}))["kind"],
        }
        self.assertIn("mcp-server", detected)
        self.assertEqual(detected, set(pack_import.KINDS))


if __name__ == "__main__":
    unittest.main()
