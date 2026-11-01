"""宿主根已装扩展并进桥 catalog 的离线测试（零 node、不写宿主根）。"""
import asyncio
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import extension_host, qqagent_host, qqagent_market
from src.plugins import qqagent_bridge


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _host_root(data_dir: Path) -> Path:
    """宿主根（plugins 目录一起建好，catalog 只读、不会自己建）。"""
    root = data_dir / "qq-only" / "qqagent_host"
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    return root


def _host_plugin(root: Path, folder: str, manifest=None, probes=None, skill=None) -> Path:
    path = root / "plugins" / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.js").write_text("export const hooks = {};\n", encoding="utf-8")
    if manifest is not None:
        _write_json(path / "plugin.json", manifest)
    if skill is not None:
        _write_json(path / "skill.json", skill)
    if probes is not None:
        _write_json(root / qqagent_host.PROBE_CACHE_NAME,
                    {"version": 1, "extensions": probes})
    return path


def _zip_bytes(ident: str = "demo", capabilities=("message.owner-check",)) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{ident}/plugin.json", json.dumps({
            "id": ident, "name": "演示扩展", "version": "1.2.3",
            "description": "测试用", "capabilities": list(capabilities),
        }, ensure_ascii=False))
        archive.writestr(f"{ident}/index.js", "export const hooks = {};\n")
    return buffer.getvalue()


class _StreamResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield self._payload


class _Client:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url):
        return _StreamResponse(self.payload)


def _install_zip(data_dir: Path, ident: str, capabilities=("message.owner-check",)) -> dict:
    item = {"id": ident, "name": "演示扩展",
            "downloadUrl": f"/api/community/plugins/{ident}/download"}
    with mock.patch.object(qqagent_market.httpx, "Client",
                           return_value=_Client(_zip_bytes(ident, capabilities))):
        return qqagent_market.install_extension(data_dir, item)


def _read(entries: list, ident: str, source: str) -> dict:
    for entry in entries:
        if entry.get("id") == ident and entry.get("source") == source:
            return entry
    raise AssertionError(f"catalog 缺 {ident}/{source}：{entries}")


def _snapshot(folder: Path) -> list:
    """目录树快照：相对路径 + 是否目录 + 大小，用来证明构建没写盘。"""
    result = []
    for item in sorted(folder.rglob("*")):
        size = item.stat().st_size if item.is_file() else 0
        result.append((str(item.relative_to(folder)), item.is_dir(), size))
    return result


class HostCatalogTests(unittest.TestCase):
    def test_probe_values_beat_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = _host_root(data_dir)
            path = _host_plugin(root, "demo-host", {
                "id": "demo-host", "name": "演示宿主扩展", "version": "2.0.0",
                "description": "宿主根里那份", "capabilities": ["tool.guard"],
                "tools": ["ping"], "hooks": ["on-message"],
            }, probes={"demo-host": {
                "ok": True, "tools": ["demo-host__ping"],
                "hooks": ["before-context"],
                "providers": ["account.appearance", "tool.guard"],
            }})
            info = qqagent_market.build_bridge_catalog(data_dir)
            self.assertEqual(info["count"], 1)
            self.assertEqual(info["hostCount"], 1)
            self.assertEqual(info["zipCount"], 0)
            self.assertEqual(info["hostProviders"], ["account.appearance", "tool.guard"])
            entry = _read(json.loads(Path(info["path"]).read_text(encoding="utf-8"))["plugins"],
                          "demo-host", "host")
            self.assertEqual(entry["path"], str(path))
            self.assertEqual(entry["name"], "演示宿主扩展")
            self.assertEqual(entry["version"], "2.0.0")
            self.assertEqual(entry["description"], "宿主根里那份")
            self.assertEqual(entry["kind"], "plugin")
            # 实测值优先：清单里的 ping / on-message 被 probes.json 覆盖。
            self.assertEqual(entry["tools"], ["demo-host__ping"])
            self.assertEqual(entry["hooks"], ["before-context"])
            self.assertEqual(entry["providers"], ["account.appearance", "tool.guard"])
            # file 指扩展目录、capabilities 是白名单：能力桥的自动发现全靠这两个键。
            self.assertEqual(entry["file"], str(path))
            self.assertEqual(entry["capabilities"], ["tool.guard", "account.appearance"])

    def test_manifest_values_used_without_probe_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = _host_root(data_dir)
            _host_plugin(root, "static-only", {
                "id": "static-only", "name": "静态扩展", "version": "0.3.1",
                "capabilities": ["tool.guard", "account.appearance"],
                "hooks": ["on-message"],
            })
            info = qqagent_market.build_bridge_catalog(data_dir)
            entry = _read(json.loads(Path(info["path"]).read_text(encoding="utf-8"))["plugins"],
                          "static-only", "host")
            self.assertEqual(entry["tools"], ["guard"])
            self.assertEqual(entry["hooks"], ["on-message"])
            self.assertEqual(entry["providers"], ["account.appearance"])
            self.assertEqual(entry["description"], "")

    def test_id_falls_back_to_directory_then_skill_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = _host_root(data_dir)
            _host_plugin(root, "plain-dir")
            _host_plugin(root, "skill-ext", skill={
                "id": "skill-ext-id", "name": "技能扩展", "version": "0.9.0"})
            info = qqagent_market.build_bridge_catalog(data_dir)
            plugins = json.loads(Path(info["path"]).read_text(encoding="utf-8"))["plugins"]
            self.assertEqual([entry["id"] for entry in plugins],
                             ["plain-dir", "skill-ext-id"])
            skill = _read(plugins, "skill-ext-id", "host")
            self.assertEqual(skill["name"], "技能扩展")
            self.assertEqual(skill["version"], "0.9.0")

    def test_same_id_host_entry_wins_and_zip_is_marked(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = _host_root(data_dir)
            host_path = _host_plugin(root, "demo", {
                "id": "demo", "name": "演示宿主扩展", "version": "2.0.0",
                "capabilities": ["account.appearance"]},
                probes={"demo": {"ok": True, "tools": ["demo__ping"],
                                 "hooks": ["before-context"],
                                 "providers": ["account.appearance"]}})
            record = _install_zip(data_dir, "demo")
            info = qqagent_market.build_bridge_catalog(data_dir)
            plugins = json.loads(Path(info["path"]).read_text(encoding="utf-8"))["plugins"]
            self.assertEqual([(entry["id"], entry["source"]) for entry in plugins],
                             [("demo", "host"), ("demo", "zip")])
            self.assertEqual(info["count"], 2)
            self.assertEqual(info["hostCount"], 1)
            self.assertEqual(info["zipCount"], 1)
            host = _read(plugins, "demo", "host")
            zipped = _read(plugins, "demo", "zip")
            self.assertNotIn("shadowed", host)
            self.assertEqual(zipped["shadowed"], True)
            # 字段不打架：宿主是真目录 + 实测元数据，zip 是原包 + 声明 capabilities。
            self.assertEqual(host["path"], str(host_path))
            # 宿主条目同样带 file（目录）与 capabilities：桥的自动发现只认这两个键。
            self.assertEqual(host["file"], str(host_path))
            self.assertEqual(host["capabilities"], ["account.appearance"])
            self.assertEqual(zipped["file"], record["path"])
            self.assertEqual(zipped["capabilities"], ["message.owner-check"])
            self.assertNotIn("path", zipped)
            self.assertEqual(zipped["tools"], [])

    def test_missing_host_root_matches_zip_only_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            record = _install_zip(data_dir, "demo")
            before = _snapshot(data_dir)
            info = qqagent_market.build_bridge_catalog(data_dir)
            plugins = json.loads(Path(info["path"]).read_text(encoding="utf-8"))["plugins"]
            self.assertEqual(plugins, [{
                "id": "demo", "name": "演示扩展", "kind": "plugin",
                "file": record["path"], "capabilities": ["message.owner-check"],
                "tools": [], "source": "zip",
            }])
            self.assertEqual(set(plugins[0]), {"id", "name", "kind", "file",
                                              "capabilities", "tools", "source"})
            self.assertEqual(info["hostCount"], 0)
            self.assertEqual(info["zipCount"], 1)
            self.assertEqual(info["hostProviders"], [])
            self.assertFalse((data_dir / "qq-only").exists())
            added = [item for item in _snapshot(data_dir) if item not in before]
            self.assertEqual(added, [("qqagent_bridge_catalog.json", False,
                                      (data_dir / "qqagent_bridge_catalog.json").stat().st_size)])

    def test_host_root_without_plugins_dir_is_skipped_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = data_dir / "qq-only" / "qqagent_host"
            root.mkdir(parents=True)
            before = _snapshot(data_dir)
            info = qqagent_market.build_bridge_catalog(data_dir)
            self.assertEqual(info["count"], 0)
            self.assertEqual(info["hostCount"], 0)
            self.assertFalse((root / "plugins").exists())
            self.assertEqual([item for item in _snapshot(data_dir) if item not in before],
                             [("qqagent_bridge_catalog.json", False,
                               (data_dir / "qqagent_bridge_catalog.json").stat().st_size)])

    def test_include_host_false_keeps_zip_only(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = _host_root(data_dir)
            _host_plugin(root, "demo-host", {"id": "demo-host", "capabilities": ["tool.guard"]})
            _install_zip(data_dir, "demo")
            info = qqagent_market.build_bridge_catalog(data_dir, include_host=False)
            plugins = json.loads(Path(info["path"]).read_text(encoding="utf-8"))["plugins"]
            self.assertEqual([(entry["id"], entry["source"]) for entry in plugins],
                             [("demo", "zip")])
            self.assertEqual(info["hostCount"], 0)
            self.assertEqual(info["zipCount"], 1)

    def test_build_never_starts_node_and_never_materializes(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = _host_root(data_dir)
            _host_plugin(root, "demo-host", {"id": "demo-host", "capabilities": ["tool.guard"]},
                         probes={"demo-host": {"ok": True, "tools": ["demo-host__ping"],
                                               "hooks": [], "providers": ["tool.guard"]}})
            before = _snapshot(root)
            with mock.patch.object(asyncio, "create_subprocess_exec",
                                   side_effect=AssertionError("catalog 不该起进程")) as spawn, \
                    mock.patch.object(subprocess, "Popen",
                                      side_effect=AssertionError("catalog 不该起进程")) as popen, \
                    mock.patch.object(qqagent_host, "import_probe",
                                      side_effect=AssertionError("catalog 不该真加载")) as probe, \
                    mock.patch.object(qqagent_host, "materialize_host_root",
                                      side_effect=AssertionError("catalog 不该 materialize")):
                info = qqagent_market.build_bridge_catalog(data_dir)
            self.assertEqual(info["hostCount"], 1)
            self.assertEqual(spawn.call_count, 0)
            self.assertEqual(popen.call_count, 0)
            self.assertEqual(probe.call_count, 0)
            # 宿主根一个字节都没动（probes.json / plugins 都原样）。
            self.assertEqual(_snapshot(root), before)


class HostDiscoveryTests(unittest.TestCase):
    """宿主根里装好的扩展必须真被能力桥发现（落点、file 键、目录型扩展、白名单 provider）。"""

    def test_default_host_root_strips_duplicate_qq_only(self):
        base = Path(tempfile.gettempdir())
        # 调用方传的就是 <data>/qq-only，别再套一层，否则装在一层、读另一层。
        self.assertEqual(qqagent_host.default_host_root(base / "qq-only"),
                         base / "qq-only" / "qqagent_host")
        self.assertEqual(qqagent_host.default_host_root(base),
                         base / "qq-only" / "qqagent_host")

    def test_host_entry_exposes_file_and_capabilities_for_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = _host_root(data_dir)
            path = _host_plugin(root, "demo", {"id": "demo", "capabilities": ["tool.guard"]},
                                probes={"demo": {"ok": True, "tools": [], "hooks": [],
                                        "providers": ["message.owner-check"]}})
            info = qqagent_market.build_bridge_catalog(data_dir)
            found = extension_host.discover_extensions(info["path"], str(root / "plugins"))
            self.assertEqual([row["id"] for row in found], ["demo"])
            self.assertEqual(found[0]["file"], path)
            # 清单声明的在前、probes.json 实测到的补在后面。
            self.assertEqual(found[0]["providers"], ["tool.guard", "message.owner-check"])

    def test_bridge_profiles_cover_host_installed_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            root = _host_root(data_dir)
            path = _host_plugin(root, "owner-identity",
                                {"id": "owner-identity", "name": "主人识别",
                                 "capabilities": ["message.owner-check"]})
            info = qqagent_market.build_bridge_catalog(data_dir)
            profiles = qqagent_bridge.catalog_profiles(info["path"], str(root / "plugins"), 2)
            self.assertEqual([row["provider"] for row in profiles], ["message.owner-check"])
            self.assertEqual(profiles[0]["extensionPath"], str(path))
            self.assertEqual(profiles[0]["label"], "主人识别")
            self.assertTrue(profiles[0]["enabled"])


if __name__ == "__main__":
    unittest.main()
