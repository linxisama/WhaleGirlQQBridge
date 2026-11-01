"""mod 目录挂载：plugin.json 清单校验、自动挂载、拒载时不带崩主体。"""
import json
import pathlib
import tempfile
import unittest

from src.plugins.manager import PluginManager

MOD_SRC = '''from . import PluginBase, PluginMeta


class Demo(PluginBase):
    META = PluginMeta(id="{pid}", name="{name}", category="utility",
                      description="测试用 mod")

    def on_ingest(self, message, runtime):
        return None
'''


class ModMountTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.plugins_dir = self.root / "custom_plugins"
        self.plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def add_mod(self, mod_id, manifest=None, entry_code=None, entry_name="mod.py",
                raw_manifest=None):
        folder = self.plugins_dir / mod_id
        folder.mkdir(parents=True, exist_ok=True)
        if entry_code is None:
            entry_code = MOD_SRC.format(pid=mod_id, name=mod_id)
        if entry_name:
            (folder / entry_name).write_text(entry_code, encoding="utf-8")
        if raw_manifest is not None:
            (folder / "plugin.json").write_text(raw_manifest, encoding="utf-8")
        else:
            data = {"id": mod_id, "api_version": 1, "entry": entry_name or "mod.py"}
            data.update(manifest or {})
            (folder / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
        return folder

    def add_bare(self, pid, code=None):
        code = code or MOD_SRC.format(pid=pid, name=pid)
        (self.plugins_dir / (pid + ".py")).write_text(code, encoding="utf-8")
        (self.root / "custom_plugins.json").write_text(
            json.dumps({"plugins": [pid]}), encoding="utf-8")
        return pid

    def audit_events(self):
        path = self.root / "qq-only" / "plugin_audit.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()
                if line.strip()]

    def problems_text(self, manager):
        return "\n".join(manager.take_load_problems())

    # ── A. 目录 mod 自动挂载 ────────────────────────────────────────────────
    def test_mod_directory_mounts_without_register_custom(self):
        self.add_mod("demo_mod")
        manager = PluginManager(self.root)
        self.assertIn("demo_mod", manager._plugins)
        self.assertEqual(manager.take_load_problems(), [])
        # 没进自定义清单，也没调 register_custom：唯一的挂载点就是目录本身。
        self.assertNotIn("demo_mod", manager.custom_plugin_ids())
        mounted = [e for e in self.audit_events() if e["event"] == "mounted"]
        self.assertEqual([e["plugin"] for e in mounted], ["demo_mod"])
        self.assertIn("mod.py", mounted[0]["detail"])

    def test_bare_py_plugin_still_loads(self):
        self.add_bare("legacy_one")
        manager = PluginManager(self.root)
        self.assertIn("legacy_one", manager._plugins)
        self.assertEqual(manager.take_load_problems(), [])
        # 裸 .py 不是 mod，不写 mounted 审计（旧通道行为一个字节都没变）。
        self.assertEqual([e for e in self.audit_events()
                          if e["plugin"] == "legacy_one"], [])

    def test_mod_and_bare_py_coexist(self):
        self.add_mod("demo_mod")
        self.add_bare("legacy_one")
        manager = PluginManager(self.root)
        self.assertIn("demo_mod", manager._plugins)
        self.assertIn("legacy_one", manager._plugins)
        self.assertEqual(manager.take_load_problems(), [])

    # ── 拒载：清单不合格 ────────────────────────────────────────────────────
    def test_api_version_mismatch_is_refused_and_body_survives(self):
        self.add_mod("future_mod", manifest={"api_version": 2})
        self.add_mod("good_mod")
        manager = PluginManager(self.root)
        self.assertNotIn("future_mod", manager._plugins)
        self.assertIn("good_mod", manager._plugins)
        self.assertIn("future_mod", self.problems_text(manager))
        self.assertGreaterEqual(len(manager._plugins), 60)   # 内置插件照常全在

    def test_missing_api_version_is_refused(self):
        self.add_mod("no_version_mod", raw_manifest=json.dumps(
            {"id": "no_version_mod", "entry": "mod.py"}))
        manager = PluginManager(self.root)
        self.assertNotIn("no_version_mod", manager._plugins)
        self.assertIn("api_version", self.problems_text(manager))

    def test_entry_escape_is_refused(self):
        self.add_mod("escape_mod", manifest={"entry": "../outside.py"})
        self.add_mod("absolute_mod", manifest={"entry": str(self.root / "abs.py")})
        manager = PluginManager(self.root)
        self.assertNotIn("escape_mod", manager._plugins)
        self.assertNotIn("absolute_mod", manager._plugins)
        problems = self.problems_text(manager)
        self.assertIn("escape_mod", problems)
        self.assertIn("absolute_mod", problems)
        self.assertIn("越出", problems)

    def test_entry_missing_is_refused(self):
        self.add_mod("lost_mod", manifest={"entry": "absent.py"})
        manager = PluginManager(self.root)
        self.assertNotIn("lost_mod", manager._plugins)
        self.assertIn("不存在", self.problems_text(manager))

    def test_bad_json_manifest_is_refused(self):
        self.add_mod("broken_mod", raw_manifest='{"id": "broken_mod",')
        manager = PluginManager(self.root)
        self.assertNotIn("broken_mod", manager._plugins)
        self.assertIn("JSON", self.problems_text(manager))

    def test_id_mismatch_is_refused(self):
        self.add_mod("dir_name", manifest={"id": "other_id"})
        manager = PluginManager(self.root)
        self.assertNotIn("dir_name", manager._plugins)
        self.assertNotIn("other_id", manager._plugins)
        self.assertIn("不一致", self.problems_text(manager))

    def test_unknown_permission_is_refused(self):
        self.add_mod("perm_mod", manifest={"permissions": ["network", "root_all"]})
        manager = PluginManager(self.root)
        self.assertNotIn("perm_mod", manager._plugins)
        self.assertIn("permissions", self.problems_text(manager))

    # ── 不误伤 / 不顶掉既有插件 ─────────────────────────────────────────────
    def test_plain_subdirectory_without_manifest_is_ignored(self):
        plain = self.plugins_dir / "unzipped_leftover"
        plain.mkdir()
        (plain / "notes.py").write_text("x = 1\n", encoding="utf-8")
        manager = PluginManager(self.root)
        self.assertNotIn("unzipped_leftover", manager._plugins)
        self.assertEqual(manager.take_load_problems(), [])

    def test_mod_cannot_take_over_builtin_id(self):
        self.add_mod("pc_control")
        manager = PluginManager(self.root)
        instance = manager._plugins["pc_control"]
        self.assertEqual(type(instance).__module__, "src.plugins.pc_control")
        self.assertIn("内置", self.problems_text(manager))

    def test_mod_cannot_take_over_bare_py_id(self):
        self.add_bare("same_id")
        self.add_mod("same_id")
        manager = PluginManager(self.root)
        instance = manager._plugins["same_id"]
        self.assertEqual(type(instance).__module__, "src_custom_plugin_same_id")
        self.assertIn("同名裸插件", self.problems_text(manager))

    # ── 可选字段与审计字段 ──────────────────────────────────────────────────
    def test_enabled_by_default_false_lands_in_settings(self):
        self.add_mod("off_mod", manifest={"enabled_by_default": False,
                                          "permissions": ["read_message"]})
        manager = PluginManager(self.root)
        self.assertIn("off_mod", manager._plugins)
        self.assertFalse(manager._plugins["off_mod"].enabled)

    def test_audit_lines_carry_required_fields(self):
        self.add_mod("demo_mod")
        self.add_mod("bad_version_mod", manifest={"api_version": 7})
        manager = PluginManager(self.root)
        manager.take_load_problems()
        events = {e["event"]: e for e in self.audit_events()}
        for name in ("mounted", "load-refused"):
            self.assertIn(name, events)
            for field in ("ts", "plugin", "event", "hook", "code", "detail"):
                self.assertIn(field, events[name])
        self.assertEqual(events["load-refused"]["plugin"], "bad_version_mod")
        self.assertEqual(events["mounted"]["plugin"], "demo_mod")


if __name__ == "__main__":
    unittest.main()
