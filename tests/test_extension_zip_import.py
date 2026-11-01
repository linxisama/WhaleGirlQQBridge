"""本地扩展 zip 导入链路：预检（inspect-local）与安装（install-local）的接口测试。

只碰临时数据根（tempfile），不写真实 data/；宿主根由真 materialize 落在临时目录里，
安装与 import_probe 全程真跑（不是 mock 宿主），所以"装进宿主根"这件事是真取证。
"""
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from src import extension_registry, qqagent_host
from src.qq_app import create_app

# 最小可加载扩展：setup 里 registerTool，hooks 里挂一个 before-context。
# 真机验证过 probe.ok=True、tools=["<id>__ping"]、hooks=["before-context"]。
DEMO_JS = """export function setup(api) {
  api.registerTool({ id: 'ping', name: '演示工具', description: '返回 pong',
                     inputSchema: { type: 'object', properties: {} },
                     execute: async () => ({ ok: true, text: 'pong' }) });
}
export function dispose() {}
export const hooks = { 'before-context': () => [] };
export const providers = {};
"""

# 同一条 import 语句既让静态扫描判出 host-source（T4），又能真加载成功。
T4_JS = ("import { ROOT as hostRoot } from '../../src/config.js';\n"
         + DEMO_JS.replace("export const providers = {};",
                           "export const providers = {};\nexport const internals = { hostRoot };"))

# 顶层直接抛：import_probe 拿到 loadError，probe.ok=False。
BROKEN_JS = "throw new Error('wp20-boom 加载即失败');\n" + DEMO_JS

_NO_MANIFEST = object()


def _manifest(ident, **over):
    data = {"id": ident, "name": "演示包 " + ident, "version": "1.0.0",
            "apiVersion": 1, "entry": "index.js", "description": "WP20 最小扩展包"}
    data.update(over)
    return data


def build_zip(folder, name, ident=None, manifest=None, js=DEMO_JS, entries=None):
    """在 folder 里造一个 <根目录>/plugin.json + index.js 的 zip，返回 Path。"""
    ident = ident or name
    path = Path(folder) / ("%s.zip" % name)
    with zipfile.ZipFile(str(path), "w") as archive:
        if manifest is not _NO_MANIFEST:
            payload = _manifest(ident) if manifest is None else manifest
            archive.writestr("%s/plugin.json" % ident, json.dumps(payload, ensure_ascii=False))
        if js is not None:
            archive.writestr("%s/index.js" % ident, js)
        for entry_name, text in (entries or {}).items():
            archive.writestr(entry_name, text)
    return path


def _tree(root):
    """整棵 data 目录的相对路径清单：预检只读就靠它前后比对。"""
    rows = []
    for base, _dirs, files in os.walk(str(root)):
        for name in files:
            rows.append(str(Path(base, name).relative_to(root)).replace("\\", "/"))
    return sorted(rows)


class _FakeManager:
    """路由只向 manager 要 data_dir（其余字段不参与本地导入链路）。"""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.custom_plugins_dir = Path(data_dir) / "custom_plugins"
        self._plugins = {}


class LocalZipImportTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wp20-import-"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.data = self.root / "data"
        (self.data / "qq-only").mkdir(parents=True)
        self.host_root = self.data / "qq-only" / "qqagent_host"
        self.state_file = self.data / "qq-only" / "qqagent_extensions.json"
        self.app = create_app()
        self.app.state.bot = SimpleNamespace(plugins=_FakeManager(self.data))
        self.client = TestClient(self.app)

    # ── 小工具 ──────────────────────────────────────────────────────────
    def inspect(self, path):
        return self.client.post("/api/extensions/inspect-local", json={"zipPath": str(path)})

    def install(self, path, **extra):
        body = {"zipPath": str(path), "confirmed": True}
        body.update(extra)
        return self.client.post("/api/extensions/install-local", json=body)

    def installed_ids(self):
        return sorted(row.get("id") for row in qqagent_host.list_installed(self.host_root))

    def state(self):
        return extension_registry.load(self.data)["extensions"]

    def plugin_dir(self, ident):
        return self.host_root / "plugins" / ident

    # ── a) 预检只读 ─────────────────────────────────────────────────────
    def test_inspect_is_read_only(self):
        """预检不许 materialize 宿主根、不许跑 node、不许写状态文件。"""
        demo = build_zip(self.root, "wp20-demo")
        t4 = build_zip(self.root, "wp20-t4", js=T4_JS)
        before = _tree(self.data)
        boom = AssertionError("预检只读：不许走安装/加载")
        with mock.patch("src.qqagent_host.materialize_host_root", side_effect=boom) as mat, \
                mock.patch("src.qqagent_host.install_market_extension", side_effect=boom) as inst, \
                mock.patch("src.extension_host.run_capability_sync", side_effect=boom) as probe:
            first = self.inspect(demo)
            second = self.inspect(t4)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["ok"])
        self.assertEqual(second.json()["tier"], "T4-host-source")
        mat.assert_not_called()
        inst.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(_tree(self.data), before)
        self.assertFalse(self.host_root.exists())
        self.assertFalse(self.state_file.exists())

    def test_inspect_missing_params(self):
        """zipPath/file 都空：inspect 返回 ok=false + message，而不是 4xx。"""
        response = self.client.post("/api/extensions/inspect-local", json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertFalse(body["ok"])
        self.assertEqual(body["problems"], ["缺少 file 或 zipPath"])
        self.assertEqual(body["message"], "缺少 file 或 zipPath")
        query = self.client.get("/api/extensions/inspect-local")
        self.assertEqual(query.json()["problems"], ["缺少 file 或 zipPath"])

    def test_inspect_success_reports_manifest(self):
        """预检把 id/name/version/文件数/层级/中文文案都摊开。"""
        demo = build_zip(self.root, "wp20-demo")
        body = self.inspect(demo).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["id"], "wp20-demo")
        self.assertEqual(body["name"], "演示包 wp20-demo")
        self.assertEqual(body["version"], "1.0.0")
        self.assertEqual(body["kind"], "plugin")
        self.assertEqual(body["files"], 2)
        self.assertGreater(body["bytes"], 0)
        self.assertEqual(body["tier"], "T1-ready")
        self.assertEqual(body["problems"], [])
        self.assertEqual(body["existing"], False)
        self.assertTrue(body["declared"]["hooks"])
        self.assertEqual(body["requested"], str(demo))
        self.assertIn("可以安装 wp20-demo", body["message"])
        self.assertIn("层级 T1-ready", body["message"])

    def test_inspect_get_accepts_query_param(self):
        """GET 也认 zipPath（GUI 走 POST，脚本/curl 走 GET）。"""
        demo = build_zip(self.root, "wp20-demo")
        body = self.client.get("/api/extensions/inspect-local",
                               params={"zipPath": str(demo)}).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["id"], "wp20-demo")

    # ── b) 安装成功 ─────────────────────────────────────────────────────
    def test_install_success_materializes_host_root(self):
        """装进宿主根 plugins/<id>，probe 真加载出工具与钩子，registry 按请求启用。"""
        demo = build_zip(self.root, "wp20-demo")
        self.assertFalse(self.host_root.exists())
        response = self.install(demo, enable=True)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["hostRoot"], str(self.host_root))
        self.assertTrue(self.plugin_dir("wp20-demo").is_dir())
        self.assertEqual(sorted(os.listdir(str(self.host_root / "plugins"))), ["wp20-demo"])
        self.assertTrue((self.plugin_dir("wp20-demo") / "plugin.json").is_file())
        self.assertTrue((self.plugin_dir("wp20-demo") / "index.js").is_file())
        self.assertEqual(self.installed_ids(), ["wp20-demo"])
        self.assertTrue(body["probe"]["ok"], body["probe"])
        self.assertIsInstance(body["probe"]["duration_ms"], int)
        self.assertEqual([one["id"] for one in body["tools"]], ["wp20-demo__ping"])
        self.assertEqual(body["hooks"], ["before-context"])
        self.assertTrue(body["enabled"])
        self.assertEqual(body["item"]["id"], "wp20-demo")
        self.assertTrue(body["item"]["enabled"])
        self.assertTrue(extension_registry.is_enabled("wp20-demo", self.data))
        self.assertTrue(self.state()["wp20-demo"]["enabled"])
        self.assertIn("并在能力桥启用", body["message"])

    def test_install_requires_confirmed(self):
        """confirmed 不是 True（含字符串 "true"）一律 400，且什么都不装。"""
        demo = build_zip(self.root, "wp20-demo")
        for extra in ({}, {"confirmed": "true"}, {"confirmed": 1}):
            body = {"zipPath": str(demo)}
            body.update(extra)
            response = self.client.post("/api/extensions/install-local", json=body)
            with self.subTest(confirmed=extra.get("confirmed")):
                self.assertEqual(response.status_code, 400)
                self.assertIn("未经确认", response.json()["detail"])
        self.assertFalse(self.host_root.exists())

    def test_install_rejects_non_bool_enable(self):
        """enable 只认 true/false；字符串 "true" 必须 400 且文案说明"布尔"。"""
        demo = build_zip(self.root, "wp20-demo")
        for value in ("true", "false", 1, 0, "1"):
            response = self.install(demo, enable=value)
            with self.subTest(enable=value):
                self.assertEqual(response.status_code, 400)
                self.assertIn("布尔", response.json()["detail"])
        self.assertFalse(self.host_root.exists())

    def test_install_defaults_to_disabled_when_enable_omitted(self):
        """不传 enable：装上但默认停用（市场安装的既有约定）。"""
        demo = build_zip(self.root, "wp20-demo")
        response = self.install(demo)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["enabled"])
        self.assertFalse(body["item"]["enabled"])
        self.assertFalse(extension_registry.is_enabled("wp20-demo", self.data))
        self.assertIn("默认停用", body["message"])

    def test_install_copies_zip_into_extension_store(self):
        """本地导入也要像链接导入一样留副本：源 zip 删掉后记录不能变成死路径。"""
        demo = build_zip(self.root, "wp20-demo")
        response = self.install(demo)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        stored = self.data / "qqagent_extensions" / "wp20-demo.zip"
        self.assertTrue(stored.is_file())
        self.assertEqual(Path(body["extension"]["path"]), stored)
        self.assertEqual(stored.read_bytes(), demo.read_bytes())
        records = json.loads((stored.parent / "extensions.json").read_text(encoding="utf-8"))
        self.assertIn("wp20-demo", [row.get("id") for row in records])

    # ── e) 同 id 已存在 + force=false ───────────────────────────────────
    def test_install_existing_without_force_rejected(self):
        """已装过且 force=false：预检 existing=true，安装 400 且不覆盖。"""
        first = build_zip(self.root, "wp20-demo")
        self.assertEqual(self.install(first, enable=True).status_code, 200)
        before_text = (self.plugin_dir("wp20-demo") / "plugin.json").read_text(encoding="utf-8")
        before_at = self.state()["wp20-demo"]["updatedAt"]

        report = self.inspect(first).json()
        self.assertTrue(report["ok"])
        self.assertTrue(report["existing"])
        self.assertTrue(report["duplicate"])
        self.assertIn("覆盖安装要 force", report["message"])

        response = self.install(first, enable=True)
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("扩展包没装进宿主运行时", detail)
        self.assertIn("已经装过", detail)
        self.assertIn("exists", detail)
        self.assertEqual((self.plugin_dir("wp20-demo") / "plugin.json").read_text(encoding="utf-8"),
                         before_text)
        self.assertEqual(self.state()["wp20-demo"]["updatedAt"], before_at)
        self.assertEqual(self.installed_ids(), ["wp20-demo"])

    # ── f) force=true 覆盖 ──────────────────────────────────────────────
    def test_install_force_overwrites(self):
        """force=true：宿主根里的文件被新版覆盖，registry updatedAt 前进。"""
        v1 = build_zip(self.root, "wp20-v1", ident="wp20-demo")
        self.assertEqual(self.install(v1, enable=True).status_code, 200)
        first_text = (self.plugin_dir("wp20-demo") / "plugin.json").read_text(encoding="utf-8")
        first_at = self.state()["wp20-demo"]["updatedAt"]
        time.sleep(1.1)          # 状态文件的时间戳只到秒，隔一秒才看得出变化

        v2 = build_zip(self.root, "wp20-v2", ident="wp20-demo",
                       manifest=_manifest("wp20-demo", version="2.0.0"),
                       js=DEMO_JS.replace("返回 pong", "返回 pong-v2"))
        response = self.install(v2, enable=True, force=True)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        manifest_after = json.loads((self.plugin_dir("wp20-demo") / "plugin.json")
                                   .read_text(encoding="utf-8"))
        self.assertEqual(manifest_after["version"], "2.0.0")
        self.assertNotEqual((self.plugin_dir("wp20-demo") / "plugin.json").read_text(encoding="utf-8"),
                            first_text)
        self.assertEqual(body["tools"][0]["description"], "返回 pong-v2")
        self.assertNotEqual(self.state()["wp20-demo"]["updatedAt"], first_at)
        self.assertEqual(self.installed_ids(), ["wp20-demo"])

    # ── g) 宿主层不再按 zip 时代判 T4：装进宿主根就能解析 ../../src ─────
    def test_host_source_package_installed_and_enabled(self):
        """包声明 host_imports，但装进宿主根后 ../../src 已能解析，允许启用。"""
        t4 = build_zip(self.root, "wp20-t4", js=T4_JS)
        response = self.install(t4, enable=True)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["enabled"])
        self.assertTrue(self.plugin_dir("wp20-t4").is_dir())
        self.assertNotEqual(body["item"]["tier"], "T4-host-source")
        self.assertEqual(body["item"]["blockers"], [])
        self.assertIn("并在能力桥启用", body["message"])
        self.assertTrue(extension_registry.is_enabled("wp20-t4", self.data))

    # ── g') 加载探测失败：装好但同样不许自动启用 ─────────────────────────
    def test_probe_failure_keeps_extension_disabled(self):
        """probe.ok=false（load-failed）时不许登记启用：否则留下每次调用都失败的白名单条目。"""
        broken = build_zip(self.root, "wp20-t6", js=BROKEN_JS)
        response = self.install(broken, enable=True)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["probe"]["ok"], body["probe"])
        self.assertIn("load-failed", json.dumps(body["probe"], ensure_ascii=False))
        self.assertTrue(self.plugin_dir("wp20-t6").is_dir())
        self.assertFalse(body["enabled"])
        self.assertFalse(body["item"]["enabled"])
        self.assertTrue(body["item"]["note"])
        self.assertIn("未启用", body["item"]["note"])
        self.assertFalse(extension_registry.is_enabled("wp20-t6", self.data))

    # ── h) 坏包 ─────────────────────────────────────────────────────────
    def test_bad_packages_rejected(self):
        """非 zip / 没有 plugin.json / 认不出 id：预检 problems 非空，install 一律 400。"""
        not_zip = self.root / "wp20-notzip.zip"
        not_zip.write_bytes(b"i am not a zip")
        no_manifest = build_zip(self.root, "wp20-nomanifest", manifest=_NO_MANIFEST)
        bad_id = self.root / "无名 包裹.zip"
        # plugin.json 没有 id，且 zip 名也不是合法 id → 认不出 id
        source = build_zip(self.root, "wp20-x", ident="noid-root",
                           manifest={"name": "没有 id", "version": "1.0.0"})
        bad_id.write_bytes(source.read_bytes())

        cases = (("not-zip", not_zip, "压缩包不是合法 zip"),
                 ("no-manifest", no_manifest, "没有 plugin.json"),
                 ("bad-id", bad_id, "无法确定合法的扩展 id"))
        for tag, path, expected in cases:
            with self.subTest(case=tag):
                report = self.inspect(path).json()
                self.assertFalse(report["ok"], report)
                self.assertTrue(report["problems"], report)
                self.assertIn(expected, report["message"])
                response = self.install(path, enable=True)
                self.assertEqual(response.status_code, 400)
                self.assertIn("本地扩展包不能安装", response.json()["detail"])
                self.assertIn(expected, response.json()["detail"])
        self.assertFalse(self.host_root.exists())

    # ── i) 路径安全 ─────────────────────────────────────────────────────
    def test_unsafe_zip_paths_rejected(self):
        """zip 里带 ../ 或绝对路径条目：预检就拒，install 400，绝不落盘。"""
        cases = (("../evil.txt", "压缩包内含可疑路径（..）"),
                 ("C:/Windows/evil.txt", "压缩包内含绝对路径"))
        for index, (entry, expected) in enumerate(cases):
            with self.subTest(entry=entry):
                path = self.root / ("wp20-trav-%d.zip" % index)
                build_zip(self.root, path.stem, entries={entry: "pwned"})
                report = self.inspect(path).json()
                self.assertFalse(report["ok"], report)
                self.assertTrue(report["problems"])
                self.assertIn(expected, report["message"])
                self.assertEqual(report["id"], "")
                response = self.install(path, enable=True)
                self.assertEqual(response.status_code, 400)
                self.assertIn("本地扩展包不能安装", response.json()["detail"])
                self.assertIn(expected, response.json()["detail"])
        self.assertFalse(self.host_root.exists())
        self.assertFalse((self.host_root / "plugins").exists())

    def test_missing_zip_file_rejected(self):
        """给了路径但文件不存在：预检 problems 说清楚，install 400。"""
        missing = self.root / "nope.zip"
        report = self.inspect(missing).json()
        self.assertFalse(report["ok"])
        self.assertTrue(report["problems"][0].startswith("扩展包不存在"))
        response = self.install(missing, enable=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("扩展包不存在", response.json()["detail"])

    # ── j) 缺参数 ───────────────────────────────────────────────────────
    def test_install_missing_params(self):
        """install 缺 zipPath/file（或只有空白）→ 400 缺少 file 或 zipPath。"""
        for body in ({"confirmed": True}, {"zipPath": "   ", "confirmed": True},
                     {"file": "", "confirmed": True}):
            response = self.client.post("/api/extensions/install-local", json=body)
            with self.subTest(body=body):
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["detail"], "缺少 file 或 zipPath")
        self.assertFalse(self.host_root.exists())


if __name__ == "__main__":
    unittest.main()
