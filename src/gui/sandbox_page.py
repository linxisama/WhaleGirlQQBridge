"""Windows 沙盒页：功能状态、隔离通道与启动入口（全部请求走后台线程）。

端点前缀与 qq_gui 的 api_get/api_post 一致（相对 /api/）：
  GET  sandbox/status
  POST sandbox/enable       {confirmed, apply}
  POST sandbox/disable      {confirmed, apply}
  GET  sandbox/channel?name=...
  POST sandbox/channel      {name, host_folder, networking, clipboard,
                             read_only, memory_mb, launch, confirmed}
  POST sandbox/launch       {name, confirmed}

页面只负责收集参数与展示结果；真正的 Windows 沙盒检测/启用由后台完成。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QCheckBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QScrollArea, QSpinBox,
                             QVBoxLayout, QWidget)

from .panel_requests import PanelRequests

STATUS_LABELS = {
    '': '未检测',
    'unknown': '未检测',
    'not_checked': '未检测',
    'available': '可用',
    'supported': '可用',
    'ok': '可用',
    'enabled': '已启用',
    'running': '已启用',
    'active': '已启用',
    'disabled': '未启用',
    'not_enabled': '未启用',
    'not_installed': '未启用',
    'restart_required': '需要重启',
    'needs_restart': '需要重启',
    'reboot_required': '需要重启',
    'unsupported': '不支持',
    'not_supported': '不支持',
    'failed': '失败',
    'error': '失败',
}
_STATE_ORDER = ('未检测', '可用', '已启用', '未启用', '需要重启', '不支持', '失败')


def status_text(payload) -> str:
    """把 /sandbox/status 的返回折成七个中文状态之一。"""
    if not isinstance(payload, dict) or not payload:
        return '未检测'
    if payload.get('success') is False:
        return '失败'
    raw = str(payload.get('status') or '').strip()
    if raw in _STATE_ORDER:
        return raw
    label = STATUS_LABELS.get(raw.lower())
    if label:
        return label
    if payload.get('supported') is False:
        return '不支持'
    if payload.get('ready') is True:
        return '已启用'
    if payload.get('supported') is True:
        return '未启用'
    return '未检测'


def _first_text(mapping, keys) -> str:
    if not isinstance(mapping, dict):
        return ''
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


def windows_text(detection) -> str:
    """Windows 版本/edition/build 的一行中文摘要。"""
    if not isinstance(detection, dict) or not detection:
        return 'Windows 版本：未检测到'
    version = _first_text(detection, ('windows_version', 'version', 'caption',
                                      'product_name', 'os_version'))
    edition = _first_text(detection, ('edition', 'edition_id', 'product_type'))
    build = _first_text(detection, ('build', 'build_number', 'current_build'))
    parts = []
    if version:
        parts.append(version if version.lower().startswith('windows') else 'Windows ' + version)
    if edition:
        parts.append(edition)
    if build:
        parts.append('Build ' + build)
    return ' · '.join(parts) if parts else 'Windows 版本：未检测到'


def alternative_text(payload) -> str:
    value = (payload or {}).get('alternative') if isinstance(payload, dict) else ''
    if isinstance(value, dict):
        value = value.get('label') or value.get('text') or value.get('message') or ''
    text = str(value or '').strip()
    return text or '—'


def channel_ready_text(channel) -> str:
    if not isinstance(channel, dict) or not channel:
        return '就绪标记：未检测'
    if channel.get('ready'):
        return '就绪标记：已就绪'
    if channel.get('exists') is False:
        return '就绪标记：未创建（未就绪）'
    return '就绪标记：未就绪'


class SandboxPage(QWidget):
    """Windows 沙盒控制页。所有网络调用都交给 PanelRequests 的工作线程。"""

    def __init__(self, api_get=None, api_post=None, parent=None):
        super().__init__(parent)
        self.api_get = api_get or (lambda path, timeout=10: {})
        self.api_post = api_post or (lambda path, data=None, timeout=20: {})
        self.get = self.api_get
        self.post = self.api_post
        self._status: dict = {}
        self._channel: dict = {}
        self._busy = False
        self._loaded = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { border: none; background: transparent; }')
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        title = QLabel('Windows 沙盒')
        title.setStyleSheet('font-size: 21px; font-weight: bold;')
        layout.addWidget(title)
        subtitle = QLabel('检测并启用系统自带的 Windows 沙盒，为机器人提供隔离运行环境。'
                          '所有操作只在本机执行，不会上传任何内容。')
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addWidget(self._build_status_card())
        layout.addWidget(self._build_channel_card())
        self.notice = QLabel('')
        self.notice.setObjectName('sandbox_notice')
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        self.requests = PanelRequests(self)
        self._sync_actions()

    # ── 界面 ────────────────────────────────────────────────────────────────
    def _build_status_card(self) -> QFrame:
        frame = QFrame()
        frame.setProperty('panel', True)
        frame.setObjectName('sandbox_status_card')
        box = QVBoxLayout(frame)
        box.setContentsMargins(18, 16, 18, 18)
        box.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(QLabel('功能状态'))
        self.status_value = QLabel('未检测')
        self.status_value.setObjectName('sandbox_status_value')
        self.status_value.setStyleSheet('font-weight: bold; font-size: 15px;')
        heading.addWidget(self.status_value)
        heading.addStretch(1)
        self.btn_refresh = QPushButton('刷新')
        self.btn_refresh.setObjectName('sandbox_refresh')
        self.btn_refresh.clicked.connect(self.refresh)
        heading.addWidget(self.btn_refresh)
        box.addLayout(heading)

        self.status_detail = QLabel('点「刷新」检测当前系统的 Windows 沙盒。')
        self.status_detail.setObjectName('sandbox_status_detail')
        self.status_detail.setWordWrap(True)
        box.addWidget(self.status_detail)
        self.windows_info = QLabel('Windows 版本：未检测到')
        self.windows_info.setObjectName('sandbox_windows_info')
        self.windows_info.setWordWrap(True)
        box.addWidget(self.windows_info)
        self.alternative = QLabel('替代方案：—')
        self.alternative.setObjectName('sandbox_alternative')
        self.alternative.setWordWrap(True)
        box.addWidget(self.alternative)

        actions = QHBoxLayout()
        self.btn_enable = QPushButton('启用 Windows 沙盒')
        self.btn_enable.setObjectName('sandbox_enable')
        self.btn_enable.setToolTip('会修改 Windows 可选功能并可能需要重启')
        self.btn_enable.clicked.connect(self.enable_sandbox)
        actions.addWidget(self.btn_enable)
        self.btn_disable = QPushButton('禁用 / 回滚')
        self.btn_disable.setObjectName('sandbox_disable')
        self.btn_disable.setToolTip('关闭 Windows 沙盒功能；系统可能要求重启')
        self.btn_disable.clicked.connect(self.disable_sandbox)
        actions.addWidget(self.btn_disable)
        actions.addStretch(1)
        box.addLayout(actions)
        return frame

    def _build_channel_card(self) -> QFrame:
        frame = QFrame()
        frame.setProperty('panel', True)
        frame.setObjectName('sandbox_channel_card')
        box = QVBoxLayout(frame)
        box.setContentsMargins(18, 16, 18, 18)
        box.setSpacing(8)
        title = QLabel('隔离通道')
        title.setStyleSheet('font-weight: bold; font-size: 15px;')
        box.addWidget(title)
        hint = QLabel('通道名默认 default；宿主目录留空时由后台使用默认目录。'
                      'Networking / 剪贴板默认关闭，只有明确需要时才勾选。')
        hint.setWordWrap(True)
        box.addWidget(hint)

        form = QFormLayout()
        self.channel_name = QLineEdit('default')
        self.channel_name.setObjectName('sandbox_channel_name')
        self.channel_name.setClearButtonEnabled(True)
        form.addRow('通道名', self.channel_name)
        self.channel_host = QLineEdit('')
        self.channel_host.setObjectName('sandbox_channel_host_folder')
        self.channel_host.setPlaceholderText('宿主目录（留空由后端使用默认目录）')
        self.channel_host.setClearButtonEnabled(True)
        form.addRow('宿主目录', self.channel_host)
        box.addLayout(form)

        options = QHBoxLayout()
        self.channel_networking = QCheckBox('Networking')
        self.channel_networking.setObjectName('sandbox_channel_networking')
        self.channel_networking.setChecked(False)
        options.addWidget(self.channel_networking)
        self.channel_clipboard = QCheckBox('剪贴板')
        self.channel_clipboard.setObjectName('sandbox_channel_clipboard')
        self.channel_clipboard.setChecked(False)
        options.addWidget(self.channel_clipboard)
        self.channel_read_only = QCheckBox('只读')
        self.channel_read_only.setObjectName('sandbox_channel_read_only')
        options.addWidget(self.channel_read_only)
        options.addWidget(QLabel('内存 MB'))
        self.channel_memory = QSpinBox()
        self.channel_memory.setObjectName('sandbox_channel_memory_mb')
        self.channel_memory.setRange(0, 262144)
        self.channel_memory.setValue(0)
        self.channel_memory.setSpecialValueText('0（默认）')
        self.channel_memory.setSuffix(' MB')
        options.addWidget(self.channel_memory)
        options.addStretch(1)
        box.addLayout(options)

        self.channel_wsb = QLabel('WSB 路径：—')
        self.channel_wsb.setObjectName('sandbox_channel_wsb_path')
        self.channel_wsb.setWordWrap(True)
        box.addWidget(self.channel_wsb)
        self.channel_host_value = QLabel('宿主目录：—')
        self.channel_host_value.setObjectName('sandbox_channel_host_path')
        self.channel_host_value.setWordWrap(True)
        box.addWidget(self.channel_host_value)
        self.channel_ready = QLabel('就绪标记：未检测')
        self.channel_ready.setObjectName('sandbox_channel_ready')
        self.channel_ready.setWordWrap(True)
        box.addWidget(self.channel_ready)

        actions = QHBoxLayout()
        self.btn_channel_create = QPushButton('创建 / 更新通道')
        self.btn_channel_create.setObjectName('sandbox_channel_create')
        self.btn_channel_create.clicked.connect(self.create_channel)
        actions.addWidget(self.btn_channel_create)
        self.btn_channel_launch = QPushButton('创建并启动沙盒')
        self.btn_channel_launch.setObjectName('sandbox_channel_launch')
        self.btn_channel_launch.clicked.connect(self.create_and_launch)
        actions.addWidget(self.btn_channel_launch)
        actions.addStretch(1)
        self.btn_open_host = QPushButton('打开宿主目录')
        self.btn_open_host.setObjectName('sandbox_open_host_folder')
        self.btn_open_host.clicked.connect(self.open_host_folder)
        actions.addWidget(self.btn_open_host)
        self.btn_open_wsb = QPushButton('打开 .wsb 所在目录')
        self.btn_open_wsb.setObjectName('sandbox_open_wsb_folder')
        self.btn_open_wsb.clicked.connect(self.open_wsb_folder)
        actions.addWidget(self.btn_open_wsb)
        box.addLayout(actions)
        return frame

    # ── 请求 ────────────────────────────────────────────────────────────────
    def _sync_actions(self) -> None:
        busy = self._busy or self.requests.busy
        for button in (self.btn_refresh, self.btn_channel_create,
                       self.btn_channel_launch, self.btn_open_host,
                       self.btn_open_wsb):
            button.setEnabled(not busy)
        if busy:
            self.btn_enable.setEnabled(False)
            self.btn_disable.setEnabled(False)
            return
        state = status_text(self._status)
        supported = self._status.get('supported')
        self.btn_enable.setEnabled(supported is not False and
                                   state not in ('已启用', '需要重启', '不支持'))
        self.btn_disable.setEnabled(supported is not False and
                                    state in ('已启用', '需要重启', '可用'))
        if supported is False:
            self.btn_enable.setToolTip('当前系统不支持 Windows 沙盒，无法启用')
        elif state in ('已启用', '需要重启'):
            self.btn_enable.setToolTip('Windows 沙盒已经启用')
        else:
            self.btn_enable.setToolTip('会修改 Windows 可选功能并可能需要重启')

    def _start_request(self, work, apply, pending: str) -> bool:
        if self._busy or self.requests.busy:
            self.set_notice('正在处理上一个操作，请稍候…')
            return False
        self._busy = True
        self._sync_actions()
        self.set_notice(pending)

        def wrapped(result):
            self._busy = False
            try:
                apply(result)
            except Exception as exc:       # noqa: BLE001 - 界面动作不许抛异常
                self._fail(exc)
            finally:
                self._sync_actions()

        started = self.requests.start(work, wrapped, self._on_request_error)
        if not started:
            self._busy = False
            self._sync_actions()
            self.set_notice('请求没有开始，请稍后重试。')
        return bool(started)

    def _on_request_error(self, exc) -> None:
        self._busy = False
        self._sync_actions()
        self.set_notice('操作失败：%s' % (exc or '后台没有响应'))

    def _fail(self, exc) -> None:
        self.set_notice('操作失败：%s' % (exc or '未知原因'))

    def set_notice(self, text: str) -> None:
        self.notice.setText(str(text or ''))
        self.notice.setTextFormat(Qt.TextFormat.PlainText)

    @property
    def busy(self) -> bool:
        return bool(self._busy or self.requests.busy)

    def refresh(self) -> bool:
        self._loaded = True
        return self._start_request(
            lambda: self.api_get('sandbox/status', timeout=20),
            self.apply_status, '正在检测 Windows 沙盒…')

    def apply_status(self, payload) -> None:
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data or data.get('success') is False:
            self._status = {}
            self.status_value.setText(status_text(data))
            self.status_detail.setText(str(data.get('message') or data.get('detail')
                                          or '后台没有返回沙盒状态。'))
            self.windows_info.setText('Windows 版本：未检测到')
            self.alternative.setText('替代方案：—')
            self.set_notice('读取沙盒状态失败：%s'
                            % (data.get('message') or data.get('detail') or '未知原因'))
            self._sync_actions()
            return
        self._status = data
        state = status_text(data)
        self.status_value.setText(state)
        detail = str(data.get('detail') or data.get('message') or '').strip()
        if not detail:
            detail = {'已启用': 'Windows 沙盒已启用，可以创建隔离通道。',
                      '可用': '当前系统可以使用 Windows 沙盒。',
                      '未启用': 'Windows 沙盒可用但尚未启用。',
                      '需要重启': '功能已变更，需要重启 Windows 才能生效。',
                      '不支持': '当前 Windows 版本/版本类型不支持 Windows 沙盒。',
                      '失败': '检测过程中出现错误，请查看下方提示。'}.get(state, '尚未检测。')
        self.status_detail.setText(detail)
        self.windows_info.setText(windows_text(data.get('detection')))
        self.alternative.setText('替代方案：' + alternative_text(data))
        self.set_notice('状态已更新：%s' % state)
        self._sync_actions()
        QTimer.singleShot(0, self.load_channel)

    # ── 隔离通道 ────────────────────────────────────────────────────────────
    def channel_name_value(self) -> str:
        return str(self.channel_name.text() or '').strip() or 'default'

    def channel_payload(self, launch: bool = False) -> dict:
        return {
            'name': self.channel_name_value(),
            'host_folder': str(self.channel_host.text() or '').strip(),
            'networking': bool(self.channel_networking.isChecked()),
            'clipboard': bool(self.channel_clipboard.isChecked()),
            'read_only': bool(self.channel_read_only.isChecked()),
            'memory_mb': int(self.channel_memory.value()),
            'launch': bool(launch),
            'confirmed': True,
        }

    def load_channel(self) -> bool:
        name = self.channel_name_value()
        path = 'sandbox/channel?name=' + quote(name, safe='')
        return self._start_request(lambda: self.api_get(path, timeout=20),
                                   self.apply_channel, '正在读取隔离通道…')

    def apply_channel(self, payload) -> None:
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data or data.get('success') is False:
            self._channel = {}
            self.channel_wsb.setText('WSB 路径：—')
            self.channel_host_value.setText('宿主目录：—')
            self.channel_ready.setText('就绪标记：未检测')
            self.set_notice('读取隔离通道失败：%s'
                            % (data.get('message') or data.get('detail') or '未知原因'))
            return
        channel = data.get('channel') if isinstance(data.get('channel'), dict) else {}
        self._channel = channel
        self.channel_wsb.setText('WSB 路径：%s' % (channel.get('wsb_path') or '—'))
        self.channel_host_value.setText('宿主目录：%s'
                                        % (channel.get('host_folder') or '—'))
        self.channel_ready.setText(channel_ready_text(channel))
        if channel.get('error'):
            self.set_notice('通道读取完成，但有错误：%s' % channel.get('error'))
        else:
            self.set_notice('隔离通道 %s：%s'
                            % (channel.get('name') or self.channel_name_value(),
                               '已就绪' if channel.get('ready') else '未就绪'))

    def create_channel(self) -> bool:
        return self._create_channel(launch=False, pending='正在创建/更新隔离通道…')

    def create_and_launch(self) -> bool:
        return self._create_channel(launch=True, pending='正在创建通道并启动沙盒…')

    def _create_channel(self, launch: bool, pending: str) -> bool:
        body = self.channel_payload(launch=launch)
        return self._start_request(
            lambda: self.api_post('sandbox/channel', body, timeout=120),
            lambda result: self._channel_done(result, launch), pending)

    def _channel_done(self, result, launch: bool) -> None:
        data = dict(result) if isinstance(result, dict) else {}
        if not data or data.get('success') is False:
            self.set_notice('创建/更新通道失败：%s'
                            % (data.get('message') or data.get('detail') or '未知原因'))
            return
        self.apply_channel(data)
        if launch:
            launch_data = data.get('launch') if isinstance(data.get('launch'), dict) else {}
            text = launch_data.get('message') or data.get('message') or '已请求启动沙盒'
            self.set_notice('创建并启动完成：%s' % text)
        else:
            self.set_notice('通道已创建/更新：%s'
                            % (data.get('message') or '可以在“打开 .wsb 所在目录”里查看'))

    def open_host_folder(self) -> bool:
        path = (self._channel.get('host_folder') if isinstance(self._channel, dict) else '')
        path = path or str(self.channel_host.text() or '').strip()
        return self._open_path(path, '宿主目录')

    def open_wsb_folder(self) -> bool:
        wsb = (self._channel.get('wsb_path') if isinstance(self._channel, dict) else '')
        if not wsb:
            self.set_notice('还没有 .wsb 路径；请先创建通道。')
            return False
        return self._open_path(str(Path(str(wsb)).parent), '.wsb 所在目录')

    def _open_path(self, path: str, label: str) -> bool:
        if not path:
            self.set_notice('没有可打开的%s。' % label)
            return False
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:           # noqa: BLE001
            self.set_notice('打开%s失败：%s' % (label, exc))
            return False
        self.set_notice('已打开%s：%s' % (label, path))
        return True

    # ── 启用 / 禁用 ─────────────────────────────────────────────────────────
    def _confirm(self, title: str, text: str) -> bool:
        from PyQt6.QtWidgets import QMessageBox
        answer = QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def enable_sandbox(self, confirmed: bool = False) -> bool:
        if self._status.get('supported') is False:
            self.set_notice('当前系统不支持 Windows 沙盒，无法启用；请查看替代方案。')
            return False
        if not confirmed and not self._confirm(
                '启用 Windows 沙盒',
                '这会启用 Windows 可选功能，可能需要重启。现在继续吗？'):
            return False
        return self._start_request(
            lambda: self.api_post('sandbox/enable',
                                  {'confirmed': True, 'apply': True}, timeout=180),
            self._enable_done, '正在启用 Windows 沙盒（可能需要一点时间）…')

    def _enable_done(self, result) -> None:
        data = dict(result) if isinstance(result, dict) else {}
        if not data or data.get('success') is False:
            self.set_notice('启用失败：%s'
                            % (data.get('message') or data.get('detail') or '未知原因'))
            return
        self._status = dict(data)
        state = status_text(data)
        self.status_value.setText(state)
        self.status_detail.setText(str(data.get('detail') or data.get('message')
                                      or '启用操作已完成。'))
        self.set_notice('启用完成：%s' % (data.get('message') or state))
        self._sync_actions()
        QTimer.singleShot(0, self.load_channel)

    def disable_sandbox(self, confirmed: bool = False) -> bool:
        if not confirmed and not self._confirm(
                '禁用 / 回滚 Windows 沙盒',
                '这会关闭 Windows 沙盒功能，可能需要重启。现在继续吗？'):
            return False
        return self._start_request(
            lambda: self.api_post('sandbox/disable',
                                  {'confirmed': True, 'apply': True}, timeout=180),
            self._disable_done, '正在禁用 / 回滚 Windows 沙盒…')

    def _disable_done(self, result) -> None:
        data = dict(result) if isinstance(result, dict) else {}
        if not data or data.get('success') is False:
            self.set_notice('禁用失败：%s'
                            % (data.get('message') or data.get('detail') or '未知原因'))
            return
        self._status = dict(data)
        state = status_text(data)
        self.status_value.setText(state)
        self.status_detail.setText(str(data.get('detail') or data.get('message')
                                      or '禁用操作已完成。'))
        self.set_notice('禁用完成：%s' % (data.get('message') or state))
        self._sync_actions()

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded:
            QTimer.singleShot(0, self.refresh)

    def close(self) -> None:
        if hasattr(self, 'requests'):
            self.requests.close()


__all__ = ['SandboxPage', 'STATUS_LABELS', 'status_text', 'windows_text',
           'alternative_text', 'channel_ready_text']
