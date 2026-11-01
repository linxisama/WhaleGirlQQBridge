"""Incremental local-engine configuration; never edits model/QQ/persona settings."""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QComboBox, QLineEdit, QLabel, QPushButton, QStackedWidget, QSizePolicy)

from .file_dialogs import open_file
from .panel_requests import PanelRequests


PROVIDERS = [('audio.cpp', 'audio_cpp'), ('GPT-SoVITS', 'gpt_sovits_api_v2'),
             ('Qwen TTS', 'qwen_webui')]
LABELS = {
    'audio_cpp': [('endpoint', '合成接口'), ('model', '模型名称'), ('voice', '已登记音色')],
    'gpt_sovits_api_v2': [('endpoint', '合成接口'), ('ref_audio_path', '参考音频'),
                        ('prompt_text', '参考音频台词'), ('prompt_lang', '参考语言'),
                        ('text_lang', '合成语言')],
    'qwen_webui': [('endpoint', '服务地址'), ('mode', '模式'), ('model', '本机模型名称'),
                  ('speaker', '说话人'), ('ref_audio_path', '参考音频'),
                  ('ref_text', '参考音频台词'), ('instruct', '表达要求')],
}


class EnginePages(QStackedWidget):
    def sizeHint(self):
        return self.currentWidget().sizeHint() if self.currentWidget() else super().sizeHint()

    def minimumSizeHint(self):
        return self.currentWidget().minimumSizeHint() if self.currentWidget() else super().minimumSizeHint()


class AudioSetup(QGroupBox):
    def __init__(self, get, post, parent=None):
        super().__init__('本地引擎设置（展开配置）', parent)
        self.get, self.post = get, post
        self.loaded = False
        self.requests = PanelRequests(self)
        self.setCheckable(True)
        self.setChecked(False)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        self.body = QWidget()
        inner = QVBoxLayout(self.body)
        inner.setContentsMargins(0, 0, 0, 0)
        self.provider = QComboBox()
        for label, value in PROVIDERS:
            self.provider.addItem(label, value)
        inner.addWidget(self.provider)
        self.stack = EnginePages()
        self.stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.edits = {}
        for _label, name in PROVIDERS:
            page = QWidget()
            form = QFormLayout(page)
            fields = self.edits[name] = {}
            for key, label in LABELS[name]:
                if key == 'mode':
                    field = QComboBox()
                    field.addItems(['custom_voice', 'voice_clone', 'voice_design'])
                else:
                    field = QLineEdit()
                fields[key] = field
                if key == 'ref_audio_path':
                    line = QHBoxLayout()
                    line.addWidget(field, 1)
                    button = QPushButton('上传参考音频…')
                    button.clicked.connect(lambda _checked=False, p=name: self.import_reference(p))
                    line.addWidget(button)
                    form.addRow(label, line)
                else:
                    form.addRow(label, field)
            self.stack.addWidget(page)
        self.provider.currentIndexChanged.connect(self._select_provider)
        self._select_provider(0)
        inner.addWidget(self.stack)
        buttons = QHBoxLayout()
        self.save = QPushButton('保存本地引擎设置')
        self.probe = QPushButton('检测连接')
        self.reload = QPushButton('重新读取')
        self.save.clicked.connect(self.save_settings)
        self.probe.clicked.connect(self.probe_connection)
        self.reload.clicked.connect(self.load_settings)
        for button in (self.save, self.probe, self.reload):
            buttons.addWidget(button)
        inner.addLayout(buttons)
        self.notice = QLabel('只配置本机接口；保存后使用本地路由。自动回复仍需在插件页单独开启。')
        self.notice.setWordWrap(True)
        inner.addWidget(self.notice)
        layout.addWidget(self.body)
        self.body.setVisible(False)
        self.toggled.connect(self._expanded)

    def _expanded(self, expanded):
        self.body.setVisible(expanded)
        if expanded and not self.loaded:
            self.load_settings()

    def _select_provider(self, index):
        self.stack.setCurrentIndex(index)
        self.stack.updateGeometry()

    def _run(self, work, apply):
        if not callable(self.get) or not callable(self.post):
            self.notice.setText('控制台尚未连接后台。')
            return
        if not self.requests.start(work, apply, self._error):
            self.notice.setText('当前请求仍在处理，请稍候。')
        else:
            self.notice.setText('正在处理…')

    def _error(self, exc):
        self.notice.setText(str(exc)[:240] or '请求失败，请检查后台连接。')

    def load_settings(self):
        self._run(lambda: self.get('/api/v2/audio/settings'), self.apply_settings)

    def apply_settings(self, data):
        providers = data.get('providers') if isinstance(data, dict) else None
        if not isinstance(providers, dict):
            self.notice.setText('后台尚不支持引擎设置，请使用匹配版本启动控制台。')
            return
        for name, fields in self.edits.items():
            params = providers.get(name) or {}
            for key, edit in fields.items():
                value = str(params.get(key) or '')
                if isinstance(edit, QComboBox):
                    edit.setCurrentText(value)
                else:
                    edit.setText(value)
        index = self.provider.findData(data.get('provider'))
        if index >= 0:
            self.provider.setCurrentIndex(index)
        self.loaded = True
        self.notice.setText('已读取。检测仅检查本机接口；生成和 QQ 播放需分别验证。')

    def collect(self):
        name = self.provider.currentData()
        params = {key: edit.currentText() if isinstance(edit, QComboBox) else edit.text()
                  for key, edit in self.edits[name].items()}
        return {'provider': name, 'params': params, 'route_policy': 'local_only'}

    def save_settings(self):
        data = self.collect()
        def applied(result):
            self.loaded = True
            self.notice.setText('已保存本地引擎设置；可以生成一句试听，自动回复未自动开启。')
        self._run(lambda: self.post('/api/v2/audio/settings', data), applied)

    def probe_connection(self):
        data = self.collect()
        def applied(result):
            self.notice.setText(str(result.get('message') or '检测未返回结果'))
        self._run(lambda: self.post('/api/v2/audio/probe', data), applied)

    def import_reference(self, provider):
        if self.requests.busy:
            self.notice.setText('请等待当前请求完成。')
            return
        path, _ = open_file(self, '导入参考音频（复制到本机运行数据目录）', '',
                            '参考音频 (*.wav *.flac *.mp3)')
        if not path:
            return
        def applied(result):
            items = result.get('imported') or []
            reference = str(items[0].get('ref_audio_path') or '') if items else ''
            if not reference:
                self.notice.setText('未得到托管参考音频路径，请检查导入结果。')
                return
            self.edits[provider]['ref_audio_path'].setText(reference)
            self.notice.setText('参考音频已复制；补全参考台词后点击保存设置。未上传到公网。')
        self._run(lambda: self.post('/api/v2/audio/voices/import',
                                   {'paths': [path], 'provider': provider}), applied)

    def cleanup(self):
        return self.requests.close()
