"""poke_counter（戳一戳统计）的离线测试。

覆盖：计数与持久化、每日/全局与按群两种口径、表达式安全求值、回复文本占位符、
概率与冷却、以及坏数据/坏表达式的回退。
"""

import json
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.plugins.poke_counter import (
    ExpressionError,
    PokeCounter,
    apply_expression,
    build_reply_text,
    today_key,
)


class ExpressionTests(unittest.TestCase):
    def test_basic_arithmetic(self):
        self.assertEqual(apply_expression("n + 1", 0), 1)
        self.assertEqual(apply_expression("n * 2 + 1", 3), 7)
        self.assertEqual(apply_expression("(n + 1) * 2", 4), 10)
        self.assertEqual(apply_expression("n - 5", 2), -3)

    def test_division_and_integral_result(self):
        self.assertEqual(apply_expression("n / 2", 8), 4)
        self.assertAlmostEqual(apply_expression("n / 4", 1), 0.25, places=6)

    def test_allowed_functions(self):
        self.assertEqual(apply_expression("round(n / 3)", 5), 2)
        self.assertEqual(apply_expression("min(n + 5, 10)", 20), 10)
        self.assertEqual(apply_expression("abs(n) - 1", -3), 2)
        self.assertEqual(apply_expression("floor(n / 2)", 7), 3)

    def test_empty_expression_defaults_to_plus_one(self):
        self.assertEqual(apply_expression("", 4), 5)

    def test_invalid_expression_keeps_original_value(self):
        for bad in ("n +", "foo(n)", "n.__class__", "__import__('os')", "1 + [1]", "n if n else 1"):
            with self.subTest(bad=bad):
                self.assertEqual(apply_expression(bad, 7), 7)

    def test_division_by_zero_keeps_original(self):
        self.assertEqual(apply_expression("n / 0", 7), 7)
        self.assertEqual(apply_expression("n % 0", 7), 7)

    def test_huge_power_is_refused(self):
        self.assertEqual(apply_expression("n ** 100000", 2), 2)

    def test_boolean_and_string_literals_are_refused(self):
        self.assertEqual(apply_expression("True", 3), 3)
        self.assertEqual(apply_expression("'abc'", 3), 3)

    def test_non_finite_result_keeps_original(self):
        self.assertEqual(apply_expression("n / n", 0), 0)


class ReplyTextTests(unittest.TestCase):
    def test_placeholder_replacement(self):
        text = build_reply_text("今天第 {count} 次了 {n}", 5, 3, picker=lambda lines: lines[0])
        self.assertEqual(text, "今天第 3 次了 5")

    def test_auto_append_when_placeholders_absent(self):
        text = build_reply_text("别戳了啦", 5, 3, picker=lambda lines: lines[0])
        self.assertIn("别戳了啦", text)
        self.assertIn("当前数值：5", text)
        self.assertIn("今天被戳 3 次了", text)

    def test_no_duplicate_append_when_placeholder_present(self):
        text = build_reply_text("数值 {n}", 5, 3, picker=lambda lines: lines[0])
        self.assertEqual(text.count("5"), 1)
        self.assertIn("今天被戳 3 次了", text)

    def test_append_can_be_disabled(self):
        text = build_reply_text("别戳了啦", 5, 3, show_number=False, show_daily=False,
                                picker=lambda lines: lines[0])
        self.assertEqual(text, "别戳了啦")

    def test_empty_texts_falls_back_to_default_line(self):
        # 没有可用文本行时，仍然会带上数值与次数（顺序与 JS 版一致：数值在前）
        self.assertEqual(build_reply_text("", 5, 3), "当前数值：5，今天被戳 3 次了")

    def test_blank_lines_are_ignored(self):
        text = build_reply_text("\n\n  你好  \n\n", 1, 1, picker=lambda lines: lines[0])
        self.assertTrue(text.startswith("你好"))

    def test_random_pick_returns_one_of_the_lines(self):
        for _ in range(20):
            text = build_reply_text("甲\n乙\n丙", 1, 1, show_number=False, show_daily=False)
            self.assertIn(text, ("甲", "乙", "丙"))


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = Path(state_dir)
        self._settings = dict(PokeCounter.META.default_settings)
        self._settings.update({"enabled": True})
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def poke_msg(group="g1", poked="10001", poker="20002"):
    return {"id": "p1", "kind": "group", "target": group, "text": "", "sender": poker,
            "sender_id": poker, "self_id": "10001", "at": False, "notice": "poke",
            "notice_data": {"poked": poked, "poker": poker, "group": group}}


class PokeCounterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        return PokeCounter(FakeManager(self.dir, settings))

    def state(self):
        # state_file(name) = manager.state_dir / <插件id> / name
        path = self.dir / PokeCounter.META.id / "poke_counter.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def test_non_poke_messages_are_ignored(self):
        plugin = self.make()
        message = poke_msg()
        message["notice"] = "recall"
        self.assertIsNone(plugin.on_ingest(message, None))
        self.assertIsNone(plugin.on_ingest({"kind": "group", "target": "g", "text": "hi"}, None))

    def test_disabled_plugin_ignores_pokes(self):
        plugin = PokeCounter(FakeManager(self.dir, {"enabled": False}))
        self.assertEqual(plugin.on_ingest(poke_msg(), None), "")
        self.assertEqual(self.state(), {})

    def test_count_is_persisted_for_today(self):
        plugin = self.make(replyProbability=0.0)
        for _ in range(3):
            plugin.on_ingest(poke_msg(), None)
        data = self.state()
        self.assertEqual(data["global"]["daily"][today_key()], 3)
        self.assertEqual(data["global"]["total"], 3)

    def test_number_expression_is_applied_and_persisted(self):
        plugin = self.make(replyProbability=0.0, expression="n + 1", initialValue=10)
        plugin.on_ingest(poke_msg(), None)
        plugin.on_ingest(poke_msg(), None)
        self.assertEqual(self.state()["global"]["number"], 12)

    def test_reply_is_sent_when_probability_is_one(self):
        plugin = self.make(replyProbability=1.0, cooldownMs=0, replyTexts="戳我干嘛")
        reply = plugin.on_ingest(poke_msg(), None)
        self.assertIsNotNone(reply)
        self.assertIn("戳我干嘛", reply)

    def test_cooldown_blocks_second_reply(self):
        plugin = self.make(replyProbability=1.0, cooldownMs=60000, replyTexts="戳我干嘛")
        self.assertIsNotNone(plugin.on_ingest(poke_msg(), None))
        # 冷却中不再用模板回，但**也不能把事件吞掉**：返回 None 交给后续插件/AI。
        # 以前返回 ""（已处理不回），真机表现就是"戳了完全没有反应"。
        self.assertIsNone(plugin.on_ingest(poke_msg(), None))
        # 计数仍然照常累加
        self.assertEqual(self.state()["global"]["total"], 2)

    def test_zero_probability_never_replies(self):
        plugin = self.make(replyProbability=0.0)
        # 不抢答，但也不吞事件（None = 交给后面的插件/AI 决定）
        self.assertIsNone(plugin.on_ingest(poke_msg(), None))

    def test_per_group_keeps_separate_counters(self):
        plugin = self.make(perGroup=True, replyProbability=0.0)
        plugin.on_ingest(poke_msg(group="g1"), None)
        plugin.on_ingest(poke_msg(group="g1"), None)
        plugin.on_ingest(poke_msg(group="g2"), None)
        data = self.state()
        self.assertEqual(data["groups"]["group:g1"]["total"], 2)
        self.assertEqual(data["groups"]["group:g2"]["total"], 1)
        self.assertEqual(data["global"]["total"], 0)

    def test_global_mode_shares_one_counter(self):
        plugin = self.make(perGroup=False, replyProbability=0.0)
        plugin.on_ingest(poke_msg(group="g1"), None)
        plugin.on_ingest(poke_msg(group="g2"), None)
        data = self.state()
        self.assertEqual(data["global"]["total"], 2)
        self.assertEqual(data["groups"], {})

    def test_legacy_state_is_migrated(self):
        # 旧版把 {daily,total,number} 直接写在顶层
        yesterday = "2000-01-01"
        legacy = self.dir / PokeCounter.META.id / "poke_counter.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"daily": {yesterday: 4}, "total": 9, "number": 3}), encoding="utf-8")
        plugin = self.make(replyProbability=0.0, expression="n + 1")
        plugin.on_ingest(poke_msg(), None)
        data = self.state()
        self.assertEqual(data["global"]["number"], 4)       # 3 + 1
        self.assertEqual(data["global"]["total"], 10)       # 9 + 1
        self.assertEqual(data["global"]["daily"][yesterday], 4)  # 旧日计数保留
        self.assertEqual(data["global"]["daily"][today_key()], 1)  # 今天新起一条

    def test_corrupt_state_file_does_not_break_counting(self):
        (self.dir / "poke_counter.json").write_text("{not json", encoding="utf-8")
        plugin = self.make(replyProbability=0.0)
        plugin.on_ingest(poke_msg(), None)
        self.assertEqual(self.state()["global"]["total"], 1)

    def test_exceptions_never_escape_on_ingest(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest(None, None))
        self.assertIsNone(plugin.on_ingest({}, None))

    def test_state_round_trips_across_instances(self):
        first = self.make(replyProbability=0.0)
        first.on_ingest(poke_msg(), None)
        second = self.make(replyProbability=0.0)
        second.on_ingest(poke_msg(), None)
        self.assertEqual(self.state()["global"]["total"], 2)


class TodayKeyTests(unittest.TestCase):
    def test_large_result_keeps_number_without_overflowing(self):
        self.assertEqual(apply_expression('9 ** 512', 5), 5)
    def test_zero_padded_date(self):
        self.assertEqual(today_key(datetime(2026, 1, 5)), "2026-01-05")


if __name__ == "__main__":
    unittest.main()
