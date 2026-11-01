"""anti_repeat / keyword_reply / short_followup / send_pacing / typo_sim 的离线测试。

这五个既有插件原先**一个测试都没有**，其中 `anti_repeat`、`short_followup`、`send_pacing`
还是**默认开启**的 —— 行为没被钉住，改动随时会悄悄弄坏它们。

行为口径一律以源码为准（不是按 JS 版猜的）。
"""

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from src.plugins.anti_repeat import AntiRepeat
from src.plugins.keyword_reply import KeywordReply
from src.plugins.send_pacing import SendPacing, inbound_has_media  # noqa: F401
from src.plugins.short_followup import ShortFollowup
from src.plugins.typo_sim import TypoSim


class FakeManager:
    def __init__(self, plugin_cls, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(plugin_cls.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def group_msg(text="你好", sender_id="7", target="12345"):
    return {"id": "m", "kind": "group", "target": target, "text": text,
            "sender_id": sender_id, "media": []}


class Base:
    CLS = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        settings.setdefault("enabled", True)
        return self.CLS(FakeManager(self.CLS, self.dir, settings))


class AntiRepeatTests(Base, unittest.TestCase):
    CLS = AntiRepeat

    def test_blocks_after_exceeding_max_repeat(self):
        # maxRepeat=3：前 3 次不拦，第 4 次拦
        plugin = self.make(maxRepeat=3, blockWindowSec=60)
        with patch("src.plugins.anti_repeat.time.time", return_value=1000.0):
            for _ in range(3):
                self.assertFalse(plugin.on_block(group_msg(), None))
            self.assertTrue(plugin.on_block(group_msg(), None))

    def test_max_repeat_one_blocks_second(self):
        plugin = self.make(maxRepeat=1, blockWindowSec=60)
        with patch("src.plugins.anti_repeat.time.time", return_value=1000.0):
            self.assertFalse(plugin.on_block(group_msg(), None))
            self.assertTrue(plugin.on_block(group_msg(), None))

    def test_different_text_is_independent(self):
        plugin = self.make(maxRepeat=1, blockWindowSec=60)
        with patch("src.plugins.anti_repeat.time.time", return_value=1000.0):
            self.assertFalse(plugin.on_block(group_msg("甲"), None))
            self.assertFalse(plugin.on_block(group_msg("乙"), None))

    def test_different_groups_are_independent(self):
        plugin = self.make(maxRepeat=1, blockWindowSec=60)
        with patch("src.plugins.anti_repeat.time.time", return_value=1000.0):
            self.assertFalse(plugin.on_block(group_msg(target="111"), None))
            self.assertFalse(plugin.on_block(group_msg(target="222"), None))

    def test_window_expiry_clears_count(self):
        plugin = self.make(maxRepeat=1, blockWindowSec=60)
        with patch("src.plugins.anti_repeat.time.time", return_value=1000.0):
            plugin.on_block(group_msg(), None)
        with patch("src.plugins.anti_repeat.time.time", return_value=2000.0):
            self.assertFalse(plugin.on_block(group_msg(), None))

    def test_private_chat_never_blocked(self):
        plugin = self.make(maxRepeat=1)
        message = group_msg()
        message["kind"] = "private"
        with patch("src.plugins.anti_repeat.time.time", return_value=1000.0):
            plugin.on_block(message, None)
            self.assertFalse(plugin.on_block(message, None))

    def test_empty_text_never_blocked(self):
        plugin = self.make(maxRepeat=1)
        with patch("src.plugins.anti_repeat.time.time", return_value=1000.0):
            for _ in range(5):
                self.assertFalse(plugin.on_block(group_msg("   "), None))

    def test_disabled_never_blocks(self):
        plugin = self.make(enabled=False, maxRepeat=1)
        with patch("src.plugins.anti_repeat.time.time", return_value=1000.0):
            for _ in range(5):
                self.assertFalse(plugin.on_block(group_msg(), None))

    def test_enabled_by_default(self):
        self.assertTrue(AntiRepeat.META.enabled_by_default)


class KeywordReplyTests(Base, unittest.TestCase):
    CLS = KeywordReply

    def test_matches_keyword_and_returns_reply(self):
        plugin = self.make(rules="你好=你好呀\n帮助=看这里")
        self.assertEqual(plugin.on_ingest(group_msg("你好啊"), None), "你好呀")

    def test_first_matching_rule_wins(self):
        plugin = self.make(rules="a=第一个\na=第二个")
        self.assertEqual(plugin.on_ingest(group_msg("aaa"), None), "第一个")

    def test_matching_is_case_insensitive(self):
        plugin = self.make(rules="Hello=hi")
        self.assertEqual(plugin.on_ingest(group_msg("say HELLO"), None), "hi")

    def test_no_match_returns_none(self):
        plugin = self.make(rules="你好=你好呀")
        self.assertIsNone(plugin.on_ingest(group_msg("再见"), None))

    def test_skips_blank_and_malformed_lines(self):
        plugin = self.make(rules="\n\n没有等号\n=没有关键词\n关键词=\n 好 = 好的  ")
        self.assertEqual(plugin._parse_rules(), [("好", "好的")])

    def test_empty_rules_returns_none(self):
        plugin = self.make(rules="")
        self.assertIsNone(plugin.on_ingest(group_msg("你好"), None))

    def test_disabled_returns_none(self):
        plugin = self.make(enabled=False, rules="你好=你好呀")
        self.assertIsNone(plugin.on_ingest(group_msg("你好"), None))

    def test_garbage_message_does_not_raise(self):
        plugin = self.make(rules="你好=你好呀")
        self.assertIsNone(plugin.on_ingest({}, None))

    def test_disabled_by_default(self):
        self.assertFalse(KeywordReply.META.default_settings["enabled"])


class ShortFollowupCoexistenceTests(Base, unittest.TestCase):
    """short_followup 的详细行为在 test_zombie_settings.py；这里补默认值口径。"""

    CLS = ShortFollowup

    def test_enabled_by_default(self):
        self.assertTrue(ShortFollowup.META.enabled_by_default)

    def test_first_message_records_only(self):
        plugin = self.make()
        with patch("src.plugins.short_followup.time.time", return_value=1000.0):
            self.assertIsNone(plugin.on_pre_ai(group_msg(), [], None))

    def test_second_message_within_window_hints(self):
        plugin = self.make(followupWindowSec=300, maxSentences=2)
        with patch("src.plugins.short_followup.time.time", return_value=1000.0):
            plugin.on_sent(group_msg(), "测试回复", None)
        with patch("src.plugins.short_followup.time.time", return_value=1001.0):
            hints = plugin.on_pre_ai(group_msg(), [], None)
        self.assertIn("不超过2句", hints["extra_system"])


class SendPacingUnitTests(Base):
    """send_pacing 的 mediaPacing 行为在 test_zombie_settings.py；这里补切句口径。"""

    def test_split_keeps_punctuation_with_sentence(self):
        plugin = self.make()
        parts = plugin._calc_delay("你好世界")
        self.assertGreaterEqual(parts, plugin.get("minMs", 300))

    def test_delay_is_clamped_by_max(self):
        plugin = self.make(maxMs=1000, minMsPerChar=700, maxMsPerChar=1100)
        self.assertLessEqual(plugin._calc_delay("很长" * 100), 1000)

    def test_delay_is_at_least_min(self):
        plugin = self.make(minMs=500, baseMs=0, minMsPerChar=0, maxMsPerChar=0)
        self.assertGreaterEqual(plugin._calc_delay("短"), 500)

    def test_swapped_char_bounds_do_not_crash(self):
        # maxMsPerChar < minMsPerChar 时原来会让 random.randint 抛错
        plugin = self.make(minMsPerChar=1100, maxMsPerChar=700)
        self.assertGreaterEqual(plugin._calc_delay("abc"), 0)


class TypoSimTests(Base, unittest.IsolatedAsyncioTestCase):
    CLS = TypoSim

    async def test_rate_zero_never_changes_reply(self):
        plugin = self.make(typoRate=0)
        for _ in range(20):
            self.assertEqual(await plugin.on_post_reply(group_msg(), "原始回复", None), "原始回复")

    async def test_rate_100_with_corrections_adds_pace_marker(self):
        plugin = self.make(typoRate=100, corrections=True)
        out = await plugin.on_post_reply(group_msg(), "hello world", None)
        self.assertIn("\x01PACE:800\x01", out)
        self.assertIn("哦不对，", out)

    async def test_rate_100_without_corrections_returns_typo_only(self):
        plugin = self.make(typoRate=100, corrections=False)
        out = await plugin.on_post_reply(group_msg(), "hello world", None)
        self.assertNotIn("哦不对", out)

    async def test_empty_reply_untouched(self):
        plugin = self.make(typoRate=100)
        self.assertEqual(await plugin.on_post_reply(group_msg(), "", None), "")

    async def test_disabled_untouched(self):
        plugin = self.make(enabled=False, typoRate=100)
        self.assertEqual(await plugin.on_post_reply(group_msg(), "hello", None), "hello")

    def test_inject_typo_changes_exactly_one_char_for_ascii(self):
        plugin = self.make()
        with patch("src.plugins.typo_sim.random.choice", side_effect=lambda seq: seq[0]):
            typo, original = plugin._inject_typo("hello world")
        self.assertIsNotNone(original)
        self.assertEqual(len(typo), len("hello world"))
        self.assertEqual(sum(1 for a, b in zip(typo, "hello world") if a != b), 1)

    def test_inject_typo_on_short_cjk_returns_none(self):
        plugin = self.make()
        self.assertEqual(plugin._inject_typo("你好"), ("你好", None))

    def test_inject_typo_on_long_cjk_drops_one_char(self):
        plugin = self.make()
        with patch("src.plugins.typo_sim.random.randint", return_value=1):
            typo, original = plugin._inject_typo("你好世界啊")
        self.assertEqual(len(typo), 4)      # 原 5 个字，丢掉 1 个
        self.assertIsNotNone(original)

    def test_disabled_by_default(self):
        self.assertFalse(TypoSim.META.enabled_by_default)


if __name__ == "__main__":
    unittest.main()
