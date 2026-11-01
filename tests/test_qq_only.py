"""Offline integration tests. All credentials, state and HTTP calls are isolated."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import yaml
from fastapi.testclient import TestClient

from src.qq_app import QQRuntime, Settings, create_app, ROOT
from src.qq_transports import OfficialQQ, OneBotQQ, signing_key, onebot_message
from src.ai_engine import AIEngine


class IsolatedRuntime:
    def setup_runtime(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.path = Path(self.tmp.name)
        config = self.path / "test.yaml"
        config.write_text(yaml.safe_dump({"ai": {"api_url": "https://example.test/v1", "api_key": "fake-key", "model": "test-model"}, "qq": {"app_id": "123", "client_secret": "fake-secret"}}), encoding="utf-8")
        self.bot = QQRuntime(config_path=config, db_path=self.path / "test.sqlite",
                             data_root=self.path / "data")


class ConsoleTests(IsolatedRuntime, unittest.TestCase):
    def setUp(self):
        self.setup_runtime()
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def signed(self, payload, valid=True):
        raw = json.dumps(payload).encode()
        stamp = str(int(time.time()))
        sig = signing_key("fake-secret").sign(stamp.encode() + raw).hex()
        return self.client.post("/qq/callback", content=raw, headers={"x-signature-timestamp": stamp, "x-signature-ed25519": sig if valid else "00" * 64})

    def event(self):
        return {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {"id": "m1", "group_openid": "g1", "content": "鲸鱼娘你好", "author": {"member_openid": "user"}}}

    def test_console_qq_only_and_no_secret_exposure(self):
        html = self.client.get("/").text
        self.assertIn("OneBot", html)
        self.assertNotIn("微信", html)
        settings = self.client.get("/api/settings")
        self.assertNotIn("fake-key", settings.text)
        self.assertNotIn("fake-secret", settings.text)
        self.assertEqual(settings.json()["model"], "test-model")

    def test_official_challenge_matches_published_vector(self):
        self.bot.official.secret = "DG5g3B4j9X2KOErG"
        res = self.client.post("/qq/callback", json={"op":13,"d":{"plain_token":"Arq0D5A61EgUu4OxUvOp","event_ts":"1725442341"}})
        self.assertEqual(res.json()["signature"], "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706")

    def test_invalid_signature_and_stopped_callback_rejected(self):
        self.assertEqual(self.signed(self.event(), False).status_code, 401)
        self.assertEqual(self.signed(self.event()).status_code, 503)

    def test_callback_durable_deduplication(self):
        self.bot.running = True
        for _ in range(2):
            self.assertEqual(self.signed(self.event()).json(), {"op":12,"d":{}})
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)

    def test_group_filter_and_private_switch(self):
        self.bot.running = True
        self.bot.config.set("qq_only.groups", ["allowed"])
        self.signed(self.event())
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)

    def test_group_whitelist_and_strict_empty(self):
        self.bot.running = True
        self.bot.config.set("qq_only.group_whitelist_strict", True)
        self.bot.ingest({"id": "g-strict", "kind": "group", "target": "g1",
                         "sender_id": "u1", "text": "在吗", "at": True}, "official")
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)

        self.bot.config.set("qq_only.groups", ["g1"])
        self.bot.ingest({"id": "g-other", "kind": "group", "target": "g2",
                         "sender_id": "u1", "text": "在吗", "at": True}, "official")
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)
        self.bot.ingest({"id": "g-ok", "kind": "group", "target": "g1",
                         "sender_id": "u1", "text": "在吗", "at": True}, "official")
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)

    def test_private_owner_binding_restricts_strangers_and_allows_list(self):
        self.bot.running = True
        self.bot.plugins.update_settings("pc_control", {"ownerIds": "owner-1"})
        self.bot.config.set("qq_only.private_whitelist_strict", True)
        self.bot.ingest({"id": "p-stranger", "kind": "private", "target": "stranger-1",
                         "sender_id": "stranger-1", "text": "你好"}, "official")
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)

        self.bot.ingest({"id": "p-owner", "kind": "private", "target": "owner-1",
                         "sender_id": "owner-1", "text": "你好"}, "official")
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)

        self.bot.config.set("qq_only.private_users", ["friend-1"])
        self.bot.ingest({"id": "p-friend", "kind": "private", "target": "friend-1",
                         "sender_id": "friend-1", "text": "你好"}, "official")
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 2)

    def test_proactive_send_respects_whitelist(self):
        self.bot.config.set("qq_only.mode", "onebot")
        self.bot.running = True
        self.bot.plugins.update_settings("pc_control", {"ownerIds": "10001"})
        self.bot.config.set("qq_only.private_users", ["20002"])
        self.assertTrue(self.bot.can_send_proactive("private", "10001"))
        self.assertTrue(self.bot.can_send_proactive("private", "20002"))
        self.assertFalse(self.bot.can_send_proactive("private", "30003"))
        self.bot.config.set("qq_only.group_whitelist_strict", True)
        self.assertFalse(self.bot.can_send_proactive("group", "40004"))

    def test_private_strict_uses_console_owner_binding(self):
        path = Path(self.bot.config.config_path).parent / "agent_admin.json"
        path.write_text(json.dumps({"admin": "2019117802"}), encoding="utf-8")
        self.bot.config.set("qq_only.listen_private", True)
        self.bot.config.set("qq_only.private_users", [])
        self.bot.config.set("qq_only.private_whitelist_strict", True)
        self.assertTrue(self.bot.conversation_allowed("private", "2019117802"))
        self.assertFalse(self.bot.conversation_allowed("private", "10001"))
        self.assertTrue(self.bot.conversation_allowed("private", "10001", sender_id="2019117802"))

    def test_conversation_whitelist_settings_roundtrip(self):
        data = self.client.get("/api/settings").json()
        for key in ("groups", "private_users", "listen_private",
                    "group_whitelist_strict", "private_whitelist_strict"):
            self.assertIn(key, data)
        response = self.client.post("/api/settings", json={
            "api_url": "https://example.test/v1", "model": "new-model", "app_id": "123",
            "groups": ["g1", "g1", "g2"], "private_users": ["u1", "u1"],
            "listen_private": True, "group_whitelist_strict": True,
            "private_whitelist_strict": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.bot.config.get("qq_only.groups"), ["g1", "g2"])
        self.assertEqual(self.bot.config.get("qq_only.private_users"), ["u1"])
        self.assertTrue(self.bot.config.get("qq_only.group_whitelist_strict"))
        self.assertTrue(self.bot.config.get("qq_only.private_whitelist_strict"))

    def test_save_retains_and_encrypts_secrets(self):
        response = self.client.post("/api/settings", json={"api_url":"https://example.test/v1","model":"new-model", "app_id":"123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.bot.ai.api_key, "fake-key")
        self.assertNotIn("fake-key", self.bot.config.config_path.read_text())
        self.assertNotIn("fake-secret", self.bot.config.config_path.read_text())
        self.assertNotIn("wechat", self.bot.config.config)

    def test_account_change_requires_matching_secret(self):
        response = self.client.post("/api/settings", json={"api_url":"https://example.test/v1", "model":"m", "app_id":"other"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.bot.official.app_id, "123")

    def test_admin_rejects_cross_origin_and_forwarded_requests(self):
        self.assertEqual(self.client.post("/api/stop", headers={"Origin":"https://other.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/settings", headers={"X-Forwarded-For":"1.2.3.4"}).status_code, 403)
        self.assertEqual(self.client.get("/api/settings", headers={"Host":"evil.example"}).status_code, 403)

    def test_onebot_auth_login_and_reconnect(self):
        self.bot.config.set("qq_only.mode", "onebot")
        self.bot.config.set("qq_only.onebot_token", "test-token")
        self.bot.running = True
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/qq/onebot"):
                pass
        for _ in range(2):
            with self.client.websocket_connect("/qq/onebot", headers={"Authorization":"Bearer test-token", "X-Self-ID":"42"}) as ws:
                req = ws.receive_json()
                self.assertEqual(req["action"], "get_login_info")
                ws.send_json({"echo":req["echo"],"status":"ok","retcode":0,"data":{"user_id":42}})
                req = ws.receive_json()
                self.assertEqual(req["action"], "get_status")
                ws.send_json({"echo":req["echo"],"status":"ok","retcode":0,"data":{"online":True,"good":True}})
                ws.send_json({"post_type":"message","message_type":"private","self_id":42,"user_id":7,"message_id":8,"message":[{"type":"text","data":{"text":"你好"}}]})
                # Receiving the next API call ensures the receive loop handled the message.
                with self.client.portal.wrap_async_context_manager(_probe_request(self.bot.onebot)):
                    req=ws.receive_json()
                    ws.send_json({"echo":req["echo"],"status":"ok","retcode":0,"data":{}})
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)


from contextlib import asynccontextmanager
@asynccontextmanager
async def _probe_request(bridge):
    task = asyncio.create_task(bridge.call("get_version_info"))
    yield
    await task


class AsyncTests(IsolatedRuntime, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_runtime()

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    async def test_official_token_singleflight_and_401_refresh(self):
        bridge = self.bot.official
        counts = {"token":0,"send":0}
        payloads = []
        def respond(req):
            if "getAppAccessToken" in str(req.url):
                counts["token"] += 1
                return httpx.Response(200,json={"access_token":str(counts["token"]),"expires_in":7200})
            counts["send"] += 1
            payloads.append(json.loads(req.content))
            return httpx.Response(401 if counts["send"] == 1 else 200,json={"id":"ok"})
        await bridge.http.aclose()
        bridge.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        await asyncio.gather(*(bridge.authorize() for _ in range(8)))
        self.assertEqual(counts["token"],1)
        await bridge.send({"kind":"group","target":"g","id":"original"},"reply")
        self.assertEqual(counts["token"],2)
        self.assertEqual(payloads[0],payloads[1])
        self.assertEqual(payloads[1]["msg_id"],"original")

    async def test_message_pipeline_and_uncertain_send_no_retry(self):
        bot=self.bot
        bot.official.authorize=AsyncMock(return_value="token")
        bot.official.authenticated=True
        bot.ai.chat=AsyncMock(return_value="哼，鲸鱼娘在呢！")
        bot.official.send=AsyncMock()
        await bot.start()
        message={"id":"1","kind":"private","target":"u","text":"你好","sender":"user","at":True}
        bot.ingest(message,"official")
        for _ in range(40):
            if bot.db.execute("SELECT state FROM inbox").fetchone()[0]=='sent':break
            await asyncio.sleep(.05)
        self.assertEqual(bot.db.execute("SELECT state FROM inbox").fetchone()[0],'sent')
        bot.official.send=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        bot.ingest({**message,"id":"2"},"official")
        for _ in range(50):
            if bot.db.execute("SELECT count(*) FROM inbox WHERE state='uncertain'").fetchone()[0]:break
            await asyncio.sleep(.05)
        self.assertEqual(bot.db.execute("SELECT count(*) FROM inbox WHERE state='uncertain'").fetchone()[0],1)
        bot.official.send.assert_awaited_once()

    async def test_ai_invalid_credentials_are_diagnostic(self):
        ai=self.bot.ai
        transport=httpx.MockTransport(lambda req:httpx.Response(401,json={}))
        # test_connection 现在用**自己的一次性客户端**（这样"点一下测试连接"不会占住
        # ai_lock 把回复堵住），所以要替换的是 httpx.AsyncClient 工厂，而不是共享客户端缓存。
        # 注意：patch 的是 httpx 模块上的属性，工厂里必须用替换前存下的真类。
        real=httpx.AsyncClient
        def factory(**kwargs):
            return real(transport=transport,**kwargs)
        with patch('src.ai_engine.httpx.AsyncClient',factory):
            ok,msg=await ai.test_connection()
        self.assertFalse(ok)
        self.assertIn("401",msg)

    async def test_onebot_does_not_parse_text_as_commands(self):
        bridge=self.bot.onebot
        bridge.online=True
        bridge.call=AsyncMock(return_value={})
        await bridge.send({"kind":"group","target":"123"},"[CQ:at,qq=all]")
        args=bridge.call.call_args.args
        self.assertEqual(args[1]["message"],[{"type":"text","data":{"text":"[CQ:at,qq=all]"}}])

    async def test_onebot_self_messages_ignored(self):
        self.assertIsNone(onebot_message({"post_type":"message","message_type":"private","user_id":42},"42"))

    async def test_restart_preserves_pending_and_marks_inflight_uncertain(self):
        bot=self.bot
        bot.running=True
        message={"id":"pending","kind":"private","target":"u","text":"hello","sender":"user","at":True}
        bot.ingest(message,"official")
        bot.ingest({**message,"id":"sending"},"official")
        bot.db.execute("UPDATE inbox SET state='sending' WHERE json_extract(payload,'$.id')='sending'")
        bot.db.commit()
        bot.db.close()
        await bot.ai.close()
        await bot.official.close()
        self.bot=QQRuntime(config_path=self.path/'test.yaml',db_path=self.path/'test.sqlite')
        states={row[0] for row in self.bot.db.execute('SELECT state FROM inbox')}
        self.assertEqual(states,{'pending','uncertain'})


if __name__ == "__main__":
    unittest.main()
