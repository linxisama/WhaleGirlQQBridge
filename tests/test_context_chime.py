"""context_chime（上下文插话判定）的离线测试。

重点覆盖两块：
1. 纯逻辑：判定输出解析（宽容）、提示词构造、规则闸门（早退分支）；
2. 失败方向：JS 版是"判不出来就不接"（保守侧），早先的 Python 实现方向相反。
   这里用假的 AI 对象验证超时/失败/解析失败各自的落点，不发真实请求。
"""

import asyncio
import unittest
from datetime import datetime

from src.plugins.context_chime import (
    ContextChime,
    build_judge_messages,
    clip,
    parse_judge,
    today_key,
)


class ClipTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(clip("  你好\n\n世界  ", 20), "你好 世界")

    def test_truncates_with_ellipsis(self):
        self.assertEqual(clip("abcdef", 3), "abc…")

    def test_short_text_untouched(self):
        self.assertEqual(clip("你好", 10), "你好")

    def test_none_and_empty(self):
        self.assertEqual(clip(None, 5), "")
        self.assertEqual(clip("", 5), "")


class ParseJudgeTests(unittest.TestCase):
    def test_reply_number(self):
        result = parse_judge('{"reply":1,"confidence":80,"why":"在问他"}')
        self.assertEqual(result["respond"], True)
        self.assertEqual(result["confidence"], 80)
        self.assertEqual(result["why"], "在问他")

    def test_reply_zero(self):
        self.assertFalse(parse_judge('{"reply":0,"confidence":10}')["respond"])

    def test_should_respond_boolean(self):
        self.assertTrue(parse_judge('{"should_respond":true}')["respond"])
        self.assertFalse(parse_judge('{"should_respond":false}')["respond"])

    def test_respond_alias(self):
        self.assertTrue(parse_judge('{"respond":1}')["respond"])

    def test_string_forms(self):
        for raw, expected in (('{"reply":"是"}', True), ('{"reply":"true"}', True),
                              ('{"reply":"no"}', False), ('{"reply":"0"}', False)):
            with self.subTest(raw=raw):
                self.assertEqual(parse_judge(raw)["respond"], expected)

    def test_markdown_fence_is_tolerated(self):
        raw = '```json\n{"reply":1,"confidence":55}\n```'
        self.assertEqual(parse_judge(raw)["confidence"], 55)

    def test_extra_text_around_json_is_tolerated(self):
        raw = '好的，我的判断是 {"reply":1,"confidence":70} 就这样'
        self.assertTrue(parse_judge(raw)["respond"])

    def test_unparseable_returns_none(self):
        for raw in ("", "   ", "not json", "{}", '{"confidence":50}', None):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_judge(raw))

    def test_confidence_is_clamped(self):
        self.assertEqual(parse_judge('{"reply":1,"confidence":999}')["confidence"], 100)
        self.assertEqual(parse_judge('{"reply":1,"confidence":-5}')["confidence"], 0)

    def test_reason_alias_and_length(self):
        result = parse_judge('{"reply":1,"reason":"' + "很" * 60 + '"}')
        self.assertLessEqual(len(result["why"]), 41)   # 40 + 省略号


class BuildJudgeMessagesTests(unittest.TestCase):
    def test_two_messages_with_roles(self):
        messages = build_judge_messages("小鲸鱼", last_line="群友：你好")
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn("该不该主动接话", messages[0]["content"])
        self.assertIn("群友：你好", messages[1]["content"])

    def test_warns_about_reading_the_wrong_speaker(self):
        system = build_judge_messages("小鲸鱼")[0]["content"]
        self.assertIn("别读错人", system)
        self.assertIn("我：", system)

    def test_self_nickname_only_when_different(self):
        with_alias = build_judge_messages("小鲸鱼", self_nickname="小鲸鲸")[1]["content"]
        without = build_judge_messages("小鲸鱼", self_nickname="小鲸鱼")[1]["content"]
        self.assertIn("小鲸鲸", with_alias)
        self.assertNotIn("也被叫", without)

    def test_optional_sections(self):
        messages = build_judge_messages("小鲸鱼", persona_brief="傲娇", recent_lines="甲：在吗",
                                        prompt_extra="别提政治")
        user = messages[1]["content"]
        self.assertIn("傲娇", user)
        self.assertIn("甲：在吗", user)
        self.assertIn("别提政治", messages[0]["content"])

    def test_asks_for_json_only(self):
        system = build_judge_messages("小鲸鱼")[0]["content"]
        self.assertIn("只输出一个 JSON 对象", system)


class FakeManager:
    def __init__(self, settings=None):
        self._settings = dict(ContextChime.META.default_settings)
        self._settings.update({"enabled": True})
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def group_msg(text="今天天气不错啊", target="g1"):
    return {"id": "m", "kind": "group", "target": target, "text": text,
            "sender": "某人", "sender_id": "10001", "sender_name": "某人", "at": False}


class ShouldSkipTests(unittest.TestCase):
    def make(self, **settings):
        return ContextChime(FakeManager(settings))

    def test_disabled(self):
        plugin = ContextChime(FakeManager({"enabled": False}))
        self.assertEqual(plugin.should_skip("group:g1", "group", "g1", "今天天气不错啊"), "已关闭")

    def test_empty_text(self):
        self.assertEqual(self.make().should_skip("group:g1", "group", "g1", ""), "无内容")

    def test_private_blocked_by_default(self):
        self.assertEqual(self.make().should_skip("private:1", "private", "1", "今天天气不错"), "私聊不判")

    def test_private_allowed_when_enabled(self):
        plugin = self.make(allowPrivate=True)
        self.assertIsNone(plugin.should_skip("private:1", "private", "1", "今天天气不错"))

    def test_deny_and_only_lists(self):
        self.assertEqual(self.make(denyGroups="g1").should_skip("group:g1", "group", "g1", "今天天气不错"), "在黑名单群")
        self.assertEqual(self.make(onlyGroups="g2").should_skip("group:g1", "group", "g1", "今天天气不错"), "不在白名单群")
        self.assertIsNone(self.make(onlyGroups="g1").should_skip("group:g1", "group", "g1", "今天天气不错"))

    def test_poke_is_skipped(self):
        self.assertEqual(self.make().should_skip("group:g1", "group", "g1", "[拍一拍] 拍了拍你"), "拍一拍")

    def test_too_short(self):
        self.assertEqual(self.make(minChars=10).should_skip("group:g1", "group", "g1", "在吗"), "消息过短")

    def test_media_only_placeholder(self):
        self.assertEqual(self.make().should_skip("group:g1", "group", "g1", "[图片:abc]"), "纯媒体")

    def test_long_media_placeholder_is_not_media_only(self):
        text = "[图片:abc]" + "这是一段很长的正文说明超过十二个字"
        self.assertIsNone(self.make().should_skip("group:g1", "group", "g1", text))

    def test_cooldown_and_duplicate(self):
        plugin = self.make(cooldownSec=60)
        now = 1000.0
        plugin._reserve("group:g1", "今天天气不错啊", now)
        self.assertEqual(plugin.should_skip("group:g1", "group", "g1", "今天天气不错啊", now + 1), "冷却中")
        # 冷却过后，同样的文本算重复
        self.assertEqual(plugin.should_skip("group:g1", "group", "g1", "今天天气不错啊", now + 120), "重复内容")
        # 冷却过后换一句新话可以判
        self.assertIsNone(plugin.should_skip("group:g1", "group", "g1", "另一句新话了", now + 120))

    def test_daily_limit(self):
        plugin = self.make(dailyCallLimit=1, cooldownSec=0)
        now = 1000.0
        plugin._reserve("group:g1", "第一句话", now)
        self.assertEqual(plugin.should_skip("group:g1", "group", "g1", "第二句话", now + 100), "已达当日上限")

    def test_probability_zero_skips(self):
        plugin = self.make(judgeProbability=0, cooldownSec=0)
        self.assertEqual(plugin.should_skip("group:g1", "group", "g1", "今天天气不错啊"), "未命中采样")


class FakeAI:
    def __init__(self, raw="", configured=True, raises=None, delay=0.0):
        self.raw = raw
        self.configured = configured
        self.raises = raises
        self.delay = delay
        self.calls = []

    async def chat(self, messages, system_prompt="", temperature=None, max_tokens=None):
        self.calls.append({"messages": messages, "system_prompt": system_prompt,
                           "temperature": temperature, "max_tokens": max_tokens})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.raw


class FakeRuntime:
    def __init__(self, ai):
        self.ai = ai
        self.recent_messages = []


class JudgeDecisionTests(unittest.TestCase):
    """失败方向与决策落点（不发真实请求）。"""

    def make(self, **settings):
        return ContextChime(FakeManager(settings))

    def judge(self, plugin, ai):
        return plugin.on_pre_ai(group_msg(), [], FakeRuntime(ai))

    def test_should_reply_allows(self):
        plugin = self.make(judgeProbability=100)
        ai = FakeAI('{"reply":1,"confidence":90}')
        self.assertIsNone(self.judge(plugin, ai))
        self.assertEqual(ai.calls[0]["temperature"], 0.0)

    def test_should_not_reply_blocks(self):
        plugin = self.make()
        ai = FakeAI('{"reply":0,"confidence":10}')
        self.assertEqual(self.judge(plugin, ai), {"block": True})

    def test_confidence_threshold_blocks(self):
        plugin = self.make(confidenceThreshold=95)
        ai = FakeAI('{"reply":1,"confidence":60}')
        self.assertEqual(self.judge(plugin, ai), {"block": True})

    def test_failure_blocks_by_default(self):
        # 与 JS 版一致：宁可少接，不可乱接
        plugin = self.make()
        ai = FakeAI(raises=RuntimeError("boom"))
        self.assertEqual(self.judge(plugin, ai), {"block": True})

    def test_failure_can_be_told_to_allow(self):
        plugin = self.make(blockOnJudgeFailure=False)
        ai = FakeAI(raises=RuntimeError("boom"))
        self.assertIsNone(self.judge(plugin, ai))

    def test_unparseable_output_blocks_by_default(self):
        plugin = self.make()
        self.assertEqual(self.judge(plugin, FakeAI("随便说了点别的")), {"block": True})

    def test_timeout_does_not_block_by_default(self):
        # 判定模型慢时仍要让机器人可用
        plugin = self.make(timeoutMs=500, cooldownSec=0)
        ai = FakeAI('{"reply":0}', delay=0.01, raises=TimeoutError("timeout"))
        self.assertIsNone(self.judge(plugin, ai))

    def test_timeout_can_block(self):
        plugin = self.make(timeoutMs=500, blockOnTimeout=True)
        ai = FakeAI(raises=TimeoutError("timeout"))
        self.assertEqual(self.judge(plugin, ai), {"block": True})

    def test_no_model_config_blocks_by_default(self):
        plugin = self.make()
        self.assertEqual(self.judge(plugin, FakeAI(configured=False)), {"block": True})

    def test_at_me_is_never_judged(self):
        plugin = self.make()
        message = group_msg()
        message["at"] = True
        ai = FakeAI('{"reply":0}')
        self.assertIsNone(plugin.on_pre_ai(message, [], FakeRuntime(ai)))
        self.assertEqual(ai.calls, [])

    def test_notice_event_is_ignored(self):
        plugin = self.make()
        message = group_msg()
        message["notice"] = "poke"
        ai = FakeAI('{"reply":0}')
        self.assertIsNone(plugin.on_pre_ai(message, [], FakeRuntime(ai)))
        self.assertEqual(ai.calls, [])

    def test_disabled_plugin_never_calls_model(self):
        plugin = ContextChime(FakeManager({"enabled": False}))
        ai = FakeAI('{"reply":0}')
        self.assertIsNone(self.judge(plugin, ai))
        self.assertEqual(ai.calls, [])

    def test_history_records_decision(self):
        plugin = self.make()
        self.judge(plugin, FakeAI('{"reply":0,"confidence":10,"why":"无关"}'))
        self.assertEqual(len(plugin.history), 1)
        self.assertFalse(plugin.history[0]["respond"])

    def test_max_tokens_setting_is_passed(self):
        plugin = self.make(maxTokens=32)
        ai = FakeAI('{"reply":1,"confidence":50}')
        self.judge(plugin, ai)
        self.assertEqual(ai.calls[0]["max_tokens"], 32)


class TodayKeyTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(today_key(datetime(2026, 3, 7)), "2026-03-07")


if __name__ == "__main__":
    unittest.main()
