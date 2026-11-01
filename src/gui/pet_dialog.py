"""桌宠资源包管理对话框：列表、导入、启动、停止。"""
from pathlib import Path

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout

from src.gui import file_dialogs
from qq_gui import api_get, api_post


class PetDialog(QDialog):
    def __init__(self, window, parent=None):
        super().__init__(parent or window)
        self.window = window
        self.setWindowTitle('桌宠')
        self.resize(620, 460)
        layout = QVBoxLayout(self)
        hint = QLabel('资源包只允许 pet.json + 图片/GIF 帧；安装到用户数据目录，不执行代码。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        import_button = QPushButton('导入资源包…')
        import_button.clicked.connect(self.import_pack)
        launch = QPushButton('启动选中')
        launch.clicked.connect(self.launch)
        stop = QPushButton('停止选中')
        stop.clicked.connect(self.stop)
        refresh = QPushButton('刷新')
        refresh.clicked.connect(self.refresh)
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        for button in (import_button, launch, stop, refresh):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        self.refresh()

    def refresh(self):
        try:
            data = api_get('/api/pet/packs')
        except Exception as exc:
            self.list.clear()
            self.list.addItem(QListWidgetItem(f'管理器不可达：{exc}'))
            return
        self.list.clear()
        for pack in data.get('packs') or []:
            row = QListWidgetItem(f"{pack.get('name')}（{pack.get('id')}）· {pack.get('frames')} 帧")
            row.setData(256, pack.get('id'))
            self.list.addItem(row)
        if not (data.get('packs') or []):
            self.list.addItem('还没有桌宠，点「导入资源包…」')

    def _selected(self):
        item = self.list.currentItem()
        return item.data(256) if item is not None else ''

    def import_pack(self):
        path, _ = file_dialogs.open_file(self, '选择桌宠资源包', '', '桌宠资源包 (*.zip)')
        if not path:
            return
        try:
            info = api_post('/api/pet/inspect', {'path': path})
        except Exception as exc:
            QMessageBox.warning(self, '桌宠', f'预检失败：{exc}')
            return
        text = (f"名称：{info.get('name')}\n标识：{info.get('id')}\n"
                f"帧数：{len(info.get('frames') or [])}\n大小：{info.get('bytes', 0) / 1024:.0f} KB")
        if QMessageBox.question(self, '导入桌宠', text + '\n\n确认导入？') != QMessageBox.StandardButton.Yes:
            return
        try:
            api_post('/api/pet/import', {'path': path, 'confirmed': True})
        except Exception as exc:
            QMessageBox.warning(self, '桌宠', f'导入失败：{exc}')
            return
        self.refresh()

    def launch(self):
        identifier = self._selected()
        if not identifier:
            return
        try:
            api_post('/api/pet/launch', {'id': identifier})
        except Exception as exc:
            QMessageBox.warning(self, '桌宠', f'启动失败：{exc}')

    def stop(self):
        identifier = self._selected()
        if not identifier:
            return
        try:
            api_post('/api/pet/stop', {'id': identifier})
        except Exception as exc:
            QMessageBox.warning(self, '桌宠', f'停止失败：{exc}')
