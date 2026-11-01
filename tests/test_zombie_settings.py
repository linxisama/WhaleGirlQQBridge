"""既有插件的"僵尸设置"修复测试（Round 10 校准）。

背景：这三个设置项以前只写在 `config_schema` + `default_settings` 里，
**代码从不读取** —— 用户拨了没有任何效果，而且不报错。审计方式：
每个键在文件里只出现 2 次（schema 一次、默认值一次）就说明没人用。

| 设置 | 原来的行为 | 现在的行为 |
|---|---|---|
| `send_pacing.mediaPacing` | 从不读取 | 关掉后，带媒体那一轮不再拆句 |
| `proactive_chat.dailyTokenBudget` | 从不读取 | 真的按天累计 token 估算并拦截 |
| `short_followup.maxTokens` | 从不读取，且提示词写死"不超过2句" | 改名为 `maxSentences`，真的写进提示词 |
"""

import pathlib
import tempfile
import unittest

from src.plugins.proactive_chat import ProactiveChat
from src.plugins.send_pacing import SendPacing, has_media, inbound_has_media
from src.plugins.short_followup import ShortFollowup


class FakeManager:
    def __init__(self, plugin_cls, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(plugin_cls.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class FakeRuntime:
    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(text)


class SendPacingMediaTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        return SendPacing(FakeManager(SendPacing, tmp.name, settings))

    def test_inbound_has_media(self):
        self.assertTrue(inbound_has_media({"media": [{"type": "image"}]}))
        self.assertTrue(inbound_has_media({"text": "看这个[图片]", "media": []}))
        self.assertTrue(inbound_has_media({"text": "[表情:微笑]", "media": []}))
        self.assertFalse(inbound_has_media({"text": "你好", "media": []}))
        self.assertFalse(inbound_has_media(None))

    def test_has_media_on_meta(self):
        self.assertTrue(has_media({"media": [{"type": "image"}]}))
        self.assertFalse(has_media({"media": []}))
        self.assertFalse(has_media(None))

    async def test_media_pacing_on_splits_sentences(self):
        plugin = self.make(mediaPacing=True)
        reply = await plugin.on_post_reply({"media": [{"type": "image"}]},
                                           "第一句。第二句。", FakeRuntime())
        self.assertIn("\x01PACE:", reply)

    async def test_media_pacing_off_keeps_media_reply_whole(self):
        plugin = self.make(mediaPacing=False)
        reply = "第一句。第二句。"
        out = await plugin.on_post_reply({"media": [{"type": "image"}]}, reply, FakeRuntime())
        self.assertEqual(out, reply)              # 带媒体 → 不拆

    async def test_media_pacing_off_still_splits_plain_text(self):
        plugin = self.make(mediaPacing=False)
        out = await plugin.on_post_reply({"text": "你好", "media": []},
                                         "第一句。第二句。", FakeRuntime())
        self.assertIn("\x01PACE:", out)           # 没媒体 → 照常拆

    async def test_single_sentence_is_not_split(self):
        plugin = self.make(mediaPacing=True)
        self.assertEqual(await plugin.on_post_reply({}, "只有一句。", FakeRuntime()), "只有一句。")

    async def test_disabled_returns_reply_unchanged(self):
        plugin = self.make(enabled=False)
        self.assertEqual(await plugin.on_post_reply({}, "甲。乙。", FakeRuntime()), "甲。乙。")

    def test_media_pacing_is_documented(self):
        self.assertIn("媒体", SendPacing.META.description)


class ShortFollowupTests(unittest.TestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        return ShortFollowup(FakeManager(ShortFollowup, tmp.name, settings))

    def test_sentence_cap_used_in_prompt(self):
        plugin = self.make(maxSentences=3)
        message = {"target": "1", "sender_id": "7"}
        self.assertIsNone(plugin.on_pre_ai(message, [], None))          # 第一次只记时间
        plugin.on_sent(message,"成功回复",None)
        hints = plugin.on_pre_ai(message, [], None)                     # 第二次在窗口内
        self.assertIn("不超过3句", hints["extra_system"])

    def test_default_cap_is_two(self):
        plugin = self.make()
        message = {"target": "1", "sender_id": "7"}
        plugin.on_pre_ai(message, [], None)
        plugin.on_sent(message,"成功回复",None)
        hints = plugin.on_pre_ai(message, [], None)
        self.assertIn("不超过2句", hints["extra_system"])

    def test_legacy_max_tokens_key_still_read(self):
        # 旧配置里存的是 maxTokens=4，也要能读出来当句数上限用。
        # 注意要模拟"配置里没有 maxSentences"这种真实旧状态 —— FakeManager 会把
        # 默认值合并进来，那样新键永远存在，回退分支就测不到了。
        plugin = self.make()
        plugin._manager._settings.pop("maxSentences", None)
        plugin._manager._settings["maxTokens"] = 4
        self.assertEqual(plugin.sentence_cap(), 4)

    def test_cap_is_clamped(self):
        self.assertEqual(self.make(maxSentences=99).sentence_cap(), 10)
        self.assertEqual(self.make(maxSentences=0).sentence_cap(), 1)
        self.assertEqual(self.make(maxSentences="x").sentence_cap(), 2)

    def test_different_senders_are_independent(self):
        plugin = self.make()
        plugin.on_pre_ai({"target": "1", "sender_id": "7"}, [], None)
        self.assertIsNone(plugin.on_pre_ai({"target": "1", "sender_id": "8"}, [], None))

    def test_outside_window_returns_none(self):
        plugin = self.make(followupWindowSec=30)
        message = {"target": "1", "sender_id": "7"}
        plugin.on_pre_ai(message, [], None)
        plugin.on_sent(message,"成功回复",None)
        key = plugin.key(message,None)
        plugin._last_replied[key] -= 3600        # 假装一小时前
        self.assertIsNone(plugin.on_pre_ai(message, [], None))

    def test_renamed_setting_is_documented(self):
        self.assertIn("maxSentences", ShortFollowup.META.schema_dict())
        self.assertNotIn("maxTokens", ShortFollowup.META.schema_dict())


class ProactiveChatBudgetTests(unittest.TestCase):
    def setUp(self):
        # 持久化测试需要同一个状态目录，所以目录在 setUp 里建、类内复用
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        settings.setdefault("enabled", True)
        return ProactiveChat(FakeManager(ProactiveChat, self.dir, settings))

    def test_budget_starts_empty(self):
        plugin = self.make()
        self.assertEqual(plugin.spent_today(), 0)
        self.assertTrue(plugin._budget_left())

    def test_budget_zero_means_unlimited(self):
        plugin = self.make(dailyTokenBudget=0)
        plugin.record_usage(10000, "很长的一段回复" * 100)
        self.assertTrue(plugin._budget_left())

    def test_usage_accumulates_and_blocks(self):
        plugin = self.make(dailyTokenBudget=50)
        self.assertTrue(plugin._budget_left())
        plugin.record_usage(200, "回复" * 100)      # 估算远超 50
        self.assertGreater(plugin.spent_today(), 50)
        self.assertFalse(plugin._budget_left())

    def test_usage_persists_across_instances(self):
        plugin = self.make(dailyTokenBudget=10000)
        plugin.record_usage(100, "回复")
        spent = plugin.spent_today()
        self.assertGreater(spent, 0)
        again = self.make(dailyTokenBudget=10000)
        self.assertEqual(again.spent_today(), spent)

    def test_state_keeps_only_recent_days(self):
        plugin = self.make(dailyTokenBudget=0)
        state = plugin._budget_state()
        for day in range(1, 11):
            state[f"2026-01-{day:02d}"] = day
        plugin.record_usage(10, "x")
        self.assertLessEqual(len(plugin._budget_state()), 8)

    def test_tick_returns_early_when_budget_exhausted(self):
        plugin = self.make(dailyTokenBudget=1, groups="12345", activeHours="00:00-23:59")
        plugin.record_usage(1000, "很长" * 200)
        runtime = FakeRuntime()
        runtime.mode = "onebot"
        import asyncio
        asyncio.run(plugin.tick(runtime))
        self.assertTrue(any("预算" in note for note in runtime.notes))

    def test_budget_setting_is_documented(self):
        self.assertIn("dailyTokenBudget", ProactiveChat.META.schema_dict())
        self.assertIn("token", ProactiveChat.META.schema_dict()["dailyTokenBudget"]["label"].lower())


if __name__ == "__main__":
    unittest.main()
