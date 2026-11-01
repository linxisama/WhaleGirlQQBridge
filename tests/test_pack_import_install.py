"""W11-4a install() 落盘用例：全部在 tempfile 目录里跑，绝不碰仓库真实 data/。"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src import pack_import  # noqa: E402

PY_PACK = Path("D:/聊天机器人/插件市场/B站运营看板-bilibili_account.zip")
JS_PACK = Path("D:/聊天机器人/qq机器人/.tmp/external-market/access-control.zip")
SKILL_PACK = Path("D:/聊天机器人/插件市场/Skill市场/5E CS2 赛事与阵容-csgo-5e.zip")


def snapshot(root) -> list:
    """目录快照：用来证明「没写任何文件」。"""
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            out.append(str(Path(base, name).relative_to(root)).replace(os.sep, "/"))
    return sorted(out)


def codes(result) -> list:
    return [str(one.get("code") or "") for one in result.get("problems") or []]


def notice_codes(result) -> list:
    return [str(one.get("code") or "") for one in result.get("notices") or []]


def build_zip(target, members: dict) -> str:
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            payload = data.encode("utf-8") if isinstance(data, str) else data
            archive.writestr(name, payload)
    return str(target)


def real_plugin_source() -> str:
    """拿真包里的插件源码当功能包夹具的 .py（保证能过 plugin_installer 的检查）。"""
    with zipfile.ZipFile(str(PY_PACK)) as archive:
        for name in archive.namelist():
            if name.endswith(".py"):
                return archive.read(name).decode("utf-8")
    raise AssertionError("真 Python 包里没有 .py")


def fixture_skill_json() -> str:
    """技能市场真包不是 whale-agent-skill 格式，功能包夹具用合法的最小技能 JSON。"""
    return json.dumps({
        "format": "whale-agent-skill",
        "version": 1,
        "id": "pack_skill",
        "name": "夹具技能",
        "instructions": "遇到夹具相关问题时按此说明处理。",
        "triggers": ["夹具"],
    }, ensure_ascii=False)


def feature_pack_fixture(target) -> str:
    manifest = {
        "format": "whale-feature-pack",
        "version": 1,
        "name": "W11-4a 夹具功能包",
        "plugins": [{"file": "plugins/hello_pack.py"}],
        "skills": [{"file": "skills/hello_pack.json"}],
        "preferences": [{"key": "称呼", "value": "老鲸"}],
    }
    return build_zip(target, {
        "pack.json": json.dumps(manifest, ensure_ascii=False),
        "plugins/hello_pack.py": real_plugin_source(),
        "skills/hello_pack.json": fixture_skill_json(),
    })


class InstallHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="w11-4a-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def plugins_dir(self) -> Path:
        return self.tmp / "plugins"

    def data_dir(self) -> Path:
        return self.tmp / "data"

    def skill_runtime(self):
        return SimpleNamespace(data_root=str(self.tmp / "rt"))

    def require(self, path: Path) -> Path:
        if not path.is_file():
            self.skipTest("真包不在：%s" % path)
        return path


class InstallPythonPluginTest(InstallHarness):
    def test_real_pack_lands_into_temp(self):
        pack = self.require(PY_PACK)
        result = pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir())
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["kind"], "plugin-py")
        landed = Path(result["landed"])
        self.assertTrue(landed.is_file(), landed)
        self.assertTrue(str(landed).startswith(str(self.tmp)), landed)
        self.assertEqual(Path(result["target_dir"]), landed.parent)
        self.assertIn(landed.name, snapshot(self.plugins_dir()))
        self.assertTrue(result["load"]["ok"], result["load"])
        self.assertEqual(result["load"]["code"], "post-install-check")
        self.assertFalse(result["installed_but_disabled"])
        self.assertTrue(any(one.get("message") for one in result["notices"]))

    def test_returns_same_keys_as_inspect(self):
        pack = self.require(PY_PACK)
        inspected = pack_import.inspect(pack, plugins_dir=self.plugins_dir())
        installed = pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir())
        self.assertEqual(set(installed), set(inspected))
        self.assertEqual(len(installed), 19)

    def test_missing_plugins_dir_reports_no_target(self):
        pack = self.require(PY_PACK)
        result = pack_import.install(pack, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertIn("no-target", codes(result))
        self.assertEqual(snapshot(self.tmp), [])

    def test_unconfirmed_raises_and_writes_nothing(self):
        pack = self.require(PY_PACK)
        with self.assertRaises(pack_import.PackImportError) as ctx:
            pack_import.install(pack, plugins_dir=self.plugins_dir())
        self.assertIn("未经确认", str(ctx.exception))
        self.assertEqual(snapshot(self.tmp), [])

    def test_kind_mismatch_blocks_write(self):
        pack = self.require(PY_PACK)
        result = pack_import.install(pack, confirmed=True, kind="skill",
                                    plugins_dir=self.plugins_dir())
        self.assertFalse(result["ok"])
        self.assertIn("kind-mismatch", codes(result))
        self.assertEqual(snapshot(self.tmp), [])

    def test_exists_without_overwrite(self):
        pack = self.require(PY_PACK)
        first = pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir())
        self.assertTrue(first["ok"], first["problems"])
        before = snapshot(self.plugins_dir())
        second = pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir())
        self.assertFalse(second["ok"])
        self.assertIn("exists", codes(second))
        self.assertEqual(snapshot(self.plugins_dir()), before)

    def test_overwrite_replaces(self):
        pack = self.require(PY_PACK)
        pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir())
        again = pack_import.install(pack, confirmed=True, overwrite=True,
                                   plugins_dir=self.plugins_dir())
        self.assertTrue(again["ok"], again["problems"])
        self.assertIn("overwritten", notice_codes(again))
        self.assertTrue(Path(again["landed"]).is_file())

    def test_enable_ignored_notice(self):
        pack = self.require(PY_PACK)
        result = pack_import.install(pack, confirmed=True, enable=True,
                                    plugins_dir=self.plugins_dir())
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("enable-ignored", notice_codes(result))

    def test_bad_zip_reports_problem(self):
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"this is not a zip at all")
        result = pack_import.install(str(bad), confirmed=True, plugins_dir=self.plugins_dir())
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])
        self.assertEqual(snapshot(self.tmp), ["bad.zip"])

    def test_missing_path_reports_problem(self):
        ghost = str(self.tmp / "ghost.zip")
        result = pack_import.install(ghost, confirmed=True, plugins_dir=self.plugins_dir())
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])
        self.assertEqual(snapshot(self.tmp), [])

    def test_pack_without_entry_reports_problem(self):
        pack = build_zip(self.tmp / "no-entry.zip", {"notes.txt": "没有入口"})
        result = pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir())
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])
        self.assertFalse(self.plugins_dir().exists())


class InstallExtensionJSTest(InstallHarness):
    def test_real_pack_lands_and_marked_disabled(self):
        pack = self.require(JS_PACK)
        result = pack_import.install(pack, confirmed=True, data_dir=self.data_dir())
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["kind"], "extension-js")
        landed = Path(result["landed"])
        self.assertTrue(landed.is_dir(), landed)
        self.assertTrue(str(landed).startswith(str(self.tmp)), landed)
        self.assertGreaterEqual(len(snapshot(landed)), 1)
        self.assertTrue(result["installed_but_disabled"])
        self.assertTrue(any(one.get("message") for one in result["notices"]))

    def test_missing_data_dir_reports_no_target(self):
        pack = self.require(JS_PACK)
        result = pack_import.install(pack, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertIn("no-target", codes(result))
        self.assertEqual(snapshot(self.tmp), [])

    def test_exists_without_overwrite(self):
        pack = self.require(JS_PACK)
        first = pack_import.install(pack, confirmed=True, data_dir=self.data_dir())
        self.assertTrue(first["ok"], first["problems"])
        second = pack_import.install(pack, confirmed=True, data_dir=self.data_dir())
        self.assertFalse(second["ok"])
        self.assertIn("exists", codes(second))
        self.assertTrue(Path(first["landed"]).is_dir())

    def test_enabled_by_default_false_is_marked_disabled(self):
        pack = self.require(JS_PACK)
        with zipfile.ZipFile(str(pack)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        names = [name for name in members if name.endswith("plugin.json")]
        self.assertTrue(names, "真 JS 包里没有 plugin.json")
        manifest = json.loads(members[names[0]].decode("utf-8"))
        manifest["enabledByDefault"] = False
        members[names[0]] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        patched = build_zip(self.tmp / "disabled.zip", members)
        result = pack_import.install(patched, confirmed=True, data_dir=self.data_dir())
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("enabled-by-default-false", notice_codes(result))
        self.assertTrue(result["installed_but_disabled"])
        notice = [one for one in result["notices"]
                  if one.get("code") == "enabled-by-default-false"][0]
        self.assertTrue(notice["message"].strip())


class InstallSkillTest(InstallHarness):
    def test_real_pack_lands(self):
        pack = self.require(SKILL_PACK)
        result = pack_import.install(pack, confirmed=True, skills_runtime=self.skill_runtime())
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["kind"], "skill")
        landed = Path(result["landed"])
        self.assertTrue(landed.is_dir(), landed)
        self.assertTrue(str(landed).startswith(str(self.tmp)), landed)
        self.assertIn("skill.json", snapshot(landed))
        self.assertTrue(result["installed_but_disabled"])
        self.assertIn("skill-target", notice_codes(result))

    def test_exists_and_overwrite(self):
        pack = self.require(SKILL_PACK)
        runtime = self.skill_runtime()
        first = pack_import.install(pack, confirmed=True, skills_runtime=runtime)
        self.assertTrue(first["ok"], first["problems"])
        landed = Path(first["landed"])
        before = snapshot(landed)
        second = pack_import.install(pack, confirmed=True, skills_runtime=runtime)
        self.assertFalse(second["ok"])
        self.assertIn("exists", codes(second))
        self.assertEqual(snapshot(landed), before)
        third = pack_import.install(pack, confirmed=True, overwrite=True,
                                    skills_runtime=runtime)
        self.assertTrue(third["ok"], third["problems"])
        self.assertEqual(Path(third["landed"]), landed)


class InstallFeaturePackTest(InstallHarness):
    def test_fixture_pack_lands(self):
        pack = feature_pack_fixture(self.tmp / "feature.zip")
        result = pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir(),
                                    data_dir=self.data_dir())
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["kind"], "feature-pack")
        self.assertTrue(Path(result["landed"]).is_file(), result["landed"])
        self.assertIn("hello_pack.py", snapshot(self.plugins_dir()))
        self.assertTrue((self.data_dir() / "agent_learning.json").is_file())
        self.assertTrue(result["load"]["ok"], result["load"])
        self.assertFalse(result["installed_but_disabled"])

    def test_missing_roots_report_no_target(self):
        pack = feature_pack_fixture(self.tmp / "feature.zip")
        result = pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir())
        self.assertFalse(result["ok"])
        self.assertIn("no-target", codes(result))
        self.assertFalse(self.plugins_dir().exists())

    def test_broken_entry_reports_problem_without_raising(self):
        self.require(PY_PACK)
        manifest = {"format": "whale-feature-pack", "version": 1, "name": "坏条目功能包",
                    "plugins": [{"file": "plugins/hello_pack.py"},
                                {"file": "plugins/broken_pack.py"}]}
        pack = build_zip(self.tmp / "broken-feature.zip", {
            "pack.json": json.dumps(manifest, ensure_ascii=False),
            "plugins/hello_pack.py": real_plugin_source(),
            "plugins/broken_pack.py": "import os\nos.system('echo hi')\n",
        })
        result = pack_import.install(pack, confirmed=True, plugins_dir=self.plugins_dir(),
                                    data_dir=self.data_dir())
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])
        self.assertTrue(set(codes(result)) & {"partial", "install-failed", "exists"})
        self.assertIn("partial=True", json.dumps(result["notices"], ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()


