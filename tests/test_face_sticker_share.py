"""face_decode / sticker_cap / share_card 三个插件的离线测试。

三个都属于"照实实现但标注不可用或部分不可用"：
- face_decode：**可用**（表 + 翻译兜底）；
- sticker_cap：看门与清理可用，**自动收表情不可用**（本项目没有表情库）；
- share_card：卡片元数据与链接识别可用，**联网读网页与 OCR 不可用**。

所以除了功能测试，每个插件都有一条"必须在说明里写清不可用点 + 默认关闭"的断言，
防止以后有人把降级说明删掉、让用户以为功能是全的。
"""

import json
import pathlib
import tempfile
import unittest

from src.plugins.face_decode import (
    FACE_NAMES,
    FaceDecode,
    translate_face_tags,
    translate_message_content,
)
from src.plugins.share_card import (
    ShareCard,
    card_text,
    extract_urls,
    platform_of,
    tidy_url,
)
from src.plugins.sticker_cap import (
    StickerCap,
    dir_usage,
    parse_id_list,
    pick_evictions,
)


class FakeManager:
    def __init__(self, plugin_cls, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(plugin_cls.META.default_settings)
        self._settings.update({"enabled": True})
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class FaceNamesDataTests(unittest.TestCase):
    def test_table_is_populated(self):
        self.assertGreaterEqual(len(FACE_NAMES), 200)

    def test_known_core_faces(self):
        # 这几个是最经典的表情，名字错了说明表被改坏
        self.assertEqual(FACE_NAMES[14], "微笑")
        self.assertEqual(FACE_NAMES[0], "惊讶")
        self.assertEqual(FACE_NAMES[5], "流泪")

    def test_all_keys_are_int_and_values_non_empty(self):
        for key, value in FACE_NAMES.items():
            self.assertIsInstance(key, int)
            self.assertTrue(str(value).strip())


class FaceDecodeTests(unittest.TestCase):
    def test_translate_known_id(self):
        self.assertEqual(translate_face_tags("你好[表情14]"), "你好[表情:微笑]")

    def test_unknown_id_kept_as_is(self):
        self.assertEqual(translate_face_tags("[表情99999]"), "[表情99999]")

    def test_already_translated_is_idempotent(self):
        self.assertEqual(translate_face_tags("[表情:微笑]"), "[表情:微笑]")

    def test_multiple_and_mixed(self):
        self.assertEqual(translate_face_tags("[表情14]x[表情0]"),
                         "[表情:微笑]x[表情:惊讶]")

    def test_no_face_returns_input(self):
        self.assertEqual(translate_face_tags("普通消息"), "普通消息")
        self.assertEqual(translate_face_tags(""), "")
        self.assertEqual(translate_face_tags(None), "")

    def test_translate_message_content_string(self):
        message = {"role": "user", "content": "[表情14]"}
        self.assertTrue(translate_message_content(message))
        self.assertEqual(message["content"], "[表情:微笑]")

    def test_translate_message_content_parts(self):
        message = {"role": "user", "content": [{"type": "text", "text": "[表情14]"}]}
        self.assertTrue(translate_message_content(message))
        self.assertEqual(message["content"][0]["text"], "[表情:微笑]")

    def test_translate_message_content_unchanged(self):
        message = {"role": "user", "content": "没有表情"}
        self.assertFalse(translate_message_content(message))
        self.assertFalse(translate_message_content(None))
        self.assertFalse(translate_message_content({"role": "user", "content": 42}))

    def test_on_pre_ai_translates_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = FaceDecode(FakeManager(FaceDecode, tmp))
            conversation = [{"role": "user", "content": "[表情14]"},
                            {"role": "assistant", "content": "[表情14]"}]
            self.assertIsNone(plugin.on_pre_ai({}, conversation, None))
            self.assertEqual(conversation[0]["content"], "[表情:微笑]")
            # 默认不动 assistant
            self.assertEqual(conversation[1]["content"], "[表情14]")

    def test_disabled_plugin_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = FaceDecode(FakeManager(FaceDecode, tmp, {"enabled": False}))
            conversation = [{"role": "user", "content": "[表情14]"}]
            plugin.on_pre_ai({}, conversation, None)
            self.assertEqual(conversation[0]["content"], "[表情14]")

    def test_bad_conversation_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = FaceDecode(FakeManager(FaceDecode, tmp))
            self.assertIsNone(plugin.on_pre_ai({}, None, None))
            self.assertIsNone(plugin.on_pre_ai({}, ["不是字典"], None))

    def test_enabled_by_default_and_faces_documented(self):
        self.assertTrue(FaceDecode.META.default_settings["enabled"])
        self.assertIn("查不到", FaceDecode.META.description)


class StickerCapTests(unittest.TestCase):
    def test_parse_id_list(self):
        self.assertEqual(parse_id_list("123, 456，789"), {"123", "456", "789"})
        self.assertEqual(parse_id_list(""), set())
        self.assertEqual(parse_id_list(None), set())

    def test_dir_usage_missing_dir(self):
        self.assertEqual(dir_usage(pathlib.Path("Z:/definitely/not/here")), (0, 0))

    def test_dir_usage_counts_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.bin").write_bytes(b"x" * 10)
            (root / "sub").mkdir()
            (root / "sub" / "b.bin").write_bytes(b"y" * 5)
            count, total = dir_usage(root)
            self.assertEqual(count, 2)
            self.assertEqual(total, 15)

    def test_pick_evictions_removes_oldest(self):
        entries = [{"name": "old", "used_at": 1}, {"name": "mid", "used_at": 2},
                   {"name": "new", "used_at": 3}]
        self.assertEqual([e["name"] for e in pick_evictions(entries, 2)], ["old"])

    def test_pick_evictions_no_limit(self):
        entries = [{"name": "a", "used_at": 1}]
        self.assertEqual(pick_evictions(entries, 0), [])
        self.assertEqual(pick_evictions(entries, 5), [])

    def test_enforce_dry_run_does_not_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            stickers = root / "src"          # 造一个"项目根"让目录解析稳定
            stickers.mkdir()
            lib = root / "data" / "qq-only" / "stickers"
            lib.mkdir(parents=True)
            for index in range(4):
                (lib / f"s{index}.bin").write_bytes(b"x" * 100)
            plugin = StickerCap(FakeManager(StickerCap, root / "data" / "qq-only",
                                            {"stickerDir": "data/qq-only/stickers",
                                             "maxItems": 2, "dryRun": True}))
            report = plugin.enforce()
            self.assertTrue(report["dryRun"])
            self.assertEqual(report["removed"], 2)
            self.assertEqual(len(list(lib.iterdir())), 4)      # 一个都没删

    def test_enforce_real_run_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            lib = root / "data" / "qq-only" / "stickers"
            lib.mkdir(parents=True)
            for index in range(4):
                (lib / f"s{index}.bin").write_bytes(b"x" * 100)
            plugin = StickerCap(FakeManager(StickerCap, root / "data" / "qq-only",
                                            {"stickerDir": "data/qq-only/stickers",
                                             "maxItems": 2, "dryRun": False}))
            report = plugin.enforce()
            self.assertEqual(report["removed"], 2)
            self.assertEqual(len(list(lib.iterdir())), 2)

    def test_enforce_without_directory_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = StickerCap(FakeManager(StickerCap, tmp, {"dryRun": False}))
            report = plugin.enforce()
            self.assertEqual(report["removed"], 0)
            self.assertIn("不存在", report["reason"])

    def test_blocked_senders(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = StickerCap(FakeManager(StickerCap, tmp, {"blockedSenders": "123,456"}))
            self.assertTrue(plugin.blocked("123"))
            self.assertFalse(plugin.blocked("999"))
            self.assertFalse(plugin.blocked(None))

    def test_on_ingest_stores_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = StickerCap(FakeManager(StickerCap, tmp))
            self.assertIsNone(plugin.on_ingest({"media": [{"type": "image"}]}, None))

    def test_disabled_by_default_and_unavailable_documented(self):
        self.assertFalse(StickerCap.META.default_settings["enabled"])
        self.assertIn("实际收藏", StickerCap.META.description)
        self.assertIn("收集由", StickerCap.META.description)


class ShareCardTests(unittest.TestCase):
    def test_tidy_url_strips_punctuation(self):
        self.assertEqual(tidy_url("https://x.com/a。"), "https://x.com/a")
        self.assertEqual(tidy_url("https://x.com/a）"), "https://x.com/a")

    def test_extract_urls_dedupes_and_keeps_order(self):
        text = "第一 https://b.com/1 第二 https://a.com/2 又 https://b.com/1"
        self.assertEqual(extract_urls(text), ["https://b.com/1", "https://a.com/2"])

    def test_extract_urls_empty(self):
        self.assertEqual(extract_urls(""), [])
        self.assertEqual(extract_urls(None), [])

    def test_platform_of(self):
        self.assertEqual(platform_of("https://www.bilibili.com/video/BV1"), "B站")
        self.assertEqual(platform_of("https://b23.tv/abc"), "B站")
        self.assertEqual(platform_of("https://www.xiaohongshu.com/x"), "小红书")
        self.assertEqual(platform_of("https://example.com"), "")
        self.assertEqual(platform_of(""), "")

    def test_card_text_from_json_wrapper(self):
        card = {"type": "json", "card": json.dumps({
            "app": "com.tencent.structmsg",
            "meta": {"detail_1": {"title": "某视频", "desc": "简介",
                                  "url": "https://www.bilibili.com/video/BV1"}},
        })}
        text = card_text(card)
        self.assertIn("某视频", text)
        self.assertIn("B站", text)
        self.assertIn("简介", text)

    def test_card_text_from_xml_like_text(self):
        card = {"type": "xml", "card": '<msg title="标题A" desc="描述B" url="https://a.com/x"/>'}
        text = card_text(card)
        self.assertIn("标题A", text)
        self.assertIn("描述B", text)

    def test_card_text_garbage_is_empty(self):
        for bad in (None, {}, {"type": "json", "card": "not json"}, {"type": "json"}):
            with self.subTest(bad=bad):
                self.assertEqual(card_text(bad), "")

    def test_note_includes_card_and_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ShareCard(FakeManager(ShareCard, tmp))
            message = {"text": "看看 https://b23.tv/xyz",
                       "media": [{"type": "json", "card": json.dumps(
                           {"meta": {"detail_1": {"title": "标题", "url": "https://b23.tv/xyz"}}})}]}
            note = plugin.note_for(message)
            self.assertIn("标题", note)
            self.assertIn("B站", note)

    def test_same_link_is_not_repeated_within_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ShareCard(FakeManager(ShareCard, tmp, {"cacheTtlSec": 600}))
            message = {"text": "https://a.com/x"}
            self.assertIn("a.com", plugin.note_for(message, now=1000.0))
            self.assertEqual(plugin.note_for(message, now=1001.0), "")

    def test_link_reappears_after_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ShareCard(FakeManager(ShareCard, tmp, {"cacheTtlSec": 60}))
            message = {"text": "https://a.com/x"}
            plugin.note_for(message, now=1000.0)
            self.assertIn("a.com", plugin.note_for(message, now=2000.0))

    def test_max_links_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ShareCard(FakeManager(ShareCard, tmp, {"maxLinks": 2, "cacheTtlSec": 0}))
            note = plugin.note_for({"text": "https://a.com/1 https://b.com/2 https://c.com/3"})
            self.assertIn("a.com", note)
            self.assertIn("b.com", note)
            self.assertNotIn("c.com", note)

    def test_disabled_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ShareCard(FakeManager(ShareCard, tmp, {"readCards": False,
                                                            "collectLinks": False}))
            self.assertEqual(plugin.note_for({"text": "https://a.com/x"}), "")

    def test_on_pre_ai_returns_extra_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ShareCard(FakeManager(ShareCard, tmp, {"cacheTtlSec": 0}))
            conversation=[]
            hints = plugin.on_pre_ai({"text": "看 https://b23.tv/a"}, conversation, None)
            self.assertIn("b23.tv/a", str(conversation))
            self.assertIn("不能声称读完", hints["extra_system"])

    def test_on_pre_ai_none_when_nothing_to_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ShareCard(FakeManager(ShareCard, tmp))
            self.assertIsNone(plugin.on_pre_ai({"text": "普通聊天"}, [], None))

    def test_disabled_by_default_and_limits_documented(self):
        self.assertFalse(ShareCard.META.default_settings["enabled"])
        self.assertIn("公开网页", ShareCard.META.description)
        self.assertFalse(ShareCard.META.default_settings["readWeb"])


if __name__ == "__main__":
    unittest.main()
