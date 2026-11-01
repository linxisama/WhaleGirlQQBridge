"""回复时机与引用：时序提示 + 冷场不硬接（移植自社区插件 reply-freshness）。

来源：kondius.cn/qq-agent/plugin-market/ 的 ``reply-freshness`` v1.0.0。
照搬：只在当前 user 消息末尾追加（不动系统提示词）；晚回/冷场两类建议；
冷场判定 = 上一条别人的消息够久 + （最后是她在说 或 没人点名）。
差异：原版按 replyToMessageId 真的引用（我们交给「引用回复」插件），这里只给措辞建议；
另外提供可选硬刹车 coldHold（默认关）。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import time
import unittest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402
from src.plugins.reply_freshness import (  # noqa: E402
    TIME_BLOCK, ago_text, build_block, is_mention, timing_of, who_of,
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


def payload(mid, sender="20002", name="小鱼", text="在吗", at=False):
    return json.dumps({"id": mid, "sender_id": sender, "sender_name": name, "text": text, "at": at},
                      ensure_ascii=False)


class HelperTests(unittest.TestCase):
    def test_ago_text(self):
        self.assertEqual(ago_text(8), "8 秒")
        self.assertEqual(ago_text(60), "1 分")
        self.assertEqual(ago_text(192), "3 分 12 秒")
        self.assertEqual(ago_text(3900), "1 小时 5 分")
        self.assertEqual(ago_text("x"), "0 秒")

    def test_who_of(self):
        self.assertEqual(who_of({"sender_id": "123", "sender_name": "小鱼"}), "小鱼(QQ:123)")
        self.assertEqual(who_of({"sender_id": "123", "sender_name": "123"}), "123")
        self.assertEqual(who_of({}), "某人")

    def test_is_mention_checks_at_and_aliases(self):
        self.assertTrue(is_mention({"at": True, "text": "hi"}, []))
        self.assertTrue(is_mention({"text": "鲸鱼娘在吗"}, ["鲸鱼娘"]))
        self.assertFalse(is_mention({"text": "大家好"}, ["鲸鱼娘"]))


class TimingTests(unittest.TestCase):
    def test_is_latest_and_newer_count(self):
        now = time.time()
        rows = [(payload("m3"), now - 5), (payload("m2", "30003", "阿豆"), now - 30),
                (payload("m1"), now - 60)]
        latest = timing_of(rows, mid="m3", now=now)
        self.assertTrue(latest["is_latest"])
        self.assertEqual(latest["newer_count"], 0)
        self.assertEqual(latest["since_current"], 5)
        older = timing_of(rows, mid="m1", now=now)
        self.assertFalse(older["is_latest"])
        self.assertEqual(older["newer_count"], 2)
        self.assertEqual(older["since_current"], 60)
        # 与原版一致："上一条别人的消息"= 最新的一条非机器人消息（可以是同一个人，也可以是当前这条）
        self.assertEqual(older["since_last_other"], 5)

    def test_empty_rows_and_bad_payload(self):
        self.assertEqual(timing_of([], mid="m1"), {})
        self.assertEqual(timing_of([("not json", 1)], mid="m1"), {})

    def test_build_block_says_what_matters(self):
        now = time.time()
        rows = [(payload("m2", "30003", "阿豆"), now - 200), (payload("m1"), now - 260)]
        timing = timing_of(rows, mid="m1", now=now)
        cold = build_block(timing, late_seconds=45, quiet_seconds=90, bot_spoke_last=False,
                           mentions_bot=False)
        self.assertIn(TIME_BLOCK, cold)
        self.assertIn("又来了 1 条", cold)
        self.assertIn("没人等你", cold)
        self.assertIn("别为了清空未读", cold)

    def test_build_block_for_plain_late_reply(self):
        now = time.time()
        rows = [(payload("m1"), now - 120)]
        timing = timing_of(rows, mid="m1", now=now)
        block = build_block(timing, late_seconds=45, quiet_seconds=90, bot_spoke_last=True,
                            mentions_bot=True)
        self.assertIn("它就是最新的", block)
        self.assertIn("刚看到", block)
        self.assertNotIn("没人等你", block, "有人点名时不该说没人等")

    def test_private_chat_never_gets_cold_advice(self):
        now = time.time()
        rows = [(payload("m1"), now - 600)]
        timing = timing_of(rows, mid="m1", now=now)
        block = build_block(timing, late_seconds=45, quiet_seconds=90, bot_spoke_last=True,
                            mentions_bot=False, is_private=True)
        self.assertNotIn("没人等你", block)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["reply_freshness"]
        self.bot.plugins.update_settings("reply_freshness", {"enabled": True})
        self.bot.plugins.update_settings("chat_controls", {"enabled": True, "aliases": "鲸鱼娘"})

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def insert(self, mid, created, sender="20002", name="小鱼", text="在吗", at=False):
        self.bot.db.execute(
            "INSERT INTO inbox(key,mode,account,room,payload,state,created)"
            " VALUES(?,?,?,?,?,'sent',?)",
            (mid, self.bot.mode, self.bot.account(), "group:123",
             payload(mid, sender, name, text, at), created))
        self.bot.db.commit()

    def message(self, mid="m2", text="在吗", at=False):
        return {"id": mid, "kind": "group", "target": "123", "sender_id": "20002",
                "sender_name": "小鱼", "text": text, "at": at}

    def test_block_is_appended_to_current_user_message_only(self):
        now = time.time()
        self.insert("m1", now - 300, "30003", "阿豆")
        self.insert("m2", now - 200)
        conversation = [{"role": "user", "content": "历史一"}, {"role": "assistant", "content": "历史二"},
                        {"role": "user", "content": "当前这句"}]
        self.plugin.on_pre_ai(self.message(), conversation, self.bot)
        self.assertEqual(conversation[0]["content"], "历史一", "历史消息一个字都不动")
        self.assertTrue(conversation[-1]["content"].startswith("当前这句"))
        self.assertIn(TIME_BLOCK, conversation[-1]["content"])

    def test_bot_spoke_last_makes_it_cold(self):
        now = time.time()
        self.insert("m1", now - 400, "30003", "阿豆")
        self.insert("m2", now - 300)
        self.plugin.note_reply("group:123")
        timing, cold = self.plugin.analyse(self.bot, self.message())
        self.assertTrue(timing["bot_spoke_last"])
        self.assertTrue(cold)

    def test_mention_prevents_cold(self):
        now = time.time()
        self.insert("m1", now - 400, "30003", "阿豆")
        self.insert("m2", now - 300, text="鲸鱼娘在吗")
        timing, cold = self.plugin.analyse(self.bot, self.message(text="鲸鱼娘在吗"))
        self.assertTrue(timing["mentions_bot"])
        self.assertFalse(cold, "有人点名就不算冷场")

    def test_cold_hold_is_off_by_default(self):
        now = time.time()
        self.insert("m1", now - 900, "30003", "阿豆")
        self.insert("m2", now - 800)
        message = self.message()
        self.plugin.on_ingest(message, self.bot)
        self.assertFalse(self.plugin.on_block(message, self.bot), "默认不做硬刹车")

    def test_cold_hold_blocks_when_enabled(self):
        now = time.time()
        self.bot.plugins.update_settings("reply_freshness", {"coldHold": True})
        self.insert("m1", now - 900, "30003", "阿豆")
        self.insert("m2", now - 800)
        message = self.message()
        self.plugin.on_ingest(message, self.bot)
        self.assertTrue(self.plugin.on_block(message, self.bot))
        self.assertTrue(any("冷场刹车" in str(v.get("text", "")) for v in getattr(self.bot, "events", [])))

    def test_records_its_own_replies_for_the_next_turn(self):
        self.plugin.on_sent(self.message(), "好", self.bot)
        self.assertGreater(self.plugin.last_reply_at(self.bot, "group:123"), 0)

    def test_disabled_plugin_touches_nothing(self):
        now = time.time()
        self.insert("m1", now - 900, "30003", "阿豆")
        self.insert("m2", now - 800)
        self.bot.plugins.update_settings("reply_freshness", {"enabled": False})
        conversation = [{"role": "user", "content": "当前"}]
        self.assertIsNone(self.plugin.on_pre_ai(self.message(), conversation, self.bot))
        self.assertEqual(conversation[-1]["content"], "当前")
        self.assertFalse(self.plugin.on_block(self.message(), self.bot))


if __name__ == "__main__":
    unittest.main()
