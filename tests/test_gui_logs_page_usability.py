"""日志页可用性回归：级别过滤、关键词高亮、复制选中行、跟随最新、切页状态。

离屏（QT_QPA_PLATFORM=offscreen）跑；没有 PyQt6 时只跑纯函数部分。
不碰网络、不碰后端日志文件：api_get 是注入的假函数。
"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from PyQt6.QtGui import QColor
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget
    HAVE_QT = True
except Exception:                   # pragma: no cover - 环境缺 PyQt6
    HAVE_QT = False

_APP = None


def _app():
    """必须持有模块级引用：QApplication 被回收后再造 QWidget 会直接终止进程。"""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def entry(ident, level, message, source='qq', detail=''):
    return {'id': ident, 'ts': '2026-10-26T17:05:12+08:00', 'level': level,
            'source': source, 'message': message, 'detail': detail,
            'code': '', 'extra': {}}


SAMPLE = [
    entry('e1', 'error', 'Connection RESET by peer', detail='Traceback: boom'),
    entry('e2', 'warning', '后端端口探活超时一次', source='console'),
    entry('e3', 'info', '打开了控制台', source='gui'),
    entry('e4', 'debug', '入站消息已解析'),
]


def ok_payload(entries):
    items = [dict(item) for item in entries]
    return {'ok': True, 'entries': items, 'total': len(items), 'truncated': False}


def filtered_payload(entries):
    """按路径里的 level= 过滤，模拟后端行为。"""
    def payload_for(path):
        for level in ('error', 'warning', 'info', 'debug'):
            if 'level=%s' % level in path:
                return ok_payload([item for item in entries if item['level'] == level])
        return ok_payload(entries)
    return payload_for


class FakeApi(object):
    def __init__(self, payload):
        self.payload = payload
        self.paths = []

    def __call__(self, path, *_args, **_kwargs):
        self.paths.append(path)
        return self.payload(path) if callable(self.payload) else self.payload


class PureLogicTests(unittest.TestCase):
    """不依赖 Qt：命中区间 / 命中判定 / 命中条数。"""

    @classmethod
    def setUpClass(cls):
        from src.gui import logs_page
        cls.mod = logs_page

    def test_module_imports_without_qt_instance(self):
        self.assertTrue(hasattr(self.mod, 'match_spans'))
        self.assertTrue(hasattr(self.mod, 'entry_matches'))
        self.assertTrue(hasattr(self.mod, 'matched_row_count'))

    def test_match_spans_is_case_insensitive_and_finds_all(self):
        self.assertEqual(self.mod.match_spans('Connection RESET by peer', 'reset'),
                         [(11, 16)])
        self.assertEqual(self.mod.match_spans('aXbXc', 'x'), [(1, 2), (3, 4)])

    def test_match_spans_empty_query_matches_nothing(self):
        self.assertEqual(self.mod.match_spans('anything', ''), [])
        self.assertEqual(self.mod.match_spans('anything', '   '), [])
        self.assertEqual(self.mod.match_spans(None, 'x'), [])

    def test_entry_matches_covers_every_displayed_field(self):
        item = SAMPLE[0]
        self.assertTrue(self.mod.entry_matches(item, 'RESET'), '摘要命中')
        self.assertTrue(self.mod.entry_matches(item, 'traceback'), '详情命中（大小写不敏感）')
        self.assertTrue(self.mod.entry_matches(item, '错误'), '级别中文名命中')
        self.assertFalse(self.mod.entry_matches(item, '没有这个词'))
        self.assertTrue(self.mod.entry_matches(item, ''), '空关键词=全放行')

    def test_matched_row_count(self):
        self.assertEqual(self.mod.matched_row_count(SAMPLE, '连接'), 0)
        self.assertEqual(self.mod.matched_row_count(SAMPLE, 'reset'), 1)
        self.assertEqual(self.mod.matched_row_count(SAMPLE, ''), 4)

    def test_theme_is_one_place_and_level_colors_come_from_it(self):
        self.assertEqual(self.mod.LEVEL_COLORS['error'], self.mod.THEME['error'])
        self.assertEqual(self.mod.FALLBACK_COLOR, self.mod.THEME['fallback'])
        self.assertEqual(self.mod.MATCH_COLOR, self.mod.THEME['match'])


@unittest.skipUnless(HAVE_QT, '需要 PyQt6')
class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        from src.gui import logs_page
        cls.mod = logs_page

    def setUp(self):
        self.pages = []

    def tearDown(self):
        for page in self.pages:
            try:
                page.shutdown()
                page.hide()
                page.deleteLater()
                _app().processEvents()
            except RuntimeError:        # C++ 对象已经没了
                pass

    def make_page(self, entries=None, payload=None, size=(760, 420)):
        api = FakeApi(payload if payload is not None else ok_payload(entries or SAMPLE))
        page = self.mod.LogsPage(api, lambda *a, **k: {'ok': True})
        self.pages.append(page)
        page.resize(*size)
        page.show()
        QTest.qWait(120)
        return page, api

    def many_entries(self, count=60):
        return [entry('m%d' % index, 'info', '第 %d 条日志' % index) for index in range(count)]

    # a) 过滤后行数正确 ---------------------------------------------------
    def test_level_filter_shows_only_matching_rows(self):
        page, api = self.make_page(payload=filtered_payload(SAMPLE))
        self.assertEqual(page.table.rowCount(), 4)
        page.combo_level.setCurrentIndex(page.combo_level.findData('warning'))
        QTest.qWait(150)
        self.assertEqual(page.table.rowCount(), 1)
        self.assertEqual(page.table.item(0, 1).text(), '警告')
        self.assertIn('level=warning', api.paths[-1])
        page.combo_level.setCurrentIndex(0)
        QTest.qWait(150)
        self.assertEqual(page.table.rowCount(), 4)

    def test_query_highlight_marks_matching_cells_case_insensitively(self):
        page, _api = self.make_page()
        page.input_query.setText('reset')
        page.refresh()
        QTest.qWait(200)
        marked = []
        for row in range(page.table.rowCount()):
            for column in range(page.table.columnCount()):
                cell = page.table.item(row, column)
                colour = QColor(cell.background().color()).name()
                if colour == self.mod.MATCH_COLOR.lower():
                    marked.append((row, column))
        self.assertEqual(marked, [(0, 3)], '只有命中「RESET」的摘要格要上底色')
        self.assertIn('关键词命中 1 条', page.status_label.text())

    def test_query_miss_leaves_every_cell_unhighlighted(self):
        page, _api = self.make_page()
        page.input_query.setText('完全不存在的词')
        page.refresh()
        QTest.qWait(200)
        colours = {QColor(page.table.item(row, col).background().color()).name()
                   for row in range(page.table.rowCount())
                   for col in range(page.table.columnCount())}
        self.assertNotIn(self.mod.MATCH_COLOR.lower(), colours)

    def test_detail_pane_highlights_matches(self):
        page, _api = self.make_page()
        page.input_query.setText('traceback')
        page.refresh()
        QTest.qWait(200)
        page.table.selectRow(0)
        self.assertTrue(page.detail.extraSelections(), '详情区也要高亮')
        page.input_query.setText('')
        page.refresh()
        QTest.qWait(200)
        page.table.selectRow(0)
        self.assertFalse(page.detail.extraSelections(), '没有关键词就不留高亮')

    # b) 复制选中行 -------------------------------------------------------
    def test_copy_selected_row_puts_one_line_on_clipboard(self):
        page, _api = self.make_page()
        page.table.selectRow(0)
        QApplication.clipboard().setText('')
        page.btn_copy_row.click()
        clip = QApplication.clipboard().text()
        self.assertIn('Connection RESET by peer', clip)
        self.assertIn('错误', clip)
        self.assertNotIn('\n', clip, '选中行复制成一行')
        self.assertIn('已复制选中行', page.status_label.text())

    def test_copy_selected_row_without_selection_does_not_touch_clipboard(self):
        page, _api = self.make_page()
        QApplication.clipboard().setText('sentinel')
        page.table.clearSelection()
        page.table.setCurrentCell(-1, -1)
        page.btn_copy_row.click()
        self.assertEqual(QApplication.clipboard().text(), 'sentinel')
        self.assertIn('先', page.status_label.text())

    # c) 跟随最新 ---------------------------------------------------------
    def test_follow_latest_off_keeps_scroll_position_when_rows_append(self):
        entries = self.many_entries(60)
        page, _api = self.make_page(entries=entries)
        page.check_follow.setChecked(False)
        bar = page.table.verticalScrollBar()
        self.assertGreater(bar.maximum(), 20, '先要有可滚动的行数')
        bar.setValue(7)
        QTest.qWait(50)
        page._apply_list(page.current_filters(), ok_payload(entries + self.many_entries(20)))
        QTest.qWait(80)
        self.assertEqual(page.table.rowCount(), 80)
        self.assertEqual(bar.value(), 7, '关闭跟随后追加日志不许把视图拽走')

    def test_follow_latest_on_scrolls_to_bottom(self):
        page, _api = self.make_page(entries=self.many_entries(60))
        bar = page.table.verticalScrollBar()
        bar.setValue(0)
        page._apply_list(page.current_filters(), ok_payload(self.many_entries(80)))
        QTest.qWait(80)
        self.assertEqual(bar.value(), bar.maximum(), '开着跟随就停在最新一条')
        self.assertNotIn('已暂停跟随最新', page.status_label.text())

    # d) 切页再回来 -------------------------------------------------------
    def test_page_state_round_trip_keeps_filters_and_scroll(self):
        entries = self.many_entries(60)
        page, _api = self.make_page(entries=entries)
        page.combo_level.setCurrentIndex(page.combo_level.findData('info'))
        page.input_query.setText('日志')
        page.check_follow.setChecked(False)
        bar = page.table.verticalScrollBar()
        bar.setValue(5)
        QTest.qWait(80)
        state = page.page_state()
        self.assertEqual(state['level'], 'info')
        self.assertEqual(state['query'], '日志')
        self.assertFalse(state['follow'])
        self.assertEqual(state['scroll'], 5)

        # 模拟「切到别的页再回来」：先被清空，再按状态字典装回去
        page.combo_level.setCurrentIndex(0)
        page.input_query.setText('')
        page.check_follow.setChecked(True)
        bar.setValue(0)
        page.apply_state(state)
        QTest.qWait(80)
        self.assertEqual(page.combo_level.currentData(), 'info')
        self.assertEqual(page.input_query.text(), '日志')
        self.assertFalse(page.check_follow.isChecked())
        self.assertEqual(bar.value(), 5)

    def test_hide_then_show_restores_filters(self):
        page, _api = self.make_page()
        page.combo_level.setCurrentIndex(page.combo_level.findData('warning'))
        page.input_query.setText('探活')
        page.hide()
        self.assertEqual(page._page_state['level'], 'warning', 'hide 时把状态存下来')
        page.combo_level.setCurrentIndex(0)
        page.input_query.setText('')
        page.show()
        QTest.qWait(120)
        self.assertEqual(page.combo_level.currentData(), 'warning')
        self.assertEqual(page.input_query.text(), '探活')

    def test_apply_state_does_not_fire_extra_requests(self):
        page, api = self.make_page()
        calls = len(api.paths)
        page.apply_state({'level': 'error', 'source': '', 'query': '失败',
                          'selected_id': '', 'scroll': 0, 'follow': True})
        QTest.qWait(150)
        self.assertEqual(len(api.paths), calls, '装状态只改控件，不自己发请求')

    # e) 实例化 -----------------------------------------------------------
    def test_instantiation_exposes_new_controls(self):
        page, _api = self.make_page()
        for name in ('logs_follow', 'logs_copy_row'):
            self.assertIsNotNone(page.findChild(QWidget, name), '缺少控件 %s' % name)
        self.assertTrue(page.check_follow.isChecked(), '默认跟随最新')
        self.assertEqual(page.check_follow.text(), self.mod.FOLLOW_LATEST_TEXT)


if __name__ == '__main__':
    unittest.main()
