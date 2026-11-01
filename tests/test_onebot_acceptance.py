"""普通端（OneBot 11）真实链路验收。

参考项目 dsh-sd-bridge-guard 的测试纪律：不只做单元测试，还要有**真机验收**一层。
这里就是普通端的验收层 —— 真起一个 FastAPI 服务、真开 WebSocket、真按 OneBot 11
规范收发，断言"收到事件 → 生成回复 → 发出正确的 API 调用"整条链路。

覆盖：
1. 握手与鉴权（token 正确/错误、角色不是 Universal、模式不对、已在连接中）
2. 登录探测（get_login_info / get_status，未登录要报错并断开）
3. 入站：群消息（@ 机器人）、私聊消息、图片段、戳一戳 notice、心跳 meta_event
4. 出站：send_group_msg / send_private_msg 的 action 与参数
5. 异常形状不崩：缺字段、类型不对、空段列表
"""
import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import uvicorn  # noqa: E402
import websockets  # noqa: E402

from src.qq_app import QQRuntime, create_app  # noqa: E402

TOKEN = "onebot-secret-token"
SELF_ID = "10001"
GROUP = 123456789
USER = 987654321


def build_root(tmp: pathlib.Path) -> pathlib.Path:
    (tmp / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (tmp / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    (tmp / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1/chat/completions\n  api_key: fake\n"
        "  model: test\n"
        "qq_only:\n  mode: onebot\n"
        f"  onebot_token: {TOKEN}\n"
        "qq:\n  enabled: true\n", encoding="utf-8")
    return tmp


class OneBotHarness:
    """一个最小的 OneBot 11 客户端：连上、应答机器人发来的 API 调用、记录收到的动作。"""

    def __init__(self, url: str, *, token: str = TOKEN, role: str = "Universal",
                 self_id: str = SELF_ID, logged_in: bool = True, omit_status: bool = False):
        self.url = url
        self.token = token
        self.role = role
        self.self_id = self_id
        self.logged_in = logged_in
        self.omit_status = omit_status   # True = 回包只带 retcode（模拟不带 status 的实现）
        self.ws = None
        self.actions = []          # 机器人发来的 API 调用
        self.closed_code = None
        self._task = None
        self.replies = []          # 机器人发来的消息（从 send_* 里解析出来）
        self.auto_accept_tasks = True

    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "X-Client-Role": self.role,
                "X-Self-ID": self.self_id}

    async def connect(self):
        self.ws = await websockets.connect(self.url, additional_headers=self.headers(),
                                          open_timeout=10)
        self._task = asyncio.create_task(self._pump())
        return self.ws

    async def _pump(self):
        """收机器人的 API 调用，按 OneBot 规范回 echo，并记录发送类动作。"""
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                self.actions.append(data)
                action = str(data.get("action") or "")
                if action.startswith("send_"):
                    self.replies.append(data)
                if "echo" not in data:
                    continue
                if action == "get_login_info":
                    result = {"status": "ok", "retcode": 0,
                              "data": {"user_id": int(self.self_id), "nickname": "鲸鱼娘"},
                              "echo": data["echo"]}
                    if not self.logged_in:
                        result = {"status": "failed", "retcode": 100, "data": None,
                                  "echo": data["echo"]}
                elif action == "get_status":
                    result = {"status": "ok", "retcode": 0,
                              "data": {"online": bool(self.logged_in), "good": True},
                              "echo": data["echo"]}
                else:
                    result = {"status": "ok", "retcode": 0,
                              "data": {"message_id": 1}, "echo": data["echo"]}
                if self.omit_status:
                    result.pop("status", None)
                await self.ws.send(json.dumps(result))
        except Exception:
            pass

    async def wait_for_action(self, action: str, timeout: float = 12.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for item in self.actions:
                if item.get("action") == action:
                    return item
            await asyncio.sleep(0.05)
        return None

    async def wait_for_reply(self, timeout: float = 12.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self.replies:
                return self.replies[0]
            await asyncio.sleep(0.05)
        return None

    async def send_event(self, payload: dict):
        await self.ws.send(json.dumps(payload))

    async def close(self):
        if self._task:
            self._task.cancel()
            # CancelledError 继承自 BaseException，用 Exception 抓不住
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None


class OneBotServerMixin:
    """起一个真 uvicorn（进程内、同一事件循环），并给出可连接的 ws:// 地址。"""

    async def start_server(self, *, mode: str = "onebot", running: bool = True,
                           token: str = TOKEN):
        self._tmp = tempfile.TemporaryDirectory()
        root = build_root(pathlib.Path(self._tmp.name))
        shutil.copytree(pathlib.Path(__file__).resolve().parents[1] / "styles",
                        root / "styles", dirs_exist_ok=True)
        self.bot = QQRuntime(root=root, config_path=root / "config.qq.yaml",
                             db_path=root / "t.sqlite3")
        self.bot.config.set("qq_only.mode", mode)
        self.bot.config.set("qq_only.onebot_token", token)
        # 本文件验证"没点名不回复"的旧契约；随机接话插件默认开启，这里显式关掉。
        self.bot.plugins.update_settings('ambient_reply', {'enabled': False})
        self.bot.running = running
        # 模型调用替换掉：验收只关心链路，不关心模型
        sent = []

        async def fake_chat(messages, **kwargs):
            return "在的，干嘛"

        self.bot.ai.chat = fake_chat
        self.sent = sent
        app = create_app(self.bot)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error",
                                log_config=None, lifespan="on")
        self.server = uvicorn.Server(config)
        self._serve = asyncio.create_task(self.server.serve())
        for _ in range(200):
            await asyncio.sleep(0.05)
            if getattr(self.server, "started", False):
                break
        else:
            raise RuntimeError("uvicorn 没起来")
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/qq/onebot"
        self.bot.worker = asyncio.create_task(self.bot._work())
        return self.url

    async def stop_server(self):
        try:
            self.server.should_exit = True
            await asyncio.wait_for(self._serve, timeout=10)
        except Exception:
            pass
        for task in (getattr(self, "bot", None) and getattr(self.bot, "worker", None),):
            try:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                pass
        try:
            await self.bot.close()
        except Exception:
            pass
        try:
            self.bot.db.close()          # Windows 下不关 sqlite，临时目录删不掉（WinError 32）
        except Exception:
            pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def group_message(self, text: str, *, at_bot: bool = True, message_id: int = 1,
                      extra_segments=None):
        segments = []
        if at_bot:
            segments.append({"type": "at", "data": {"qq": SELF_ID}})
        if text:
            segments.append({"type": "text", "data": {"text": " " + text}})
        segments.extend(extra_segments or [])
        return {"post_type": "message", "message_type": "group", "sub_type": "normal",
                "message_id": message_id, "group_id": GROUP,
                "user_id": USER, "self_id": int(SELF_ID), "time": 1760000000,
                "raw_message": text, "font": 0, "sender": {"nickname": "西莲寺紫"},
                "message": segments}

    def private_message(self, text: str, *, message_id: int = 2):
        return {"post_type": "message", "message_type": "private", "sub_type": "friend",
                "message_id": message_id, "user_id": USER, "self_id": int(SELF_ID),
                "time": 1760000000, "raw_message": text, "font": 0,
                "sender": {"nickname": "西莲寺紫"},
                "message": [{"type": "text", "data": {"text": text}}]}


class OneBotAcceptanceTests(OneBotServerMixin, unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.loop.close()

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    # ── 1. 握手与鉴权 ──────────────────────────────────────────────────────
    def test_valid_client_connects_and_probes_login(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                info = await client.wait_for_action("get_login_info")
                status = await client.wait_for_action("get_status")
                self.assertIsNotNone(info, "连上后应当探测登录信息")
                self.assertIsNotNone(status, "连上后应当探测在线状态")
                self.assertTrue(self.bot.onebot.online, "探测通过后应当标记在线")
                self.assertEqual(self.bot.onebot.self_id, SELF_ID)
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_wrong_token_is_rejected(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url, token="wrong-token")
            with self.assertRaises(Exception):
                await client.connect()
                await client.wait_for_action("get_login_info", timeout=2)
            await self.stop_server()
        self.run_async(main())

    def test_non_universal_role_is_rejected(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url, role="API")
            with self.assertRaises(Exception):
                await client.connect()
                await client.wait_for_action("get_login_info", timeout=2)
            await self.stop_server()
        self.run_async(main())

    def test_connection_refused_when_bot_not_running(self):
        async def main():
            await self.start_server(running=False)
            client = OneBotHarness(self.url)
            with self.assertRaises(Exception):
                await client.connect()
                await client.wait_for_action("get_login_info", timeout=2)
            await self.stop_server()
        self.run_async(main())

    def test_logged_out_client_is_disconnected(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url, logged_in=False)
            try:
                await client.connect()
                for _ in range(60):
                    await asyncio.sleep(0.1)
                    if self.bot.onebot.ws is None:
                        break
                self.assertFalse(self.bot.onebot.online, "未登录不该标记在线")
                error = self.bot.onebot.last_error or ""
                self.assertIn("尚未登录", error, f"提示要说清是没登录，而不是只给错误码：{error}")
                self.assertIn("get_login_info", error, "提示要写清是哪一步调用失败")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    # ── 2. 入站 → 出站 ─────────────────────────────────────────────────────
    def test_group_at_message_gets_a_group_reply(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                await client.send_event(self.group_message("在吗"))
                reply = await client.wait_for_reply()
                self.assertIsNotNone(reply, f"群里 @ 机器人后应当回消息，实际动作：{client.actions}")
                self.assertEqual(reply["action"], "send_group_msg")
                self.assertEqual(reply["params"]["group_id"], GROUP)
                segments = reply["params"]["message"]
                self.assertTrue(any(seg.get("type") == "text" and "在的" in seg["data"]["text"]
                                    for seg in segments), segments)
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_private_message_gets_a_private_reply(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                await client.send_event(self.private_message("在吗"))
                reply = await client.wait_for_reply()
                self.assertIsNotNone(reply, f"私聊应当回消息，实际动作：{client.actions}")
                self.assertEqual(reply["action"], "send_private_msg")
                self.assertEqual(reply["params"]["user_id"], USER)
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_group_message_with_image_segment_is_ingested(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                event = self.group_message("看看这个", extra_segments=[
                    {"type": "image", "data": {"file": "a.png", "url": "https://example.test/a.png"}}])
                await client.send_event(event)
                for _ in range(80):
                    await asyncio.sleep(0.1)
                    rows = self.bot.db.execute(
                        "SELECT json_extract(payload,'$.media') FROM inbox").fetchall()
                    if rows:
                        break
                self.assertTrue(rows, "带图片的群消息应当进入队列")
                media = json.loads(rows[0][0] or "[]")
                self.assertTrue(media, f"媒体段应当被解析出来：{rows[0][0]}")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_poke_notice_does_not_break_the_connection(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                await client.send_event({
                    "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                    "time": 1760000000, "self_id": int(SELF_ID), "user_id": USER,
                    "target_id": int(SELF_ID), "group_id": GROUP})
                await client.send_event(self.group_message("还在吗", message_id=9))
                reply = await client.wait_for_reply()
                self.assertIsNotNone(reply, "戳一戳之后链路仍应能正常回复")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_group_message_without_at_is_not_replied(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                await client.send_event(self.group_message("没点名的一句话", at_bot=False))
                await asyncio.sleep(3.0)
                self.assertEqual(client.replies, [], f"没 @ 机器人不该回复：{client.replies}")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    # ── 3. 异常形状不崩 ────────────────────────────────────────────────────
    def test_garbage_events_keep_the_connection_alive(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                garbage = [
                    {}, {"post_type": "message"},
                    {"post_type": "message", "message_type": "group"},
                    {"post_type": "message", "message_type": "group", "message": None},
                    {"post_type": "message", "message_type": "group", "message": []},
                    {"post_type": "message", "message_type": "group", "message": "字符串"},
                    {"post_type": "message", "message_type": "group", "message": [
                        {"type": "at", "data": None}]},
                    {"post_type": "notice", "notice_type": "notify", "sub_type": "poke"},
                    {"post_type": "meta_event", "meta_event_type": "heartbeat",
                     "status": "不是字典"},
                    {"post_type": "request", "request_type": "friend"},
                ]
                for payload in garbage:
                    await client.send_event(payload)
                await asyncio.sleep(1.0)      # 让后台把坏事件消化掉（会留一条审计）
                await client.send_event(self.group_message("还能回吗", message_id=99))
                reply = await client.wait_for_reply(timeout=15)
                self.assertIsNotNone(reply, f"畸形事件之后连接仍应可用：{client.actions[-4:]}")
                self.assertIsNotNone(self.bot.onebot.ws, "连接不该被畸形事件打断")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_string_message_format_does_not_drop_the_connection(self):
        """OneBot 11 字符串上报应与数组一样回复，且下一条仍可正常处理。

        实测（.tmp-work/probe_onebot_garbage.py）：message 为 None / 字符串 / 缺失
        以前会一路冒到接收循环把连接关掉 —— 用户看到"机器人突然掉线"。
        """
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                await client.send_event({"post_type": "message", "message_type": "group",
                                         "group_id": GROUP, "user_id": USER,
                                         "message_id": 5, "message": f"[CQ:at,qq={SELF_ID}] 字符串格式的上报"})
                self.assertIsNotNone(await client.wait_for_reply(timeout=15),
                                     "合法的字符串格式消息必须得到回复")
                self.assertIsNotNone(self.bot.onebot.ws, "连接必须保持")
                self.assertNotIn("array", getattr(self.bot,'last_error','') or "")
                await client.send_event(self.group_message("还能回吗", message_id=99))
                self.assertIsNotNone(await client.wait_for_reply(timeout=15),
                                     "字符串事件之后仍应能正常回复数组消息")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_weird_heartbeat_status_does_not_drop_the_connection(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                self.bot.onebot.online = True
                await client.send_event({"post_type": "meta_event",
                                         "meta_event_type": "heartbeat",
                                         "status": "不是字典", "time": 1760000000})
                await asyncio.sleep(0.6)
                self.assertIsNotNone(self.bot.onebot.ws, "心跳畸形不该掉线")
                await client.send_event(self.group_message("心跳之后", message_id=100))
                self.assertIsNotNone(await client.wait_for_reply(timeout=8))
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_unparsed_event_is_recorded_for_diagnosis(self):
        """坏事件要能"未回复诊断"里查到，否则用户只会觉得"它没回我"。"""
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                await client.send_event({"post_type": "message", "message_type": "group",
                                         "group_id": GROUP, "user_id": USER,
                                         "message_id": 6, "message": {"invalid": "not a message"}})
                rows = []
                for _ in range(60):
                    await asyncio.sleep(0.05)
                    rows = self.bot.db.execute(
                        "SELECT outcome, reason FROM inbound_audit").fetchall()
                    if rows:
                        break
                self.assertTrue(rows, "解析失败的事件也应当留痕")
                self.assertEqual(rows[0][0], "unparsed")
                self.assertTrue(rows[0][1], "要写明原因")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    def test_heartbeat_marks_online(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                self.bot.onebot.online = False
                await client.send_event({
                    "post_type": "meta_event", "meta_event_type": "heartbeat",
                    "time": 1760000000, "self_id": int(SELF_ID),
                    "status": {"online": True, "good": True}})
                for _ in range(60):
                    await asyncio.sleep(0.05)
                    if self.bot.onebot.online:
                        break
                self.assertTrue(self.bot.onebot.online, "心跳应当把状态标成在线")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())


if __name__ == "__main__":
    unittest.main()
