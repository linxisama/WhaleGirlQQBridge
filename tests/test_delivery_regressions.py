"""Offline regressions for delivery semantics and OneBot input robustness."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import yaml

from src.qq_app import QQRuntime
from src.qq_transports import OneBotQQ, onebot_message
from src.ai_engine import AIEngine
from tests.test_qq_only import IsolatedRuntime


class DeliveryRegressionTests(IsolatedRuntime, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_runtime()
        self.bot.official.authenticated = True
        self.bot.official.authorize = AsyncMock(return_value="tok")

    async def asyncTearDown(self):
        if getattr(self, "bot", None):
            await self.bot.close()
        self.tmp.cleanup()

    async def test_timeout_survives_two_cycles_without_resending_same_message_and_processes_later_pending(self):
        bot = self.bot
        bot.ai.chat = AsyncMock(return_value="ok")
        sent = []

        async def flaky_send(message, reply):
            if len(sent) == 0:
                sent.append(message["id"])
                raise httpx.ReadTimeout("timeout")
            sent.append(message["id"])

        bot.official.send = AsyncMock(side_effect=flaky_send)
        await bot.start()
        bot.ingest({"id": "one", "kind": "private", "target": "u", "text": "first", "sender": "s", "at": True}, "official")
        deadline = time.time() + 12
        while bot.db.execute("SELECT count(*) FROM inbox WHERE state='uncertain'").fetchone()[0] == 0:
            if time.time() > deadline:
                self.fail("message did not become uncertain")
            await asyncio.sleep(0.05)
        bot.ingest({"id": "two", "kind": "private", "target": "u", "text": "second", "sender": "s", "at": True}, "official")
        deadline = time.time() + 12
        while bot.db.execute("SELECT count(*) FROM inbox WHERE state='sent'").fetchone()[0] != 1:
            if time.time() > deadline:
                self.fail("second pending did not become sent")
            await asyncio.sleep(0.05)
        await asyncio.sleep(2.5)
        self.assertEqual(sent, ["one", "two"])
        self.assertEqual(bot.db.execute("SELECT state FROM inbox WHERE json_extract(payload,'$.id')='one'").fetchone()[0], "uncertain")
        self.assertEqual(bot.db.execute("SELECT state FROM inbox WHERE json_extract(payload,'$.id')='two'").fetchone()[0], "sent")

    async def test_restart_does_not_automatically_resend_uncertain_messages(self):
        bot = self.bot
        bot.running = True
        bot.ingest({"id": "u", "kind": "private", "target": "u", "text": "hi", "sender": "s", "at": True}, "official")
        bot.db.execute("UPDATE inbox SET state='uncertain', error='in flight' WHERE json_extract(payload,'$.id')='u'")
        bot.db.commit()
        await bot.close()

        self.bot = QQRuntime(config_path=self.path / "test.yaml", db_path=self.path / "test.sqlite")
        self.bot.official.authenticated = True
        self.bot.official.authorize = AsyncMock(return_value="tok")
        self.bot.official.send = AsyncMock()
        self.bot.ai.chat = AsyncMock(return_value="ok")
        await self.bot.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            await asyncio.sleep(0.2)
        self.bot.official.send.assert_not_awaited()
        self.assertEqual(self.bot.db.execute("SELECT state FROM inbox WHERE json_extract(payload,'$.id')='u'").fetchone()[0], "uncertain")

    async def test_worker_uses_shared_ai_lock(self):
        self.bot.ai.chat = AsyncMock(return_value='ok')
        self.bot.official.send = AsyncMock()
        await self.bot.ai_lock.acquire()
        try:
            await self.bot.start()
            self.bot.ingest({'id':'locked','kind':'private','target':'u','text':'hi','sender':'s','at':True}, 'official')
            await asyncio.sleep(0.4)
            self.bot.ai.chat.assert_not_awaited()
        finally:
            self.bot.ai_lock.release()
        for _ in range(40):
            if self.bot.official.send.await_count:
                break
            await asyncio.sleep(0.05)
        self.bot.official.send.assert_awaited_once()

    async def test_official_sequence_stable_across_refresh_and_same_reply(self):
        bridge = self.bot.official
        payloads = []
        def respond(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(401 if len(payloads)==1 else 200, json={'id':'ok'})
        await bridge.http.aclose()
        bridge.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        message = {'id':'original','kind':'group','target':'g'}
        await bridge.send(message, 'reply')
        await bridge.send(message, 'reply')
        self.assertEqual(len(payloads), 3)
        self.assertTrue(all(p == payloads[0] for p in payloads))
        self.assertEqual(payloads[0]['msg_seq'], 1)


class OneBotInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_mentions_preserve_explicit_self(self):
        for targets in ([42, 8, 'all'], ['all', 8, 42]):
            msg = onebot_message({"post_type": "message", "message_type": "group",
                "user_id": 7, "group_id": 9, "message_id": 104,
                "message": [{"type": "at", "data": {"qq": q}} for q in targets]}, "42")
            self.assertTrue(msg['at'])

    async def test_at_all_does_not_trigger_self_but_explicit_at_does(self):
        payload_all = {"post_type": "message", "message_type": "group", "user_id": 7, "group_id": 9, "message_id": 100,
                       "message": [{"type": "at", "data": {"qq": "all"}}, {"type": "text", "data": {"text": "大家看"}}],
                       "sender": {"nickname": "n"}}
        payload_self = {"post_type": "message", "message_type": "group", "user_id": 7, "group_id": 9, "message_id": 101,
                       "message": [{"type": "at", "data": {"qq": "42"}}, {"type": "text", "data": {"text": "嗨"}}],
                       "sender": {"nickname": "n"}}
        all_msg = onebot_message(payload_all, "42")
        self_msg = onebot_message(payload_self, "42")
        self.assertTrue(all_msg["at"] is False)
        self.assertTrue(self_msg["at"] is True)
        self.assertEqual(all_msg["text"], "大家看")

    async def test_invalid_segments_and_sender_do_not_crash(self):
        data = {"post_type": "message", "message_type": "group", "user_id": 7, "group_id": 9, "message_id": 102,
                "message": ["bad", {"type": "text"}, {"type": "at", "data": "bad"}, {"type": "at", "data": {"qq": 42}},
                            {"type": "text", "data": {"text": "hello"}}],
                "sender": None}
        msg = onebot_message(data, "42")
        self.assertEqual(msg["text"], "hello")
        self.assertTrue(msg["at"])
        self.assertEqual(msg["sender"], "7")

    async def test_text_cq_not_treated_as_at(self):
        msg = onebot_message({"post_type": "message", "message_type": "group", "user_id": 7, "group_id": 9, "message_id": 103,
                              "message": [{"type": "text", "data": {"text": "[CQ:at,qq=42] hi"}}], "sender": {"nickname": "n"}}, "42")
        self.assertFalse(msg["at"])
        self.assertEqual(msg["text"], "[CQ:at,qq=42] hi")
