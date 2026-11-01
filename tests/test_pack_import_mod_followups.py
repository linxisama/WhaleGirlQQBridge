"""F1-F4 follow-up: 装完即挂载 / 坏清单病因 / 回滚 hint 真假 / 可执行文件拒收。

四条都是第六家族（mod-py）落地后只读复核提出的问题，所以每条用例都写成「修前会挂、
修后必过」的形状：断言直接盯用户看得见的字段（notices / problems / hint / landed），
不读私有中间量。

夹具全部落在 tempfile：不碰仓库 data/、不读插件市场、不联网。
唯一替身是 F3 两条里的 `_Aside.move` / `_Aside.restore` 与 `Path.replace`：
用来在同一个进程里复现「Windows 下目标目录被占用」的两种失败。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src import import_api, pack_import  # noqa: E402
from src.plugins.manager import PluginManager  # noqa: E402

SCHEMA_KEYS = set(import_api.SCHEMA_KEYS)

MANIFEST = {
    "id": "demo_mod",
    "api_version": 1,
    "entry": "main.py",
    "name": "用例目录 mod",
    "version": "0.1.0",
    "description": "注册一个回显工具",
    "permissions": ["register_tool"],
}

ENTRY = '''from . import PluginBase, PluginMeta


def _echo(arguments, runtime):
    return {"echo": str((arguments or {}).get("text", "")), "plugin": "demo_mod"}


class DemoMod(PluginBase):
    META = PluginMeta(id="demo_mod", name="用例目录 mod", category="utility",
                      description="注册一个回显工具")

    def post_load(self):
        self.register_tool("verify_echo", _echo, description="回显一段文本",
                           parameters={"type": "object",
                                       "properties": {"text": {"type": "string"}}})
'''


def notice_codes(payload) -> list:
    return [str(one.get("code") or "") for one in payload.get("notices") or []]


def notice_text(payload) -> str:
    return " ".join(str(one.get("message") or "") for one in payload.get("notices") or [])


def codes(payload, key="problems") -> list:
    return [str(one.get("code") or "") for one in payload.get(key) or []]


def problems_text(payload) -> str:
    return " ".join("%s %s %s" % (one.get("code"), one.get("message"), one.get("hint"))
                    for one in payload.get("problems") or [])


def hint_text(payload) -> str:
    return " ".join(str(one.get("hint") or "") for one in payload.get("problems") or [])


def tree(root: Path) -> dict:
    root = Path(root)
    if not root.exists():
        return {}
    return {item.relative_to(root).as_posix(): item.read_bytes()
            for item in sorted(root.rglob("*")) if item.is_file()}


class FollowupCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pkg_dir = self.root / "pkgs"
        self.pkg_dir.mkdir()
        self.data_dir = self.root / "data"
        self.custom = self.data_dir / "custom_plugins"
        self.custom.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    # ── 夹具 ───────────────────────────────────────────────────────────────
    def build_mod(self, name="demo_mod", manifest=None, extra=None) -> Path:
        folder = self.pkg_dir / name
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        man = MANIFEST if manifest is None else manifest
        text = man if isinstance(man, str) else json.dumps(man, ensure_ascii=False)
        (folder / "plugin.json").write_text(text, encoding="utf-8")
        (folder / "main.py").write_text(ENTRY, encoding="utf-8")
        for rel, data in (extra or {}).items():
            item = folder / rel
            item.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                item.write_bytes(data)
            else:
                item.write_text(str(data), encoding="utf-8")
        return folder

    def as_zip(self, folder: Path) -> Path:
        target = self.root / (folder.name + ".zip")
        with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(folder.rglob("*")):
                if item.is_file():
                    archive.write(item, folder.name + "/" + item.relative_to(folder).as_posix())
        return target

    def install(self, folder, **kwargs):
        return pack_import.install(str(folder), confirmed=True,
                                   custom_plugins_dir=str(self.custom),
                                   data_dir=str(self.data_dir), **kwargs)

    def client_for(self, manager) -> TestClient:
        app = FastAPI()
        app.state.bot = SimpleNamespace(root=str(self.data_dir),
                                        data_root=str(self.data_dir), plugins=manager)
        import_api.register(app, data_dir=self.data_dir, plugins_dir=self.custom)
        return TestClient(app)


class MountAfterInstall(FollowupCase):
    """F1：装完立刻让运行中的宿主挂上，挂不上要如实写原因。"""

    def test_install_through_api_mounts_into_running_host(self):
        manager = PluginManager(self.data_dir)
        before = len(manager._plugins)
        pack = self.as_zip(self.build_mod())
        reply = self.client_for(manager).post(
            "/api/import/install", json={"path": str(pack), "confirmed": True})
        self.assertEqual(reply.status_code, 200)
        body = reply.json()
        self.assertTrue(body["ok"], body.get("problems"))
        self.assertEqual(body["kind"], "mod-py")
        # 1) 真 manager 身上已经挂上了（不用重启、不用手工 sync_mods）
        self.assertEqual(manager._mod_plugins.get("demo_mod"), "demo_mod")
        self.assertEqual(len(manager._plugins), before + 1)
        # 2) 挂上去的 mod 真能执行
        called = manager._mod_tool_hosts["demo_mod"].call_tool("verify_echo", {"text": "hi"})
        self.assertTrue(called["ok"], called)
        self.assertEqual(called["value"]["echo"], "hi")
        # 3) 文案口径：不许再说「下一次启动才会挂载」
        self.assertIn("mod-mounted", notice_codes(body))
        self.assertNotIn("下一次启动", notice_text(body))
        self.assertNotIn("才会挂载它", notice_text(body))
        self.assertEqual(body["load"], {"ok": True, "code": "mounted", "error": ""})
        # 4) 字段不越界
        self.assertEqual(sorted(set(body) - SCHEMA_KEYS), [])

    def test_sync_failure_keeps_install_successful(self):
        manager = PluginManager(self.data_dir)

        def boom():
            raise RuntimeError("stub boom 故意炸")

        manager.sync_mods = boom
        pack = self.as_zip(self.build_mod())
        reply = self.client_for(manager).post(
            "/api/import/install", json={"path": str(pack), "confirmed": True})
        self.assertEqual(reply.status_code, 200)
        body = reply.json()
        self.assertTrue(body["ok"], "sync 失败不许把成功的安装改编成失败")
        self.assertIn("stub boom", json.dumps(body, ensure_ascii=False))
        self.assertIn("mod-mount-pending", notice_codes(body))
        self.assertTrue((self.custom / "demo_mod" / "plugin.json").is_file())
        self.assertEqual(sorted(set(body) - SCHEMA_KEYS), [])
        # 落盘成功但没挂上：load 不许谎报 mounted
        self.assertNotEqual(body["load"].get("code"), "mounted")

    def test_missing_plugin_manager_is_a_notice_not_a_failure(self):
        app = FastAPI()
        app.state.bot = SimpleNamespace(root=str(self.data_dir), data_root=str(self.data_dir))
        import_api.register(app, data_dir=self.data_dir, plugins_dir=self.custom)
        pack = self.as_zip(self.build_mod())
        reply = TestClient(app).post("/api/import/install",
                                     json={"path": str(pack), "confirmed": True})
        body = reply.json()
        self.assertTrue(body["ok"])
        self.assertIn("PluginManager", notice_text(body))

    def test_rejected_by_sync_is_reported_as_rejected(self):
        manager = PluginManager(self.data_dir)
        manager.sync_mods = lambda: {"added": [], "removed": [], "reloaded": [],
                                     "unchanged": [], "rejected": ["demo_mod"],
                                     "failed": [], "actions": 0}
        pack = self.as_zip(self.build_mod())
        body = self.client_for(manager).post(
            "/api/import/install", json={"path": str(pack), "confirmed": True}).json()
        self.assertTrue(body["ok"])
        self.assertIn("mod-mount-failed", notice_codes(body))
        self.assertIn("rejected=demo_mod", notice_text(body))

    def test_other_kinds_do_not_call_sync_mods(self):
        manager = PluginManager(self.data_dir)
        calls = []
        manager.sync_mods = lambda: calls.append(1) or {}
        pack_import.install(str(self.build_mod()), confirmed=True,
                            custom_plugins_dir=str(self.custom), data_dir=str(self.data_dir))
        self.assertEqual(calls, [])


class BrokenManifestCause(FollowupCase):
    """F2：plugin.json 坏了要说清是哪一种坏，不许退回 plugin-py 的报错口径。"""

    CASES = (
        ("非 JSON 文本", "{ this is not json",
         "manifest-broken", "plugin.json 不是合法 JSON"),
        ("顶层是数组", "[1, 2, 3]",
         "manifest-broken", "plugin.json 顶层必须是对象"),
        ("缺 api_version", json.dumps({"id": "demo_mod", "entry": "main.py"}),
         "missing_api_version", "api_version"),
    )

    def test_each_bad_manifest_gets_its_own_cause(self):
        for label, raw, code, needle in self.CASES:
            with self.subTest(label):
                folder = self.build_mod(manifest=raw)
                got = pack_import.inspect(str(folder), custom_plugins_dir=str(self.custom),
                                          data_dir=str(self.data_dir))
                self.assertEqual(codes(got), [code], problems_text(got))
                self.assertIn(needle, problems_text(got))
                self.assertTrue(hint_text(got).strip(), "病因必须带 hint")
                self.assertNotEqual(got["kind"], "mod-py")

                done = self.install(folder)
                self.assertFalse(done["ok"])
                self.assertEqual(codes(done), [code], problems_text(done))
                self.assertFalse((self.custom / "demo_mod").exists())

    def test_no_plugin_py_noise_for_bad_manifests(self):
        for label, raw, _code, _needle in self.CASES:
            with self.subTest(label):
                blob = problems_text(self.install(self.build_mod(manifest=raw)))
                for noise in ("bad-id", "只支持 .py 或 .zip", "py-entry-missing"):
                    self.assertNotIn(noise, blob)

    def test_real_extension_js_is_still_extension_js(self):
        folder = self.pkg_dir / "js_ext"
        folder.mkdir()
        (folder / "plugin.json").write_text(
            json.dumps({"name": "js", "apiVersion": "0.4", "entry": "index.js"}),
            encoding="utf-8")
        (folder / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
        got = pack_import.inspect(str(folder), custom_plugins_dir=str(self.custom),
                                  data_dir=str(self.data_dir))
        self.assertEqual(got["kind"], "extension-js")
        self.assertEqual(codes(got, "problems"), []) if got["ok"] else None
        self.assertNotIn("manifest-broken", codes(got))


class RollbackHint(FollowupCase):
    """F3：回滚失败时不许说「旧版本已挪回」，两种失败要各自说真话。"""

    def _run(self, broken: str):
        self.install(self.build_mod())
        target = self.custom / "demo_mod"
        before = tree(target)
        newer = self.build_mod(extra={"extra.txt": "v2\n"})
        real_replace = Path.replace

        def fake_replace(one, other):
            if str(other) == str(target) and str(one) != str(target):
                raise PermissionError(13, "Access is denied")
            return real_replace(one, other)

        with mock.patch.object(pack_import._Aside, broken, lambda self: False), \
                mock.patch.object(Path, "replace", fake_replace):
            done = self.install(newer, overwrite=True)
        stashes = sorted(one for one in self.custom.iterdir() if one.name.startswith(".old-"))
        return done, target, before, stashes

    def test_move_failure_says_old_tree_untouched(self):
        done, target, before, stashes = self._run("move")
        self.assertFalse(done["ok"])
        hint = hint_text(done)
        self.assertNotIn("已挪回", hint)
        self.assertIn("原地没动过", hint)
        self.assertEqual(tree(target), before)      # 数据安全：旧树逐字节不变
        self.assertEqual(stashes, [])

    def test_restore_failure_points_at_the_backup(self):
        done, target, before, stashes = self._run("restore")
        self.assertFalse(done["ok"])
        hint = hint_text(done)
        self.assertNotIn("已挪回", hint)
        self.assertIn(".old-", hint)
        self.assertIn(str(target), hint)            # 说得清要挪回哪里
        self.assertFalse(target.exists())
        self.assertEqual(len(stashes), 1)
        self.assertEqual(tree(stashes[0]), before)  # 数据安全：旧树完整留着


class ForbiddenFiles(FollowupCase):
    """F4：可执行文件 / 动态库 / 脚本宿主一律拒收并点名。"""

    EXTRA = {
        "banned.pyd": b"MZ\x90\x00pyd",
        "run.bat": "@echo off\r\necho hi\r\n",
        "tool.exe": b"MZ\x90\x00exe",
        "evil.py.txt": "print('smuggled')\n",
    }

    def test_inspect_and_install_refuse_each_file_by_name(self):
        folder = self.build_mod(extra=self.EXTRA)
        got = pack_import.inspect(str(folder), custom_plugins_dir=str(self.custom),
                                  data_dir=str(self.data_dir))
        self.assertFalse(got["ok"])
        self.assertEqual(set(codes(got)), {"mod-forbidden-file"})
        blob = problems_text(got)
        for name in self.EXTRA:
            self.assertIn(name, blob)
        done = self.install(folder)
        self.assertFalse(done["ok"])
        self.assertEqual(set(codes(done)), {"mod-forbidden-file"})
        self.assertFalse(done["landed"])
        self.assertEqual(sorted(one.name for one in self.custom.iterdir()), [])

    def test_plain_data_files_are_still_fine(self):
        folder = self.build_mod(extra={"data/conf.json": "{}\n", "note.md": "# hi\n"})
        done = self.install(folder)
        self.assertEqual(done["problems"], [])
        self.assertTrue(done["ok"])
        self.assertTrue((self.custom / "demo_mod" / "data" / "conf.json").is_file())

    def test_python_source_is_still_scanned_by_the_old_rule(self):
        folder = self.build_mod(extra={"evil.py": "import socket\n"})
        done = self.install(folder)
        self.assertFalse(done["ok"])
        self.assertNotIn("mod-forbidden-file", codes(done))


if __name__ == "__main__":
    unittest.main()
