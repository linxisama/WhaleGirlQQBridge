"""W13b：桌面「日志」页 —— 只读浏览 /api/logs 的错误与关键事件，不碰后端文件。

三张卡共用的冻结契约（字段名逐字不改）：
    entry = {"id","ts","level","source","message","detail","code","extra"}
    端点：GET logs?level=&source=&query=&limit=
          GET logs/export?level=&source=&query=&limit=1000   （附件下载，同一份 JSON）
          POST logs/client

三条设计约定：
1. 网络请求全部经 PanelRequests 丢到工作线程、由 QTimer 回主线程应用结果；主线程
   从不阻塞。串联队列而不是丢请求：刷新 / 自动刷新 / 导出排成一条串行队列。
2. 接口未就绪（404 / 后端没起）只写一行中文状态，不抛、不白屏。
3. 关闭或页面销毁时停掉所有 QTimer 并关掉工作池，轮询不许活到窗口之外。

本模块的纯函数（时间/级别/查询串/详情文本/导出落盘）不依赖 Qt，可直接单测。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

try:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QBrush, QColor, QFont, QTextCursor
    from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                                 QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                                 QLineEdit, QPlainTextEdit, QPushButton,
                                 QTableWidget, QTableWidgetItem, QTextEdit,
                                 QVBoxLayout, QWidget, QApplication)
    _HAS_QT = True
except Exception:       # pragma: no cover - 没有 PyQt 的纯逻辑环境
    _HAS_QT = False
    QWidget = object       # type: ignore[assignment,misc]

LOGS_PATH = 'logs'
EXPORT_PATH = 'logs/export'
DEFAULT_LIMIT = 200
EXPORT_LIMIT = 1000
AUTO_REFRESH_MS = 5000
COLUMNS = ('时间', '级别', '来源', '摘要')
ALL_SOURCES_TEXT = '全部'
LEVEL_CHOICES = (('全部', ''), ('错误', 'error'), ('警告', 'warning'),
                 ('信息', 'info'), ('调试', 'debug'))
LEVEL_TEXT = {'error': '错误', 'warning': '警告', 'info': '信息', 'debug': '调试'}
# 主题：卡片/正文/弱化/边框/强调复用 src/gui/menu_style.DEFAULT_THEME（同一套浅色底），
# 日志页独有的语义色（级别、搜索命中）集中定义在这里，方法里不再出现颜色字面量。
try:
    from .menu_style import DEFAULT_THEME as _BASE_THEME
except Exception:      # pragma: no cover - 脱离包运行时兜底
    _BASE_THEME = {'card': '#FFFFFF', 'text': '#1F2733', 'muted': '#5F6B7A',
                   'border': '#D3DAE3', 'accent': '#2FA8A0',
                   'accent_light': '#E2F5F2'}

THEME = {
    'card': _BASE_THEME['card'],
    'text': _BASE_THEME['text'],
    'muted': _BASE_THEME['muted'],
    'border': _BASE_THEME['border'],
    'accent': _BASE_THEME['accent'],
    'accent_light': _BASE_THEME['accent_light'],
    'error': '#b42318',
    'warning': '#b54708',
    'info': '#3a5a80',
    'debug': '#6b7280',
    'fallback': '#57606a',
    'match': '#FFF0B3',
}
# 级别列前景色：错误=红，警告=橙，信息=蓝灰，调试=灰。
LEVEL_COLORS = {'error': THEME['error'], 'warning': THEME['warning'],
                'info': THEME['info'], 'debug': THEME['debug']}
FALLBACK_COLOR = THEME['fallback']
MATCH_COLOR = THEME['match']
STATUS_ERROR_STYLE = 'color:%s;' % THEME['error']
FOLLOW_LATEST_TEXT = '跟随最新'
NOT_READY_TEXT = '日志接口还没就绪，请重启控制台后再试'
FAIL_TEXT = '日志接口请求失败：%s'
EMPTY_DETAIL_TEXT = '（这条没有附加详情）'
DETAIL_HINT = '选中一条日志看完整详情'
EXPORT_DIALOG_FILTER = 'JSON 文件 (*.json)'


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _one_line(value: Any) -> str:
    return ' '.join(_text(value).split())


def level_text(level: Any) -> str:
    """error → 错误；未知级别原样显示（空值当信息）。"""
    raw = _text(level).strip()
    return LEVEL_TEXT.get(raw.lower(), raw or LEVEL_TEXT['info'])


def level_color(level: Any) -> str:
    return LEVEL_COLORS.get(_text(level).strip().lower(), FALLBACK_COLOR)


def display_time(ts: Any) -> str:
    """2026-10-26T17:05:12+08:00 → 2026-10-26 17:05:12（只影响显示，不改数据）。"""
    raw = _text(ts).strip()
    if 'T' not in raw:
        return raw
    return raw.replace('T', ' ', 1)[:19]


def build_query(level: Any = '', source: Any = '', query: Any = '',
                limit: Any = DEFAULT_LIMIT) -> str:
    """筛选 → 查询串（空值不发，limit 一定带）。"""
    pairs: List[tuple] = []
    for key, value in (('level', level), ('source', source), ('query', query)):
        text = _text(value).strip()
        if text:
            pairs.append((key, text))
    try:
        size = int(limit)
    except Exception:
        size = DEFAULT_LIMIT
    pairs.append(('limit', str(size)))
    return urlencode(pairs, quote_via=quote)


def logs_path(level: Any = '', source: Any = '', query: Any = '',
              limit: Any = DEFAULT_LIMIT) -> str:
    return '%s?%s' % (LOGS_PATH, build_query(level, source, query, limit))


def export_path(level: Any = '', source: Any = '', query: Any = '',
                limit: Any = EXPORT_LIMIT) -> str:
    return '%s?%s' % (EXPORT_PATH, build_query(level, source, query, limit))


def entry_detail_text(entry: Any) -> str:
    """详情区正文：原始时间 级别 来源 摘要 + 换行 + 完整 detail。"""
    item = entry if isinstance(entry, dict) else {}
    head = '%s %s %s %s' % (_text(item.get('ts')).strip(), level_text(item.get('level')),
                            _text(item.get('source')).strip(), _one_line(item.get('message')))
    body = _text(item.get('detail')).rstrip()
    return '%s\n%s' % (head.strip(), body if body.strip() else EMPTY_DETAIL_TEXT)


def entry_line(entry: Any) -> str:
    """复制全部：一条一行，detail 单行化。"""
    item = entry if isinstance(entry, dict) else {}
    head = '%s %s %s %s' % (display_time(item.get('ts')), level_text(item.get('level')),
                            _text(item.get('source')).strip(), _one_line(item.get('message')))
    detail = _one_line(item.get('detail'))
    return '%s | %s' % (head.strip(), detail) if detail else head.strip()


def match_spans(text: Any, query: Any) -> List[tuple]:
    """关键词命中的字符区间：大小写不敏感，返回 [(start, end), ...]。"""
    haystack = _text(text)
    needle = _text(query).strip()
    if not haystack or not needle:
        return []
    folded, target = haystack.casefold(), needle.casefold()
    spans: List[tuple] = []
    start = folded.find(target)
    while start >= 0:
        spans.append((start, start + len(needle)))
        start = folded.find(target, start + len(needle))
    return spans


def entry_matches(entry: Any, query: Any) -> bool:
    """一条日志是否命中关键词：时间/级别/来源/摘要/详情任一命中即算。"""
    if not _text(query).strip():
        return True
    item = entry if isinstance(entry, dict) else {}
    fields = (display_time(item.get('ts')), level_text(item.get('level')),
              item.get('source'), item.get('message'), item.get('detail'))
    return any(match_spans(field, query) for field in fields)


def matched_row_count(entries: Any, query: Any) -> int:
    return sum(1 for item in (entries or []) if entry_matches(item, query))


class LogsError(Exception):
    """带一句用户可读中文的请求失败；界面层只显示 str(exc)，不再往上抛。"""


def _is_404(code: Any) -> bool:
    return _text(code).strip() in ('404', '404.0')


def _failure_text(payload: Any) -> str:
    """404 / 明确失败消息 → 一句中文；判不出失败时返回 ''。"""
    if not isinstance(payload, dict) or not payload:
        return NOT_READY_TEXT
    message = _text(payload.get('message') or payload.get('detail') or payload.get('error'))
    status = payload.get('status') or payload.get('status_code') or payload.get('code')
    if _is_404(status) or '404' in message or 'not found' in message.lower():
        return NOT_READY_TEXT
    if (payload.get('ok') is False or payload.get('success') is False) and message.strip():
        return FAIL_TEXT % _one_line(message)
    return ''


def payload_error(payload: Any) -> str:
    """列表契约 {"ok":true,"entries":[...]} 正常返回 ''，否则一句中文。"""
    if isinstance(payload, dict) and payload.get('ok') is True \
            and isinstance(payload.get('entries'), list):
        return ''
    return _failure_text(payload) or NOT_READY_TEXT


def export_error(payload: Any) -> str:
    """导出契约 {"version","exported_at","filters","count","entries"}（没有 ok 键）。"""
    if isinstance(payload, dict) and isinstance(payload.get('entries'), list):
        return ''
    return _failure_text(payload) or NOT_READY_TEXT


def parse_entries(payload: Any) -> tuple:
    """→ (entries, total, truncated)；契约不满足时抛 LogsError(中文)。"""
    error = payload_error(payload)
    if error:
        raise LogsError(error)
    entries = [item for item in payload.get('entries') or [] if isinstance(item, dict)]
    total = payload.get('total')
    if not isinstance(total, int) or isinstance(total, bool):
        total = len(entries)
    return entries, total, bool(payload.get('truncated'))


def exception_text(exc: Any) -> str:
    """工作线程冒出来的任何异常 → 一行中文（已是中文的 LogsError 原样透传）。"""
    if isinstance(exc, LogsError):
        return _one_line(exc) or NOT_READY_TEXT
    text = _one_line(exc) or exc.__class__.__name__
    lowered = text.lower()
    if '404' in text or 'not found' in lowered:
        return NOT_READY_TEXT
    return FAIL_TEXT % text


def write_export_file(target: Any, payload: Any) -> int:
    """把导出字典写成 UTF-8 无 BOM 的缩进 JSON，返回条数。"""
    error = export_error(payload)
    if error:
        raise LogsError(error)
    entries = payload.get('entries') if isinstance(payload.get('entries'), list) else []
    count = payload.get('count')
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = len(entries)
    with open(target, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return count


if _HAS_QT:

    class LogsPage(QWidget):
        """日志页：工具条 → 表格 → 详情区 → 按钮行 → 状态行。"""

        def __init__(self, api_get, api_post=None, parent=None):
            super().__init__(parent)
            self.get = api_get
            self.post = api_post
            self.entries: List[Dict[str, Any]] = []
            self.total = 0
            self.truncated = False
            self._sources: List[str] = []
            self._selected_id = ''
            self._queue: List[Dict[str, Any]] = []
            self._closed = False
            self._first_show = False
            self.follow_latest = True
            self._pending_scroll: Optional[int] = None
            self._page_state: Dict[str, Any] = {}
            self.auto_interval_ms = AUTO_REFRESH_MS
            self.build()
            from .panel_requests import PanelRequests
            self.requests = PanelRequests(self)
            self.auto_timer = QTimer(self)
            self.auto_timer.setInterval(self.auto_interval_ms)
            self.auto_timer.timeout.connect(self.refresh)
            # 首帧之后再拉，构造期不碰网络：单次定时器归属本页，销毁时一并停掉。
            self.first_timer = QTimer(self)
            self.first_timer.setSingleShot(True)
            self.first_timer.setInterval(0)
            self.first_timer.timeout.connect(self.refresh)

        # ── 界面 ────────────────────────────────────────────────────────────
        def build(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(18, 16, 18, 16)
            layout.setSpacing(10)

            self.title = QLabel('日志')
            self.title.setObjectName('logs_title')
            layout.addWidget(self.title)
            self.hint = QLabel('端点 /api/logs，只记错误与关键事件，不记聊天正文')
            self.hint.setObjectName('logs_hint')
            self.hint.setWordWrap(True)
            layout.addWidget(self.hint)

            toolbar = QHBoxLayout()
            self.combo_level = QComboBox()
            self.combo_level.setObjectName('logs_level')
            for label, value in LEVEL_CHOICES:
                self.combo_level.addItem(label, value)
            self.combo_level.setToolTip('按级别筛选（错误 / 警告 / 信息 / 调试）')
            self.combo_level.currentIndexChanged.connect(self.refresh)
            toolbar.addWidget(QLabel('级别'))
            toolbar.addWidget(self.combo_level)

            self.combo_source = QComboBox()
            self.combo_source.setObjectName('logs_source')
            self.combo_source.addItem(ALL_SOURCES_TEXT, '')
            self.combo_source.setToolTip('来源取自当前列表（qq / console / agent / …）')
            self.combo_source.currentIndexChanged.connect(self.refresh)
            toolbar.addWidget(QLabel('来源'))
            toolbar.addWidget(self.combo_source)

            self.input_query = QLineEdit()
            self.input_query.setObjectName('logs_query')
            self.input_query.setPlaceholderText('关键词（回车即查）')
            self.input_query.setClearButtonEnabled(True)
            self.input_query.returnPressed.connect(self.refresh)
            toolbar.addWidget(self.input_query, 1)

            self.btn_copy_row = QPushButton('复制选中行')
            self.btn_copy_row.setObjectName('logs_copy_row')
            self.btn_copy_row.setToolTip('把表格里选中的那一条按「时间 级别 来源 摘要」复制成一行')
            self.btn_copy_row.clicked.connect(self.action_copy_row)
            toolbar.addWidget(self.btn_copy_row)

            self.btn_refresh = QPushButton('刷新')
            self.btn_refresh.setObjectName('logs_refresh')
            self.btn_refresh.setToolTip('重新拉取列表（任何状态都能点）')
            self.btn_refresh.clicked.connect(self.refresh)
            toolbar.addWidget(self.btn_refresh)

            self.check_auto = QCheckBox('自动刷新（5 秒）')
            self.check_auto.setObjectName('logs_auto')
            self.check_auto.setToolTip('页面可见时每 5 秒重拉一次；切走就暂停')
            self.check_auto.toggled.connect(self._on_auto_toggled)
            toolbar.addWidget(self.check_auto)

            self.check_follow = QCheckBox(FOLLOW_LATEST_TEXT)
            self.check_follow.setObjectName('logs_follow')
            self.check_follow.setToolTip('新日志到达时滚到最新一条；关掉后停在当前位置，方便翻旧记录')
            self.check_follow.setChecked(self.follow_latest)
            self.check_follow.toggled.connect(self._on_follow_toggled)
            toolbar.addWidget(self.check_follow)
            layout.addLayout(toolbar)

            self.table = QTableWidget(0, len(COLUMNS))
            self.table.setObjectName('logs_table')
            self.table.setHorizontalHeaderLabels(list(COLUMNS))
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            header = self.table.horizontalHeader()
            for column in range(len(COLUMNS) - 1):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
            self.table.verticalHeader().setVisible(False)
            self.table.itemSelectionChanged.connect(self._show_selected)
            self.table.itemClicked.connect(self._show_selected)
            self.table.itemDoubleClicked.connect(self._show_selected)
            layout.addWidget(self.table, 2)

            self.detail = QPlainTextEdit()
            self.detail.setObjectName('logs_detail')
            self.detail.setReadOnly(True)
            font = QFont('Consolas')
            font.setStyleHint(QFont.StyleHint.Monospace)
            self.detail.setFont(font)
            self.detail.setPlaceholderText(DETAIL_HINT)
            layout.addWidget(self.detail, 1)

            buttons = QHBoxLayout()
            self.btn_copy_one = QPushButton('复制这条')
            self.btn_copy_one.setObjectName('logs_copy_one')
            self.btn_copy_one.clicked.connect(self.action_copy_one)
            buttons.addWidget(self.btn_copy_one)
            self.btn_copy_all = QPushButton('复制全部')
            self.btn_copy_all.setObjectName('logs_copy_all')
            self.btn_copy_all.clicked.connect(self.action_copy_all)
            buttons.addWidget(self.btn_copy_all)
            self.btn_export = QPushButton('导出 JSON…')
            self.btn_export.setObjectName('logs_export')
            self.btn_export.clicked.connect(self.action_export)
            buttons.addWidget(self.btn_export)
            self.btn_clear = QPushButton('清空列表')
            self.btn_clear.setObjectName('logs_clear')
            self.btn_clear.setToolTip('只清这张表，不删后端日志文件')
            self.btn_clear.clicked.connect(self.action_clear_list)
            buttons.addWidget(self.btn_clear)
            buttons.addStretch(1)
            layout.addLayout(buttons)

            self.status_label = QLabel('')
            self.status_label.setObjectName('logs_status')
            self.status_label.setWordWrap(True)
            layout.addWidget(self.status_label)
            self._apply_style()

        def _apply_style(self) -> None:
            """颜色只从 THEME 取：表格/详情/状态行与外壳同一套浅色底。"""
            self.setStyleSheet(f"""
                QLabel#logs_title {{ color:{THEME['text']}; font-size:18px; font-weight:600; }}
                QLabel#logs_hint {{ color:{THEME['muted']}; font-size:11px; }}
                QLabel#logs_status {{ color:{THEME['muted']}; font-size:11px; }}
                QTableWidget#logs_table {{ background:{THEME['card']}; color:{THEME['text']};
                    border:1px solid {THEME['border']}; border-radius:8px;
                    gridline-color:{THEME['border']}; }}
                QTableWidget#logs_table::item:selected {{ background:{THEME['accent_light']};
                    color:{THEME['text']}; }}
                QPlainTextEdit#logs_detail {{ background:{THEME['card']}; color:{THEME['text']};
                    border:1px solid {THEME['border']}; border-radius:8px; padding:6px; }}
            """)

        # ── 筛选 / 请求 ─────────────────────────────────────────────────────
        def current_filters(self) -> Dict[str, str]:
            return {
                'level': _text(self.combo_level.currentData()).strip(),
                'source': _text(self.combo_source.currentData()).strip(),
                'query': self.input_query.text().strip(),
            }

        def refresh(self, *_ignored) -> bool:
            """刷新 / 回车 / 自动刷新 / 切筛选：排队一次列表请求（忙时排队不丢）。"""
            if self._closed:
                return False
            spec = self.current_filters()
            path = logs_path(spec['level'], spec['source'], spec['query'], DEFAULT_LIMIT)
            self._queue.append({'path': path,
                                'apply': partial(self._apply_guard,
                                                 partial(self._apply_list, spec))})
            self._pump()
            return True

        def _pump(self) -> None:
            if self._closed or not self._queue or self.requests.busy:
                return
            item = self._queue.pop(0)
            path = item['path']
            if not self.requests.start(lambda: self.get(path), item['apply'], self._fail):
                self._queue.insert(0, item)

        def _apply_guard(self, handler, result) -> None:
            try:
                handler(result)
            finally:
                self._pump()

        def _fail(self, exc) -> None:
            self._set_status(exception_text(exc), error=True)
            self._pump()

        def _set_status(self, text: str, error: bool = False) -> None:
            self.status_label.setText(text)
            self.status_label.setStyleSheet(STATUS_ERROR_STYLE if error else '')

        # ── 渲染 ────────────────────────────────────────────────────────────
        def _apply_list(self, spec, payload) -> None:
            try:
                entries, total, truncated = parse_entries(payload)
            except LogsError as exc:
                self._fail(exc)
                return
            self.entries = entries
            self.total = total
            self.truncated = truncated
            self._render()

        def _render(self) -> None:
            self._fill_sources(self.entries)
            query = self.current_filters()['query']
            bar = self.table.verticalScrollBar()
            previous = bar.value()
            self.table.setRowCount(len(self.entries))
            for row, entry in enumerate(self.entries):
                cells = (QTableWidgetItem(display_time(entry.get('ts'))),
                         QTableWidgetItem(level_text(entry.get('level'))),
                         QTableWidgetItem(_text(entry.get('source')).strip()),
                         QTableWidgetItem(_one_line(entry.get('message'))))
                cells[1].setForeground(QBrush(QColor(level_color(entry.get('level')))))
                ident = _text(entry.get('id'))
                for column, cell in enumerate(cells):
                    cell.setData(Qt.ItemDataRole.UserRole, ident)
                    if match_spans(cell.text(), query):
                        cell.setBackground(QBrush(QColor(MATCH_COLOR)))
                        cell.setToolTip('关键词命中：%s' % query)
                    self.table.setItem(row, column, cell)
            keep = -1
            if self._selected_id:
                for row, entry in enumerate(self.entries):
                    if _text(entry.get('id')) == self._selected_id:
                        keep = row
                        break
            if keep >= 0:
                self.table.selectRow(keep)
            else:
                self._selected_id = ''
                self.detail.setPlaceholderText(DETAIL_HINT)
                self.detail.setPlainText('')
            target = previous if self._pending_scroll is None else self._pending_scroll
            self._pending_scroll = None
            if self.follow_latest:
                self._scroll_to_latest()
            else:
                bar.setValue(min(target, bar.maximum()))
            shown = len(self.entries)
            message = '共 %d 条（显示最近 %d 条）' % (self.total, shown)
            if self.truncated:
                message += '，已截断'
            hits = matched_row_count(self.entries, query)
            if query:
                message += '，关键词命中 %d 条' % hits
            if not self.follow_latest:
                message += '，已暂停跟随最新'
            # 选中行没变时 itemSelectionChanged 不会重发，高亮要在这里按新关键词重刷。
            self._highlight_detail()
            self._set_status(message)

        def _fill_sources(self, entries) -> None:
            """来源下拉：从返回条目里收集去重，并且绝不丢用户当前的选择。"""
            current = _text(self.combo_source.currentData()).strip()
            for entry in entries:
                source = _text(entry.get('source')).strip()
                if source and source not in self._sources:
                    self._sources.append(source)
            names = list(self._sources)
            if current and current not in names:
                names.append(current)
            blocked = self.combo_source.blockSignals(True)
            try:
                self.combo_source.clear()
                self.combo_source.addItem(ALL_SOURCES_TEXT, '')
                for name in names:
                    self.combo_source.addItem(name, name)
                index = self.combo_source.findData(current) if current else 0
                self.combo_source.setCurrentIndex(index if index >= 0 else 0)
            finally:
                self.combo_source.blockSignals(blocked)

        def _show_selected(self, *_ignored) -> None:
            row = self.table.currentRow()
            if row < 0 or row >= len(self.entries):
                return
            entry = self.entries[row]
            self._selected_id = _text(entry.get('id'))
            self.detail.setPlaceholderText('')
            self.detail.setPlainText(entry_detail_text(entry))
            self._highlight_detail()

        def _highlight_detail(self) -> None:
            """详情区里同样高亮关键词（大小写不敏感）；没有关键词就清空高亮。"""
            query = self.current_filters()['query']
            selections = []
            text = self.detail.toPlainText()
            for start, end in match_spans(text, query):
                selection = QTextEdit.ExtraSelection()
                selection.format.setBackground(QBrush(QColor(MATCH_COLOR)))
                cursor = self.detail.textCursor()
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                selection.cursor = cursor
                selections.append(selection)
            self.detail.setExtraSelections(selections)

        def _scroll_to_latest(self) -> None:
            if self._closed:
                return
            self.table.scrollToBottom()
            bar = self.table.verticalScrollBar()
            bar.setValue(bar.maximum())

        def _on_follow_toggled(self, checked) -> None:
            self.follow_latest = bool(checked)
            if self.follow_latest:
                self._scroll_to_latest()
            self._set_status('跟随最新：开' if self.follow_latest else '跟随最新：关（列表不再自动滚动）')

        # ── 动作 ────────────────────────────────────────────────────────────
        def action_copy_one(self) -> None:
            text = self.detail.toPlainText().strip()
            if not text:
                self._set_status('先在列表里选中一条日志，再复制')
                return
            QApplication.clipboard().setText(text)
            self._set_status('已复制 1 条')

        def action_copy_row(self) -> None:
            """复制表格里选中的那一行（不用先点「复制这条」拿详情）。"""
            row = self.table.currentRow()
            if row < 0 or row >= len(self.entries):
                self._set_status('先在列表里选中一行，再复制')
                return
            QApplication.clipboard().setText(entry_line(self.entries[row]))
            self._set_status('已复制选中行（第 %d 行）' % (row + 1))

        def action_copy_all(self) -> None:
            if not self.entries:
                self._set_status('列表里还没有日志，没有可复制的内容')
                return
            QApplication.clipboard().setText('\n'.join(entry_line(item)
                                                     for item in self.entries))
            self._set_status('已复制 %d 条' % len(self.entries))

        def action_export(self) -> None:
            if self._closed:
                return
            spec = self.current_filters()
            default_name = 'app-log-%s.json' % datetime.now().strftime('%Y%m%d-%H%M%S')
            target, _selected = QFileDialog.getSaveFileName(
                self, '导出日志', default_name, EXPORT_DIALOG_FILTER)
            if not target:
                self._set_status('已取消导出')
                return
            path = export_path(spec['level'], spec['source'], spec['query'], EXPORT_LIMIT)
            self._queue.append({'path': path,
                                'apply': partial(self._apply_guard,
                                                 partial(self._apply_export, target))})
            self._set_status('正在导出到 %s …' % os.path.basename(_text(target)))
            self._pump()

        def _apply_export(self, target, payload) -> None:
            try:
                count = write_export_file(target, payload)
            except LogsError as exc:
                self._fail(exc)
                return
            except OSError as exc:
                self._fail(exc)
                return
            self._set_status('已导出 %d 条到 %s' % (count, os.path.basename(_text(target))))

        def action_clear_list(self) -> None:
            """只清界面，不碰后端日志文件。"""
            self.entries = []
            self.total = 0
            self.truncated = False
            self._selected_id = ''
            self.table.setRowCount(0)
            self.detail.setPlainText('')
            self.detail.setPlaceholderText(DETAIL_HINT)
            self._set_status('列表已清空（后端日志文件没动，点刷新会重新读回来）')

        # ── 页面状态 ────────────────────────────────────────────────────────
        def page_state(self) -> Dict[str, Any]:
            """轻量状态字典：切页再回来靠它恢复筛选、选中行与滚动位置。"""
            bar = self.table.verticalScrollBar()
            return {
                'level': _text(self.combo_level.currentData()).strip(),
                'source': _text(self.combo_source.currentData()).strip(),
                'query': self.input_query.text().strip(),
                'selected_id': self._selected_id,
                'scroll': int(bar.value()),
                'follow': bool(self.check_follow.isChecked()),
            }

        def apply_state(self, state: Any) -> None:
            """把 page_state() 装回控件；信号全程静音，不触发多余请求。"""
            data = state if isinstance(state, dict) else {}
            level, source = self.combo_level, self.combo_source
            follow = self.check_follow
            blocked = (level.blockSignals(True), source.blockSignals(True),
                       follow.blockSignals(True))
            try:
                index = level.findData(_text(data.get('level')).strip())
                level.setCurrentIndex(index if index >= 0 else 0)
                name = _text(data.get('source')).strip()
                index = source.findData(name) if name else 0
                source.setCurrentIndex(index if index >= 0 else 0)
                self.input_query.setText(_text(data.get('query')))
                value = data.get('follow')
                follow.setChecked(True if value is None else bool(value))
                self.follow_latest = follow.isChecked()
                self._selected_id = _text(data.get('selected_id'))
            finally:
                level.blockSignals(blocked[0])
                source.blockSignals(blocked[1])
                follow.blockSignals(blocked[2])
            scroll = data.get('scroll')
            self._pending_scroll = scroll if isinstance(scroll, int) else None
            bar = self.table.verticalScrollBar()
            if self._pending_scroll is not None:
                bar.setValue(min(self._pending_scroll, bar.maximum()))

        # ── 生命周期 ────────────────────────────────────────────────────────
        def _on_auto_toggled(self, checked) -> None:
            if checked and self.isVisible():
                self.auto_timer.start()
            else:
                self.auto_timer.stop()

        def showEvent(self, event):
            super().showEvent(event)
            if self._page_state:
                self.apply_state(self._page_state)
            if self.check_auto.isChecked():
                self.auto_timer.start()
            if not self._first_show:
                self._first_show = True
                self.first_timer.start()

        def hideEvent(self, event):
            self._page_state = self.page_state()
            self.auto_timer.stop()
            super().hideEvent(event)

        def shutdown(self) -> None:
            """关窗 / 销毁路径：停表、清队列、关工作池，之后不再发请求。"""
            self._closed = True
            self._queue = []
            for timer in (getattr(self, 'auto_timer', None),
                          getattr(self, 'first_timer', None),
                          getattr(getattr(self, 'requests', None), 'timer', None)):
                if timer is not None:
                    timer.stop()
            requests = getattr(self, 'requests', None)
            if requests is not None:
                try:
                    requests.close()
                except Exception:       # noqa: BLE001 - 关窗路径不许抛
                    pass

        def closeEvent(self, event):
            self.shutdown()
            super().closeEvent(event)
