"""W13b：桌面「日志」页（src/gui/logs_page.py）的离屏验收。

注入假的 api_get / api_post（返回构造好的日志条目），断言控件树、筛选参数映射、
详情区、剪贴板、导出落盘、404 兜底、自动刷新与定时器清理。全程不碰网络、
不碰后端日志文件（导出只写临时目录）。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QColor
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QPlainTextEdit, QWidget
    HAVE_QT = True
except Exception:                   # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_APP = None


def _app():
    # 必须持有模块级引用：QApplication 被回收后再构造 QWidget 会直接终止进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


TRACEBACK = ('Traceback (most recent call last):\n'
             '  File "src/qq_app.py", line 88, in reply\n'
             '    raise RuntimeError("boom")\n'
             'RuntimeError: boom')

SAMPLE = [
    {'id': 'id-error-1', 'ts': '2026-10-26T17:05:12+08:00', 'level': 'error', 'source': 'qq',
     'message': '发送失败：连接被重置', 'detail': TRACEBACK, 'code': 'QQ_SEND_FAIL', 'extra': {}},
    {'id': 'id-warning-1', 'ts': '2026-10-26T17:02:00+08:00', 'level': 'warning',
     'source': 'console', 'message': '后端端口探活超时一次', 'detail': '', 'code': '', 'extra': {}},
    {'id': 'id-info-1', 'ts': '2026-10-26T16:58:41+08:00', 'level': 'info', 'source': 'gui',
     'message': '打开了控制台', 'detail': '窗口尺寸 1280x800\n主题：深色', 'code': '', 'extra': {}},
    {'id': 'id-debug-1', 'ts': '2026-10-26T16:50:03+08:00', 'level': 'debug', 'source': 'qq',
     'message': '入站消息已解析', 'detail': '', 'code': '', 'extra': {}},
]


def ok_payload(entries=None, total=None, truncated=False):
    items = [dict(entry) for entry in (SAMPLE if entries is None else entries)]
    return {'ok': True, 'entries': items,
            'total': len(items) if total is None else total, 'truncated': truncated}


def export_payload(entries=None):
    items = [dict(entry) for entry in (SAMPLE if entries is None else entries)]
    return {'version': 1, 'exported_at': '2026-10-26T17:05:12+08:00',
            'filters': {'level': '', 'source': '', 'query': '', 'limit': 1000},
            'count': len(items), 'entries': items}


class FakeApi(object):
    """记录请求路径的假 api_get；payload 可以是字典，也可以是 (path) -> 字典 的函数。"""

    def __init__(self, payload=None, error=None):
        self.payload = ok_payload() if payload is None else payload
        self.error = error
        self.paths = []

    def __call__(self, path, *_args, **_kwargs):
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        if callable(self.payload):
            return self.payload(path)
        return self.payload


@unittest.skipUnless(HAVE_QT, '需要 PyQt6')
class _PageCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        from src.gui import logs_page
        cls.mod = logs_page

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='w13b-')
        self.addCleanup(self.tmp.cleanup)

    def make_page(self, payload=None, error=None, show=True):
        api = FakeApi(payload, error)
        page = self.mod.LogsPage(api, lambda *a, **k: {'ok': True})
        self.addCleanup(self._dispose, page)
        if show:
            page.show()
            self.pump()
        return page, api

    def pump(self, ms=250):
        QTest.qWait(ms)

    def _dispose(self, page):
        try:
            page.shutdown()
            page.hide()
            page.deleteLater()
            _app().processEvents()
        except RuntimeError:        # C++ 对象已经没了
            pass

    def cell_texts(self, page):
        table = page.table
        return [[table.item(row, col).text() for col in range(table.columnCount())]
                for row in range(table.rowCount())]


class LayoutTests(_PageCase):
    """控件树、首帧不碰网络、表格四列与状态行。"""

    def test_construction_does_not_touch_network(self):
        api = FakeApi()
        page = self.mod.LogsPage(api, None)
        self.addCleanup(self._dispose, page)
        self.pump(150)
        self.assertEqual(api.paths, [], '构造期不许发请求')
        self.assertEqual(page.table.rowCount(), 0)

    def test_widget_tree_and_ids(self):
        page, _api = self.make_page()
        self.assertEqual(page.title.text(), '日志')
        self.assertIn('/api/logs', page.hint.text())
        for name in ('logs_table', 'logs_detail', 'logs_level', 'logs_source', 'logs_query',
                     'logs_refresh', 'logs_auto', 'logs_copy_one', 'logs_copy_all',
                     'logs_export', 'logs_clear', 'logs_status'):
            found = page.findChild(QWidget, name)
            self.assertIsNotNone(found, '缺少控件 %s' % name)
        self.assertTrue(page.detail.isReadOnly())
        self.assertIsInstance(page.detail, QPlainTextEdit)

    def test_table_renders_four_columns_for_every_entry(self):
        page, _api = self.make_page()
        title = [page.table.horizontalHeaderItem(i).text() for i in range(4)]
        self.assertEqual(title, list(self.mod.COLUMNS))
        self.assertEqual(page.table.rowCount(), len(SAMPLE))
        rows = self.cell_texts(page)
        self.assertEqual(rows[0][0], '2026-10-26 17:05:12')
        self.assertEqual(rows[0][1], '错误')
        self.assertEqual(rows[0][2], 'qq')
        self.assertEqual(rows[0][3], SAMPLE[0]['message'])
        self.assertEqual([row[1] for row in rows], ['错误', '警告', '信息', '调试'])

    def test_status_reports_total_and_shown(self):
        page, _api = self.make_page()
        self.assertEqual(page.status_label.text(), '共 4 条（显示最近 4 条）')

    def test_level_column_is_coloured_by_level(self):
        page, _api = self.make_page()
        wanted = ['#b42318', '#b54708', '#3a5a80', '#6b7280']
        got = [QColor(page.table.item(row, 1).foreground().color()).name()
               for row in range(page.table.rowCount())]
        self.assertEqual(got, wanted)


class RequestMappingTests(_PageCase):
    """筛选 → 请求 query 的映射（参数名逐字对齐契约）。"""

    def test_level_combo_sends_level_parameter(self):
        page, api = self.make_page()
        page.combo_level.setCurrentIndex(page.combo_level.findData('error'))
        self.pump()
        self.assertIn('level=error', api.paths[-1])
        self.assertIn('limit=200', api.paths[-1])
        page.combo_level.setCurrentIndex(0)
        self.pump()
        self.assertNotIn('level=', api.paths[-1])

    def test_query_edit_enter_sends_query_parameter(self):
        page, api = self.make_page()
        page.input_query.setText('失败')
        QTest.keyClick(page.input_query, Qt.Key.Key_Return)
        self.pump()
        self.assertIn('query=', api.paths[-1])
        self.assertIn('%E5%A4%B1%E8%B4%A5', api.paths[-1])

    def test_source_combo_collects_unique_sources_and_keeps_choice(self):
        def payload_for(path):
            if 'source=console' in path:
                return ok_payload([item for item in SAMPLE if item['source'] == 'console'])
            return ok_payload()

        page, api = self.make_page(payload=payload_for)
        names = [page.combo_source.itemText(i) for i in range(page.combo_source.count())]
        self.assertEqual(names, ['全部', 'qq', 'console', 'gui'], '来源要去重（qq 出现两次）')
        page.combo_source.setCurrentIndex(page.combo_source.findData('console'))
        self.pump()
        self.assertIn('source=console', api.paths[-1])
        self.assertEqual(page.table.rowCount(), 1)
        after = [page.combo_source.itemText(i) for i in range(page.combo_source.count())]
        self.assertEqual(after, names, '收窄后也不许把别处来源丢掉')
        self.assertEqual(page.combo_source.currentData(), 'console', '用户的选择要留着')

    def test_refresh_button_keeps_current_filters(self):
        page, api = self.make_page()
        page.combo_level.setCurrentIndex(page.combo_level.findData('warning'))
        page.input_query.setText('探活')
        self.pump()
        page.btn_refresh.click()
        self.pump()
        self.assertIn('level=warning', api.paths[-1])
        self.assertIn('query=', api.paths[-1])


class DetailAndClipboardTests(_PageCase):
    """点行灌详情、复制这条 / 复制全部。"""

    def test_click_row_fills_detail_area(self):
        page, _api = self.make_page()
        page.table.selectRow(0)
        text = page.detail.toPlainText()
        self.assertIn(SAMPLE[0]['ts'], text)
        self.assertIn('错误', text)
        self.assertIn('qq', text)
        self.assertIn('RuntimeError: boom', text)

    def test_entry_without_detail_shows_placeholder(self):
        page, _api = self.make_page()
        page.table.selectRow(1)
        self.assertIn(self.mod.EMPTY_DETAIL_TEXT, page.detail.toPlainText())

    def test_copy_one_puts_detail_on_clipboard(self):
        page, _api = self.make_page()
        page.table.selectRow(0)
        QApplication.clipboard().setText('')
        page.btn_copy_one.click()
        clip = QApplication.clipboard().text()
        self.assertIn('RuntimeError: boom', clip)
        self.assertIn(SAMPLE[0]['ts'], clip)
        self.assertEqual(page.status_label.text(), '已复制 1 条')

    def test_copy_all_is_one_line_per_entry(self):
        page, _api = self.make_page()
        QApplication.clipboard().setText('')
        page.btn_copy_all.click()
        lines = QApplication.clipboard().text().splitlines()
        self.assertEqual(len(lines), len(SAMPLE))
        self.assertIn(SAMPLE[0]['message'], lines[0])
        self.assertIn('Traceback', lines[0], 'detail 要单行化后同一条里')
        self.assertNotIn('\n', lines[0])
        self.assertEqual(page.status_label.text(), '已复制 4 条')


class ExportTests(_PageCase):
    """导出：默认文件名、走 /logs/export、UTF-8 无 BOM、count 正确。"""

    def make_export_page(self):
        def payload_for(path):
            if path.startswith('logs/export?'):
                return export_payload()
            return ok_payload()
        return self.make_page(payload=payload_for)

    def export_to(self, page, name):
        target = Path(self.tmp.name) / name
        with patch.object(self.mod.QFileDialog, 'getSaveFileName',
                          return_value=(str(target), self.mod.EXPORT_DIALOG_FILTER)):
            page.btn_export.click()
            self.pump()
        return target

    def test_export_writes_readable_json_without_bom(self):
        page, _api = self.make_export_page()
        target = self.export_to(page, 'app-log-test.json')
        raw = target.read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'), '不许写 BOM')
        data = json.loads(raw.decode('utf-8'))
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['count'], len(SAMPLE))
        self.assertEqual(len(data['entries']), len(SAMPLE))
        self.assertIn('已导出 4 条', page.status_label.text())

    def test_export_uses_current_filters_and_export_limit(self):
        page, api = self.make_export_page()
        page.combo_level.setCurrentIndex(page.combo_level.findData('error'))
        self.pump()
        self.export_to(page, 'filtered.json')
        calls = [path for path in api.paths if path.startswith('logs/export?')]
        self.assertEqual(len(calls), 1, '一次点击只发一次导出请求')
        self.assertIn('level=error', calls[0])
        self.assertIn('limit=1000', calls[0])

    def test_export_cancel_writes_nothing(self):
        page, api = self.make_page()
        before = len(api.paths)
        with patch.object(self.mod.QFileDialog, 'getSaveFileName', return_value=('', '')):
            page.btn_export.click()
            self.pump()
        self.assertEqual(len(api.paths), before)
        self.assertIn('已取消', page.status_label.text())
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_default_file_name_is_timestamped(self):
        page, _api = self.make_page()
        seen = {}

        def fake_dialog(_parent, _title, default_name, _filter):
            seen['name'] = default_name
            return ('', '')

        with patch.object(self.mod.QFileDialog, 'getSaveFileName', side_effect=fake_dialog):
            page.btn_export.click()
        self.assertRegex(seen['name'], r'^app-log-\d{8}-\d{6}\.json$')


class FailureModeTests(_PageCase):
    """接口没就绪 / HTTP 异常：一行中文，不抛不白屏。"""

    def test_missing_endpoint_shows_restart_hint(self):
        page, api = self.make_page(payload={'detail': 'Not Found'})
        self.assertEqual(page.status_label.text(), self.mod.NOT_READY_TEXT)
        self.assertEqual(page.table.rowCount(), 0)
        self.assertEqual(len(api.paths), 1)

    def test_http_exception_becomes_one_chinese_line(self):
        page, _api = self.make_page(error=RuntimeError('HTTP Error 404: Not Found'))
        self.assertEqual(page.status_label.text(), self.mod.NOT_READY_TEXT)

    def test_backend_down_falls_back_to_restart_hint(self):
        page, _api = self.make_page(payload={})
        self.assertEqual(page.status_label.text(), self.mod.NOT_READY_TEXT)

    def test_other_error_keeps_one_line_with_reason(self):
        page, _api = self.make_page(error=RuntimeError('connection reset by peer'))
        text = page.status_label.text()
        self.assertTrue(text.startswith('日志接口请求失败：'), text)
        self.assertNotIn('\n', text)


class RefreshAndLifecycleTests(_PageCase):
    """自动刷新 5 秒、可见性、清空、定时器清理。"""

    def test_auto_refresh_ticks_while_visible_and_stops_when_hidden(self):
        page, api = self.make_page()
        self.assertEqual(page.auto_timer.interval(), 5000)
        self.assertFalse(page.auto_timer.isActive(), '默认不自动刷新')
        page.check_auto.setChecked(True)
        self.assertTrue(page.auto_timer.isActive())
        calls = len(api.paths)
        QTest.qWait(5600)
        self.assertGreaterEqual(len(api.paths), calls + 1, '5 秒后要再发一次请求')
        self.assertEqual(page.table.rowCount(), len(SAMPLE))
        page.hide()
        self.assertFalse(page.auto_timer.isActive(), '看不见就不许跑')

    def test_shutdown_stops_every_timer_and_queue(self):
        page, api = self.make_page()
        page.check_auto.setChecked(True)
        self.assertTrue(page.auto_timer.isActive())
        page.shutdown()
        self.assertFalse(page.auto_timer.isActive())
        self.assertFalse(page.requests.timer.isActive())
        for timer in page.findChildren(QTimer):
            self.assertFalse(timer.isActive())
        calls = len(api.paths)
        page.refresh()
        self.pump(200)
        self.assertEqual(len(api.paths), calls, 'shutdown 之后不许再发请求')

    def test_clear_list_only_clears_the_widget(self):
        page, api = self.make_page()
        calls = len(api.paths)
        page.btn_clear.click()
        self.assertEqual(page.table.rowCount(), 0)
        self.assertEqual(page.entries, [])
        self.assertEqual(len(api.paths), calls, '清空列表不碰后端')
        self.assertIn('列表已清空', page.status_label.text())
        page.refresh()
        self.pump()
        self.assertEqual(page.table.rowCount(), len(SAMPLE), '刷新能重新读回来')


class PureHelperTests(unittest.TestCase):
    """纯函数（不依赖 Qt）：请求串、详情文本、导出落盘、错误翻译。"""

    @classmethod
    def setUpClass(cls):
        from src.gui import logs_page
        cls.mod = logs_page

    def test_build_query_skips_empty_and_keeps_limit(self):
        mod = self.mod
        self.assertEqual(mod.build_query('', '', '', 200), 'limit=200')
        query = mod.build_query('error', 'qq', '失败', 200)
        self.assertIn('level=error', query)
        self.assertIn('source=qq', query)
        self.assertIn('query=%E5%A4%B1%E8%B4%A5', query)
        self.assertIn('limit=200', query)

    def test_paths_point_at_the_frozen_endpoints(self):
        mod = self.mod
        self.assertTrue(mod.logs_path().startswith('logs?'))
        self.assertTrue(mod.export_path().startswith('logs/export?'))
        self.assertIn('limit=1000', mod.export_path())

    def test_display_time_and_level_text(self):
        mod = self.mod
        self.assertEqual(mod.display_time('2026-10-26T17:05:12+08:00'), '2026-10-26 17:05:12')
        self.assertEqual(mod.level_text('error'), '错误')
        self.assertEqual(mod.level_text('whatever'), 'whatever')

    def test_entry_detail_text_and_line(self):
        mod = self.mod
        text = mod.entry_detail_text(SAMPLE[0])
        self.assertIn('RuntimeError: boom', text)
        self.assertIn('\n', text)
        self.assertIn(mod.EMPTY_DETAIL_TEXT, mod.entry_detail_text(SAMPLE[1]))
        line = mod.entry_line(SAMPLE[0])
        self.assertNotIn('\n', line)
        self.assertIn(' | ', line)

    def test_payload_error_translates_404_and_contract(self):
        mod = self.mod
        self.assertEqual(mod.payload_error(ok_payload()), '')
        self.assertEqual(mod.payload_error({'detail': 'Not Found'}), mod.NOT_READY_TEXT)
        self.assertEqual(mod.payload_error({'status': 404}), mod.NOT_READY_TEXT)
        self.assertEqual(mod.payload_error({}), mod.NOT_READY_TEXT)
        failure = mod.payload_error({'ok': False, 'message': '磁盘只读'})
        self.assertIn('磁盘只读', failure)

    def test_export_error_accepts_the_export_shape(self):
        mod = self.mod
        self.assertEqual(mod.export_error(export_payload()), '')
        self.assertEqual(mod.export_error({'detail': 'Not Found'}), mod.NOT_READY_TEXT)
        self.assertEqual(mod.export_error({}), mod.NOT_READY_TEXT)

    def test_write_export_file_round_trip(self):
        mod = self.mod
        tmp = tempfile.TemporaryDirectory(prefix='w13b-pure-')
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / 'out.json'
        payload = export_payload()
        self.assertEqual(mod.write_export_file(str(target), payload), len(SAMPLE))
        data = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(data['count'], len(SAMPLE))
        self.assertFalse(target.read_bytes().startswith(b'\xef\xbb\xbf'))


if __name__ == '__main__':
    unittest.main()
