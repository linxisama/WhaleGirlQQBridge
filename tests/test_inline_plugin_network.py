"""回复前的"插件联网"必须在预算内完成：分享卡片读正文、/搜索。

实测（本机模拟一个 6 秒才响应的站点、一条消息带 3 个链接、模型 0.9 秒）：
- 旧实现（逐个 await，默认 12 秒超时）：**18.93 秒**，其中插件段 18.00 秒；
- 新实现（并发 + 单页 3 秒 + 总预算 4 秒）：**3.92 秒**，插件段 2.98 秒。
"""
import asyncio
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import src.plugins.share_card as share_card  # noqa: E402
from src.qq_app import QQRuntime  # noqa: E402


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n", encoding="utf-8")
    return root


class ShareCardBudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.plugin = self.bot.plugins._plugins["share_card"]
        self.bot.plugins.update_settings("share_card", {
            "enabled": True, "readWeb": True, "maxLinks": 3,
            "readTimeoutMs": 600, "readBudgetMs": 900})

    def tearDown(self):
        # 先直接关库再走异步 close：close() 里任何一步抛错都不能让 sqlite 句柄留着
        # （Windows 下临时目录会因句柄未释放而删不掉）
        try:
            self.bot.db.close()
        except Exception:
            pass
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass
        self._tmp.cleanup()

    def message(self, count=3):
        links = " ".join(f"https://slow.example.com/{index}" for index in range(count))
        return {"id": "m1", "kind": "group", "target": "456", "sender_id": "20002",
                "text": f"看看这个 {links}", "media": []}

    def test_links_are_read_concurrently_within_budget(self):
        async def slow(url, **kwargs):
            await asyncio.sleep(5)                # 站点很慢，超过单页超时
            return "<html><body>正文</body></html>"

        async def run():
            with mock.patch.object(share_card, "read_public", side_effect=slow):
                started = time.perf_counter()
                message = self.message(3)
                await self.plugin.on_prepare(message, self.bot)
                return time.perf_counter() - started, message

        elapsed, message = asyncio.run(run())
        self.assertLess(elapsed, 1.8, f"三个链接并发读取后不该超过预算，实测 {elapsed:.2f}s")
        self.assertEqual(len(message["share_pages"]), 3)
        self.assertTrue(all("超时" in item["status"] or "失败" in item["status"]
                            for item in message["share_pages"]), "超时要说清楚，不能编正文")

    def test_single_page_timeout_is_passed_to_the_reader(self):
        seen = {}

        async def quick(url, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return "<html><body>正文</body></html>"

        async def run():
            with mock.patch.object(share_card, "read_public", side_effect=quick):
                await self.plugin.on_prepare(self.message(1), self.bot)

        asyncio.run(run())
        self.assertAlmostEqual(float(seen["timeout"]), 0.6, delta=0.05)

    def test_fast_site_returns_content(self):
        async def quick(url, **kwargs):
            return "<html><body><p>这是正文</p></body></html>"

        async def run():
            message = self.message(2)
            with mock.patch.object(share_card, "read_public", side_effect=quick):
                await self.plugin.on_prepare(message, self.bot)
            return message

        message = asyncio.run(run())
        self.assertIn("这是正文", message["share_pages"][0]["content"])

    def test_web_reading_off_costs_nothing(self):
        self.bot.plugins.update_settings("share_card", {"readWeb": False})

        async def boom(*args, **kwargs):
            raise AssertionError("关掉读正文后不该联网")

        async def run():
            message = self.message(2)
            with mock.patch.object(share_card, "read_public", side_effect=boom):
                await self.plugin.on_prepare(message, self.bot)
            return message

        message = asyncio.run(run())
        self.assertNotIn("share_pages", message)


class SearchTimeoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.plugin = self.bot.plugins._plugins["search_query_hygiene"]

    def tearDown(self):
        # 先直接关库再走异步 close：close() 里任何一步抛错都不能让 sqlite 句柄留着
        # （Windows 下临时目录会因句柄未释放而删不掉）
        try:
            self.bot.db.close()
        except Exception:
            pass
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass
        self._tmp.cleanup()

    def test_search_uses_the_configured_timeout(self):
        import src.plugins.search_query_hygiene as search_module
        self.bot.plugins.update_settings("search_query_hygiene", {"timeoutMs": 4000})
        seen = {}

        async def fake(url, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return "<html></html>"

        async def run():
            message = {"id": "m1", "kind": "group", "target": "456", "sender_id": "1",
                       "text": "/搜索 今天的新闻"}
            with mock.patch.object(search_module, "read_public", side_effect=fake):
                await self.plugin.on_prepare(message, self.bot)
            return message

        asyncio.run(run())
        self.assertAlmostEqual(float(seen["timeout"]), 4.0, delta=0.1,
                               msg="插件的 timeoutMs 设置要真正传到底层读取")

    def test_search_failure_is_reported_not_invented(self):
        import src.plugins.search_query_hygiene as search_module

        async def boom(url, **kwargs):
            raise TimeoutError("slow")

        async def run():
            message = {"id": "m1", "kind": "group", "target": "456", "sender_id": "1",
                       "text": "/搜索 关键词"}
            with mock.patch.object(search_module, "read_public", side_effect=boom):
                await self.plugin.on_prepare(message, self.bot)
            return message

        message = asyncio.run(run())
        self.assertIn("失败", message["search_data"]["error"])


class FastProfileCoversWebReadsTests(unittest.TestCase):
    def test_profile_tightens_inline_web_reads(self):
        from src.speed_profile import FAST_VALUES, profile_diff
        self.assertIn("share_card", FAST_VALUES)
        self.assertIn("search_query_hygiene", FAST_VALUES)
        plan = profile_diff({
            "share_card": {"enabled": True, "readTimeoutMs": 12000, "readBudgetMs": 30000},
            "search_query_hygiene": {"enabled": True, "timeoutMs": 12000},
        })
        self.assertEqual(plan["share_card"], {"readTimeoutMs": 2000, "readBudgetMs": 2500})
        self.assertEqual(plan["search_query_hygiene"], {"timeoutMs": 4000})

    def test_disabled_plugin_is_still_not_touched_elsewhere(self):
        from src.speed_profile import profile_diff
        plan = profile_diff({"share_card": {"enabled": False, "readTimeoutMs": 12000,
                                            "readBudgetMs": 30000}})
        self.assertEqual(list(plan), ["share_card"])
        self.assertNotIn("enabled", plan["share_card"])


if __name__ == "__main__":
    unittest.main()
