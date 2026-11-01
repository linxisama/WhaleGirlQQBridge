"""桌宠独立进程：透明置顶窗口，读取资源包播放图片/GIF 帧。

不依赖主控制台；关闭主程序不影响，右键菜单退出。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QMovie, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget

from src.gui.menu_style import style_menu


class PetWindow(QWidget):
    def __init__(self, folder: Path):
        super().__init__()
        self.folder = Path(folder)
        self.meta = json.loads((self.folder / 'pet.json').read_text(encoding='utf-8'))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(str(self.meta.get('name') or '桌宠'))
        self.label = QLabel(self)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.frames = []
        self.index = 0
        self.movie = None
        scale = max(0.1, min(1.0, float(self.meta.get('scale') or 0.35)))
        frames = list(self.meta.get('frames') or [])
        gif = next((str(name) for name in frames if str(name).lower().endswith('.gif')), '')
        if gif:
            self.movie = QMovie(str(self.folder / gif))
            self.movie.setCacheMode(QMovie.CacheMode.CacheAll)
            self.label.setMovie(self.movie)
            self.movie.start()
            size = self.movie.currentPixmap().size()
            if size.isEmpty():
                size = self.movie.frameRect().size()
            self.label.resize(max(80, int(size.width() * scale)), max(80, int(size.height() * scale)))
            self.label.setScaledContents(True)
            self.resize(self.label.size())
        else:
            for name in frames:
                pix = QPixmap(str(self.folder / str(name)))
                if not pix.isNull():
                    self.frames.append(pix)
            if not self.frames:
                raise ValueError('资源包里没有可读取的桌宠帧')
            first = self.frames[0]
            width = max(80, min(480, int(first.width() * scale)))
            height = max(80, min(480, int(first.height() * scale)))
            self.label.resize(width, height)
            self.label.setScaledContents(True)
            self.resize(width, height)
            self._show_frame()
            fps = max(1, min(30, int(self.meta.get('fps') or 8)))
            if len(self.frames) > 1:
                self.timer = QTimer(self)
                self.timer.timeout.connect(self._next)
                self.timer.start(max(40, int(1000 / fps)))
        self._drag = None
        self._position_file = self.folder / '.position.json'
        self._restore_position()

    def _show_frame(self):
        if self.frames:
            self.label.setPixmap(self.frames[self.index])

    def _next(self):
        self.index = (self.index + 1) % max(1, len(self.frames))
        self._show_frame()

    def _restore_position(self):
        try:
            data = json.loads(self._position_file.read_text(encoding='utf-8'))
            self.move(int(data['x']), int(data['y']))
            return
        except (OSError, ValueError, KeyError):
            pass
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 40, screen.bottom() - self.height() - 60)

    def _save_position(self):
        try:
            self._position_file.write_text(json.dumps({'x': self.x(), 'y': self.y()}), encoding='utf-8')
        except OSError:
            pass

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag = None
        self._save_position()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        style_menu(menu)          # 桌宠右键菜单同样要吃全局浅色规则（深色系统下不然是黑框）
        hide = QAction('隐藏 10 秒', self)
        hide.triggered.connect(lambda: (self.hide(), QTimer.singleShot(10000, self.show)))
        quit_action = QAction('退出桌宠', self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(hide)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.exec(event.globalPos())

    def closeEvent(self, event):
        self._save_position()
        super().closeEvent(event)


def main(folder) -> int:
    app = QApplication.instance() or QApplication([sys.argv[0]])
    window = PetWindow(Path(folder))
    window.show()
    return app.exec()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
