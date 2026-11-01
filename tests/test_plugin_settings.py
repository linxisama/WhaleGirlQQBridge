"""PluginManager 设置校验、加载回退与原子性的离线测试。

核心审核修正（相对 WorkBuddy 原始候选）：
1. 去掉 _make_manager 里"创建又立刻删除临时目录"的死代码与无用 mock 分支；
2. 去掉 importlib/os 未使用导入，修正无意义的 is not None 断言；
3. 回退断言用精确默认值，不再用 .get(key, default) 绕过。
"""
import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.plugins import PluginBase, PluginMeta, FieldSchema
import src.plugins.manager  # noqa: F401  确保 PluginManager 已注册进 sys.modules


class _StubPluginA(PluginBase):
    META = PluginMeta(
        id="stub_a",
        name="Stub A",
        description="",
        category="test",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="enabled"),
            "count": FieldSchema(type="int", label="count", min=0, max=100),
            "ratio": FieldSchema(type="float", label="ratio"),
            "text": FieldSchema(type="string", label="text", multiline=True),
            "choice": FieldSchema(
                type="select",
                label="choice",
                values=["low", "high"],
                options=[{"label": "Low", "value": "low"}, {"label": "High", "value": "high"}],
            ),
        },
        default_settings={
            "enabled": False,
            "count": 5,
            "ratio": 0.5,
            "text": "",
            "choice": "low",
        },
    )


class _StubPluginB(PluginBase):
    META = PluginMeta(
        id="stub_b",
        name="Stub B",
        description="",
        category="test",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="enabled"),
            "label": FieldSchema(type="string", label="label"),
        },
        default_settings={"enabled": True, "label": "b"},
    )


def _reload_from_disk(tmp_dir: Path):
    """按磁盘上的现有设置文件重新构造管理器，用于验证加载回退路径。"""
    manager_cls = sys.modules["src.plugins.manager"].PluginManager
    manager = manager_cls(tmp_dir)
    stubs = {}
    for cls in (_StubPluginA, _StubPluginB):
        instance = cls(manager)
        stubs[instance.META.id] = instance
    manager._plugins = stubs
    manager._reconcile_settings_with_schemas()
    return manager


def _make_manager(tmp_dir: Path):
    """用临时目录建真实 PluginManager，再把插件替换成两个 stub，保持完全离线可控。"""
    manager_cls = sys.modules["src.plugins.manager"].PluginManager
    manager = manager_cls(tmp_dir)
    manager._plugins = {}
    manager._settings = {}
    for cls in (_StubPluginA, _StubPluginB):
        instance = cls(manager)
        manager._plugins[instance.META.id] = instance
        manager._settings[instance.META.id] = copy.deepcopy(instance.META.default_settings)
    manager._reconcile_settings_with_schemas()
    return manager


class PluginManagerSettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        (self.tmp_dir / "plugin_settings.json").write_text(json.dumps({}), "utf-8")
        self.mgr = _make_manager(self.tmp_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_plugin_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.mgr.update_settings("nope", {})

    def test_unknown_field_raises_valueerror(self):
        with self.assertRaises(ValueError):
            self.mgr.update_settings("stub_a", {"bad_key": 1})

    def test_non_dict_settings_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.update_settings("stub_a", ["not", "a", "dict"])

    def test_int_rejects_bool(self):
        with self.assertRaises(ValueError):
            self.mgr.update_settings("stub_a", {"count": True})

    def test_int_rejects_string(self):
        with self.assertRaises(ValueError):
            self.mgr.update_settings("stub_a", {"count": "3"})

    def test_float_rejects_nan_inf(self):
        for bad in (math.nan, math.inf):
            with self.assertRaises(ValueError):
                self.mgr.update_settings("stub_a", {"ratio": bad})

    def test_select_rejects_unlisted_and_bool(self):
        with self.assertRaises(ValueError):
            self.mgr.update_settings("stub_a", {"choice": "medium"})
        with self.assertRaises(ValueError):
            self.mgr.update_settings("stub_a", {"choice": False})

    def test_partial_update_keeps_defaults_and_previous(self):
        self.mgr.update_settings("stub_a", {"count": 10})
        s = self.mgr.get_settings("stub_a")
        self.assertEqual(s["count"], 10)
        self.assertEqual(s["ratio"], 0.5)
        self.assertEqual(s["text"], "")
        self.assertEqual(s["enabled"], False)

    def test_load_fallback_top_level_not_dict(self):
        (self.tmp_dir / "plugin_settings.json").write_text(json.dumps([1, 2, 3]), "utf-8")
        mgr = _reload_from_disk(self.tmp_dir)
        self.assertEqual(mgr.get_settings("stub_a")["count"], 5)

    def test_load_fallback_single_plugin_not_dict(self):
        (self.tmp_dir / "plugin_settings.json").write_text(
            json.dumps({"stub_a": "oops", "stub_b": {"enabled": True}}), "utf-8")
        mgr = _reload_from_disk(self.tmp_dir)
        self.assertEqual(mgr.get_settings("stub_a")["count"], 5)
        self.assertEqual(mgr.get_settings("stub_b")["enabled"], True)

    def test_load_fallback_old_value_type_mismatch(self):
        (self.tmp_dir / "plugin_settings.json").write_text(
            json.dumps({"stub_a": {"count": "not_int", "ratio": [1], "enabled": "x"}}), "utf-8")
        mgr = _reload_from_disk(self.tmp_dir)
        s = mgr.get_settings("stub_a")
        self.assertEqual(s["count"], 5)
        self.assertEqual(s["ratio"], 0.5)
        self.assertEqual(s["enabled"], False)

    def test_unknown_schema_key_ignored_on_load(self):
        (self.tmp_dir / "plugin_settings.json").write_text(
            json.dumps({"stub_a": {"unknown_key": 1, "count": 7}}), "utf-8")
        mgr = _reload_from_disk(self.tmp_dir)
        s = mgr.get_settings("stub_a")
        self.assertEqual(s["count"], 7)
        self.assertNotIn("unknown_key", s)

    def test_get_settings_does_not_leak_mutable_reference(self):
        leaked = self.mgr.get_settings("stub_a")
        leaked["count"] = 999
        self.assertEqual(self.mgr.get_settings("stub_a")["count"], 5)

    def test_list_plugins_does_not_leak_mutable_reference(self):
        plugins = self.mgr.list_plugins()
        target = next(p for p in plugins if p["id"] == "stub_a")
        target["settings"]["count"] = 111
        target["defaults"]["count"] = 222
        again = next(p for p in self.mgr.list_plugins() if p["id"] == "stub_a")
        self.assertEqual(again["settings"]["count"], 5)
        self.assertEqual(again["defaults"]["count"], 5)

    def test_write_failure_keeps_memory_and_file_unchanged(self):
        before = copy.deepcopy(self.mgr.get_settings("stub_a"))
        with mock.patch.object(self.mgr, "_save_settings", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.mgr.update_settings("stub_a", {"count": 42})
        self.assertEqual(self.mgr.get_settings("stub_a"), before)
        on_disk = json.loads((self.tmp_dir / "plugin_settings.json").read_text("utf-8"))
        self.assertEqual(on_disk.get("stub_a", {}).get("count", 5), 5)

    def test_valid_update_writes_file_and_memory(self):
        self.mgr.update_settings("stub_a", {"count": 33})
        self.assertEqual(self.mgr.get_settings("stub_a")["count"], 33)
        on_disk = json.loads((self.tmp_dir / "plugin_settings.json").read_text("utf-8"))
        self.assertEqual(on_disk["stub_a"]["count"], 33)

    def test_integral_float_accepted_for_int_field(self):
        self.mgr.update_settings("stub_a", {"count": 10.0})
        self.assertEqual(self.mgr.get_settings("stub_a")["count"], 10)

    def test_min_max_enforced(self):
        with self.assertRaises(ValueError):
            self.mgr.update_settings("stub_a", {"count": -1})
        with self.assertRaises(ValueError):
            self.mgr.update_settings("stub_a", {"count": 101})


if __name__ == "__main__":
    unittest.main()
