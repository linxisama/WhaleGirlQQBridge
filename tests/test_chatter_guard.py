"""chatter_guard（水聊免回）的离线测试。

重点钉住"不用字数当判据"这条：JS 版注释明确拒绝按字数判水，因为会误伤
「在吗」「谢谢」「几点」。测试里专门有反例。
"""

import time
import unittest

from src.plugins.chatter_guard import (
    DEFAULT_FILLERS,
    ChatterGuard,
    is_filler,
    is_media_only,
    split_words,
    water_reason,
)


class MediaOnlyTests(unittest.TestCase):
    def test_media_placeholders_are_media_only(self):
        for text in ("[图片:abc]", "[表情:xyz]", "[卡片：分享]", "[图片:a][表情:b]", "  [图片:a]  "):
            with self.subTest(text=text):
                self.assertTrue(is_media_only(text))

    def test_empty_text_is_media_only(self):
        self.assertTrue(is_media_only(""))
        self.assertTrue(is_media_only("   "))

    def test_text_with_content_is_not_media_only(self):
        for text in ("你好", "[图片:a] 好看", "6"):
            with self.subTest(text=text):
                self.assertFalse(is_media_only(text))

    def test_punctuation_only_is_media_only(self):
        # 对齐 JS 的 \s\p{P}\p{S} 剥离：纯标点没有字，属于"没有一个字"这一档
        for text in ("？", "？？？", "。。。", "!?!"):
            with self.subTest(text=text):
                self.assertTrue(is_media_only(text))


class FillerTests(unittest.TestCase):
    def test_builtin_filler_words(self):
        for text in ("哈哈", "草", "6", "。。。", "笑死", "233", "hhh"):
            with self.subTest(text=text):
                self.assertTrue(is_filler(text))

    def test_doubled_filler_word(self):
        self.assertTrue(is_filler("哈哈哈哈"))

    def test_repeated_single_char(self):
        self.assertTrue(is_filler("啊啊啊"))

    def test_all_punctuation(self):
        for text in ("？？？", "！！！", "、、、"):
            with self.subTest(text=text):
                self.assertTrue(is_filler(text))

    def test_real_short_messages_are_not_filler(self):
        # 这就是"不能用字数当判据"的原因
        for text in ("在吗", "谢谢", "几点", "好的", "在不在", "多少钱"):
            with self.subTest(text=text):
                self.assertFalse(is_filler(text))

    def test_custom_filler_words_are_honoured(self):
        # 「咕咕咕」本来就命中了"同一个字重复 3 次以上"，所以这里用不重复的词来验证自定义表
        self.assertFalse(is_filler("咕咕"))
        self.assertTrue(is_filler("咕咕", ["咕咕"]))
        self.assertFalse(is_filler("好耶"))
        self.assertTrue(is_filler("好耶", ["好耶"]))

    def test_repeated_single_char_still_water_even_with_custom_list(self):
        # 与 JS 版一致：同字重复 3 次以上一律算水，与自定义表无关
        self.assertTrue(is_filler("咕咕咕", []))

    def test_case_sensitive_builtin_ok(self):
        self.assertTrue(is_filler("OK"))
        self.assertTrue(is_filler("ok"))

    def test_empty_is_filler(self):
        self.assertTrue(is_filler(""))
        self.assertTrue(is_filler("   "))


class WaterReasonTests(unittest.TestCase):
    def test_media_and_filler_reasons(self):
        self.assertEqual(water_reason("[图片:a]"), '只有表情/图片')
        self.assertEqual(water_reason("哈哈"), '纯水话')
        self.assertIsNone(water_reason("在吗"))

    def test_switches_can_disable_both_rules(self):
        self.assertIsNone(water_reason("[图片:a]", skip_media=False, skip_filler=True))
        self.assertIsNone(water_reason("哈哈", skip_media=True, skip_filler=False))

    def test_media_takes_priority_in_reason(self):
        self.assertEqual(water_reason("[图片:a]", skip_media=True, skip_filler=True), '只有表情/图片')


class SplitWordsTests(unittest.TestCase):
    def test_splits_on_mixed_separators(self):
        self.assertEqual(split_words("哈哈, 咕咕、嘿嘿;呵呵 哈哈"), ["哈哈", "咕咕", "嘿嘿", "呵呵"])

    def test_empty_input(self):
        self.assertEqual(split_words(""), [])
        self.assertEqual(split_words(None), [])


class FakeManager:
    def __init__(self, settings=None):
        self._settings = dict(ChatterGuard.META.default_settings)
        self._settings.update({"enabled": True})
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def msg(text="哈哈", sender_id="10001", kind="group", target="g1", at=False):
    return {"id": "m", "kind": kind, "target": target, "text": text,
            "sender": "某人", "sender_id": sender_id, "at": at}


class ChatterGuardTests(unittest.TestCase):
    def make(self, **settings):
        return ChatterGuard(FakeManager(settings))

    def test_disabled_plugin_never_blocks(self):
        plugin = ChatterGuard(FakeManager({"enabled": False}))
        self.assertFalse(plugin.on_block(msg(), None))

    def test_threshold_three(self):
        plugin = self.make(threshold=3, windowSec=180)
        self.assertFalse(plugin.on_block(msg(), None))   # 第 1 条
        self.assertFalse(plugin.on_block(msg(), None))   # 第 2 条
        self.assertTrue(plugin.on_block(msg(), None))    # 第 3 条起免回

    def test_normal_message_resets_streak(self):
        plugin = self.make(threshold=3)
        plugin.on_block(msg("哈哈"), None)
        plugin.on_block(msg("哈哈"), None)
        self.assertFalse(plugin.on_block(msg("今天有空吗"), None))  # 正常话清零
        self.assertFalse(plugin.on_block(msg("哈哈"), None))       # 重新从 1 计
        self.assertFalse(plugin.on_block(msg("哈哈"), None))
        self.assertTrue(plugin.on_block(msg("哈哈"), None))

    def test_window_expiry_resets_streak(self):
        plugin = self.make(threshold=3, windowSec=10)
        plugin.on_block(msg("哈哈"), None)
        plugin.on_block(msg("哈哈"), None)
        key = ("group:g1", "10001")
        plugin._streaks[key]["at"] -= 600      # 假装超过时间窗
        self.assertFalse(plugin.on_block(msg("哈哈"), None))  # 重新计数

    def test_streaks_are_per_sender(self):
        plugin = self.make(threshold=2)
        self.assertFalse(plugin.on_block(msg("哈哈", sender_id="1"), None))
        self.assertFalse(plugin.on_block(msg("哈哈", sender_id="2"), None))
        self.assertTrue(plugin.on_block(msg("哈哈", sender_id="1"), None))

    def test_streaks_are_per_room(self):
        plugin = self.make(threshold=2)
        self.assertFalse(plugin.on_block(msg("哈哈", target="g1"), None))
        self.assertFalse(plugin.on_block(msg("哈哈", target="g2"), None))
        self.assertTrue(plugin.on_block(msg("哈哈", target="g1"), None))

    def test_at_me_exemption_when_enabled(self):
        # threshold 最小是 2（与 JS 版一致：设 1 会太容易误伤）
        plugin = self.make(threshold=2, alwaysReplyWhenAtMe=True)
        self.assertFalse(plugin.on_block(msg("哈哈", at=True), None))   # @ 了它 → 放行
        self.assertFalse(plugin.on_block(msg("哈哈", at=False), None))  # 第 1 条
        self.assertTrue(plugin.on_block(msg("哈哈", at=False), None))   # 第 2 条起免回

    def test_keyword_exemption(self):
        plugin = self.make(threshold=2, exemptKeyword="鲸鱼娘")
        self.assertFalse(plugin.on_block(msg("哈哈哈 鲸鱼娘"), None))
        self.assertFalse(plugin.on_block(msg("哈哈哈 鲸鱼娘"), None))

    def test_media_only_counts_as_water(self):
        plugin = self.make(threshold=2)
        self.assertFalse(plugin.on_block(msg("[图片:a]"), None))
        self.assertTrue(plugin.on_block(msg("[表情:b]"), None))

    def test_media_rule_can_be_disabled(self):
        plugin = self.make(threshold=2, skipMedia=False)
        self.assertFalse(plugin.on_block(msg("[图片:a]"), None))
        self.assertFalse(plugin.on_block(msg("[图片:a]"), None))

    def test_filler_rule_can_be_disabled(self):
        plugin = self.make(threshold=2, skipFiller=False)
        self.assertFalse(plugin.on_block(msg("哈哈"), None))
        self.assertFalse(plugin.on_block(msg("哈哈"), None))

    def test_short_real_messages_are_never_blocked(self):
        # 反例保护：这是本插件与旧实现最关键的区别
        plugin = self.make(threshold=2)
        for text in ("在吗", "谢谢", "几点"):
            with self.subTest(text=text):
                self.assertFalse(plugin.on_block(msg(text), None))
                self.assertFalse(plugin.on_block(msg(text), None))

    def test_threshold_is_clamped_to_at_least_two(self):
        # 与 JS 版一致：threshold 最小 2，设 1 也不会生效
        plugin = self.make(threshold=1)
        self.assertFalse(plugin.on_block(msg("哈哈"), None))
        self.assertTrue(plugin.on_block(msg("哈哈"), None))

    def test_missing_sender_id_is_not_blocked(self):
        plugin = self.make(threshold=2)
        for _ in range(3):
            self.assertFalse(plugin.on_block(msg("哈哈", sender_id=""), None))

    def test_notice_events_are_ignored(self):
        plugin = self.make(threshold=2)
        self.assertFalse(plugin.on_block(msg("", sender_id="1", at=False) | {"notice": "poke"}, None))

    def test_streak_table_is_bounded(self):
        plugin = self.make(threshold=99)
        for index in range(700):
            plugin.on_block(msg("哈哈", sender_id=str(index)), None)
        self.assertLessEqual(len(plugin._streaks), 600)

    def test_exceptions_do_not_escape(self):
        plugin = self.make()
        self.assertFalse(plugin.on_block(None, None))
        self.assertFalse(plugin.on_block({}, None))

    def test_default_filler_list_has_no_reply_needed_words(self):
        # 「在吗」「谢谢」「好的」这类需要回应的词不能在水词表里
        for word in ("在吗", "谢谢", "好的", "几点"):
            self.assertNotIn(word, DEFAULT_FILLERS)


if __name__ == "__main__":
    unittest.main()
