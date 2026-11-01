"""目录 mod 增量热重载：sync_mods() 的挂载 / 摘除 / 重挂 / 幂等 / 容错。"""
import json
import os
import pathlib
import shutil
import tempfile
import types
import unittest

from src.plugins.manager import PluginManager

HOT_SRC = '''from . import PluginBase, PluginMeta

MARK = {mark!r}
POST = {post_file!r}
INGEST = {ingest_file!r}
TOOL = {tool!r}


class Hot(PluginBase):
    META = PluginMeta(id="{pid}", name="{pid}", category="utility",
                      description="热重载探针")

    def post_load(self):
        with open(POST, "a", encoding="utf-8") as fh:
            fh.write("{pid}\\n")
        self.register_tool(TOOL, self._answer, description="探针工具")

    def _answer(self, name, arguments):
        return MARK

    def on_ingest(self, message, runtime):
        with open(INGEST, "a", encoding="utf-8") as fh:
            fh.write(MARK + "\\n")
        return MARK
'''

PLAIN_SRC = '''from . import PluginBase, PluginMeta


class Plain(PluginBase):
    META = PluginMeta(id="{pid}", name="{pid}", category="utility",
                      description="测试用 mod")

    def on_ingest(self, message, runtime):
        return None
'''

BAD_SRC = '''from . import PluginBase, PluginMeta


class Bad(PluginBase):
    META = PluginMeta(id="{pid}", name="{pid}", category="utility",
                      description="post_load 必炸的 mod")

    def post_load(self):
        raise RuntimeError("post_load 故意炸：这一条不许打断同轮其它 mod")
'''


class RecordingRegistry:
    """工具环的最小替身：只记下"哪个来源登记了哪些工具名"。"""

    def __init__(self):
        self.tables = {}

    def register_tools(self, source, tools, handler=None):
        self.tables[source] = [tool["name"] for tool in tools]
        return len(tools)


class ModSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.plugins_dir = self.root / "custom_plugins"
        self.plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    # ── 脚手架 ──────────────────────────────────────────────────────────────
    def add_mod(self, mod_id, depends=None, entry_code=None, manifest=None):
        folder = self.plugins_dir / mod_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "mod.py").write_text(
            entry_code if entry_code is not None
            else PLAIN_SRC.format(pid=mod_id), encoding="utf-8")
        data = {"id": mod_id, "api_version": 1, "entry": "mod.py"}
        if depends is not None:
            data["depends"] = depends
        data.update(manifest or {})
        (folder / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
        return folder

    def add_hot(self, mod_id="hot", mark="v1", tool="ping", depends=None):
        return self.add_mod(
            mod_id, depends=depends,
            manifest={"permissions": ["register_tool"]},
            entry_code=HOT_SRC.format(pid=mod_id, mark=mark, tool=tool,
                                      post_file=str(self.post_log),
                                      ingest_file=str(self.ingest_log)))

    def bump(self, path):
        """把 mtime 往前推 2 秒：只改内容时文件系统可能复用同一个时间戳。"""
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    def new_manager(self):
        manager = PluginManager(self.root)
        self.assertEqual(manager.take_load_problems(), [])
        return manager

    def audit_events(self):
        path = self.root / "qq-only" / "plugin_audit.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()
                if line.strip()]

    def problems_text(self, manager):
        return "\n".join(manager.take_load_problems())

    def registry_tables(self, manager):
        from src.tool_loop import register_plugin_tools
        registry = RecordingRegistry()
        register_plugin_tools(registry, manager)
        return registry.tables

    def post_lines(self):
        return self.post_log.read_text("utf-8").split() if self.post_log.is_file() else []

    def ingest_lines(self):
        if not self.ingest_log.is_file():
            return []
        return self.ingest_log.read_text("utf-8").split()

    def runtime_stub(self):
        return types.SimpleNamespace(note=lambda *a, **k: None)

    # ── 验收 7：增量热重载三步 ──────────────────────────────────────────────
    def test_hot_reload_add_reload_remove(self):
        self.post_log = self.root / "post.log"
        self.ingest_log = self.root / "ingest.log"
        manager = self.new_manager()
        self.add_hot(mark="v1", tool="ping")

        # 第一步：新目录 → 挂载，钩子生效
        report = manager.sync_mods()
        self.assertEqual(report["added"], ["hot"])
        self.assertEqual(report["actions"], 1)
        self.assertIn("hot", manager._plugins)
        self.assertEqual(self.post_lines(), ["hot"])
        plugin = manager._plugins["hot"]
        self.assertEqual(
            manager._call_mod_hook(plugin, "on_ingest", plugin.on_ingest,
                                   self.runtime_stub(), {"text": "hi"}), "v1")
        manager.run_on_ingest({"text": "热重载探针消息"}, self.runtime_stub())
        self.assertIn("v1", self.ingest_lines())
        self.assertIn("ping", self.registry_tables(manager).get("hot", []))

        # 第二步：改入口文件 → 重挂，新实现生效，旧工具消失
        self.add_hot(mark="v2", tool="ping2")
        self.bump(self.plugins_dir / "hot" / "mod.py")
        report = manager.sync_mods()
        self.assertEqual(report["reloaded"], ["hot"])
        self.assertEqual(report["actions"], 1)
        self.assertEqual(self.post_lines(), ["hot", "hot"])
        reloaded = manager._plugins["hot"]
        self.assertIsNot(reloaded, plugin)
        self.assertEqual(
            manager._call_mod_hook(reloaded, "on_ingest", reloaded.on_ingest,
                                   self.runtime_stub(), {"text": "hi"}), "v2")
        self.assertIn("v2", self.ingest_lines())
        tables = self.registry_tables(manager)
        self.assertEqual(tables.get("hot"), ["ping2"])

        # 第三步：目录消失 → 摘除，工具从注册表消失，设置键保留
        self.assertIn("hot", manager._settings)
        shutil.rmtree(self.plugins_dir / "hot")
        report = manager.sync_mods()
        self.assertEqual(report["removed"], ["hot"])
        self.assertEqual(report["actions"], 1)
        self.assertNotIn("hot", manager._plugins)
        self.assertNotIn("hot", manager._mod_plugins)
        self.assertNotIn("hot", manager._mod_tool_hosts)
        self.assertNotIn("hot", manager._mod_entries)
        self.assertNotIn("hot", manager._mod_manifests)
        self.assertNotIn("hot", manager._mod_stamps)
        self.assertIn("hot", manager._settings)      # 摘除不删设置键
        self.assertNotIn("hot", self.registry_tables(manager))
        self.assertEqual(self.post_lines(), ["hot", "hot"])   # 摘除不重跑 post_load
        unmounted = [(e["plugin"], e["code"]) for e in self.audit_events()
                     if e["event"] == "mod-unmounted"]
        self.assertEqual(unmounted, [("hot", "file-changed"), ("hot", "dir-gone")])

    # ── 验收 8：连调两次，第二次零动作 ──────────────────────────────────────
    def test_sync_twice_second_round_is_noop(self):
        self.post_log = self.root / "post.log"
        self.ingest_log = self.root / "ingest.log"
        manager = self.new_manager()
        self.add_hot()
        first = manager.sync_mods()
        self.assertEqual(first["added"], ["hot"])
        audit_before = len(self.audit_events())
        posts_before = self.post_lines()

        second = manager.sync_mods()
        self.assertEqual(second["actions"], 0)
        for key in ("added", "removed", "reloaded", "rejected", "failed"):
            self.assertEqual(second[key], [], key)
        self.assertEqual(second["unchanged"], ["hot"])
        self.assertEqual(len(self.audit_events()), audit_before)   # 一条审计都没多写
        self.assertEqual(self.post_lines(), posts_before)          # post_load 没重跑

    # ── 验收 9：坏 mod 不打断同轮的好 mod ───────────────────────────────────
    def test_bad_mod_does_not_break_sync(self):
        self.post_log = self.root / "post.log"
        self.ingest_log = self.root / "ingest.log"
        manager = self.new_manager()
        self.add_mod("bad", entry_code=BAD_SRC.format(pid="bad"))
        self.add_hot(mod_id="good")

        report = manager.sync_mods()
        self.assertEqual(report["added"], ["good"])
        self.assertEqual(report["failed"], ["bad"])
        self.assertIn("good", manager._plugins)
        self.assertNotIn("bad", manager._plugins)
        problems = self.problems_text(manager)
        self.assertIn("bad", problems)
        self.assertIn("初始化失败", problems)

        # 同一个坏文件不再每轮重试
        again = manager.sync_mods()
        self.assertEqual(again["failed"], [])
        self.assertEqual(again["actions"], 0)

    # ── 依赖联动：被依赖的目录消失，依赖它的 mod 一并摘除 ────────────────────
    def test_removing_dependency_unmounts_dependent(self):
        self.post_log = self.root / "post.log"
        self.ingest_log = self.root / "ingest.log"
        manager = self.new_manager()
        self.add_mod("base")
        self.add_mod("leaf", depends=["base"])
        report = manager.sync_mods()
        self.assertEqual(report["added"], ["base", "leaf"])
        self.assertIn("leaf", manager._plugins)

        shutil.rmtree(self.plugins_dir / "base")
        report = manager.sync_mods()
        self.assertEqual(report["removed"], ["base", "leaf"])
        self.assertNotIn("leaf", manager._plugins)
        self.assertIn("dependency-missing", self.problems_text(manager))


if __name__ == "__main__":
    unittest.main()
