"""turn_complete（对话完成判定）的离线测试。

判定规则移植自 JS 插件 turn-complete，注释里记录了每一条判据的来由
（尤其是"短 = 还没说完"和"没有标点不等于说完了"这两条踩过的坑）。
"""

import unittest

from src.plugins.turn_complete import TurnComplete, summarize, verdict


class VerdictTests(unittest.TestCase):
    def test_empty_message_is_unfinished(self):
        self.assertEqual(verdict("")["verdict"], "unfinished")
        self.assertEqual(verdict("   ")["verdict"], "unfinished")

    def test_open_tail_punctuation_means_unfinished(self):
        for text in ("我想买个，", "然后呢……", "大概是——", "（还没说完", "他说：“"):
            with self.subTest(text=text):
                self.assertEqual(verdict(text)["verdict"], "unfinished")

    def test_continuation_word_at_end_means_unfinished(self):
        for text in ("我今天想说的是然后", "这个功能不错但是", "我再想想那个"):
            with self.subTest(text=text):
                self.assertEqual(verdict(text)["verdict"], "unfinished")

    def test_short_message_is_unfinished(self):
        # 短是最容易被低估的信号：有人真的把一句话拆成一条一条发
        for text in ("你", "认为", "有", "那个功能"):
            with self.subTest(text=text):
                self.assertEqual(verdict(text)["verdict"], "unfinished")

    def test_opener_is_done(self):
        # 「哈喽~」按 JS 原版顺序会被"结尾悬着"先判成 unfinished（波浪号在 OPEN_TAIL 里），
        # 这里如实照搬原版行为，不擅自改判据。
        for text in ("在吗", "在不在", "你好", "hi"):
            with self.subTest(text=text):
                self.assertEqual(verdict(text)["verdict"], "done")
        self.assertEqual(verdict("哈喽~")["verdict"], "unfinished")

    def test_short_question_is_done(self):
        for text in ("有货吗", "多少钱", "还有吗"):
            with self.subTest(text=text):
                self.assertEqual(verdict(text)["verdict"], "done")

    def test_single_question_particle_is_not_done(self):
        # 单个「吗」「呢」只是对方那句话的尾巴，不是一句问话
        for text in ("吗", "呢", "嘛"):
            with self.subTest(text=text):
                self.assertEqual(verdict(text)["verdict"], "unfinished")

    def test_long_statement_is_done(self):
        self.assertEqual(verdict("我今天下午三点左右会把那个新的接口文档整理好发给你")["verdict"], "done")

    def test_tail_decorations_are_stripped_before_judging(self):
        # 结尾是引号/括号时，要看引号里面的真实结尾
        self.assertEqual(verdict("我想说的是“")["verdict"], "unfinished")
        self.assertEqual(verdict("这个问题你怎么看）")["verdict"], "done")

    def test_why_is_always_a_non_empty_string(self):
        for text in ("", "在吗", "我想买个，", "我今天下午三点会给你完整的答复"):
            with self.subTest(text=text):
                result = verdict(text)
                self.assertIn(result["verdict"], ("done", "unfinished"))
                self.assertTrue(result["why"])


class SummarizeTests(unittest.TestCase):
    def test_self_messages_are_excluded(self):
        info = summarize([
            {"sender": "1", "sender_name": "甲", "text": "a"},
            {"sender": "1", "sender_name": "甲", "text": "b", "self": True},
        ])
        self.assertEqual(info["total"], 1)
        self.assertEqual(info["speakers"][0]["count"], 1)

    def test_counts_and_order_follow_first_appearance(self):
        info = summarize([
            {"sender": "2", "sender_name": "乙", "text": "x"},
            {"sender": "1", "sender_name": "甲", "text": "y"},
            {"sender": "2", "sender_name": "乙", "text": "z"},
        ])
        self.assertEqual([s["id"] for s in info["speakers"]], ["2", "1"])
        self.assertEqual([s["count"] for s in info["speakers"]], [2, 1])
        self.assertEqual(info["total"], 3)

    def test_missing_nickname_falls_back_to_qq_label(self):
        info = summarize([{"sender": "7", "text": "hi"}])
        self.assertEqual(info["speakers"][0]["name"], "QQ:7")

    def test_better_name_replaces_placeholder(self):
        info = summarize([
            {"sender": "7", "text": "hi"},
            {"sender": "7", "sender_name": "丙", "text": "again"},
        ])
        self.assertEqual(info["speakers"][0]["name"], "丙")

    def test_garbage_entries_are_ignored(self):
        info = summarize([None, "x", 3, {"sender": "1", "text": "ok"}])
        self.assertEqual(info["total"], 1)


class TurnCompleteHookTests(unittest.TestCase):
    """直接调用插件对象（不走管理器），验证延迟与提示词注入。"""

    class FakeManager:
        def __init__(self, settings):
            self._settings = settings

        def get_settings(self, plugin_id):
            return dict(self._settings)

        @property
        def state_dir(self):
            import pathlib
            import tempfile
            return pathlib.Path(tempfile.gettempdir())

    def make(self, **overrides):
        settings = dict(TurnComplete.META.default_settings)
        settings.update({"enabled": True})
        settings.update(overrides)
        plugin = TurnComplete(self.FakeManager(settings))
        return plugin

    def msg(self, text, sender="u1", kind="group", target="g1"):
        return {"id": "m", "kind": kind, "target": target, "text": text, "sender": sender, "at": True}

    def test_disabled_plugin_returns_none(self):
        plugin = TurnComplete(self.FakeManager(dict(TurnComplete.META.default_settings)))
        self.assertIsNone(plugin.on_pre_ai(self.msg("我想买个，"), [], None))

    def test_unfinished_message_produces_delay(self):
        plugin = self.make(quietMs=6000, capMs=20000, maxDelayMs=8000)
        hints = plugin.on_pre_ai(self.msg("我想买个，"), [], None)
        self.assertIsNotNone(hints)
        self.assertAlmostEqual(hints["delay"], 6.0, places=3)

    def test_delay_is_capped_by_capMs(self):
        plugin = self.make(quietMs=60000, capMs=20000, maxDelayMs=120000)
        hints = plugin.on_pre_ai(self.msg("我想买个，"), [], None)
        self.assertAlmostEqual(hints["delay"], 20.0, places=3)

    def test_delay_is_capped_by_maxDelayMs(self):
        plugin = self.make(quietMs=60000, capMs=120000, maxDelayMs=8000)
        hints = plugin.on_pre_ai(self.msg("我想买个，"), [], None)
        self.assertAlmostEqual(hints["delay"], 8.0, places=3)

    def test_capMs_zero_disables_waiting(self):
        plugin = self.make(capMs=0)
        self.assertIsNone(plugin.on_pre_ai(self.msg("我想买个，"), [], None))

    def test_done_message_does_not_delay(self):
        plugin = self.make()
        hints = plugin.on_pre_ai(self.msg("有货吗"), [], None)
        self.assertTrue(not hints or "delay" not in hints)

    def test_rapid_follow_up_overrides_done_verdict(self):
        # 单看「什么好玩的」像一句问话，但跟上一条只隔一小会儿 → 判为还没说完
        plugin = self.make(quietMs=6000, capMs=20000)
        plugin.on_pre_ai(self.msg("你认为"), [], None)
        hints = plugin.on_pre_ai(self.msg("什么好玩的"), [], None)
        self.assertIsNotNone(hints)
        self.assertIn("delay", hints)

    def test_anchor_only_ignores_other_speakers(self):
        plugin = self.make(anchorOnly=True)
        plugin.on_pre_ai(self.msg("我想买个，", sender="u1"), [], None)
        self.assertIsNone(plugin.on_pre_ai(self.msg("我也说一句，", sender="u2"), [], None))

    def test_anchor_only_off_considers_everyone(self):
        plugin = self.make(anchorOnly=False)
        plugin.on_pre_ai(self.msg("我想买个，", sender="u1"), [], None)
        hints = plugin.on_pre_ai(self.msg("我也说一句，", sender="u2"), [], None)
        self.assertIsNotNone(hints)

    def test_multi_speaker_note_is_injected(self):
        plugin = self.make()
        plugin.on_pre_ai(self.msg("我想买个，", sender="u1"), [], None)
        # 锚点过期后换人说话：这才是"群里换人接话"的真实情形
        mark = plugin._marks["group:g1"]
        mark["ts"] -= 600
        hints = plugin.on_pre_ai(self.msg("你要买什么", sender="u2"), [], None)
        self.assertIsNotNone(hints)
        self.assertIn("不只一个人在说话", hints.get("extra_system", ""))

    def test_anchor_expires_so_other_speakers_are_not_starved(self):
        plugin = self.make()
        plugin.on_pre_ai(self.msg("我想买个，", sender="u1"), [], None)
        plugin._marks["group:g1"]["ts"] -= 600
        hints = plugin.on_pre_ai(self.msg("我也问一句，", sender="u2"), [], None)
        self.assertIsNotNone(hints)

    def test_fresh_anchor_still_filters_other_speakers(self):
        plugin = self.make()
        plugin.on_pre_ai(self.msg("我想买个，", sender="u1"), [], None)
        self.assertIsNone(plugin.on_pre_ai(self.msg("我也问一句，", sender="u2"), [], None))

    def test_tell_model_off_produces_no_extra_system(self):
        plugin = self.make(tellModel=False)
        hints = plugin.on_pre_ai(self.msg("我想买个，"), [], None)
        self.assertNotIn("extra_system", hints or {})

    def test_notice_events_are_ignored(self):
        plugin = self.make()
        message = self.msg("")
        message["notice"] = "poke"
        self.assertIsNone(plugin.on_pre_ai(message, [], None))

    def test_broken_settings_do_not_escape_on_pre_ai(self):
        # enabled 属性在 PluginBase 层求值，异常由 PluginManager.run_on_pre_ai 兜底；
        # 这里覆盖的是"设置值本身是脏的"时，钩子内部不抛错。
        broken_keys = {
            "enabled": True, "capMs": "not-a-number", "quietMs": None,
            "maxDelayMs": {}, "contentJudge": True, "anchorOnly": True, "tellModel": True,
        }
        plugin = TurnComplete(self.FakeManager(broken_keys))
        self.assertIsNone(plugin.on_pre_ai(self.msg("我想买个，"), [], None))

    def test_manager_swallows_plugin_exceptions(self):
        # 真实调用路径：manager 不能因为某个插件读设置失败而中断整条链路
        from src.plugins.manager import PluginManager

        class BrokenManager:
            def get_settings(self, plugin_id):
                raise RuntimeError("boom")

        manager = PluginManager.__new__(PluginManager)
        manager._plugins = {"turn_complete": TurnComplete(BrokenManager())}
        result = manager.run_on_pre_ai(self.msg("hi"), [], None)
        self.assertEqual(result["delay"], 0.0)
        self.assertFalse(result["block"])


if __name__ == "__main__":
    unittest.main()
