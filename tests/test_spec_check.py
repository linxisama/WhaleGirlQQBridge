"""spec_check 的守卫测试：坏包被点名、好包三层全过、原因码与三层结果对得上。

夹具两类：临时构造（坏清单/老方言/能力坏形状/只有 url 的 MCP/subprocess/重复 id/空包）、
真实语料里的包（合法 JS 扩展、合法 MCP、合法 skill）。断言的是 spec_check 的判定，
不是 pack_import 的实现细节。
"""
import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "tools" / "spec_check.py"
MARKET = Path(r"D:/聊天机器人/qq机器人/.tmp/external-market")
SKILL_MARKET = Path(r"D:/聊天机器人/插件市场/Skill市场")


def load_script():
    spec = importlib.util.spec_from_file_location("spec_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SPEC = load_script()

JS_INDEX = """export const hooks = {};
export async function setup(api) {
  api.registerTool({
    id: 'demo_ping',
    name: 'demo_ping',
    description: '演示',
    parameters: { type: 'object', properties: {} },
    async execute() { return { content: 'pong' }; },
  });
}
"""


def manifest(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False)


def write_pack(base: Path, name: str, files: dict) -> Path:
    root = base / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def zip_pack(base: Path, name: str, files: dict) -> Path:
    path = base / ("%s.zip" % name)
    with zipfile.ZipFile(str(path), "w") as archive:
        for rel, text in files.items():
            archive.writestr(rel, text)
    return path


class FixtureCase(unittest.TestCase):
    """一套隔离宿主根 + 一批构造夹具，避免每个用例都重建宿主。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="spec-check-test-")
        cls.work = Path(cls._tmp.name)
        cls.packs = cls.work / "packs"
        cls.packs.mkdir(parents=True, exist_ok=True)
        cls.host = SPEC.Host(cls.work / "host")
        cls.host.prepare()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def opts(self, layers=("l1", "l2", "l3")):
        return argparse.Namespace(layers=set(layers), call_limit=1, timeout=20.0,
                                  overwrite=False, seen_ids={})

    def check(self, target, opts=None):
        return SPEC.check_one(target, "fixture", self.host, opts or self.opts())


class BadPackTests(FixtureCase):
    """坏包必须在对应层被点名，且给出具体原因码。"""

    def test_bad_manifest_is_manifest_invalid(self):
        target = write_pack(self.packs, "bad_manifest",
                            {"plugin.json": "{ this is not json", "index.js": JS_INDEX})
        row = self.check(target)
        self.assertFalse(row["l1"])
        self.assertEqual(row["l1_codes"], ["manifest-invalid"])
        self.assertIn("不是合法 JSON", row["details"]["l1"])
        self.assertFalse(row["l2"])

    def test_capability_field_bad_shape(self):
        target = write_pack(self.packs, "cap_bad_shape", {
            "plugin.json": manifest(id="cap_bad_shape", name="能力坏形状", version="1.0.0",
                                    apiVersion="0.4", entry="index.js",
                                    capabilities=[{"name": "x.status"}]),
            "index.js": JS_INDEX})
        row = self.check(target)
        self.assertEqual(row["kind"], "extension-js")
        self.assertFalse(row["l1"])
        self.assertEqual(row["l1_codes"], ["capability-names-invalid"])

    def test_mcp_with_url_only_is_unsupported_transport(self):
        target = write_pack(self.packs, "mcp_url_only", {"mcp.json": manifest(
            mcpServers={"remote": {"url": "https://example.com/mcp"}})})
        row = self.check(target)
        self.assertEqual(row["kind"], "mcp-server")
        self.assertFalse(row["l1"])
        self.assertEqual(row["l1_codes"], ["unsupported-transport"])

    def test_empty_pack_is_unrecognized(self):
        target = write_pack(self.packs, "empty_pack", {"readme.txt": "nothing\n"})
        row = self.check(target)
        self.assertEqual(row["kind"], "unknown")
        self.assertFalse(row["l1"])
        self.assertIn("unrecognized", row["l1_codes"])

    def test_python_subprocess_blocked_at_install(self):
        target = write_pack(self.packs, "py_subprocess", {
            "plugin.json": manifest(id="py_subprocess", name="子进程", version="1.0.0",
                                    format="whale-plugin", entry="evil.py"),
            "evil.py": "import subprocess\n\nVALUE = 1\n"})
        row = self.check(target)
        self.assertTrue(row["l1"], row["l1_codes"])
        self.assertFalse(row["l2"])
        self.assertIn("security-forbidden-subprocess", row["l2_codes"])

    def test_same_id_twice_is_duplicate_id(self):
        files = {"plugin.json": manifest(id="dup_same", name="重复", version="1.0.0",
                                         apiVersion="0.4", entry="index.js"),
                 "index.js": JS_INDEX}
        first = write_pack(self.packs, "dup_a", files)
        second = write_pack(self.packs, "dup_b", files)
        opts = self.opts()
        self.assertTrue(self.check(first, opts)["l2"])
        row = self.check(second, opts)
        self.assertTrue(row["l1"])
        self.assertFalse(row["l2"])
        self.assertEqual(row["l2_codes"], ["duplicate-id"])


class GoodPackTests(FixtureCase):
    """合规包必须三层全过；告知码不许当成失败。"""

    def test_legacy_dialect_is_normalized_not_rejected(self):
        target = write_pack(self.packs, "legacy_dialect", {
            "plugin.json": manifest(id="legacy_demo", name="老方言", version=1,
                                    app_version="2.0.0", entry="main.py"),
            "main.py": "from . import PluginBase, PluginMeta\n\n\n"
                       "class LegacyPlugin(PluginBase):\n"
                       "    META = PluginMeta(id='legacy_demo', name='老方言')\n"})
        row = self.check(target)
        self.assertEqual(row["kind"], "plugin-py")
        self.assertTrue(row["l1"], row["l1_codes"])
        self.assertIn("b1-dialect-normalized", row["notices"])
        self.assertTrue(row["l2"], row["l2_codes"] + [row["details"].get("l2", "")])

    def test_valid_extension_dir_passes_three_layers(self):
        target = write_pack(self.packs, "valid_js", {
            "plugin.json": manifest(id="valid_js", name="合法JS", version="1.0.0",
                                    apiVersion="0.4", entry="index.js"),
            "index.js": JS_INDEX})
        row = self.check(target)
        self.assertEqual(row["kind"], "extension-js")
        self.assertTrue(row["l1"], row["l1_codes"])
        self.assertTrue(row["l2"], row["l2_codes"])
        if row["l3_status"] == "unverified" and "node-missing" in row["l3_codes"]:
            self.skipTest("本机没有 node，扩展真加载不可验证")
        self.assertTrue(row["l3"], row["l3_codes"] + [row["details"].get("l3", "")])
        self.assertEqual(row["l3_status"], "pass")
        self.assertIn("demo_ping", json.dumps(row.get("l3_calls") or [], ensure_ascii=False))

    def test_valid_extension_zip_passes_three_layers(self):
        target = zip_pack(self.packs, "valid_js_zip", {
            "plugin.json": manifest(id="valid_js_zip", name="合法JS-ZIP", version="1.0.0",
                                    apiVersion="0.4", entry="index.js"),
            "index.js": JS_INDEX})
        row = self.check(target)
        self.assertTrue(row["l1"], row["l1_codes"])
        self.assertTrue(row["l2"], row["l2_codes"])
        if "node-missing" in row["l3_codes"]:
            self.skipTest("本机没有 node")
        self.assertTrue(row["l3"], row["l3_codes"])

    def test_valid_mcp_stdio_installs_and_reports_no_host_call(self):
        target = write_pack(self.packs, "mcp_stdio", {"mcp.json": manifest(
            mcpServers={"demo": {"command": sys.executable, "args": ["-y", "demo-mcp"]}})})
        row = self.check(target)
        self.assertEqual(row["kind"], "mcp-server")
        self.assertTrue(row["l1"], row["l1_codes"])
        self.assertTrue(row["l2"], row["l2_codes"])
        self.assertEqual(row["l3_status"], "unverified")
        self.assertEqual(row["l3_codes"], ["no-host-call"])


class RealCorpusTests(FixtureCase):
    """真实语料里的合法包：L1/L2 必须过，L3 的结论要与宿主实情一致。"""

    def real(self, path: Path):
        if not path.is_file():
            self.skipTest("语料不在：%s" % path)
        return path

    def test_real_extension_passes_l1_l2(self):
        row = self.check(self.real(MARKET / "affinity.zip"))
        self.assertEqual(row["kind"], "extension-js")
        self.assertTrue(row["l1"], row["l1_codes"])
        self.assertTrue(row["l2"], row["l2_codes"])

    def test_real_skill_passes_l1_l2(self):
        target = None
        if SKILL_MARKET.is_dir():
            for item in sorted(SKILL_MARKET.glob("*.zip")):
                found = SPEC.pack_import.detect(item) or {}
                if found.get("kind") == "skill":
                    target = item
                    break
        if target is None:
            self.skipTest("找不到 skill 语料")
        row = self.check(target)
        self.assertEqual(row["kind"], "skill")
        self.assertTrue(row["l1"], row["l1_codes"])
        self.assertTrue(row["l2"], row["l2_codes"])


class CodeBookTests(unittest.TestCase):
    """原因码字典与三层字典必须自洽：一个码只许挂一层。"""

    def test_every_code_has_one_layer(self):
        for code, (layer, meaning) in SPEC.CODE_LAYERS.items():
            self.assertIn(layer, SPEC.LAYERS, code)
            self.assertTrue(meaning.strip(), code)

    def test_unverified_codes_are_declared(self):
        for code in SPEC.UNVERIFIED_CODES:
            self.assertTrue(code.strip())

    def test_cli_codes_exits_zero(self):
        self.assertEqual(SPEC.main(["--codes"]), 0)
