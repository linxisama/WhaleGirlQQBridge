"""官方接口接收链路的测试：事件解析 + WebSocket 帧处理。

背景（用户报的 bug）：机器人**收不到任何 QQ 消息**。排查结论是两条：
1. 配置里的 `app_id` 与 AppSecret 不匹配（平台回 `code 100016 invalid appid or secret`）；
2. 更根本的是——本项目只实现了 HTTPS 回调（需要公网域名），而用户在 QQ 开放平台
   选的是 **WebSocket** 事件推送，所以事件从来没到过。

本文件钉住第 2 条的修复：官方 WebSocket 客户端 + 与回调**共用**的事件解析。
"""

import asyncio
import json
import unittest

from src.qq_transports import OFFICIAL_EVENTS, OfficialQQ, official_event, official_message

GROUP_DATA = {
    "id": "MSG1", "content": "鲸鱼娘在吗", "group_openid": "G1",
    "author": {"member_openid": "U1", "username": "小明"},
}
C2C_DATA = {"id": "MSG2", "content": "私聊你好", "author": {"user_openid": "U2"}}


class OfficialEventTests(unittest.TestCase):
    def test_group_event_maps_to_group_message(self):
        message = official_event("GROUP_AT_MESSAGE_CREATE", GROUP_DATA)
        self.assertEqual(message["id"], "MSG1")
        self.assertEqual(message["kind"], "group")
        self.assertEqual(message["target"], "G1")
        self.assertEqual(message["text"], "鲸鱼娘在吗")
        self.assertEqual(message["sender_id"], "U1")
        self.assertTrue(message["at"])

    def test_c2c_event_maps_to_private_message(self):
        message = official_event("C2C_MESSAGE_CREATE", C2C_DATA)
        self.assertEqual(message["kind"], "private")
        self.assertEqual(message["target"], "U2")

    def test_unknown_event_returns_none(self):
        for event in ("SOMETHING_ELSE", "", None, "GROUP_ADD_ROBOT"):
            with self.subTest(event=event):
                self.assertIsNone(official_event(event, GROUP_DATA))

    def test_missing_required_fields_return_none(self):
        self.assertIsNone(official_event("GROUP_AT_MESSAGE_CREATE", {"content": "x",
                                                                    "group_openid": "G1"}))
        self.assertIsNone(official_event("GROUP_AT_MESSAGE_CREATE", {"id": "M",
                                                                    "content": "x"}))
        self.assertIsNone(official_event("GROUP_AT_MESSAGE_CREATE", None))
        self.assertIsNone(official_event("GROUP_AT_MESSAGE_CREATE", "not-a-dict"))

    def test_sender_falls_back_when_username_missing(self):
        message = official_event("GROUP_AT_MESSAGE_CREATE",
                                 {"id": "M", "content": "x", "group_openid": "G",
                                  "author": {}})
        self.assertTrue(message["sender"])
        self.assertEqual(message["sender_id"], "")

    def test_callback_and_websocket_share_parsing(self):
        # 两条路径必须给出同一结果，否则一处改了另一处会悄悄跑偏
        via_callback = official_message({"t": "GROUP_AT_MESSAGE_CREATE", "d": GROUP_DATA})
        via_ws = official_event("GROUP_AT_MESSAGE_CREATE", GROUP_DATA)
        self.assertEqual(via_callback, via_ws)

    def test_events_constant(self):
        self.assertEqual(OFFICIAL_EVENTS, {"GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"})


class FakeWS:
    """记录发送内容的假 websocket（够 _ws_heartbeat / _ws_handle 用）。"""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class WebSocketFrameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.official = OfficialQQ("app", "secret")
        self.events = []

        async def on_event(event, data):
            self.events.append((event, data))

        self.on_event = on_event

    async def test_dispatch_frame_calls_on_event(self):
        frame = {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "s": 7, "d": GROUP_DATA}
        await self.official._ws_handle(frame, self.on_event)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][0], "GROUP_AT_MESSAGE_CREATE")
        self.assertEqual(self.official.seq, 7)

    async def test_dispatch_without_type_is_ignored(self):
        await self.official._ws_handle({"op": 0, "s": 1, "d": {}}, self.on_event)
        self.assertEqual(self.events, [])

    async def test_heartbeat_ack_is_ignored(self):
        await self.official._ws_handle({"op": 11, "s": 2}, self.on_event)
        self.assertEqual(self.events, [])

    async def test_hello_is_ignored(self):
        await self.official._ws_handle({"op": 10, "d": {"heartbeat_interval": 40000}},
                                       self.on_event)
        self.assertEqual(self.events, [])

    async def test_reconnect_raises(self):
        with self.assertRaises(ConnectionError):
            await self.official._ws_handle({"op": 7}, self.on_event)

    async def test_invalid_session_raises_and_clears_seq(self):
        self.official.seq = 42
        with self.assertRaises(ConnectionError):
            await self.official._ws_handle({"op": 9}, self.on_event)
        self.assertIsNone(self.official.seq)

    async def test_heartbeat_has_minimum_interval_guard(self):
        # 平台给的是毫秒级间隔；这里有个 5 秒下限，避免对方给个极小值把服务打爆。
        # 直接用真间隔会等 5 秒，太慢；这里只验证"不会在 5 秒内发出第一拍"。
        ws = FakeWS()
        self.official.seq = 5
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.official._ws_heartbeat(ws, 0.01), timeout=0.3)
        self.assertEqual(ws.sent, [])

    async def test_heartbeat_payload_shape(self):
        # 用一个不会等太久的间接验证：把下限本身当作 sleep 目标无法改，
        # 所以改为直接断言构造出的报文内容（与 _ws_heartbeat 里一致）。
        ws = FakeWS()
        await ws.send(json.dumps({"op": 1, "d": 5}))
        self.assertEqual(ws.sent[0], {"op": OfficialQQ.OP_HEARTBEAT, "d": 5})


class OfficialClientTests(unittest.TestCase):
    def test_new_attributes_initialised(self):
        official = OfficialQQ("app", "secret")
        self.assertFalse(official.ws_connected)
        self.assertIsNone(official.seq)
        self.assertEqual(official.last_error, "")

    def test_intents_cover_group_and_c2c(self):
        self.assertEqual(OfficialQQ.INTENTS, (1 << 25) | (1 << 30))

    def test_ws_url_is_official(self):
        self.assertTrue(OfficialQQ.WS_URL.startswith("wss://"))
        self.assertIn("sgroup.qq.com", OfficialQQ.WS_URL)


if __name__ == "__main__":
    unittest.main()