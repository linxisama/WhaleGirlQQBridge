"""T17：原生“本地电脑操作”审批页（设备状态 + 动作预览 + 本地同意 + 审计摘要 + 急停）。

设计（1.7 / T17 验收）：
- 配对设备、L0/L1 能力、待批动作在这里**本地**确认；从不自动点“允许”。
- 截图/文件回传不因“允许操作”自动放开（需独立允许范围）。
- 急停按钮：一键清空未执行授权，下一动作前生效。
- 结果只回原绑定私聊；取消/解绑立即使后续动作失效。

PyQt 未安装时仍可 import（暴露可测的状态助手）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                                 QPushButton, QTableWidget, QTableWidgetItem,
                                 QAbstractItemView, QHeaderView)
    _HAS_QT = True
    QWidget = QWidget
except Exception:
    _HAS_QT = False
    QWidget = object


@dataclass
class PendingAction:
    """一条待本地批准的动作（脱敏展示，不显示参数全文/密钥）。"""
    job_id: str
    device_id: str
    action: str
    args_summary: str
    expires_at: float = 0.0
    policy_version: int = 1

    def needs_reapproval(self, new_args_summary: str) -> bool:
        """参数变化 → 必须重新审批（1.7.2 / T17：改参重审）。"""
        return new_args_summary != self.args_summary


class LocalAgentPanel(QWidget):
    """原生审批页：动作预览/本地同意/设备状态/审计摘要/急停。"""

    def __init__(self, get: Optional[Callable] = None,
                 post: Optional[Callable] = None,
                 root: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self.get = get
        self.post = post
        self.root = Path(root) if root else Path.cwd()
        self._pending: list[PendingAction] = []
        if _HAS_QT:
            self.build()
            from .panel_requests import PanelRequests
            self.requests = PanelRequests(self)
            self.stop_requests = PanelRequests(self)

    def build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        self.status = QLabel('设备状态：未读取')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        hint = QLabel('这里用于查看和审批已配对设备的动作；设备配对向导尚未接入。'
                      '群聊/引用/转发不能授权电脑操作。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        self.btn_refresh = QPushButton('刷新待批')
        self.btn_refresh.clicked.connect(self.request_refresh)
        buttons.addWidget(self.btn_refresh)
        self.btn_approve = QPushButton('允许所选')
        self.btn_approve.clicked.connect(self._approve_selected)
        buttons.addWidget(self.btn_approve)
        self.btn_reject = QPushButton('拒绝所选')
        self.btn_reject.clicked.connect(self._reject_selected)
        buttons.addWidget(self.btn_reject)
        self.btn_stop = QPushButton('急停')
        self.btn_stop.clicked.connect(self._emergency_clicked)
        buttons.addWidget(self.btn_stop)
        self.btn_resume = QPushButton('解除急停')
        self.btn_resume.clicked.connect(self._resume_clicked)
        buttons.addWidget(self.btn_resume)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.table = QTableWidget(0, 4)
        self.table.setStyleSheet('QTableWidget { background: white; color: #292723; } '
            'QHeaderView::section { background: #efede7; color: #57534b; padding: 6px; }')
        self.table.setHorizontalHeaderLabels(['动作', '设备', '参数摘要', '策略版本'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.audit = QLabel('审计：未读取')
        self.audit.setWordWrap(True)
        layout.addWidget(self.audit)
        self.feedback = QLabel('')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)

    # ── 数据 ────────────────────────────────────────────────────────────────
    def pending_actions(self) -> list[PendingAction]:
        if self.get is None:
            return self._pending
        try:
            payload = self.get('/api/v2/agent/pending') or {}
        except Exception:
            self.feedback.setText('读取待批动作失败；请确认后台已启动。')
            return self._pending
        return self._apply_pending(payload)

    def _apply_pending(self, payload):
        selected = self._selected()
        items = payload.get('pending') if isinstance(payload, dict) else None
        self._pending = [self._pending_from(item) for item in (items or [])
                         if isinstance(item, dict)]
        if _HAS_QT:
            self.table.setRowCount(len(self._pending))
            for row, pending in enumerate(self._pending):
                values = [pending.action, pending.device_id, pending.args_summary,
                          str(pending.policy_version)]
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QTableWidgetItem(str(value)))
                if selected and selected.job_id == pending.job_id:
                    self.table.selectRow(row)
        return self._pending

    def request_refresh(self, *, preserve_feedback=False):
        if self.get is None:
            return
        def work():
            return (self.get('/api/v2/agent/pending'), self.get('/api/v2/agent/status'),
                    self.get('/api/v2/agent/audit?limit=10'))
        def apply(result):
            self._apply_pending(result[0])
            self.status.setText(self._format_status(result[1]))
            self.audit.setText(self._format_audit(result[2], 10))
            self.btn_approve.setEnabled(bool(self._pending) and not result[1].get('stopped'))
            self.btn_resume.setEnabled(bool(result[1].get('stopped')))
            if not preserve_feedback:
                self.feedback.setText(f'待批 {len(self._pending)} 条')
        self.requests.start(work, apply,
                            lambda exc: self.feedback.setText('读取失败：' + str(exc)[:200]))

    @staticmethod
    def _pending_from(item: dict) -> PendingAction:
        return PendingAction(
            job_id=str(item.get('job_id', '')),
            device_id=str(item.get('device_id', '')),
            action=str(item.get('action', '')),
            args_summary=str(item.get('arg_summary', '')),
            expires_at=float(item.get('expires_at') or 0.0),
            policy_version=int(item.get('policy_version') or 1))

    def refresh(self) -> list[PendingAction]:
        self.pending_actions()
        status = self._status_text()
        self.status.setText(status)
        self.audit.setText(self.audit_summary())
        self.feedback.setText(f'待批 {len(self._pending)} 条'
                              if self._pending else '没有待批动作')
        return self._pending

    def _status_text(self) -> str:
        if self.get is None:
            return '设备状态：未读取'
        try:
            payload = self.get('/api/v2/agent/status') or {}
        except Exception:
            return '设备状态：读取失败'
        return self._format_status(payload)

    @staticmethod
    def _format_status(payload):
        devices = [item for item in (payload.get('devices') or []) if isinstance(item, dict)]
        stopped = bool(payload.get('stopped'))
        if not devices:
            return '设备状态：尚未配对；当前不能控制电脑，配对向导仍待接入。'
        names = ', '.join(f"{item.get('device_id')}({'已配对' if item.get('paired') else '已解绑'})"
                          for item in devices)
        capabilities = ', '.join(map(str, payload.get('capabilities') or [])) or '无已授权能力'
        return f"设备状态：{names}；急停：{'是' if stopped else '否'}；能力：{capabilities}"

    def audit_summary(self, limit: int = 10) -> str:
        if self.get is None:
            return '审计：未读取'
        try:
            payload = self.get('/api/v2/agent/audit?limit=%d' % int(limit)) or {}
        except Exception:
            return '审计：读取失败'
        return self._format_audit(payload, limit)

    @staticmethod
    def _format_audit(payload, limit):
        events = payload.get('events') or []
        if not events:
            return '审计：暂无记录'
        lines = [f"{item.get('event', '')}" for item in events[:limit] if isinstance(item, dict)]
        return '审计（最近）：' + '；'.join(lines)

    # ── 批准/拒绝 ───────────────────────────────────────────────────────────
    def approve(self, pending: PendingAction) -> None:
        if self.post is None or not pending.job_id:
            return
        try:
            result = self.post('/api/v2/agent/approve', {'job_id': pending.job_id}) or {}
            state = result.get('state', 'unknown')
            self.feedback.setText(f'已批准并执行：{pending.action} → {state}')
        except Exception:
            self.feedback.setText('批准失败；参数变化后需要重新拉取再审批。')
        self.refresh()

    def _approve_selected(self) -> None:
        pending = self._selected()
        if pending is not None:
            self._request_action('/api/v2/agent/approve', {'job_id': pending.job_id}, '动作结果')

    def reject(self, pending: PendingAction) -> None:
        if self.post is None or not pending.job_id:
            return
        try:
            self.post('/api/v2/agent/reject', {'job_id': pending.job_id})
            self.feedback.setText(f'已拒绝：{pending.action}')
        except Exception:
            self.feedback.setText('拒绝失败。')
        self.refresh()

    def _reject_selected(self) -> None:
        pending = self._selected()
        if pending is not None:
            self._request_action('/api/v2/agent/reject', {'job_id': pending.job_id}, '拒绝结果')

    def _request_action(self, path, data, label, *, urgent=False):
        if self.post is None:
            return
        def apply(result):
            state = result.get('state') or ('已急停' if result.get('stopped') else '已解除急停')
            self.feedback.setText(f'{label}：{state}')
            self.request_refresh(preserve_feedback=True)
        runner = self.stop_requests if urgent else self.requests
        if not runner.start(lambda: self.post(path, data), apply,
                            lambda exc: self.feedback.setText(str(exc)[:240])):
            self.feedback.setText('当前请求仍在处理，请稍候。')

    def _resume_clicked(self):
        self._request_action('/api/v2/agent/resume', {}, '恢复结果', urgent=True)

    def _emergency_clicked(self):
        self._request_action('/api/v2/agent/emergency-stop', {}, '急停结果', urgent=True)

    @property
    def busy(self):
        return _HAS_QT and (self.requests.busy or self.stop_requests.busy)

    def _selected(self) -> Optional[PendingAction]:
        if not _HAS_QT or self.table.currentRow() < 0:
            return None
        index = self.table.currentRow()
        return self._pending[index] if 0 <= index < len(self._pending) else None

    # ── 急停 ────────────────────────────────────────────────────────────────
    def emergency_stop(self) -> None:
        if self.post is None:
            return
        try:
            result = self.post('/api/v2/agent/emergency-stop', {}) or {}
            self.feedback.setText(f"已急停；清空 {result.get('cleared_approvals', 0)} 条未执行授权。")
        except Exception:
            self.feedback.setText('急停请求失败；请直接关闭本地 Agent 进程。')
        self.refresh()

    def cleanup(self) -> None:
        if _HAS_QT and (self.requests.busy or self.stop_requests.busy):
            return False
        if _HAS_QT:
            self.requests.close()
            self.stop_requests.close()
        self._pending.clear()
        return True

    def closeEvent(self, event):  # noqa: N802
        if not self.cleanup():
            event.ignore()
            return
        super().closeEvent(event)
