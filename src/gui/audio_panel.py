"""T14：原生“音频”配置页（音色/路由 + 上传按钮 + 试听 + 排队/失败/取消 + 预算/数据去向）。

与现有原生控制台同栈（PyQt6）。上传按钮由 audio_assets_config 插件的
FieldSchema.file_picker 驱动（原生配置窗自动生成），这里只做“试听/任务/预算”
这类需要实时交互的增强 UI。

原则（T14 验收 / 3.2）：
- file/files/labeled_files/directory 各按用途用；素材复制到运行数据目录后删除原文件仍可用。
- 关闭窗口时任务与工作线程可安全回收（不遗留孤儿）。
- 取消配置不改设置；旧配置/人设/API/登录状态保留。
- 试听走“本机受控试听或检查受管理文件”，不假设无鉴权公网下载。

PyQt 未安装时本模块仍可 import（只暴露可测的数据助手），供 CI 结构检查。
"""
from __future__ import annotations

import os
import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QDesktopServices
    from PyQt6.QtCore import QUrl
    from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                                 QPushButton, QTableWidget, QTableWidgetItem,
                                 QAbstractItemView, QHeaderView, QComboBox,
                                 QLineEdit, QFileDialog, QScrollArea)
    _HAS_QT = True
except Exception:   # 无 PyQt 环境（如纯单测）：降级为结构占位
    _HAS_QT = False
    QWidget = object


@dataclass
class TaskRow:
    """一行的任务状态（渲染无关，便于单测）。"""
    job_id: str
    capability: str
    state: str
    provider: str = ''
    cost_note: str = ''
    can_cancel: bool = True


STATE_LABELS = {
    'queued': '排队中', 'preparing': '准备中', 'running': '生成中',
    'postprocessing': '后处理', 'ready': '已完成', 'failed': '失败',
    'cancel_requested': '取消中', 'cancelled': '已取消',
    'expired': '已过期', 'unknown': '结果待核实',
}


class AudioPanel(QWidget):
    """原生音频页：试听/排队/取消/预算显示 + 线程安全回收。"""

    def __init__(self, get: Optional[Callable] = None,
                 post: Optional[Callable] = None,
                 root: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self.get = get
        self.post = post
        self.root = Path(root) if root else Path.cwd()
        self._workers: list = []
        self._rows: list[TaskRow] = []
        self._budget_text = ''
        self._voices: list[dict] = []
        if _HAS_QT:
            self.build()
            from .panel_requests import PanelRequests
            from PyQt6.QtCore import QTimer
            self.requests = PanelRequests(self)
            self.task_timer = QTimer(self)
            self.task_timer.setInterval(2000)
            self.task_timer.timeout.connect(self._poll_visible)

    # ── UI ──────────────────────────────────────────────────────────────────
    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        hint = QLabel('先配置本地引擎，再生成一句试听。歌曲合成尚未接入此页；'
                      '语音生成完成不代表已经发送到 QQ。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        from .audio_setup import AudioSetup
        self.setup = AudioSetup(self.get, self.post, self)
        layout.addWidget(self.setup)
        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel('音色'))
        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(160)
        self.voice_combo.setToolTip('导入的参考音频/标签表会出现在这里')
        voice_row.addWidget(self.voice_combo)
        self.btn_import_files = QPushButton('导入音色…')
        self.btn_import_files.setToolTip('选择一个或多个参考音频（wav/flac/mp3/m4a）')
        self.btn_import_files.clicked.connect(self._pick_voice_files)
        voice_row.addWidget(self.btn_import_files)
        self.btn_import_table = QPushButton('导入标签表…')
        self.btn_import_table.setToolTip('CSV/TSV/JSON：id,name,engine,voice,ref_audio,...')
        self.btn_import_table.clicked.connect(self._pick_voice_table)
        voice_row.addWidget(self.btn_import_table)
        self.btn_refresh_voices = QPushButton('刷新音色')
        self.btn_refresh_voices.clicked.connect(self.request_refresh)
        voice_row.addWidget(self.btn_refresh_voices)
        self.btn_default_voice = QPushButton('设为默认')
        self.btn_default_voice.setToolTip('自动语音将使用所选音色')
        self.btn_default_voice.clicked.connect(self._set_default_voice)
        voice_row.addWidget(self.btn_default_voice)
        voice_row.addStretch(1)
        layout.addLayout(voice_row)
        sample_row = QHBoxLayout()
        self.sample_text = QLineEdit()
        self.sample_text.setPlaceholderText('输入一句试听文本（1–300 字）')
        self.sample_text.setText('你好，我是鲸鱼娘。')
        sample_row.addWidget(self.sample_text, 1)
        self.btn_generate = QPushButton('生成语音')
        self.btn_generate.clicked.connect(self._generate_sample)
        sample_row.addWidget(self.btn_generate)
        layout.addLayout(sample_row)
        buttons = QHBoxLayout()
        self.btn_refresh = QPushButton('刷新任务')
        self.btn_refresh.clicked.connect(self.request_refresh)
        buttons.addWidget(self.btn_refresh)
        self.btn_cancel = QPushButton('取消所选')
        self.btn_cancel.clicked.connect(self._cancel_selected)
        buttons.addWidget(self.btn_cancel)
        self.btn_preview = QPushButton('试听所选')
        self.btn_preview.clicked.connect(self._preview_selected)
        buttons.addWidget(self.btn_preview)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.table = QTableWidget(0, 5)
        self.table.setMinimumHeight(160)
        self.table.setStyleSheet('QTableWidget { background: white; color: #292723; } '
            'QHeaderView::section { background: #efede7; color: #57534b; padding: 6px; }')
        self.table.setHorizontalHeaderLabels(['任务', '模式', '状态', '引擎', '费用'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.budget = QLabel('预算：未读取')
        self.budget.setWordWrap(True)
        layout.addWidget(self.budget)
        self.feedback = QLabel('')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)

    # ── 数据 ────────────────────────────────────────────────────────────────
    def refresh_tasks(self) -> list[TaskRow]:
        if self.get is None:
            return self._rows
        try:
            payload = self.get('/api/v2/audio/status') or {}
        except Exception as exc:
            self.feedback.setText('读取任务失败；请确认后台已启动。')
            return self._rows
        return self._apply_tasks(payload)

    def _apply_tasks(self, payload):
        if payload.get('data_root'):
            self.root = Path(payload['data_root'])
        selected = self._selected()
        self._rows = self._rows_from_payload(payload)
        self._budget_text = self._budget_from_payload(payload)
        self.budget.setText(self._budget_text or '预算：未配置云端')
        if _HAS_QT:
            self.table.setRowCount(len(self._rows))
            for row, task in enumerate(self._rows):
                values = [task.job_id[:12], task.capability or 'tts',
                          STATE_LABELS.get(task.state, task.state),
                          task.provider or '未指定', task.cost_note]
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QTableWidgetItem(str(value)))
                if selected and selected.job_id == task.job_id:
                    self.table.selectRow(row)
        self.feedback.setText(self._summary_text(payload))
        if hasattr(self, 'task_timer'):
            if any(row.can_cancel for row in self._rows):
                self.task_timer.start()
            else:
                self.task_timer.stop()
        return self._rows

    def _poll_visible(self):
        if self.isVisible() and not self.requests.busy:
            self.request_refresh()

    def request_refresh(self):
        if self.get is None:
            return
        def work():
            return self.get('/api/v2/audio/status'), self.get('/api/v2/audio/voices')
        def apply(result):
            self._apply_tasks(result[0])
            self._apply_voices(result[1])
        self.requests.start(work, apply, lambda exc: self.feedback.setText(str(exc)[:240]))

    @staticmethod
    def _summary_text(payload: dict) -> str:
        jobs = AudioPanel._rows_from_payload(payload)
        capabilities = (payload or {}).get('capabilities') or {}
        cache = capabilities.get('cache') if isinstance(capabilities, dict) else {}
        text = f'共 {len(jobs)} 条任务' if jobs else '暂无音频任务'
        if isinstance(cache, dict) and cache.get('hits'):
            text += f"；缓存命中 {cache.get('hits')} 次"
        return text

    @staticmethod
    def _task_row(job: dict) -> TaskRow:
        """把 /jobs 返回转成可渲染行（纯函数，可单测）。"""
        try:
            cost = float(job.get('cost_actual') or job.get('cost_reserved') or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        return TaskRow(job_id=str(job.get('job_id', '')),
                       capability=str(job.get('capability') or job.get('mode') or ''),
                       state=str(job.get('state', '')),
                       provider=str(job.get('provider', '')),
                       cost_note=(f'{cost:.4f}' if cost else ''),
                       can_cancel=job.get('state') in ('queued', 'preparing', 'running'))

    @staticmethod
    def _rows_from_payload(payload: dict) -> list[TaskRow]:
        jobs = payload.get('jobs') if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []
        return [AudioPanel._task_row(job) for job in jobs if isinstance(job, dict)]

    def refresh(self) -> list[TaskRow]:
        """标签页切换时与 refresh_tasks 同一入口（保持控件刷新一致）。"""
        rows = self.refresh_tasks()
        self.refresh_voices()
        return rows

    # ── 音色导入/选择 ───────────────────────────────────────────────────────
    def refresh_voices(self) -> list[dict]:
        if self.get is None:
            return self._voices
        try:
            payload = self.get('/api/v2/audio/voices') or {}
        except Exception:
            return self._voices
        return self._apply_voices(payload)

    def _apply_voices(self, payload):
        voices = payload.get('voices') if isinstance(payload, dict) else None
        self._voices = [item for item in (voices or []) if isinstance(item, dict)]
        if _HAS_QT:
            previous = self.voice_combo.currentData()
            self.voice_combo.clear()
            self.voice_combo.addItem('默认音色（跟随设置）', 'persona-default')
            for voice in self._voices:
                engines = ', '.join((voice.get('provider_map') or {}).keys()) or '未映射引擎'
                self.voice_combo.addItem(
                    f"{voice.get('name') or voice.get('profile_id')} · {engines}",
                    voice.get('profile_id'))
            if previous:
                index = self.voice_combo.findData(previous)
                if index >= 0:
                    self.voice_combo.setCurrentIndex(index)
        return self._voices

    def selected_voice(self) -> str:
        if _HAS_QT and self.voice_combo.currentData():
            return str(self.voice_combo.currentData())
        return 'persona-default'

    def import_voices(self, paths: list[str], *, provider: str = '',
                      licensed: bool = False, allow_cloud: bool = False) -> int:
        if self.post is None or not paths:
            return 0
        try:
            result = self.post('/api/v2/audio/voices/import', {
                'paths': list(paths), 'table_path': '', 'provider': provider,
                'licensed': licensed, 'allow_cloud': allow_cloud}) or {}
            count = int(result.get('count') or 0)
            self.feedback.setText(f'已导入 {count} 个音色（已复制到运行数据目录）。')
            self.refresh_voices()
            return count
        except Exception:
            self.feedback.setText('音色导入失败；请检查文件格式/大小后重试。')
            return 0

    def import_voice_table(self, table_path: str) -> int:
        if self.post is None or not table_path:
            return 0
        try:
            result = self.post('/api/v2/audio/voices/import', {
                'paths': [], 'table_path': table_path}) or {}
            count = int(result.get('count') or 0)
            self.feedback.setText(f'已从标签表导入 {count} 个音色。')
            self.refresh_voices()
            return count
        except Exception:
            self.feedback.setText('标签表导入失败；请检查表头与参考音频路径。')
            return 0

    def set_default_voice(self, profile_id: Optional[str] = None) -> bool:
        """把所选音色设为自动语音默认音色（后端持久化到配置）。"""
        if self.post is None:
            return False
        target = str(profile_id or self.selected_voice())
        if target in ('', 'persona-default'):
            self.feedback.setText('请先选择一个已导入的音色。')
            return False
        try:
            self.post(f'/api/v2/audio/voices/{target}/default', {})
            self.feedback.setText(f'已把音色 {target} 设为自动语音默认。')
            return True
        except Exception:
            self.feedback.setText('设置默认音色失败。')
            return False

    def _set_default_voice(self) -> None:
        target = self.selected_voice()
        if target in ('', 'persona-default') or self.post is None:
            self.feedback.setText('请先选择一个已导入的音色。')
            return
        self.requests.start(lambda: self.post(f'/api/v2/audio/voices/{target}/default', {}),
                            lambda _result: self.feedback.setText('已设为默认音色，自动回复需单独开启。'),
                            lambda exc: self.feedback.setText(str(exc)[:240]))

    def generate_sample(self, text: Optional[str] = None) -> Optional[str]:
        if self.post is None:
            return None
        value = str(text if text is not None else (
            self.sample_text.text() if _HAS_QT else '')).strip()
        if not 1 <= len(value) <= 300:
            self.feedback.setText('试听文本须为 1–300 字。')
            return None
        try:
            result = self.post('/api/v2/audio/jobs', {
                'client_request_id': 'ui-' + uuid.uuid4().hex[:16],
                'mode': 'tts', 'text': value,
                'voice_profile_id': self.selected_voice()}) or {}
        except Exception:
            self.feedback.setText('提交失败；请确认后台已启动且引擎已配置。')
            return None
        job_id = str(result.get('job_id') or '')
        if job_id:
            self.feedback.setText(f'已提交试听：{job_id[:12]}（完成后可点“试听所选”）')
            self.refresh_tasks()
            return job_id
        self.feedback.setText('提交失败：没有返回任务编号。')
        return None

    def _pick_voice_files(self) -> None:
        if not _HAS_QT:
            return
        from .file_dialogs import open_files
        paths, _ = open_files(
            self, '选择参考音频（可多选）', '',
            '音频 (*.wav *.flac *.mp3 *.m4a)')
        if paths:
            self._request_import({'paths': paths})

    def _pick_voice_table(self) -> None:
        if not _HAS_QT:
            return
        from .file_dialogs import open_file
        path, _ = open_file(
            self, '选择音色标签表', '', '标签表 (*.csv *.tsv *.json)')
        if path:
            self._request_import({'paths': [], 'table_path': path})

    def _request_import(self, data):
        if self.post is None:
            return
        def apply(result):
            self.feedback.setText(f"已导入 {result.get('count', 0)} 个音色，文件保存在本机。")
            self.request_refresh()
        self.requests.start(lambda: self.post('/api/v2/audio/voices/import', data), apply,
                            lambda exc: self.feedback.setText(str(exc)[:240]))

    def _generate_sample(self) -> None:
        value = self.sample_text.text().strip()
        if not 1 <= len(value) <= 300:
            self.feedback.setText('试听文本须为 1–300 字。')
            return
        if self.post is None:
            return
        data = {'client_request_id': 'ui-' + uuid.uuid4().hex[:16], 'mode': 'tts',
                'text': value, 'voice_profile_id': self.selected_voice()}
        def apply(result):
            job_id = str(result.get('job_id') or '')
            self.feedback.setText('已提交试听：' + job_id[:12] if job_id else '提交失败：没有任务编号。')
            self.request_refresh()
        if self.requests.start(lambda: self.post('/api/v2/audio/jobs', data), apply,
                               lambda exc: self.feedback.setText(str(exc)[:240])):
            self.feedback.setText('正在提交试听…')

    @staticmethod
    def _budget_from_payload(payload: dict) -> str:
        capabilities = payload.get('capabilities') if isinstance(payload, dict) else {}
        budget = (capabilities or {}).get('budget') or {}
        if not isinstance(budget, dict) or not budget:
            return ''
        return (f"预算：今日已用 {budget.get('spent_today', 0)} / "
                f"上限 {budget.get('daily_limit', 0)}，"
                f"预留 {budget.get('reserved_today', 0)}，"
                f"待核实 {budget.get('unknown_today', 0)}")

    def cancel(self, job_id: str) -> None:
        if self.post is None or not job_id:
            return
        try:
            self.post(f'/api/v2/audio/jobs/{job_id}/cancel', {})
            self.feedback.setText('已请求取消；外部服务可能继续生成或计费。')
        except Exception:
            self.feedback.setText('取消失败；请确认后台仍在运行。')
        self.refresh_tasks()

    def _cancel_selected(self) -> None:
        task = self._selected()
        if task is not None and self.post is not None:
            if not task.can_cancel:
                self.feedback.setText('所选任务已结束或正在取消。')
                return
            self.requests.start(lambda: self.post(f'/api/v2/audio/jobs/{task.job_id}/cancel', {}),
                                lambda _result: self.request_refresh(),
                                lambda exc: self.feedback.setText(str(exc)[:240]))

    def _selected(self) -> Optional[TaskRow]:
        if not _HAS_QT or self.table.currentRow() < 0:
            return None
        index = self.table.currentRow()
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def preview(self, job_id: str) -> bool:
        """试听只打开本机受管理文件；不访问公网。"""
        if not job_id or self.get is None:
            self.feedback.setText('请先选择一个已完成的任务。')
            return False
        try:
            row = self.get(f'/api/v2/audio/jobs/{job_id}') or {}
        except Exception:
            self.feedback.setText('无法读取所选任务；没有播放其他音频。')
            return False
        return self._preview_row(row)

    def _preview_row(self, row):
        asset_id = str((row.get('audio') or {}).get('audio_id') or '')
        if row.get('state') != 'ready' or not re.fullmatch(r'[a-f0-9]{64}', asset_id):
            self.feedback.setText('所选任务尚无已完成音频；没有播放其他音频。')
            return False
        directory = (self.root / 'data' / 'voice' / 'assets').resolve()
        path = (directory / (asset_id + '.wav')).resolve()
        try:
            if (not path.is_relative_to(directory) or not path.is_file()
                    or path.stat().st_size > 8 * 1024 * 1024
                    or hashlib.sha256(path.read_bytes()).hexdigest() != asset_id):
                raise ValueError('asset unavailable')
        except (OSError, ValueError):
            self.feedback.setText('所选任务的本机音频缺失或已变化，请重新生成。')
            return False
        self.feedback.setText(f'试听：{path.name}')
        try:
            if _HAS_QT:
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    self.feedback.setText('未能打开系统播放器，请检查默认音频播放器。')
                    return False
            elif os.name == 'nt':
                os.startfile(str(path))  # noqa: S606 —— 只是打开本机文件
        except Exception:
            return False
        return True

    def _preview_selected(self) -> None:
        task = self._selected()
        if task is None or self.get is None:
            self.feedback.setText('请先选择一个已完成的任务。')
            return
        self.requests.start(lambda: self.get(f'/api/v2/audio/jobs/{task.job_id}'),
                            self._preview_row,
                            lambda exc: self.feedback.setText(str(exc)[:240]))

    # ── 回收 ────────────────────────────────────────────────────────────────
    @property
    def busy(self):
        return _HAS_QT and (self.requests.busy or self.setup.requests.busy)

    def cleanup(self) -> None:
        """关闭窗口时安全回收：停工作线程、取消挂起请求（T14 验收）。"""
        if _HAS_QT and (self.requests.busy or self.setup.requests.busy):
            return False
        if _HAS_QT:
            self.task_timer.stop()
            self.requests.close()
            self.setup.cleanup()
        for worker in getattr(self, '_workers', []):
            stop = getattr(worker, 'stop', None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        self._workers.clear()
        return True

    def closeEvent(self, event):  # noqa: N802 —— Qt 约定
        if not self.cleanup():
            event.ignore()
            return
        super().closeEvent(event)
