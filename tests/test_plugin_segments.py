"""消息段组装与插件元数据链路的离线回归测试。

背景：插件在 on_post_reply 里返回 (文本, 元数据) 时，元数据必须真的变成
OneBot 消息段（@ 提醒、图片），而不是被静默丢弃。这条链路跨越
manager（收集元数据）→ qq_app（_send_with_pacing 透传）→ transport（组装消息段），
任何一环断掉都会表现为"插件看起来生效了、对方却收不到提醒"。
"""

import asyncio
import json
import unittest

from src.plugin_segments import (
    ALLOWED_TYPES,
    build_reply_segments,
    normalize_mentions,
    strip_mentions_from_text,
)
from src.qq_app import _send_with_pacing


class ReplySegmentsTests(unittest.TestCase):
    def test_text_only_keeps_single_text_segment(self):
        self.assertEqual(
            build_reply_segments("你好", None),
            [{"type": "text", "data": {"text": "你好"}}],
        )
        self.assertEqual(
            build_reply_segments("你好", {}),
            [{"type": "text", "data": {"text": "你好"}}],
        )

    def test_mentions_go_before_text_by_default(self):
        segments = build_reply_segments("你说得对", {"mentions": ["12345"]})
        self.assertEqual(segments[0], {"type": "at", "data": {"qq": "12345"}})
        self.assertEqual(segments[1], {"type": "text", "data": {"text": "你说得对"}})

    def test_append_mentions_puts_at_last(self):
        segments = build_reply_segments("在的", {"mentions": ["12345"], "append_mentions": True})
        self.assertEqual(segments[0]["type"], "text")
        self.assertEqual(segments[1], {"type": "at", "data": {"qq": "12345"}})

    def test_mention_forms_are_normalized_and_deduplicated(self):
        mentions = normalize_mentions(["42", 42, {"qq": 42}, {"user_id": "7"}, None, ""])
        self.assertEqual([m["data"]["qq"] for m in mentions], ["42", "7"])

    def test_all_mention_is_kept_as_its_own_segment(self):
        # @全体成员 在 OneBot 里就是 qq="all"，插件可能显式要求
        segments = build_reply_segments("集合", {"mentions": ["all"]})
        self.assertEqual(segments[0], {"type": "at", "data": {"qq": "all"}})

    def test_media_segments_are_appended_in_order(self):
        meta = {"media": [
            {"type": "image", "data": {"file": "a.png"}},
            {"type": "face", "data": {"id": "1"}},
        ]}
        segments = build_reply_segments("看图", meta)
        self.assertEqual([s["type"] for s in segments], ["text", "image", "face"])

    def test_disallowed_media_types_are_ignored(self):
        meta = {"media": [
            {"type": "video", "data": {"file": "a.mp4"}},
            {"type": "image", "data": "not-a-dict"},
            "garbage",
        ]}
        segments = build_reply_segments("x", meta)
        self.assertEqual([s["type"] for s in segments], ["text"])
        self.assertNotIn("video", ALLOWED_TYPES)

    def test_empty_text_with_mentions_only(self):
        segments = build_reply_segments("", {"mentions": ["42"]})
        self.assertEqual(segments, [{"type": "at", "data": {"qq": "42"}}])

    def test_empty_text_and_no_meta_returns_no_segments(self):
        self.assertEqual(build_reply_segments("", None), [])

    def test_text_override_replaces_reply(self):
        segments = build_reply_segments("原文本", {"text": "新文本"})
        self.assertEqual(segments[0]["data"]["text"], "新文本")


class StripMentionsTests(unittest.TestCase):
    def test_strips_claimed_mention(self):
        self.assertEqual(strip_mentions_from_text("@泡影之梦 你说得对", ["泡影之梦"]), " 你说得对")

    def test_does_not_touch_other_mentions(self):
        self.assertEqual(strip_mentions_from_text("@a 和 @b", ["a"]), " 和 @b")

    def test_does_not_strip_inside_email_like_text(self):
        self.assertEqual(strip_mentions_from_text("foo@bar 你好", ["bar"]), "foo@bar 你好")

    def test_longest_name_first(self):
        text = "@小鲸鱼娘 你好"
        self.assertEqual(strip_mentions_from_text(text, ["鲸鱼娘", "小鲸鱼娘"]), " 你好")


class FakeTransportMeta:
    """同时记录是否收到 meta 的假 transport（支持第三参数）。"""

    def __init__(self):
        self.calls = []

    async def send(self, message, text, meta=None):
        self.calls.append((text, meta))


class FakeTransportNoMeta:
    """只接受两个参数的旧式 transport，用于验证向后兼容。"""

    def __init__(self):
        self.calls = []

    async def send(self, message, text):
        self.calls.append(text)


class MetaPassthroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_meta_is_passed_when_supported(self):
        transport = FakeTransportMeta()
        meta = {"mentions": ["42"]}
        await _send_with_pacing(transport, {"id": "m"}, "你好", meta)
        self.assertEqual(transport.calls, [("你好", meta)])

    async def test_meta_ignored_gracefully_when_unsupported(self):
        transport = FakeTransportNoMeta()
        await _send_with_pacing(transport, {"id": "m"}, "你好", {"mentions": ["42"]})
        self.assertEqual(transport.calls, ["你好"])

    async def test_no_meta_keeps_two_argument_call(self):
        transport = FakeTransportNoMeta()
        await _send_with_pacing(transport, {"id": "m"}, "你好", None)
        self.assertEqual(transport.calls, ["你好"])

    async def test_meta_only_on_first_chunk_when_split(self):
        transport = FakeTransportMeta()
        marker = "\x01PACE:10\x01"
        from unittest.mock import patch
        with patch("src.qq_app.asyncio.sleep", new=_noop_sleep):
            await _send_with_pacing(transport, {"id": "m"}, f"第一句{marker}第二句", {"mentions": ["42"]})
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][0], "第一句")
        self.assertIsNotNone(transport.calls[0][1])
        self.assertEqual(transport.calls[1][0], "第二句")
        self.assertIsNone(transport.calls[1][1])


async def _noop_sleep(_seconds):
    return None


class OneBotSegmentTests(unittest.IsolatedAsyncioTestCase):
    """真实传输层：确认 @ 段与媒体段真的进了 send_*_msg 的 message 数组。"""

    async def _capture(self, kind, text, meta):
        from src.qq_transports import OneBotQQ

        client = OneBotQQ()
        client.online = True
        captured = {}

        async def fake_call(action, params=None):
            captured["action"] = action
            captured["params"] = params
            return {}

        client.call = fake_call
        message = {"kind": kind, "target": "793502060" if kind == "group" else "42", "id": "m1"}
        await client.send(message, text, meta)
        return captured

    async def test_group_send_builds_at_and_text_segments(self):
        captured = await self._capture("group", "你说得对", {"mentions": ["12345"]})
        self.assertEqual(captured["action"], "send_group_msg")
        self.assertEqual(captured["params"]["group_id"], 793502060)
        self.assertEqual(captured["params"]["message"], [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": "你说得对"}},
        ])

    async def test_private_send_without_meta_is_plain_text(self):
        captured = await self._capture("private", "你好", None)
        self.assertEqual(captured["action"], "send_private_msg")
        self.assertEqual(captured["params"]["user_id"], 42)
        self.assertEqual(captured["params"]["message"], [{"type": "text", "data": {"text": "你好"}}])

    async def test_empty_reply_and_no_meta_sends_nothing(self):
        captured = await self._capture("group", "", None)
        self.assertEqual(captured, {})

    async def test_image_media_segment_reaches_wire_format(self):
        captured = await self._capture("group", "给你", {"media": [{"type": "image", "data": {"file": "https://x/y.png"}}]})
        self.assertEqual(captured["params"]["message"][1], {"type": "image", "data": {"file": "https://x/y.png"}})


class OfficialDegradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_send_accepts_meta_without_crashing(self):
        from src.qq_transports import OfficialQQ

        bridge = OfficialQQ("app", "secret")
        sent = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"id": "ok"}

            def raise_for_status(self):
                return None

        class FakeHttp:
            async def post(self, url, json=None, headers=None, **kwargs):
                sent["payload"] = json
                sent["timeout"] = kwargs.get("timeout")
                return FakeResponse()

        bridge.http = FakeHttp()

        async def fake_authorize(force=False):
            return "token"

        bridge.authorize = fake_authorize
        await bridge.send({"kind": "group", "target": "g", "id": "m1"}, "你好", {"mentions": ["42"]})
        # 官方接口只有文本：@ 被降级忽略，但消息必须照常发出
        self.assertEqual(sent["payload"]["content"], "你好")
        self.assertEqual(sent["payload"]["msg_seq"], 1)


if __name__ == "__main__":
    unittest.main()

class RecordSegmentTests(unittest.TestCase):
    def test_record_media_is_allowed_for_voice_reply(self):
        from src.plugin_segments import build_reply_segments
        segments = build_reply_segments('语音', {'media': [{'type': 'record',
            'data': {'file': 'C:/Temp/test.wav'}}]})
        self.assertEqual([s['type'] for s in segments], ['text', 'record'])
