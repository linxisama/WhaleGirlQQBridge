"""QQ 账号直连（QqBridge / SnowLuma OneBot 出站）验收。

与 test_onebot_acceptance 对称，但方向相反：QqBridge 是**出站** OneBot v11 客户端，
主动连到本进程里起的一个 websockets 服务（扮演 SnowLuma），覆盖：
1. 连接握手与登录探测（get_login_info / get_status → self_id / online）
2. 出站发送（send_group_msg 的 action 与参数被服务端收到）
3. 入站解析（服务端推一条群消息 → bridge.on_event 收到 onebot_message 形状）
4. 心跳更新 online
5. close() 幂等、断开后台任务

不需要真实 QQ / SnowLuma，只验传输层链路，回归成本极低。
"""
import asyncio
import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import websockets  # noqa: E402

from src.qq_transports import QqBridge  # noqa: E402


class FakeSnowLuma:
    """一个最简 OneBot v11 服务端：应答 call()、可推送事件。"""

    def __init__(self):
        self._ws = None
        self.sent = []          # 收到的出站 action（dict）

    async def handler(self, ws):
        self._ws = ws
        while True:
            raw = await ws.recv()
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            action = data.get("action")
            echo = data.get("echo")
            if action == "get_login_info":
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo,
                                          "data": {"user_id": 999}}))
            elif action == "get_status":
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo,
                                          "data": {"online": True, "good": True}}))
            elif action in ("send_group_msg", "send_private_msg"):
                self.sent.append(data)
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo, "data": {}}))
            else:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo, "data": {}}))

    async def push(self, payload):
        if self._ws:
            await self._ws.send(json.dumps(payload))


class QqBridgeAcceptanceTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.bridge = None
        self._server = None
        self.events = []

    def _make_on_event(self):
        def on_event(message):
            self.events.append(message)
            return None
        return on_event

    async def asyncTearDown(self):
        if self.bridge is not None:
            await self.bridge.close()
        server = self._server
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass

    async def start_server(self):
        # 绑 0 端口让系统分配空闲端口，避开固定端口被防火墙/保留拒绝（winerror 10013）
        self.fake = FakeSnowLuma()
        self._server = await websockets.serve(self.fake.handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.bridge = QqBridge(ws_url=f"ws://127.0.0.1:{port}", access_token="tok", snowluma_exe="")
        self.bridge.on_event = self._make_on_event()
        await self.bridge.connect()
        # connect 非阻塞（首次需扫码），轮询等 probe 把 online 置 True
        for _ in range(120):
            if self.bridge.online:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.bridge.online, "连接未就绪：last_error=" + self.bridge.last_error)
        self.assertEqual(self.bridge.self_id, "999")

    async def test_login_probe(self):
        await self.start_server()
        await self.bridge.probe()
        self.assertTrue(self.bridge.online)
        self.assertEqual(self.bridge.self_id, "999")

    async def test_group_round_trip(self):
        await self.start_server()
        # 出站：群里发一条回复
        await self.bridge.send({"kind": "group", "target": 123456789, "id": "m1"}, "你好呀")
        self.assertTrue(any(s.get("action") == "send_group_msg" for s in self.fake.sent))
        grp = [s for s in self.fake.sent if s.get("action") == "send_group_msg"][0]
        self.assertEqual(grp["params"]["group_id"], 123456789)
        segs = grp["params"]["message"]
        self.assertIn({"type": "text", "data": {"text": "你好呀"}}, segs)

    async def test_inbound_group_event(self):
        await self.start_server()
        await self.fake.push({
            "post_type": "message", "message_type": "group", "group_id": 111,
            "user_id": 222, "message_id": 5,
            "message": [{"type": "text", "data": {"text": "hi"}},
                        {"type": "at", "data": {"qq": "999", "name": "bot"}}],
            "sender": {"nickname": "A"}})
        for _ in range(50):
            if self.events:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.events)
        msg = self.events[0]
        self.assertEqual(msg["kind"], "group")
        self.assertEqual(msg["target"], "111")
        self.assertTrue(msg["at"])
        self.assertEqual(msg["sender_id"], "222")

    async def test_heartbeat_updates_online(self):
        await self.start_server()
        self.bridge.online = False
        await self.fake.push({"meta_event_type": "heartbeat",
                              "status": {"online": True, "good": True}})
        for _ in range(50):
            if self.bridge.online:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.bridge.online)

    async def test_send_offline_raises(self):
        # 未 connect（offline）时发送要抛 ConnectionError，而不是静默
        offline = QqBridge(ws_url="ws://127.0.0.1:0", snowluma_exe="")
        with self.assertRaises(ConnectionError):
            await offline.send({"kind": "group", "target": 1, "id": "x"}, "hi")
        await offline.close()

    async def test_close_idempotent(self):
        await self.start_server()
        await self.bridge.close()
        await self.bridge.close()
        self.assertIsNone(self.bridge.ws)
        self.assertFalse(self.bridge.online)


if __name__ == "__main__":
    unittest.main()
