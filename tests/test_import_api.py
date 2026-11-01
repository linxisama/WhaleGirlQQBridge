"""W11-4b 统一导入端点用例：/api/import/inspect 与 /api/import/install。

真的起 ASGI（TestClient 而不是直接调函数），所以鉴权、状态码、JSON 形状都是端到端看到的；
落盘目标一律指向 tempfile 目录，绝不碰仓库真实 data/。
"""

import json
import os
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

PY_PACK = Path("D:/聊天机器人/插件市场/B站运营看板-bilibili_account.zip")
LEGACY_PY_PACK = Path("D:/聊天机器人/插件市场/@群友（按名字）-at_mention.zip")
JS_PACK = Path("D:/聊天机器人/qq机器人/.tmp/external-market/access-control.zip")
SKILL_PACK = Path("D:/聊天机器人/插件市场/Skill市场/5E CS2 赛事与阵容-csgo-5e.zip")

SCHEMA_KEYS = set(import_api.SCHEMA_KEYS)


def snapshot(root) -> list:
    """目录快照：用来证明「一个文件都没写」。"""
    root = Path(root)
    if not root.exists():
        return []
    out = []
    for base, _dirs, files in os.walk(str(root)):
        for name in files:
            out.append(str(Path(base, name).relative_to(root)).replace(os.sep, "/"))
    return sorted(out)


def codes(payload) -> list:
    return [str(one.get("code") or "") for one in payload.get("problems") or []]


def notice_codes(payload) -> list:
    return [str(one.get("code") or "") for one in payload.get("notices") or []]


def build_zip(target, members: dict) -> str:
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            payload = data.encode("utf-8") if isinstance(data, str) else data
            archive.writestr(name, payload)
    return str(target)


def real_plugin_source() -> str:
    with zipfile.ZipFile(str(PY_PACK)) as archive:
        for name in archive.namelist():
            if name.endswith(".py"):
                return archive.read(name).decode("utf-8")
    raise AssertionError("真 Python 包里没有 .py")


def feature_pack_fixture(target) -> str:
    """功能包没有真包（市场里还没发），用最小合法夹具顶这一族。"""
    manifest = {
        "format": "whale-feature-pack",
        "version": 1,
        "name": "W11-4b 夹具功能包",
        "plugins": [{"file": "plugins/hello_pack.py"}],
        "skills": [{"file": "skills/hello_pack.json"}],
        "preferences": [{"key": "称呼", "value": "老鲸"}],
    }
    skill = json.dumps({
        "format": "whale-agent-skill",
        "version": 1,
        "id": "pack_skill",
        "name": "夹具技能",
        "instructions": "遇到夹具相关问题时按此说明处理。",
        "triggers": ["夹具"],
    }, ensure_ascii=False)
    return build_zip(target, {
        "pack.json": json.dumps(manifest, ensure_ascii=False),
        "plugins/hello_pack.py": real_plugin_source(),
        "skills/hello_pack.json": skill,
    })


class ApiHarness(unittest.TestCase):
    """每个用例一个 tempfile 数据根 + 一个挂了 /api/import 的裸 FastAPI。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="w11-4b-api-")
        self.tmp = Path(self._tmp.name)
        self.app = FastAPI()
        self.app.state.bot = SimpleNamespace(
            root=str(self.tmp / "data"),
            plugins=SimpleNamespace(custom_plugins_dir=str(self.tmp / "plugins")))
        import_api.register(self.app)
        self.client = TestClient(self.app)

    def tearDown(self):
        self._tmp.cleanup()

    def require(self, path: Path) -> Path:
        if not Path(path).is_file():
            self.skipTest("真包不在：%s" % path)
        return Path(path)

    def inspect(self, path, client=None):
        return (client or self.client).post("/api/import/inspect", json={"path": str(path)})

    def install(self, path, client=None, **body):
        payload = {"path": str(path), "confirmed": True, "kind": "",
                   "overwrite": False, "enable": None}
        payload.update(body)
        return (client or self.client).post("/api/import/install", json=payload)


class InspectFamilyTest(ApiHarness):
    def test_python_plugin_family(self):
        response = self.inspect(self.require(PY_PACK))
        body = response.json()
        self.assertEqual(response.status_code, 200, body["problems"])
        self.assertTrue(body["ok"], body["problems"])
        self.assertEqual(body["kind"], "plugin-py")

    def test_js_extension_family(self):
        response = self.inspect(self.require(JS_PACK))
        body = response.json()
        self.assertEqual(response.status_code, 200, body["problems"])
        self.assertEqual(body["kind"], "extension-js")

    def test_skill_family(self):
        response = self.inspect(self.require(SKILL_PACK))
        body = response.json()
        self.assertEqual(response.status_code, 200, body["problems"])
        self.assertEqual(body["kind"], "skill")
        # skills_runtime 传的是宿主数据根，所以计划落点必须算得出来
        self.assertTrue(body["target_dir"], body)

    def test_feature_pack_fixture_family(self):
        pack = feature_pack_fixture(self.tmp / "feature.zip")
        response = self.inspect(pack)
        body = response.json()
        self.assertEqual(response.status_code, 200, body["problems"])
        self.assertEqual(body["kind"], "feature-pack")

    def test_unknown_family_is_refused(self):
        pack = build_zip(self.tmp / "junk.zip", {"readme.txt": "这里什么都没有"})
        response = self.inspect(pack)
        body = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["kind"], "unknown")

    def test_legacy_py_dialect_is_normalized_and_accepted(self):
        """真包里的老方言（whale-plugin v1）：家族认得，归一后按 plugin-py 接收，不再拒收。"""
        response = self.inspect(self.require(LEGACY_PY_PACK))
        body = response.json()
        self.assertEqual(response.status_code, 200, body["problems"])
        self.assertEqual(body["kind"], "plugin-py")
        self.assertTrue(body["ok"], body["problems"])
        self.assertEqual(body["problems"], [])
        # 包里除入口还有 README.md / 使用说明.md：Python 插件只落单文件 <id>.py，这些确实不会
        # 装进去，所以现在多一条 extra-files-not-installed 告知（见 src/pack_import.py
        # _fill_plugin_py 的 extras 分支；以前这条丢文件的事完全无声）。
        self.assertEqual(notice_codes(body), ["b1-dialect-normalized", "extra-files-not-installed"])
        self.assertIn("app_version", body["notices"][0]["message"])
        self.assertIn("不会装进去", body["notices"][1]["message"])

    def test_empty_path_is_bad_path(self):
        response = self.client.post("/api/import/inspect", json={"path": ""})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(codes(response.json()), ["bad-path"])

    def test_missing_path_is_bad_path(self):
        response = self.inspect(self.tmp / "没有这个包.zip")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(codes(response.json()), ["bad-path"])

    def test_schema_keys_match_pack_import(self):
        body = self.inspect(self.require(JS_PACK)).json()
        self.assertEqual(set(body), SCHEMA_KEYS)
        self.assertEqual(SCHEMA_KEYS, set(pack_import._new_result()))
        self.assertIsInstance(body["duration_ms"], int)


class InstallEndpointTest(ApiHarness):
    def test_unconfirmed_writes_nothing(self):
        pack = self.require(PY_PACK)
        response = self.install(pack, confirmed=False)
        body = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(codes(body), ["unconfirmed"])
        self.assertEqual(snapshot(self.tmp / "plugins"), [])
        self.assertEqual(snapshot(self.tmp / "data"), [])

    def test_confirmed_installs_plugin_into_temp(self):
        pack = self.require(PY_PACK)
        response = self.install(pack, overwrite=True)
        body = response.json()
        self.assertEqual(response.status_code, 200, body["problems"])
        self.assertTrue(body["ok"], body["problems"])
        self.assertEqual(body["kind"], "plugin-py")
        landed = Path(body["landed"])
        self.assertTrue(landed.is_file(), landed)
        self.assertTrue(str(landed).startswith(str(self.tmp)), landed)

    def test_confirmed_installs_skill_into_runtime(self):
        pack = self.require(SKILL_PACK)
        response = self.install(pack, enable=False)
        body = response.json()
        self.assertEqual(response.status_code, 200, body["problems"])
        self.assertEqual(body["kind"], "skill")
        self.assertTrue(str(body["landed"]).startswith(str(self.tmp)), body["landed"])
        self.assertTrue(Path(body["landed"]).exists(), body["landed"])

    def test_install_without_path_is_bad_path(self):
        response = self.client.post("/api/import/install", json={"confirmed": True, "path": ""})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(codes(response.json()), ["bad-path"])

    def test_pack_import_error_is_400_not_500(self):
        pack = self.require(JS_PACK)
        boom = pack_import.PackImportError("未确认：夹具里抛的")
        with mock.patch.object(import_api.pack_import, "install", side_effect=boom):
            response = self.install(pack)
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(codes(body), ["unconfirmed"])

    def test_unexpected_error_is_400_not_500(self):
        pack = self.require(JS_PACK)
        with mock.patch.object(import_api.pack_import, "install", side_effect=RuntimeError("炸了")):
            response = self.install(pack)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(codes(response.json()), ["install-failed"])


class AuthTest(ApiHarness):
    def test_forwarded_header_is_rejected(self):
        response = self.client.post("/api/import/inspect", json={"path": str(PY_PACK)},
                                    headers={"x-forwarded-for": "10.0.0.9"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "auth_denied")

    def test_forwarded_header_rejected_on_install(self):
        response = self.client.post("/api/import/install",
                                    json={"path": str(PY_PACK), "confirmed": True},
                                    headers={"forwarded": "for=10.0.0.9"})
        self.assertEqual(response.status_code, 403)

    def test_non_loopback_client_is_rejected(self):
        remote = TestClient(self.app, client=("10.1.2.3", 5000))
        response = self.inspect(self.require(JS_PACK), client=remote)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "auth_denied")

    def test_cross_origin_is_rejected(self):
        response = self.client.post("/api/import/inspect", json={"path": str(JS_PACK)},
                                    headers={"origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_same_origin_is_allowed(self):
        response = self.client.post("/api/import/inspect",
                                    json={"path": str(self.require(JS_PACK))},
                                    headers={"origin": "http://testserver"})
        self.assertEqual(response.status_code, 200)

    def test_routes_are_registered_without_included_router(self):
        """直接 app.post 注册：app.routes 里每一项都得有 .path（不许出现 _IncludedRouter）。"""
        paths = [getattr(route, "path", None) for route in self.app.routes]
        self.assertNotIn(None, paths)
        self.assertIn("/api/import/inspect", paths)
        self.assertIn("/api/import/install", paths)


if __name__ == "__main__":
    unittest.main()
