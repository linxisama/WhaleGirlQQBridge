"""affinity_proactive（好感度主动开话题）的离线测试。

其中 PluginDiscoveryTests 是核心审核时补的关键验收：插件必须继承项目真正的
PluginBase 并被 PluginManager 扫到 —— 原稿自造了一个 PluginBase，装上不会生效
且不报错，这类问题只有靠「能否被发现」的断言才拦得住。
"""

import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from src.plugins.affinity_proactive import (
    ATTITUDE_BRIEF,
    LEVEL_MINS,
    LEVEL_NAMES,
    PROACTIVE_TABLE,
    AffinityProactive,
    build_proactive_prompt,
    clean_reply,
    escape_cq_text,
    in_quiet_hours,
    in_whitelist,
    level_of,
    next_check_at,
    parse_scores,
    should_send,
)


class FakeManager:
    """最小 manager：提供 state_dir 与设置读取（与真 PluginManager 的用法一致）。"""

    def __init__(self, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(AffinityProactive.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class PluginDiscoveryTests(unittest.TestCase):
    def test_plugin_is_discoverable_by_plugin_manager(self):
        from src.plugins import PluginBase
        from src.plugins.manager import PluginManager

        self.assertTrue(issubclass(AffinityProactive, PluginBase),
                        "必须继承 src.plugins.PluginBase，不能自造一个")
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(pathlib.Path(tmp))
            self.assertIn("affinity_proactive", manager._plugins)
            self.assertFalse(manager._plugins["affinity_proactive"].enabled)

    def test_meta_is_a_plugin_meta_with_schema(self):
        from src.plugins import PluginMeta

        self.assertIsInstance(AffinityProactive.META, PluginMeta)
        schema = AffinityProactive.META.schema_dict()
        for key in ("enabled", "affinityScores", "quietStartHour", "quietEndHour",
                    "idleMinMs", "maxTokens", "allowList", "denyList", "allowAllWhenEmpty"):
            self.assertIn(key, schema)


class ConstantTests(unittest.TestCase):
    def test_level_mins(self):
        self.assertEqual(LEVEL_MINS, [0, 20, 50, 90, 140, 200])

    def test_level_names(self):
        self.assertEqual(LEVEL_NAMES, ['陌生', '面熟', '熟络', '亲近', '交心', '挚友'])

    def test_proactive_table(self):
        self.assertEqual(PROACTIVE_TABLE, [
            (3600000, 7200000, 0.02),
            (2700000, 5400000, 0.05),
            (1800000, 5400000, 0.10),
            (1800000, 3600000, 0.25),
            (3600000, 7200000, 0.15),
            (5400000, 10800000, 0.08),
        ])

    def test_attitude_brief_matches_js_source_verbatim(self):
        # 防止语气简表再被改写：必须与 JS 原文逐字一致
        self.assertEqual(ATTITUDE_BRIEF, [
            '礼貌、简短、有距离但不冷；几乎不主动；不接玩笑；私人问题模糊带过',
            '稍微放松、多给半句；极少主动（出现时可能先说「来了」）；能接一句玩笑但不追',
            '自然友好、接得住话；偶尔主动但话说一半；顺手逗一下；允许绕着说',
            '明显放松、有温度但不黏；会主动分享日常；偶尔泄露一点真实情绪',
            '亲近自然、偶尔柔软；会记挂对方；做具体的小事而不是只说',
            '真实坦率、不端着；可以只是一句「嗯」，也可以忽然说一句重的；稀有但真诚',
        ])


class LevelTests(unittest.TestCase):
    def test_boundaries(self):
        for score, expected in ((19, 0), (20, 1), (50, 2), (139, 3), (140, 4), (200, 5),
                                (500, 5), (-10, 0), (None, 0), ("abc", 0), (True, 0)):
            with self.subTest(score=score):
                self.assertEqual(level_of(score), expected)


class QuietHoursTests(unittest.TestCase):
    def test_cross_midnight(self):
        for hour, expected in ((22, True), (23, True), (0, True), (7, True),
                               (8, False), (12, False)):
            with self.subTest(hour=hour):
                self.assertEqual(in_quiet_hours(hour, 22, 8), expected)

    def test_same_day_window(self):
        self.assertTrue(in_quiet_hours(13, 13, 15))
        self.assertFalse(in_quiet_hours(12, 13, 15))
        self.assertFalse(in_quiet_hours(15, 13, 15))

    def test_equal_bounds_means_no_quiet(self):
        self.assertFalse(in_quiet_hours(3, 8, 8))


class WhitelistTests(unittest.TestCase):
    @staticmethod
    def cfg(allow="", deny="", allow_all=False):
        return {"allowList": allow, "denyList": deny, "allowAllWhenEmpty": allow_all}

    def test_invalid_chat_key(self):
        for key in ("", "123", "group:", "private:abc", "GROUP:1", "group:1:2"):
            with self.subTest(key=key):
                self.assertFalse(in_whitelist(key, self.cfg(allow_all=True)))

    def test_deny_takes_precedence(self):
        self.assertFalse(in_whitelist("group:1", self.cfg(allow="group:1", deny="group:1")))

    def test_allow_must_match_when_present(self):
        self.assertTrue(in_whitelist("group:1", self.cfg(allow="group:1")))
        self.assertFalse(in_whitelist("group:2", self.cfg(allow="group:1")))
        self.assertFalse(in_whitelist("private:1", self.cfg(allow="group:1")))

    def test_bare_id_matches_any_kind(self):
        self.assertTrue(in_whitelist("group:1", self.cfg(allow="1")))
        self.assertTrue(in_whitelist("private:1", self.cfg(allow="1")))

    def test_both_empty_needs_explicit_flag(self):
        self.assertFalse(in_whitelist("group:1", self.cfg()))
        self.assertTrue(in_whitelist("group:1", self.cfg(allow_all=True)))

    def test_deny_only_is_blacklist_semantics(self):
        # allow 为空、deny 非空 = 只拉黑：没被拉黑就放行
        self.assertTrue(in_whitelist("group:2", self.cfg(deny="group:1")))
        self.assertFalse(in_whitelist("group:1", self.cfg(deny="group:1")))

    def test_separators(self):
        self.assertTrue(in_whitelist("group:1", self.cfg(allow="group:9，group:1 group:8")))


class ScheduleTests(unittest.TestCase):
    def test_next_check_at_uses_injected_picker(self):
        self.assertEqual(next_check_at(0, 100.0, picker=lambda low, high: 60.0), 160.0)

    def test_next_check_at_bounds_per_level(self):
        for level, (min_ms, max_ms, _) in enumerate(PROACTIVE_TABLE):
            with self.subTest(level=level):
                value = next_check_at(level, 0.0)
                self.assertGreaterEqual(value, min_ms / 1000.0)
                self.assertLessEqual(value, max_ms / 1000.0)

    def test_next_check_at_clamps_level(self):
        self.assertEqual(next_check_at(99, 0.0, picker=lambda low, high: 1.0), 1.0)


def _at_hour(hour):
    return time.struct_time((2026, 10, 13, hour, 0, 0, 0, 0, 0))


class ShouldSendTests(unittest.TestCase):
    def test_quiet_hours(self):
        with patch("src.plugins.affinity_proactive.time.localtime", return_value=_at_hour(2)):
            result = should_send(0, None, False, 1000.0, 600000,
                                 quiet_start_hour=0, quiet_end_hour=8, random_value=0.0)
        self.assertFalse(result["send"])
        self.assertEqual(result["reason"], "静默时段")

    def test_just_interacted_uses_idle_min_ms_not_table(self):
        # 关键：判「刚互动过」用的是 idleMinMs（设置），不是 PROACTIVE_TABLE 的排期间隔
        with patch("src.plugins.affinity_proactive.time.localtime", return_value=_at_hour(15)):
            now = 1_000_000.0
            result = should_send(0, now - 60, False, now, 600000, random_value=0.0)
        self.assertFalse(result["send"])
        self.assertEqual(result["reason"], "刚互动过")

    def test_long_idle_passes_idle_check(self):
        with patch("src.plugins.affinity_proactive.time.localtime", return_value=_at_hour(15)):
            now = 10_000_000.0
            result = should_send(0, now - 100000, False, now, 600000, random_value=0.0)
        self.assertTrue(result["send"])

    def test_unread_blocks(self):
        with patch("src.plugins.affinity_proactive.time.localtime", return_value=_at_hour(15)):
            now = 10_000_000.0
            result = should_send(0, now - 100000, True, now, 600000, random_value=0.0)
        self.assertFalse(result["send"])
        self.assertEqual(result["reason"], "有未读消息")

    def test_probability_miss_and_hit(self):
        with patch("src.plugins.affinity_proactive.time.localtime", return_value=_at_hour(15)):
            now = 10_000_000.0
            miss = should_send(0, now - 100000, False, now, 600000, random_value=0.5)
            hit = should_send(0, now - 100000, False, now, 600000, random_value=0.01)
        self.assertEqual(miss, {"send": False, "reason": "未命中概率"})
        self.assertTrue(hit["send"])

    def test_level_probability_is_used(self):
        with patch("src.plugins.affinity_proactive.time.localtime", return_value=_at_hour(15)):
            now = 10_000_000.0
            # 等级 3 的概率是 0.25：0.2 命中，0.3 不命中
            self.assertTrue(should_send(3, now - 100000, False, now, 0, random_value=0.2)["send"])
            self.assertFalse(should_send(3, now - 100000, False, now, 0, random_value=0.3)["send"])


class PromptAndCleanTests(unittest.TestCase):
    def test_prompt_contains_level_attitude(self):
        prompt = build_proactive_prompt(2)
        self.assertIn(LEVEL_NAMES[2], prompt)
        self.assertIn(ATTITUDE_BRIEF[2], prompt)

    def test_prompt_includes_optional_parts(self):
        prompt = build_proactive_prompt(0, persona_brief="傲娇鲸鱼娘", guide="多聊学习")
        self.assertIn("傲娇鲸鱼娘", prompt)
        self.assertIn("多聊学习", prompt)

    def test_clean_strips_fence_quotes_and_prefix(self):
        self.assertEqual(clean_reply("```\n主动：今天在忙什么呀？\n```", 100), "今天在忙什么呀？")
        self.assertEqual(clean_reply('"今天在忙什么呀？"', 100), "今天在忙什么呀？")
        self.assertEqual(clean_reply("暖场：晚上好", 100), "晚上好")

    def test_clean_truncates(self):
        self.assertEqual(clean_reply("字" * 300, 50), "字" * 50)

    def test_clean_empty(self):
        self.assertEqual(clean_reply(None, 50), "")
        self.assertEqual(clean_reply("   ", 50), "")


class EscapeTests(unittest.TestCase):
    def test_order_and_result(self):
        # & 必须最先转义，否则会把新生成的 &amp; 再转一遍
        self.assertEqual(escape_cq_text("&[a],b"), "&amp;&#91;a&#93;&#44;b")

    def test_empty(self):
        self.assertEqual(escape_cq_text(""), "")
        self.assertEqual(escape_cq_text(None), "")


class ParseScoresTests(unittest.TestCase):
    def test_parses_valid_keys(self):
        self.assertEqual(parse_scores("group:123=95, private:456=140"),
                         {"group:123": 95.0, "private:456": 140.0})

    def test_ignores_invalid(self):
        for raw in ("", None, "hello", "123=5", "group:abc=5", "group:1=x"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_scores(raw), {})


class FakeAI:
    def __init__(self, reply="今天在忙什么呀？", configured=True, raises=None):
        self.reply = reply
        self.configured = configured
        self.raises = raises
        self.calls = []

    async def chat(self, messages, system_prompt="", temperature=None, max_tokens=None):
        self.calls.append({"temperature": temperature, "max_tokens": max_tokens})
        if self.raises:
            raise self.raises
        return self.reply


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


class FakeRuntime:
    def __init__(self, ai=None, onebot=None, mode="onebot"):
        self.ai = ai or FakeAI()
        self.onebot = onebot or FakeOneBot()
        self.mode = mode


class TickTests(unittest.IsolatedAsyncioTestCase):
    """tick 的行为测试。

    注意：等级概率是真实掷骰（等级 3 只有 0.25），所以这里必须把
    ``random.random`` 固定成 0.0，否则测试会随机失败 —— 这也是原稿没考虑到的地方。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._rand = patch("src.plugins.affinity_proactive.random.random", return_value=0.0)
        self._rand.start()

    def tearDown(self):
        self._rand.stop()
        self._tmp.cleanup()

    @staticmethod
    def base(**extra):
        config = {"enabled": True, "affinityScores": "group:1=100", "allowAllWhenEmpty": True,
                  "quietStartHour": 0, "quietEndHour": 0}
        config.update(extra)
        return config

    def make(self, **settings):
        # 清掉上一次测试留下的状态文件，避免 nextCheckAt 串到下一例
        state = self.dir / AffinityProactive.META.id / "affinity_proactive.json"
        if state.is_file():
            state.unlink()
        return AffinityProactive(FakeManager(self.dir, settings))

    def make_keeping_state(self, **settings):
        """不清状态文件的构造器，用于验证持久化。"""
        return AffinityProactive(FakeManager(self.dir, settings))

    @staticmethod
    def due(plugin, chat_key, last_sent=0):
        plugin.state()["chats"][chat_key] = {"nextCheckAt": 0, "lastSentAt": last_sent}
        return plugin

    async def test_not_reached_does_not_generate(self):
        plugin = self.make(**self.base())
        plugin.state()["chats"]["group:1"] = {"nextCheckAt": time.time() + 10000}
        runtime = FakeRuntime()
        await plugin.tick(runtime)
        self.assertEqual(runtime.ai.calls, [])

    async def test_reached_and_sent(self):
        plugin = self.due(self.make(**self.base()), "group:1")
        runtime = FakeRuntime()
        await plugin.tick(runtime)
        self.assertEqual(len(runtime.ai.calls), 1)
        self.assertEqual(runtime.onebot.groups, [(1, "今天在忙什么呀？")])
        chat = plugin.state()["chats"]["group:1"]
        self.assertGreater(chat["lastSentAt"], 0)
        self.assertGreater(chat["nextCheckAt"], 0)

    async def test_private_target_uses_private_sender(self):
        plugin = self.due(self.make(**self.base(affinityScores="private:7=100")), "private:7")
        runtime = FakeRuntime()
        await plugin.tick(runtime)
        self.assertEqual(runtime.onebot.privates, [(7, "今天在忙什么呀？")])

    async def test_ai_error_does_not_escape(self):
        plugin = self.due(self.make(**self.base()), "group:1")
        runtime = FakeRuntime(ai=FakeAI(raises=RuntimeError("AI Error")))
        await plugin.tick(runtime)
        self.assertEqual(runtime.onebot.groups, [])

    async def test_send_error_does_not_advance_schedule(self):
        plugin = self.due(self.make(**self.base()), "group:1")
        runtime = FakeRuntime(onebot=FakeOneBot(fail=True))
        await plugin.tick(runtime)
        self.assertEqual(plugin.state()["chats"].get("group:1", {}).get("lastSentAt", 0), 0)

    async def test_disabled_does_not_generate(self):
        plugin = self.make(**self.base(enabled=False))
        runtime = FakeRuntime()
        await plugin.tick(runtime)
        self.assertEqual(runtime.ai.calls, [])

    async def test_official_mode_does_not_generate(self):
        plugin = self.due(self.make(**self.base()), "group:1")
        runtime = FakeRuntime(mode="official")
        await plugin.tick(runtime)
        self.assertEqual(runtime.ai.calls, [])

    async def test_max_tokens_is_passed(self):
        plugin = self.due(self.make(**self.base(maxTokens=42)), "group:1")
        runtime = FakeRuntime()
        await plugin.tick(runtime)
        self.assertEqual(runtime.ai.calls[0]["max_tokens"], 42)

    async def test_quiet_hours_reschedules_without_sending(self):
        plugin = self.due(self.make(**self.base(quietStartHour=0, quietEndHour=23)), "group:1")
        runtime = FakeRuntime()
        # 固定在静默时段内，不能让这条断言每天到23点就失败。
        with patch('src.plugins.affinity_proactive.time.localtime',
                   return_value=time.struct_time((2026,10,16,12,0,0,4,289,-1))):
            await plugin.tick(runtime)
        self.assertEqual(runtime.ai.calls, [])
        self.assertGreater(plugin.state()["chats"]["group:1"]["nextCheckAt"], 0)

    async def test_quiet_end_at_23_allows_the_scheduled_turn(self):
        plugin = self.due(self.make(**self.base(quietStartHour=0, quietEndHour=23)), 'group:1')
        runtime = FakeRuntime()
        with patch('src.plugins.affinity_proactive.time.localtime',
                   return_value=time.struct_time((2026,10,16,23,0,0,4,289,-1))):
            await plugin.tick(runtime)
        self.assertTrue(runtime.ai.calls)

    async def test_whitelist_blocks_unlisted_chat(self):
        plugin = self.due(self.make(**self.base(allowAllWhenEmpty=False,
                                                allowList="group:999")), "group:1")
        runtime = FakeRuntime()
        await plugin.tick(runtime)
        self.assertEqual(runtime.ai.calls, [])

    async def test_state_persists_across_instances(self):
        first = self.make_keeping_state(**self.base())
        now = time.time()
        first.state()["chats"]["group:1"] = {"nextCheckAt": now, "lastSentAt": now}
        first.persist()
        second = self.make_keeping_state(**self.base())
        self.assertEqual(second.state()["chats"]["group:1"]["lastSentAt"], now)

    async def test_corrupt_state_falls_back_to_empty(self):
        path = self.dir / AffinityProactive.META.id / "affinity_proactive.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        plugin = self.make(**self.base())
        self.assertEqual(plugin.state()["chats"], {})


if __name__ == "__main__":
    unittest.main()
