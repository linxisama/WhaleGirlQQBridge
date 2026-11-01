# -*- coding: utf-8 -*-
"""宿主根准备 / 市场安装落进宿主根 / 三条端点的测试。

全程临时目录 + env 隔离（不起真实运行时、不碰仓库 data/ 与用户目录）。
真加载那几条会真跑 node：没有 node 就 skip，不当失败。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from src import extension_registry, qqagent_host

REPO = Path(__file__).resolve().parents[1]
PAYLOAD = REPO / "runtime" / "qqagent-host"
REAL_ZIP = REPO / ".tmp" / "external-market" / "account-appearance.zip"


def _has_node() -> bool:
    return qqagent_host.node_binary() is not None


def _isolated_env(case, root: Path):
    """把两个来源环境变量摘掉，兼容层指向不存在的目录：只留仓库自带的载荷。"""
    patcher = mock.patch.dict(os.environ, {
        "QQAGENT_COMPAT_DIR": str(root / "no-compat"),
    })
    patcher.start()
    case.addCleanup(patcher.stop)
    os.environ.pop("QQAGENT_HOST_RUNTIME", None)


def _bad_entry_zip(path: Path) -> Path:
    """manifest 合法、entry 指向不存在的文件：装得上，加载必定失败。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"id": "wp10-broken", "name": "坏入口扩展", "version": "1.0.0",
                "entry": "missing-entry.js"}
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False))
    return path


class MaterializeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        _isolated_env(self, self.root)

    def test_first_call_copies_then_second_call_reuses(self):
        first = qqagent_host.materialize_host_root(self.data)
        self.assertTrue(first["ok"], first.get("error"))
        self.assertFalse(first["reused"], "第一次必须是真复制")
        self.assertGreater(first["copied"], 0)
        self.assertEqual(first["source"], str(PAYLOAD))
        root = Path(first["root"])
        self.assertEqual(root, qqagent_host.default_host_root(self.data))
        self.assertTrue((root / "src" / "config.js").is_file())
        self.assertTrue((root / "plugins").is_dir())
        self.assertGreater(first["files"], 200)
        self.assertTrue(first["smoke"]["ok"], first["smoke"])

        second = qqagent_host.materialize_host_root(self.data)
        self.assertTrue(second["ok"], second.get("error"))
        self.assertTrue(second["reused"], "已有可用根必须复用")
        self.assertEqual(second["copied"], 0, "复用不许再搬 3.9MB")
        self.assertEqual(second["root"], first["root"])

    def test_no_source_anywhere_reports_source_missing(self):
        with mock.patch("src.qqagent_host.runtime_source_candidates", return_value=[]), \
                mock.patch("src.qqagent_host.resolve_host_root", return_value=None):
            result = qqagent_host.materialize_host_root(self.data)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "source-missing")
        self.assertEqual(result["copied"], 0)
        self.assertFalse(Path(result["root"]).exists(), "失败时不许留下半个根")

    def test_broken_existing_root_is_rebuilt(self):
        broken = qqagent_host.default_host_root(self.data)
        (broken / "src").mkdir(parents=True)
        (broken / "src" / "config.js").write_text("// broken\n", encoding="utf-8")
        self.assertFalse(qqagent_host.host_root_ready(broken))
        result = qqagent_host.materialize_host_root(self.data)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertFalse(result["reused"], "坏根不能当可用根复用")
        self.assertNotEqual(
            (broken / "src" / "config.js").read_text(encoding="utf-8"), "// broken\n")
        self.assertTrue(qqagent_host.host_root_ready(broken))


class InstallIntoHostTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        _isolated_env(self, self.root)

    def test_real_market_zip_installs_and_probes(self):
        if not REAL_ZIP.is_file():
            self.skipTest("缺少市场 zip 样本：%s" % REAL_ZIP)
        if not _has_node():
            self.skipTest("没有 node，真加载探测跑不了")
        result = qqagent_host.install_market_extension(self.data, REAL_ZIP)
        tools = [item["id"] for item in result["tools"]]
        print("\n[真机] install_market_extension(%s)" % REAL_ZIP.name)
        print("[真机] hostRoot=%s copied=%s smoke.exports=%s"
              % (result["hostRoot"], result["copied"],
                 (result.get("smoke") or {}).get("exports")))
        print("[真机] probe=%s tools=%s hooks=%s 总耗时 %sms"
              % (result["probe"].get("ok"), tools, result["hooks"],
                 result["duration_ms"]))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["id"], "account-appearance")
        root = Path(result["hostRoot"])
        self.assertEqual(root, qqagent_host.default_host_root(self.data))
        self.assertEqual(result["path"], str(root / "plugins" / "account-appearance"))
        self.assertTrue((root / "plugins" / "account-appearance" / "plugin.json").is_file())
        self.assertTrue(result["probe"]["ok"], result["probe"].get("error"))
        self.assertTrue(tools, "装完真加载必须报出工具")
        self.assertTrue(all(name.startswith("account-appearance__") for name in tools), tools)
        self.assertEqual([row["id"] for row in qqagent_host.list_installed(root)],
                         ["account-appearance"])

    def test_install_stays_ok_when_probe_fails(self):
        if not _has_node():
            self.skipTest("没有 node，真加载探测跑不了")
        broken = _bad_entry_zip(self.root / "wp10-broken.zip")
        result = qqagent_host.install_market_extension(self.data, broken)
        print("\n[真机] 坏入口 zip：ok=%s probe.ok=%s error=%s"
              % (result["ok"], result["probe"].get("ok"),
                 result["probe"].get("error") or result["probe"].get("loadError")))
        self.assertTrue(result["ok"], "装上了就是装上了：%s" % (result.get("error"),))
        self.assertEqual(result["id"], "wp10-broken")
        self.assertTrue(Path(result["path"]).is_dir())
        self.assertFalse(result["probe"]["ok"], "入口不存在，加载不可能成功")
        self.assertEqual(result["tools"], [], "加载失败不许报工具")
        reason = str(result["probe"].get("error") or result["probe"].get("loadError") or "")
        self.assertTrue(reason, "失败必须说清原因")


class HostEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        (self.data / "qq-only").mkdir(parents=True)
        self.manifest = self.root / "manifest"
        self.manifest.mkdir()
        (self.manifest / "all-plugins-v4.json").write_text("[]", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "QQAGENT_MANIFEST_DIR": str(self.manifest),
            "QQAGENT_COMPAT_DIR": str(self.root / "no-compat"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("QQAGENT_HOST_RUNTIME", None)
        extension_registry.invalidate()
        self.addCleanup(extension_registry.invalidate)
        self.manager = SimpleNamespace(data_dir=self.data,
                                       custom_plugins_dir=self.data / "qq_plugins",
                                       _plugins={})

    def _client(self):
        from src.qq_app import create_app

        app = create_app()
        app.state.bot = SimpleNamespace(plugins=self.manager)
        return TestClient(app)

    def test_host_status_is_read_only(self):
        body = self._client().get("/api/extensions/host").json()
        self.assertTrue(body["success"])
        self.assertFalse(body["ok"], "还没准备，ok 必须是 false")
        self.assertFalse(body["prepared"])
        self.assertFalse(body["exists"])
        self.assertEqual(body["root"], str(qqagent_host.default_host_root(self.data)))
        self.assertEqual(body["source"], str(PAYLOAD))
        self.assertIn(str(PAYLOAD), body["candidates"])
        self.assertEqual(body["smoke"], {}, "状态查询不许顺手起 node")
        self.assertFalse(Path(body["root"]).exists(), "GET 不许复制 4MB")

    def test_prepare_endpoint_copies_then_reuses(self):
        if not _has_node():
            self.skipTest("没有 node，prepare 的冒烟跑不了")
        client = self._client()
        first = client.post("/api/extensions/host/prepare", json={})
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertTrue(body["prepared"])
        self.assertFalse(body["reused"])
        self.assertGreater(body["copied"], 0)
        self.assertTrue(body["smoke"]["ok"], body["smoke"])
        self.assertIsInstance(body["duration_ms"], int)
        self.assertTrue((Path(body["root"]) / "src" / "config.js").is_file())

        second = client.post("/api/extensions/host/prepare", json={}).json()
        self.assertTrue(second["reused"])
        self.assertEqual(second["copied"], 0)

        status = client.get("/api/extensions/host").json()
        self.assertTrue(status["ok"])
        self.assertTrue(status["prepared"])
        self.assertGreater(status["files"], 200)

    def test_prepare_failure_maps_to_chinese_error(self):
        with mock.patch("src.qqagent_host.runtime_source_candidates", return_value=[]), \
                mock.patch("src.qqagent_host.resolve_host_root", return_value=None):
            response = self._client().post("/api/extensions/host/prepare", json={})
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn("宿主运行时来源", detail)
        self.assertIn("source-missing", detail)

    def test_install_endpoint_lands_in_host_root(self):
        if not REAL_ZIP.is_file():
            self.skipTest("缺少市场 zip 样本：%s" % REAL_ZIP)
        if not _has_node():
            self.skipTest("没有 node，真加载探测跑不了")
        client = self._client()
        installed = {"id": "account-appearance", "path": str(REAL_ZIP)}
        with mock.patch("src.qqagent_market.load_catalog",
                        return_value={"items": [{"id": "account-appearance"}]}), \
                mock.patch("src.qqagent_market.install_extension", return_value=installed):
            response = client.post("/api/extensions/install",
                                   json={"id": "account-appearance", "confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        root = Path(body["hostRoot"])
        self.assertEqual(root, qqagent_host.default_host_root(self.data))
        self.assertTrue((root / "plugins" / "account-appearance").is_dir())
        self.assertTrue(body["probe"]["ok"], body["probe"])
        self.assertTrue(body["tools"])
        self.assertIn("宿主已加载", body["message"])

        scan = client.get("/api/extensions").json()
        self.assertTrue(scan["summary"]["hostRoot"]["ready"])
        self.assertEqual(scan["summary"]["hostRoot"]["root"], str(root))
        row = next(item for item in scan["items"] if item["id"] == "account-appearance")
        self.assertEqual(row["path"], str(root / "plugins" / "account-appearance"),
                         "清单里的 path 必须是真安装目录，不是 zip")
        self.assertEqual(row["tools"], [item["id"] for item in body["tools"]],
                         "工具名要来自宿主那次真加载，不是 plugin.json 里的猜测")
        self.assertEqual(row["hooks"], body["hooks"])


# ── WP10b：运行时指纹（stale 检测 / 刷新载荷） ───────────────────────────────
def _fake_source(root: Path, script: str = "export const ok = 1;\n") -> Path:
    """一个能当宿主根用的小载荷：判定文件 + package.json + node_modules + 两个脚本。

    用它就不必真复制 4MB，也不必改仓库里的 runtime/qqagent-host（那是别人的写域）。
    """
    base = root / "fake-host"
    (base / "src").mkdir(parents=True, exist_ok=True)
    (base / "src" / "config.js").write_text("export const ok = 1;\n", encoding="utf-8")
    (base / "src" / "plugin-loader.js").write_text("export const load = 1;\n", encoding="utf-8")
    (base / "package.json").write_text('{"name": "fake-host", "version": "0.4.0"}\n',
                                       encoding="utf-8")
    (base / "node_modules" / "dep").mkdir(parents=True, exist_ok=True)
    (base / "node_modules" / "dep" / "index.js").write_text("module.exports = {};\n",
                                                            encoding="utf-8")
    (base / "host-one.mjs").write_text(script, encoding="utf-8")
    (base / "host.mjs").write_text("// host manifest\n", encoding="utf-8")
    return base


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RuntimeStampTests(unittest.TestCase):
    """指纹一致就复用、不一致就刷新；刷新绝不碰 plugins/ 下已装扩展。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        _isolated_env(self, self.root)
        self.source = _fake_source(self.root)
        self.host = qqagent_host.default_host_root(self.data)

    def _materialize(self, **kwargs):
        return qqagent_host.materialize_host_root(self.data, extra_source=self.source,
                                                  smoke=False, **kwargs)

    def test_matching_stamp_reuses_without_copy(self):
        first = self._materialize()
        self.assertTrue(first["ok"], first.get("error"))
        self.assertFalse(first["reused"], "第一次必须是真复制")
        self.assertGreater(first["copied"], 0)
        self.assertFalse(first["refreshed"])
        stamp_path = self.host / qqagent_host.STAMP_NAME
        self.assertTrue(stamp_path.is_file(), "制备完必须落指纹")
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        self.assertEqual(stamp["hashes"]["host-one.mjs"], _sha256(self.source / "host-one.mjs"))
        print("\n[真机] 指纹 files=%s bytes=%s hashes=%s"
              % (stamp["count"], stamp["bytes"], sorted(stamp["hashes"])))

        second = self._materialize()
        self.assertTrue(second["reused"], "指纹一致必须复用")
        self.assertEqual(second["copied"], 0, "复用不许再搬载荷")
        self.assertEqual(second["refreshedFiles"], [])
        self.assertFalse(second["stampChanged"])
        self.assertEqual(second["stamp"], stamp)

    def test_changed_script_refreshes_and_keeps_installed_extensions(self):
        self._materialize()
        fake_ext = self.host / "plugins" / "fake-ext"
        fake_ext.mkdir(parents=True)
        (fake_ext / "plugin.json").write_text('{"id": "fake-ext"}\n', encoding="utf-8")
        before = sorted(item.name for item in (self.host / "plugins").iterdir())
        self.assertEqual(before, ["fake-ext"])

        (self.source / "host-one.mjs").write_text("// old loader: tool.execute(args, ctx)\n",
                                                  encoding="utf-8")
        result = self._materialize()
        print("\n[真机] stale 刷新 reused=%s refreshed=%s stampChanged=%s changed=%s 重写 %d 个"
              % (result["reused"], result["refreshed"], result["stampChanged"],
                 result["stampChanges"], len(result["refreshedFiles"])))
        self.assertFalse(result["reused"], "载荷变了不许复用")
        self.assertTrue(result["refreshed"])
        self.assertTrue(result["stampChanged"])
        self.assertEqual(result["stampChanges"], ["host-one.mjs"], result["stampChanges"])
        self.assertIn("host-one.mjs", result["refreshedFiles"])
        self.assertEqual(_sha256(self.host / "host-one.mjs"),
                         _sha256(self.source / "host-one.mjs"), "根内脚本必须等于源")
        after = sorted(item.name for item in (self.host / "plugins").iterdir())
        self.assertEqual(after, before, "刷新不许删/覆盖已装扩展")
        self.assertTrue((fake_ext / "plugin.json").is_file())

        again = self._materialize()
        self.assertTrue(again["reused"], "刷新完第二次必须幂等复用")
        self.assertEqual(again["copied"], 0)
        self.assertFalse(again["refreshed"])

    def test_history_root_without_stamp_refreshes_once(self):
        self._materialize()
        stamp_path = self.host / qqagent_host.STAMP_NAME
        stamp_path.unlink()
        (self.host / "host-one.mjs").write_text("// 安装那天的旧脚本\n", encoding="utf-8")
        result = self._materialize()
        self.assertFalse(result["reused"], "无指纹的历史根必须当 stale")
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["previousStamp"], {})
        self.assertEqual(result["stampChanges"], ["(无旧指纹)"])
        self.assertTrue(stamp_path.is_file(), "刷新后必须写指纹")
        self.assertEqual(_sha256(self.host / "host-one.mjs"),
                         _sha256(self.source / "host-one.mjs"))
        self.assertTrue(self._materialize()["reused"], "写进指纹后下一次复用")

    def test_corrupt_stamp_is_stale_not_crash(self):
        self._materialize()
        stamp_path = self.host / qqagent_host.STAMP_NAME
        stamp_path.write_text("{坏掉的 json", encoding="utf-8")
        self.assertEqual(qqagent_host.runtime_stamp(self.host), {})
        result = self._materialize()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["refreshed"], "坏指纹按 stale 处理")
        self.assertTrue(result["stampChanged"])
        json.loads(stamp_path.read_text(encoding="utf-8"))


class BridgeSyncPatchTests(unittest.TestCase):
    """sync 写进桥的四个路径键必须以宿主根为准，不再指只有 zip 的旧目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        (self.data / "qq-only").mkdir(parents=True)
        self.manifest = self.root / "manifest"
        self.manifest.mkdir()
        (self.manifest / "all-plugins-v4.json").write_text("[]", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "QQAGENT_MANIFEST_DIR": str(self.manifest),
            "QQAGENT_COMPAT_DIR": str(self.root / "no-compat"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("QQAGENT_HOST_RUNTIME", None)
        extension_registry.invalidate()
        self.addCleanup(extension_registry.invalidate)
        self.written = {}
        self.manager = SimpleNamespace(
            data_dir=self.data, custom_plugins_dir=self.data / "qq_plugins",
            _plugins={"qqagent_bridge": object()},
            update_settings=lambda pid, patch: self.written.update(patch),
            get_settings=lambda pid: dict(self.written))

    def _client(self):
        from src.qq_app import create_app

        app = create_app()
        app.state.bot = SimpleNamespace(plugins=self.manager, owner_ids=lambda: {"10001"})
        return TestClient(app)

    def _post_sync(self):
        catalog = {"path": str(self.root / "catalog.json"), "count": 0, "providers": []}
        with mock.patch("src.qqagent_market.build_bridge_catalog", return_value=catalog), \
                mock.patch("src.qqagent_market.find_runtime", return_value=("", "")):
            return self._client().post("/api/qqagent/bridge/sync")

    def test_patch_keys_are_real_settings(self):
        """sync 写的每个键都必须在插件 schema 里：2026-10 实测 5.2.59 装好的控制台点同步直接 400
        “qqagent_bridge: unknown setting field 'hostRoot'”，能力桥因此永远配不起来。"""
        from src.plugins.qqagent_bridge import QqAgentBridge

        patch = self._post_sync().json()["patch"]
        schema = QqAgentBridge.META.config_schema
        defaults = QqAgentBridge.META.default_settings
        for key in patch:
            self.assertIn(key, schema, "qqagent_bridge 不认 '%s'，/api/qqagent/bridge/sync 会 400" % key)
            self.assertIn(key, defaults, "'%s' 有 schema 但无默认值" % key)

    def test_patch_points_at_host_root_plugins(self):
        response = self._post_sync()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        patch = body["patch"]
        host = qqagent_host.default_host_root(self.data)
        print("\n[真机] sync patch extensionsDir=%s hostRoot=%s hostScript=%s prepared=%s"
              % (patch["extensionsDir"], patch["hostRoot"], patch["hostScript"],
                 body["prepared"]))
        self.assertEqual(patch["extensionsDir"], str(host / "plugins"))
        self.assertEqual(patch["hostRoot"], str(host))
        self.assertEqual(patch["sourceDir"], str(host))
        self.assertEqual(patch["hostScript"], str(host / "host-one.mjs"))
        for key in ("hostRoot", "sourceDir", "hostScript", "extensionsDir"):
            self.assertIn(key, patch, "桥认这四个键，缺一个就退化成猜路径")
            self.assertTrue(patch[key], key)
        self.assertTrue((host / "src" / "config.js").is_file(), "sync 应先把宿主根备好")
        self.assertTrue(Path(patch["extensionsDir"]).is_dir())
        self.assertTrue(Path(patch["hostScript"]).is_file())
        self.assertTrue(body["prepared"])
        self.assertEqual(body["note"], "")
        self.assertEqual(body["missing"], [])
        self.assertEqual(self.written, patch, "响应里的 patch 必须是真写进桥的那份")
        self.assertEqual(body["settings"]["extensionsDir"], patch["extensionsDir"])
        for key in ("catalog", "hostScript", "sourceDir", "missing", "settings"):
            self.assertIn(key, body, "旧字段只加不改")

    def test_stale_bundled_node_path_is_replaced(self):
        stale = str(self.root / "old" / "_internal" / "runtime" / "node" / "node.exe")
        self.written["nodeExe"] = stale
        response = self._post_sync()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["nodeReady"])
        self.assertNotIn("Node 20+", body["missing"])
        self.assertIn("已失效", body["note"])
        self.assertNotEqual(body["patch"]["nodeExe"], stale)
        self.assertTrue(Path(body["patch"]["nodeExe"]).is_file())

    def test_sync_keeps_user_max_profiles(self):
        """同步只该改路径：用户把「自动选择上限」调到 4 之后点同步，不能掉回 2 ——
        掉回 2 的后果是第 3、4 个扩展静默失效，界面还显示已启用。"""
        self.written = {"maxProfiles": 4}
        body = self._post_sync().json()
        self.assertEqual(body["patch"]["maxProfiles"], 4)

    def test_sync_max_profiles_defaults_and_clamps(self):
        body = self._post_sync().json()
        self.assertEqual(body["patch"]["maxProfiles"], 2, "没设过就是默认 2")
        # 0/坏值当没设（默认 2），越界夹进 1..8，字符串数字照收。
        for messy, expect in ((99, 8), (0, 2), ("x", 2), ("3", 3), (-3, 1)):
            self.written = {"maxProfiles": messy}
            body = self._post_sync().json()
            self.assertEqual(body["patch"]["maxProfiles"], expect,
                             "maxProfiles=%r 应归到 %d" % (messy, expect))

    def test_sync_raises_max_profiles_to_cover_enabled_extensions(self):
        """6 个扩展已启用、上限还停在 4：同步必须抬到 6，否则第 5、6 个静默不装载。"""
        self.written = {"maxProfiles": 4}
        for name in ("ext-a", "ext-b", "ext-c", "ext-d", "ext-e", "ext-f"):
            extension_registry.set_enabled(name, True, data_dir=self.data)
        body = self._post_sync().json()
        self.assertEqual(body["patch"]["maxProfiles"], 6)

    def test_sync_max_profiles_stays_clamped_at_eight(self):
        """启用数再多也不能越过 schema 上限 8。"""
        for index in range(9):
            extension_registry.set_enabled("ext-%d" % index, True, data_dir=self.data)
        body = self._post_sync().json()
        self.assertEqual(body["patch"]["maxProfiles"], 8)

    def test_unprepared_host_root_says_so_in_chinese(self):
        host = qqagent_host.default_host_root(self.data)
        with mock.patch("src.qqagent_host.runtime_source_candidates", return_value=[]), \
                mock.patch("src.qqagent_host.resolve_host_root", return_value=None):
            response = self._post_sync()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["prepared"])
        self.assertIn("宿主根未准备", body["note"])
        self.assertIn("source-missing", body["note"])
        self.assertIn("QQ Agent 0.4 源码目录", body["missing"])
        self.assertEqual(body["patch"]["extensionsDir"], str(host / "plugins"),
                         "备不出来也不许写回只有 zip 的旧目录")

def _owner_zip(path: Path) -> Path:
    """最小「主人识别」式扩展：声明 settings.ids，没填号码就 available()=false。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"id": "demo-owner", "name": "主人识别示例", "version": "1.0.0",
                "apiVersion": 1, "entry": "index.js", "enabledByDefault": False,
                "capabilities": ["message.owner-check"], "settings": {"ids": ""}}
    index = "\n".join([
        "let cfg = () => ({});",
        "export function setup(api) { cfg = api.config; }",
        "export function available() {",
        "  return cfg().ids ? { ok: true } : { ok: false, reason: '还没填主人 QQ 号' };",
        "}",
        "export const providers = { 'message.owner-check': () => ({ isOwner: false }) };",
        "",
    ])
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False))
        handle.writestr("index.js", index)
    return path


class HostOwnerSeedTests(unittest.TestCase):
    """市场扩展的配置要落在宿主自己的 config.json：不填主人号码它就拒绝加载。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        _isolated_env(self, self.root)

    def test_install_without_owner_ids_reports_the_reason(self):
        if not _has_node():
            self.skipTest("没有 node，跳过真加载")
        zip_path = _owner_zip(self.root / "market" / "demo-owner.zip")
        result = qqagent_host.install_market_extension(
            self.data, zip_path=zip_path, extension_id="demo-owner")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertFalse(result["probe"]["ok"])
        self.assertIn("还没填主人 QQ 号", str(result["probe"]["error"].get("message")))

    def test_install_seeds_owner_ids_before_probe(self):
        if not _has_node():
            self.skipTest("没有 node，跳过真加载")
        zip_path = _owner_zip(self.root / "market" / "demo-owner.zip")
        result = qqagent_host.install_market_extension(
            self.data, zip_path=zip_path, extension_id="demo-owner", owner_ids=["10001"])
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["probe"]["ok"], result["probe"].get("error"))
        self.assertEqual(result["ownerSeed"]["written"], {"ids": "10001"})
        config = qqagent_host.read_host_config(Path(result["hostRoot"]))
        self.assertEqual(config["skills"]["demo-owner"],
                         {"enabled": True, "ids": "10001"})

    def test_existing_extension_is_reused_and_force_replaces_it(self):
        if not _has_node():
            self.skipTest("没有 node，跳过真加载")
        zip_path = _owner_zip(self.root / "market" / "demo-owner.zip")
        first = qqagent_host.install_market_extension(
            self.data, zip_path=zip_path, extension_id="demo-owner", owner_ids=["10001"])
        self.assertTrue(first["ok"], first.get("error"))
        marker = Path(first["path"]) / "keep.txt"
        marker.write_text("用户改过的文件", encoding="utf-8")

        again = qqagent_host.install_market_extension(
            self.data, zip_path=zip_path, extension_id="demo-owner", owner_ids=["10001"])
        self.assertTrue(again["ok"], again.get("error"))
        self.assertTrue(again["alreadyInstalled"])
        self.assertTrue(marker.is_file(), "未传 force 时不许覆盖已有扩展目录")
        self.assertTrue(again["probe"]["ok"], again["probe"].get("error"))

        replaced = qqagent_host.install_market_extension(
            self.data, zip_path=zip_path, extension_id="demo-owner", force=True,
            owner_ids=["10001"])
        self.assertTrue(replaced["ok"], replaced.get("error"))
        self.assertFalse(replaced["alreadyInstalled"])
        self.assertFalse(marker.exists(), "force=True 才是覆盖重装")


class RegistryMirrorTests(unittest.TestCase):
    """启用态的权威存储是 extension_registry，但宿主只读自己的 config.json。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "data" / "qq-only"
        self.data.mkdir(parents=True)

    def _host_config(self):
        root = qqagent_host.default_host_root(self.data)
        return qqagent_host.read_host_config(root)["skills"]["demo"]

    def test_set_enabled_mirrors_into_host_config(self):
        extension_registry.set_enabled("demo", True, self.data)
        self.assertIs(self._host_config()["enabled"], True)
        extension_registry.set_enabled("demo", False, self.data)
        self.assertIs(self._host_config()["enabled"], False)

    def test_set_many_mirrors_each_id(self):
        extension_registry.set_many(["demo", "demo2"], True, self.data)
        root = qqagent_host.default_host_root(self.data)
        skills = qqagent_host.read_host_config(root)["skills"]
        self.assertIs(skills["demo"]["enabled"], True)
        self.assertIs(skills["demo2"]["enabled"], True)


class ProbeDisabledFlagTests(unittest.TestCase):
    """探测的启用态必须以宿主 config.json 为准，不能只看清单 enabledByDefault。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "host"

    def test_host_switch_overrides_manifest_default_off(self):
        manifest = {"enabledByDefault": False}
        self.assertTrue(qqagent_host._disabled_flags(manifest)["installedButDisabled"])
        self.assertFalse(qqagent_host._disabled_flags(
            manifest, loaded=True, providers=["message.owner-check"],
            host_enabled=True)["installedButDisabled"])

    def test_host_skill_enabled_reads_config_not_only_manifest(self):
        manifest = {"enabledByDefault": False}
        qqagent_host.set_extension_enabled(self.root, "owner-identity", True)
        self.assertTrue(qqagent_host._host_skill_enabled(
            self.root, "owner-identity", manifest))
        qqagent_host.set_extension_enabled(self.root, "owner-identity", False)
        self.assertFalse(qqagent_host._host_skill_enabled(
            self.root, "owner-identity", manifest))


if __name__ == "__main__":
    unittest.main()
