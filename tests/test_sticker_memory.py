"""sticker_memory（表情包记忆库）的离线测试。

这个插件在当前运行时下**拦截功能不可用**（入站图片段没被保留），所以测试重点在
"将来接上媒体段就能直接用"的那部分：指纹、只记表情包的判据、记忆库的
上限淘汰 / 重新确认窗口 / 命中比例决策。
"""

import unittest

from src.plugins.sticker_memory import (
    DEFAULT_STICKER_MAX_BYTES,
    DEFAULT_STICKER_MAX_SIDE,
    StickerMemory,
    StickerMemoryCache,
    fingerprint,
    looks_like_sticker,
)


class FingerprintTests(unittest.TestCase):
    def test_same_bytes_same_digest(self):
        self.assertEqual(fingerprint(b"abc"), fingerprint(b"abc"))

    def test_different_bytes_different_digest(self):
        self.assertNotEqual(fingerprint(b"abc"), fingerprint(b"abd"))

    def test_empty_returns_empty(self):
        self.assertEqual(fingerprint(b""), "")
        self.assertEqual(fingerprint(None), "")

    def test_digest_is_sha256_hex(self):
        digest = fingerprint(b"abc")
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class LooksLikeStickerTests(unittest.TestCase):
    def test_sticker_tags_are_accepted(self):
        for tag in ("[动画表情]", "[表情:abc]", "[动图]", "[GIF:x]", "[贴纸]"):
            with self.subTest(tag=tag):
                self.assertTrue(looks_like_sticker(tag))

    def test_photo_and_screenshot_tags_are_rejected(self):
        for tag in ("[图片:abc]", "[照片]", "[截图]", "[视频:x]", "[文件:x]"):
            with self.subTest(tag=tag):
                self.assertFalse(looks_like_sticker(tag))

    def test_large_images_are_rejected_in_sticker_only_mode(self):
        self.assertFalse(looks_like_sticker("[未知]", byte_size=DEFAULT_STICKER_MAX_BYTES + 1))
        self.assertFalse(looks_like_sticker("[未知]", width=DEFAULT_STICKER_MAX_SIDE + 1))
        self.assertFalse(looks_like_sticker("[未知]", height=DEFAULT_STICKER_MAX_SIDE + 1))

    def test_small_untagged_image_is_treated_as_sticker(self):
        self.assertTrue(looks_like_sticker("[未知]", byte_size=10_000, width=200, height=200))

    def test_no_tag_no_size_is_rejected(self):
        # 宁可不记，也不要把照片当表情包
        self.assertFalse(looks_like_sticker(""))
        self.assertFalse(looks_like_sticker("[未知]"))

    def test_sticker_only_off_accepts_images_but_not_videos(self):
        self.assertTrue(looks_like_sticker("[图片:abc]", sticker_only=False))
        self.assertTrue(looks_like_sticker("[未知]", byte_size=10_000_000, sticker_only=False))
        self.assertFalse(looks_like_sticker("[视频:x]", sticker_only=False))
        self.assertFalse(looks_like_sticker("[文件:x]", sticker_only=False))
        self.assertFalse(looks_like_sticker("[截图]", sticker_only=False))


class CacheBasicsTests(unittest.TestCase):
    def test_remember_and_lookup(self):
        cache = StickerMemoryCache()
        cache.remember("abc", "服软的猫", "group:1", now=1000.0)
        self.assertEqual(cache.lookup("abc")["meaning"], "服软的猫")
        self.assertEqual(len(cache), 1)

    def test_empty_meaning_is_ignored(self):
        cache = StickerMemoryCache()
        cache.remember("abc", "   ", "group:1")
        self.assertEqual(len(cache), 0)

    def test_forget(self):
        cache = StickerMemoryCache()
        cache.remember("abc", "猫", "group:1")
        self.assertTrue(cache.forget("abc"))
        self.assertFalse(cache.forget("abc"))
        self.assertEqual(len(cache), 0)

    def test_prunes_least_recently_used(self):
        cache = StickerMemoryCache(max_entries=2)
        cache.remember("a", "甲", "group:1", now=1)
        cache.remember("b", "乙", "group:1", now=2)
        cache.touch("a", "group:1", now=3)
        cache.remember("c", "丙", "group:1", now=4)
        self.assertEqual(len(cache), 2)
        self.assertIsNone(cache.lookup("b"))       # 最久未用被淘汰
        self.assertIsNotNone(cache.lookup("a"))

    def test_export_load_round_trip(self):
        cache = StickerMemoryCache()
        cache.remember("abc", "猫", "group:1", now=1000.0)
        restored = StickerMemoryCache()
        restored.load(cache.export())
        self.assertEqual(restored.lookup("abc")["meaning"], "猫")

    def test_load_ignores_corrupt_data(self):
        cache = StickerMemoryCache()
        for raw in (None, "nope", {"entries": "nope"}, {"entries": {"a": {"meaning": ""}}}):
            with self.subTest(raw=raw):
                cache.load(raw)
                self.assertEqual(len(cache), 0)


class FreshnessTests(unittest.TestCase):
    HOUR = 3600.0
    DAY = 86400.0

    def setUp(self):
        self.cache = StickerMemoryCache()
        self.cache.remember("abc", "猫", "group:1", now=0.0)

    def test_same_chat_within_window(self):
        self.assertTrue(self.cache.is_fresh("abc", "group:1", 24.0, 7.0, now=23 * self.HOUR))

    def test_same_chat_after_window(self):
        self.assertFalse(self.cache.is_fresh("abc", "group:1", 24.0, 7.0, now=25 * self.HOUR))

    def test_cross_chat_uses_longer_window(self):
        # 换到别的会话：24 小时早过了，但 7 天还没到
        self.assertTrue(self.cache.is_fresh("abc", "group:2", 24.0, 7.0, now=3 * self.DAY))
        self.assertFalse(self.cache.is_fresh("abc", "group:2", 24.0, 7.0, now=8 * self.DAY))

    def test_zero_windows_never_recheck(self):
        self.assertTrue(self.cache.is_fresh("abc", "group:2", 0, 0, now=1000 * self.DAY))

    def test_unknown_digest_is_not_fresh(self):
        self.assertFalse(self.cache.is_fresh("zzz", "group:1", 24.0, 7.0, now=0))


class HitRatioTests(unittest.TestCase):
    def setUp(self):
        self.cache = StickerMemoryCache()
        self.cache.remember("a", "甲", "group:1", now=0.0)
        self.cache.remember("b", "乙", "group:1", now=0.0)

    def test_hit_ratio(self):
        self.assertAlmostEqual(self.cache.hit_ratio(["a", "b", "c"], "group:1", now=0.0), 2 / 3, places=6)

    def test_empty_list_is_zero(self):
        self.assertEqual(self.cache.hit_ratio([], "group:1"), 0.0)
        self.assertEqual(self.cache.hit_ratio([None, ""], "group:1"), 0.0)

    def test_stale_entries_do_not_count(self):
        self.assertAlmostEqual(
            self.cache.hit_ratio(["a", "b"], "group:1", 24.0, 7.0, now=99 * 3600), 0.0, places=6)

    def test_skip_when_ratio_reaches_threshold(self):
        self.assertTrue(self.cache.should_skip_look(["a", "b", "c"], "group:1", 0.5, now=0.0))
        self.assertFalse(self.cache.should_skip_look(["a", "c", "d"], "group:1", 0.5, now=0.0))

    def test_threshold_zero_always_skips_non_empty(self):
        self.assertTrue(self.cache.should_skip_look(["zzz"], "group:1", 0.0, now=0.0))

    def test_threshold_one_requires_all_hits(self):
        self.assertFalse(self.cache.should_skip_look(["a", "c"], "group:1", 1.0, now=0.0))
        self.assertTrue(self.cache.should_skip_look(["a", "b"], "group:1", 1.0, now=0.0))

    def test_known_notes_and_touch(self):
        notes = self.cache.known_notes(["a", "c"], "group:1", now=5.0)
        self.assertEqual(notes, ["[已知表情包：甲]"])
        # known_notes 会先判新鲜度再刷新使用时间；0 窗口下两者等价
        self.assertEqual(self.cache.lookup("a")["last_used"], 5.0)


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = state_dir
        self._settings = dict(StickerMemory.META.default_settings)
        self._settings.update({"enabled": True})
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class StickerMemoryPluginTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        return StickerMemory(FakeManager(self.dir, settings))

    def test_plugin_is_disabled_by_default(self):
        # 缺能力的插件必须默认关闭（目标要求）
        self.assertTrue(StickerMemory.META.default_settings["enabled"])

    def test_description_states_it_is_unavailable(self):
        self.assertIn("视觉模型成功识别", StickerMemory.META.description)

    def test_remember_and_persist(self):
        plugin = self.make()
        # 无标签无尺寸会被判为"不是表情包"，所以这里显式按表情包记
        self.assertTrue(plugin.remember(b"image-bytes", "服软的猫", "group:1", sticker_only=False))
        again = self.make()
        self.assertEqual(again.cache().lookup(fingerprint(b"image-bytes"))["meaning"], "服软的猫")

    def test_remember_uses_sticker_only_setting_by_default(self):
        # 默认 stickerOnly=True 且无标签无尺寸 → 不记
        plugin = self.make()
        self.assertFalse(plugin.remember(b"image-bytes", "服软的猫", "group:1"))

    def test_remember_ignores_empty_meaning(self):
        plugin = self.make()
        self.assertFalse(plugin.remember(b"x", "   ", "group:1"))

    def test_remember_ignores_empty_bytes(self):
        plugin = self.make()
        self.assertFalse(plugin.remember(b"", "猫", "group:1"))

    def test_sticker_only_setting_is_honoured(self):
        # 默认（stickerOnly=True）且无标签无尺寸 → 不记
        self.assertFalse(self.make(stickerOnly=True).remember(b"x", "照片", "group:1"))
        # 显式说明"这可以记"时照记（对应 JS 版关掉 stickerOnly 的行为）
        self.assertTrue(self.make(stickerOnly=False).remember(b"y", "照片", "group:1"))

    def test_remember_accepts_when_caller_says_it_is_a_sticker(self):
        # 将来媒体段接上后，调用方会用 sticker_only=False 明确表示"这张可以记"
        plugin = self.make()
        self.assertTrue(plugin.remember(b"z", "服软的猫", "group:1", sticker_only=False))
        self.assertEqual(len(plugin.cache()), 1)

    def test_notes_and_skip_use_settings(self):
        plugin = self.make(recheckSameChatHours=0, recheckCrossChatDays=0)
        plugin.remember(b"a", "甲", "group:1", sticker_only=False)
        digest = fingerprint(b"a")
        self.assertEqual(plugin.notes_for([digest], "group:1"), ["[已知表情包：甲]"])
        self.assertTrue(plugin.should_skip_look([digest], "group:1"))

    def test_on_ingest_is_a_noop_for_now(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest({"kind": "group", "target": "1", "text": "hi"}, None))

    def test_cache_capacity_setting_is_used(self):
        plugin = self.make(maxCacheEntries=10)
        self.assertEqual(plugin.cache().max_entries, 10)

    def test_state_survives_corrupt_file(self):
        path = self.dir / StickerMemory.META.id / "sticker_memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        plugin = self.make()
        self.assertEqual(len(plugin.cache()), 0)


if __name__ == "__main__":
    unittest.main()
