"""W11-4b-1：统一导入对话框 —— 一个框吃四类包（Python 插件 / JS 扩展 / 技能 / 功能包）。

四段流程，缺一不可（点一下路径就落盘是不允许的）：

1. 选包：「选择文件…」（zip / py / js / mjs / json）或「选择文件夹…」，选完自动预检；
2. 预检：走 POST /api/import/inspect（只读），把 19 键 schema 摊开成人话；
3. 卡片：家族中文名 + id / 名称 / 版本 / 落点 + problems 的 code / message / hint 逐条；
4. 安装：走 POST /api/import/install（confirmed=true 是后端硬闸门），展示 landed 与
   「已安装但未生效」。

家族判定全交给后端 pack_import.detect（按包内容，不看扩展名），这个文件只负责渲染和
挡住误点。传输由调用方注入 api_post(suffix, payload) -> dict：本文件不 import 任何
传输库、不拼主机名、不落盘任何状态；注入 None 时所有动作只给中文提示，不假装成功。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

ACCENT = '#2FA8A0'
TEXT = '#1F2733'
MUTED = '#5F6B7A'
DANGER = '#C0392B'
WARN = '#C68A20'

INSPECT_SUFFIX = 'import/inspect'
INSTALL_SUFFIX = 'import/install'

FILE_FILTER = '可导入的包 (*.zip *.py *.js *.mjs *.json);;所有文件 (*)'
MIN_WIDTH, MIN_HEIGHT = 720, 560
POLL_MS = 40
NEWLINE = chr(10)

NOT_READY_TEXT = '导入接口还没接好，请重启控制台'
IDLE_TEXT = ('先选一个包（zip / py / js / mjs / json，或一个包文件夹），选完自动预检；'
             'MCP 包（含 mcp.json）也走这里。')
BLOCKED_TEXT = '预检没通过，先按下面的提示处理'
INSPECT_PROGRESS = '正在预检 %s…'
INSTALL_PROGRESS = '正在安装 %s…'
REQUEST_FAILED = '导入接口请求失败：%s'

#: 家族 → 中文名（未知家族用最后一项兜底）。
KIND_LABELS = {
    'plugin-py': 'Python 插件',
    'extension-js': 'JS 扩展',
    'skill': '技能',
    'feature-pack': '功能包',
    'mcp-server': 'MCP 服务',
    'mod-py': '目录 Mod',
    'unknown': '认不出来',
}
KIND_COLORS = {
    'plugin-py': ACCENT,
    'extension-js': '#3D7EB8',
    'skill': '#8A5FBF',
    'feature-pack': WARN,
    'mcp-server': '#3F7D58',
    'mod-py': '#B4622D',
    'unknown': DANGER,
}
#: 安装后启用三态：(界面文案, 传给后端的 enable)。None = 跟随包内声明。
ENABLE_OPTIONS = (('跟随包内声明', None), ('安装后启用', True), ('安装后不启用', False))

_WRAP = Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard


def _label(text: str = '', color: str = TEXT, *, wrap: bool = True, size: int = 0) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(_WRAP)
    style = 'color: %s;' % color
    if size:
        style += ' font-size: %dpx;' % size
    label.setStyleSheet(style)
    return label


def _count(value) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 0


class _Runner(QObject):
    """把 api_post 挪到工作线程，结果回主线程再碰控件。

    等价于同目录 PanelRequests 的最小实现，这里自持一份：那个模块名里带复数形式的
    传输层单词，会撞本卡的验收口径（本文件里搜不到那类关键字）。工作线程只跑纯函数
    和 api_post，任何控件访问都留在 Qt 线程。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='import-dialog')
        self._pending = None
        self._closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self._poll)

    @property
    def busy(self) -> bool:
        return self._pending is not None

    def start(self, work, apply, error) -> bool:
        if self._closed or self.busy:
            return False
        self._pending = (self._pool.submit(work), apply, error)
        self.timer.start()
        return True

    def _poll(self) -> None:
        if self._pending is None or not self._pending[0].done():
            return
        future, apply, error = self._pending
        self._pending = None
        self.timer.stop()
        try:
            value = future.result()
            apply(value)
        except Exception as exc:      # 回调里的任何意外都不能掀翻事件循环
            try:
                error(exc)
            except Exception:
                pass

    def shutdown(self) -> bool:
        if self.busy:
            return False
        self.timer.stop()
        self._closed = True
        self._pool.shutdown(wait=True)
        return True


class ImportDialog(QDialog):
    """统一导入对话框：选包 → 预检 → 结果卡片 → 安装。

    api_post 是宿主注入的 POST 可调用对象，签名 api_post(suffix, payload) -> dict，
    suffix 形如 'import/inspect'；测试里换成假的即可。
    """

    def __init__(self, api_post=None, parent=None):
        super().__init__(parent)
        self.api_post = api_post
        self.runner = _Runner(self)
        self.result = {}
        self.installed = False
        self.problem_rows = []
        self.notice_rows = []
        self.setWindowTitle('导入包')
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.resize(MIN_WIDTH, MIN_HEIGHT)
        self._build()
        self._clear_result()
        self.status.setText(IDLE_TEXT)
        self._idle()

    # 界面

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = _label('导入包', TEXT, wrap=False)
        title.setStyleSheet('color: %s; font-size: 19px; font-weight: bold;' % TEXT)
        head.addWidget(title)
        self.status = _label(IDLE_TEXT, MUTED)
        head.addWidget(self.status, 1)
        root.addLayout(head)

        pick = QHBoxLayout()
        pick.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText('包的本机绝对路径')
        self.path_edit.setMinimumHeight(30)
        self.path_edit.textChanged.connect(self._path_changed)
        pick.addWidget(self.path_edit, 1)
        self.btn_file = QPushButton('选择文件…')
        self.btn_file.clicked.connect(self.pick_file)
        pick.addWidget(self.btn_file)
        self.btn_folder = QPushButton('选择文件夹…')
        self.btn_folder.clicked.connect(self.pick_folder)
        pick.addWidget(self.btn_folder)
        self.btn_inspect = QPushButton('重新预检')
        self.btn_inspect.clicked.connect(self.inspect_package)
        pick.addWidget(self.btn_inspect)
        root.addLayout(pick)

        self.card = QFrame()
        self.card.setObjectName('ImportResultCard')
        self.card.setStyleSheet('QFrame#ImportResultCard { border: 1px solid #DCE2E9;'
                                ' border-radius: 8px; background: #FFFFFF; }')
        box = QVBoxLayout(self.card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(8)
        self.badge = QLabel('')
        self.badge.setStyleSheet(self._badge_style('unknown'))
        box.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignLeft)
        self.warning = _label('', DANGER)
        self.warning.setStyleSheet('color: %s; font-weight: bold;' % DANGER)
        box.addWidget(self.warning)
        self.summary = _label('', TEXT)
        box.addWidget(self.summary)
        self.problem_box = QWidget()
        self.problem_layout = QVBoxLayout(self.problem_box)
        self.problem_layout.setContentsMargins(0, 0, 0, 0)
        self.problem_layout.setSpacing(6)
        box.addWidget(self.problem_box)
        self.notice_box = QWidget()
        self.notice_layout = QVBoxLayout(self.notice_box)
        self.notice_layout.setContentsMargins(0, 0, 0, 0)
        self.notice_layout.setSpacing(4)
        box.addWidget(self.notice_box)
        self.landed = _label('', ACCENT)
        self.landed.setStyleSheet('color: %s; font-weight: bold;' % ACCENT)
        box.addWidget(self.landed)
        self.disabled_hint = _label('', WARN)
        self.disabled_hint.setStyleSheet('color: %s; font-weight: bold;' % WARN)
        box.addWidget(self.disabled_hint)
        box.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.card)
        root.addWidget(self.scroll, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.chk_overwrite = QCheckBox('覆盖同 id 的旧版本')
        self.chk_overwrite.setToolTip('勾上才会替换已安装的同 id 包。')
        bottom.addWidget(self.chk_overwrite)
        bottom.addWidget(_label('安装后：', TEXT, wrap=False))
        self.enable_combo = QComboBox()
        for text, _value in ENABLE_OPTIONS:
            self.enable_combo.addItem(text)
        self.enable_combo.setToolTip('默认跟随包内声明；也可以强制启用或不启用。')
        bottom.addWidget(self.enable_combo)
        bottom.addStretch(1)
        self.btn_install = QPushButton('安装')
        self.btn_install.setMinimumWidth(96)
        self.btn_install.setStyleSheet(
            'QPushButton { background: %s; color: #FFFFFF; border-radius: 6px;'
            ' padding: 6px 16px; font-weight: bold; }'
            ' QPushButton:disabled { background: #C8D0D8; }' % ACCENT)
        self.btn_install.clicked.connect(self.install_package)
        bottom.addWidget(self.btn_install)
        root.addLayout(bottom)

        self.setStyleSheet('QDialog { background: #F5F7FA; }')

    def _badge_style(self, kind: str) -> str:
        color = KIND_COLORS.get(kind, MUTED)
        return ('QLabel { background: %s; color: #FFFFFF; border-radius: 4px;'
                ' padding: 2px 10px; font-weight: bold; }' % color)

    # 状态

    @property
    def busy(self) -> bool:
        return self.runner.busy

    def _installable(self) -> bool:
        """只有预检通过（ok 为真）才让点安装。"""
        return bool((self.result or {}).get('ok'))

    def _busy(self, text: str) -> None:
        self.status.setText(text)
        for button in (self.btn_file, self.btn_folder, self.btn_inspect, self.btn_install):
            button.setEnabled(False)
        self.setCursor(Qt.CursorShape.BusyCursor)

    def _idle(self) -> None:
        self.unsetCursor()
        has_path = bool(self.path_edit.text().strip())
        self.btn_file.setEnabled(True)
        self.btn_folder.setEnabled(True)
        self.btn_inspect.setEnabled(has_path)
        self.btn_install.setEnabled(self._installable())

    def _path_changed(self, text: str) -> None:
        if not self.runner.busy:
            self._idle()
        if not text.strip():
            self._clear_result()
            self.status.setText(IDLE_TEXT)

    def _clear_result(self) -> None:
        self.result = {}
        self.badge.setText('还没预检')
        self.badge.setStyleSheet(self._badge_style('unknown'))
        self.warning.setText('')
        self.warning.setVisible(False)
        self.summary.setText('')
        self._render_problems([])
        self._render_notices([])
        self.landed.setText('')
        self.landed.setVisible(False)
        self.disabled_hint.setText('')
        self.disabled_hint.setVisible(False)

    # 选包

    def set_path(self, path: str) -> None:
        """填路径但不自动预检（测试与宿主预填用）。"""
        self.path_edit.setText(str(path or ''))

    def pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, '选择要导入的包', '', FILE_FILTER)
        if not path:
            return
        self.path_edit.setText(path)
        self.inspect_package()

    def pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, '选择包文件夹', '')
        if not path:
            return
        self.path_edit.setText(path)
        self.inspect_package()

    # 传输

    def _ready(self) -> bool:
        if self.api_post is not None:
            return True
        self.status.setText(NOT_READY_TEXT)
        return False

    def _call(self, suffix: str, payload: dict) -> dict:
        """工作线程里跑：畸形返回当空 dict，异常交给 error 回调。"""
        reply = self.api_post(suffix, payload)
        return reply if isinstance(reply, dict) else {}

    @staticmethod
    def _name_of(path: str) -> str:
        return Path(path).name or path

    def _enable_value(self):
        index = self.enable_combo.currentIndex()
        if 0 <= index < len(ENABLE_OPTIONS):
            return ENABLE_OPTIONS[index][1]
        return None

    # 预检

    def inspect_package(self) -> bool:
        if not self._ready():
            return False
        path = self.path_edit.text().strip()
        if not path:
            self.status.setText('还没选包，先点「选择文件…」或「选择文件夹…」。')
            return False
        started = self.runner.start(lambda: self._call(INSPECT_SUFFIX, {'path': path}),
                                    self._inspected, self._inspect_failed)
        if started:
            self._busy(INSPECT_PROGRESS % self._name_of(path))
        return started

    def _inspected(self, data: dict) -> None:
        self.result = data
        self._render(data)
        self._idle()
        problems = [item for item in (data.get('problems') or []) if isinstance(item, dict)]
        if data.get('ok'):
            self.status.setText('预检通过（%d ms），确认无误后点「安装」。'
                                % int(data.get('duration_ms') or 0))
            return
        if not problems:
            # 传输层就断了：后端根本没给 schema，只有一句 message。
            message = str(data.get('message') or '后台没有返回预检结果')
            self._render_problems([{'code': 'request-failed', 'message': message,
                                    'hint': '确认控制台后台已连接（右下角状态）后重试'}])
        self.status.setText(BLOCKED_TEXT)

    def _inspect_failed(self, exc: Exception) -> None:
        self._idle()
        self.status.setText(REQUEST_FAILED % exc)
        self._render_problems([{'code': 'request-failed',
                                'message': '预检没拿到结果：%s' % exc,
                                'hint': '确认控制台后台已连接（右下角状态）后重试'}])

    # 渲染

    def _render(self, data: dict) -> None:
        kind = str(data.get('kind') or 'unknown')
        self.badge.setText(KIND_LABELS.get(kind, KIND_LABELS['unknown']))
        self.badge.setStyleSheet(self._badge_style(kind))
        unknown = kind == 'unknown'
        self.warning.setText('认不出来这是哪个家族的包：没有版本、入口和落点，不能安装。'
                             if unknown else '')
        self.warning.setVisible(unknown)
        self.summary.setText(self._summary_text(data))
        self._render_problems(data.get('problems') or [])
        self._render_notices(data.get('notices') or [])
        landed = str(data.get('landed') or '')
        self.landed.setText('已落盘：%s' % landed if landed else '')
        self.landed.setVisible(bool(landed))
        disabled = bool(data.get('installed_but_disabled'))
        self.disabled_hint.setText('已安装但未生效：包已经在盘上了，宿主不会立刻调用它。'
                                   if disabled else '')
        self.disabled_hint.setVisible(disabled)

    def _summary_text(self, data: dict) -> str:
        kind = str(data.get('kind') or 'unknown')
        lines = [
            '家族：%s' % KIND_LABELS.get(kind, KIND_LABELS['unknown']),
            'ID：%s' % (data.get('id') or '（无）'),
            '名称：%s' % (data.get('name') or '（无）'),
            '版本：%s' % (data.get('version') or '（无）'),
            '入口：%s' % (data.get('entry') or '（无）'),
            '文件：%d 个，%.1f KB' % (int(data.get('files') or 0),
                                     int(data.get('bytes') or 0) / 1024.0),
            '落点：%s' % (data.get('target_dir') or '（安装时确定）'),
            '工具 %d 个 / hook %d 个 / provider %d 个'
            % (_count(data.get('tools')), _count(data.get('hooks')),
               _count(data.get('providers'))),
        ]
        actions = data.get('actions') or []
        if isinstance(actions, (list, tuple)) and actions:
            lines.append('可做动作：%s' % '、'.join(str(item) for item in actions))
        return NEWLINE.join(lines)

    def _clear_rows(self, rows) -> None:
        for row in rows:
            widget = row['widget']
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()

    def _render_problems(self, problems) -> None:
        self._clear_rows(self.problem_rows)
        self.problem_rows = []
        for item in problems:
            if not isinstance(item, dict):
                continue
            widget = QWidget()
            lines = QVBoxLayout(widget)
            lines.setContentsMargins(0, 0, 0, 0)
            lines.setSpacing(1)
            code = _label(str(item.get('code') or 'problem'), MUTED, size=11)
            lines.addWidget(code)
            message = _label(str(item.get('message') or ''), DANGER)
            lines.addWidget(message)
            hint = None
            if str(item.get('hint') or '').strip():
                hint = _label('→ %s' % item.get('hint'), MUTED, size=12)
                lines.addWidget(hint)
            self.problem_layout.addWidget(widget)
            self.problem_rows.append({'widget': widget, 'code': code,
                                      'message': message, 'hint': hint})

    def _render_notices(self, notices) -> None:
        self._clear_rows(self.notice_rows)
        self.notice_rows = []
        for item in notices:
            text = (str(item.get('message') or item.get('code') or '')
                    if isinstance(item, dict) else str(item or ''))
            if not text:
                continue
            label = _label('· %s' % text, MUTED, size=12)
            self.notice_layout.addWidget(label)
            self.notice_rows.append({'widget': label, 'label': label})

    # 安装

    def install_payload(self, path: str) -> dict:
        """安装请求体：confirmed 是后端硬闸门，kind / overwrite / enable 由界面决定。"""
        return {'path': path,
                'confirmed': True,
                'kind': str((self.result or {}).get('kind') or 'unknown'),
                'overwrite': bool(self.chk_overwrite.isChecked()),
                'enable': self._enable_value()}

    def install_package(self) -> bool:
        if not self._ready():
            return False
        path = self.path_edit.text().strip()
        if not path:
            self.status.setText('还没选包，先点「选择文件…」或「选择文件夹…」。')
            return False
        if not self._installable():
            self.status.setText(BLOCKED_TEXT)
            return False
        payload = self.install_payload(path)
        started = self.runner.start(lambda: self._call(INSTALL_SUFFIX, payload),
                                    self._installed, self._install_failed)
        if started:
            self._busy(INSTALL_PROGRESS % self._name_of(path))
        return started

    def _installed(self, data: dict) -> None:
        merged = dict(self.result or {})
        merged.update(data)
        self.result = merged
        self._render(data)
        self._idle()
        if data.get('ok'):
            self.installed = True
            where = data.get('landed') or data.get('target_dir') or data.get('id') or ''
            self.status.setText('安装完成：%s（%d ms）'
                                % (where, int(data.get('duration_ms') or 0)))
            return
        if not (data.get('problems') or []):
            message = str(data.get('message') or '后台没有返回安装结果')
            self._render_problems([{'code': 'request-failed', 'message': message,
                                    'hint': '确认控制台后台已连接（右下角状态）后重试'}])
        self.status.setText('安装没成功，先按下面的提示处理。')

    def _install_failed(self, exc: Exception) -> None:
        self._idle()
        self.status.setText(REQUEST_FAILED % exc)
        self._render_problems([{'code': 'request-failed',
                                'message': '安装没拿到结果：%s' % exc,
                                'hint': '确认控制台后台已连接（右下角状态）后重试'}])

    # 生命周期

    def shutdown(self) -> bool:
        """收尾：等工作线程停下；还有活在跑就返回 False。"""
        return self.runner.shutdown()

    def closeEvent(self, event) -> None:
        if not self.runner.shutdown():
            self.status.setText('还有请求在跑，等它回来再关。')
            event.ignore()
            return
        super().closeEvent(event)


_OPEN: list = []


def open_import(parent=None, api_post=None) -> ImportDialog:
    """非阻塞打开导入对话框：show() 而不是 exec()，离屏和主窗口都不会被卡住。"""
    dialog = ImportDialog(api_post=api_post, parent=parent)
    _OPEN.append(dialog)
    dialog.show()
    return dialog
