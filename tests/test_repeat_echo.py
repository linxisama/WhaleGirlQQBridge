"""repeat_echo（群聊复读机）的离线测试。

核心审核修正的两点：
1. **媒体占位不复读**。传输层保留媒体段之后（`qq_transports.MEDIA_SEGMENTS`），
   多人发表情包会得到相同的占位文本（`[表情:微笑]`），复读它就是刷屏 ——
   这是核心改动引入的回归，必须挡住。查不到名字的 `[表情99999]` 同样要挡。
2. **通知事件不参与**。拍一拍/撤回/入群这些 `notice` 事件不是聊天内容。
"""

import pathlib
import tempfile
import time
import unittest

from src.plugins.repeat_echo import MEDIA_PLACEHOLDER_RE, RepeatEcho, is_echoable


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(RepeatEcho.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def group_msg(text, sender_id="7", target="12345"):
    return {"id": "m", "kind": "group", "target": target, "text": text,
            "sender_id": sender_id, "sender": "某人", "media": []}


class IsEchoableTests(unittest.TestCase):
    def test_normal_text_is_echoable(self):
        for text in ("你好", "哈哈哈哈", "。。。", "草", "[不是占位]"):
            with self.subTest(text=text):
                self.assertTrue(is_echoable(text))

    def test_media_placeholders_are_not_echoable(self):
        for text in ("[图片]", "[表情:微笑]", "[表情14]", "[表情99999]", "[表情]",
                     "[语音]", "[视频]", "[文件:a.zip]", "[合并转发]", "[卡片消息:标题]"):
            with self.subTest(text=text):
                self.assertFalse(is_echoable(text), f"{text} 不该被复读")

    def test_empty_is_not_echoable(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                self.assertFalse(is_echoable(text))

    def test_placeholder_with_surrounding_text_is_echoable(self):
        # "看图[图片]" 是有人打了字又发图，这算说话
        self.assertTrue(is_echoable("看图[图片]"))

    def test_regex_matches_transport_placeholders(self):
        # 与 qq_transports 的渲染保持同一口径
        from src.qq_transports import onebot_message
        message = onebot_message({
            "post_type": "message", "message_type": "group", "group_id": 1,
            "message_id": 9, "user_id": 7, "sender": {"nickname": "小明"},
            "message": [{"type": "face", "data": {"id": 14}}]}, "999")
        self.assertFalse(is_echoable(message["text"]))


class RepeatEchoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        settings.setdefault("enabled", True)
        settings.setdefault("cooldownSec", 0)
        settings.setdefault("sameTextBlockMin", 0)
        return RepeatEcho(FakeManager(self.dir, settings))

    def test_echoes_when_two_people_say_same_text(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest(group_msg("你好", "7"), None))
        self.assertEqual(plugin.on_ingest(group_msg("你好", "8"), None), "你好")

    def test_same_person_repeating_does_not_echo(self):
        plugin = self.make()
        plugin.on_ingest(group_msg("你好", "7"), None)
        self.assertIsNone(plugin.on_ingest(group_msg("你好", "7"), None))

    def test_media_placeholder_does_not_echo(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest(group_msg("[表情:微笑]", "7"), None))
        self.assertIsNone(plugin.on_ingest(group_msg("[表情:微笑]", "8"), None))

    def test_unknown_face_placeholder_does_not_echo(self):
        plugin = self.make()
        plugin.on_ingest(group_msg("[表情99999]", "7"), None)
        self.assertIsNone(plugin.on_ingest(group_msg("[表情99999]", "8"), None))

    def test_notice_events_are_ignored(self):
        plugin = self.make()
        notice = group_msg("")
        notice["notice"] = "poke"
        self.assertIsNone(plugin.on_ingest(notice, None))

    def test_private_chat_not_echoed(self):
        plugin = self.make()
        message = group_msg("你好", "7")
        message["kind"] = "private"
        plugin.on_ingest(message, None)
        message2 = group_msg("你好", "8")
        message2["kind"] = "private"
        self.assertIsNone(plugin.on_ingest(message2, None))

    def test_max_len_blocks_long_text(self):
        plugin = self.make(maxLen=4)
        plugin.on_ingest(group_msg("很长的一句话", "7"), None)
        self.assertIsNone(plugin.on_ingest(group_msg("很长的一句话", "8"), None))

    def test_cooldown_blocks_second_echo(self):
        plugin = RepeatEcho(FakeManager(self.dir, {"enabled": True, "cooldownSec": 3600,
                                                   "sameTextBlockMin": 0}))
        plugin.on_ingest(group_msg("甲", "7"), None)
        self.assertEqual(plugin.on_ingest(group_msg("甲", "8"), None), "甲")
        plugin.on_ingest(group_msg("乙", "7"), None)
        self.assertIsNone(plugin.on_ingest(group_msg("乙", "8"), None))

    def test_same_text_block(self):
        plugin = RepeatEcho(FakeManager(self.dir, {"enabled": True, "cooldownSec": 0,
                                                   "sameTextBlockMin": 30}))
        plugin.on_ingest(group_msg("甲", "7"), None)
        self.assertEqual(plugin.on_ingest(group_msg("甲", "8"), None), "甲")
        plugin.on_ingest(group_msg("甲", "9"), None)
        self.assertIsNone(plugin.on_ingest(group_msg("甲", "10"), None))

    def test_window_quota(self):
        plugin = RepeatEcho(FakeManager(self.dir, {"enabled": True, "cooldownSec": 0,
                                                   "sameTextBlockMin": 0, "maxPerWindow": 1}))
        plugin.on_ingest(group_msg("甲", "7"), None)
        self.assertEqual(plugin.on_ingest(group_msg("甲", "8"), None), "甲")
        plugin.on_ingest(group_msg("乙", "7"), None)
        self.assertIsNone(plugin.on_ingest(group_msg("乙", "8"), None))

    def test_pair_window_expiry(self):
        plugin = self.make(pairWindowSec=10)
        plugin.on_ingest(group_msg("你好", "7"), None)
        # 手动把历史时间往前推，模拟窗口过期
        plugin._history["12345"] = [(t, s, ts - 3600) for t, s, ts in plugin._history["12345"]]
        self.assertIsNone(plugin.on_ingest(group_msg("你好", "8"), None))

    def test_only_when_idle_skips_when_active(self):
        plugin = self.make(onlyWhenIdle=True, pairWindowSec=180)
        plugin.on_ingest(group_msg("你好", "7"), None)
        # 紧接着的第二条：还"不安静"，即使文本相同也不复读
        self.assertIsNone(plugin.on_ingest(group_msg("你好", "8"), None))

    def test_only_when_idle_echoes_after_quiet(self):
        plugin = self.make(onlyWhenIdle=True, pairWindowSec=180)
        plugin.on_ingest(group_msg("你好", "7"), None)
        plugin._last_activity["12345"] = time.time() - 3600   # 假装安静了一小时
        self.assertEqual(plugin.on_ingest(group_msg("你好", "8"), None), "你好")

    def test_disabled_returns_none(self):
        plugin = self.make(enabled=False)
        plugin.on_ingest(group_msg("你好", "7"), None)
        self.assertIsNone(plugin.on_ingest(group_msg("你好", "8"), None))

    def test_documented_media_rule(self):
        self.assertIn("媒体", RepeatEcho.META.description)
        self.assertIn("onlyWhenIdle", RepeatEcho.META.schema_dict())


if __name__ == "__main__":
    unittest.main()
