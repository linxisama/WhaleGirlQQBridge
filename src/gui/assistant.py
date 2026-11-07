"""控制台助手页面；长任务在后端运行，Qt 只收发短请求，无工作线程生命周期问题。"""
import html
import json
import sys
import threading
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest
from PyQt6.QtWidgets import (QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QInputDialog, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QTextEdit, QVBoxLayout, QWidget)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from .file_dialogs import open_files

# Agent 是控制台主体：只有对话窗口。动作由后台直接执行，只有本机操控动作会在聊天里请用户点一次确认。

from src.runtime_identity import backend_identity_error
from ..agent_scheduler import action_choices
from src import voice_client
from .dialogs import tell


class ChatBubble(QFrame):
    """一条对话气泡；内容先转义，模型输出不会变成 HTML。"""
    def __init__(self, text, user=False):
        super().__init__()
        self.setObjectName('chatBubbleUser' if user else 'chatBubbleAgent')
        self.setMaximumWidth(620)
        label = QLabel(html.escape(str(text or '')).replace('\n', '<br>'))
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setStyleSheet('background:transparent;border:0;color:#23384a;font-size:13px;')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(label)
        if user:
            self.setStyleSheet('QFrame#chatBubbleUser{background:#d9f3ee;border:0;border-radius:14px;}')
        else:
            self.setStyleSheet('QFrame#chatBubbleAgent{background:#ffffff;border:1px solid #e1eaf1;border-radius:14px;}')


class AgentPrompt(QTextEdit):
    """Enter sends; Shift+Enter newline. IME composition is left to Qt."""
    sendRequested = pyqtSignal()
    filesDropped = pyqtSignal(list)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction(); return
        super().dragEnterEvent(event)

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths); event.acceptProposedAction(); return
        super().dropEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)):
            self.sendRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class AssistantTab(QWidget):
    navigate = pyqtSignal(str)
    applied = pyqtSignal(dict)
    features = pyqtSignal()
    speech_status = pyqtSignal(str)

    def __init__(self, base_url, data_root, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.data_root = data_root
        self.queued_request = None
        self.network = None
        self.setObjectName('consoleAssistant')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.pending = None
        self.request_kind = ''
        self.job = None
        self._last_state = None
        self._reported = set()
        self.session_id = ''
        self.session_title = '默认对话'
        self._open_after_sessions = ''
        self._pending_skill_package = None
        self._open_skills_after = False
        self._last_agent_text = ''
        self._failures = 0
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.setSpacing(12)
        self.avatar = QLabel('鲸')
        self.avatar.setFixedSize(46, 46)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
        avatar_file = base / 'assets' / 'whale_girl_avatar.png'
        if avatar_file.is_file():
            self.avatar.setPixmap(QPixmap(str(avatar_file)))
            self.avatar.setScaledContents(True)
            self.avatar.setStyleSheet('background:transparent;border:0;')
        else:
            self.avatar.setStyleSheet('background:qlineargradient(x1:0,y1:0,x2:1,y2:1,'
                                      'stop:0 #6fd3c7,stop:1 #2f8fa8);color:#ffffff;'
                                      'border-radius:23px;font-size:20px;font-weight:bold;')
        header.addWidget(self.avatar)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.title = QLabel('鲸鱼娘')
        self.title.setStyleSheet('font-size:20px;font-weight:bold;color:#1c6f74;')
        title_box.addWidget(self.title)
        self.subtitle = QLabel('今天想让我帮你做什么？')
        self.subtitle.setStyleSheet('color:#5b7285;font-size:12px;')
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch()
        layout.addLayout(header)
        tools = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem('自动判断：人设 / 控制台', 'auto')
        self.mode.addItem('只生成人设', 'persona')
        tools.addWidget(self.mode)
        self.status_button = QPushButton('查看状态')
        self.status_button.clicked.connect(self._status)
        tools.addWidget(self.status_button)
        self.example_button = QPushButton('填入人设示例')
        self.example_button.clicked.connect(self._example)
        tools.addWidget(self.example_button)
        self.features_button=QPushButton('功能')
        self.features_button.setToolTip('功能与会话人设')
        self.features_button.clicked.connect(lambda checked=False:self.features.emit())
        self.learn_button = QPushButton('学习')
        self.learn_button.setToolTip('学习记录')
        self.learn_button.clicked.connect(self.open_learning)
        self.history_button = QPushButton('历史')
        self.history_button.setToolTip('操作历史')
        self.history_button.clicked.connect(self.open_history)
        self.import_button = QPushButton('技能包')
        self.import_button.setToolTip('导入技能包')
        self.import_button.clicked.connect(self.import_skill)
        tools.addStretch()
        # 顶部只保留对话本身；模式/示例等入口收进「更多」和技能工具栏，避免表单感。
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 6, 0)
        self.history_title = QLabel('对话')
        self.history_title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        ll.addWidget(self.history_title)
        self.history = QTextEdit(); self.history.setReadOnly(True); self.history.setVisible(False)
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setObjectName('agentChatScroll')
        self.chat_host = QWidget()
        self.chat_host.setObjectName('agentChatHost')
        self.chat_layout = QVBoxLayout(self.chat_host)
        self.chat_layout.setContentsMargins(4, 8, 8, 8)
        self.chat_layout.setSpacing(10)
        self.chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.welcome = QFrame()
        self.welcome.setObjectName('agentWelcome')
        welcome_layout = QVBoxLayout(self.welcome)
        welcome_layout.setContentsMargins(18, 16, 18, 16)
        welcome_layout.setSpacing(10)
        welcome_title = QLabel('嗨，我是鲸鱼娘')
        welcome_title.setStyleSheet('font-size:22px;font-weight:bold;color:#1c6f74;background:transparent;')
        welcome_layout.addWidget(welcome_title)
        welcome_sub = QLabel('说一句想改什么，我直接动手；要碰你的电脑时会先问你一次。')
        welcome_sub.setWordWrap(True)
        welcome_sub.setStyleSheet('color:#5b7285;font-size:13px;background:transparent;')
        welcome_layout.addWidget(welcome_sub)
        chips = QVBoxLayout()
        chips.setSpacing(6)
        for text in ('把回复调快一点', '记住我习惯叫老鲸', '打开表情插件'):
            chip = QPushButton(text)
            chip.setProperty('class', 'secondary')
            chip.clicked.connect(lambda checked=False, value=text: self.prompt.setPlainText(value))
            chips.addWidget(chip)
        welcome_layout.addLayout(chips)
        self.chat_layout.addWidget(self.welcome)
        self.chat_layout.addStretch(1)
        self.chat_scroll.setWidget(self.chat_host)
        self._bubbles = []
        ll.addWidget(self.chat_scroll, 1)
        self.composer = QFrame()
        self.composer.setObjectName('agentComposer')
        composer_layout = QVBoxLayout(self.composer)
        composer_layout.setContentsMargins(12, 10, 12, 10)
        composer_layout.setSpacing(8)
        self.prompt = AgentPrompt(); self.prompt.setAcceptRichText(False)
        self.prompt.sendRequested.connect(self.send)
        self.prompt.filesDropped.connect(self.add_attachments)
        self.prompt.setPlaceholderText('和鲸鱼娘 Agent 说你想做什么，例如：把回复调快一点，顺手打开表情插件')
        self.prompt.setMaximumHeight(92)
        composer_layout.addWidget(self.prompt)
        row = QHBoxLayout()
        self.attach_button = QPushButton('＋ 文件')
        self.attach_button.setToolTip('随消息附带本机文件，读取文本作为上下文；不会上传到公网')
        self.attach_button.clicked.connect(self.attach_files)
        row.addWidget(self.attach_button)
        self.skill_button = QPushButton('技能包')
        self.skill_button.clicked.connect(self.import_skill)
        row.addWidget(self.skill_button)
        self.attach_label = QLabel('')
        self.attach_label.setStyleSheet('color:#60798c;font-size:12px;')
        row.addWidget(self.attach_label)
        self.clear_attach_button = QPushButton('清空附件')
        self.clear_attach_button.setVisible(False)
        self.clear_attach_button.clicked.connect(self.clear_attachments)
        row.addWidget(self.clear_attach_button)
        self.attachments = []
        self.send_button = QPushButton('发送')
        self.send_button.clicked.connect(self.send)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.send_button); row.addWidget(self.cancel_button)
        self.retry_button = QPushButton('重新查询结果')
        self.retry_button.clicked.connect(self.poll)
        self.retry_button.setVisible(False)
        row.addWidget(self.retry_button)
        row.addStretch()
        composer_layout.addLayout(row)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        # 本机操控动作的一次性确认：只在轮到它时出现。
        self.confirm_button = QPushButton('允许这次操作')
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self.confirm)
        actions.addWidget(self.confirm_button)
        self.undo_button = QPushButton('撤销这次修改')
        self.undo_button.setVisible(False)
        self.undo_button.clicked.connect(self.undo)
        actions.addWidget(self.undo_button)
        self.good_button = QPushButton('这次对了')
        self.good_button.clicked.connect(lambda checked=False: self.feedback(1))
        self.bad_button = QPushButton('这次不对')
        self.bad_button.clicked.connect(lambda checked=False: self.feedback(-1))
        self.good_button.setVisible(False); self.bad_button.setVisible(False)
        actions.addWidget(self.good_button); actions.addWidget(self.bad_button)
        self.personas_button = QPushButton('人设页查看 / 导出')
        self.personas_button.clicked.connect(self._personas)
        actions.addWidget(self.personas_button)
        actions.addStretch()
        composer_layout.addLayout(actions)
        ll.addWidget(self.composer)
        left.setMinimumWidth(240)
        work = QHBoxLayout()
        work.setSpacing(8)
        work.addWidget(left, 1)
        self.rail = QFrame()
        self.rail.setObjectName('agentRail')
        self.rail.setFixedWidth(88)
        rail_layout = QVBoxLayout(self.rail)
        rail_layout.setContentsMargins(6, 4, 6, 4)
        rail_layout.setSpacing(8)
        self.sessions_button = QPushButton('会话')
        self.sessions_button.setToolTip('多段对话与任务卡片')
        self.sessions_button.clicked.connect(self.open_sessions)
        self.guide_button = QPushButton('说明')
        self.guide_button.setToolTip('使用说明')
        self.guide_button.clicked.connect(self.open_guide)
        self.skills_button = QPushButton('技能')
        self.skills_button.setToolTip('技能与工具面板')
        self.skills_button.clicked.connect(self.open_skills)
        self.schedules_button = QPushButton('定时')
        self.schedules_button.setToolTip('定时任务：连接检查 / 日报')
        self.schedules_button.clicked.connect(self.open_schedules)
        self.speak_button = QPushButton('朗读')
        self.speak_button.setToolTip('用本地语音组件朗读上一条 Agent 回复')
        self.speak_button.clicked.connect(self.read_aloud)
        self.admin_button = QPushButton('管理员')
        self.admin_button.setToolTip('绑定 Agent 管理员 QQ')
        self.admin_button.clicked.connect(self.open_admin)
        self.local_agent_button = QPushButton('本地 Agent')
        self.local_agent_button.setToolTip('检查 DSH MCP 记忆/控制桥和 DSH QQBot 插件')
        self.local_agent_button.clicked.connect(self.open_local_agent)
        for button in (self.sessions_button, self.guide_button, self.skills_button,
                       self.schedules_button, self.speak_button, self.admin_button,
                       self.local_agent_button,
                       self.learn_button, self.history_button,
                       self.import_button, self.features_button):
            button.setMinimumHeight(38)
            rail_layout.addWidget(button)
        rail_layout.addStretch()
        self.rail_badge = QLabel('鲸')
        self.rail_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rail_badge.setStyleSheet('color:#2f8fa8;font-size:18px;')
        rail_layout.addWidget(self.rail_badge)
        work.addWidget(self.rail)
        layout.addLayout(work, 1)
        self.notice = QLabel('支持人设、启停、聊天插件开关、回复等待与长度、页面导航、长期偏好与本机操控（操控前会请你确认一次）。')
        self.notice.setWordWrap(True); self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.speech_status.connect(self.notice.setText)
        layout.addWidget(self.notice)
        self.timer = QTimer(self); self.timer.setInterval(700); self.timer.timeout.connect(self.poll)
        self.apply_panel_alpha(.65)

    def apply_panel_alpha(self, alpha, glass=False):
        channel=round(255*max(0.0,min(1.0,float(alpha))))
        # 有背景图时（glass）外面那层 agentChatHost 已经是玻璃底，欢迎卡再叠一层白
        # 等于把照片糊两遍（实测 0.65 + 0.65 叠成 0.88 白），所以它只留描边。
        welcome = 'transparent' if glass else f'rgba(255,255,255,{channel})'
        # 页底与滚动容器透出主窗口背景；正文面板跟随统一外观设置。
        self.setStyleSheet('QWidget#consoleAssistant {background:transparent;color:#26364b;}'
            'QLabel,QCheckBox {color:#26364b;background:transparent;}'
            'QStackedWidget,QScrollArea,QScrollArea > QWidget > QWidget {background:transparent;border:0;}'
            f'QTextEdit,QLineEdit,QComboBox {{background:rgba(255,255,255,{channel});color:#18324d;'
            'border:1px solid #cfdae5;border-radius:6px;padding:6px;}'
            'QComboBox QAbstractItemView {background:#ffffff;color:#18324d;}'
            f'QFrame#agentComposer {{background:rgba(255,255,255,{channel});border:1px solid #cfdae5;'
            'border-radius:18px;}'
            'QFrame#agentComposer QTextEdit {border:0;background:transparent;padding:2px;}'
            f'QFrame#agentRail {{background:rgba(255,255,255,{max(60, channel - 40)});'
            'border:1px solid #d8e3ec;border-radius:20px;}'
            'QFrame#agentRail QPushButton {border:0;background:transparent;color:#2c5d6b;'
            'font-size:12px;border-radius:16px;padding:6px 2px;}'
            'QFrame#agentRail QPushButton:hover {background:#e6f4f1;}'
            f'QWidget#agentChatHost {{background:rgba(255,255,255,{channel});border:1px solid #e1eaf1;'
            'border-radius:16px;}'
            'QScrollArea#agentChatScroll,QWidget#chatRow {background:transparent;border:0;}'
            f'QFrame#agentWelcome {{background:{welcome};border:1px solid #e1eaf1;'
            'border-radius:16px;}')

    def _append(self, text):
        # 控制台只渲染纯文本气泡；先转义，模型输出里的 <b> 不会被当 HTML 执行。
        value = str(text or '')
        user = value.startswith('你：')
        agent = value.startswith(('助手：', 'Agent：'))
        body = value[2:].strip() if user else value
        if agent:
            body = body.split('：', 1)[-1].strip() if '：' in body.split('\n')[0] else body
        if not user:
            self._last_agent_text = body
        cursor = self.history.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(value + '\n')
        self.history.document().setMaximumBlockCount(300)
        self.welcome.setVisible(False)
        row = QWidget()
        row.setObjectName('chatRow')
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(2, 0, 2, 0)
        row_layout.setSpacing(8)
        bubble = ChatBubble(body, user=user)
        if user:
            row_layout.addStretch(1)
            row_layout.addWidget(bubble)
        else:
            row_layout.addWidget(bubble)
            row_layout.addStretch(1)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        self._bubbles.append(row)
        QTimer.singleShot(0, lambda: self.chat_scroll.verticalScrollBar().setValue(
            self.chat_scroll.verticalScrollBar().maximum()))

    def _clear_chat(self):
        for row in self._bubbles:
            row.setParent(None)
            row.deleteLater()
        self._bubbles.clear()
        self.history.clear()
        self.welcome.setVisible(True)

    def _example(self):
        self.mode.setCurrentIndex(1)
        self.prompt.setPlainText('生成一个叫鲸鱼妈妈的人设，温柔可靠，偶尔开玩笑，回复简短自然，不用每句叫主人。给三组聊天示例。')

    def _personas(self):
        self.navigate.emit('人设')

    def _status(self):
        if not self.pending and not self.timer.isActive():
            self._submit('查看状态', 'auto', '')

    def _request(self, kind, path, data=None, *, verified=False):
        if self.pending:
            return
        if not verified:
            self.queued_request = (kind, path, data)
            self._request('identity', 'runtime', verified=True)
            return
        request = QNetworkRequest(QUrl(self.base_url + '/api/' + path))
        if self.network is None:
            self.network = QNetworkAccessManager(self)
            self.network.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
            self.network.finished.connect(self._response)
        request.setTransferTimeout(3000)
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, 'application/json')
        self.request_kind = kind
        if kind == 'submit':
            self.present({'id': data['request_id'], 'state': 'planning', 'message': '正在提交要求…'})
        self.pending = (self.network.get(request) if data is None else
                        self.network.post(request, json.dumps(data, ensure_ascii=False).encode('utf-8')))
        self._buttons()

    def _buttons(self):
        state = str((self.job or {}).get('state') or '')
        busy = self.pending is not None or state in ('planning', 'applying', 'undoing')
        self.send_button.setEnabled(not busy)
        self.status_button.setEnabled(not busy)
        self.example_button.setEnabled(not busy)
        self.confirm_button.setEnabled(not busy and state == 'confirm')
        self.cancel_button.setEnabled(self.pending is None and state in ('planning', 'confirm'))
        self.retry_button.setVisible(state == 'disconnected')
        self.retry_button.setEnabled(self.pending is None)
        applied = state == 'applied'
        self.good_button.setVisible(applied); self.bad_button.setVisible(applied)
        self.good_button.setEnabled(not busy); self.bad_button.setEnabled(not busy)
        self.undo_button.setVisible(applied and bool(self.job.get('can_undo')))
        self.undo_button.setEnabled(not busy)
        self.learn_button.setEnabled(not busy); self.import_button.setEnabled(not busy)
        self.history_button.setEnabled(not busy)

    def send(self):
        prompt = self.prompt.toPlainText().strip()
        if not prompt and not self.attachments:
            self.notice.setText('请填写 1～4000 字的要求，或附带文件。'); return
        if len(prompt) > 4000:
            self.notice.setText('请填写 1～4000 字的要求。'); return
        attachments = list(self.attachments)
        self.clear_attachments()
        self._submit(prompt, self.mode.currentData(), attachments)

    def attach_files(self):
        workspace = Path(r'D:\工作\agent-workspace')
        start = workspace if workspace.is_dir() else (Path.home() / 'Documents')
        paths, _ = open_files(self, '附带文件给 Agent', str(start), '文本/代码/表格/图片 (*.*)')
        if paths:
            self.add_attachments(paths)

    def add_attachments(self, paths):
        for path in list(paths or []):
            if path not in self.attachments and len(self.attachments) < 5:
                self.attachments.append(path)
        self._render_attachments()
        self.notice.setText('文件只会本机读取文本上下文，不会上传到公网。')

    def clear_attachments(self):
        self.attachments = []
        self._render_attachments()

    def _render_attachments(self):
        names = '、'.join(Path(p).name for p in self.attachments)
        self.attach_label.setText(('已附带：' + names) if names else '')
        self.clear_attach_button.setVisible(bool(self.attachments))

    def _submit(self, prompt, mode, attachments=None):
        shown = prompt or ('（附带文件：' + '、'.join(Path(p).name for p in (attachments or [])) + '）')
        self._append('你：' + shown)
        request_id = uuid.uuid4().hex
        self._request('submit', 'assistant/jobs', {'prompt': prompt, 'mode': mode,
                                                 'request_id': request_id,
                                                 'session_id': self.session_id,
                                                 'attachments': list(attachments or [])})

    def poll(self):
        if self.job and not self.pending:
            self._request('poll', f'assistant/jobs/{self.job["id"]}')

    def cancel(self):
        if self.job:
            self._request('cancel', f'assistant/jobs/{self.job["id"]}/cancel', {})

    def undo(self):
        if not self.job or self.job.get('state') != 'applied' or self.pending:
            return
        self._request('undo', f"assistant/jobs/{self.job['id']}/undo", {})

    def confirm(self):
        if not self.job or self.job.get('state') != 'confirm' or self.pending:
            return
        self._request('confirm', f'assistant/jobs/{self.job["id"]}/confirm', {})

    def _aux_dialog(self):
        from .qt_locale import light_palette
        dialog=QDialog(self)
        dialog.setObjectName('agentAuxDialog')
        dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        dialog.setAutoFillBackground(True)
        dialog.setPalette(light_palette())
        # 顶层弹窗不能继承主窗口的透明底：Windows 下会绘制为黑色。
        dialog.setStyleSheet("""
            QDialog#agentAuxDialog, QDialog#agentAuxDialog QWidget {background:#f4f7fb;color:#26364b;}
            QDialog#agentAuxDialog QLabel {background:transparent;color:#26364b;}
            QDialog#agentAuxDialog QListWidget, QDialog#agentAuxDialog QListWidget QWidget,
            QDialog#agentAuxDialog QTextEdit, QDialog#agentAuxDialog QTextEdit QWidget,
            QDialog#agentAuxDialog QLineEdit, QDialog#agentAuxDialog QComboBox,
            QDialog#agentAuxDialog QSpinBox {background:#ffffff;color:#18324d;border:1px solid #cfdce8;}
            QDialog#agentAuxDialog QAbstractItemView::item:selected {background:#ccece8;color:#18324d;}
            QDialog#agentAuxDialog QPushButton {background:#2da6a0;color:#ffffff;border:0;border-radius:6px;padding:8px 12px;}
            QDialog#agentAuxDialog QPushButton:disabled {background:#e1e8f0;color:#68778c;}
        """)
        return dialog

    def open_skills(self):
        if self.pending:
            return
        self._open_skills_after = True
        self._request('learning_skills', 'assistant/learning')

    def _skills_dialog(self):
        if getattr(self, 'skills_dialog', None) is None:
            dialog = self._aux_dialog()
            dialog.setWindowTitle('技能与工具')
            dialog.resize(680, 520)
            layout = QVBoxLayout(dialog)
            hint = QLabel('技能只作为规划说明使用，不执行代码；权限声明仅用于提示与审阅。')
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.skills_list = QListWidget()
            layout.addWidget(self.skills_list, 1)
            self.skills_empty=QLabel('还没有导入技能包，可点击下方“导入技能包”。')
            self.skills_empty.setWordWrap(True)
            layout.addWidget(self.skills_empty)
            row = QHBoxLayout()
            toggle = QPushButton('启用 / 停用')
            toggle.clicked.connect(self._toggle_skill)
            delete = QPushButton('删除')
            delete.clicked.connect(self._delete_skill)
            import_button = QPushButton('导入技能包')
            import_button.clicked.connect(self.import_skill)
            close = QPushButton('关闭')
            close.clicked.connect(dialog.accept)
            for button in (toggle, delete, import_button):
                row.addWidget(button)
            row.addStretch()
            row.addWidget(close)
            layout.addLayout(row)
            self.skills_dialog = dialog
        return self.skills_dialog

    def _show_skills(self, data):
        dialog = self._skills_dialog()
        self.skills_list.clear()
        for skill in data.get('skills') or []:
            perms = '、'.join(skill.get('permissions') or []) or '无权限声明'
            state = '已启用' if skill.get('enabled', True) else '已停用'
            row = QListWidgetItem(f"{skill.get('name')}（{skill.get('id')}）· {state} · {perms}")
            row.setData(256, skill.get('id'))
            row.setData(257, bool(skill.get('enabled', True)))
            if not skill.get('enabled', True):
                row.setForeground(Qt.GlobalColor.gray)
            self.skills_list.addItem(row)
        self.skills_empty.setVisible(self.skills_list.count()==0)
        dialog.show(); dialog.raise_(); dialog.activateWindow()

    def _toggle_skill(self):
        item = self.skills_list.currentItem()
        if item is None or self.pending:
            return
        self._request('skill_toggle', f"assistant/skills/{item.data(256)}/toggle",
                      {'enabled': not bool(item.data(257))})

    def _delete_skill(self):
        item = self.skills_list.currentItem()
        if item is None or self.pending:
            return
        if QMessageBox.question(self, '删除技能', '删除选中的技能包？') != QMessageBox.StandardButton.Yes:
            return
        self._request('skill_delete', 'assistant/learning/forget',
                      {'skill_id': item.data(256)})

    def open_schedules(self):
        if self.pending:
            return
        self._request('schedules', 'assistant/schedules')

    def _schedules_dialog(self):
        if getattr(self, 'schedules_dialog', None) is None:
            dialog = self._aux_dialog()
            dialog.setWindowTitle('Agent 定时任务')
            dialog.resize(700, 540)
            layout = QVBoxLayout(dialog)
            hint = QLabel('后台每 30 秒检查一次，不会发消息。夜间固化记忆与重写自画像按设置页开关生效。')
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.schedules_list = QListWidget()
            layout.addWidget(self.schedules_list, 1)
            form = QHBoxLayout()
            self.schedule_name = QLineEdit()
            self.schedule_name.setPlaceholderText('任务名称')
            self.schedule_action = QComboBox()
            for label, key in action_choices():
                self.schedule_action.addItem(label, key)
            self.schedule_minutes = QSpinBox()
            self.schedule_minutes.setRange(5, 1440)
            self.schedule_minutes.setValue(60)
            add = QPushButton('添加')
            add.clicked.connect(self._create_schedule)
            for widget in (self.schedule_name, self.schedule_action, self.schedule_minutes, add):
                form.addWidget(widget)
            layout.addLayout(form)
            row = QHBoxLayout()
            run = QPushButton('立即运行')
            run.clicked.connect(self._run_schedule)
            toggle = QPushButton('启用 / 停用')
            toggle.clicked.connect(self._toggle_schedule)
            delete = QPushButton('删除')
            delete.clicked.connect(self._delete_schedule)
            close = QPushButton('关闭')
            close.clicked.connect(dialog.accept)
            for button in (run, toggle, delete):
                row.addWidget(button)
            row.addStretch()
            row.addWidget(close)
            layout.addLayout(row)
            self.schedules_dialog = dialog
        return self.schedules_dialog

    def _show_schedules(self, data):
        dialog = self._schedules_dialog()
        self.schedules_list.clear()
        for task in data.get('tasks') or []:
            text = (f"{task.get('name')} · 每 {task.get('every_minutes')} 分钟 · "
                    f"{'启用' if task.get('enabled') else '停用'}")
            if task.get('last_result'):
                text += ' · ' + str(task['last_result']).splitlines()[0][:60]
            row = QListWidgetItem(text)
            row.setData(256, task.get('id'))
            row.setData(257, bool(task.get('enabled')))
            self.schedules_list.addItem(row)
        dialog.show(); dialog.raise_(); dialog.activateWindow()

    def _create_schedule(self):
        if self.pending:
            return
        self._request('schedule_create', 'assistant/schedules', {
            'name': self.schedule_name.text().strip() or None,
            'action': self.schedule_action.currentData(),
            'every_minutes': self.schedule_minutes.value()})

    def _run_schedule(self):
        item = self.schedules_list.currentItem()
        if item is None or self.pending:
            return
        self._request('schedule_run', f"assistant/schedules/{item.data(256)}/run", {})

    def _toggle_schedule(self):
        item = self.schedules_list.currentItem()
        if item is None or self.pending:
            return
        self._request('schedule_toggle', f"assistant/schedules/{item.data(256)}/toggle",
                      {'enabled': not bool(item.data(257))})

    def _delete_schedule(self):
        item = self.schedules_list.currentItem()
        if item is None or self.pending:
            return
        if QMessageBox.question(self, '删除定时任务', '删除选中的定时任务？') != QMessageBox.StandardButton.Yes:
            return
        self._request('schedule_delete', f"assistant/schedules/{item.data(256)}/delete", {})

    def read_aloud(self):
        text = (self._last_agent_text or '').strip()
        if not text:
            self.notice.setText('还没有可朗读的 Agent 回复。')
            return
        self.notice.setText('正在朗读…（本地语音组件）')
        threading.Thread(target=self._speak_worker, args=(text,), daemon=True).start()

    def _speak_worker(self, text):
        try:
            voice_client.speak(text)
            self.speech_status.emit('朗读完成。')
        except (voice_client.VoiceUnavailable, ValueError) as exc:
            self.speech_status.emit(str(exc) + '；可在「插件市场\\语音组件」安装后启动。')
        except Exception as exc:
            self.speech_status.emit('朗读失败：' + type(exc).__name__)

    def open_local_agent(self):
        if self.pending:
            return
        self._request('local_agent', 'assistant/local-agent')

    def _show_local_agent(self, data):
        # bot_state / bot_alive 是探活结果：配置登记齐全不等于 DSH 里此刻真能连上，
        # 只报「已登记」会让用户以为接好了，实际控制台没开。
        state = str(data.get('bot_state') or '')
        alive = data.get('bot_alive')
        # 探不通时必须带上探的地址：用户改了端口却以为没生效，是这类问题的第一大成因。
        probed = str((data.get('bot_probe') or {}).get('url') or '').strip()
        if state == 'broken':
            alive_text = '配置不完整，DSH 里拉不起来'
        elif alive is True:
            alive_text = '控制台可达'
        elif alive is False:
            alive_text = '配置能启动，但控制台此刻探不通'
            if probed:
                alive_text += '（探的地址：%s）' % probed
        else:
            alive_text = '未配置 WHALE_BOT_URL，无法探活'
        lines = [
            'DSH 目录：' + str(data.get('dsh_home') or ''),
            'profile：' + str(data.get('profile') or ''),
            '记忆 MCP：' + ('已登记' if data.get('memory_bridge') else '未登记'),
            '控制 MCP：' + ('已登记' if data.get('bot_bridge') else '未登记'),
            '控制台探活：' + alive_text,
            'QQBot 入口：' + ('已配置' if data.get('qqbot_entry') else '未配置'),
            'QQBot 插件：' + ('已安装' if data.get('qqbot_plugin_installed') else '未安装'),
            'QQBot 凭据：' + ('已填' if data.get('qqbot_credentials_configured') else '未填/待扫码'),
            '鲸鱼娘插件：' + ('已安装' if data.get('whalegirl_plugin_installed') else '未安装'),
            '',
        ]
        alive_hint = str(data.get('bot_alive_hint') or '').strip()
        if alive_hint:
            lines.append(alive_hint)
        problems = [str(item) for item in (data.get('bot_problems') or []) if str(item).strip()]
        if problems:
            lines.append('待处理：')
            lines.extend('- ' + item for item in problems[:6])
        hint = str(data.get('hint') or '').strip()
        if hint:
            lines.append(hint)
        tell(self, '本地 Agent 关联', '\n'.join(lines), ok_text='知道了')

    def open_admin(self):
        if self.pending:
            return
        self._request('admin', 'assistant/admin')

    def _show_admin(self, data):
        dialog = self._aux_dialog()
        dialog.setWindowTitle('Agent 管理员')
        dialog.resize(440, 230)
        layout = QVBoxLayout(dialog)
        masked = str(data.get('masked') or '')
        status = QLabel('当前管理员：' + (masked if data.get('bound') else '未绑定'))
        status.setStyleSheet('font-size:15px;color:#1c6f74;')
        layout.addWidget(status)
        hint = QLabel('管理员用于确认高风险操作；Agent 不会因此获得电脑控制、密钥或外发权限。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText('管理员 QQ 号（5～15 位数字）')
        edit.setText(str(data.get('admin') or ''))
        row.addWidget(edit, 1)
        bind = QPushButton('绑定')
        bind.clicked.connect(lambda: (self._request('admin_bind', 'assistant/admin/bind',
                                                     {'qq': edit.text().strip()}), dialog.accept()))
        unbind = QPushButton('解绑')
        unbind.clicked.connect(lambda: (self._request('admin_unbind', 'assistant/admin/unbind'),
                                        dialog.accept()))
        row.addWidget(bind); row.addWidget(unbind)
        layout.addLayout(row)
        close = QPushButton('关闭')
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def open_sessions(self):
        if self.pending:
            return
        self._request('sessions', 'assistant/sessions')

    def _session_dialog(self):
        if getattr(self, 'session_dialog', None) is None:
            dialog = self._aux_dialog()
            dialog.setWindowTitle('Agent 对话')
            dialog.resize(560, 460)
            layout = QVBoxLayout(dialog)
            hint = QLabel('每段对话独立保留任务卡片；切换后对话记录会分开，避免串台。')
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.session_list = QListWidget()
            layout.addWidget(self.session_list, 1)
            row = QHBoxLayout()
            new = QPushButton('新对话')
            new.clicked.connect(self._new_session)
            open_button = QPushButton('打开选中')
            open_button.clicked.connect(self._open_session)
            delete = QPushButton('删除选中')
            delete.clicked.connect(self._delete_session)
            close = QPushButton('关闭')
            close.clicked.connect(dialog.accept)
            for button in (new, open_button, delete):
                row.addWidget(button)
            row.addStretch()
            row.addWidget(close)
            layout.addLayout(row)
            self.session_dialog = dialog
        return self.session_dialog

    def _show_sessions(self, data):
        dialog = self._session_dialog()
        self.session_list.clear()
        for item in data.get('sessions') or []:
            text = f"{item.get('title') or '新对话'} · {item.get('turns', 0)} 条"
            row = QListWidgetItem(text)
            row.setData(256, item.get('id'))
            self.session_list.addItem(row)
            if item.get('id') == self.session_id:
                self.session_list.setCurrentItem(row)
        if self.session_list.currentItem() is None and self.session_list.count():
            self.session_list.setCurrentRow(0)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _new_session(self):
        if self.pending:
            return
        self._request('session_create', 'assistant/sessions', {'title': '新对话'})

    def _open_session(self):
        item = self.session_list.currentItem()
        if item is None:
            self.notice.setText('先选一段对话。')
            return
        if self.pending:
            return
        self._request('session_open', f"assistant/sessions/{item.data(256)}")

    def _delete_session(self):
        item = self.session_list.currentItem()
        if item is None:
            return
        if QMessageBox.question(self, '删除对话', '删除选中的对话和它的任务卡片？') != QMessageBox.StandardButton.Yes:
            return
        self._request('session_delete', f"assistant/sessions/{item.data(256)}/delete", {})

    def _render_session(self, session):
        self.session_id = str(session.get('id') or '')
        self.session_title = str(session.get('title') or '新对话')
        self.sessions_button.setToolTip('多段对话 · 当前：' + self.session_title)
        self.job = None
        self._last_state = None
        self._clear_chat()
        labels = {'applied': '已应用', 'undone': '已撤销', 'failed': '失败', 'reuse': '复用经验',
                  'recall': '只查询', 'answered': '已回复', 'confirm': '待确认'}
        turns = session.get('turns') or []
        if not turns:
            self._append('这是新对话，直接输入要求开始。')
        for turn in turns[-50:]:
            label = labels.get(str(turn.get('event') or ''), '记录')
            text = f"[{label}] {turn.get('prompt') or ''}"
            if turn.get('summary'):
                text += ' — ' + str(turn['summary'])
            self._append(text)
        self._buttons()

    def open_guide(self):
        dialog = self._aux_dialog()
        dialog.setWindowTitle('鲸鱼娘 Agent · 使用说明')
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        title = QLabel('第一次用 Agent？按这 5 步走')
        title.setStyleSheet('font-size:18px;font-weight:bold;color:#1c6f74;')
        layout.addWidget(title)
        body = QTextEdit()
        body.setReadOnly(True)
        body.setHtml("""
        <style>
        h3 { color:#1c6f74; margin:14px 0 4px; }
        p, li { color:#2b3d4d; line-height:1.65; }
        code { background:#eef3f8; padding:1px 5px; border-radius:4px; }
        .tip { color:#5b7285; }
        </style>
        <h3>1. 直接说你要做什么</h3>
        <p>在底部输入框写一句自然语言，例如 <code>把回复调快一点</code>、
        <code>打开表情插件</code>、<code>生成一个温柔的人设</code>。
        Agent 会直接执行白名单动作，并在对话里报告结果。</p>
        <h3>2. 直接执行</h3>
        <p>没有确认页：Agent 拿到 1～3 个动作就直接执行，结果写在对话里。
        启停服务、开文件/网页、敲键盘、截图这类本机操控会先请你点一次「允许这次操作」；
        全部动作都受白名单限制，不会购物或付款。</p>
        <h3>3. 改错了就撤销</h3>
        <p>执行后出现「撤销这次修改」：设置、插件开关、人设切换、启停、偏好、
        本次创建的人设都能还原。若执行后又手动改过，会拒绝自动撤销，避免覆盖新改动。</p>
        <h3>4. 告诉它这次对不对</h3>
        <p>「这次对了 / 这次不对」会调整经验权重；下次相似要求会优先复用成功做法，
        高置信度的轻量做法不再调用模型。撤销也会记为一次负反馈。</p>
        <h3>5. 查记忆、看历史、导技能</h3>
        <p>直接问 <code>机器人记得什么</code> 可以只读查询记忆；
        「操作历史」保留每段对话的任务卡片；「技能包」可以导入 JSON 扩展能力，
        导入前会先预览名称、触发词与权限声明。</p>
        <h3>几个例子</h3>
        <ul>
        <li>把模型等待上限改成 30 秒，顺便把历史条数调到 30</li>
        <li>记住我习惯叫老鲸</li>
        <li>把回复调快一点，再打开随机接话（最多 3 步）</li>
        <li>机器人记得我什么？</li>
        </ul>
        <p class="tip">安全边界：聊天里的机器人只能管理机器人自身，不能截图、关机、
        运行程序或读写宿主文件；本机 DSH 的 MCP 控制也需要控制台开着才生效。</p>
        """)
        body.setStyleSheet('QTextEdit{background:#ffffff;border:1px solid #d6e2ea;'
                           'border-radius:10px;padding:12px;}')
        layout.addWidget(body, 1)
        buttons = QHBoxLayout()
        example = QPushButton('填入示例')
        example.clicked.connect(lambda: (self.prompt.setPlainText('把回复调快一点，顺便打开表情插件'),
                                         dialog.accept()))
        buttons.addWidget(example)
        buttons.addStretch()
        close = QPushButton('知道了')
        close.clicked.connect(dialog.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.exec()

    def open_history(self):
        if self.pending:
            return
        self._request('history', 'assistant/history')

    def _history_dialog(self):
        if getattr(self, 'history_dialog', None) is None:
            dialog = self._aux_dialog()
            dialog.setWindowTitle('Agent 操作历史')
            dialog.resize(700, 520)
            layout = QVBoxLayout(dialog)
            hint = QLabel('最近 20 条规划/应用/撤销记录。选中一条已应用的记录可以撤销。')
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.history_list = QListWidget()
            layout.addWidget(self.history_list)
            row = QHBoxLayout()
            self.history_undo_button = QPushButton('撤销选中')
            self.history_undo_button.clicked.connect(self._undo_selected)
            row.addWidget(self.history_undo_button)
            refresh = QPushButton('刷新')
            refresh.clicked.connect(self.open_history)
            row.addWidget(refresh)
            row.addStretch()
            layout.addLayout(row)
            self.history_dialog = dialog
        return self.history_dialog

    def _show_history(self, data):
        dialog = self._history_dialog()
        self.history_list.clear()
        for item in data.get('history') or []:
            when = time.strftime('%m-%d %H:%M', time.localtime(float(item.get('time') or 0)))
            event = str(item.get('event') or '')
            label = {'applied': '已应用', 'undone': '已撤销', 'failed': '失败', 'reuse': '复用经验',
                     'recall': '只查询', 'answered': '已回复', 'confirm': '待确认'}.get(event, event)
            text = f"[{when}] {label} · {item.get('prompt', '')}"
            if item.get('summary'):
                text += ' — ' + str(item['summary'])
            row = QListWidgetItem(text)
            row.setData(256, item.get('job'))
            row.setData(257, event)
            self.history_list.addItem(row)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _undo_selected(self):
        item = self.history_list.currentItem()
        if item is None:
            self.notice.setText('先在操作历史里选一条记录。')
            return
        job_id = item.data(256)
        if not job_id:
            self.notice.setText('这条记录没有关联任务。')
            return
        self._request('undo', f'assistant/jobs/{job_id}/undo', {})

    def open_learning(self):
        if self.pending:
            return
        self._request('learning', 'assistant/learning')

    def import_skill(self):
        if self.pending:
            return
        from . import file_dialogs
        path, _ = file_dialogs.open_file(self, '导入技能包', '', 'JSON 技能包 (*.json)')
        if not path:
            return
        try:
            package = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            self.notice.setText('技能包读取失败：需要 UTF-8 编码的 JSON 文件。')
            return
        self._pending_skill_package = (package, Path(path).name)
        self._request('skill_preview', 'assistant/skills/preview', {'package': package})

    def feedback(self, score):
        if self.pending or not self.job or self.job.get('state') != 'applied':
            return
        self._request('feedback', f"assistant/jobs/{self.job['id']}/feedback", {'score': score})

    def _learning_dialog(self):
        if getattr(self, 'learning_dialog', None) is None:
            dialog = self._aux_dialog()
            dialog.setWindowTitle('学习记录')
            dialog.resize(660, 540)
            layout = QVBoxLayout(dialog)
            self.learning_summary = QLabel()
            self.learning_summary.setWordWrap(True)
            layout.addWidget(self.learning_summary)
            self.learning_list = QListWidget()
            layout.addWidget(self.learning_list)
            row = QHBoxLayout()
            self.forget_button = QPushButton('忘记选中项')
            self.forget_button.clicked.connect(self._forget)
            row.addWidget(self.forget_button)
            self.maintain_button = QPushButton('整理学习库')
            self.maintain_button.clicked.connect(self._maintain)
            row.addWidget(self.maintain_button)
            row.addStretch()
            layout.addLayout(row)
            self.learning_dialog = dialog
        return self.learning_dialog

    def _show_learning(self, data):
        dialog = self._learning_dialog()
        stats = data.get('stats') or {}
        self.learning_summary.setText(
            f"经验 {stats.get('lessons', 0)} 条 · 技能 {stats.get('skills', 0)} 个 · "
            f"偏好 {stats.get('preferences', 0)} 条 · 成功 {stats.get('success', 0)} / "
            f"失败 {stats.get('failure', 0)}")
        self.learning_list.clear()
        for item in data.get('lessons') or []:
            text = f"[{item.get('ok', 0)}✓/{item.get('fail', 0)}✗] {item.get('intent', '')}"
            if item.get('summary'):
                text += ' — ' + str(item['summary'])
            row = QListWidgetItem(text)
            row.setData(256, item.get('id'))
            row.setData(257, 'lesson')
            self.learning_list.addItem(row)
        for pref in data.get('preferences') or []:
            row = QListWidgetItem(f"偏好「{pref.get('key')}」：{pref.get('value')}")
            row.setData(256, pref.get('key'))
            row.setData(257, 'preference')
            self.learning_list.addItem(row)
        for skill in data.get('skills') or []:
            row = QListWidgetItem(
                f"技能「{skill.get('name')}」：{skill.get('description', '')}")
            row.setData(256, skill.get('id'))
            row.setData(257, 'skill')
            self.learning_list.addItem(row)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _forget(self):
        item = self.learning_list.currentItem()
        if item is None:
            self.notice.setText('先在学习记录里选一条经验。')
            return
        identifier = item.data(256)
        kind = item.data(257) or 'lesson'
        if not identifier:
            self.notice.setText('这一项没有可删除的标识。')
            return
        key = {'lesson': 'lesson_id', 'preference': 'preference', 'skill': 'skill_id'}.get(kind, 'lesson_id')
        self._request('forget', 'assistant/learning/forget', {key: identifier})

    def _maintain(self):
        if self.pending:
            return
        self._request('maintain', 'assistant/learning/maintain', {})

    def _response(self, reply):
        if reply is not self.pending:
            reply.deleteLater(); return
        kind = self.request_kind
        self.pending = None
        try:
            data = json.loads(bytes(reply.readAll()).decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            from urllib.parse import urlsplit
            parsed=urlsplit(self.base_url)
            target=f'{parsed.hostname or "本机"}:{parsed.port or 80}'
            status=reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            reason=(f'接口返回 HTTP {status}' if status else '后台未响应')
            data = {'success': False, 'message': f'{reason}（当前账号后台 {target}）。请检查对应账号控制台，未读取到数据不等于没有记录。'}
        finally:
            reply.deleteLater()
        if kind == 'identity':
            queued = self.queued_request
            self.queued_request = None
            if not data.get('app') and data.get('success') is False:
                error=str(data.get('message') or data.get('detail') or '当前账号后台身份验证失败')
            else:
                error = backend_identity_error(data, self.data_root)
            from urllib.parse import urlsplit
            parsed=urlsplit(self.base_url)
            error=error.replace('8090端口',f'当前账号后台 {parsed.hostname or "本机"}:{parsed.port or 80}')
            if not error and queued:
                self._request(*queued, verified=True)
                return
            data = {'success': False, 'message': error}
            kind = queued[0] if queued else 'identity'
        if data.get('success') is not True:
            self._failures += 1
            self.notice.setText(str(data.get('message') or data.get('detail') or '请求未完成'))
            if kind == 'confirm':
                # 确认回包丢失时查询同一任务，不能自动重发执行。
                self.timer.start()
            if kind == 'submit' and self.job:
                # 提交回包丢失时仍知道请求 ID；查询该任务，不重新消耗一次模型调用。
                self.timer.start()
            if kind == 'poll' and self._failures >= 5:
                self.timer.stop()
                self.notice.setText('后台暂时不可达，已停止轮询。重新打开控制台后检查操作结果。')
                if self.job:
                    self.job['state'] = 'disconnected'
            self._buttons(); return
        self._failures = 0
        if kind == 'submit':
            self.prompt.clear()
            if data.get('session_id'):
                self.session_id = str(data['session_id'])
        if kind == 'local_agent':
            self._show_local_agent(data)
            self._buttons()
            return
        if kind in ('admin', 'admin_bind', 'admin_unbind'):
            if kind == 'admin':
                self._show_admin(data)
            else:
                self.notice.setText('管理员绑定已更新。')
                self._show_admin(data)
            self._buttons()
            return
        if kind == 'sessions':
            self._show_sessions(data)
            if self._open_after_sessions:
                target = self._open_after_sessions
                self._open_after_sessions = ''
                self._request('session_open', f"assistant/sessions/{target}")
                return
            self._buttons()
            return
        if kind == 'session_create':
            session = data.get('session') or {}
            if session.get('id'):
                self.session_id = str(session['id'])
                self.session_title = str(session.get('title') or '新对话')
                self._open_after_sessions = self.session_id
                self._request('sessions', 'assistant/sessions')
            return
        if kind == 'session_open':
            session = data.get('session') or {}
            if session.get('id'):
                self._render_session(session)
            if getattr(self, 'session_dialog', None) is not None:
                self.session_dialog.accept()
            self._buttons()
            return
        if kind == 'session_delete':
            if data.get('sessions') is not None:
                self._show_sessions(data)
            self._buttons()
            return
        if kind in ('schedules', 'schedule_create', 'schedule_run',
                    'schedule_toggle', 'schedule_delete'):
            self._show_schedules(data)
            self._buttons()
            return
        if kind == 'skill_preview':
            skill = data.get('skill') or {}
            perms = '、'.join(skill.get('permissions') or []) or '无权限声明'
            text = (f"名称：{skill.get('name')}\n标识：{skill.get('id')}\n"
                    f"触发词：{'、'.join(skill.get('triggers') or []) or '无'}\n"
                    f"权限声明：{perms}\n\n{str(skill.get('instructions') or '')[:800]}")
            if QMessageBox.question(self, '导入技能包', text + '\n\n确认导入？') != QMessageBox.StandardButton.Yes:
                self._pending_skill_package = None
                self._buttons()
                return
            package, source = self._pending_skill_package or (None, '')
            self._pending_skill_package = None
            if package is not None:
                self._request('skill', 'assistant/skills/import',
                              {'package': package, 'source': source})
            return
        if kind == 'learning_skills':
            self._open_skills_after = False
            self._show_skills(data)
            self._buttons()
            return
        if kind == 'skill_toggle':
            self._show_skills(data)
            self.notice.setText('技能状态已更新。')
            self._buttons()
            return
        if kind == 'skill_delete':
            self._show_skills(data)
            self.notice.setText('技能已删除。')
            self._buttons()
            return
        if kind == 'history':
            self._show_history(data)
            self.notice.setText('操作历史已刷新。')
            self._buttons()
            return
        if kind in ('learning', 'forget', 'skill', 'maintain'):
            if kind == 'skill':
                skill = data.get('skill') or {}
                self.notice.setText('已导入技能包：' + str(skill.get('name') or skill.get('id') or ''))
            elif kind == 'forget':
                self.notice.setText('已删除选中的学习记录。' if data.get('removed')
                                    else '这条记录已经不存在。')
            elif kind == 'maintain':
                removed = data.get('removed') or {}
                self.notice.setText(
                    f"已整理：清理反复失败 {removed.get('removed_failed', 0)} 条、"
                    f"长期未用 {removed.get('removed_stale', 0)} 条。")
            else:
                self.notice.setText('学习记录已刷新。')
            if getattr(self, '_open_skills_after', False):
                self._open_skills_after = False
                self._show_skills(data)
            else:
                self._show_learning(data)
                if getattr(self, 'skills_dialog', None) is not None:
                    self._show_skills(data)
            self._buttons()
            return
        if kind == 'feedback':
            stats = data.get('stats') or {}
            self.notice.setText(f"反馈已记录：成功 {stats.get('success', 0)} / 失败 {stats.get('failure', 0)}")
            self._buttons()
            return
        self.present(data)

    def present(self, data):
        self.job = data
        state = data.get('state')
        signature = (data.get('id'), state)
        self.notice.setText(str(data.get('message') or ''))
        if state in ('planning', 'applying', 'undoing'):
            self.timer.start()
        else:
            self.timer.stop()
        if signature != self._last_state and state not in ('planning', 'applying', 'undoing'):
            self._append('助手：' + str(data.get('message') or ''))
            if state == 'confirm':
                self._append('（要碰你的电脑，需要你点一次「允许这次操作」）')
        if state == 'applied' and data.get('id') not in self._reported:
            self._reported.add(data['id'])
            self.applied.emit(data.get('result', {}))
            if data.get('result', {}).get('page'):
                self.navigate.emit(data['result']['page'])
        self._last_state = signature
        self._buttons()

    def shutdown(self):
        self.timer.stop()
        self.queued_request = None
        if self.pending:
            reply = self.pending; self.pending = None; reply.abort(); reply.deleteLater()
