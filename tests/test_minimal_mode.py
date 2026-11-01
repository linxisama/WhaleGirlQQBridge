"""极简模式：按档位裁历史、封顶输出、加短话纪律（移植自社区插件 minimal-mode）。

来源：kondius.cn/qq-agent/plugin-market/ 的 ``minimal-mode`` v1.3.1（作者 雨夜流汐，MIT）。
那边砍"技能说明段 + 工具定义 + 历史条数"；本运行时没有工具定义，所以砍的是
**历史条数 + 输出上限 + 追加纪律文本**，这一点在插件描述与文档里都写明了。
"""
import asyncio
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402
from src.plugins.minimal_mode import (  # noqa: E402
    NOTES, TIER_NAMES, history_limit, match_command,
)

BUILTIN = "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n"


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    (root / "config.qq.yaml").write_text(yaml.safe_dump({
        "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
    }), encoding="utf-8")
    return root


class HistoryLimitTests(unittest.TestCase):
    def test_keeps_recent_and_always_the_last(self):
        messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        kept, dropped = history_limit(messages, 3)
        self.assertEqual([m["content"] for m in kept], ["m7", "m8", "m9"])
        self.assertEqual(dropped, 7)
        self.assertEqual(messages[-1], kept[-1], "当前这一条必须留下")

    def test_no_trim_when_short_enough_or_off(self):
        messages = [{"role": "user", "content": "hi"}]
        self.assertEqual(history_limit(messages, 5), (messages, 0))
        self.assertEqual(history_limit(messages, 0), (messages, 0))
        self.assertEqual(history_limit("not a list", 3), ("not a list", 0))


class CommandTests(unittest.TestCase):
    def test_recognises_the_three_tiers(self):
        self.assertEqual(match_command("/极简 开"), "on")
        self.assertEqual(match_command("开一下极简模式"), "on")
        self.assertEqual(match_command("/极简 关"), "off")
        self.assertEqual(match_command("把极简模式关了吧"), "off")
        self.assertEqual(match_command("/极简 极限"), "ultra")
        self.assertEqual(match_command("/极简 敢死"), "kamikaze")
        self.assertEqual(match_command("/极简 敢死 关"), "kamikaze-off")
        self.assertEqual(match_command("/极简"), "status")
        self.assertEqual(match_command("极简模式怎么开"), "status")

    def test_ignores_normal_chat(self):
        for text in ("今天天气不错", "极简主义装修真好看", "我在说极简主义的书", ""):
            self.assertIsNone(match_command(text), text)

    def test_ignores_long_messages(self):
        self.assertIsNone(match_command("极简" + "很长的闲聊" * 20))


class MinimalModePluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["minimal_mode"]
        self.bot.plugins.update_settings("minimal_mode", {"enabled": True})
        self.bot.plugins.update_settings("pc_control", {"enabled": True, "ownerIds": "10001"})

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def message(self, text, sender="10001"):
        return {"id": "m1", "kind": "private", "target": "admin", "sender_id": sender,
                "sender_name": "主人", "text": text}

    def test_tier_switch_via_command_and_persistence(self):
        reply = self.plugin.on_ingest(self.message("/极简 极限"), self.bot)
        self.assertIn("极限", reply)
        self.assertEqual(self.plugin.tier(), "ultra")
        again = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                          db_path=self.root / "second.sqlite")
        try:
            self.assertEqual(again.plugins._plugins["minimal_mode"].tier(), "ultra",
                             "档位要跨重启保留")
        finally:
            asyncio.run(again.close())

    def test_off_restores_everything(self):
        self.plugin.set_tier("kamikaze")
        self.plugin.on_ingest(self.message("/极简 关"), self.bot)
        self.assertEqual(self.plugin.limits(),
                         {"tier": "off", "messages": 0, "max_tokens": 0, "note": ""})

    def test_non_owner_cannot_switch(self):
        reply = self.plugin.on_ingest(self.message("/极简 敢死", sender="999"), self.bot)
        self.assertIn("只有绑定管理员", reply)
        self.assertEqual(self.plugin.tier(), "off")

    def test_group_message_is_ignored(self):
        message = self.message("/极简 开")
        message["kind"] = "group"
        self.assertIsNone(self.plugin.on_ingest(message, self.bot))

    def test_hints_trim_history_and_cap_output(self):
        self.plugin.set_tier("ultra")
        conversation = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        hints = self.plugin.on_pre_ai(self.message("随便聊聊"), conversation, self.bot)
        self.assertEqual(len(conversation), self.plugin.get("ultraMsgs"))
        self.assertEqual(conversation[-1]["content"], "m19")
        self.assertEqual(hints["max_tokens"], self.plugin.get("ultraMaxTokens"))
        self.assertIn("极限档", hints["extra_system"])
        self.assertGreaterEqual(self.plugin.stats().get("messages", 0), 14)

    def test_disabled_plugin_does_nothing(self):
        self.bot.plugins.update_settings("minimal_mode", {"enabled": False})
        conversation = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        self.assertIsNone(self.plugin.on_pre_ai(self.message("hi"), conversation, self.bot))
        self.assertEqual(len(conversation), 20)

    def test_status_lists_tier_and_savings(self):
        self.plugin.set_tier("minimal")
        text = self.plugin.status_text(self.bot)
        self.assertIn("极简", text)
        self.assertIn("切换", text)
        for name in TIER_NAMES.values():
            self.assertTrue(name)

    def test_reply_path_applies_the_cap_and_the_note(self):
        """真回复路径：纪律进系统提示词、输出上限真的传给了模型。"""
        from tests.test_chat_features import message

        self.plugin.set_tier("kamikaze")
        self.bot.running = True
        self.bot.official.authenticated = True
        self.bot.official.send = mock.AsyncMock()
        self.bot.ai.chat = mock.AsyncMock(return_value="嗯。")

        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.bot.ingest(message(mid="mm1", text="在吗"), "official")
            for _ in range(100):
                if self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='sent'").fetchone()[0]:
                    break
                await asyncio.sleep(0.05)

        asyncio.run(run())
        kwargs = self.bot.ai.chat.call_args.kwargs
        self.assertIn("敢死档", kwargs.get("system_prompt", ""))
        self.assertEqual(kwargs.get("max_tokens"), self.plugin.get("kamikazeMaxTokens"))

    def test_note_text_keeps_the_three_styles_distinct(self):
        self.assertIn("最省", NOTES["kamikaze"])
        self.assertNotEqual(NOTES["minimal"], NOTES["ultra"])


if __name__ == "__main__":
    unittest.main()
