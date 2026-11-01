"""W5b/W12：外部 agent 后端面板（列表 / 状态 / 探测 / 启动 / 停止 / 工作区 / profile 编辑）。

端点前缀 /api/agent/backends（实现见 src/agent_backend_api.py）。面板只做两件事：
1. 用注入的 get/post/delete（路径相对 /api/，与 qq_gui.api_get/api_post/api_delete 一致）
   发起请求；
2. 所有请求都经 PanelRequests 丢到工作线程、由 QTimer 回主线程应用结果 ——
   构造、按钮、轮询都不许阻塞 Qt 事件循环；同一时刻只跑一个请求，忙时新请求排队
   （排队而不是丢掉：任何一个点击都不会没有下文）。

W12 的可读性约定：按钮用 enabled + tooltip 说明「为什么点不了」；动作一发出就先写
一句进行中文案，结果回来再改写（成功带关键结果，失败保留错误码原文）；没有可操作
对象时也写回音 —— 面板里不留静默路径。

PyQt 未安装时仍可 import（暴露可测的状态助手）。
"""
from __future__ import annotations

import shlex
from typing import Callable, Optional
from urllib.parse import quote

try:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QBrush, QColor
    from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                                 QDoubleSpinBox, QFrame, QGridLayout,
                                 QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                                 QPlainTextEdit, QPushButton, QScrollArea,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout,
                                 QWidget)
    _HAS_QT = True
except Exception:       # pragma: no cover - 无 PyQt 的纯逻辑环境
    _HAS_QT = False
    QWidget = object       # type: ignore[assignment,misc]
    QDialog = object       # type: ignore[assignment,misc]

PANEL_PATH = 'agent/backends'
COLUMNS = ['ID', '状态', 'PID', '命令', '工作区']
STATE_LABELS = {'idle': '空闲', 'running': '运行中', 'starting': '启动中',
                'stopped': '已停止', 'failed': '失败'}
# 状态列前景色：运行中=绿，未运行=灰，失败=红（状态一眼可辨）。
STATE_COLORS = {'running': '#1a7f37', 'starting': '#9a6700',
                'stopped': '#57606a', 'idle': '#57606a',
                'failed': '#b42318', 'error': '#b42318'}
EMPTY_TEXT = ('还没有外部后端。三步就能跑起来：\n'
              '① 点「自动识别」找本机 DSH（或直接在下方表单里填命令）\n'
              '② 点「保存 profile」把它存进上面的列表\n'
              '③ 选中列表里这一行，点「启动」\n'
              '启动是真实进程（列表 PID 列能看到）；「停止」会按进程树结束它。')
POLL_INTERVAL_MS = 1500
DETECT_PATH = 'agent/backends/detect'
CANDIDATE_COLUMNS = ['ID', 'kind', 'profile', '命令', '版本', '来源', '操作']
# 候选框多一列「推荐」：profile=headless 的候选是 DSH 默认无头入口。
DIALOG_COLUMNS = ['ID', 'kind', 'profile', '命令', '版本', '来源', '推荐', '操作']
COL_ID, COL_KIND, COL_PROFILE, COL_COMMAND = 0, 1, 2, 3
COL_VERSION, COL_SOURCE, COL_RECOMMEND, COL_ACTION = 4, 5, 6, 7
REFRESHING_TEXT = '正在刷新列表…'
NO_SELECTION_TIP = '先在列表里选中一个后端'
VERSION_PROBING = '版本（探测中…）'
RECOMMEND_COLOR = '#1a7f37'
# DSH 联动：写入 / 撤销 / 只读状态三件事，前缀与后端端点一致。
DSH_PATH = PANEL_PATH + '/dsh'
DSH_STATUS_PATH = DSH_PATH + '/status'
DSH_LINK_PATH = DSH_PATH + '/link'
DSH_UNLINK_PATH = DSH_PATH + '/unlink'
DSH_DEFAULT_PROFILE = 'desktop'


def dsh_status_path(profile: str = '', repo_root: str = '') -> str:
    """GET 状态时带上 profile 与源码仓库根（空值就让后台按自己的规则处理）。"""
    query = []
    name = str(profile or '').strip()
    if name:
        query.append('profile=' + quote(name))
    repo = str(repo_root or '').strip()
    if repo:
        query.append('repo_root=' + quote(repo))
    return DSH_STATUS_PATH + ('?' + '&'.join(query) if query else '')


def detect_path(probe: bool = False) -> str:
    """探测请求路径：probe=1 才让端点真跑 --version。"""
    return '%s?probe=%d' % (DETECT_PATH, 1 if probe else 0)


def args_text(args) -> str:
    """把 args 列表写成参数行：含空白的项加双引号，供 split_args 还原。"""
    parts = []
    for item in args or []:
        text = str(item)
        parts.append('"%s"' % text if (' ' in text or '\t' in text) else text)
    return ' '.join(parts)


def split_args(text: str) -> list:
    """把参数行拆成 argv 片段：优先 shlex，引号不配对时退化为空白切分。"""
    text = (text or '').strip()
    if not text:
        return []
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        return text.split()
    # posix=False 会保留引号，这里成对去掉；Windows 路径的反斜杠不能被吃掉。
    cleaned = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
            token = token[1:-1]
        cleaned.append(token)
    return cleaned


def error_of(payload) -> Optional[tuple]:
    """把接口返回归一成 (code, message)；成功返回 None。"""
    if not isinstance(payload, dict) or not payload:
        return ('offline', '控制台后台没有响应')
    if payload.get('ok'):
        return None
    code = payload.get('code')
    if code:
        return (str(code), str(payload.get('message') or ''))
    if payload.get('success') is False:
        return ('internal', str(payload.get('message') or '接口调用失败'))
    return None


def state_of(row) -> str:
    if not isinstance(row, dict):
        return 'idle'
    state = str(row.get('state') or '')
    if state:
        return state
    return 'running' if row.get('running') else 'idle'


def pid_text(payload) -> str:
    """响应里的 pid 写成人看的形式（没有就写 -）。"""
    pid = payload.get('pid') if isinstance(payload, dict) else None
    return '-' if pid in (None, '') else str(pid)


def is_recommended(item) -> bool:
    """候选是否是推荐项（DSH 默认 headless profile）。"""
    return str((item or {}).get('profile') or '') == 'headless'


class AgentBackendPanel(QWidget):
    """外部 agent 后端页：列表 + 状态 + 四个动作 + profile 编辑 + 错误码原文回显。"""

    def __init__(self, get: Optional[Callable] = None,
                 post: Optional[Callable] = None,
                 delete: Optional[Callable] = None,
                 opener: Optional[Callable] = None,
                 parent=None):
        super().__init__(parent)
        self.get = get
        self.post = post
        self.delete = delete
        self.opener = opener
        self._rows: list = []
        self._selected_id = ''
        self._candidates: list = []
        self._scanned: list = []
        self._detect_hint = ''
        self._detect_reason = ''
        self._form_kind = 'cli'
        self._pending_select = ''
        self._form_manual = False
        self._queue: list = []
        self._dsh_loaded = False
        self.dialog = None
        self.poll_interval_ms = POLL_INTERVAL_MS
        if _HAS_QT:
            self.build()
            from .panel_requests import PanelRequests
            self.requests = PanelRequests(self)
            self._apply_selection_buttons()
            self.poll_timer = QTimer(self)
            self.poll_timer.setInterval(self.poll_interval_ms)
            self.poll_timer.timeout.connect(self.refresh)
            QTimer.singleShot(0, self.refresh)      # 首帧之后再拉，构造期不碰网络

    # ── 界面 ────────────────────────────────────────────────────────────────
    def build(self) -> None:
        # 内容区纵向可滚动：这一页的正文（列表 + 表单 + DSH 联动）在 900x600 下
        # 高于窗口，若直接堆在一个不滚动的竖直布局里，Qt 会把子控件压到最小高度
        # 以下 —— 表格被压成 0 高、表单 label 与输入框叠在一起。页头（标题 + 说明 +
        # 动作行）留在壳上不滚动，按钮永远点得到。只换父控件，
        # self.table / self.empty / self.form_title / self.input_* / self.dsh_profile
        # 等对象本身和属性名都不变。
        shell = QVBoxLayout(self)
        shell.setContentsMargins(18, 16, 18, 16)
        shell.setSpacing(10)
        self.scroll = QScrollArea()
        self.scroll.setObjectName('agent_backend_scroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 关掉横向滚动条：按钮行/表单都在 body 的最小宽度内，宁可不动也不许出现
        # 一条横条把「删除」这类最右按钮挤出可视区。
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet('QScrollArea { border: none; background: transparent; }')
        body = QWidget()
        body.setObjectName('agent_backend_body')
        body.setAutoFillBackground(False)
        self.scroll.viewport().setAutoFillBackground(False)
        layout = QVBoxLayout(body)
        # 边距归 shell：body 只留右侧 8px 给竖直滚动条，避免双重缩进。
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(10)
        # 页头（标题 + 说明 + 动作行）钉在壳上不滚动：900x600 下按钮永远点得到，
        # 也让 panel.layout() 里仍能直接找到动作行（外部依赖该结构的用例不再失效）。
        title = QLabel('外部后端')
        title.setObjectName('agent_backend_title')
        shell.addWidget(title)
        hint = QLabel('这里管理本地命令行 / DSH 后端（端点 /api/agent/backends，仅本机可访问）。'
                      '启动是真进程，停止会按进程树结束。')
        hint.setWordWrap(True)
        shell.addWidget(hint)

        buttons = QHBoxLayout()
        self.btn_refresh = QPushButton('刷新')
        self.btn_refresh.setToolTip('重新读取 profile 列表（任何状态都能点）')
        self.btn_refresh.clicked.connect(self.refresh)
        buttons.addWidget(self.btn_refresh)
        self.btn_detect = QPushButton('自动识别')
        self.btn_detect.setToolTip('只读探测本机 DSH（PATH / npm / app.asar），'
                                   '把候选一键存成 profile（任何状态都能点）')
        self.btn_detect.clicked.connect(self.action_detect)
        buttons.addWidget(self.btn_detect)
        self.btn_probe = QPushButton('探测')
        self.btn_probe.clicked.connect(self.action_probe)
        buttons.addWidget(self.btn_probe)
        self.btn_start = QPushButton('启动')
        self.btn_start.clicked.connect(self.action_start)
        buttons.addWidget(self.btn_start)
        self.btn_stop = QPushButton('停止')
        self.btn_stop.clicked.connect(self.action_stop)
        buttons.addWidget(self.btn_stop)
        self.btn_open = QPushButton('打开工作区目录')
        self.btn_open.clicked.connect(self.action_open_workspace)
        buttons.addWidget(self.btn_open)
        self.btn_delete = QPushButton('删除')
        self.btn_delete.clicked.connect(self.action_delete)
        buttons.addWidget(self.btn_delete)
        buttons.addStretch(1)
        shell.addLayout(buttons)

        self.empty = QLabel(EMPTY_TEXT)
        self.empty.setWordWrap(True)
        self.empty.setObjectName('agent_backend_empty')
        layout.addWidget(self.empty)

        self.table = QTableWidget(0, len(COLUMNS))
        # 表格至少要真的画出表头 + 一行数据（>100 设备像素），不许被布局压扁。
        self.table.setMinimumHeight(180)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.setVisible(False)
        layout.addWidget(self.table, 1)

        # 表单归属：说清这次编辑会覆盖谁。
        self.form_title = QLabel('正在编辑：新建 profile')
        self.form_title.setObjectName('agent_backend_edit_title')
        self.form_title.setWordWrap(True)
        layout.addWidget(self.form_title)

        form = QGridLayout()
        self.input_id = QLineEdit()
        self.input_id.setPlaceholderText('backendId（字母/数字/_/-，1-64）')
        self.input_command = QLineEdit()
        self.input_command.setPlaceholderText('命令（如 python.exe 或 node.exe）')
        self.input_args = QLineEdit()
        self.input_args.setPlaceholderText('参数（空白分隔，如 -c "print(1)"）')
        self.input_workdir = QLineEdit()
        self.input_workdir.setPlaceholderText('工作目录（留空用数据目录）')
        self.input_timeout = QDoubleSpinBox()
        self.input_timeout.setRange(1.0, 600.0)
        self.input_timeout.setDecimals(1)
        self.input_timeout.setValue(120.0)
        rows = [('ID', self.input_id, 0), ('命令', self.input_command, 1),
                ('参数', self.input_args, 2), ('工作目录', self.input_workdir, 3)]
        for label, widget, index in rows:
            form.addWidget(QLabel(label), index, 0)
            form.addWidget(widget, index, 1, 1, 3)
        form.addWidget(QLabel('超时(秒)'), 4, 0)
        form.addWidget(self.input_timeout, 4, 1)
        self.btn_save = QPushButton('保存 profile')
        self.btn_save.setToolTip('把下面这份表单写成 profile（同名会覆盖），保存后自动选中该行')
        self.btn_save.clicked.connect(self.save_profile)
        form.addWidget(self.btn_save, 4, 2)
        self.btn_clear_form = QPushButton('清空表单')
        self.btn_clear_form.setToolTip('清空下面五个输入框，变成「新建 profile」')
        self.btn_clear_form.clicked.connect(self.clear_form)
        form.addWidget(self.btn_clear_form, 4, 3)
        layout.addLayout(form)

        # DSH 联动：一条独立的区块（自己的 profile 下拉、三个按钮、只读命令框、回音行），
        # 与上面的后端列表解耦 —— 它改的是 DSH 的 profile，不是本控制台的 backend 列表。
        dsh_title = QLabel('DSH 联动')
        dsh_title.setObjectName('agent_dsh_title')
        layout.addWidget(dsh_title)
        dsh_hint = QLabel('把本控制台的三个 MCP 桥（记忆 / 控制 / 工具）登记进 DSH 的 profile，'
                          'DSH 的模型就能看见并调用控制台的工具。写入前会备份、重复点不会重复写；'
                          '安装版已内置 MCP 宿主，不用填仓库、也不用装 Python，点一下就能接上；'
                          '只读框给出与这次操作等价的命令行（安装版显示内置说明是正常的）。')
        dsh_hint.setWordWrap(True)
        layout.addWidget(dsh_hint)
        # 安装版自带 MCP 宿主，不再要求用户填「源码仓库根」；源码版仍自动用当前仓库。
        dsh_row = QHBoxLayout()
        dsh_row.addWidget(QLabel('profile'))
        self.dsh_profile = QComboBox()
        self.dsh_profile.setObjectName('agent_dsh_profile')
        self.dsh_profile.addItem(DSH_DEFAULT_PROFILE)
        self.dsh_profile.setToolTip('要接上的 DSH profile：列表来自 DSH 目录下真实存在的 profile')
        dsh_row.addWidget(self.dsh_profile)
        self.btn_dsh_link = QPushButton('让 DSH 接上控制台')
        self.btn_dsh_link.setObjectName('agent_dsh_link')
        self.btn_dsh_link.setToolTip('把三条 MCP 条目写进 DSH 的 cordis.patch.yml'
                                     '（先备份、只改 insert 块、重复点幂等）')
        self.btn_dsh_link.clicked.connect(self.action_dsh_link)
        dsh_row.addWidget(self.btn_dsh_link)
        self.btn_dsh_unlink = QPushButton('撤销接入')
        self.btn_dsh_unlink.setObjectName('agent_dsh_unlink')
        self.btn_dsh_unlink.setToolTip('把这三条登记从 DSH profile 里删掉（同样先备份）')
        self.btn_dsh_unlink.clicked.connect(self.action_dsh_unlink)
        dsh_row.addWidget(self.btn_dsh_unlink)
        self.btn_dsh_status = QPushButton('查看状态')
        self.btn_dsh_status.setObjectName('agent_dsh_status')
        self.btn_dsh_status.setToolTip('只读：DSH 那边现在登记了什么、有哪些 profile')
        self.btn_dsh_status.clicked.connect(self.action_dsh_status)
        dsh_row.addWidget(self.btn_dsh_status)
        self.btn_dsh_report = QPushButton('连接体检')
        self.btn_dsh_report.setObjectName('agent_dsh_connection_check')
        self.btn_dsh_report.setToolTip('只读检查当前 profile：新会话是否可用、MCP 是否登记齐、'
                                       '是否需要重启 DSH')
        self.btn_dsh_report.clicked.connect(self.action_dsh_report)
        dsh_row.addWidget(self.btn_dsh_report)
        dsh_row.addStretch(1)
        layout.addLayout(dsh_row)
        # 回音紧贴按钮行：1302x763 下原来它排在 54px 只读框之后，整个落在可视区外面，
        # 点完只看得到「没反应」。滚动区里先写结果，再放可复制的命令。
        self.dsh_feedback = QLabel('')
        self.dsh_feedback.setObjectName('agent_dsh_feedback')
        self.dsh_feedback.setWordWrap(True)
        layout.addWidget(self.dsh_feedback)
        self.dsh_report = QLabel('')
        self.dsh_report.setObjectName('agent_dsh_report')
        self.dsh_report.setWordWrap(True)
        self.dsh_report.setTextFormat(Qt.TextFormat.PlainText)
        self.dsh_report.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dsh_report.setStyleSheet('color: #334155; font-size: 12px;')
        layout.addWidget(self.dsh_report)
        self.dsh_profile.currentIndexChanged.connect(self._dsh_profile_changed)
        self.dsh_command = QPlainTextEdit()
        self.dsh_command.setObjectName('agent_dsh_command')
        self.dsh_command.setReadOnly(True)
        self.dsh_command.setFixedHeight(54)
        self.dsh_command.setToolTip('等价的命令行：可以整段复制出来自己跑')
        self.dsh_command.setPlaceholderText('点上面任一按钮后，这里显示可复制的等价命令')
        layout.addWidget(self.dsh_command)

        self.feedback = QLabel('')
        self.feedback.setWordWrap(True)
        self.feedback.setObjectName('agent_backend_feedback')
        layout.addWidget(self.feedback)

        self.scroll.setWidget(body)
        shell.addWidget(self.scroll, 1)

    # ── 请求：全部异步，忙时排队（任何点击都不会没下文）──────────────────────
    def _request(self, work, apply) -> bool:
        """把 work 丢进工作线程，结果回主线程交给 apply；busy 时排队。"""
        if not _HAS_QT or not hasattr(self, 'requests'):
            return False
        if self.requests.busy:
            self._queue.append((work, apply))
            return True
        return self._dispatch(work, apply)

    def _dispatch(self, work, apply) -> bool:
        def wrapped(result):
            try:
                apply(result)
            finally:
                self._drain()
        return self.requests.start(work, wrapped, self._on_request_error)

    def _drain(self) -> None:
        """上一条请求落定后接着跑排队的下一条。"""
        if not _HAS_QT or not hasattr(self, 'requests'):
            return
        if self.requests.busy or not self._queue:
            return
        work, apply = self._queue.pop(0)
        self._dispatch(work, apply)

    def refresh(self, force: bool = False, quiet: bool = False) -> bool:
        """拉列表。quiet=True 用于动作后的自动刷新：不覆盖刚写下的结果文案。"""
        if self.get is None:
            return False
        started = self._request(lambda: self.get(PANEL_PATH),
                                lambda payload: self.apply_list(payload, force=force))
        if started and not quiet:
            self._say(REFRESHING_TEXT)
        return started

    def apply_list(self, payload, force: bool = False) -> list:
        problem = error_of(payload)
        if problem is not None:
            if problem[0] == 'bad_profile_file':
                # 坏文件：空表 + 原样回显错误码（不把整页打成空白）。
                self._rows = []
                self._render()
                self.show_error(*problem)
                return self._rows
            self.show_error(*problem)
            return self._rows
        rows = payload.get('backends') if isinstance(payload, dict) else None
        self._rows = [dict(row) for row in (rows or []) if isinstance(row, dict)]
        self._render(force=force)
        self._finish_refresh()
        return self._rows

    def _finish_refresh(self) -> None:
        """刷新成功只擦掉「正在刷新」，保留其它动作刚写下的结果文案。"""
        if not _HAS_QT:
            return
        if self.feedback.text() == REFRESHING_TEXT:
            self.clear_error()

    def apply_status(self, backend_id: str, payload) -> None:
        problem = error_of(payload)
        if problem is not None:
            self.show_error(*problem)
            return
        if not isinstance(payload, dict):
            return
        for row in self._rows:
            if row.get('id') == backend_id:
                row.update({'state': state_of(payload), 'pid': payload.get('pid'),
                            'running': payload.get('running')})
        self._render()

    def _on_request_error(self, exc) -> None:
        self.show_error('panel_error', '%s: %s' % (type(exc).__name__, exc))
        self._drain()

    # ── 渲染 ────────────────────────────────────────────────────────────────
    def _render(self, force: bool = False) -> None:
        if not _HAS_QT:
            return
        has_rows = bool(self._rows)
        self.table.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            state = state_of(row)
            values = [str(row.get('id') or ''), STATE_LABELS.get(state, state),
                      '-' if row.get('pid') in (None, '') else str(row.get('pid')),
                      str(row.get('command') or ''), str(row.get('workspace') or '')]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, row.get('id'))
                item.setToolTip(text)       # 列窄时长路径也能悬停看全
                if column == 1:
                    item.setData(Qt.ItemDataRole.UserRole + 1, state)
                    color = STATE_COLORS.get(state)
                    if color:
                        item.setForeground(QBrush(QColor(color)))
                self.table.setItem(index, column, item)
        self.table.blockSignals(False)
        self._select_after_render(force=force)
        self._apply_selection_buttons()

    def _select_after_render(self, force: bool = False) -> None:
        """优先选中刚保存的那一行；用户刚清空表单时不要抢回选中。"""
        if not _HAS_QT:
            return
        pending = str(self._pending_select or '')
        if pending:
            self._pending_select = ''
            if self.select(pending):
                return
        if not self._rows or self._form_manual:
            return
        if force or not self.current_id():
            self.table.selectRow(0)

    def _on_selection_changed(self) -> None:
        backend_id = self.current_id()
        if backend_id:
            self._selected_id = backend_id
            self._form_manual = False
            row = self.table.currentRow()
            if 0 <= row < len(self._rows):
                self._fill_form(self._rows[row])
        else:
            self._selected_id = ''
        self._apply_selection_buttons()

    def _fill_form(self, row) -> None:
        if not _HAS_QT:
            return
        self.input_id.setText(str(row.get('id') or ''))
        self.input_command.setText(str(row.get('command') or ''))
        args = row.get('args') or []
        self.input_args.setText(' '.join(str(part) for part in args))
        self.input_workdir.setText(str(row.get('workdir') or ''))
        try:
            timeout = float(row.get('timeout') or 120.0)
        except (TypeError, ValueError):
            timeout = 120.0
        self.input_timeout.setValue(min(max(timeout, 1.0), 600.0))

    @staticmethod
    def _set_enabled(widget, enabled: bool, tip: str) -> None:
        """按钮态说话：能点就给说明，不能点就把原因写进 tooltip。"""
        widget.setEnabled(bool(enabled))
        widget.setToolTip(tip)

    def _apply_selection_buttons(self) -> None:
        if not _HAS_QT:
            return
        backend_id = self.current_id()
        has_selection = bool(backend_id)
        state = self.state_of(backend_id) if has_selection else ''
        running = state in ('running', 'starting')
        pid = ''
        for row in self._rows:
            if str(row.get('id') or '') == backend_id:
                pid = row.get('pid')
        pid = '-' if pid in (None, '') else str(pid)
        self.btn_refresh.setEnabled(True)
        self.btn_detect.setEnabled(True)
        self._set_enabled(
            self.btn_probe, has_selection,
            ('用一次 --version 干跑 %s，确认这条命令真的能执行（不启动常驻进程）' % backend_id)
            if has_selection else NO_SELECTION_TIP)
        self._set_enabled(
            self.btn_open, has_selection,
            ('在资源管理器里打开 %s 的工作区目录' % backend_id)
            if has_selection else NO_SELECTION_TIP)
        self._set_enabled(
            self.btn_start, has_selection and not running,
            ('正在运行（pid %s），先点「停止」再启动' % pid) if running
            else ('拉起 %s 的真实进程' % backend_id if has_selection else NO_SELECTION_TIP))
        self._set_enabled(
            self.btn_stop, has_selection and running,
            ('按进程树结束 %s（pid %s）' % (backend_id, pid)) if running
            else ('当前没在运行，无需停止' if has_selection else NO_SELECTION_TIP))
        self._set_enabled(
            self.btn_delete, has_selection and not running,
            ('正在运行，先点「停止」再删除' if running
             else ('删除 %s 的 profile（不会动已启动的进程）' % backend_id))
            if has_selection else NO_SELECTION_TIP)
        self.form_title.setText('正在编辑：%s' % (backend_id or '新建 profile'))

    # ── 只读查询（测试与调用方用）───────────────────────────────────────────
    def rows(self) -> list:
        return [dict(row) for row in self._rows]

    def states(self) -> dict:
        return {str(row.get('id') or ''): state_of(row) for row in self._rows}

    def state_of(self, backend_id) -> str:
        for row in self._rows:
            if str(row.get('id') or '') == str(backend_id or ''):
                return state_of(row)
        return ''

    def current_id(self) -> str:
        """当前选中的 id；没有高亮行就是空（clearSelection 也算没选中）。"""
        if not _HAS_QT or not hasattr(self, 'table'):
            return self._selected_id
        if not self.table.selectionModel().hasSelection():
            return ''
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            return str(self._rows[row].get('id') or '')
        return ''

    def select(self, backend_id: str) -> str:
        if not _HAS_QT or not hasattr(self, 'table'):
            return ''
        for index, row in enumerate(self._rows):
            if str(row.get('id') or '') == backend_id:
                self.table.selectRow(index)
                self._selected_id = backend_id
                self._apply_selection_buttons()
                return backend_id
        return ''

    def button_state(self) -> dict:
        if not _HAS_QT:
            return {}
        return {'probe': self.btn_probe.isEnabled(),
                'start': self.btn_start.isEnabled(),
                'stop': self.btn_stop.isEnabled(),
                'open': self.btn_open.isEnabled(),
                'delete': self.btn_delete.isEnabled()}

    def button_tips(self) -> dict:
        if not _HAS_QT:
            return {}
        return {'refresh': self.btn_refresh.toolTip(),
                'detect': self.btn_detect.toolTip(),
                'probe': self.btn_probe.toolTip(),
                'start': self.btn_start.toolTip(),
                'stop': self.btn_stop.toolTip(),
                'open': self.btn_open.toolTip(),
                'delete': self.btn_delete.toolTip()}

    def edit_title(self) -> str:
        return self.form_title.text() if _HAS_QT else ''

    def feedback_text(self) -> str:
        return self.feedback.text() if _HAS_QT else ''

    def empty_visible(self) -> bool:
        if not _HAS_QT:
            return True
        return bool(self.empty.isVisibleTo(self))

    def empty_text(self) -> str:
        return self.empty.text() if _HAS_QT else EMPTY_TEXT

    def error_text(self) -> str:
        return self.feedback.text() if _HAS_QT else ''

    def _say(self, text: str) -> None:
        """把一句进行中文案立刻写到 feedback（不等任何响应）。"""
        if _HAS_QT:
            self.feedback.setText(str(text))

    def show_error(self, code: str, message: str = '') -> None:
        text = '[%s] %s' % (code, message) if message else '[%s]' % code
        if _HAS_QT:
            self.feedback.setText(text)

    def clear_error(self) -> None:
        if _HAS_QT:
            self.feedback.setText('')

    def _need_target(self, label: str) -> bool:
        """没有选中行时也必须回音：写清要做什么、怎么做，返回 True 让调用方收手。"""
        if self.current_id():
            return False
        self.show_error('no_selection', '%s需要先选中一个后端：%s'
                        % (label, NO_SELECTION_TIP))
        return True

    def _need_transport(self, label: str, transport) -> bool:
        if transport is not None:
            return False
        self.show_error('offline', '%s需要控制台接口（面板没有注入这条请求函数）' % label)
        return True

    def clear_form(self) -> None:
        if not _HAS_QT:
            return
        for widget in (self.input_id, self.input_command, self.input_args,
                       self.input_workdir):
            widget.setText('')
        self.input_timeout.setValue(120.0)
        self._form_kind = 'cli'
        # 清选中时屏蔽信号：否则 selectionChanged 会把刚清空的表单又填回旧行。
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self._selected_id = ''
        self._form_manual = True
        self._apply_selection_buttons()
        self._say('表单已清空：填好命令后点「保存 profile」就是新 profile')

    # ── 动作 ────────────────────────────────────────────────────────────────
    def profile_from_form(self) -> dict:
        profile = {'command': self.input_command.text().strip() if _HAS_QT else ''}
        if _HAS_QT:
            args = split_args(self.input_args.text())
            if args:
                profile['args'] = args
            workdir = self.input_workdir.text().strip()
            if workdir:
                profile['workdir'] = workdir
            profile['timeout'] = float(self.input_timeout.value())
        return profile

    def save_profile(self, on_done=None) -> bool:
        """保存表单 → refresh 并选中刚保存那一行。on_done(ok, id, problem) 只在请求跑完后回调。"""
        if self.post is None:
            self.show_error('offline', '保存 profile 需要控制台接口（面板没有注入 post）')
            return False
        body = {'id': self.input_id.text().strip() if _HAS_QT else '',
                'kind': str(self._form_kind or 'cli'),
                'profile': self.profile_from_form()}
        if not body['id']:
            self.show_error('bad_profile', '请先填写 backendId')
            return False

        def done(payload):
            problem = error_of(payload)
            if problem is not None:
                self.show_error(*problem)
                if on_done is not None:
                    on_done(False, body['id'], problem)
                return
            self.clear_error()
            self._form_manual = False
            self._pending_select = body['id']
            self.feedback.setText('已保存 profile：%s，点「启动」开始' % body['id'])
            self.refresh(quiet=True)
            if on_done is not None:
                on_done(True, body['id'], None)

        started = self._request(lambda: self.post(PANEL_PATH, body), done)
        if started:
            self._say('正在保存 profile：%s…' % body['id'])
        else:
            self.show_error('busy', '上一条请求还没回来，保存没有发出去，请再点一次')
        return started

    def _action(self, backend_id, work, name, pending='', success=None) -> bool:
        def done(payload):
            problem = error_of(payload)
            if problem is not None:
                self.show_error(*problem)
                return
            self.clear_error()
            self.feedback.setText(success(payload) if success is not None
                                  else '%s %s' % (name, backend_id))
            self.refresh(quiet=True)
        started = self._request(work, done)
        if started and pending:
            self._say(pending)
        return started

    @staticmethod
    def _probe_summary(payload) -> str:
        """探测结果写成人看的一句：resolved / runnable / version。"""
        if not isinstance(payload, dict):
            return '-'
        parts = []
        if payload.get('resolved'):
            parts.append('resolved=%s' % payload.get('resolved'))
        parts.append('runnable=%s' % ('是' if payload.get('runnable') else '否'))
        version = str(payload.get('version') or '').strip()
        if not version:
            lines = str(payload.get('stdout') or '').strip().splitlines()
            version = lines[0].strip() if lines else ''
        if version:
            parts.append('version=%s' % version[:80])
        parts.append('version_ok=%s' % ('是' if payload.get('version_ok') else '否'))
        return '，'.join(parts)

    def action_probe(self) -> bool:
        if self._need_target('探测'):
            return False
        if self._need_transport('探测', self.post):
            return False
        backend_id = self.current_id()

        def done(payload):
            problem = error_of(payload)
            if problem is not None:
                self.show_error(*problem)
                return
            self.clear_error()
            self.feedback.setText('已探测 %s：%s'
                                  % (backend_id, self._probe_summary(payload)))
            self.refresh(quiet=True)

        started = self._request(
            lambda: self.post('%s/%s/probe' % (PANEL_PATH, backend_id)), done)
        if started:
            self._say('正在探测 %s…（跑一次 --version，不启动常驻进程）' % backend_id)
        return started

    def action_start(self) -> bool:
        if self._need_target('启动'):
            return False
        if self._need_transport('启动', self.post):
            return False
        backend_id = self.current_id()
        return self._action(
            backend_id,
            lambda: self.post('%s/%s/start' % (PANEL_PATH, backend_id), {}),
            '已启动',
            pending='正在启动 %s…（会拉起真实进程）' % backend_id,
            success=lambda payload: '已启动 %s：pid=%s（点「停止」按进程树结束）'
                                    % (backend_id, pid_text(payload)))

    def action_stop(self) -> bool:
        if self._need_target('停止'):
            return False
        if self._need_transport('停止', self.post):
            return False
        backend_id = self.current_id()
        return self._action(
            backend_id,
            lambda: self.post('%s/%s/stop' % (PANEL_PATH, backend_id)),
            '已停止',
            pending='正在停止 %s…（按进程树结束）' % backend_id,
            success=lambda payload: '已停止 %s：pid=%s%s'
                                    % (backend_id, pid_text(payload),
                                       '（原本就没在跑）'
                                       if isinstance(payload, dict)
                                       and payload.get('already_stopped') else ''))

    def action_delete(self) -> bool:
        if self._need_target('删除'):
            return False
        if self._need_transport('删除', self.delete):
            return False
        backend_id = self.current_id()
        return self._action(
            backend_id,
            lambda: self.delete('%s/%s' % (PANEL_PATH, backend_id)),
            '已删除',
            pending='正在删除 %s…' % backend_id)

    def action_open_workspace(self) -> bool:
        if self._need_target('打开工作区目录'):
            return False
        if self._need_transport('打开工作区目录', self.get):
            return False
        backend_id = self.current_id()

        def done(payload):
            problem = error_of(payload)
            if problem is not None:
                self.show_error(*problem)
                return
            self.clear_error()
            path = str(payload.get('root') or payload.get('path') or '')
            self.feedback.setText('工作区：%s' % path)
            if self.opener is not None and path:
                try:
                    self.opener(path)
                except Exception as exc:        # noqa: BLE001 - 打开失败只回显
                    self.show_error('open_failed', '%s: %s' % (type(exc).__name__, exc))
        started = self._request(
            lambda: self.get('%s/%s/workspace' % (PANEL_PATH, backend_id)), done)
        if started:
            self._say('正在获取 %s 的工作区路径…' % backend_id)
        return started

    # ── 自动识别（只读探测本机 DSH）─────────────────────────────────────────
    def action_detect(self, probe: bool = False) -> bool:
        """点「自动识别」：走 PanelRequests 调探测端点，主线程不等网络。"""
        if self.get is None:
            self.show_error('offline', '自动识别需要控制台接口（面板没有注入 get）')
            return False
        started = self._request(lambda: self.get(detect_path(probe)), self.apply_detect)
        if started:
            self._set_hint('正在识别本机 DSH…（只读扫描 PATH / npm / app.asar）')
        return started

    def apply_detect(self, payload) -> list:
        """探测结果回主线程：1 个候选直接预填，多个弹候选框，0 个写清扫描路径。"""
        problem = error_of(payload)
        if problem is not None:
            self.show_error(*problem)
            self._set_hint('[%s] %s' % problem)
            return []
        rows = payload.get('items') if isinstance(payload, dict) else None
        self._candidates = [dict(row) for row in (rows or []) if isinstance(row, dict)]
        scanned = payload.get('scanned') if isinstance(payload, dict) else None
        self._scanned = [str(path) for path in (scanned or [])]
        self._detect_reason = str((payload or {}).get('reason') or '')
        if not self._candidates:
            self._set_hint(self._empty_hint())
            return []
        self.clear_error()
        if len(self._candidates) == 1:
            self.fill_candidate(self._candidates[0])
            self._set_hint('已识别 1 个候选：%s（已填入表单，确认后点「保存 profile」）'
                           % self._candidates[0].get('id', ''))
            return self.candidates()
        self.open_candidates(self._candidates)
        self._set_hint('已识别 %d 个候选，在候选框里选中一行点「存选中」'
                       '（双击任意行也行）；「全部保存」可一次全存。'
                       % len(self._candidates))
        return self.candidates()

    def candidates(self) -> list:
        """最近一次识别的候选（副本，测试与调用方用）。"""
        return [dict(item) for item in self._candidates]

    def scanned(self) -> list:
        return list(self._scanned)

    def detect_hint(self) -> str:
        """最近一次识别的提示文本：0 候选时含扫描路径与原因。"""
        return str(self._detect_hint)

    def fill_candidate(self, item) -> dict:
        """把一个候选的 command/args/timeout 填进表单（不落盘）。"""
        item = dict(item or {})
        self._form_kind = str(item.get('kind') or 'cli')
        args = [str(part) for part in (item.get('args') or [])]
        try:
            timeout = float(item.get('timeout') or 120.0)
        except (TypeError, ValueError):
            timeout = 120.0
        timeout = min(max(timeout, 1.0), 600.0)
        if _HAS_QT:
            self.input_id.setText(str(item.get('id') or ''))
            self.input_command.setText(str(item.get('command') or ''))
            self.input_args.setText(args_text(args))
            self.input_workdir.setText(str(item.get('workdir') or ''))
            self.input_timeout.setValue(timeout)
        return {'id': str(item.get('id') or ''), 'kind': self._form_kind,
                'command': str(item.get('command') or ''), 'args': args,
                'workdir': str(item.get('workdir') or ''), 'timeout': timeout}

    def save_candidate(self, item, on_done=None) -> bool:
        """候选 → 表单 → 现有 save_profile() 落盘（kind 取候选自己的）。"""
        filled = self.fill_candidate(item)
        if not filled['id']:
            message = '候选缺少 backendId，无法保存'
            self.show_error('bad_profile', message)
            return False
        return self.save_profile(on_done=on_done)

    def open_candidates(self, items=None) -> object:
        """弹出候选对话框（非阻塞 show()，双击/选中/全部保存三条路都能落盘）。"""
        rows = [dict(item) for item in (items if items is not None else self._candidates)]
        if not _HAS_QT or not rows:
            return None
        if self.dialog is not None:
            try:
                self.dialog.close()
            except Exception:       # noqa: BLE001 - 旧框已经没了也无所谓
                pass
        self.dialog = DetectDialog(self, rows)
        self.dialog.show()
        return self.dialog

    def _set_hint(self, text: str) -> None:
        self._detect_hint = str(text or '')
        if _HAS_QT:
            self.feedback.setText(self._detect_hint)

    def _empty_hint(self) -> str:
        scanned = '、'.join(self._scanned) or '(没有可扫描的路径)'
        reason = self._detect_reason or '没有找到可用的 DSH 入口'
        return '没有识别到 DSH 候选：%s；已扫描：%s' % (reason, scanned)

    # ── 轮询 / 收尾 ─────────────────────────────────────────────────────────
    def start_polling(self) -> None:
        if _HAS_QT and hasattr(self, 'poll_timer'):
            self.poll_timer.start()

    def stop_polling(self) -> None:
        if _HAS_QT and hasattr(self, 'poll_timer'):
            self.poll_timer.stop()

    def shutdown_backends(self) -> None:
        """面板/窗口关闭时把所有后端真进程停干净（服务端也有同样的退出钩子）。"""
        if self.post is None:
            return
        try:
            self.post('%s/shutdown' % PANEL_PATH, {})
        except Exception:           # noqa: BLE001 - 关窗路径不许抛
            pass

    # ── DSH 联动（三个动作全都经后台线程：UI 线程里一次网络都不许碰）──────────
    def dsh_profile_name(self) -> str:
        """下拉里选中的 profile；没有控件时按默认 desktop。"""
        if not _HAS_QT or not hasattr(self, 'dsh_profile'):
            return DSH_DEFAULT_PROFILE
        return str(self.dsh_profile.currentText() or '').strip() or DSH_DEFAULT_PROFILE

    def dsh_profiles_list(self) -> list:
        """下拉里现有的 profile 名（空列表/纯逻辑环境下就是空的）。"""
        if not _HAS_QT or not hasattr(self, 'dsh_profile'):
            return []
        return [self.dsh_profile.itemText(index)
                for index in range(self.dsh_profile.count())]

    def set_dsh_profiles(self, profiles, prefer: str = '') -> list:
        """用后台给的 profile 列表重填下拉；原选择（或 prefer）还在就留住。"""
        names = [str(name).strip() for name in (profiles or []) if str(name or '').strip()]
        if not _HAS_QT or not hasattr(self, 'dsh_profile'):
            return names
        want = str(prefer or '').strip() or self.dsh_profile_name()
        self.dsh_profile.clear()
        for name in (names or [DSH_DEFAULT_PROFILE]):
            self.dsh_profile.addItem(name)
        target = want if want in names else (
            DSH_DEFAULT_PROFILE if DSH_DEFAULT_PROFILE in names else '')
        index = self.dsh_profile.findText(target) if target else -1
        self.dsh_profile.setCurrentIndex(index if index >= 0 else 0)
        return names

    def dsh_command_text(self) -> str:
        return self.dsh_command.toPlainText() if _HAS_QT else ''

    def set_dsh_command(self, text: str) -> None:
        if _HAS_QT:
            self.dsh_command.setPlainText(str(text or ''))

    def dsh_feedback_text(self) -> str:
        return self.dsh_feedback.text() if _HAS_QT else ''

    def dsh_repo_root(self) -> str:
        """旧版「源码仓库根」输入框已移除；保留空实现，兼容旧调用。"""
        return ''

    def set_dsh_repo(self, text: str) -> None:
        """旧版接口：现在没有这个输入框，忽略。"""
        return None

    def _dsh_reveal(self) -> None:
        """回音刚写完就滚进可视区：窗口不高时它原来整个在折叠区外面。"""
        if _HAS_QT and hasattr(self, 'scroll'):
            self.scroll.ensureWidgetVisible(self.dsh_feedback, 0, 24)

    def _dsh_say(self, text: str) -> None:
        if _HAS_QT:
            self.dsh_feedback.setText(str(text))
            self._dsh_reveal()

    def show_dsh_error(self, code: str, message: str = '') -> None:
        text = '[%s] %s' % (code, message) if message else '[%s]' % code
        if _HAS_QT:
            self.dsh_feedback.setText(text)
            self._dsh_reveal()

    @staticmethod
    def _dsh_result_text(data, profile: str) -> str:
        """结果一句话说清：哪个 profile、变没变、几条登记、下一步注意什么。"""
        action = str(data.get('action') or 'status')
        count = int(data.get('entry_count') or 0)
        installed = bool(data.get('tools_installed'))
        changed = data.get('changed')
        if action == 'link':
            head = ('已把 DSH 接上控制台：profile=%s，三条 MCP 登记都在' % profile
                    if installed else '写入完成，但 profile=%s 里三条登记没齐（看状态）' % profile)
            if changed is False:
                head = 'profile=%s 之前就登记好了，这次没有改动文件（幂等）' % profile
        elif action == 'unlink':
            head = ('已撤销 profile=%s 的接入：该 profile 里没有鲸鱼娘的三条登记了' % profile
                    if not installed else
                    '撤销跑了，但 profile=%s 里还剩登记（检查 cordis.patch.yml）' % profile)
            if changed is False:
                head = 'profile=%s 里本来没有鲸鱼娘的登记，没有改动文件' % profile
        else:
            head = ('profile=%s：三条 MCP 登记都在' % profile if installed
                    else 'profile=%s：还没接上控制台' % profile)
        parts = [head, '条目总数 %d' % count]
        bare = [str(item) for item in (data.get('bare_entry_ids') or [])]
        if bare:
            parts.append('有裸行登记（DSH 会丢弃，重新点接入会搬进 insert 块）：%s'
                         % '、'.join(bare))
        if str(data.get('mcp_mode') or '') == 'bundled':
            parts.append('MCP 模式：安装版内置宿主（不需要 Python，也不需要源码仓库）')
        warning = str(data.get('warning') or '')
        if warning:
            parts.append(warning)
        hint = str(data.get('hint') or '')
        if hint:
            parts.append(hint)
        return '\n'.join(parts)

    def apply_dsh(self, payload) -> dict:
        """应用 /dsh/* 的返回：填下拉、写只读命令框、把结果写成中文回音。"""
        problem = error_of(payload)
        if problem is not None:
            self.show_dsh_error(problem[0], problem[1])
            return {'ok': False, 'code': problem[0], 'message': problem[1]}
        data = dict(payload) if isinstance(payload, dict) else {}
        self._dsh_loaded = True
        profile = str(data.get('profile') or '')
        self.set_dsh_profiles(data.get('profiles'), prefer=profile)
        command = str(data.get('command') or '')
        if command:
            self.set_dsh_command(command)
        elif str(data.get('mcp_mode') or '') == 'bundled':
            self.set_dsh_command('安装版已内置 MCP 宿主，无需命令行（DSH 会直接启动控制台 exe）。')
        self._dsh_say(self._dsh_result_text(data, profile or self.dsh_profile_name()))
        return data

    @staticmethod
    def _dsh_report_names(value) -> list:
        """把 installed/missing 归一成名字列表（支持列表、映射与逗号字符串）。"""
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item or '').strip()]
        if isinstance(value, dict):
            return [str(key).strip() for key, flag in value.items()
                    if flag and str(key or '').strip()]
        text = str(value or '').strip()
        return [part.strip() for part in text.split(',') if part.strip()] if text else []

    @staticmethod
    def _dsh_connection_report_text(payload) -> str:
        """把 /dsh/connection-report 写成中文体检报告（不截断 hint）。"""
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data:
            return '连接体检失败：后台没有返回报告。'
        lines = []
        if data.get('ok') is True:
            lines.append('体检结果：通过')
        elif data.get('ok') is False:
            lines.append('体检结果：未通过')
        profile = str(data.get('profile') or '').strip()
        if profile:
            lines.append('profile：%s' % profile)
        ready = data.get('new_session_ready')
        if ready is True:
            lines.append('新会话可用')
        elif ready is False:
            lines.append('新会话不可用')
        else:
            lines.append('新会话可用性未知')
        installed = AgentBackendPanel._dsh_report_names(data.get('installed'))
        missing = AgentBackendPanel._dsh_report_names(data.get('missing'))
        lines.append('已登记 MCP：%s' % ('、'.join(installed) if installed else '无'))
        lines.append('缺少 MCP：%s' % ('、'.join(missing) if missing else '无'))
        lines.append('需要重启：%s' % ('是' if data.get('restart_required') else '否'))
        raw_issues = data.get('issues')
        if isinstance(raw_issues, dict):
            raw_issues = [raw_issues]
        issues = []
        for item in raw_issues if isinstance(raw_issues, list) else []:
            if isinstance(item, dict):
                message = (item.get('message') or item.get('detail') or item.get('description')
                           or item.get('reason') or item.get('issue') or '')
                code = item.get('code') or ''
                text = str(message or code or '').strip()
                if code and message and str(code) != str(message):
                    text = '%s：%s' % (code, text)
                if text:
                    issues.append(text)
            elif str(item or '').strip():
                issues.append(str(item).strip())
        lines.append('问题：%s' % ('；'.join(issues) if issues else '无'))
        hint = str(data.get('hint') or '').strip()
        lines.append('提示：%s' % (hint if hint else '无'))
        return '\n'.join(lines)

    def apply_dsh_report(self, payload) -> dict:
        """应用连接体检结果：只读报告标签 + 一句中文回音。"""
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data or (data.get('success') is False and not data.get('profile')):
            message = str(data.get('message') or data.get('detail') or '后台没有返回体检报告')
            if hasattr(self, 'dsh_report'):
                self.dsh_report.setText('连接体检失败：%s' % message)
            self._dsh_say('连接体检失败：%s' % message)
            return {'ok': False, 'message': message}
        text = self._dsh_connection_report_text(data)
        if hasattr(self, 'dsh_report'):
            self.dsh_report.setText(text)
        self._dsh_say('DSH 连接体检完成。')
        return data

    def action_dsh_report(self) -> bool:
        """只读连接体检：GET /dsh/connection-report?profile=当前 profile。"""
        if self.get is None:
            self.show_dsh_error('offline', '面板没有注入 get，做不了连接体检')
            return False
        profile = self.dsh_profile_name()      # worker 线程不许碰控件
        path = DSH_PATH + '/connection-report?profile=' + quote(profile)
        started = self._request(lambda: self.get(path), self.apply_dsh_report)
        if started:
            if hasattr(self, 'dsh_report'):
                self.dsh_report.setText('正在体检 DSH 连接…')
            self._dsh_say('正在体检 DSH 连接…')
        return started

    def _dsh_profile_changed(self, *_args) -> None:
        """切换 profile 后旧报告已经不对应当前目标，立即清空。"""
        if hasattr(self, 'dsh_report'):
            self.dsh_report.clear()

    def action_dsh_status(self) -> bool:
        """只读状态：填 profile 下拉与只读命令框，不写任何文件。"""
        if self.get is None:
            self.show_dsh_error('offline', '面板没有注入 get，读不到 DSH 状态')
            return False
        profile = self.dsh_profile_name()      # 先取值：worker 线程不许碰控件
        started = self._request(lambda: self.get(dsh_status_path(profile)),
                                self.apply_dsh)
        if started:
            self._dsh_say('正在读取 DSH 状态…')
        return started

    def action_dsh_link(self) -> bool:
        return self._dsh_post(DSH_LINK_PATH, '正在把三条 MCP 条目写进 DSH…')

    def action_dsh_unlink(self) -> bool:
        return self._dsh_post(DSH_UNLINK_PATH, '正在撤销 DSH 里的三条登记…')

    def _dsh_post(self, path: str, pending: str) -> bool:
        """写侧动作：POST 到 /dsh/link|unlink，参数就是下拉里选中的 profile。"""
        if self.post is None:
            self.show_dsh_error('offline', '面板没有注入 post，没法改 DSH 配置')
            return False
        profile = self.dsh_profile_name()
        body = {'profile': profile}
        started = self._request(lambda: self.post(path, body), self.apply_dsh)
        if started:
            self._dsh_say(pending)
        return started

    def auto_dsh_status(self) -> bool:
        """切到本页时补一次状态（只跑一次）：用来把 profile 下拉填成真实列表。"""
        if self._dsh_loaded or self.get is None:
            return False
        return self.action_dsh_status()

    def showEvent(self, event):
        """切到本页才开始轮询：隐藏时一个请求都不发。"""
        self.start_polling()
        self.refresh()
        self.auto_dsh_status()
        super().showEvent(event)

    def hideEvent(self, event):
        self.stop_polling()
        super().hideEvent(event)

    def close(self) -> None:
        self.stop_polling()
        self._queue = []
        if self.dialog is not None:
            try:
                self.dialog.close()
            except Exception:       # noqa: BLE001 - 关窗路径不许抛
                pass
            self.dialog = None
        if _HAS_QT and hasattr(self, 'requests'):
            self.requests.close()


# ── 候选对话框 ──────────────────────────────────────────────────────────────

class DetectDialog(QDialog):
    """「自动识别」的候选列表：命令列不截断、版本列异步补齐、双击即存 profile。

    非阻塞（show()）：对话框不跑自己的事件循环，落盘依旧经面板的 PanelRequests；
    所以「版本」列的 --version 探测与保存都排在面板那条串行队列里，不会互相打断。
    """

    def __init__(self, panel, items, parent=None):
        super().__init__(panel if parent is None else parent)
        self.panel = panel
        self.items = [dict(item) for item in (items or [])]
        self.buttons: list = []
        self.save_results: list = []
        self._save_queue: list = []
        self._probing = False
        self.setWindowTitle('自动识别到的 DSH 候选（%d）' % len(self.items))
        layout = QVBoxLayout(self)
        caption = QLabel('选中一行点「存选中」（双击任意行也行）把 command/args/timeout '
                         '落成 profile；「全部保存」逐条保存并逐条报告结果。'
                         '这里不会启动任何进程。')
        caption.setWordWrap(True)
        layout.addWidget(caption)

        self.table = QTableWidget(len(self.items), len(DIALOG_COLUMNS))
        self.table.setObjectName('agent_detect_table')
        self.table.setHorizontalHeaderLabels(DIALOG_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        # 命令列不许被截成 C...：不省略 + 列宽按内容 + 横向滚动 + 完整 tooltip。
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        for index, item in enumerate(self.items):
            values = [str(item.get('id') or ''), str(item.get('kind') or ''),
                      str(item.get('profile') or ''), str(item.get('command') or ''),
                      str(item.get('version') or ''), str(item.get('source') or ''),
                      '推荐' if is_recommended(item) else '']
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)            # 任何一格都能悬停看全文
                if column == COL_RECOMMEND and text:
                    cell.setForeground(QBrush(QColor(RECOMMEND_COLOR)))
                self.table.setItem(index, column, cell)
            action_cell = QTableWidgetItem('存为 profile')
            action_cell.setToolTip('把这一行存成 profile（双击整行也行）')
            self.table.setItem(index, COL_ACTION, action_cell)
            holder = QWidget()
            row_layout = QHBoxLayout(holder)
            row_layout.setContentsMargins(2, 0, 2, 0)
            button = QPushButton('存为 profile')
            button.setObjectName('agent_detect_save_%d' % index)
            button.clicked.connect(lambda _checked=False, row=index: self.save(row))
            row_layout.addWidget(button)
            self.buttons.append(button)
            self.table.setCellWidget(index, COL_ACTION, holder)
        self.table.itemDoubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.btn_save_selected = QPushButton('存选中')
        self.btn_save_selected.setObjectName('agent_detect_save_selected')
        self.btn_save_selected.setToolTip('把当前选中的那一行存成 profile')
        self.btn_save_selected.clicked.connect(self.save_selected)
        bottom.addWidget(self.btn_save_selected)
        self.btn_save_all = QPushButton('全部保存')
        self.btn_save_all.setObjectName('agent_detect_save_all')
        self.btn_save_all.setToolTip('逐条存成 profile，失败的逐条报告，不中断其它条')
        self.btn_save_all.clicked.connect(self.save_all)
        bottom.addWidget(self.btn_save_all)
        bottom.addStretch(1)
        self.btn_close_dialog = QPushButton('关闭')
        self.btn_close_dialog.setObjectName('agent_detect_close')
        self.btn_close_dialog.clicked.connect(self.close)
        bottom.addWidget(self.btn_close_dialog)
        layout.addLayout(bottom)

        self.hint = QLabel('')
        self.hint.setWordWrap(True)
        self.hint.setObjectName('agent_detect_hint')
        layout.addWidget(self.hint)

        self._fit_columns()
        self.setMinimumSize(880, 380)
        self.resize(1040, 440)
        if self.items:
            self.table.selectRow(0)
        self.start_version_probe()

    # ── 列宽 / 版本 ─────────────────────────────────────────────────────────
    def _fit_columns(self) -> None:
        """列宽按内容自适应：命令列至少 260px，操作/推荐列留出按钮宽度。"""
        if not _HAS_QT:
            return
        self.table.resizeColumnsToContents()
        floors = {COL_COMMAND: 260, COL_ACTION: 130, COL_RECOMMEND: 72}
        for index in range(len(DIALOG_COLUMNS)):
            width = max(self.table.columnWidth(index) + 16, floors.get(index, 64))
            self.table.setColumnWidth(index, width)

    def start_version_probe(self) -> bool:
        """打开候选框就异步补「版本」列（/detect?probe=1），列头先写「探测中…」。"""
        panel = self.panel
        if not _HAS_QT or panel is None or getattr(panel, 'get', None) is None:
            return False
        if not self.items:
            return False
        self._probing = True
        self.table.setHorizontalHeaderItem(COL_VERSION, QTableWidgetItem(VERSION_PROBING))
        started = panel._request(lambda: panel.get(detect_path(True)), self.apply_versions)
        if not started:
            self._probing = False
            self.table.setHorizontalHeaderItem(COL_VERSION, QTableWidgetItem('版本'))
            self.hint.setText('「版本」列没有探测（上一条请求还没回来），可再点一次「自动识别」')
            return False
        self.hint.setText('正在补「版本」列（最多 3 个命令各跑一次 --version）…')
        return True

    def apply_versions(self, payload) -> list:
        """探测结果回填「版本」列；拿不到就如实说，不阻塞保存。"""
        self._probing = False
        problem = error_of(payload)
        if problem is not None:
            self.table.setHorizontalHeaderItem(COL_VERSION,
                                               QTableWidgetItem('版本（探测失败）'))
            self.hint.setText('[%s] %s' % problem)
            return []
        found = {}
        rows = payload.get('items') if isinstance(payload, dict) else None
        for row in rows or []:
            if isinstance(row, dict):
                found[str(row.get('id') or '')] = str(row.get('version') or '')
        self.table.setHorizontalHeaderItem(COL_VERSION, QTableWidgetItem('版本'))
        filled = []
        for index, item in enumerate(self.items):
            version = found.get(str(item.get('id') or ''), '')
            if not version:
                continue
            item['version'] = version
            cell = self.table.item(index, COL_VERSION)
            if cell is not None:
                cell.setText(version)
                cell.setToolTip(version)
            filled.append(index)
        self._fit_columns()
        if filled:
            self.hint.setText('「版本」列已补齐 %d/%d 条' % (len(filled), len(self.items)))
        else:
            self.hint.setText('这次探测没拿到版本号（命令可能不支持 --version），不影响保存')
        return filled

    # ── 保存 ────────────────────────────────────────────────────────────────
    def row_count(self) -> int:
        return len(self.items)

    def current_row(self) -> int:
        return self.table.currentRow() if _HAS_QT else -1

    def _on_double_clicked(self, item) -> None:
        if item is not None:
            self.save(item.row())

    def save_selected(self) -> bool:
        row = self.current_row()
        if row < 0:
            row = 0
        return self.save(row)

    def save(self, index: int) -> bool:
        """第 index 行 → 面板 save_candidate()（异步，经 PanelRequests）。"""
        if not (0 <= index < len(self.items)):
            return False
        started = self.panel.save_candidate(self.items[index])
        if started:
            self.close()
        return started

    def save_all(self) -> list:
        """逐条保存：失败的逐条报告，不中断其它条。返回目前已有的逐条结果。"""
        self.save_results = []
        self._save_queue = list(range(len(self.items)))
        self._advance_save_queue()
        return self.save_all_results()

    def save_all_results(self) -> list:
        return [dict(row) for row in self.save_results]

    def _advance_save_queue(self) -> None:
        while self._save_queue:
            index = self._save_queue.pop(0)
            item = self.items[index]
            outcome = {'index': index, 'id': str(item.get('id') or ''),
                       'ok': False, 'code': '', 'message': ''}

            def on_done(ok, backend_id, problem, outcome=outcome):
                outcome['ok'] = bool(ok)
                outcome['id'] = backend_id or outcome['id']
                if problem:
                    outcome['code'] = str(problem[0])
                    outcome['message'] = str(problem[1]) if len(problem) > 1 else ''
                self.save_results.append(outcome)
                self._advance_save_queue()

            started = (self.panel.save_candidate(item, on_done=on_done)
                       if self.panel is not None else False)
            if started:
                return                  # 落定后由 on_done 接着跑下一条
            outcome['code'] = 'not_started'
            outcome['message'] = '保存请求没有启动'
            self.save_results.append(outcome)
        self._report_save_all()

    def _report_save_all(self) -> None:
        saved = [row for row in self.save_results if row['ok']]
        failed = [row for row in self.save_results if not row['ok']]
        parts = ['成功 %d/%d 条' % (len(saved), len(self.save_results))]
        for row in failed:
            parts.append('✗ %s [%s] %s'
                         % (row['id'] or '#%d' % row['index'],
                            row['code'] or 'error', row['message']))
        text = '全部保存：' + '；'.join(parts)
        self.hint.setText(text)
        if self.panel is not None:
            self.panel._say(text)
