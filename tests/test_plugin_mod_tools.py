"""mod 自定义工具：注册入口、权限、工具环可见、调用三闸（权限/超时自愈/审计）、写确认。"""
import asyncio
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src import tool_loop
from src.plugins import ModToolError
from src.plugins.manager import PluginManager

MOD_SRC = '''import time

from . import PluginBase, PluginMeta


class ToolMod(PluginBase):
    META = PluginMeta(id="__PID__", name="工具 mod", category="utility", description="x")

    def post_load(self):
        self.register_tool("echo", self.echo, description="回显",
                           parameters={"type": "object",
                                       "properties": {"text": {"type": "string"}}})
        self.register_tool("boom", self.boom)
        self.register_tool("slow", self.slow)
        self.register_tool("poke", self.poke, description="写操作", write=True)

    def _bump(self, key):
        data = self.load_state("calls.json", {})
        data[key] = int(data.get(key, 0)) + 1
        self.save_state("calls.json", data)

    def echo(self, arguments, runtime):
        self.save_state("facade.json", {"type": type(runtime).__name__,
                                        "mod_id": getattr(runtime, "mod_id", "")})
        return "echo:" + str(arguments.get("text", ""))

    def boom(self, arguments, runtime):
        self._bump("boom")
        raise RuntimeError("handler 坏了")

    def slow(self, arguments, runtime):
        time.sleep(0.5)
        return "慢"

    def poke(self, arguments, runtime):
        self._bump("write")
        return "poked"
'''

PLAIN_SRC = '''from . import PluginBase, PluginMeta


class Plain(PluginBase):
    META = PluginMeta(id="__PID__", name="无权限 mod", category="utility", description="x")
'''

LAZY_SRC = '''from . import PluginBase, PluginMeta


class Lazy(PluginBase):
    META = PluginMeta(id="__PID__", name="懒注册", category="utility", description="x")

    def on_ingest(self, message, runtime):
        if not getattr(self, "_registered", False):
            runtime.register_tool("later", lambda arguments, rt: "later-ok")
            self._registered = True
        return None
'''

BARE_SRC = '''from . import PluginBase, PluginMeta


class Bare(PluginBase):
    META = PluginMeta(id="__PID__", name="裸插件", category="utility", description="x")

    def on_ingest(self, message, runtime):
        return None
'''


class ModToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.plugins_dir = self.root / "custom_plugins"
        self.plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def add_mod(self, pid="tool_mod", permissions=("register_tool",), source=MOD_SRC):
        folder = self.plugins_dir / pid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "mod.py").write_text(source.replace("__PID__", pid), encoding="utf-8")
        manifest = {"id": pid, "api_version": 1, "entry": "mod.py",
                    "permissions": list(permissions)}
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        return folder

    def add_bare(self, pid, source=BARE_SRC):
        (self.plugins_dir / (pid + ".py")).write_text(
            source.replace("__PID__", pid), encoding="utf-8")
        (self.root / "custom_plugins.json").write_text(
            json.dumps({"plugins": [pid]}), encoding="utf-8")

    def load(self):
        manager = PluginManager(self.root)
        self.assertEqual(manager.take_load_problems(), [])
        return manager

    def registry(self, manager):
        runtime = SimpleNamespace(plugins=manager, config=None, data_root=self.root)
        return tool_loop.registry_for(runtime)

    def audit_rows(self, event="tool-call"):
        path = self.root / "qq-only" / "plugin_audit.jsonl"
        if not path.is_file():
            return []
        rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()
                if line.strip()]
        return [row for row in rows if row.get("event") == event]

    def state(self, pid="tool_mod"):
        path = self.root / "plugin_state" / pid / "calls.json"
        return json.loads(path.read_text("utf-8")) if path.is_file() else {}

    # ── 注册面 ──────────────────────────────────────────────────────────────
    def test_register_success_and_names(self):
        self.add_mod()
        manager = self.load()
        host = manager._mod_tool_hosts["tool_mod"]
        plugin = manager._plugins["tool_mod"]
        self.assertEqual(host.full_name("echo"), "mod__tool_mod__echo")
        self.assertEqual(host.registry_name("echo"), "tool_mod__echo")
        self.assertEqual(sorted(tool["name"] for tool in plugin.list_tools()),
                         ["boom", "echo", "poke", "slow"])

    def test_registry_for_sees_mod_tools(self):
        self.add_mod()
        manager = self.load()
        names = self.registry(manager).names()
        self.assertIn("tool_mod__echo", names)
        self.assertIn("tool_mod__poke", names)

    def test_missing_permission_rejected(self):
        self.add_mod(permissions=(), source=PLAIN_SRC)
        manager = self.load()
        plugin = manager._plugins["tool_mod"]
        with self.assertRaises(ModToolError) as caught:
            plugin.register_tool("nope", lambda arguments, runtime: "x")
        message = str(caught.exception)
        self.assertIn("register_tool", message)
        self.assertIn("plugin.json", message)
        self.assertEqual(plugin.list_tools(), [])

    def test_duplicate_name_rejected(self):
        self.add_mod()
        manager = self.load()
        plugin = manager._plugins["tool_mod"]
        with self.assertRaises(ModToolError) as caught:
            plugin.register_tool("echo", lambda arguments, runtime: "第二个")
        self.assertIn("重名", str(caught.exception))
        self.assertEqual(len(plugin.list_tools()), 4)

    def test_overlong_name_rejected(self):
        self.add_mod()
        manager = self.load()
        plugin = manager._plugins["tool_mod"]
        with self.assertRaises(ModToolError) as caught:
            plugin.register_tool("x" * 60, lambda arguments, runtime: "x")
        self.assertIn("64", str(caught.exception))

    def test_bad_name_format_rejected(self):
        self.add_mod()
        manager = self.load()
        plugin = manager._plugins["tool_mod"]
        for bad in ("has space", "a.b", "中文名", "", "tab\there"):
            with self.assertRaises(ModToolError, msg=bad):
                plugin.register_tool(bad, lambda arguments, runtime: "x")
        self.assertEqual(len(plugin.list_tools()), 4)

    def test_list_tools_shape(self):
        self.add_mod()
        manager = self.load()
        tools = manager._plugins["tool_mod"].list_tools()
        for tool in tools:
            self.assertEqual(sorted(tool), ["description", "name", "parameters"])
            self.assertEqual(tool["parameters"]["type"], "object")
            self.assertIsInstance(tool["description"], str)
        echo = [tool for tool in tools if tool["name"] == "echo"][0]
        self.assertEqual(sorted(echo["parameters"]["properties"]), ["text"])
        poke = [tool for tool in tools if tool["name"] == "poke"][0]
        self.assertIn("confirm", poke["description"])

    # ── 调用面 ──────────────────────────────────────────────────────────────
    def test_call_ok_returns_value_and_facade(self):
        self.add_mod()
        manager = self.load()
        plugin = manager._plugins["tool_mod"]
        result = plugin.call_tool("echo", {"text": "hi"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], "echo:hi")
        self.assertEqual(result["tool"], "mod__tool_mod__echo")
        self.assertEqual(result["code"], "ok")
        facade = json.loads((self.root / "plugin_state" / "tool_mod" / "facade.json")
                            .read_text("utf-8"))
        self.assertEqual(facade["type"], "ModRuntime")
        self.assertEqual(facade["mod_id"], "tool_mod")

    def test_tool_loop_invoke_end_to_end(self):
        self.add_mod()
        manager = self.load()
        spec = self.registry(manager).get("tool_mod__echo")
        ok, value, error = asyncio.run(
            tool_loop._invoke(spec.handler, spec.raw_name, {"text": "环"}, 5))
        self.assertTrue(ok, error)
        self.assertEqual(value["value"], "echo:环")

    def test_audit_line_fields(self):
        self.add_mod()
        manager = self.load()
        manager._plugins["tool_mod"].call_tool("echo", {"text": "a"})
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for key in ("ts", "plugin", "event", "tool", "write", "ok", "code",
                    "duration_ms"):
            self.assertIn(key, row)
        self.assertEqual(row["plugin"], "tool_mod")
        self.assertEqual(row["event"], "tool-call")
        self.assertEqual(row["tool"], "mod__tool_mod__echo")
        self.assertFalse(row["write"])
        self.assertTrue(row["ok"])
        self.assertEqual(row["code"], "ok")
        self.assertIsInstance(row["duration_ms"], int)

    def test_unknown_tool_refused(self):
        self.add_mod()
        manager = self.load()
        result = manager._plugins["tool_mod"].call_tool("missing", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "unknown-tool")
        self.assertEqual(self.audit_rows("tool-call")[0]["code"], "unknown-tool")

    def test_exception_counted_and_audited(self):
        self.add_mod()
        manager = self.load()
        result = manager._plugins["tool_mod"].call_tool("boom", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "exception")
        self.assertIn("handler 坏了", result["error"]["message"])
        self.assertEqual(manager._hook_failures["tool_mod"], 1)
        row = self.audit_rows("tool-call")[0]
        self.assertFalse(row["ok"])
        self.assertEqual(row["code"], "exception")

    def test_timeout_refused_and_counted(self):
        self.add_mod()
        manager = self.load()
        with mock.patch("src.plugins.manager.MOD_HOOK_TIMEOUT", 0.05):
            result = manager._plugins["tool_mod"].call_tool("slow", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "timeout")
        self.assertEqual(manager._hook_failures["tool_mod"], 1)
        self.assertEqual(self.audit_rows("tool-call")[0]["code"], "timeout")

    def test_write_without_confirm_not_executed(self):
        self.add_mod()
        manager = self.load()
        plugin = manager._plugins["tool_mod"]
        # 旧断言把 {"confirm": "true"} 列在这里，等于把"闸门认字符串、mod 层只认 is True"
        # 这个口径漂移固化成规格：那条路径上闸门放行、这里再拒一次，模型被连拒两次。
        # 现在两处共用 src/plugins/__init__.py:has_confirm，字符串真值一律算确认
        # （见下一条用例），这里只剩真正"没确认"的写法。
        for arguments in ({}, {"confirm": False}, {"confirm": "0"}, {"confirm": 0},
                          {"confirm": ""}, {"confirm": None}):
            result = plugin.call_tool("poke", arguments)
            self.assertFalse(result["ok"], arguments)
            self.assertEqual(result["code"], "confirm-required")
        self.assertEqual(self.state(), {})
        rows = self.audit_rows()
        self.assertEqual([row["code"] for row in rows], ["confirm-required"] * 6)
        self.assertTrue(all(row["write"] for row in rows))
        self.assertTrue(all(row["ok"] is False for row in rows))

    def test_write_confirm_accepts_every_truthy_form(self):
        """与闸门同一张真值表：闸门认的确认写法，mod 层也必须放行（不许连拒两次）。"""
        self.add_mod()
        manager = self.load()
        plugin = manager._plugins["tool_mod"]
        forms = ({"confirm": True}, {"confirm": 1}, {"confirm": 1.0}, {"confirm": "true"},
                 {"confirm": " TRUE "}, {"confirm": "yes"}, {"confirm": "on"},
                 {"confirm": "开"}, {"confirm": "开启"})
        for arguments in forms:
            with self.subTest(arguments=arguments):
                result = plugin.call_tool("poke", arguments)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["code"], "ok")
        # 每种写法都真的执行了一次 handler（不是"放行但空跑"）
        self.assertEqual(self.state(), {"write": len(forms)})
        self.assertEqual([row["code"] for row in self.audit_rows()], ["ok"] * len(forms))

    def test_write_with_confirm_executes_and_audited(self):
        self.add_mod()
        manager = self.load()
        result = manager._plugins["tool_mod"].call_tool("poke", {"confirm": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], "poked")
        self.assertEqual(self.state(), {"write": 1})
        row = self.audit_rows()[0]
        self.assertTrue(row["write"])
        self.assertTrue(row["ok"])
        self.assertEqual(row["code"], "ok")

    def test_three_failures_auto_disable(self):
        self.add_mod()
        manager = self.load()
        plugin = manager._plugins["tool_mod"]
        for _ in range(3):
            self.assertEqual(plugin.call_tool("boom", {})["code"], "exception")
        self.assertFalse(plugin.enabled)
        self.assertEqual(plugin.list_tools(), [])
        self.assertEqual(self.registry(manager).names(), [])
        settings = json.loads((self.root / "plugin_settings.json").read_text("utf-8"))
        self.assertFalse(settings["tool_mod"]["enabled"])
        self.assertEqual(self.audit_rows("auto-disabled")[0]["plugin"], "tool_mod")
        result = plugin.call_tool("boom", {})
        self.assertEqual(result["code"], "disabled")

    # ── 老插件不受影响 ──────────────────────────────────────────────────────
    def test_lazy_register_from_hook_facade(self):
        self.add_mod(pid="lazy_mod", source=LAZY_SRC)
        manager = self.load()
        plugin = manager._plugins["lazy_mod"]
        self.assertEqual(plugin.list_tools(), [])
        runtime = SimpleNamespace(note=None)
        manager.run_on_ingest({"text": "hi"}, runtime)
        manager.run_on_ingest({"text": "hi"}, runtime)
        self.assertEqual([tool["name"] for tool in plugin.list_tools()], ["later"])
        self.assertTrue(plugin.call_tool("later", {})["ok"])

    def test_builtin_and_bare_plugins_have_no_tool_bridge(self):
        self.add_bare("legacy_bare")
        manager = self.load()
        self.assertIn("legacy_bare", manager._plugins)
        self.assertEqual(manager._mod_tool_hosts, {})
        for pid, plugin in manager._plugins.items():
            # qqagent_bridge 是既有的 JS 扩展桥，它本来就自己实现这两个方法；
            # 新增的挂载只对目录 mod 生效，内置插件一个都没多出这些属性。
            if pid == "qqagent_bridge":
                continue
            self.assertFalse(hasattr(plugin, "list_tools"), pid)
            self.assertFalse(hasattr(plugin, "call_tool"), pid)
            self.assertFalse(hasattr(plugin, "register_tool"), pid)
        self.assertEqual(self.registry(manager).names(), [])


if __name__ == '__main__':
    unittest.main()
