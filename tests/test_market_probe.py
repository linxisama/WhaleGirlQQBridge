"""市场探针：目录解析（JSON / HTML）、关键词过滤、清单格式，以及"只看看不装"的边界。

用户原来的"市场探针"只有元数据、没有任何钩子；现在它抓目录、筛关键词、回清单，
但**绝不自动安装**：远程代码仍旧要走控制台「添加插件」的预检 + 二次确认。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402
from src.plugins.market_probe import (  # noqa: E402
    filter_entries, format_report, parse_market,
)

JSON_MARKET = """{"plugins": [
  {"id": "dsh-qq-bridge", "name": "QQ 桥接", "url": "https://github.com/example/dsh-qq-bridge",
   "description": "把 QQ 消息接进 agent", "tags": ["qq", "机器人"]},
  {"name": "latex-tools", "url": "https://github.com/example/latex", "description": "排版工具"}
]}"""

HTML_MARKET = """
<div class="card">
  <a href="detail.php?id=someone%2Fqq-bridge">QQ 桥接插件</a>
  <p>someone</p>
  <p>把 QQ 消息接进 agent，支持图片</p>
  <p>qq chatbot dsh-plugin</p>
</div>
<div class="card">
  <a href="detail.php?id=other%2Flatex">LaTeX 工具</a>
  <p>other</p>
  <p>排版用，与机器人无关</p>
  <p>latex tool</p>
</div>
"""


class ParseMarketTests(unittest.TestCase):
    def test_json_market_parses_fields(self):
        entries = parse_market(JSON_MARKET)
        self.assertEqual([e['name'] for e in entries], ['QQ 桥接', 'latex-tools'])
        self.assertEqual(entries[0]['url'], 'https://github.com/example/dsh-qq-bridge')
        self.assertEqual(entries[0]['tags'], ['qq', '机器人'])

    def test_html_market_parses_detail_links(self):
        entries = parse_market(HTML_MARKET, 'https://market.test/index.php')
        self.assertEqual([e['name'] for e in entries], ['QQ 桥接插件', 'LaTeX 工具'])
        self.assertTrue(entries[0]['url'].startswith('https://market.test/'))
        self.assertIn('QQ 消息', entries[0]['description'])
        self.assertIn('someone', entries[0]['author'])

    def test_slug_style_market_parses_and_skips_navigation(self):
        html = ('<a href="/zh/plugins/directory/">插件目录</a>'
                '<a href="/zh/plugins/qq-bridge/">QQ 桥接</a>'
                '<a href="/zh/plugins/">全部</a>'
                '<a href="/zh/plugins/memory-pack/">记忆包</a>')
        entries = parse_market(html, 'https://market.test/zh/plugins/directory/')
        names = [e['name'] for e in entries]
        self.assertIn('QQ 桥接', names)
        self.assertIn('记忆包', names)
        self.assertNotIn('插件目录', names, '目录/全部这类导航链接不算插件条目')

    def test_garbage_returns_empty(self):
        self.assertEqual(parse_market('不是目录页'), [])
        self.assertEqual(parse_market(''), [])
        self.assertEqual(parse_market('{"broken": '), [])

    def test_keyword_filter_matches_name_description_tags(self):
        entries = parse_market(JSON_MARKET)
        hits = filter_entries(entries, '机器人,qq')
        self.assertEqual([e['name'] for e in hits], ['QQ 桥接'])
        self.assertEqual(len(filter_entries(entries, '')), 2, '空关键词=全都要')
        self.assertEqual(filter_entries(entries, '不存在的词'), [])

    def test_report_says_it_does_not_install(self):
        text = format_report(parse_market(JSON_MARKET), total=2, keywords='qq', url='https://m/x')
        self.assertIn('共 2 项', text)
        self.assertIn('QQ 桥接', text)
        self.assertIn('确认', text, '清单要说明安装需要预检+确认')


COMMUNITY_MARKET = json.dumps({
    "categories": {"message": "消息与身份", "utility": "工具与运行控制"},
    "items": [
        {"id": "early-finish", "name": "token 直接减半", "author": "小肥鱼",
         "category": "utility", "categoryName": "工具与运行控制", "version": "1.2.0",
         "downloads": 2170, "installCode": "8KLWS4", "status": "approved",
         "description": "她在一次运行里把话发出去之后，如果这一批没有遗留动作就直接结束。",
         "downloadUrl": "/api/community/plugins/early-finish/download",
         "updatedAt": "2026-10-14T22:35:53Z"},
        {"id": "context-reuse", "name": "上下文延续", "author": "某位群友",
         "categoryName": "其他", "version": "1.0.0", "downloads": 948,
         "description": "同一会话的连续消息共享最近几轮对话历史。",
         "downloadUrl": "/api/community/plugins/context-reuse/download"},
        {"id": "brand-new-thing", "name": "全新的插件", "author": "作者",
         "categoryName": "媒体与多媒体", "version": "2.1.0", "downloads": 42,
         "description": "还没有移植过的插件。",
         "downloadUrl": "/api/community/plugins/brand-new-thing/download"}
    ]
}, ensure_ascii=False)


class CommunityMarketTests(unittest.TestCase):
    """QQ Agent 社区市场（kondius.cn/api/community/plugins）的真实 JSON 形状。"""

    def setUp(self):
        self.entries = parse_market(COMMUNITY_MARKET, "https://kondius.cn/api/community/plugins")

    def test_community_fields_are_kept(self):
        self.assertEqual(len(self.entries), 3)
        first = self.entries[0]
        self.assertEqual(first["market_id"], "early-finish")
        self.assertEqual(first["downloads"], 2170)
        self.assertEqual(first["install_code"], "8KLWS4")
        self.assertEqual(first["category"], "工具与运行控制")
        self.assertEqual(first["version"], "1.2.0")

    def test_download_url_is_made_absolute(self):
        self.assertEqual(self.entries[0]["url"],
                         "https://kondius.cn/api/community/plugins/early-finish/download")

    def test_report_marks_ported_and_rejected(self):
        text = format_report(self.entries, total=3, url="https://kondius.cn/api/community/plugins",
                             limit=8)
        self.assertIn("✅ 本项目已移植（提前收尾（省 token））", text)
        self.assertIn("⏭ 已评估不适用", text)
        self.assertIn("2170 次下载", text)
        self.assertIn("口令 8KLWS4", text)
        self.assertNotIn("✅", text.split("3. ")[1][:60], "没有移植过的插件不该被标成已移植")

    def test_default_market_points_at_the_community_api(self):
        from src.plugins import market_probe
        self.assertEqual(market_probe.DEFAULT_MARKET, "https://kondius.cn/api/community/plugins")
        self.assertIn("early-finish", market_probe.PORTED)
        self.assertIn("context-reuse", market_probe.REJECTED)


class MarketProbePluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
        self.bot = QQRuntime(root=self.root, db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["market_probe"]
        self.bot.plugins.update_settings("market_probe", {"enabled": True})

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def test_dynamic_catalogue_page_explains_itself(self):
        """脚本渲染的目录页：要明确告诉用户为什么没解析出来、该怎么办。"""
        import asyncio

        with mock.patch("src.public_web.read_public",
                        new=mock.AsyncMock(return_value='<html><nav><a href="/x/">首页</a></nav></html>')):
            text = asyncio.run(self.plugin.fetch(self.bot))
        self.assertIn('没解析出插件条目', text)
        self.assertIn('JSON', text)

    def test_probe_filters_and_reports(self):
        with mock.patch("src.public_web.read_public",
                        new=mock.AsyncMock(return_value=JSON_MARKET)):
            text = asyncio.run(self.plugin.fetch(self.bot, keywords='机器人'))
        self.assertIn('QQ 桥接', text)
        self.assertNotIn('latex-tools', text)

    def test_probe_reports_fetch_failure_in_plain_words(self):
        with mock.patch("src.public_web.read_public",
                        new=mock.AsyncMock(side_effect=OSError('连不上'))):
            text = asyncio.run(self.plugin.fetch(self.bot))
        self.assertIn('抓取失败', text)
        self.assertIn('连不上', text)

    def test_bad_url_is_rejected_before_any_request(self):
        with mock.patch("src.public_web.read_public", new=mock.AsyncMock()) as reader:
            text = asyncio.run(self.plugin.fetch(self.bot, url='ftp://x/y'))
        reader.assert_not_called()
        self.assertIn('http', text)

    def test_admin_private_message_triggers_probe(self):
        self.bot.plugins.update_settings("pc_control", {"enabled": True, "ownerIds": "10001"})
        message = {"id": "m1", "kind": "private", "target": "admin", "sender_id": "10001",
                   "text": "市场 qq"}
        with mock.patch.object(self.plugin, "probe_now", return_value='清单') as probe:
            reply = self.plugin.on_ingest(message, self.bot)
        probe.assert_called_once()
        self.assertEqual(reply, '清单')

    def test_non_admin_message_is_refused(self):
        self.bot.plugins.update_settings("pc_control", {"enabled": True, "ownerIds": "10001"})
        message = {"id": "m2", "kind": "private", "target": "x", "sender_id": "999",
                   "text": "市场"}
        self.assertIn('绑定管理员', self.plugin.on_ingest(message, self.bot))

    def test_group_message_is_ignored(self):
        message = {"id": "m3", "kind": "group", "target": "g", "sender_id": "10001", "text": "市场"}
        self.assertIsNone(self.plugin.on_ingest(message, self.bot))


if __name__ == "__main__":
    unittest.main()
