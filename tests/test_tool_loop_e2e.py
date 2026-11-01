"""工具调用环端到端：真实入站消息 → 模型要工具 → 工具执行 → 结果回灌 → 最终回复。

用 fastapi.testclient.TestClient 跑真实应用（create_app + 真实 _work 工作循环）；只有两处是
替身：模型 HTTP 客户端（脚本化响应）和最终发送（捕获回复文本，不碰真网络）。
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient

from src import ai_engine, tool_loop
from src.agent_scheduler import AgentScheduler
from src.qq_app import QQRuntime, create_app
from src.qq_transports import onebot_message

OWNER = 10001
SELF_ID = 90000
AUDIT = "data/qq-only/tool-loop-audit.jsonl"

ECHO_TOOL = {"name": "echo", "description": "回声",
             "parameters": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]}}


def echo_handler(name, arguments):
    return {"echo": arguments.get("text", "")}


def tool_call_reply():
    return {"choices": [{"message": {"role": "assistant", "content": None,
                         "tool_calls": [{"id": "call_1", "type": "function",
                                         "function": {"name": "echo",
                                                      "arguments": json.dumps(
                                                          {"text": "查天气"},
                                                          ensure_ascii=False)}}]},
                         "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17}}


def text_reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29}}


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
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    async def post(self, url, **kwargs):
        self.payloads.append(kwargs.get("json"))
        index = min(len(self.payloads) - 1, len(self.responses) - 1)
        return FakeResponse(self.responses[index])

    async def aclose(self):
        return None


def build_root(tmp, tool_loop_on):
    root = Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        chr(10).join(["name: default", "display_name: 默认",
                      "system_prompt: 你是默认助手。"]) + chr(10), encoding="utf-8")
    (root / "data" / "qq-only").mkdir(parents=True, exist_ok=True)
    ai = ["ai:", "  api_url: https://example.test/v1", "  api_key: fake",
          "  model: test-model", "  reply_timeout: 8"]
    if tool_loop_on:
        # 开关必须写在 ai 段里：QQRuntime 只保留 ai/qq/style/qq_only（src/qq_app.py:339）。
        ai.append("  tool_loop: true")
    lines = ai + ["qq:", "  processing_status: false",
                  "qq_only:", "  mode: onebot", "  listen_private: true",
                  "  onebot_token: token123"]
    (root / "config.qq.yaml").write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    return root


def inbound_message():
    event = {"post_type": "message", "message_type": "private", "sub_type": "friend",
             "self_id": SELF_ID, "user_id": OWNER, "message_id": 4321,
             "message": [{"type": "text", "data": {"text": "查一下天气"}}],
             "sender": {"user_id": OWNER, "nickname": "老哥"}}
    return onebot_message(event, SELF_ID)


class BaseEndToEnd(unittest.TestCase):
    tool_loop_on = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name, self.tool_loop_on)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "t.sqlite3")
        self.bot.running = True
        self.bot.owner_ids = lambda: {str(OWNER)}
        self.bot.onebot.online = True   # 工作循环只在网关在线时才取队列，这里直接开闸
        self.registry = tool_loop.ToolRegistry()
        self.registry.register_tools("builtin", [ECHO_TOOL], handler=echo_handler)
        self.bot.tool_registry = self.registry
        self.replies = []
        if self.tool_loop_on:
            self.client = FakeClient([tool_call_reply(),
                                      text_reply("查到了：查天气，今天晴。")])
        else:
            self.client = FakeClient([text_reply("今天晴，注意防晒。")])
        self.worker = None
        # 定时日记会在 create_app 启动时先发一次模型请求；这里只测工具环，先关掉自动排期。
        self._no_diary = mock.patch.object(AgentScheduler, '_ensure_daily_diary',
                                           lambda self: None)
        self._no_diary.start()

    def tearDown(self):
        self._no_diary.stop()
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    async def fake_send(self, transport, message, reply, meta=None, progress=None):
        self.replies.append(reply)
        return {"message_id": "sent-1"}

    def run_one_message(self):
        """把一条真实 onebot 事件推进真实工作循环，等它落库。"""
        async def fake_get_client(engine):
            return self.client

        async def kick():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.worker = self.bot.worker
            self.bot.ingest(inbound_message(), "onebot")

        async def stop():
            worker = self.worker
            if worker is not None:
                worker.cancel()
                try:
                    await worker
                except BaseException:
                    pass

        with mock.patch.object(ai_engine.AIEngine, "_get_client", fake_get_client):
            with mock.patch("src.qq_app._send_with_pacing", self.fake_send):
                with TestClient(create_app(self.bot)) as client:
                    client.portal.call(kick)
                    row = None
                    deadline = time.time() + 20
                    while time.time() < deadline:
                        row = self.bot.db.execute(
                            "SELECT state, reply FROM inbox").fetchone()
                        if row and row[0] in ("sent", "failed", "uncertain", "blocked",
                                              "expired", "error_replied"):
                            break
                        time.sleep(0.05)
                    client.portal.call(stop)
        return row

    def audit_records(self):
        path = self.root / AUDIT
        if not path.exists():
            return []
        return [json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines() if line]


class ToolLoopOnEndToEnd(BaseEndToEnd):
    tool_loop_on = True

    def test_inbound_message_round_trips_through_a_tool_call(self):
        row = self.run_one_message()
        self.assertIsNotNone(row, "消息没有落库")
        self.assertEqual(row[0], "sent", row)
        with_tools = [item for item in self.client.payloads if item.get("tools")]
        self.assertTrue(with_tools, "第一次请求就应该带上 tools")
        self.assertEqual([item["function"]["name"] for item in with_tools[0]["tools"]],
                         ["echo"])
        fed_back = [item for item in self.client.payloads
                    if any(turn.get("role") == "tool" for turn in item["messages"])]
        self.assertEqual(len(fed_back), 1, "工具结果必须回灌给模型一次")
        tool_turn = [turn for turn in fed_back[0]["messages"]
                     if turn.get("role") == "tool"][-1]
        self.assertEqual(tool_turn["tool_call_id"], "call_1")
        self.assertIn("查天气", tool_turn["content"])
        self.assertTrue(self.replies, "回复没有发出去")
        self.assertIn("查到了", self.replies[-1])
        self.assertIn("查到了", row[1] or "")
        records = self.audit_records()
        self.assertTrue(records, "工具调用必须写审计")
        last = records[-1]
        self.assertEqual(last["phase"], "tool")
        self.assertEqual(last["tool"], "echo")
        self.assertEqual(last["arguments"]["text"], "查天气")
        self.assertIn("查天气", last["result"])


class ToolLoopOffEndToEnd(BaseEndToEnd):
    tool_loop_on = False

    def test_switch_off_keeps_the_old_single_call_path(self):
        row = self.run_one_message()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "sent", row)
        self.assertEqual(len(self.client.payloads), 1, self.client.payloads)
        self.assertNotIn("tools", self.client.payloads[0])
        self.assertEqual(self.audit_records(), [])
        self.assertIn("今天晴", self.replies[-1])


if __name__ == "__main__":
    unittest.main()
