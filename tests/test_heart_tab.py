"""网页控制台「心情」标签页（/heart 嵌入）的静态回归测试。

只做离线静态断言：不启动服务、不需要浏览器。真正的路由契约在 test_heart_api.py。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "src/web/static/qq.html"
JS_PATH = ROOT / "src/web/static/qq.js"


def read_text(path):
    return path.read_text(encoding="utf-8")


class HeartTabHTMLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read_text(HTML_PATH)

    def test_tab_panel_and_nav_button_exist(self):
        self.assertIn('id="tab-heart" class="tab-panel"', self.html)
        self.assertIn("switchTab('heart')", self.html)

    def test_panel_embeds_the_standalone_page(self):
        self.assertIn('id="heart-frame"', self.html)
        self.assertIn('class="heart-frame"', self.html)
        # 内嵌用 ?embed=1 让面板自己收掉整页留白。
        self.assertIn("/heart?embed=1", read_text(JS_PATH))

    def test_reload_button_and_external_link_present(self):
        self.assertIn('id="heart-reload"', self.html)
        self.assertIn('href="/heart"', self.html)

    def test_existing_five_tabs_still_present(self):
        for tab in ("settings", "plugins", "persona", "messages", "logs"):
            self.assertIn('id="tab-%s"' % tab, self.html)
            self.assertIn("switchTab('%s')" % tab, self.html)


class HeartTabJSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read_text(JS_PATH)

    def test_switch_tab_mapping_is_unchanged(self):
        match = re.search(r"const panels = \[([^\]]*)\]", self.js)
        self.assertIsNotNone(match, "switchTab 里找不到 panels 映射表")
        names = re.findall(r"'([a-z]+)'", match.group(1))
        self.assertEqual(names, ["settings", "plugins", "persona", "messages", "logs"])

    def test_new_tab_declares_itself_via_data_tab(self):
        self.assertIn("data-tab", self.js)
        self.assertIn("b.dataset.tab", self.js)

    def test_heart_tab_loads_lazily_once(self):
        self.assertIn("function enterHeart()", self.js)
        self.assertIn("heartLoaded", self.js)
        self.assertIn("if (name === 'heart') enterHeart();", self.js)

    def test_reload_bypasses_cache(self):
        self.assertIn("frame.src = '/heart?embed=1&t=' + Date.now();", self.js)


class HeartPanelEmbedTests(unittest.TestCase):
    """面板页在 embed=1 下必须收掉整页留白。"""

    @classmethod
    def setUpClass(cls):
        cls.page = read_text(Path('D:/插件/dsh-heart-qq/web/heart.html'))

    def test_embed_class_is_applied_from_query(self):
        self.assertIn("embed=1", self.page)
        self.assertIn("classList.add('embed')", self.page)

    def test_embed_rules_collapse_padding(self):
        block = re.search(r'\.embed \.wrap\{([^}]*)\}', self.page)
        self.assertIsNotNone(block, "缺少 .embed .wrap 样式")
        self.assertIn("padding:0", block.group(1))
        self.assertIn("max-width:none", block.group(1))

    def test_standalone_header_still_exists(self):
        self.assertIn("<h1>心情系统 · 全景</h1>", self.page)
        self.assertIn('href="/"', self.page)


if __name__ == "__main__":
    unittest.main()
