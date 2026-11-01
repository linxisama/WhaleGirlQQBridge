"""网页控制台「日志」页（W13c）的静态回归测试。

三条端点（GET /api/logs、GET /api/logs/export、POST /api/logs/client）由另一张卡
接线，本卡只负责静态资源。因此这里全部是离线静态断言：不启动服务、不需要浏览器。
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "src/web/static/qq.html"
JS_PATH = ROOT / "src/web/static/qq.js"


def read_text(path):
    return path.read_text(encoding="utf-8")


class LogsTabHTMLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read_text(HTML_PATH)

    def test_logs_tab_panel_and_nav_button_exist(self):
        self.assertIn('id="tab-logs" class="tab-panel"', self.html)
        self.assertIn("switchTab('logs')", self.html)

    def test_toolbar_controls_have_ids(self):
        for control_id in ("log-level", "log-source", "log-query", "log-refresh",
                           "log-auto", "log-copy-all", "log-export"):
            self.assertIn('id="%s"' % control_id, self.html)

    def test_level_dropdown_options(self):
        select = re.search(r'<select id="log-level">(.*?)</select>', self.html, re.S)
        self.assertIsNotNone(select, "缺少级别下拉 #log-level")
        body = select.group(1)
        for value in ('value="error"', 'value="warning"', 'value="info"'):
            self.assertIn(value, body)
        self.assertIn("全部", body)

    def test_level_colour_classes_present(self):
        for cls in (".log-error", ".log-warning", ".log-info", ".log-debug"):
            self.assertIn(cls, self.html)

    def test_detail_area_keeps_newlines_and_monospace(self):
        block = re.search(r'\.log-detail pre\{([^}]*)\}', self.html)
        self.assertIsNotNone(block, "缺少展开区 .log-detail pre 样式")
        css = block.group(1)
        self.assertIn("white-space:pre-wrap", css)
        self.assertIn("monospace", css)

    def test_list_has_time_level_source_message_columns(self):
        for header in ("时间", "级别", "来源", "摘要"):
            self.assertIn(header, self.html)

    def test_existing_four_tabs_still_present(self):
        for tab in ("settings", "plugins", "persona", "messages"):
            self.assertIn('id="tab-%s"' % tab, self.html)
            self.assertIn("switchTab('%s')" % tab, self.html)


class LogsTabJSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read_text(JS_PATH)

    def test_switch_tab_mapping_includes_logs(self):
        match = re.search(r"const panels = \[([^\]]*)\]", self.js)
        self.assertIsNotNone(match, "switchTab 里找不到 panels 映射表")
        names = re.findall(r"'([a-z]+)'", match.group(1))
        self.assertEqual(names, ["settings", "plugins", "persona", "messages", "logs"])

    def test_list_fetch_hits_api_logs_with_limit_200(self):
        self.assertIn("'/api/logs?' + logQuery(200)", self.js)
        self.assertIn("'/api/logs/client'", self.js)

    def test_export_uses_export_endpoint_and_window_open(self):
        self.assertIn("'/api/logs/export?' + logQuery(1000)", self.js)
        self.assertIn("window.open(url)", self.js)

    def test_copy_uses_clipboard_then_execcommand_fallback(self):
        self.assertIn("navigator.clipboard", self.js)
        self.assertIn("writeText", self.js)
        self.assertIn("createElement('textarea')", self.js)
        self.assertIn("document.execCommand('copy')", self.js)

    def test_copy_all_is_one_line_per_entry(self):
        self.assertIn("logEntries.map(logLine).join('\\n')", self.js)

    def test_client_errors_reported_to_api_logs_client(self):
        self.assertIn("window.addEventListener('error'", self.js)
        self.assertIn("window.addEventListener('unhandledrejection'", self.js)
        self.assertIn("'/api/logs/client'", self.js)
        self.assertIn("source: 'web'", self.js)

    def test_404_and_network_errors_show_chinese_hint(self):
        self.assertIn("res.status === 404", self.js)
        self.assertIn("日志接口还没就绪", self.js)
        self.assertIn("导出失败：日志接口未就绪", self.js)

    def test_auto_refresh_is_5s_and_only_on_visible_logs_tab(self):
        self.assertIn("}, 5000)", self.js)
        self.assertIn("classList.contains('active')", self.js)
        self.assertIn("document.visibilityState === 'hidden'", self.js)

    def test_entry_contract_fields_are_used(self):
        for field in ("entry.ts", "entry.level", "entry.source", "entry.message",
                      "entry.detail", "entry.code", "entry.id"):
            self.assertIn(field, self.js)

    def test_single_row_copy_includes_detail(self):
        self.assertIn("entry.detail ? logLine(entry) + '\\n' + entry.detail : logLine(entry)", self.js)


class AssetHygieneTests(unittest.TestCase):
    def test_no_bom_and_lf_only(self):
        for path in (HTML_PATH, JS_PATH):
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "%s 含 BOM" % path.name)
            self.assertNotIn(b"\r\n", raw, "%s 用了 CRLF 换行" % path.name)
            self.assertNotIn(b"\r", raw, "%s 含裸 CR" % path.name)

    def test_js_syntax_with_node_check(self):
        """qq.js 是普通脚本（无 import/export），所以 node --check 即可解析。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("未找到 node，跳过语法检查")
        result = subprocess.run([node, "--check", str(JS_PATH)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
