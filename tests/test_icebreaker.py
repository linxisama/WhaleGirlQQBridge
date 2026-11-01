"""icebreaker（定时暖场）的离线测试。

覆盖纯逻辑：目标解析、睡觉时间（含跨零点）、随机间隔与"每轮上限"、
提示词与清理、以及 tick 的调度行为（用假的 AI 与假 onebot，不发真实请求）。
"""

import unittest
from datetime import datetime

from src.plugins.icebreaker import (
    Icebreaker,
    build_prompt,
    clean_reply,
    in_quiet_hours,
    now_minute,
    parse_chats,
    parse_hhmm,
)


class ParseChatsTests(unittest.TestCase):
    def test_parses_group_and_private(self):
        self.assertEqual(
            parse_chats("group_1059300934, private_1410897629"),
            ["group:1059300934", "private:1410897629"],
        )

    def test_tolerates_chinese_comma_and_spaces(self):
        self.assertEqual(
            parse_chats("group_1，private_2  group_3"),
            ["group:1", "private:2", "group:3"],
        )

    def test_deduplicates(self):
        self.assertEqual(parse_chats("group_1,group_1"), ["group:1"])

    def test_ignores_junk(self):
        for raw in ("", None, "hello", "group_", "private_abc", "_123"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_chats(raw), [])

    def test_two_letter_prefixes_are_case_sensitive_lowercase_only(self):
        self.assertEqual(parse_chats("GROUP_1"), [])
        self.assertEqual(parse_chats("Group_1"), [])


class ParseHhmmTests(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(parse_hhmm("23:30"), 23 * 60 + 30)
        self.assertEqual(parse_hhmm("08:00"), 8 * 60)
        self.assertEqual(parse_hhmm("8:05"), 8 * 60 + 5)
        self.assertEqual(parse_hhmm("0:00"), 0)

    def test_full_width_colon(self):
        self.assertEqual(parse_hhmm("23：30"), 23 * 60 + 30)

    def test_hour_only(self):
        self.assertEqual(parse_hhmm("7"), 7 * 60)

    def test_clamps_out_of_range(self):
        self.assertEqual(parse_hhmm("99:99"), 23 * 60 + 59)

    def test_invalid_falls_back(self):
        self.assertEqual(parse_hhmm("abc", "08:00"), 8 * 60)
        self.assertEqual(parse_hhmm("", "23:30"), 23 * 60 + 30)


class QuietHoursTests(unittest.TestCase):
    def test_cross_midnight_window(self):
        start, end = parse_hhmm("23:30"), parse_hhmm("08:00")
        for minute, expected in ((23 * 60 + 45, True), (0, True), (7 * 60 + 59, True),
                                 (8 * 60, False), (12 * 60, False), (23 * 60 + 29, False)):
            with self.subTest(minute=minute):
                self.assertEqual(in_quiet_hours(minute, start, end), expected)

    def test_same_day_window(self):
        start, end = parse_hhmm("13:00"), parse_hhmm("14:00")
        self.assertTrue(in_quiet_hours(13 * 60 + 30, start, end))
        self.assertFalse(in_quiet_hours(12 * 60 + 59, start, end))
        self.assertFalse(in_quiet_hours(14 * 60, start, end))

    def test_start_equals_end_means_no_quiet(self):
        self.assertFalse(in_quiet_hours(0, 60, 60))

    def test_now_minute(self):
        self.assertEqual(now_minute(datetime(2026, 10, 13, 9, 7)), 9 * 60 + 7)


class PromptAndCleanTests(unittest.TestCase):
    def test_default_guide_is_used(self):
        prompt = build_prompt("", 150)
        self.assertIn("暖场", prompt)
        self.assertIn("150", prompt)

    def test_custom_guide_replaces_default(self):
        prompt = build_prompt("多聊学习考试话题", 80)
        self.assertIn("多聊学习考试话题", prompt)
        self.assertIn("80", prompt)

    def test_clean_strips_fences_and_prefix(self):
        self.assertEqual(clean_reply("```\n暖场：今天在忙什么呀？\n```", 150), "今天在忙什么呀？")

    def test_clean_strips_quotes(self):
        self.assertEqual(clean_reply('"今天在忙什么呀？"', 150), "今天在忙什么呀？")

    def test_clean_truncates(self):
        self.assertEqual(len(clean_reply("字" * 300, 100)), 100)

    def test_clean_empty(self):
        self.assertEqual(clean_reply(None, 100), "")
        self.assertEqual(clean_reply("   ", 100), "")


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = state_dir
        self._settings = dict(Icebreaker.META.default_settings)
        self._settings.update({"enabled": True})
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class FakeOneBot:
    def __init__(self, fail=False):
        self.groups = []
        self.privates = []
        self.fail = fail

    async def send_group_msg(self, group_id, text):
        if self.fail:
            raise RuntimeError("send failed")
        self.groups.append((group_id, text))

    async def send_private_msg(self, user_id, text):
        if self.fail:
            raise RuntimeError("send failed")
        self.privates.append((user_id, text))


class FakeAI:
    def __init__(self, raw="今天在忙什么呀？", configured=True, raises=None):
        self.raw = raw
        self.configured = configured
        self.raises = raises
        self.calls = []

    async def chat(self, messages, system_prompt="", temperature=None, max_tokens=None):
        self.calls.append({"temperature": temperature, "max_tokens": max_tokens})
        if self.raises:
            raise self.raises
        return self.raw


class FakeRuntime:
    mode = "onebot"

    def __init__(self, ai, onebot=None, recent=None):
        self.ai = ai
        self.onebot = onebot or FakeOneBot()
        self.recent_messages = recent or []


class PlanningTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        return Icebreaker(FakeManager(self.dir, settings))

    def test_interval_is_within_bounds_and_handles_swapped_bounds(self):
        plugin = self.make(intervalMinHours=4, intervalMaxHours=2)
        for _ in range(20):
            seconds = plugin.next_interval_seconds()
            self.assertGreaterEqual(seconds, 2 * 3600 - 1)
            self.assertLessEqual(seconds, 4 * 3600 + 1)

    def test_next_interval_uses_picker(self):
        plugin = self.make(intervalMinHours=2, intervalMaxHours=6)
        self.assertEqual(plugin.next_interval_seconds(picker=lambda low, high: 3.0), 3 * 3600)

    def test_ensure_plan_spreads_new_chats(self):
        plugin = self.make()
        plugin.ensure_plan(["group:1", "group:2"], now=1000.0)
        self.assertEqual(len(plugin._plan), 2)
        for entry in plugin._plan.values():
            self.assertGreaterEqual(entry["next_at"], 1000.0)

    def test_due_chats_respects_cap_and_order(self):
        plugin = self.make(maxChatsPerRound=2)
        plugin.ensure_plan(["group:1", "group:2", "group:3"], now=0.0)
        plugin._plan["group:1"]["next_at"] = 10.0
        plugin._plan["group:2"]["next_at"] = 20.0
        plugin._plan["group:3"]["next_at"] = 30.0
        due = plugin.due_chats(["group:1", "group:2", "group:3"], now=100.0)
        self.assertEqual(due, ["group:1", "group:2"])

    def test_not_due_yet(self):
        plugin = self.make()
        plugin.ensure_plan(["group:1"], now=0.0)
        plugin._plan["group:1"]["next_at"] = 500.0
        self.assertEqual(plugin.due_chats(["group:1"], now=100.0), [])

    def test_mark_sent_schedules_next_and_counts(self):
        plugin = self.make()
        plugin.mark_sent("group:1", now=1000.0, picker=lambda low, high: 1.0)
        entry = plugin._plan["group:1"]
        self.assertEqual(entry["sent"], 1)
        self.assertEqual(entry["last_sent"], 1000.0)
        self.assertAlmostEqual(entry["next_at"], 1000.0 + 3600, places=3)

    def test_state_round_trips(self):
        first = self.make()
        first.mark_sent("group:1", now=1000.0, picker=lambda low, high: 1.0)
        first._save()
        second = self.make()
        second._load()
        self.assertEqual(second._plan["group:1"]["sent"], 1)

    def test_resolve_chats_prefers_configured(self):
        plugin = self.make(chats="group_9")
        runtime = FakeRuntime(FakeAI(), recent=[{"kind": "group", "target": "1"}])
        self.assertEqual(plugin.resolve_chats(runtime), ["group:9"])

    def test_resolve_chats_falls_back_to_recent(self):
        plugin = self.make(chats="")
        runtime = FakeRuntime(FakeAI(), recent=[
            {"kind": "group", "target": "1"}, {"kind": "private", "target": "2"},
            {"kind": "group", "target": "1"},
        ])
        self.assertEqual(plugin.resolve_chats(runtime), ["group:1", "private:2"])

    def test_resolve_chats_empty_without_recent_data(self):
        plugin = self.make(chats="")
        runtime = FakeRuntime(FakeAI())
        runtime.recent_messages = None
        self.assertEqual(plugin.resolve_chats(runtime), [])


class TickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        return Icebreaker(FakeManager(self.dir, settings))

    async def _run_due(self, plugin, runtime):
        chats = plugin.resolve_chats(runtime)
        plugin.ensure_plan(chats, now=0.0)
        for key in chats:
            plugin._plan[key]["next_at"] = 0.0
        await plugin.tick(runtime)

    async def test_sends_to_group_and_private(self):
        plugin = self.make(chats="group_9, private_7", quietStart="00:00", quietEnd="00:00")
        runtime = FakeRuntime(FakeAI("今天在忙什么呀？"))
        await self._run_due(plugin, runtime)
        self.assertEqual(runtime.onebot.groups, [("9", "今天在忙什么呀？")])
        self.assertEqual(runtime.onebot.privates, [("7", "今天在忙什么呀？")])

    async def test_temperature_and_max_tokens_are_passed(self):
        plugin = self.make(chats="group_9", quietStart="00:00", quietEnd="00:00",
                           temperature=0.9, maxLength=50)
        runtime = FakeRuntime(FakeAI())
        await self._run_due(plugin, runtime)
        self.assertEqual(runtime.ai.calls[0]["temperature"], 0.9)
        self.assertEqual(runtime.ai.calls[0]["max_tokens"], 100)

    async def test_dry_run_does_not_send(self):
        plugin = self.make(chats="group_9", quietStart="00:00", quietEnd="00:00", dryRun=True)
        runtime = FakeRuntime(FakeAI())
        await self._run_due(plugin, runtime)
        self.assertEqual(runtime.onebot.groups, [])
        self.assertEqual(plugin._plan["group:9"]["sent"], 1)   # 仍然计时

    async def test_quiet_hours_block_everything(self):
        plugin = self.make(chats="group_9")
        plugin.is_quiet = lambda moment=None: True
        runtime = FakeRuntime(FakeAI())
        await plugin.tick(runtime)
        self.assertEqual(runtime.onebot.groups, [])
        self.assertEqual(runtime.ai.calls, [])

    async def test_disabled_plugin_does_nothing(self):
        plugin = Icebreaker(FakeManager(self.dir, {"enabled": False, "chats": "group_9"}))
        runtime = FakeRuntime(FakeAI())
        await plugin.tick(runtime)
        self.assertEqual(runtime.ai.calls, [])

    async def test_official_mode_is_skipped(self):
        plugin = self.make(chats="group_9", quietStart="00:00", quietEnd="00:00")
        runtime = FakeRuntime(FakeAI())
        runtime.mode = "official"
        plugin.ensure_plan(["group:9"], now=0.0)
        plugin._plan["group:9"]["next_at"] = 0.0
        await plugin.tick(runtime)
        self.assertEqual(runtime.ai.calls, [])

    async def test_generation_failure_is_swallowed(self):
        plugin = self.make(chats="group_9", quietStart="00:00", quietEnd="00:00")
        runtime = FakeRuntime(FakeAI(raises=RuntimeError("boom")))
        await self._run_due(plugin, runtime)
        self.assertEqual(runtime.onebot.groups, [])
        self.assertEqual(plugin._plan["group:9"]["sent"], 0)

    async def test_send_failure_is_swallowed(self):
        plugin = self.make(chats="group_9", quietStart="00:00", quietEnd="00:00")
        runtime = FakeRuntime(FakeAI(), onebot=FakeOneBot(fail=True))
        await self._run_due(plugin, runtime)   # 不该抛出去
        self.assertEqual(plugin._plan["group:9"]["sent"], 0)

    async def test_no_ai_configured_skips(self):
        plugin = self.make(chats="group_9", quietStart="00:00", quietEnd="00:00")
        runtime = FakeRuntime(FakeAI(configured=False))
        await self._run_due(plugin, runtime)
        self.assertEqual(runtime.onebot.groups, [])

    async def test_min_idle_minutes_defers_recent_chat(self):
        import time as _time
        plugin = self.make(chats="group_9", quietStart="00:00", quietEnd="00:00",
                           minIdleMinutes=30)
        runtime = FakeRuntime(FakeAI(), recent=[
            {"kind": "group", "target": "9", "ts": _time.time()},
        ])
        await self._run_due(plugin, runtime)
        self.assertEqual(runtime.onebot.groups, [])

    async def test_empty_generated_text_does_not_send(self):
        plugin = self.make(chats="group_9", quietStart="00:00", quietEnd="00:00")
        runtime = FakeRuntime(FakeAI("   "))
        await self._run_due(plugin, runtime)
        self.assertEqual(runtime.onebot.groups, [])
        self.assertEqual(plugin._plan["group:9"]["sent"], 0)

    async def test_max_chats_per_round_is_enforced(self):
        plugin = self.make(chats="group_1, group_2, group_3", maxChatsPerRound=1,
                           quietStart="00:00", quietEnd="00:00")
        runtime = FakeRuntime(FakeAI())
        await self._run_due(plugin, runtime)
        self.assertEqual(len(runtime.onebot.groups), 1)


if __name__ == "__main__":
    unittest.main()
