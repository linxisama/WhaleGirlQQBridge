"""工具环预热与保鲜（WP11）：开关关闭零副作用、开关打开真的装表、TTL 重扫、桥工具不在主线程枚举。

用真实的 QQRuntime（临时 root + 临时 sqlite）跑 start()/stop()，只把两处外部动作换成替身：
skill 注册（会起 node，实测 260-340ms）和 qqagent_bridge 插件（本机没装扩展）。"起 node"这件事
用 asyncio.create_subprocess_exec 的 spy 守着——开关关闭时它必须一次都不被调用。
"""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src import ai_engine, skill_tools, tool_loop
from src.qq_app import QQRuntime
from src.qq_transports import onebot_message

OWNER = 10001
SELF_ID = 90000
MAIN_THREAD = threading.main_thread().ident
WARM_AUDIT = "data/qq-only/tool-loop-audit.jsonl"
# 表里的名字是 _clean_name(source + '__' + raw_name)：冒号会被折成下划线。
SKILL_TOOL = "skill_demo__demo"
BRIDGE_TOOL = "qqagent_bridge__qq_query"

SKILL_SPEC = {"name": "demo", "description": "演示 skill 工具",
              "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}


def skill_handler(name, arguments):
    return {"skill": name, "q": arguments.get("q", "")}


def build_root(tmp, tool_loop_on, extra_ai=()):
    root = Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        chr(10).join(["name: default", "display_name: 默认",
                      "system_prompt: 你是默认助手。"]) + chr(10), encoding="utf-8")
    (root / "data" / "qq-only").mkdir(parents=True, exist_ok=True)
    # 开关和新键都必须写在 ai 段里：QQRuntime 只保留 ai/qq/style/qq_only（src/qq_app.py:339）。
    ai = ["ai:", "  api_url: https://example.test/v1", "  api_key: fake",
          "  model: test-model", "  reply_timeout: 8"]
    if tool_loop_on:
        ai.append("  tool_loop: true")
    ai.extend(extra_ai)
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


def text_reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29}}


class FakeBridge:
    """替身桥插件：list_tools/call_tool 都是同步的（和 src/plugins/qqagent_bridge.py 一致）。"""

    def __init__(self, tools=None):
        self.tools = list(tools or [])
        self.calls = 0
        self.threads = []

    def list_tools(self):
        self.calls += 1
        self.threads.append(threading.get_ident())
        return list(self.tools)

    def call_tool(self, name, arguments):
        return {"bridge": name, "args": arguments}


class WarmHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def make_bot(self, tool_loop_on=True, extra_ai=()):
        root = build_root(self._tmp.name, tool_loop_on, extra_ai)
        bot = QQRuntime(root=root, config_path=root / "config.qq.yaml",
                        db_path=root / "t.sqlite3")
        self.addCleanup(self._close, bot)
        return bot

    @staticmethod
    def _close(bot):
        try:
            bot.db.close()
        except Exception:
            pass

    def make_register(self, calls=None, sleep=0.0, raises=None, tools=(SKILL_SPEC,)):
        """造一个 register_skills 替身：记账（含线程 ident），可选睡眠 / 抛错 / 注册一个真工具。"""
        calls = [] if calls is None else calls

        def fake(runtime, allow=None, limit=None, **kwargs):
            calls.append({"allow": allow, "limit": limit, "thread": threading.get_ident()})
            if raises is not None:
                raise raises
            if sleep:
                time.sleep(sleep)
            count = tool_loop.register_tools(runtime, "skill:demo", list(tools),
                                             handler=skill_handler)
            return {"registered": 1 if count else 0, "skipped": 0, "failed": 0,
                    "counts": {"registered": 1 if count else 0, "registeredTools": count,
                               "skipped": 0, "failed": 0, "env": "test",
                               "envProblem": "", "elapsedMs": 1}}

        return fake, calls


class SwitchOffTests(WarmHarness):
    """验收 1 + 验收 C：开关关闭时今天的行为逐字不变。"""

    async def test_switch_off_does_nothing(self):
        bot = self.make_bot(tool_loop_on=False)
        fake, calls = self.make_register()
        with mock.patch.object(skill_tools, "register_skills", fake), \
                mock.patch("asyncio.create_subprocess_exec") as spawn:
            await bot.start()
            await asyncio.wait_for(bot.tool_warmup, timeout=10)
            await bot.stop()
        self.assertEqual(spawn.call_count, 0, "开关关闭时不得有任何 node 子进程")
        self.assertEqual(calls, [], "开关关闭时不得注册 skill")
        self.assertIsNone(getattr(bot, "tool_registry", None), "开关关闭时不得建工具表")
        self.assertEqual(bot._tool_warm_state, {})
        texts = [event["text"] for event in bot.events]
        self.assertFalse([text for text in texts if "工具" in text], texts)
        self.assertFalse((bot.root / WARM_AUDIT).exists(), "开关关闭时不得写预热审计")
        self.assertIsNone(bot.tool_warmup)

    async def test_switch_off_message_chain_unchanged(self):
        bot = self.make_bot(tool_loop_on=False)
        sent = []

        async def fake_send(transport, message, reply, meta=None, progress=None):
            sent.append(reply)
            return {"message_id": "sent-1"}

        async def fake_get_client(engine):
            return FakeClient([text_reply("今天晴，注意防晒。")])

        explode = mock.Mock(side_effect=AssertionError("开关关闭时不得进工具环"))
        with mock.patch.object(skill_tools, "register_skills",
                               mock.Mock(side_effect=AssertionError("开关关闭时不得注册 skill"))), \
                mock.patch.object(tool_loop, "respond_with_tools", explode), \
                mock.patch.object(ai_engine.AIEngine, "_get_client", fake_get_client), \
                mock.patch("src.qq_app._send_with_pacing", fake_send):
            await bot.start()
            bot.onebot.online = True
            bot.ingest(inbound_message(), "onebot")
            row = None
            deadline = time.time() + 20
            while time.time() < deadline:
                await asyncio.sleep(0.05)
                row = bot.db.execute("SELECT state, reply FROM inbox").fetchone()
                if row and row[0] in ("sent", "failed", "uncertain", "blocked"):
                    break
            await bot.stop()
        self.assertIsNotNone(row, "消息没走进库")
        self.assertEqual(row[1], "今天晴，注意防晒。")
        self.assertEqual(sent, ["今天晴，注意防晒。"])
        self.assertEqual(explode.call_count, 0)


class SwitchOnTests(WarmHarness):
    """验收 2/3/5/7：打开后真的装表、装表不挡事件循环、桥工具在树外线程枚举、停止后不留 task。"""

    async def test_switch_on_registers_skill_tools(self):
        bot = self.make_bot(tool_loop_on=True)
        fake, calls = self.make_register()
        with mock.patch.object(skill_tools, "register_skills", fake):
            await bot.start()
            await asyncio.wait_for(bot.tool_warmup, timeout=10)
            # 预热行要在第一次工具调用之前读：同一个文件当审计流，工具调用会往后追加
            # phase="tool" 行，事后取 audit[-1] 拿到的永远是那条工具记录。
            warm_audit = (bot.root / WARM_AUDIT).read_text(
                encoding="utf-8").strip().splitlines()
            registry = tool_loop.registry_for(bot)
            names = registry.names()
            outcome = await tool_loop.run_calls(
                registry, bot.plugins, bot,
                [tool_loop._call_from(SKILL_TOOL, json.dumps({"q": "天气"}))])
            state = dict(bot._tool_warm_state)
            audit = (bot.root / WARM_AUDIT).read_text(encoding="utf-8").strip().splitlines()
            await bot.stop()
        self.assertIn(SKILL_TOOL, names, names)
        self.assertEqual(len(calls), 1, "默认只跑一批（allow 为空）")
        self.assertEqual(calls[0]["limit"], 20, "默认上限 20")
        self.assertNotEqual(calls[0]["thread"], MAIN_THREAD, "注册必须发生在树外线程")
        self.assertIn("天气", outcome["messages"][0][2], outcome["messages"])
        self.assertEqual(state["skills"], 1)
        self.assertEqual(state["total"], 1)
        self.assertEqual(len(warm_audit), 1, warm_audit)
        self.assertIn('"phase": "warm"', warm_audit[-1], warm_audit[-1])
        self.assertIn('"reason": "启动预热"', warm_audit[-1], warm_audit[-1])
        self.assertEqual(len(audit), len(warm_audit) + 1, audit)
        self.assertIn('"phase": "tool"', audit[-1], audit[-1])
        self.assertIn(SKILL_TOOL, audit[-1], audit[-1])

    async def test_skill_allow_registers_second_batch(self):
        bot = self.make_bot(tool_loop_on=True,
                            extra_ai=["  tool_loop_skill_allow: extra_one, extra_two"])
        fake, calls = self.make_register()
        with mock.patch.object(skill_tools, "register_skills", fake):
            await bot.start()
            await asyncio.wait_for(bot.tool_warmup, timeout=10)
            await bot.stop()
        self.assertEqual([item["allow"] for item in calls], [None, ["extra_one", "extra_two"]])

    async def test_warmup_does_not_block_event_loop(self):
        bot = self.make_bot(tool_loop_on=True)
        fake, calls = self.make_register(sleep=0.3)

        with mock.patch.object(skill_tools, "register_skills", fake):
            await bot.start()
            still_running = not bot.tool_warmup.done()
            await asyncio.wait_for(bot.tool_warmup, timeout=10)
            await bot.stop()
        # 这里刻意不用挂钟阈值。同一条 50ms 探测在空跑时是 47-63ms，在 4 个 CPU 抢占
        # 进程下仍是 47ms，可整套 5017 个用例连跑时飙到 187ms、343ms——343ms 比这段
        # 0.3s 的假活本身还长，不可能是产品堵住循环，量的是大进程的 GC/调度抖动。
        # 改成两条确定性证据：start() 返回时 0.3s 的活还没做完（同步等待会等到 done），
        # 以及注册落在树外线程（跑在循环上就等于主线程自己干）。
        self.assertTrue(still_running, "start() 返回时预热就该还在跑（0.3s 的活）")
        self.assertEqual(len(calls), 1, calls)
        self.assertNotEqual(calls[0]["thread"], MAIN_THREAD, "注册必须发生在树外线程")

    async def test_bridge_enumeration_runs_off_main_thread(self):
        bot = self.make_bot(tool_loop_on=True)
        bridge = FakeBridge([{"name": "qq_query", "description": "查询扩展",
                              "parameters": {"type": "object", "properties": {}}}])
        bot.plugins._plugins["qqagent_bridge"] = bridge
        fake, calls = self.make_register()
        with mock.patch.object(skill_tools, "register_skills", fake):
            await bot.start()
            await asyncio.wait_for(bot.tool_warmup, timeout=10)
            after_warm = bridge.calls
            names = tool_loop.registry_for(bot).names()
            second = bridge.calls
            state = dict(bot._tool_warm_state)
            await bot.stop()
        self.assertGreaterEqual(after_warm, 1)
        self.assertNotIn(MAIN_THREAD, bridge.threads, "桥枚举跑在主线程上")
        self.assertEqual(second, after_warm, "首个工具轮又冷枚举了一次")
        self.assertIn(BRIDGE_TOOL, names, names)
        self.assertEqual(state["bridge"], 1)
        self.assertNotIn(MAIN_THREAD, calls[0]["thread"] and [calls[0]["thread"]])

    async def test_stop_leaves_no_dangling_warmup_task(self):
        bot = self.make_bot(tool_loop_on=True)
        fake, calls = self.make_register(sleep=0.25)
        with mock.patch.object(skill_tools, "register_skills", fake):
            await bot.start()
            await asyncio.sleep(0.05)
            self.assertFalse(bot.tool_warmup.done())
            await bot.stop()
            await asyncio.sleep(0)
            current = asyncio.current_task()
            leftover = [task for task in asyncio.all_tasks()
                        if task is not current and "warm" in repr(task.get_coro()).lower()]
        self.assertEqual(leftover, [], leftover)
        self.assertIsNone(bot.tool_warmup)
        self.assertIsNone(bot._tool_warm_task)


class TtlTests(WarmHarness):
    """验收 4：ttl=0 永不重扫；ttl 到期重扫一次；重扫失败保留上一张表。"""

    async def test_ttl_zero_never_rescans(self):
        bot = self.make_bot(tool_loop_on=True, extra_ai=["  tool_loop_skill_ttl: 0"])
        fake, calls = self.make_register()
        with mock.patch.object(skill_tools, "register_skills", fake):
            await bot.start()
            await asyncio.wait_for(bot.tool_warmup, timeout=10)
            first = len(calls)
            bot._tool_warm_stamp -= 3600.0        # 表再旧也不许重扫
            self.assertIsNone(bot._tool_refresh_maybe())
            await asyncio.sleep(0)
            await bot.stop()
        self.assertEqual(first, 1)
        self.assertEqual(len(calls), first, "ttl=0 时不许重扫")
        self.assertIsNone(bot._tool_warm_task)

    async def test_ttl_expired_rescans_once(self):
        bot = self.make_bot(tool_loop_on=True, extra_ai=["  tool_loop_skill_ttl: 1"])
        fake, calls = self.make_register()
        with mock.patch.object(skill_tools, "register_skills", fake):
            await bot.start()
            await asyncio.wait_for(bot.tool_warmup, timeout=10)
            first = len(calls)
            self.assertIsNone(bot._tool_refresh_maybe(), "还没过期不该重扫")
            self.assertEqual(len(calls), first)
            await asyncio.sleep(1.1)
            task = bot._tool_refresh_maybe()
            self.assertIsNotNone(task)
            self.assertIsNone(bot._tool_refresh_maybe(), "同一时刻只留一个重扫任务")
            await asyncio.wait_for(task, timeout=10)
            self.assertEqual(len(calls), first + 1, "过期后应当重扫一次")
            await bot.stop()
        self.assertIsNone(bot._tool_warm_task)

    async def test_failed_rescan_keeps_previous_table(self):
        bot = self.make_bot(tool_loop_on=True, extra_ai=["  tool_loop_skill_ttl: 1"])
        fake, calls = self.make_register()
        boom = mock.Mock(side_effect=RuntimeError("node 起不来"))
        with mock.patch.object(skill_tools, "register_skills", fake):
            await bot.start()
            await asyncio.wait_for(bot.tool_warmup, timeout=10)
            table = tool_loop.registry_for(bot)
            self.assertIn(SKILL_TOOL, table.names())
            with mock.patch.object(skill_tools, "register_skills", boom):
                bot._tool_warm_stamp -= 3600.0
                await asyncio.wait_for(bot._tool_refresh_maybe(), timeout=10)
            same = tool_loop.registry_for(bot)
            names = same.names()
            texts = [event["text"] for event in bot.events]
            await bot.stop()
        self.assertTrue(boom.called)
        self.assertIs(same, table, "重扫失败不该换表")
        self.assertIn(SKILL_TOOL, names, "重扫失败后旧表必须还在")
        self.assertTrue([text for text in texts
                         if "skill 注册失败" in text or "重扫失败" in text], texts)


if __name__ == "__main__":
    unittest.main()
