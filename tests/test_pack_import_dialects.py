"""两道闸门的口径用例：whale-plugin v1 老方言归一 + 控制台 UI 缺失降级为 notice。

夹具对应市场实测：42 个 b1-dialect 包（version:int + app_version、无 entry、包内唯一 .py）与
18 个 needs-console-ui 包（16 个只缺 ui/index.html；ui-accent / ui-cache-rate 的 capabilities
是对象数组，宿主 host-one.mjs 会直接拒）。全部在 tempfile 里跑，不碰仓库 data/。
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
    "ok", "kind", "id", "name", "version", "entry", "files", "bytes", "landed", "target_dir",
    "tools", "hooks", "providers", "problems", "notices", "load", "installed_but_disabled",
    "actions", "duration_ms",
}

JS_ESM = "export async function setup(api) { api.log('ok'); }\n"
JS_UI = (
    "const UI_INDEX = 'ui/index.html';\n"
    "if (!fs.existsSync(UI_INDEX)) throw new Error('找不到 ui/index.html');\n"
)
PY_SOURCE = "from host import PluginBase\n\n\nclass Demo(PluginBase):\n    pass\n"
PY_BANNED = (
    "import subprocess\n"
    "from host import PluginBase, PluginMeta\n\n\n"
    "class Demo(PluginBase):\n"
    "    meta = PluginMeta(name='demo')\n\n"
    "    def run(self):\n"
    "        return subprocess.run(['echo', 'x'])\n"
)
# 42 个老方言包的实测形状（含 config，无 entry 字段）。
B1_MANIFEST = {
    "format": "whale-plugin",
    "version": 1,
    "id": "at_mention",
    "name": "@群友（按名字）",
    "description": "按名字点名群友",
    "category": "message",
    "enabled_by_default": False,
    "app_version": "5.0.0",
    "config": [{"key": "prefix", "type": "string", "default": "@"}],
}


def manifest_js(**extra) -> str:
    data = {"id": "demo-js", "name": "Demo JS", "version": "1.0.0", "entry": "index.js"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, indent=2)


def manifest_py(**extra) -> str:
    data = {"id": "demo_py", "name": "Demo PY", "version": "1.0.0", "entry": "demo_py.py"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, indent=2)


def manifest_skill(**extra) -> str:
    """技能家族：包根 skill.json（记忆宫殿-palace.zip 的实测形状，entry 也是 index.js）。"""
    data = {"id": "demo-skill", "name": "Demo Skill", "version": "1.0.0",
            "apiVersion": 1, "entry": "index.js"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, indent=2)


def pack_json(**extra) -> str:
    data = {"format": "whale-feature-pack", "version": 1, "name": "Demo Pack",
            "plugins": ["a.py"], "skills": ["b"], "preferences": []}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, indent=2)


MCP_JSON = json.dumps({"mcpServers": {"demo": {"command": "C:/Windows/System32/notepad.exe",
                                               "args": []}},
                       "requires": {}, "capabilities": {"demo.status": True}},
                      ensure_ascii=False, indent=2)


class DialectTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pack-dialects-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugins_dir = self.tmp / "data" / "plugins"
        self.data_dir = self.tmp / "data"
        self.skills_root = self.tmp / "data"
        for one in (self.plugins_dir, self.data_dir, self.skills_root):
            one.mkdir(parents=True, exist_ok=True)

    def pack(self, name, files) -> Path:
        path = self.tmp / "zips" / (name + ".zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
            for member, text in files.items():
                archive.writestr(member, text)
        return path

    def inspect(self, path) -> dict:
        return pack_import.inspect(path, plugins_dir=str(self.plugins_dir),
                                   data_dir=str(self.data_dir))

    def install(self, path) -> dict:
        return pack_import.install(path, confirmed=True, plugins_dir=str(self.plugins_dir),
                                   skills_runtime=str(self.skills_root),
                                   data_dir=str(self.data_dir))

    def codes(self, report, key="problems") -> list:
        return [item["code"] for item in report[key]]


class B1DialectNormalizeTests(DialectTestCase):
    def test_int_version_without_entry_normalized_to_notice(self):
        path = self.pack("b1-noentry", {
            "plugin.json": json.dumps(dict(B1_MANIFEST), ensure_ascii=False),
            "at_mention.py": PY_SOURCE,
        })
        report = self.inspect(path)
        self.assertNotIn("b1-dialect", self.codes(report))
        self.assertIn("b1-dialect-normalized", self.codes(report, "notices"))
        self.assertTrue(report["ok"])
        self.assertEqual(report["version"], "5.0.0")
        self.assertEqual(report["entry"], "at_mention.py")

    def test_int_version_with_explicit_entry_keeps_entry(self):
        data = dict(B1_MANIFEST, entry="main.py", id="pixiv_illust")
        path = self.pack("b1-entry", {"plugin.json": json.dumps(data, ensure_ascii=False),
                                      "main.py": PY_SOURCE, "other.py": PY_SOURCE})
        report = self.inspect(path)
        self.assertEqual(report["entry"], "main.py")
        self.assertEqual(report["version"], "5.0.0")
        self.assertTrue(report["ok"])

    def test_int_version_stale_declared_entry_falls_back(self):
        data = dict(B1_MANIFEST, entry="gone.py", id="context_chime")
        path = self.pack("b1-stale", {"plugin.json": json.dumps(data, ensure_ascii=False),
                                      "real.py": PY_SOURCE})
        report = self.inspect(path)
        self.assertEqual(report["entry"], "real.py")
        self.assertTrue(report["ok"])

    def test_int_version_without_source_still_blocked(self):
        data = dict(B1_MANIFEST, id="no_source_pack")
        data.pop("entry", None)
        path = self.pack("b1-nosource", {"plugin.json": json.dumps(data, ensure_ascii=False),
                                         "readme.txt": "no code here"})
        report = self.inspect(path)
        self.assertIn("py-entry-missing", self.codes(report))
        self.assertFalse(report["ok"])

    def test_int_version_two_sources_still_blocked(self):
        path = self.pack("b1-two", {"plugin.json": json.dumps(dict(B1_MANIFEST, id="two_src"),
                                                              ensure_ascii=False),
                                    "a.py": PY_SOURCE, "b.py": PY_SOURCE})
        report = self.inspect(path)
        self.assertIn("py-entry-missing", self.codes(report))
        self.assertFalse(report["ok"])

    def test_string_version_gets_no_dialect_notice(self):
        path = self.pack("b2", {"plugin.json": manifest_py(version="1.0.0"), "demo_py.py": PY_SOURCE})
        report = self.inspect(path)
        self.assertNotIn("b1-dialect-normalized", self.codes(report, "notices"))
        self.assertEqual(report["version"], "1.0.0")

    def test_schema_keys_complete_on_notice_path(self):
        path = self.pack("b1-keys", {"plugin.json": json.dumps(dict(B1_MANIFEST, id="keys_pack"),
                                                               ensure_ascii=False),
                                     "keys_pack.py": PY_SOURCE})
        self.assertTrue(SCHEMA_KEYS.issubset(set(self.inspect(path))))


class ConsoleUiNoticeTests(DialectTestCase):
    def test_missing_ui_is_notice_not_problem(self):
        path = self.pack("no-ui", {"plugin.json": manifest_js(entry="index.js"), "index.js": JS_UI})
        report = self.inspect(path)
        self.assertIn("needs-console-ui", self.codes(report, "notices"))
        self.assertNotIn("needs-console-ui", self.codes(report))
        self.assertTrue(report["ok"])

    def test_missing_ui_pack_still_installs(self):
        path = self.pack("no-ui-install", {"plugin.json": manifest_js(id="no-ui-demo",
                                                                     entry="index.js"),
                                           "index.js": JS_UI})
        report = self.install(path)
        self.assertTrue(report["ok"], report["problems"])
        self.assertIn("needs-console-ui", self.codes(report, "notices"))
        landed = Path(report["landed"])
        self.assertTrue(landed.is_dir())
        self.assertTrue((landed / "index.js").is_file())
        self.assertFalse((landed / "ui" / "index.html").exists())
        self.assertTrue(report["installed_but_disabled"])

    def test_ui_present_no_notice(self):
        path = self.pack("ui-ok", {"plugin.json": manifest_js(entry="index.js"), "index.js": JS_UI,
                                   "ui/index.html": "<html></html>"})
        self.assertNotIn("needs-console-ui", self.codes(self.inspect(path), "notices"))


class CapabilitySchemaTests(DialectTestCase):
    def test_capabilities_object_list_rejected(self):
        caps = [{"name": "ui-accent.status", "description": "当前界面主题色与生效状态"}]
        path = self.pack("cap-obj", {"plugin.json": manifest_js(id="ui-accent", capabilities=caps),
                                     "index.js": JS_ESM, "ui/index.html": "<html></html>"})
        report = self.inspect(path)
        self.assertIn("capability-names-invalid", self.codes(report))
        self.assertFalse(report["ok"])
        message = [item["message"] for item in report["problems"]][0]
        self.assertIn("capabilities", message)
        self.assertIn("能力名字符串数组", message)

    def test_requires_object_list_rejected(self):
        path = self.pack("req-obj", {
            "plugin.json": manifest_js(id="req_demo", requires=[{"name": "other.status"}]),
            "index.js": JS_ESM, "ui/index.html": "<html></html>"})
        report = self.inspect(path)
        self.assertIn("capability-names-invalid", self.codes(report))
        self.assertIn("requires", [item["message"] for item in report["problems"]][0])

    def test_capabilities_not_a_list_rejected(self):
        path = self.pack("cap-str", {"plugin.json": manifest_js(id="cap_str", capabilities="demo.status"),
                                     "index.js": JS_ESM, "ui/index.html": "<html></html>"})
        self.assertIn("capability-names-invalid", self.codes(self.inspect(path)))

    def test_capabilities_too_many_rejected(self):
        caps = ["demo.status%d" % index for index in range(129)]
        path = self.pack("cap-many", {"plugin.json": manifest_js(id="cap_many", capabilities=caps),
                                      "index.js": JS_ESM, "ui/index.html": "<html></html>"})
        self.assertIn("capability-names-invalid", self.codes(self.inspect(path)))

    def test_empty_capabilities_accepted(self):
        path = self.pack("cap-empty", {"plugin.json": manifest_js(id="cap_empty", capabilities=[]),
                                       "index.js": JS_ESM, "ui/index.html": "<html></html>"})
        report = self.inspect(path)
        self.assertEqual(self.codes(report), [])
        self.assertTrue(report["ok"])

    def test_schema_violation_blocks_install(self):
        caps = [{"name": "ui-cache-rate.status", "description": "缓存命中率"}]
        path = self.pack("cap-obj-install", {"plugin.json": manifest_js(id="topo_rate",
                                                                        capabilities=caps),
                                             "index.js": JS_ESM, "ui/index.html": "<html></html>"})
        report = self.install(path)
        self.assertFalse(report["ok"])
        self.assertIn("capability-names-invalid", self.codes(report))
        self.assertFalse(report["landed"])


class CapabilityFamilyGateTests(DialectTestCase):
    """capability-names-invalid 按家族补齐：只有宿主真会拒收的家族才加闸。

    依据 runtime/qqagent-host/host-one.mjs:69-91 —— 宿主取包根清单（skill.json 优先）后，
    names() 对 requires/capabilities 做「数组 + ≤128 项 + 每项非空字符串」检查，不符即
    manifest-invalid；而 plugin-py 的 plugin.json 由 src/plugin_installer.py 读、
    feature-pack 的 pack.json 由 src/feature_pack.py 读、mcp.json 由独立进程协议消费，
    三者都没有这道校验，加闸就是误拒。
    """

    def test_skill_requires_object_rejected(self):
        # 记忆宫殿-palace.zip 的真实形状：skill.json 里 requires 是空对象。
        path = self.pack("skill-req-obj", {
            "skill.json": manifest_skill(requires={}, capabilities=[]),
            "index.js": JS_ESM})
        report = self.inspect(path)
        self.assertEqual(report["kind"], "skill")
        self.assertFalse(report["ok"])
        gate = [item for item in report["problems"]
                if item["code"] == "capability-names-invalid"]
        self.assertEqual(len(gate), 1)
        self.assertIn("skill.json", gate[0]["message"])
        self.assertIn("requires", gate[0]["message"])
        self.assertIn("host-one.mjs:86", gate[0]["hint"])
        self.assertNotIn("同一口径", gate[0]["hint"])

    def test_skill_capabilities_not_a_list_rejected(self):
        path = self.pack("skill-cap-str", {
            "skill.json": manifest_skill(capabilities="demo.status"),
            "index.js": JS_ESM})
        report = self.inspect(path)
        self.assertIn("capability-names-invalid", self.codes(report))
        self.assertIn("capabilities", [item["message"] for item in report["problems"]
                                       if item["code"] == "capability-names-invalid"][0])

    def test_skill_capabilities_over_128_rejected(self):
        caps = ["demo.status%d" % index for index in range(129)]
        path = self.pack("skill-cap-many", {
            "skill.json": manifest_skill(capabilities=caps),
            "index.js": JS_ESM})
        self.assertIn("capability-names-invalid", self.codes(self.inspect(path)))

    def test_skill_capability_violation_blocks_landing(self):
        path = self.pack("skill-req-land", {
            "skill.json": manifest_skill(id="palace", requires={}),
            "index.js": JS_ESM})
        report = self.install(path)
        self.assertFalse(report["ok"])
        self.assertIn("capability-names-invalid", self.codes(report))
        self.assertFalse(report["landed"])
        self.assertFalse((self.skills_root / "skills" / "palace").exists())

    def test_skill_valid_capability_shapes_accepted(self):
        # 128 项正好放行（宿主只拒 >128），空数组也放行：别把合法包一起拒了。
        caps = ["demo.status%d" % index for index in range(128)]
        path = self.pack("skill-cap-ok", {
            "skill.json": manifest_skill(capabilities=caps, requires=[]),
            "index.js": JS_ESM})
        report = self.inspect(path)
        self.assertEqual([code for code in self.codes(report)
                          if code == "capability-names-invalid"], [])
        self.assertTrue(report["ok"])

    def test_plugin_py_capability_shape_not_gated(self):
        # src/plugin_installer.py 全文不读 capabilities/requires → 宿主不会因此拒收。
        path = self.pack("py-cap-obj", {
            "plugin.json": manifest_py(requires={"name": "other.status"},
                                       capabilities={"x": 1}),
            "demo_py.py": PY_SOURCE})
        report = self.inspect(path)
        self.assertEqual(report["kind"], "plugin-py")
        self.assertNotIn("capability-names-invalid", self.codes(report))
        self.assertTrue(report["ok"])

    def test_feature_pack_capability_shape_not_gated(self):
        # src/feature_pack.py:74-88 只查 pack.json 存在/大小/JSON/对象。
        path = self.pack("pack-cap-obj", {
            "pack.json": pack_json(requires={}, capabilities="nope")})
        report = self.inspect(path)
        self.assertEqual(report["kind"], "feature-pack")
        self.assertNotIn("capability-names-invalid", self.codes(report))
        self.assertTrue(report["ok"])

    def test_mcp_server_capability_shape_not_gated(self):
        # MCP 工具由独立进程按协议给出，宿主不读清单里的 capabilities/requires。
        path = self.pack("mcp-cap-obj", {"mcp.json": MCP_JSON})
        report = self.inspect(path)
        self.assertEqual(report["kind"], "mcp-server")
        self.assertNotIn("capability-names-invalid", self.codes(report))
        self.assertTrue(report["ok"])


class SecurityGateTests(DialectTestCase):
    def test_subprocess_plugin_still_rejected(self):
        path = self.pack("subproc", {"plugin.json": manifest_py(id="sub_proc"),
                                     "demo_py.py": PY_BANNED})
        report = self.install(path)
        self.assertFalse(report["ok"])
        self.assertEqual(self.codes(report), ["install-failed"])
        self.assertIn("安全检查未通过", report["problems"][0]["message"])
        self.assertIn("启动子进程 subprocess", report["problems"][0]["message"])
        self.assertFalse(report["landed"])

    def test_b1_normalization_does_not_relax_security_check(self):
        data = dict(B1_MANIFEST, id="b1_sub_proc")
        path = self.pack("b1-subproc", {"plugin.json": json.dumps(data, ensure_ascii=False),
                                        "b1_sub_proc.py": PY_BANNED})
        report = self.install(path)
        self.assertIn("b1-dialect-normalized", self.codes(report, "notices"))
        self.assertFalse(report["ok"])
        self.assertEqual(self.codes(report), ["install-failed"])
        self.assertIn("启动子进程 subprocess", report["problems"][0]["message"])


if __name__ == "__main__":
    unittest.main()
