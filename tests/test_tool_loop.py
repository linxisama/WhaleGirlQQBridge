"""工具调用环（tool loop）单元测试。

本机 venv 没装 pytest，所以用 unittest 写（pytest 也能收集）。覆盖：工具表注册与静态
清单、配置开关、tools 请求体构造、多后端 tool_calls 解析、before/after 钩子拦截与改写、
超时与异常降级、轮数上限、审计脱敏。
"""
import asyncio
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src import ai_engine, tool_loop
from src.plugins.manager import PluginManager


ECHO_TOOL = {"name": "echo", "description": "回声",
             "parameters": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]}}


def echo(name, arguments):
    return {"echo": arguments.get("text", "")}


async def async_echo(name, arguments):
    return "async:" + str(arguments.get("text", ""))


def echo_registry():
    registry = tool_loop.ToolRegistry()
    registry.register_tools("builtin", [ECHO_TOOL], handler=echo)
    return registry


def tool_reply(name="echo", arguments=None, call_id="call_1", text=""):
    """造一条 OpenAI 形状的 assistant 回复（带 tool_calls）。"""
    if arguments is None:
        arguments = {"text": "你好"}
    return {"text": text,
            "message": {"role": "assistant", "content": text or None,
                        "tool_calls": [{"id": call_id, "type": "function",
                                        "function": {"name": name,
                                                     "arguments": json.dumps(
                                                         arguments, ensure_ascii=False)}}]},
            "content_blocks": [], "finish_reason": "tool_calls", "api_style": "openai"}


class FakeRuntime:
    """只实现工具环用到的那部分 runtime 接口。"""

    def __init__(self, root, config=None, owners=("u1",)):
        self.root = Path(root)
        self.data_root = Path(root)
        self.config = config or {}
        self.ai = None
        self.plugins = None
        self.notes = []
        self._owners = set(owners)

    def owner_ids(self):
        return set(self._owners)

    def note(self, text):
        self.notes.append(str(text))


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = json.dumps(data, ensure_ascii=False)

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeClient:
    """按脚本依次返回响应，同时记下每次请求体。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    async def post(self, url, **kwargs):
        self.payloads.append(kwargs.get("json"))
        index = min(len(self.payloads) - 1, len(self.responses) - 1)
        return FakeResponse(self.responses[index])

    async def aclose(self):
        return None


def engine_with(responses, api_url="https://example.test/v1", api_key="fake",
                model="test-model", api_style="auto"):
    """造一个真 AIEngine，但把 httpx 客户端换成脚本化假客户端。"""
    engine = ai_engine.AIEngine(api_url, api_key, model=model, api_style=api_style)
    client = FakeClient(responses)

    async def fake_get_client(self):
        return client

    patcher = mock.patch.object(ai_engine.AIEngine, "_get_client", fake_get_client)
    patcher.start()
    return engine, client, patcher


class ScriptedEngine:
    """工具环用的引擎替身：按脚本返回 dict，并记录每次调用的关键字参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.configured = True
        self.api_style = "openai"
        self.last_error = ""
        self.model = "fake-model"

    async def chat(self, messages, **kwargs):
        self.calls.append(dict(kwargs, messages=[dict(item) for item in messages]))
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[index]


class RegistryTests(unittest.TestCase):
    def test_register_tools_exposes_openai_spec(self):
        registry = echo_registry()
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry.names(), ["echo"])
        spec = registry.specs()[0]
        self.assertEqual(spec["type"], "function")
        self.assertEqual(spec["function"]["name"], "echo")
        self.assertEqual(spec["function"]["description"], "回声")
        self.assertEqual(spec["function"]["parameters"]["type"], "object")

    def test_handler_is_required_for_availability(self):
        registry = tool_loop.ToolRegistry()
        registry.register_tools("builtin", [ECHO_TOOL])
        self.assertEqual(len(registry), 0)
        self.assertIsNone(registry.get("echo"))

    def test_non_builtin_source_is_namespaced(self):
        registry = tool_loop.ToolRegistry()
        registry.register_tools("market", [{"name": "web.fetch"}], handler=echo)
        self.assertEqual(registry.names(), ["market__web_fetch"])
        self.assertEqual(registry.get("market__web_fetch").raw_name, "web.fetch")

    def test_duplicate_names_get_a_suffix(self):
        registry = tool_loop.ToolRegistry()
        registry.register_tools("builtin", [ECHO_TOOL], handler=echo)
        registry.register_tools("builtin", [ECHO_TOOL], handler=echo)
        self.assertEqual(sorted(registry.names()), ["echo", "echo_2"])

    def test_unregister_source(self):
        registry = tool_loop.ToolRegistry()
        registry.register_tools("market", [{"name": "a"}, {"name": "b"}], handler=echo)
        registry.register_tools("builtin", [ECHO_TOOL], handler=echo)
        registry.unregister_source("market")
        self.assertEqual(registry.names(), ["echo"])

    def test_manifest_can_disable_and_override(self):
        registry = echo_registry()
        self.assertEqual(registry.apply_manifest([{"name": "echo", "enabled": False}]), 1)
        self.assertEqual(registry.names(), [])
        registry.apply_manifest([{"name": "echo", "enabled": True, "description": "新描述"}])
        self.assertEqual(registry.names(), ["echo"])
        self.assertEqual(registry.specs()[0]["function"]["description"], "新描述")

    def test_manifest_declared_tool_is_not_dispatched(self):
        registry = echo_registry()
        registry.apply_manifest([{"name": "later_tool", "description": "以后才实现"}])
        self.assertIn("later_tool", registry.declared)
        self.assertNotIn("later_tool", registry.names())

    def test_manifest_file_is_optional(self):
        runtime = FakeRuntime(tempfile.mkdtemp())
        self.assertEqual(tool_loop.load_manifest(tool_loop.manifest_path(runtime)), [])

    def test_plugin_tools_are_registered_by_duck_typing(self):
        class Bridge:
            def list_tools(self):
                return [{"name": "web_fetch", "description": "抓网页"}]

            def call_tool(self, name, arguments):
                return {"url": arguments.get("url"), "text": "ok"}

        class NoTools:
            pass

        plugins = types.SimpleNamespace(_plugins={"qqagent_bridge": Bridge(), "other": NoTools()})
        registry = tool_loop.ToolRegistry()
        self.assertEqual(tool_loop.register_plugin_tools(registry, plugins), 1)
        self.assertEqual(registry.names(), ["qqagent_bridge__web_fetch"])

    def test_registry_for_caches_on_runtime(self):
        runtime = FakeRuntime(tempfile.mkdtemp())
        runtime.plugins = types.SimpleNamespace(_plugins={})
        first = tool_loop.registry_for(runtime)
        self.assertIs(first, tool_loop.registry_for(runtime))
        self.assertEqual(tool_loop.register_tools(runtime, "builtin", [ECHO_TOOL], handler=echo), 1)
        self.assertEqual(first.names(), ["echo"])


class SettingsTests(unittest.TestCase):
    def test_default_is_off(self):
        settings = tool_loop.loop_settings({})
        self.assertFalse(settings.enabled)
        self.assertTrue(settings.owner_only)
        self.assertEqual(settings.max_rounds, 3)
        self.assertEqual(settings.tool_timeout, 15.0)
        self.assertEqual(settings.total_budget, 40.0)

    def test_switch_reads_top_level_and_alias(self):
        self.assertTrue(tool_loop.loop_settings({"toolLoop": True}).enabled)
        self.assertTrue(tool_loop.loop_settings({"toolLoop": "true"}).enabled)
        self.assertTrue(tool_loop.loop_settings({"ai": {"tool_loop": "on"}}).enabled)

    def test_values_are_clamped(self):
        high = tool_loop.loop_settings({"toolLoop": True, "toolLoopMaxRounds": 99,
                                        "toolLoopToolTimeout": 999})
        self.assertEqual(high.max_rounds, 5)
        self.assertEqual(high.tool_timeout, 60.0)
        low = tool_loop.loop_settings({"toolLoopMaxRounds": 0, "toolLoopToolTimeout": 0})
        self.assertEqual(low.max_rounds, 1)
        self.assertEqual(low.tool_timeout, 1.0)

    def test_turn_budget_can_be_pinned(self):
        settings = tool_loop.loop_settings({"toolLoopTurnBudget": 12})
        self.assertEqual(settings.total_budget, 12.0)


class ParseTests(unittest.TestCase):
    def test_openai_string_arguments(self):
        calls = tool_loop.parse_tool_calls(tool_reply(arguments={"text": "你好"}))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_1")
        self.assertEqual(calls[0].name, "echo")
        self.assertEqual(calls[0].arguments, {"text": "你好"})
        self.assertEqual(calls[0].arguments_error, "")

    def test_openai_dict_arguments(self):
        reply = {"message": {"tool_calls": [{"id": "c9", "function": {
            "name": "echo", "arguments": {"text": "已解析"}}}]}}
        calls = tool_loop.parse_tool_calls(reply)
        self.assertEqual(calls[0].arguments, {"text": "已解析"})
        self.assertEqual(calls[0].id, "c9")

    def test_legacy_function_call(self):
        reply = {"message": {"function_call": {"name": "echo",
                                                  "arguments": {"text": "老式"}}}}
        calls = tool_loop.parse_tool_calls(reply)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "echo")
        self.assertEqual(calls[0].arguments, {"text": "老式"})

    def test_anthropic_tool_use_blocks(self):
        reply = {"content_blocks": [{"type": "text", "text": "我查一下"},
                                    {"type": "tool_use", "id": "toolu_1",
                                     "name": "echo", "input": {"text": "hi"}}]}
        calls = tool_loop.parse_tool_calls(reply)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "toolu_1")
        self.assertEqual(calls[0].arguments, {"text": "hi"})

    def test_flat_shape(self):
        reply = {"name": "echo", "arguments": {"text": "扁平"}}
        calls = tool_loop.parse_tool_calls(reply)
        self.assertEqual(calls[0].name, "echo")
        self.assertEqual(calls[0].arguments, {"text": "扁平"})

    def test_bad_json_keeps_the_call_with_an_error(self):
        reply = {"message": {"tool_calls": [{"id": "c1", "function": {
            "name": "echo", "arguments": "{oops"}}]}}
        calls = tool_loop.parse_tool_calls(reply)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].arguments, {})
        self.assertTrue(calls[0].arguments_error)

    def test_plain_text_has_no_calls(self):
        self.assertEqual(tool_loop.parse_tool_calls({"text": "就是聊天"}), [])
        self.assertEqual(tool_loop.parse_tool_calls("纯字符串"), [])
        self.assertEqual(tool_loop.parse_tool_calls(None), [])


class PayloadTests(unittest.TestCase):
    def test_tools_are_added_only_when_given(self):
        engine, _client, patcher = engine_with([{"choices": []}])
        try:
            messages = [{"role": "user", "content": "在吗"}]
            plain = engine._build_payload(messages, "系统", 0.7)
            with_tools = engine._build_payload(messages, "系统", 0.7,
                                               tools=[ECHO_TOOL])
        finally:
            patcher.stop()
        self.assertNotIn("tools", plain)
        self.assertEqual(with_tools["tools"], [ECHO_TOOL])

    def test_anthropic_style_does_not_take_openai_tools(self):
        engine, _client, patcher = engine_with([{"content": []}],
                                               api_url="https://api.anthropic.com/v1/messages",
                                               api_style="anthropic")
        try:
            payload = engine._build_payload([{"role": "user", "content": "在吗"}], "系统", 0.7,
                                            tools=[ECHO_TOOL])
        finally:
            patcher.stop()
        self.assertNotIn("tools", payload)


class ChatWithMessageTests(unittest.TestCase):
    def test_tool_calls_reply_is_not_treated_as_empty(self):
        data = {"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "echo",
                                         "arguments": json.dumps({"text": "你好"},
                                                                 ensure_ascii=False)}}]},
            "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
        engine, client, patcher = engine_with([data])
        try:
            reply = asyncio.run(engine.chat([{"role": "user", "content": "在吗"}],
                                            with_message=True))
        finally:
            patcher.stop()
        self.assertIsInstance(reply, dict)
        self.assertEqual(reply["text"], "")
        self.assertEqual(engine.last_error, "")
        self.assertEqual(len(client.payloads), 1)
        calls = tool_loop.parse_tool_calls(reply)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].arguments, {"text": "你好"})

    def test_plain_reply_stays_a_string_by_default(self):
        data = {"choices": [{"message": {"role": "assistant", "content": "好的"},
                             "finish_reason": "stop"}], "usage": {}}
        engine, _client, patcher = engine_with([data])
        try:
            plain = asyncio.run(engine.chat([{"role": "user", "content": "在吗"}]))
            wrapped = asyncio.run(engine.chat([{"role": "user", "content": "在吗"}],
                                              with_message=True))
        finally:
            patcher.stop()
        self.assertEqual(plain, "好的")
        self.assertEqual(wrapped["text"], "好的")
        self.assertEqual(wrapped["finish_reason"], "stop")

    def test_anthropic_tool_use_is_visible_via_with_message(self):
        data = {"content": [{"type": "text", "text": "我查一下"},
                            {"type": "tool_use", "id": "toolu_1", "name": "echo",
                             "input": {"text": "hi"}}],
                "stop_reason": "tool_use", "usage": {}}
        engine, _client, patcher = engine_with([data],
                                               api_url="https://api.anthropic.com/v1/messages",
                                               api_style="anthropic")
        try:
            reply = asyncio.run(engine.chat([{"role": "user", "content": "在吗"}],
                                            with_message=True))
        finally:
            patcher.stop()
        self.assertEqual(reply["text"], "我查一下")
        self.assertEqual(engine.last_error, "")
        calls = tool_loop.parse_tool_calls(reply)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "toolu_1")


class GatePlugin:
    enabled = True
    META = types.SimpleNamespace(id="gate")

    def __init__(self):
        self.seen = []

    def on_before_tool(self, tool, arguments, runtime):
        self.seen.append(("before", tool, dict(arguments)))
        if arguments.get("text") == "block":
            return {"block": True, "reason": "测试拦截"}
        return {"arguments": dict(arguments, text="改写后的")}

    def on_after_tool(self, tool, arguments, value, runtime):
        self.seen.append(("after", tool))
        return {"wrapped": value}


class HookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = FakeRuntime(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def manager(self):
        manager = PluginManager(self.root / "plugin-data")
        plugin = GatePlugin()
        manager._plugins["gate"] = plugin
        return manager, plugin

    def audit_lines(self):
        path = self.root / "data/qq-only/tool-loop-audit.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_before_tool_hook_can_block_a_call(self):
        manager, plugin = self.manager()
        calls = tool_loop.parse_tool_calls(tool_reply(arguments={"text": "block"}))
        outcome = asyncio.run(tool_loop.run_calls(echo_registry(), manager, self.runtime, calls,
                                                 tool_loop.LoopSettings(enabled=True),
                                                 context={"sender": "u1"}))
        text = outcome["messages"][0][2]
        self.assertIn("被安全策略拦下", text)
        self.assertIn("测试拦截", text)
        self.assertEqual(plugin.seen[0], ("before", "echo", {"text": "block"}))
        self.assertEqual(self.audit_lines()[-1]["phase"], "blocked")

    def test_before_tool_can_rewrite_and_after_tool_can_replace(self):
        manager, _plugin = self.manager()
        calls = tool_loop.parse_tool_calls(tool_reply(arguments={"text": "原参数"}))
        outcome = asyncio.run(tool_loop.run_calls(echo_registry(), manager, self.runtime, calls,
                                                 tool_loop.LoopSettings(enabled=True)))
        self.assertEqual(json.loads(outcome["messages"][0][2]),
                         {"wrapped": {"echo": "改写后的"}})

    def test_manager_hook_contract_is_what_tool_loop_expects(self):
        """:721 与 :748 的返回契约直接对一遍，别让工具环和 manager 走偏。"""
        manager, _plugin = self.manager()
        hint = asyncio.run(manager.run_on_before_tool("echo", {"text": "block"}, self.runtime))
        self.assertTrue(hint["block"])
        self.assertEqual(hint["reason"], "测试拦截")
        replaced = asyncio.run(manager.run_on_after_tool("echo", {"text": "x"}, "原结果",
                                                         self.runtime))
        self.assertEqual(replaced, {"wrapped": "原结果"})


class InvokeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = FakeRuntime(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def run_one(self, registry, name="echo", arguments=None, settings=None):
        calls = tool_loop.parse_tool_calls(tool_reply(name=name, arguments=arguments or {}))
        outcome = asyncio.run(tool_loop.run_calls(registry, None, self.runtime, calls,
                                                 settings or tool_loop.LoopSettings(enabled=True)))
        return outcome["messages"][0][2]

    def test_async_handler_is_awaited(self):
        registry = tool_loop.ToolRegistry()
        registry.register_tools("builtin", [ECHO_TOOL], handler=async_echo)
        self.assertEqual(self.run_one(registry, arguments={"text": "嘿"}), "async:嘿")

    def test_slow_tool_times_out(self):
        async def slow(name, arguments):
            await asyncio.sleep(0.6)
            return "太慢了"

        registry = tool_loop.ToolRegistry()
        registry.register_tools("builtin", [{"name": "slow_tool"}], handler=slow)
        settings = tool_loop.LoopSettings(enabled=True, tool_timeout=0.05)
        text = self.run_one(registry, name="slow_tool", settings=settings)
        self.assertIn("工具执行失败", text)
        self.assertIn("工具超时", text)

    def test_failing_tool_degrades_to_text(self):
        def boom(name, arguments):
            raise ValueError("坏了")

        registry = tool_loop.ToolRegistry()
        registry.register_tools("builtin", [{"name": "boom"}], handler=boom)
        text = self.run_one(registry, name="boom")
        self.assertIn("工具执行失败", text)
        self.assertIn("ValueError", text)
        self.assertIn("坏了", text)

    def test_unknown_tool_is_reported(self):
        text = self.run_one(echo_registry(), name="nope")
        self.assertIn("没有这个工具：nope", text)

    def test_bad_arguments_are_reported_without_executing(self):
        calls = tool_loop.parse_tool_calls({"message": {"tool_calls": [{"id": "c1", "function": {
            "name": "echo", "arguments": "{oops"}}]}})
        outcome = asyncio.run(tool_loop.run_calls(echo_registry(), None, self.runtime, calls,
                                                 tool_loop.LoopSettings(enabled=True)))
        self.assertIn("参数解析失败", outcome["messages"][0][2])

    def test_audit_redacts_sensitive_arguments(self):
        registry = tool_loop.ToolRegistry()
        registry.register_tools("builtin", [{"name": "secret_tool"}],
                                handler=lambda name, arguments: "ok")
        self.run_one(registry, name="secret_tool",
                     arguments={"api_key": "sk-123", "text": "hi"})
        path = self.root / "data/qq-only/tool-loop-audit.jsonl"
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("sk-123", raw)
        record = json.loads(raw.strip().splitlines()[-1])
        self.assertEqual(record["tool"], "secret_tool")
        self.assertEqual(record["arguments"]["api_key"], "***")
        self.assertEqual(record["arguments"]["text"], "hi")
        self.assertEqual(record["phase"], "tool")


class LoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = FakeRuntime(self.root, config={"toolLoop": True})
        self.runtime.tool_registry = echo_registry()

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_limit_forces_a_text_reply(self):
        settings = tool_loop.LoopSettings(enabled=True, max_rounds=2, round_timeout=5.0,
                                          turn_budget=20.0)
        engine = ScriptedEngine([tool_reply(call_id="call_1"),
                                 tool_reply(call_id="call_2"),
                                 {"text": "最终文本", "message": {}, "content_blocks": []}])
        text = asyncio.run(tool_loop.respond_with_tools(
            engine, self.runtime, [{"role": "user", "content": "在吗"}],
            system_prompt="系统提示", settings=settings))
        self.assertEqual(text, "最终文本")
        self.assertEqual(len(engine.calls), 3)
        self.assertIsNotNone(engine.calls[0]["tools"])
        self.assertIsNone(engine.calls[-1]["tools"])
        self.assertEqual(engine.calls[0]["tools"][0]["function"]["name"], "echo")
        tool_message = engine.calls[1]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertIn("echo", tool_message["content"])
        self.assertIn(tool_loop.TOOL_HINT, engine.calls[0]["system_prompt"])

    def test_tool_failure_still_produces_a_reply(self):
        settings = tool_loop.LoopSettings(enabled=True, max_rounds=2, round_timeout=5.0,
                                          turn_budget=20.0)
        engine = ScriptedEngine([tool_reply(name="nope"),
                                 {"text": "那就先这样", "message": {}, "content_blocks": []}])
        text = asyncio.run(tool_loop.respond_with_tools(
            engine, self.runtime, [{"role": "user", "content": "在吗"}], settings=settings))
        self.assertEqual(text, "那就先这样")
        self.assertIn("没有这个工具", engine.calls[1]["messages"][-1]["content"])

    def test_text_only_engine_short_circuits(self):
        settings = tool_loop.LoopSettings(enabled=True)
        engine = ScriptedEngine(["就是一段纯文本"])
        text = asyncio.run(tool_loop.respond_with_tools(
            engine, self.runtime, [{"role": "user", "content": "在吗"}], settings=settings))
        self.assertEqual(text, "就是一段纯文本")
        self.assertEqual(len(engine.calls), 1)

    def test_engine_without_tools_support_falls_back(self):
        class OldEngine:
            configured = True
            api_style = "openai"
            last_error = ""

            async def chat(self, messages, tools=None, **kwargs):
                raise TypeError("chat() got an unexpected keyword argument tools")

        settings = tool_loop.LoopSettings(enabled=True)
        text = asyncio.run(tool_loop.respond_with_tools(OldEngine(), self.runtime,
                                                        [{"role": "user", "content": "在吗"}],
                                                        settings=settings))
        self.assertIsNone(text)

    def test_rejected_tools_degrade_to_plain_text(self):
        class RejectingEngine(ScriptedEngine):
            async def chat(self, messages, **kwargs):
                self.calls.append(dict(kwargs, messages=list(messages)))
                if kwargs.get("tools"):
                    self.last_error = "HTTP 400: unknown field tools"
                    return {"text": "", "message": {}, "content_blocks": []}
                return {"text": "不用工具也能聊", "message": {}, "content_blocks": []}

        settings = tool_loop.LoopSettings(enabled=True)
        engine = RejectingEngine([])
        text = asyncio.run(tool_loop.respond_with_tools(
            engine, self.runtime, [{"role": "user", "content": "在吗"}], settings=settings))
        self.assertEqual(text, "不用工具也能聊")
        self.assertTrue(self.runtime.tool_loop_unsupported)
        self.assertEqual(len(engine.calls), 2)

    def test_empty_reply_gets_a_fallback_line(self):
        settings = tool_loop.LoopSettings(enabled=True)
        engine = ScriptedEngine([{"text": "", "message": {}, "content_blocks": []}])
        text = asyncio.run(tool_loop.respond_with_tools(
            engine, self.runtime, [{"role": "user", "content": "在吗"}], settings=settings))
        self.assertEqual(text, tool_loop.FALLBACK_EMPTY)


class ShouldUseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = echo_registry()

    def tearDown(self):
        self._tmp.cleanup()

    def runtime(self, config):
        runtime = FakeRuntime(self.root, config=config)
        runtime.ai = types.SimpleNamespace(configured=True, api_style="openai")
        runtime.plugins = types.SimpleNamespace(_plugins={})
        runtime.tool_registry = self.registry
        return runtime

    def test_switch_off_means_no_tool_loop(self):
        runtime = self.runtime({})
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "u1"}))
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "u1"},
                                             tool_loop.LoopSettings()))

    def test_switch_on_lets_the_owner_use_tools(self):
        runtime = self.runtime({"toolLoop": True})
        self.assertTrue(tool_loop.should_use(runtime, {"sender_id": "u1"}))

    def test_stranger_is_blocked_by_default(self):
        runtime = self.runtime({"toolLoop": True})
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "someone"}))
        allowed = tool_loop.LoopSettings(enabled=True, owner_only=False)
        self.assertTrue(tool_loop.should_use(runtime, {"sender_id": "someone"}, allowed))

    def test_admin_channel_and_local_replies_are_skipped(self):
        runtime = self.runtime({"toolLoop": True})
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "u1", "_robot_admin": True}))
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "u1",
                                                   "_direct_reply": "现成回复"}))

    def test_unsupported_backend_is_skipped(self):
        runtime = self.runtime({"toolLoop": True})
        runtime.ai = types.SimpleNamespace(configured=True, api_style="anthropic")
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "u1"}))
        runtime.ai = types.SimpleNamespace(configured=False, api_style="openai")
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "u1"}))

    def test_empty_registry_is_skipped(self):
        runtime = self.runtime({"toolLoop": True})
        runtime.tool_registry = tool_loop.ToolRegistry()
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "u1"}))

    def test_unsupported_flag_is_sticky(self):
        runtime = self.runtime({"toolLoop": True})
        runtime.tool_loop_unsupported = True
        self.assertFalse(tool_loop.should_use(runtime, {"sender_id": "u1"}))


if __name__ == "__main__":
    unittest.main()
