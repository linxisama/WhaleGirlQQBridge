import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import ai_engine
from src.plugins.manager import PluginManager
from src.tool_loop import LoopSettings, ToolRegistry, respond_with_tools


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.text = json.dumps(data, ensure_ascii=False)

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeClient:
    def __init__(self):
        self.payloads = []

    async def post(self, url, **kwargs):
        self.payloads.append(kwargs.get("json"))
        return FakeResponse({"choices": [{"message": {"content": "收到"}}]})

    async def aclose(self):
        return None


class FakeBridge:
    enabled = True

    def __init__(self):
        self.seen = None

    async def transform_request(self, body, context):
        self.seen = (dict(body), dict(context))
        return {"body": dict(body, temperature=0.42), "omitTemperature": False}


class FakeEngine:
    model = "fake-model"

    def __init__(self):
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append(dict(kwargs, messages=messages))
        return "好的"


class ToolEngine:
    model = "tool-model"
    last_error = ""
    api_style = "openai"

    def __init__(self):
        self.transformed = None
        self.kwargs = None

    async def chat(self, messages, **kwargs):
        self.kwargs = kwargs
        transform = kwargs.get("body_transform")
        if callable(transform):
            self.transformed = await transform({"model": self.model, "tools": []})
        return {"text": "完成", "message": {}, "content_blocks": []}


class BodyTransformTests(unittest.TestCase):
    def _engine(self, transform):
        engine = ai_engine.AIEngine("https://example.test/v1", "fake", model="test-model")
        client = FakeClient()

        async def fake_get_client(self):
            return client

        patcher = mock.patch.object(ai_engine.AIEngine, "_get_client", fake_get_client)
        patcher.start()
        self.addCleanup(patcher.stop)
        text = asyncio.run(engine.chat(
            [{"role": "user", "content": "你好"}],
            temperature=0.7,
            tools=[{"type": "function", "function": {"name": "ping"}}],
            body_transform=transform,
        ))
        return text, client.payloads[0]

    def test_transform_rewrites_real_payload(self):
        async def transform(body):
            body["temperature"] = 0.11
            body["tools"] = [{"type": "function", "function": {"name": "trimmed"}}]
            return {"body": body, "omitTemperature": False}

        text, payload = self._engine(transform)
        self.assertEqual(text, "收到")
        self.assertEqual(payload["temperature"], 0.11)
        self.assertEqual(payload["tools"][0]["function"]["name"], "trimmed")

    def test_omit_temperature_removes_the_field(self):
        async def transform(body):
            return {"body": body, "omitTemperature": True}

        text, payload = self._engine(transform)
        self.assertEqual(text, "收到")
        self.assertNotIn("temperature", payload)

    def test_transform_failure_keeps_original_payload(self):
        async def transform(body):
            raise RuntimeError("provider boom")

        text, payload = self._engine(transform)
        self.assertEqual(text, "收到")
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["tools"][0]["function"]["name"], "ping")


class ManagerTransformTests(unittest.TestCase):
    def test_generate_reply_passes_a_body_transform(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(Path(tmp))
            bridge = FakeBridge()
            manager._plugins = {"qqagent_bridge": bridge}
            engine = FakeEngine()
            reply = asyncio.run(manager.generate_reply(
                engine,
                [{"role": "user", "content": "你好"}],
                _message={"sender_id": "12345"},
                timeout=5,
            ))
            self.assertEqual(reply, "好的")
            transform = engine.calls[0].get("body_transform")
            self.assertTrue(callable(transform))
            result = asyncio.run(transform({"model": "fake-model"}))
            self.assertEqual(result["body"]["temperature"], 0.42)
            self.assertEqual(bridge.seen[1]["senderId"], "12345")
            self.assertEqual(bridge.seen[1]["model"], "fake-model")

    def test_disabled_bridge_leaves_body_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(Path(tmp))
            bridge = FakeBridge()
            bridge.enabled = False
            manager._plugins = {"qqagent_bridge": bridge}
            body = {"model": "m", "temperature": 0.7}
            result = asyncio.run(manager.transform_request(body, {"senderId": "1"}))
            self.assertEqual(result, {"body": body})
            self.assertIsNone(bridge.seen)


class ToolLoopTransformTests(unittest.TestCase):
    def test_tool_loop_passes_messages_tools_and_sender_to_provider(self):
        class Plugins:
            def __init__(self):
                self.seen = None

            async def transform_request(self, body, context):
                self.seen = dict(context)
                return {"body": dict(body, temperature=0.25), "omitTemperature": False}

        registry = ToolRegistry()
        registry.register("demo", "ping", lambda name, arguments: {"ok": True},
                          description="ping")
        plugins = Plugins()
        runtime = SimpleNamespace(tool_registry=registry, plugins=plugins, config={})
        engine = ToolEngine()
        text = asyncio.run(respond_with_tools(
            engine, runtime, [{"role": "user", "content": "你好"}],
            settings=LoopSettings(enabled=True), turn={"sender_id": "s1"},
        ))
        self.assertEqual(text, "完成")
        self.assertTrue(callable(engine.kwargs.get("body_transform")))
        self.assertIsNotNone(engine.transformed)
        self.assertEqual(engine.transformed["body"]["temperature"], 0.25)
        self.assertEqual(plugins.seen["senderId"], "s1")
        self.assertEqual(plugins.seen["model"], "tool-model")
        self.assertTrue(plugins.seen["tools"])
        self.assertEqual(plugins.seen["messages"][0]["content"], "你好")



class RealBridgeProviderPayloadTests(unittest.TestCase):
    """llm.request-params 拿到的是请求体本身，不是桥的 payload 外壳。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name))
        self.bridge = self.manager._plugins["qqagent_bridge"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_provider_receives_the_request_body(self):
        body = {"model": "m", "temperature": 0.9,
                "tools": [{"function": {"name": "demo", "description": "  长描述  "}}]}
        seen = {}

        async def stage(profile, payload, event):
            seen.update(profile=profile, payload=payload, event=event)
            return {"ok": True, "value": {"tools": [
                {"function": {"name": "demo", "description": "短"}}]}}

        self.bridge._event_profiles = lambda message, event: [
            {"provider": "llm.request-params", "extensionId": "token-saver"}]
        with mock.patch.object(self.bridge, "_stage", side_effect=stage):
            out = asyncio.run(self.bridge.transform_request(
                body, {"senderId": "1", "model": "m"}))

        self.assertEqual(seen["event"], "on_pre_ai")
        self.assertEqual(seen["payload"]["tools"][0]["function"]["description"], "  长描述  ")
        self.assertNotIn("body", seen["payload"])
        self.assertEqual(out["body"]["tools"][0]["function"]["description"], "短")
        self.assertFalse(out["omitTemperature"])


if __name__ == "__main__":
    unittest.main()
