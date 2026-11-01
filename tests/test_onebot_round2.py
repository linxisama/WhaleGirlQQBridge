"""普通端（OneBot 11）第二轮验收：审计列出的缺口与已修问题。

对应 OneBot 链路审计（2026-10-16）里的"仍缺用例"清单，逐条钉住：
- 队列满不能静默丢消息（要有可区分的 overflow 记录 + 处理原因）
- 回包不带 status 也要算成功（否则回复发不出去）
- 健康检查一次失败不能掐断连接（在途回复会变 uncertain）
- 私聊里的 at 段不应把消息判成"被 @"
- at/face 段缺字段时文本不能出现 "None"
- 加好友/加群请求、自己发的消息：不崩、不误回
- 自己发的群消息不入库（防自问自答）
- 两个客户端抢占连接：第二个被拒、第一个不受影响
- 断线重连后仍能正常收发
"""
import asyncio
import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_transports import onebot_message  # noqa: E402
from tests.test_onebot_acceptance import (GROUP, SELF_ID, USER, OneBotHarness,  # noqa: E402
                                          OneBotServerMixin)


class OneBotSecondRoundTests(OneBotServerMixin, unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.loop.close()

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    # ── 队列满：不能静默丢 ─────────────────────────────────────────────────
    def test_full_queue_is_reported_as_overflow_not_silently_dropped(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                # 先把回复循环停掉，否则它几秒内就把预填的 200 条消化完了
                self.bot.worker.cancel()
                await asyncio.gather(self.bot.worker, return_exceptions=True)
                # 预填 200 条 pending，模拟"队列满"
                for index in range(200):
                    self.bot.db.execute(
                        "INSERT INTO inbox(key,mode,account,room,payload,state,created)"
                        " VALUES(?,?,?,?,?,'pending',?)",
                        (f"seed{index}", "onebot", SELF_ID, f"group:{GROUP}",
                         json.dumps({"id": f"seed{index}", "kind": "group",
                                     "target": str(GROUP), "text": "占位"}), 1.0))
                self.bot.db.commit()
                await client.send_event(self.group_message("队列满时发的", message_id=555))
                for _ in range(60):
                    await asyncio.sleep(0.05)
                    rows = self.bot.db.execute(
                        "SELECT outcome, reason FROM inbound_audit WHERE outcome='overflow'"
                    ).fetchall()
                    if rows:
                        break
                self.assertTrue(rows, "队列满时必须留一条可区分的记录，不能静默丢弃："
                                      f"全部审计={self.bot.db.execute('SELECT outcome, reason FROM inbound_audit').fetchall()[:3]}")
                self.assertIn("已满", rows[0][1])
                reasons = [item["reason"] for item in self.bot.decisions]
                self.assertTrue(any("已满" in reason for reason in reasons),
                                f"处理原因里要写明是队列满：{reasons[-3:]}")
                self.assertIsNotNone(self.bot.onebot.ws, "队列满不该断开连接")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    # ── 回包容错 ───────────────────────────────────────────────────────────
    def test_reply_survives_a_response_without_status_field(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            client.omit_status = True          # 只回 retcode，不带 status
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                await client.send_event(self.group_message("在吗"))
                reply = await client.wait_for_reply(timeout=12)
                self.assertIsNotNone(reply, "回包只带 retcode=0 时也必须算成功")
                self.assertEqual(reply["action"], "send_group_msg")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    # ── 健康检查不该掐断连接 ───────────────────────────────────────────────
    def test_health_probe_failure_does_not_drop_the_connection_immediately(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                ws_before = self.bot.onebot.ws
                # 让 probe 连续两次失败（模拟客户端不支持 get_status）
                original = self.bot.onebot.probe
                calls = {"n": 0}

                async def flaky():
                    calls["n"] += 1
                    if calls["n"] <= 2:
                        raise RuntimeError("客户端不支持 get_status")
                    return await original()

                self.bot.onebot.probe = flaky
                from src.qq_app import QQRuntime
                self.bot.running = True
                health = asyncio.create_task(QQRuntime._health(self.bot))
                await asyncio.sleep(0.5)       # 让第一次探测失败
                health.cancel()                # cancel() 不是协程，不能 await
                await asyncio.gather(health, return_exceptions=True)
                self.assertIs(self.bot.onebot.ws, ws_before,
                              "一次/两次探测失败不该断开连接（旧实现会立刻 close）")
                await client.send_event(self.group_message("探测失败后还能回吗", message_id=88))
                self.assertIsNotNone(await client.wait_for_reply(timeout=12))
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    # ── 私聊 at 段不参与 @ 判定 ────────────────────────────────────────────
    def test_private_message_with_at_segment_is_not_treated_as_mentioned(self):
        message = onebot_message({
            "post_type": "message", "message_type": "private", "message_id": 3,
            "user_id": USER, "self_id": int(SELF_ID), "time": 1760000000,
            "sender": {"nickname": "西莲寺紫"},
            "message": [{"type": "at", "data": {"qq": SELF_ID}},
                        {"type": "text", "data": {"text": " 私聊里带 at 段"}}]},
            SELF_ID)
        self.assertFalse(message["at"], "私聊不是被 @")
        self.assertEqual(message["at_ids"], [], "私聊不收集 at_ids（不该影响 @ 判定）")

    # ── 缺字段的段渲染 ─────────────────────────────────────────────────────
    def test_missing_at_and_face_fields_render_readably(self):
        message = onebot_message({
            "post_type": "message", "message_type": "group", "message_id": 4,
            "group_id": GROUP, "user_id": USER, "self_id": int(SELF_ID),
            "time": 1760000000, "sender": {"nickname": "谁"},
            "message": [{"type": "at", "data": {}},
                        {"type": "face", "data": {}},
                        {"type": "text", "data": {"text": " 看看"}}]},
            SELF_ID)
        self.assertNotIn("None", message["text"], f"文本里不该出现 None：{message['text']}")
        self.assertNotIn("None", str(message["at_ids"]), "at_ids 不该出现 'None'")

    # ── 不处理的事件：不崩、不误回 ─────────────────────────────────────────
    def test_request_and_self_sent_events_are_ignored_safely(self):
        async def main():
            await self.start_server()
            client = OneBotHarness(self.url)
            try:
                await client.connect()
                await client.wait_for_action("get_login_info")
                await client.send_event({"post_type": "request", "request_type": "friend",
                                         "user_id": USER, "comment": "求加好友"})
                await client.send_event({"post_type": "message_sent", "message_type": "group",
                                         "group_id": GROUP, "user_id": int(SELF_ID),
                                         "message_id": 7,
                                         "message": [{"type": "text", "data": {"text": "自己发的"}}]})
                # 自己发的群消息（收报路径）
                await client.send_event({"post_type": "message", "message_type": "group",
                                         "group_id": GROUP, "user_id": int(SELF_ID),
                                         "message_id": 8, "time": 1760000000,
                                         "message": [{"type": "text", "data": {"text": "自问自答？"}}]})
                await asyncio.sleep(2.0)
                self.assertEqual(client.replies, [], "不该回复自己发的消息/请求事件")
                self.assertIsNotNone(self.bot.onebot.ws, "这些事件不该断开连接")
                await client.send_event(self.group_message("真实提问", message_id=9))
                self.assertIsNotNone(await client.wait_for_reply(timeout=12),
                                     "跳过无关事件后仍要能正常回复")
            finally:
                await client.close()
                await self.stop_server()
        self.run_async(main())

    # ── 双连接抢占与重连 ───────────────────────────────────────────────────
    def test_second_client_is_rejected_and_reconnect_works(self):
        async def main():
            await self.start_server()
            first = OneBotHarness(self.url)
            try:
                await first.connect()
                await first.wait_for_action("get_login_info")
                second = OneBotHarness(self.url)
                with self.assertRaises(Exception):
                    await second.connect()
                    await second.wait_for_action("get_login_info", timeout=2)
                self.assertIsNotNone(self.bot.onebot.ws, "第一个连接不该被影响")
                await first.send_event(self.group_message("第一个还在吗", message_id=21))
                self.assertIsNotNone(await first.wait_for_reply(timeout=12),
                                     "第一个连接必须照常工作")
            finally:
                await first.close()
            # 断开后重连
            again = OneBotHarness(self.url)
            try:
                await again.connect()
                info = await again.wait_for_action("get_login_info")
                self.assertIsNotNone(info, "断开后应当能重新连上")
                self.assertEqual(self.bot.onebot.self_id, SELF_ID)
                await again.send_event(self.group_message("重连之后", message_id=22))
                self.assertIsNotNone(await again.wait_for_reply(timeout=12),
                                     "重连后必须能继续收发")
            finally:
                await again.close()
                await self.stop_server()
        self.run_async(main())


if __name__ == "__main__":
    unittest.main()
