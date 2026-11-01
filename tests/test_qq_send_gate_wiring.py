"""三条发送路径的过闸接线测试：官方分片 / outbox / /api 直发。

不碰真实网络：把 transport 的网络动作替换成记录器，用 FakeClock 验证
第 2 次发送前 elapsed >= 800ms（或冷却期 /api 被拒）。
"""
import asyncio
import json
import types
import time as real_time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import src.qq_app as qq_app
import src.qq_send_gate as gate_mod
from src.qq_send_gate import SendGate
from src.qq_transports import OfficialQQ

_REAL_SLEEP = asyncio.sleep


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now
        self.slept = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        await _REAL_SLEEP(0)


def patch_clock(clock):
    stack = ExitStack()
    stack.enter_context(patch.object(gate_mod.time, "monotonic", clock.monotonic))
    stack.enter_context(patch.object(gate_mod.asyncio, "sleep", clock.sleep))
    return stack


class FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeDB:
    def __init__(self, outbox_rows):
        self._outbox = list(outbox_rows)
        self.updates = []

    def execute(self, sql, params=None):
        sql = (sql or "").lstrip()
        if sql.startswith("SELECT key,kind,target,text,created"):
            return FakeCursor(self._outbox)
        if sql.startswith("SELECT plugin,inbound"):
            return FakeCursor([])
        self.updates.append((sql, params))
        return FakeCursor([])

    def commit(self):
        pass


class FakeOneBotCall:
    """记录 call 时刻的假 OneBot transport（/api 直发用）。"""

    def __init__(self):
        self.calls = []

    async def call(self, action, params):
        self.calls.append((action, params))
        return {"message_id": 1}


class FakeBot:
    def __init__(self, gate):
        self.config = {"qq_only.mode": "onebot"}
        self.send_gate = gate
        self.onebot = FakeOneBotCall()


def _route(app, path):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route
    raise AssertionError(f"没有找到路由 {path}")


class SendGateWiringTests(unittest.IsolatedAsyncioTestCase):

    async def test_official_split_each_piece_acquires_gate(self):
        clock = FakeClock(0.0)
        gate = SendGate()
        transport = OfficialQQ("app", "secret", send_gate=gate)
        sent_at = []

        async def fake_fallback(self, action):
            sent_at.append(clock.now)
            return {"id": "ok"}

        transport._send_with_sandbox_fallback = types.MethodType(fake_fallback, transport)
        try:
            with patch_clock(clock):
                message = {"kind": "group", "target": "123", "id": "m1",
                           "text": "hi", "_passive_seq": 1}
                await qq_app._send_with_pacing(
                    transport, message, "第一段\x01PACE:0\x01第二段")
        finally:
            await transport.close()

        self.assertEqual(len(sent_at), 2)
        self.assertGreaterEqual(sent_at[1] - sent_at[0], 0.8 - 1e-9)
        self.assertGreaterEqual(gate.status(now=clock.now)["min_gap_ms"], 800.0)

    async def test_outbox_multi_send_keeps_gap(self):
        clock = FakeClock(0.0)
        gate = SendGate()
        transport = OfficialQQ("app", "secret", send_gate=gate)
        transport.authenticated = True
        sent_at = []

        async def fake_fallback(self, action):
            sent_at.append(clock.now)
            return {"id": "active"}

        transport._send_with_sandbox_fallback = types.MethodType(fake_fallback, transport)
        created = real_time.time()
        rows = [
            ("k1", "group", "123", "第一条", created),
            ("k2", "group", "123", "第二条", created),
            ("k3", "group", "123", "第三条", created),
        ]
        fake = types.SimpleNamespace(
            mode="official",
            official=transport,
            reply_paused=False,
            db=FakeDB(rows),
            account=lambda: "app",
            can_send_proactive=lambda kind, target: True,
            note=lambda msg: None,
        )
        try:
            with patch_clock(clock):
                await qq_app.QQRuntime._drain_outbox(fake)
        finally:
            await transport.close()

        self.assertEqual(len(sent_at), 3)
        self.assertGreaterEqual(sent_at[1] - sent_at[0], 0.8 - 1e-9)
        self.assertGreaterEqual(sent_at[2] - sent_at[1], 0.8 - 1e-9)
        sent = [sql for sql, _ in fake.db.updates
                if "state='sent'" in sql]
        self.assertEqual(len(sent), 3)

    async def test_api_direct_send_keeps_gap_and_rejects_in_cooldown(self):
        clock = FakeClock(0.0)
        gate = SendGate()
        app = qq_app.create_app()
        bot = FakeBot(gate)
        app.state.bot = bot
        endpoint = _route(app, "/api/onebot/action").endpoint

        with patch_clock(clock):
            payload = {"action": "send_group_msg",
                       "params": {"group_id": "123",
                                  "message": [{"type": "text",
                                               "data": {"text": "hi"}}]}}
            first = await endpoint(dict(payload))
            second = await endpoint(dict(payload))
            self.assertIsInstance(first, dict)
            self.assertTrue(first.get("success"))
            self.assertIsInstance(second, dict)
            self.assertTrue(second.get("success"))
            self.assertEqual(len(bot.onebot.calls), 2)
            self.assertGreaterEqual(clock.now, 0.8 - 1e-9)

            # 冷却中：/api 直发必须拒绝，不得静默丢库。
            await gate.report_code(429)
            rejected = await endpoint(dict(payload))
            self.assertEqual(rejected.status_code, 429)
            body = json.loads(rejected.body.decode("utf-8"))
            self.assertEqual(body.get("code"), "QQ_RATE_LIMITED")
            self.assertIn("冷却", body.get("message", ""))

    async def test_api_media_send_acquires_gate(self):
        clock = FakeClock(0.0)
        gate = SendGate()
        app = qq_app.create_app()
        bot = FakeBot(gate)
        app.state.bot = bot
        endpoint = _route(app, "/api/media/send").endpoint

        with patch_clock(clock):
            payload = {"target_type": "group", "target_id": "123",
                       "kind": "image", "url": "https://example.com/a.png"}
            first = await endpoint(dict(payload))
            second = await endpoint(dict(payload))
            self.assertIsInstance(first, dict)
            self.assertTrue(first.get("success"))
            self.assertIsInstance(second, dict)
            self.assertTrue(second.get("success"))
            self.assertEqual(len(bot.onebot.calls), 2)
            self.assertGreaterEqual(clock.now, 0.8 - 1e-9)


if __name__ == "__main__":
    unittest.main()
