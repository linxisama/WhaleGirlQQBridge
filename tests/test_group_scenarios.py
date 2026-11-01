"""群聊场景集成测试：多人、连发、图片、串话、限流、重启。

用户反复反馈「群聊回复还有很多 bug」。这个文件把群聊里最容易出问题的几件事
按**真实事件形状**（官方 GROUP_AT_MESSAGE_CREATE）跑一遍，断言"人、话、上下文"
三者不会串：谁说的要认得出、回给谁要清楚、不同人/不同群的历史不能混。
"""
import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest
import uuid
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402
from src.qq_transports import official_event  # noqa: E402

USER_ROOT = pathlib.Path.home() / "AppData" / "Local" / "WhaleGirlQQ"
GROUP_A = "1CBE87EF553193840AFFDCC788043580"
GROUP_B = "4CB29DE9C5991853BE7EA85E4D1074DE"
ALICE = "3B511E5EC940DAA7EF2CD5BCCD42EAEA"
BOB = "83422AE563B2857513F2BA0AF1809DC8"
BOT = "BOTOPENID1234567890ABCDEF1234567"
TEXT = "群聊还是有些bug"


class GroupScenarioTests(unittest.TestCase):
    """官方群聊的端到端行为（不联网：假的模型 + 假的发送通道）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
        user_styles = USER_ROOT / "styles"
        if user_styles.is_dir():
            shutil.copytree(user_styles, root / "styles", dirs_exist_ok=True)
        data = root / "data" / "qq-only"
        data.mkdir(parents=True, exist_ok=True)
        # 这些用例只需要"一份可有可无的插件设置"：用户数据目录里不一定有这个文件
        # （全新机器 / 清过缓存时就没有），没有就写一份最小合法 JSON 顶上。
        user_settings = USER_ROOT / "data" / "qq-only" / "plugin_settings.json"
        if user_settings.exists():
            shutil.copy2(user_settings, data / "plugin_settings.json")
        else:
            (data / "plugin_settings.json").write_text("{}", encoding="utf-8")
        # 同一类问题：本机没有用户配置时不能让 setUp 直接 FileNotFoundError。
        # 缺就用一份最小合法配置（其余字段由 ConfigManager.DEFAULT_CONFIG 补齐）。
        user_config = USER_ROOT / "config.qq.yaml"
        config_text = (user_config.read_text(encoding="utf-8") if user_config.exists()
                       else "qq:\n  mode: official\n")
        # 测试固定用官方模式：用户可能在本机把模式改成 qqbridge/onebot，
        # 那不该让"官方群场景"测试跟着变。
        import re as _re
        config_text = _re.sub(r"(?m)^  mode:.*$", "  mode: official", config_text, count=1)
        (root / "config.qq.yaml").write_text(config_text, encoding="utf-8")
        self.bot = QQRuntime(root=root, config_path=root / "config.qq.yaml",
                             db_path=root / "t.sqlite3")
        self.db_path = root / "t.sqlite3"
        self.bot.running = True
        self.bot.official.authenticated = True
        self.sent = []
        self.prompts = []

        async def fake_send(message, text, meta=None):
            self.sent.append({"kind": message.get("kind"), "target": message.get("target"),
                              "text": text, "meta": meta or {}})
            return {"id": str(len(self.sent))}

        async def fake_chat(messages, **kwargs):
            self.prompts.append(json.dumps(messages, ensure_ascii=False))
            await asyncio.sleep(0.02)
            return f"回复{len(self.sent) + 1}"

        self.bot.official.send = fake_send
        self.bot.ai.chat = fake_chat

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    # ── 工具 ────────────────────────────────────────────────────────────────
    def event(self, sender_id, name, text=TEXT, group=GROUP_A, mid=None, images=0):
        # mid 必须唯一：重复的 mid 会被当成"平台重发"直接去重丢掉
        # （用 time_ns() 会撞车 —— Windows 时钟精度不够，测试里连发几条会只剩一条）
        payload = {"id": mid or f"M{uuid.uuid4().hex}", "content": f"<@!{BOT}> {text}",
                   "group_openid": group,
                   "author": {"member_openid": sender_id, "username": name}}
        if images:
            payload["attachments"] = [
                {"content_type": "image/png", "url": f"https://gchat.qpic.cn/{index}.png",
                 "filename": f"{index}.png"} for index in range(images)]
        return official_event("GROUP_AT_MESSAGE_CREATE", payload)

    async def feed(self, messages, wait=8.0):
        """把事件喂进去，等所有消息都不再是 pending。

        等待要足够长：连发合并（默认静默 0.6 秒）＋模型调用会让一条消息花掉一秒多，
        多个人的消息是**串行**处理的，等太短会把"还在处理"误判成"没回复"。
        """
        for message in messages:
            self.bot.ingest(message, "official")
        deadline = time.time() + wait
        while time.time() < deadline:
            await asyncio.sleep(0.05)
            pending = self.bot.db.execute(
                "SELECT count(*) FROM inbox WHERE state IN ('pending','sending')").fetchone()[0]
            if not pending:
                break
        await asyncio.sleep(0.3)

    def replies(self):
        return [item["text"] for item in self.sent]

    def last_prompt(self):
        return self.prompts[-1] if self.prompts else ""

    # ── 场景 ────────────────────────────────────────────────────────────────
    def test_two_members_both_get_replies_and_are_kept_apart(self):
        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            await self.feed([self.event(ALICE, "慕白", "第一个问题"),
                             self.event(BOB, "西莲寺紫", "第二个问题")])

        asyncio.run(run())
        self.assertEqual(len(self.sent), 2, self.sent)
        self.assertTrue(all(item["target"] == GROUP_A for item in self.sent))
        rows = self.bot.db.execute(
            "SELECT json_extract(payload,'$.sender_name'), reply FROM inbox ORDER BY created").fetchall()
        self.assertEqual([row[0] for row in rows], ["慕白", "西莲寺紫"])
        self.assertEqual([row[1] for row in rows], ["回复1", "回复2"])

    def test_prompt_carries_the_speaker_name_not_just_openid(self):
        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            await self.feed([self.event(ALICE, "慕白", "我是谁")])

        asyncio.run(run())
        prompt = self.last_prompt()
        self.assertIn("慕白", prompt, "群聊里模型必须知道是谁在说话")
        self.assertIn(ALICE, prompt, "同时要保留 openid，供区分同名的人")

    def test_group_reply_is_sent_back_to_the_same_group(self):
        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            await self.feed([self.event(ALICE, "慕白", text="在吗", group=GROUP_B)])

        asyncio.run(run())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["kind"], "group")
        self.assertEqual(self.sent[0]["target"], GROUP_B)

    def test_two_groups_do_not_share_history(self):
        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            await self.feed([self.event(ALICE, "慕白", "A群第一句", group=GROUP_A)])
            first = self.last_prompt()
            await self.feed([self.event(BOB, "西莲寺紫", "B群第一句", group=GROUP_B)])
            second = self.last_prompt()
            return first, second

        first, second = asyncio.run(run())
        self.assertIn("A群第一句", first)
        self.assertNotIn("A群第一句", second, "B 群不该看到 A 群的对话")
        self.assertIn("B群第一句", second)

    def test_image_message_reaches_the_group_with_a_description(self):
        async def run():
            with mock.patch.object(self.bot.plugins, "describe_images",
                                   new=mock.AsyncMock(side_effect=lambda message, bot: message.update(
                                       {"vision_description": "一只白猫", "vision_status": "ok"}))):
                self.bot.worker = asyncio.create_task(self.bot._work())
                await self.feed([self.event(ALICE, "慕白", "看看这个", images=1)])

        asyncio.run(run())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["target"], GROUP_A)
        self.assertIn("一只白猫", self.last_prompt())

    def test_burst_from_one_member_becomes_one_reply(self):
        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            await self.feed([self.event(ALICE, "慕白", "第一句"),
                             self.event(ALICE, "慕白", "第二句"),
                             self.event(ALICE, "慕白", "第三句")], wait=3.0)

        asyncio.run(run())
        self.assertLessEqual(len(self.sent), 2, f"连发不该刷屏：{self.replies()}")
        states = dict(self.bot.db.execute("SELECT state, count(*) FROM inbox GROUP BY state").fetchall())
        self.assertIn("merged", states, f"连发应当被合并：{states}")

    def test_reply_of_one_person_is_not_attributed_to_another(self):
        """A 的历史不能以"对话轮次"出现在 B 的请求里（A 的话只能作为标注过的背景）。"""

        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            await self.feed([self.event(ALICE, "慕白", "甲说的话")])
            await self.feed([self.event(BOB, "西莲寺紫", "乙说的话")])

        asyncio.run(run())
        self.assertGreaterEqual(len(self.prompts), 2, self.prompts)
        second = json.loads(self.prompts[-1])
        for item in second:
            if item.get("role") == "user" and "甲说的话" in item.get("content", ""):
                self.assertIn("背景", item["content"],
                              "上一个人的话只能作为背景出现，不能当成对话轮次")
        # 当前这条必须原样在场
        self.assertTrue(any("乙说的话" in (item.get("content") or "") for item in second))

    def test_rate_limit_works_across_different_members(self):
        """限流按"这个会话一分钟内回复了几条"算 —— 换人说话也算，不能靠连发合并绕过去。"""

        async def run():
            self.bot.plugins.update_settings("chat_controls", {"perMinute": 3})
            self.bot.worker = asyncio.create_task(self.bot._work())
            members = [f"MEMBER{index}AAAAAAAAAAAAAAAAAAAAAAAAAAAAA"[:32] for index in range(5)]
            await self.feed([self.event(member, f"群友{index}", f"第{index}个问题")
                             for index, member in enumerate(members)], wait=12.0)

        asyncio.run(run())
        reasons = [item["reason"] for item in self.bot.decisions]
        self.assertTrue(any("限流" in reason or "点名" in reason for reason in reasons),
                        f"超过每分钟上限时要拦下并写清原因：{reasons[-8:]}")
        audit = self.bot.db.execute(
            "SELECT outcome, reason FROM inbound_audit ORDER BY at").fetchall()
        self.assertTrue(any(row[0] == "dropped" for row in audit), f"审计里要能看到被拦：{audit}")
        self.assertLessEqual(len(self.sent), 4, f"限流后不该人人有份：{self.replies()}")

    def test_one_member_flooding_is_merged_not_answered_six_times(self):
        """同一个人刷屏：应当合并成少量回复（不刷屏），也不必靠丢弃实现。"""

        async def run():
            self.bot.plugins.update_settings("chat_controls", {"perMinute": 3})
            self.bot.worker = asyncio.create_task(self.bot._work())
            await self.feed([self.event(ALICE, "慕白", f"刷屏第{index}句") for index in range(6)],
                            wait=10.0)

        asyncio.run(run())
        self.assertLessEqual(len(self.sent), 3, f"六连发不该回六条：{self.replies()}")
        states = dict(self.bot.db.execute(
            "SELECT state, count(*) FROM inbox GROUP BY state").fetchall())
        self.assertTrue(states.get("merged", 0) >= 1, f"应当有消息被合并：{states}")

    def test_group_history_survives_restart(self):
        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            await self.feed([self.event(ALICE, "慕白", "重启前说的话")])

        asyncio.run(run())
        asyncio.run(self.bot.close())
        # 用同一个数据目录重新起一个运行时：历史与记忆应当还在
        revived = QQRuntime(root=self.bot.root, config_path=self.bot.root / "config.qq.yaml",
                            db_path=self.db_path)
        try:
            rows = revived.db.execute(
                "SELECT substr(reply,1,10) FROM inbox WHERE room=? ORDER BY created",
                (f"group:{GROUP_A}",)).fetchall()
            self.assertTrue(rows, "重启后群聊记录不该丢")
            self.assertIn("回复", rows[0][0])
        finally:
            try:
                revived.db.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
