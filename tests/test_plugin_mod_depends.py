"""目录 mod 的依赖声明：depends 校验、缺依赖/成环拒载、拓扑挂载顺序。"""
import json
import pathlib
import shutil
import tempfile
import unittest

from src.plugins.manager import PLUGIN_MODULES, PluginManager

ORDER_SRC = '''from . import PluginBase, PluginMeta


class Order(PluginBase):
    META = PluginMeta(id="{pid}", name="{pid}", category="utility",
                      description="拓扑顺序探针")

    def post_load(self):
        with open({order_file!r}, "a", encoding="utf-8") as fh:
            fh.write("{pid}\\n")
'''

PLAIN_SRC = '''from . import PluginBase, PluginMeta


class Plain(PluginBase):
    META = PluginMeta(id="{pid}", name="{pid}", category="utility",
                      description="测试用 mod")

    def on_ingest(self, message, runtime):
        return None
'''


class ModDependsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.plugins_dir = self.root / "custom_plugins"
        self.plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    # ── 脚手架 ──────────────────────────────────────────────────────────────
    def add_mod(self, mod_id, depends=None, entry_code=None, base=None,
                entry_name="mod.py"):
        base = base or self.plugins_dir
        folder = base / mod_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / entry_name).write_text(
            entry_code if entry_code is not None
            else PLAIN_SRC.format(pid=mod_id), encoding="utf-8")
        data = {"id": mod_id, "api_version": 1, "entry": entry_name}
        if depends is not None:
            data["depends"] = depends
        (folder / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
        return folder

    def fresh_root(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "custom_plugins").mkdir()
        return root

    def audit_events(self, root=None):
        path = (root or self.root) / "qq-only" / "plugin_audit.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()
                if line.strip()]

    def problems_text(self, manager):
        return "\n".join(manager.take_load_problems())

    def order_lines(self, path):
        if not path.is_file():
            return []
        return path.read_text("utf-8").split()

    # ── 验收 1：拓扑顺序 a,b,c ───────────────────────────────────────────────
    def test_topological_mount_order(self):
        order = self.root / "order.txt"
        depends = {"a": [], "b": ["a"], "c": ["a", "b"]}
        for pid in ("c", "b", "a"):        # 建目录顺序故意与依赖顺序相反
            self.add_mod(pid, depends=depends[pid],
                         entry_code=ORDER_SRC.format(pid=pid,
                                                     order_file=str(order)))
        manager = PluginManager(self.root)
        self.assertEqual(self.problems_text(manager), "")
        self.assertEqual(self.order_lines(order), ["a", "b", "c"])
        # 挂载顺序就是 _mod_entries 的顺序，也就是 post_load 的调用顺序
        self.assertEqual(list(manager._mod_entries), ["a", "b", "c"])
        for pid in ("a", "b", "c"):
            self.assertIn(pid, manager._plugins)

    # ── 验收 2：缺依赖 ──────────────────────────────────────────────────────
    def test_missing_dependency_refused_others_unaffected(self):
        self.add_mod("good_mod")
        self.add_mod("needy", depends=["nope"])
        manager = PluginManager(self.root)
        problems = self.problems_text(manager)
        self.assertIn("dependency-missing", problems)
        self.assertIn("nope", problems)
        self.assertNotIn("needy", manager._plugins)
        self.assertIn("good_mod", manager._plugins)
        refused = [e for e in self.audit_events() if e["event"] == "load-refused"]
        self.assertEqual([(e["plugin"], e["code"]) for e in refused],
                         [("needy", "dependency-missing")])

    # ── 验收 3：成环 ────────────────────────────────────────────────────────
    def test_dependency_cycle_refused_with_path(self):
        self.add_mod("x", depends=["y"])
        self.add_mod("y", depends=["x"])
        manager = PluginManager(self.root)
        problems = self.problems_text(manager)
        self.assertEqual(problems.count("dependency-cycle"), 2)
        self.assertIn("x -> y -> x", problems)
        self.assertNotIn("x", manager._plugins)
        self.assertNotIn("y", manager._plugins)
        self.assertEqual(dict(manager._mod_entries), {})

    # ── 验收 4：依赖内置插件 / 依赖不存在的内置 id ────────────────────────────
    def test_depends_on_builtin_plugin_mounts(self):
        self.assertIn("keyword_reply", PLUGIN_MODULES)
        self.add_mod("uses_builtin", depends=["keyword_reply"])
        manager = PluginManager(self.root)
        self.assertEqual(self.problems_text(manager), "")
        self.assertIn("uses_builtin", manager._plugins)

    def test_depends_on_missing_builtin_refused(self):
        self.assertNotIn("no_such_builtin", PLUGIN_MODULES)
        self.add_mod("uses_ghost", depends=["no_such_builtin"])
        manager = PluginManager(self.root)
        problems = self.problems_text(manager)
        self.assertIn("dependency-missing", problems)
        self.assertIn("no_such_builtin", problems)
        self.assertNotIn("uses_ghost", manager._plugins)

    # ── 验收 5：字段非法 ────────────────────────────────────────────────────
    def test_bad_depends_field_string(self):
        root = self.fresh_root()
        self.add_mod("dep_str", depends="a", base=root / "custom_plugins")
        manager = PluginManager(root)
        problems = self.problems_text(manager)
        self.assertIn("[bad_depends]", problems)
        self.assertIn("必须是 mod id 列表", problems)
        self.assertNotIn("dep_str", manager._plugins)

    def test_bad_depends_field_non_string_item(self):
        root = self.fresh_root()
        self.add_mod("dep_int", depends=[1], base=root / "custom_plugins")
        manager = PluginManager(root)
        problems = self.problems_text(manager)
        self.assertIn("[bad_depends]", problems)
        self.assertIn("每一项都必须是非空字符串 mod id", problems)
        self.assertNotIn("dep_int", manager._plugins)

    def test_bad_depends_field_empty_string_item(self):
        root = self.fresh_root()
        self.add_mod("dep_empty", depends=[""], base=root / "custom_plugins")
        manager = PluginManager(root)
        problems = self.problems_text(manager)
        self.assertIn("[bad_depends]", problems)
        self.assertIn("每一项都必须是非空字符串 mod id", problems)
        self.assertNotIn("dep_empty", manager._plugins)

    def test_depends_on_self_refused(self):
        root = self.fresh_root()
        self.add_mod("dep_self", depends=["dep_self"], base=root / "custom_plugins")
        manager = PluginManager(root)
        problems = self.problems_text(manager)
        self.assertIn("[dependency-self]", problems)
        self.assertIn("mod 不能依赖自身", problems)
        self.assertNotIn("dep_self", manager._plugins)
        refused = [e for e in self.audit_events(root) if e["event"] == "load-refused"]
        self.assertEqual([(e["plugin"], e["code"]) for e in refused],
                         [("dep_self", "dependency-self")])

    # ── 验收 6：老 mod 零变化 ───────────────────────────────────────────────
    def test_old_mod_without_depends_unchanged(self):
        self.add_mod("old_mod")
        manager = PluginManager(self.root)
        self.assertEqual(self.problems_text(manager), "")
        self.assertEqual(len(manager._plugins), len(PLUGIN_MODULES) + 1)
        self.assertEqual(len([pid for pid in manager._plugins
                              if pid in PLUGIN_MODULES]), len(PLUGIN_MODULES))
        self.assertIn("old_mod", manager._plugins)
        manifest = manager._mod_manifests["old_mod"]
        self.assertEqual(manifest["depends"], [])      # 缺字段默认空列表
        mounted = [e for e in self.audit_events() if e["event"] == "mounted"]
        self.assertEqual([e["plugin"] for e in mounted], ["old_mod"])


if __name__ == "__main__":
    unittest.main()
