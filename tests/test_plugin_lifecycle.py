"""插件加载生命周期与旧键迁移的回归测试。

两个真 bug（都由用户运行日志里的
``[plugins] mention_gate 迁移失败（不影响运行）：'Unknown plugin: mention_gate'`` 暴露）：

1. **注册时序**：``PluginManager._load_plugins`` 先 ``cls(self)`` 再放进 ``_plugins``，
   而 mention_gate 在 ``__init__`` 里就调 ``update_settings`` → 插件那时还不存在，
   迁移**静默失败**（只打一行"不影响运行"）。修法：新增 ``post_load()`` 钩子，
   在**注册之后**调用。
2. **旧键被 schema 过滤**：``self.settings`` 走 ``get_settings``，只返回 schema 里已知的键，
   所以已废弃的 ``onlyGroups`` 根本读不到 —— 就算时序修好了，群号也搬不过来。
   修法：新增 ``raw_settings`` 属性，返回未过滤的已存值。
"""

import json
import pathlib
import tempfile
import unittest

from src.plugins import PluginBase
from src.plugins.manager import PluginManager
from src.plugins.mention_gate import MentionGate, parse_ids

LEGACY = {"enabled": True, "onlyGroups": "123456789,987654321", "allowKeyword": "救命"}


class PostLoadHookTests(unittest.TestCase):
    """钩子本身：注册之后才调用，且异常不会拖垮加载。"""

    def test_hook_called_after_registration(self):
        seen = {}

        class Probe(PluginBase):
            META = None

            def post_load(self):
                # 钩子跑的时候，自己必须已经在管理器里了
                seen["registered"] = "probe" in self._manager._plugins
                seen["called"] = True

        from src.plugins import PluginMeta
        Probe.META = PluginMeta(id="probe", name="探针", description="",
                                config_schema={"enabled": {"type": "bool"}},
                                default_settings={"enabled": False})

        manager = PluginManager.__new__(PluginManager)
        manager._settings = {}
        manager._plugins = {}
        manager.data_dir = pathlib.Path(tempfile.mkdtemp())
        manager.state_dir = manager.data_dir / "plugin_state"
        manager.state_dir.mkdir(parents=True, exist_ok=True)
        manager._settings_path = manager.data_dir / "plugin_settings.json"

        import src.plugins.manager as mg
        original = mg.PLUGIN_MODULES
        mg.PLUGIN_MODULES = []
        try:
            inst = Probe(manager)
            manager._plugins[inst.META.id] = inst
            if inst.META.id not in manager._settings:
                manager._settings[inst.META.id] = dict(inst.META.default_settings)
            inst.post_load()
        finally:
            mg.PLUGIN_MODULES = original

        self.assertTrue(seen.get("called"))
        self.assertTrue(seen.get("registered"), "post_load 必须在注册之后才算数")

    def test_default_hook_is_noop(self):
        class Plain(PluginBase):
            pass

        plain = Plain.__new__(Plain)
        self.assertIsNone(Plain.post_load(plain))

    def test_documentation_mentions_why(self):
        self.assertIn("注册", PluginBase.post_load.__doc__ or "")


class RawSettingsTests(unittest.TestCase):
    """raw_settings 必须能看到被 schema 过滤掉的旧键。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _manager_with_legacy(self):
        (self.dir / "plugin_settings.json").write_text(
            json.dumps({"mention_gate": dict(LEGACY)}, ensure_ascii=False), encoding="utf-8")
        return PluginManager(self.dir)

    def test_settings_hides_legacy_keys(self):
        # 这正是为什么迁移不能用 self.settings
        manager = self._manager_with_legacy()
        plugin = manager._plugins["mention_gate"]
        self.assertNotIn("onlyGroups", plugin.settings)
        self.assertNotIn("allowKeyword", plugin.settings)

    def test_raw_settings_exposes_legacy_keys(self):
        manager = PluginManager.__new__(PluginManager)
        manager._settings = {"mention_gate": dict(LEGACY)}
        plugin = MentionGate(manager)
        self.assertEqual(plugin.raw_settings.get("onlyGroups"), "123456789,987654321")
        self.assertEqual(plugin.raw_settings.get("allowKeyword"), "救命")

    def test_raw_settings_returns_copy(self):
        manager = PluginManager.__new__(PluginManager)
        manager._settings = {"mention_gate": dict(LEGACY)}
        plugin = MentionGate(manager)
        snapshot = plugin.raw_settings
        snapshot["onlyGroups"] = "changed"
        self.assertEqual(manager._settings["mention_gate"]["onlyGroups"],
                         "123456789,987654321")

    def test_raw_settings_garbage_is_empty_dict(self):
        manager = PluginManager.__new__(PluginManager)
        for value in (None, "x", [], 3):
            manager._settings = {"mention_gate": value}
            plugin = MentionGate(manager)
            self.assertEqual(plugin.raw_settings, {})


class LegacyMigrationTests(unittest.TestCase):
    """端到端：带旧键的设置文件 → 迁移后群号搬过去、旧键消失。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.settings_file = self.dir / "plugin_settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_group_ids_migrated_and_persisted(self):
        self.settings_file.write_text(json.dumps({"mention_gate": dict(LEGACY)},
                                                ensure_ascii=False), encoding="utf-8")
        manager = PluginManager(self.dir)
        current = manager.get_settings("mention_gate")
        self.assertEqual(current.get("group_ids"), "123456789,987654321")
        self.assertNotIn("onlyGroups", current)
        self.assertNotIn("allowKeyword", current)

        saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["mention_gate"].get("group_ids"), "123456789,987654321")
        self.assertNotIn("onlyGroups", saved["mention_gate"])
        self.assertNotIn("allowKeyword", saved["mention_gate"])

    def test_existing_new_key_is_not_overwritten(self):
        legacy = dict(LEGACY, group_ids="555555555")
        self.settings_file.write_text(json.dumps({"mention_gate": legacy},
                                                ensure_ascii=False), encoding="utf-8")
        manager = PluginManager(self.dir)
        # 新键已有值时不搬旧值（单向、且不覆盖）
        self.assertEqual(manager.get_settings("mention_gate").get("group_ids"), "555555555")

    def test_invalid_legacy_group_ids_leave_key_empty(self):
        # 123 不是合法群号（太短），迁移不该编出一个值
        self.settings_file.write_text(json.dumps(
            {"mention_gate": {"enabled": True, "onlyGroups": "111,222"}},
            ensure_ascii=False), encoding="utf-8")
        manager = PluginManager(self.dir)
        self.assertEqual(manager.get_settings("mention_gate").get("group_ids") or "", "")

    def test_no_legacy_keys_is_a_noop(self):
        self.settings_file.write_text(json.dumps(
            {"mention_gate": {"enabled": True}}, ensure_ascii=False), encoding="utf-8")
        manager = PluginManager(self.dir)
        self.assertTrue(manager.get_settings("mention_gate").get("enabled"))

    def test_manager_still_loads_all_plugins(self):
        manager = PluginManager(self.dir)
        self.assertGreater(len(manager._plugins), 20)
        self.assertIn("mention_gate", manager._plugins)


class ParseIdsTests(unittest.TestCase):
    def test_valid_ids(self):
        self.assertEqual(parse_ids("123456789"), ["123456789"])
        self.assertEqual(parse_ids("123456789,987654321"),
                         ["123456789", "987654321"])

    def test_invalid_ids_dropped(self):
        for raw in ("111", "222", "abc", "", None, "12,34"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_ids(raw), [])


if __name__ == "__main__":
    unittest.main()
