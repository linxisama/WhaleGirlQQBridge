"""戳一戳的回复：不能拿上一条消息当话题，也不能长篇大论（真机 2026-10-16）。

用户截图：群里有人戳了机器人，机器人却回了两段针对上一条 @ 消息的挑衅
（"少搁这儿吓唬人，谁怕你了？…"），跟"被戳了一下"完全无关。

根因两条：
1. 戳一戳事件的正文是**空**的，`turn_text()` 只产出"发送者资料"这一行 →
   模型看到一条空的 user 轮次，只能顺着历史去接上一条话题；
2. `_work` 仍然带着整段对话历史（build_history），把上一条 @ 的内容喂给了模型。

修法：`turn_text()` 给戳一戳一句明确的"发生了什么"；`_work` 对戳一戳**清空历史**，
并把输出上限压到很短（60 tokens），使回应只针对"被戳"这件事本身。
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

USER_ROOT = pathlib.Path.home() / "AppData" / "Local" / "WhaleGirlQQ"
GROUP = "1CBE87EF553193840AFFDCC788043580"
POKER = "3B511E5EC940DAA7EF2CD5BCCD42EAEA"


class PokePromptTests(unittest.TestCase):
    def test_poke_turn_text_says_what_happened(self):
        poke = {"id": "p1", "kind": "group", "target": GROUP, "text": "",
                "notice": "poke", "notice_data": {"poked": "BOT", "poker": POKER},
                "sender_id": POKER, "sender_name": "西莲寺紫"}
        text = turn_text(poke)
        self.assertIn("戳了戳你", text)
        self.assertIn("没有文字或图片内容", text, "要明确告诉模型这是个无内容动作")
        self.assertIn("西莲寺紫", text)

    def test_private_poke_has_no_fake_group_identity(self):
        poke = {"id": "p2", "kind": "private", "target": POKER, "text": "",
                "notice": "poke", "notice_data": {"poked": "BOT", "poker": POKER},
                "sender_id": POKER, "sender_name": "西莲寺紫"}
        text = turn_text(poke)
        self.assertNotIn("发送者资料", text)
        self.assertIn("戳了戳你", text)

    def test_normal_message_is_unchanged(self):
        message = {"id": "n1", "kind": "group", "target": GROUP, "text": "在吗",
                   "sender_id": POKER, "sender_name": "麦麦"}
        text = turn_text(message)
        self.assertIn("在吗", text)
        self.assertIn("发送者资料", text)


class PokePipelineTests(unittest.TestCase):
    """戳一戳走到模型时：历史必须为空、输出上限必须很短。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
        shutil.copytree(USER_ROOT / "styles", root / "styles", dirs_exist_ok=True)
        (root / "config.qq.yaml").write_text(
            "ai:\n  api_url: https://example.test/v1/chat/completions\n  api_key: fake\n"
            "  model: test\nqq_only:\n  mode: onebot\n  onebot_token: t\n", encoding="utf-8")
        self.bot = QQRuntime(root=root, config_path=root / "config.qq.yaml",
                             db_path=root / "t.sqlite3")
        self.bot.running = True
        self.prompts = []
        self.sent = []

        async def fake_chat(messages, **kwargs):
            self.prompts.append({"messages": messages, "kwargs": kwargs})
            return "别戳啦"

        async def fake_send(message, text, meta=None):
            self.sent.append(text)
            return {"id": "1"}

        # 固定两个戳相关插件的行为，避免随机概率让测试抖动：
        # - poke_counter：只统计、不抢答（replyProbability=0）
        # - probability_reply：100% 交给 AI（不反戳、不忽略）
        for plugin_id, values in {
            "poke_counter": {"enabled": True, "replyProbability": 0.0},
            "probability_reply": {"enabled": True, "replyPercent": 100,
                                  "pokeBackPercent": 0, "ignorePokes": False},
        }.items():
            try:
                self.bot.plugins.update_settings(plugin_id, values)
            except Exception:
                pass
        self.bot.ai.chat = fake_chat
        # 生产环境里 self_id 由 OneBot 握手（X-Self-ID / get_login_info）写入；
        # 测试必须补上，否则 ingest 会判定这条戳不是戳机器人本人而忽略。
        self.bot.onebot.self_id = 'BOT'
        self.bot.onebot.online = True
        self.bot.onebot.send = fake_send
        self.bot.onebot.send_group_msg = fake_send
        self.bot.onebot.send_private_msg = fake_send

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def message(self, **extra):
        base = {"id": "m1", "kind": "group", "target": GROUP, "text": "",
                "notice": "poke", "notice_data": {"poked": "BOT", "poker": POKER},
                "sender_id": POKER, "sender_name": "西莲寺紫", "media": []}
        base.update(extra)
        return base

    def run_once(self, message, wait=2.0):
        async def main():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.bot.ingest(message, "onebot")
            deadline = time.time() + wait
            while time.time() < deadline:
                await asyncio.sleep(0.05)
                pending = self.bot.db.execute(
                    "SELECT count(*) FROM inbox WHERE state IN ('pending','sending')").fetchone()[0]
                if not pending:
                    break
            await asyncio.sleep(0.2)
        asyncio.run(main())

    def test_poke_prompt_has_no_previous_turn(self):
        # 先放一条"上一条消息"的历史，确认戳一戳不会把它喂给模型
        self.bot.db.execute(
            "INSERT INTO inbox(key,mode,account,room,payload,state,reply,created)"
            " VALUES(?,?,?,?,?,'sent',?,?)",
            ("old", "onebot", str(POKER), f"group:{GROUP}",
             json.dumps({"id": "old", "kind": "group", "target": GROUP,
                         "text": "少搁这儿吓唬人，谁怕你了", "sender_id": POKER,
                         "sender_name": "西莲寺紫", "_persona_key": None}),
             "本鲸不怕", time.time() - 30))
        self.bot.db.commit()
        self.run_once(self.message())
        self.assertTrue(self.prompts, "戳一戳应当触发一次模型调用（probability_reply 决定要不要回）")
        prompt = json.dumps(self.prompts[0]["messages"], ensure_ascii=False)
        self.assertNotIn("少搁这儿吓唬人", prompt,
                         "戳一戳不该把上一条消息带进上下文（真机就是因此答非所问）")
        self.assertIn("戳了戳你", prompt, "当前轮要说明这是戳一戳")
        self.assertLessEqual(int(self.prompts[0]["kwargs"].get("max_tokens") or 500), 60,
                             "戳一戳的回应要短")

    def test_poke_reply_is_sent_once(self):
        self.run_once(self.message())
        self.assertEqual(len(self.sent), 1, f"一次戳应当只发一条：{self.sent}")
        self.assertIn("别戳", self.sent[0])


if __name__ == "__main__":
    unittest.main()
