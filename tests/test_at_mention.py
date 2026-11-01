"""at_mention（用名字 @ 群友）与其解析器的离线测试。

解析规则移植自 JS 版 at-resolver.js，核心原则是"宁可认不出，也绝不猜错"：
@ 错人是社交事故，比没 @ 上严重。测试按这条原则逐条钉住行为。
"""

import unittest

from src.member_directory import MemberDirectory, resolve_mentions
from src.plugins.at_mention import AtMention


def members(*pairs):
    return [{"userId": uid, "name": name} for uid, name in pairs]


class ResolveMentionsTests(unittest.TestCase):
    def test_no_at_sign_returns_text_unchanged(self):
        result = resolve_mentions("你好呀", members(("1", "小明")))
        self.assertEqual(result["text"], "你好呀")
        self.assertEqual(result["atUserIds"], [])

    def test_exact_match_is_claimed(self):
        result = resolve_mentions("@小明 你说得对", members(("10001", "小明")))
        self.assertEqual(result["atUserIds"], ["10001"])
        self.assertEqual(result["text"], "你说得对")

    def test_mention_inside_sentence_keeps_rest(self):
        result = resolve_mentions("我觉得 @小明 说得对", members(("10001", "小明")))
        self.assertEqual(result["atUserIds"], ["10001"])
        self.assertEqual(result["text"], "我觉得 说得对")

    def test_multiple_mentions(self):
        result = resolve_mentions("@小明 @小红 都来", members(("1", "小明"), ("2", "小红")))
        self.assertEqual(result["atUserIds"], ["1", "2"])

    def test_longest_name_wins(self):
        # 名单里同时有「小明」和「小明同学」时，写 @小明同学 要认长的那个，
        # 否则会留下孤零零的"同学"
        pool = members(("1", "小明"), ("2", "小明同学"))
        self.assertEqual(resolve_mentions("@小明同学 你好", pool)["atUserIds"], ["2"])
        self.assertEqual(resolve_mentions("@小明 你好", pool)["atUserIds"], ["1"])

    def test_partial_match_resolves_longer_name(self):
        # 少打一个字：@小明同 → 认成「小明同学」
        result = resolve_mentions("@小明同 在吗", members(("2", "小明同学")))
        self.assertEqual(result["atUserIds"], ["2"])

    def test_partial_match_can_be_disabled(self):
        result = resolve_mentions("@小明同 在吗", members(("2", "小明同学")), allow_partial_match=False)
        self.assertEqual(result["atUserIds"], [])

    def test_single_character_name_is_never_claimed(self):
        # 单字昵称匹配面太广，极易误伤
        self.assertEqual(resolve_mentions("@甲 你好", members(("1", "甲")))["atUserIds"], [])

    def test_repeated_mention_is_consumed_but_not_duplicated(self):
        pool = members(("1", "小明"), ("2", "小明同学"))
        result = resolve_mentions("@小明 @小明 早", pool)
        self.assertEqual(result["atUserIds"], ["1"])          # 只 @ 一次
        self.assertNotIn("@", result["text"])                  # 第二段也被吃掉

    def test_repeated_mention_does_not_leak_to_other_member(self):
        # 反例保护：第二次 @小明 不该走前缀匹配去认「小明同学」
        pool = members(("1", "小明"), ("2", "小明同学"))
        self.assertEqual(resolve_mentions("@小明 @小明 早", pool)["atUserIds"], ["1"])

    def test_email_like_at_is_ignored(self):
        result = resolve_mentions("发到 foo@小明.com", members(("1", "小明")))
        self.assertEqual(result["atUserIds"], [])

    def test_double_at_is_claimed(self):
        result = resolve_mentions("@@小明 你好", members(("1", "小明")))
        self.assertEqual(result["atUserIds"], ["1"])
        self.assertNotIn("@", result["text"])

    def test_max_mentions_limits_claims(self):
        pool = members(("1", "小明"), ("2", "小红"), ("3", "小刚"))
        result = resolve_mentions("@小明 @小红 @小刚", pool, max_mentions=2)
        self.assertEqual(result["atUserIds"], ["1", "2"])

    def test_unknown_name_is_left_alone(self):
        result = resolve_mentions("@查无此人 你好", members(("1", "小明")))
        self.assertEqual(result["atUserIds"], [])
        self.assertEqual(result["text"], "@查无此人 你好")

    def test_members_without_numeric_id_are_ignored(self):
        result = resolve_mentions("@小明 你好", [{"userId": "abc", "name": "小明"}])
        self.assertEqual(result["atUserIds"], [])

    def test_no_members_returns_original(self):
        self.assertEqual(resolve_mentions("@小明 你好", [])["text"], "@小明 你好")

    def test_garbage_input_does_not_raise(self):
        for text in (None, "", "@", "@@", "@ "):
            with self.subTest(text=text):
                self.assertEqual(resolve_mentions(text, members(("1", "小明")))["atUserIds"], [])
        self.assertEqual(resolve_mentions("hi", [None, "x", 3])["atUserIds"], [])

    def test_leading_punctuation_is_trimmed(self):
        result = resolve_mentions("@小明，你好", members(("1", "小明")))
        self.assertEqual(result["text"], "你好")

    def test_name_stops_at_punctuation(self):
        # 名字后面的标点不会被吃进名字里
        result = resolve_mentions("@小明。你好", members(("1", "小明")))
        self.assertEqual(result["atUserIds"], ["1"])
        self.assertEqual(result["text"], "。你好".lstrip() or "你好")


class MemberDirectoryTests(unittest.TestCase):
    def test_observes_sender_and_offers_members(self):
        directory = MemberDirectory()
        message = {"kind": "group", "target": "g1", "sender_id": "10001", "sender_name": "小明"}
        directory.observe(message)
        self.assertEqual(directory.members(message), [{"userId": "10001", "name": "小明"}])

    def test_rooms_are_isolated(self):
        directory = MemberDirectory()
        left = {"kind": "group", "target": "g1", "sender_id": "1", "sender_name": "甲"}
        right = {"kind": "group", "target": "g2", "sender_id": "2", "sender_name": "乙"}
        directory.observe(left)
        self.assertEqual(directory.members(right), [])

    def test_notice_events_are_not_recorded(self):
        directory = MemberDirectory()
        directory.observe({"kind": "group", "target": "g", "notice": "poke", "sender_id": "1", "sender_name": "甲"})
        self.assertEqual(directory.members({"kind": "group", "target": "g"}), [])

    def test_non_numeric_sender_is_ignored(self):
        directory = MemberDirectory()
        directory.observe({"kind": "group", "target": "g", "sender_id": "abc", "sender_name": "甲"})
        self.assertEqual(directory.members({"kind": "group", "target": "g"}), [])

    def test_missing_name_is_not_offered(self):
        directory = MemberDirectory()
        directory.observe({"kind": "group", "target": "g", "sender_id": "1"})
        self.assertEqual(directory.members({"kind": "group", "target": "g"}), [])

    def test_capacity_is_bounded(self):
        directory = MemberDirectory(max_per_room=3)
        for index in range(5):
            directory.observe({"kind": "group", "target": "g", "sender_id": str(index), "sender_name": f"人{index}"})
        self.assertEqual(len(directory.members({"kind": "group", "target": "g"})), 3)

    def test_forget_clears_room(self):
        directory = MemberDirectory()
        message = {"kind": "group", "target": "g", "sender_id": "1", "sender_name": "甲"}
        directory.observe(message)
        directory.forget(message)
        self.assertEqual(directory.members(message), [])


class FakeManager:
    def __init__(self, settings=None):
        self._settings = dict(AtMention.META.default_settings)
        self._settings.update({"enabled": True})
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def group_msg(target="g1", sender_id="10001", sender_name="小明"):
    return {"id": "m", "kind": "group", "target": target, "text": "hi",
            "sender": sender_name, "sender_id": sender_id, "sender_name": sender_name,
            "at": True, "at_ids": []}


class AtMentionPluginTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **settings):
        return AtMention(FakeManager(settings))

    async def test_turns_inline_at_into_real_mention_metadata(self):
        plugin = self.make()
        plugin.on_ingest(group_msg(), None)
        result = await plugin.on_post_reply(group_msg(), "@小明 你说得对", None)
        self.assertIsInstance(result, tuple)
        text, meta = result
        self.assertEqual(meta["mentions"], ["10001"])
        self.assertNotIn("@", text)

    async def test_returns_plain_string_when_nothing_resolved(self):
        plugin = self.make()
        plugin.on_ingest(group_msg(), None)
        result = await plugin.on_post_reply(group_msg(), "你好", None)
        self.assertEqual(result, "你好")

    async def test_private_chat_is_not_touched(self):
        plugin = self.make()
        message = group_msg()
        message["kind"] = "private"
        plugin.on_ingest(message, None)
        result = await plugin.on_post_reply(message, "@小明 你好", None)
        self.assertEqual(result, "@小明 你好")

    async def test_disabled_plugin_is_a_noop(self):
        plugin = AtMention(FakeManager({"enabled": False}))
        plugin.on_ingest(group_msg(), None)
        result = await plugin.on_post_reply(group_msg(), "@小明 你好", None)
        self.assertEqual(result, "@小明 你好")

    async def test_unknown_member_leaves_text_alone(self):
        plugin = self.make()
        plugin.on_ingest(group_msg(sender_id="1", sender_name="小红"), None)
        result = await plugin.on_post_reply(group_msg(), "@小明 你好", None)
        self.assertEqual(result, "@小明 你好")

    async def test_roster_accumulates_across_messages(self):
        plugin = self.make()
        plugin.on_ingest(group_msg(sender_id="1", sender_name="小明"), None)
        plugin.on_ingest(group_msg(sender_id="2", sender_name="小红"), None)
        result = await plugin.on_post_reply(group_msg(), "@小红 早", None)
        self.assertEqual(result[1]["mentions"], ["2"])

    async def test_partial_match_setting_is_honoured(self):
        plugin = self.make(allowPartialMatch=False)
        plugin.on_ingest(group_msg(sender_id="2", sender_name="小明同学"), None)
        result = await plugin.on_post_reply(group_msg(), "@小明同 早", None)
        self.assertEqual(result, "@小明同 早")

    async def test_ingest_returns_none_so_other_plugins_still_run(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest(group_msg(), None))
        self.assertFalse(plugin.on_block(group_msg(), None))


if __name__ == "__main__":
    unittest.main()
