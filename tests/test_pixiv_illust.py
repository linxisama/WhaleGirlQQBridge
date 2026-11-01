"""pixiv_illust（Pixiv 插图，离线逻辑）的离线测试。

本插件当前**未接入网络**（用户决定外部服务暂不接），所以测试只覆盖可独立验证的部分：
按会话分级、URL 模板构造与编码、候选筛选去重、CQ 转义、状态容量与持久化。

其中 PluginDiscoveryTests 是硬门槛：上一轮有交付自造 PluginBase 导致插件静默不可用，
这一轮原稿又在 import 期就崩（FieldSchema 不接受 default=、load_state 递归、
on_ingest 少参数），所以"能被真 PluginManager 发现"和"设置项能被基类读到"必须各有一条断言。
"""

import pathlib
import tempfile
import unittest

from src.plugins.pixiv_illust import (
    DEFAULT_IMAGE_TEMPLATE,
    STATE_FILE,
    PixivIllust,
    build_image_url,
    build_search_url,
    escape_cq_text,
    parse_ratings,
    pick_candidates,
    pick_image_index,
    rating_allowed,
)


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(PixivIllust.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class PluginDiscoveryTests(unittest.TestCase):
    def test_plugin_is_discoverable_by_plugin_manager(self):
        from src.plugins import PluginBase
        from src.plugins.manager import PluginManager

        self.assertTrue(issubclass(PixivIllust, PluginBase),
                        "必须继承 src.plugins.PluginBase，不能自造一个")
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(pathlib.Path(tmp))
            self.assertIn("pixiv_illust", manager._plugins)
            self.assertFalse(manager._plugins["pixiv_illust"].enabled)

    def test_meta_is_a_plugin_meta_with_all_schema_keys(self):
        from src.plugins import PluginMeta

        self.assertIsInstance(PixivIllust.META, PluginMeta)
        self.assertFalse(PixivIllust.META.enabled_by_default)
        schema = PixivIllust.META.schema_dict()
        for key in ("enabled", "searchBackend", "loliconApiUrl", "searchUrlTemplate",
                    "imageUrlTemplate", "cookie", "userAgent", "proxyUrl", "excludeAI",
                    "poolSize", "maxCount", "ratings", "allowR18", "ownerIds", "adminIds",
                    "stateCap", "retryCandidates", "deadTtlDays", "timeoutMs"):
            self.assertIn(key, schema)

    def test_every_schema_key_has_a_default_setting(self):
        # 基类的 self.get(key) 读的是 default_settings；schema 有键而默认值缺键会静默取 None
        defaults = PixivIllust.META.default_settings
        for key in PixivIllust.META.schema_dict():
            self.assertIn(key, defaults, f"{key} 缺少 default_settings")

    def test_enabled_by_default_is_false(self):
        self.assertFalse(PixivIllust.META.default_settings["enabled"])


class ParseRatingsTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_ratings("group:123=r18, private:456=sfw"),
                         {"group:123": "r18", "private:456": "sfw"})

    def test_case_insensitive(self):
        self.assertEqual(parse_ratings("group:1=SFW"), {"group:1": "sfw"})
        self.assertEqual(parse_ratings("group:1=R18"), {"group:1": "r18"})
        self.assertEqual(parse_ratings("group:1=All"), {"group:1": "all"})

    def test_chinese_comma_and_spaces(self):
        self.assertEqual(parse_ratings("group:1=r18，group:2=sfw group:3=all"),
                         {"group:1": "r18", "group:2": "sfw", "group:3": "all"})

    def test_invalid_entries_ignored(self):
        for raw in ("", None, "hello", "123=r18", "group:abc=r18", "group:1=x",
                    "group:1=", "=sfw", "group:1:2=sfw"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_ratings(raw), {})

    def test_later_duplicate_wins(self):
        self.assertEqual(parse_ratings("group:1=r18,group:1=sfw"), {"group:1": "sfw"})


class RatingAllowedTests(unittest.TestCase):
    def test_all_allows_both(self):
        self.assertTrue(rating_allowed("sfw", "all"))
        self.assertTrue(rating_allowed("r18", "all"))

    def test_sfw_only(self):
        self.assertTrue(rating_allowed("sfw", "sfw"))
        self.assertFalse(rating_allowed("r18", "sfw"))

    def test_r18_only(self):
        self.assertTrue(rating_allowed("r18", "r18"))
        self.assertFalse(rating_allowed("sfw", "r18"))

    def test_unknown_rating_is_false(self):
        for rating in ("", None, "unknown", 3):
            with self.subTest(rating=rating):
                self.assertFalse(rating_allowed(rating, "all"))


class UrlTests(unittest.TestCase):
    def test_keyword_is_url_encoded_with_percent20(self):
        url = build_search_url("https://x/{kw}?p={page}", "猫 娘", 2)
        self.assertIn("%E7%8C%AB%20%E5%A8%98", url)
        self.assertNotIn("+", url)
        self.assertIn("p=2", url)

    def test_page_substituted(self):
        self.assertEqual(build_search_url("https://x/{kw}/{page}", "a", 3), "https://x/a/3")

    def test_missing_kw_is_appended(self):
        url = build_search_url("https://x/search", "cat", 1)
        self.assertIn("kw=cat", url)

    def test_missing_page_is_appended(self):
        url = build_search_url("https://x/{kw}", "cat", 4)
        self.assertIn("page=4", url)

    def test_empty_template_falls_back_to_default(self):
        self.assertTrue(build_search_url("", "cat", 1).startswith("https://"))

    def test_bad_page_becomes_one(self):
        self.assertIn("page=1", build_search_url("https://x", "cat", "abc"))

    def test_image_url_pid_and_page(self):
        self.assertEqual(build_image_url("https://i/{pid}-{page}.png", "123", 2),
                         "https://i/123-2.png")

    def test_image_url_default_template(self):
        url = build_image_url("", "123", 1)
        self.assertIn("123", url)
        self.assertEqual(DEFAULT_IMAGE_TEMPLATE.count("{pid}"), 1)

    def test_image_url_bad_page_clamped(self):
        self.assertIn("page=1", build_image_url("https://i/x", "1", 0))


class PickCandidatesTests(unittest.TestCase):
    WORKS = [
        {"id": "a", "xRestrict": 0, "aiType": 1, "pageCount": 1},
        {"id": "b", "xRestrict": 1, "aiType": 1, "pageCount": 2},
        {"id": "a", "xRestrict": 0, "aiType": 1, "pageCount": 1},
        {"id": "c", "xRestrict": 0, "aiType": 2, "pageCount": 1},
    ]

    def test_sfw_filter_excludes_r18(self):
        self.assertEqual([w["id"] for w in pick_candidates(self.WORKS, "sfw", 10)], ["a", "c"])

    def test_r18_filter_excludes_sfw(self):
        self.assertEqual([w["id"] for w in pick_candidates(self.WORKS, "r18", 10)], ["b"])

    def test_all_allows_everything(self):
        self.assertEqual([w["id"] for w in pick_candidates(self.WORKS, "all", 10)], ["a", "b", "c"])

    def test_exclude_ai(self):
        self.assertEqual([w["id"] for w in pick_candidates(self.WORKS, "all", 10, exclude_ai=True)],
                         ["a", "b"])

    def test_max_count_truncates(self):
        self.assertEqual(len(pick_candidates(self.WORKS, "all", 2)), 2)

    def test_order_is_preserved(self):
        works = [{"id": "z"}, {"id": "y"}, {"id": "x"}]
        self.assertEqual([w["id"] for w in pick_candidates(works, "sfw", 10)], ["z", "y", "x"])

    def test_empty_and_garbage(self):
        self.assertEqual(pick_candidates([], "all", 5), [])
        self.assertEqual(pick_candidates(None, "all", 5), [])
        self.assertEqual(pick_candidates([None, "x", 3], "all", 5), [])

    def test_zero_max_count(self):
        self.assertEqual(pick_candidates(self.WORKS, "all", 0), [])


class EscapeTests(unittest.TestCase):
    def test_expected_output(self):
        self.assertEqual(escape_cq_text("&[a],b"), "&amp;&#91;a&#93;&#44;b")

    def test_amp_is_escaped_first(self):
        self.assertEqual(escape_cq_text("&amp;"), "&amp;amp;")

    def test_empty(self):
        self.assertEqual(escape_cq_text(""), "")
        self.assertEqual(escape_cq_text(None), "")


class PickImageIndexTests(unittest.TestCase):
    def test_injected_picker(self):
        self.assertEqual(pick_image_index(5, picker=lambda candidates: candidates[-1]), 5)

    def test_picker_may_see_all_pages(self):
        seen = []

        def picker(candidates):
            seen.append(list(candidates))
            return candidates[0]

        pick_image_index(3, picker=picker)
        self.assertEqual(seen, [[1, 2, 3]])

    def test_non_positive_returns_one(self):
        for count in (0, -1, None, "abc"):
            with self.subTest(count=count):
                self.assertEqual(pick_image_index(count), 1)

    def test_out_of_range_pick_falls_back(self):
        self.assertEqual(pick_image_index(3, picker=lambda candidates: 99), 1)


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        return PixivIllust(FakeManager(self.dir, settings))

    def test_remember_and_already_seen(self):
        plugin = self.make()
        plugin.remember("a", now=1.0)
        self.assertTrue(plugin.already_seen("a"))
        self.assertFalse(plugin.already_seen("b"))

    def test_capacity_prunes_oldest(self):
        plugin = self.make(stateCap=3)
        for index in range(5):
            plugin.remember(str(index), now=float(index))
        state = plugin.state()["seen"]
        self.assertEqual(len(state), 3)
        self.assertNotIn("0", state)
        self.assertNotIn("1", state)
        self.assertIn("4", state)

    def test_persist_and_reload(self):
        first = self.make()
        first.remember("a", now=10.0)
        first.persist()
        second = self.make()
        self.assertTrue(second.already_seen("a"))

    def test_corrupt_state_falls_back_to_empty(self):
        path = self.dir / PixivIllust.META.id / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        plugin = self.make()
        self.assertEqual(plugin.state()["seen"], {})

    def test_state_shape_is_versioned(self):
        plugin = self.make()
        self.assertEqual(plugin.state()["version"], 1)

    def test_no_recursion_on_state_load(self):
        # 原稿定义了 load_state(self) 覆盖基类并在内部再调 self.load_state → 无限递归
        plugin = self.make()
        plugin.state()
        plugin.remember("x", now=1.0)
        plugin.persist()
        plugin.state()

    def test_on_ingest_returns_none_and_takes_runtime(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest({"kind": "group", "target": "1", "text": "来张图 猫"}, None))


if __name__ == "__main__":
    unittest.main()
