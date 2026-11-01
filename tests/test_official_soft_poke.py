"""官方模式下的"戳一戳"替代交互（_soft_poke）。

背景（用户 2026-10-17 两轮反馈）：官方模式下反复戳机器人完全没反应。
查他机器上的 inbound_audit：只有 GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE /
READY / GROUP_ADD_ROBOT，**一条 poke 都没有** —— QQ 开放平台不推送戳一戳事件，
程序侧拿不到这个信号。

能做的：官方群 @ 事件允许空 content（"@ 了机器人但一个字没说"）。这是官方接口下
唯一能表达"戳你一下"的信号，所以把它标成 ``_soft_poke``，走戳一戳链路：
统计插件计数、概率插件决定回不回、回则清空历史 + 60 token 短回应（不反戳）。
"""
import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.chat_state import turn_text  # noqa: E402
from src.qq_app import QQRuntime  # noqa: E402
from src.qq_transports import official_event  # noqa: E402

USER_ROOT = pathlib.Path.home() / "AppData" / "Local" / "WhaleGirlQQ"
GROUP = "A9FC46C6BC722A1B8BAADC3F8B2D65AD"
SENDER = "AF0F8E4592235DF38738182B028C2E0B"


def make_bot(tmpdir):
    root = pathlib.Path(tmpdir)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    shutil.copytree(USER_ROOT / "styles", root / "styles", dirs_exist_ok=True)
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1/chat/completions\n  api_key: fake\n"
        "  model: test\nqq:\n  app_id: appid\n  sandbox: false\n"
        "qq_only:\n  mode: official\n", encoding="utf-8")
    return QQRuntime(root=root, config_path=root / "config.qq.yaml",
                     db_path=root / "t.sqlite3")


class OfficialSoftPokeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = make_bot(self._tmp.name)
        self.bot.running = True
        self.prompts, self.sent, self.notes = [], [], []
        self.bot.note = lambda text: self.notes.append(text)
        for plugin_id, values in {
            "poke_counter": {"enabled": True, "replyProbability": 0.0},
            "probability_reply": {"enabled": True, "replyPercent": 100,
                                  "pokeBackPercent": 0, "ignorePokes": False},
        }.items():
            try:
                self.bot.plugins.update_settings(plugin_id, values)
            except Exception:
                pass

        async def fake_chat(messages, **kwargs):
            self.prompts.append({"messages": messages, "kwargs": kwargs})
            return "别戳啦"

        async def fake_send(*args, **kwargs):
            self.sent.append(args)
            return {"id": "1"}

        # _work 只在官方已鉴权时才取队列；测试里直接放行（生产由 authorize 置位）
        self.bot.official.authenticated = True
        self.bot.ai.chat = fake_chat
        self.bot.official.send = fake_send
        self.bot.official.send_active = fake_send

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def event(self, content=" ", attachments=None, event_id="E1"):
        data = {"id": event_id, "content": content, "group_openid": GROUP,
                "author": {"id": SENDER, "member_openid": SENDER},
                "timestamp": "2026-10-17T19:30:00+08:00"}
        if attachments is not None:
            data["attachments"] = attachments
        return official_event("GROUP_AT_MESSAGE_CREATE", data)

    def run_ingest(self, message, wait=1.5):
        async def main():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.bot.ingest(message, "official")
            deadline = time.time() + wait
            while time.time() < deadline:
                await asyncio.sleep(0.05)
                pending = self.bot.db.execute(
                    "SELECT count(*) FROM inbox WHERE state IN ('pending','sending')").fetchone()[0]
                if not pending:
                    break
            await asyncio.sleep(0.2)
        asyncio.run(main())

    def test_empty_at_message_becomes_soft_poke(self):
        message = self.event(content=" ")
        self.assertTrue(message.get("at"), "@ 是平台给的")
        self.assertEqual(message.get("text"), "", "官方空正文解析成空串")
        self.run_ingest(message)
        self.assertTrue(self.prompts, f"应触发一次模型调用；决策={[d.get('reason') for d in self.bot.decisions]}")
        self.assertLessEqual(int(self.prompts[0]["kwargs"].get("max_tokens") or 500), 60)
        self.assertIn("戳", json.dumps(self.prompts[0]["messages"], ensure_ascii=False))

    def test_image_only_at_message_is_not_a_poke(self):
        """@ + 只有图片是正常提问，不能当成戳。"""
        message = self.event(content="", attachments=[
            {"content_type": "image/png", "url": "https://example.test/a.png"}])
        self.assertTrue(message.get("media"))
        self.run_ingest(message)
        text = json.dumps(self.prompts[0]["messages"], ensure_ascii=False) if self.prompts else ""
        self.assertNotIn("戳了戳你", text, "图片消息不该被当成戳一戳")

    def test_soft_poke_counts_in_poke_counter(self):
        counter = self.bot.plugins._plugins.get("poke_counter")
        counter.on_ingest({"kind": "group", "target": GROUP, "text": "[用户点名]",
                           "_soft_poke": True}, self.bot)
        stats = counter._block_for(counter._load(), f"group:{GROUP}")
        self.assertGreaterEqual(int(stats.get("total") or 0), 1, "官方模式的软戳也要计数")

    def test_turn_text_explains_the_soft_poke(self):
        text = turn_text({"kind": "group", "target": GROUP, "text": "[用户点名]",
                          "sender_id": SENDER, "sender_name": "西莲寺紫", "_soft_poke": True})
        self.assertIn("只 @ 了你一下", text)
        self.assertIn("戳", text)


class EmojiTagTests(unittest.TestCase):
    def test_emoji_tag_is_translated(self):
        self.assertEqual(official_event(
            "GROUP_AT_MESSAGE_CREATE",
            {"id": "E2", "content": "<emoji:4>", "group_openid": GROUP,
             "author": {"id": SENDER}, "timestamp": "2026-10-17T19:30:00+08:00"}
        ).get("text"), "[表情:4]")


if __name__ == "__main__":
    unittest.main()
