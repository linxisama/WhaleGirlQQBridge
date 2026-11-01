"""mention_gate（点名闸门）的离线测试。

对齐 JS 版 mention-gate 的三条规则：① 群闸门（只在被 @ / 戳一戳时开口）、
② 只回指定的人、③ 名单外群的待遇。@ 判定按 QQ 号，重名不误判。
"""

import unittest

from src.plugins.mention_gate import MentionGate, parse_ids, parse_user_rules


class ParseHelpersTests(unittest.TestCase):
    def test_parse_ids_tolerates_separators_and_deduplicates(self):
        self.assertEqual(parse_ids("123456789, 987654321，111222333、123456789"), ["123456789", "987654321", "111222333"])

    def test_parse_ids_ignores_short_numbers(self):
        self.assertEqual(parse_ids("123 45678 1234567890"), ["45678", "1234567890"])

    def test_parse_user_rules_multiline(self):
        rules = parse_user_rules("123456789=10001,10002\n987654321＝张三 20003\n")
        self.assertEqual(rules["123456789"], ["10001", "10002"])
        self.assertEqual(rules["987654321"], ["20003"])  # 昵称忽略，只留 QQ 号

    def test_parse_user_rules_skips_blank_and_comment_lines(self):
        rules = parse_user_rules("\n# 说明\nbad line\n123456789=10001\n")
        self.assertEqual(rules, {"123456789": ["10001"]})

    def test_parse_user_rules_ignores_lines_without_ids(self):
        self.assertEqual(parse_user_rules("group=someone"), {})


class FakeManager:
    """最小管理器。

    ``_settings`` 的结构必须与真实 ``PluginManager`` 一致：
    **``{plugin_id: {key: value}}``**（不是扁平的 ``{key: value}``）。
    否则 ``PluginBase.raw_settings``（读 ``_manager._settings[META.id]``）拿到 None，
    旧键迁移会静默失效 —— 这个细节曾经把测试和实现一起带偏。
    """

    def __init__(self, settings=None):
        merged = dict(MentionGate.META.default_settings)
        merged.update({"enabled": True})
        merged.update(settings or {})
        self._settings = {MentionGate.META.id: merged}
        self.updates = []

    def get_settings(self, plugin_id):
        return dict(self._settings.get(plugin_id, {}))

    def update_settings(self, plugin_id, settings):
        self.updates.append(dict(settings))
        self._settings[plugin_id] = dict(settings)


def group_msg(text="随便聊聊", sender_id="10001", at=False, group="123456789", notice=None, at_ids=None):
    message = {"id": "m1", "kind": "group", "target": group, "text": text, "sender": "某人",
               "sender_id": sender_id, "at": at, "at_ids": at_ids or []}
    if notice:
        message["notice"] = notice
    return message


class _Runtime:
    """最小 runtime：只为"机器人自己的 QQ 号"提供 self_id。"""

    def __init__(self, self_id=""):
        self.self_id = self_id
        self.mode = "onebot"


class GroupGateTests(unittest.TestCase):
    def make(self, **settings):
        return MentionGate(FakeManager(settings))

    def test_unmanaged_group_is_untouched_in_allowlist_mode(self):
        plugin = self.make(group_ids="999999999")
        self.assertFalse(plugin.on_block(group_msg(group="123456789"), None))

    def test_unmanaged_group_is_blocked_in_only_mode(self):
        plugin = self.make(group_ids="999999999", mode="only")
        self.assertTrue(plugin.on_block(group_msg(group="123456789"), None))

    def test_managed_group_requires_at(self):
        plugin = self.make(group_ids="123456789")
        self.assertTrue(plugin.on_block(group_msg(at=False), None))

    def test_managed_group_allows_at(self):
        plugin = self.make(group_ids="123456789")
        self.assertFalse(plugin.on_block(group_msg(at=True), None))

    def test_managed_group_allows_poke(self):
        plugin = self.make(group_ids="123456789")
        self.assertFalse(plugin.on_block(group_msg(text="", notice="poke"), None))

    def test_poke_to_self_is_allowed(self):
        plugin = self.make(group_ids="123456789")
        runtime = _Runtime(self_id="999")
        message = group_msg(text="", notice="poke")
        message["notice_data"] = {"poked": "999", "poker": "10001", "group": "123456789"}
        self.assertFalse(plugin.on_block(message, runtime))

    def test_poke_to_someone_else_is_blocked(self):
        # 群里"甲戳乙"不该让机器人开口 —— 原来这里任何拍一拍都放行
        plugin = self.make(group_ids="123456789")
        runtime = _Runtime(self_id="999")
        message = group_msg(text="", notice="poke")
        message["notice_data"] = {"poked": "10002", "poker": "10001", "group": "123456789"}
        self.assertTrue(plugin.on_block(message, runtime))

    def test_poke_without_notice_data_keeps_old_behaviour(self):
        # 拿不到"被戳的是谁"时保守放行，避免因为取不到身份把功能关死
        plugin = self.make(group_ids="123456789")
        self.assertFalse(plugin.on_block(group_msg(text="", notice="poke"), _Runtime(self_id="999")))

    def test_poke_without_self_id_keeps_old_behaviour(self):
        plugin = self.make(group_ids="123456789")
        message = group_msg(text="", notice="poke")
        message["notice_data"] = {"poked": "10002", "poker": "10001", "group": "123456789"}
        self.assertFalse(plugin.on_block(message, _Runtime(self_id="")))

    def test_at_verdict_uses_at_ids_not_nickname(self):
        # 同一个昵称的人在 @ 段里可能是别人；判定只看 at（由 at_ids 推导）
        plugin = self.make(group_ids="123456789")
        self.assertFalse(plugin.on_block(group_msg(at=True, at_ids=["10001"]), None))
        self.assertTrue(plugin.on_block(group_msg(at=False, at_ids=["10002"]), None))

    def test_private_chat_never_blocked(self):
        plugin = self.make(group_ids="123456789")
        message = group_msg()
        message["kind"] = "private"
        self.assertFalse(plugin.on_block(message, None))

    def test_disabled_plugin_never_blocks(self):
        plugin = MentionGate(FakeManager({"enabled": False, "group_ids": "123456789"}))
        self.assertFalse(plugin.on_block(group_msg(at=False), None))


class UserRuleTests(unittest.TestCase):
    def make(self, **settings):
        return MentionGate(FakeManager(settings))

    def test_listed_sender_is_ignored_even_when_at(self):
        plugin = self.make(user_rules="123456789=10001")
        self.assertTrue(plugin.on_block(group_msg(sender_id="10001", at=True), None))

    def test_other_senders_reply_normally(self):
        plugin = self.make(user_rules="123456789=10001")
        self.assertFalse(plugin.on_block(group_msg(sender_id="20002", at=True), None))

    def test_user_rule_applies_even_when_group_not_in_group_ids(self):
        plugin = self.make(group_ids="", user_rules="123456789=10001")
        self.assertTrue(plugin.on_block(group_msg(sender_id="10001", at=True), None))

    def test_missing_sender_id_does_not_block(self):
        # 拿不到 QQ 号时宁可照常回复，也不要静默吃掉消息
        plugin = self.make(user_rules="123456789=10001")
        self.assertFalse(plugin.on_block(group_msg(sender_id="", at=True), None))

    def test_rules_are_per_group(self):
        plugin = self.make(user_rules="123456789=10001")
        self.assertFalse(plugin.on_block(group_msg(sender_id="10001", group="987654321", at=True), None))


class PromptNoteTests(unittest.TestCase):
    def make(self, **settings):
        return MentionGate(FakeManager(settings))

    def test_note_added_for_managed_group(self):
        plugin = self.make(group_ids="123456789")
        hints = plugin.on_pre_ai(group_msg(), [], None)
        self.assertIn("点名闸门", hints["extra_system"])

    def test_no_note_for_unmanaged_group(self):
        plugin = self.make(group_ids="999999999")
        self.assertIsNone(plugin.on_pre_ai(group_msg(), [], None))

    def test_note_can_be_disabled(self):
        plugin = self.make(group_ids="123456789", prompt_note=False)
        self.assertIsNone(plugin.on_pre_ai(group_msg(), [], None))


class LegacyMigrationTests(unittest.TestCase):
    """旧键迁移。

    注意调用点是 ``post_load()`` 而不是构造时：``__init__`` 跑在管理器注册实例**之前**，
    那时 ``update_settings`` 会抛 ``Unknown plugin``（迁移曾经就是这么静默失败的）。
    想让插件在"已注册"状态下迁移，必须显式调 ``post_load()``。
    真实的注册/迁移顺序在 tests/test_plugin_lifecycle.py 里端到端覆盖。
    """

    def test_legacy_only_groups_migrates_to_group_ids(self):
        manager = FakeManager({"onlyGroups": "123456789,987654321"})
        MentionGate(manager).post_load()
        self.assertEqual(manager.updates[-1]["group_ids"], "123456789,987654321")
        self.assertNotIn("onlyGroups", manager.updates[-1])

    def test_new_value_wins_over_legacy(self):
        manager = FakeManager({"onlyGroups": "123456789", "group_ids": "555555555"})
        MentionGate(manager).post_load()
        self.assertEqual(manager.get_settings("mention_gate")["group_ids"], "555555555")

    def test_legacy_keyword_is_dropped_and_logged(self):
        manager = FakeManager({"allowKeyword": "鲸鱼娘"})
        MentionGate(manager).post_load()
        self.assertNotIn("allowKeyword", manager.updates[-1])

    def test_no_legacy_keys_means_no_update(self):
        manager = FakeManager({})
        MentionGate(manager).post_load()
        self.assertEqual(manager.updates, [])


if __name__ == "__main__":
    unittest.main()
