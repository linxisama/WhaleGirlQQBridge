"""GUI 表头黑条回归：全局透明背景不能把 QHeaderView 画成黑底。

截图里「本地工作空间」和「外部后端」的表格表头是一条纯黑横条。根因是
STYLESHEET 只有 QWidget { background: transparent }，QHeaderView 跟着透明后
Windows 原生素材会画成黑底。这里用离屏渲染取表头顶部像素，锁住浅色表头。
"""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication, QWidget
    from src.gui import pc_workspace
    from src.gui.qt_locale import light_palette
    import qq_gui
    HAVE_QT = True
except Exception:  # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

_APP = None


def _app():
    """必须持有模块级引用：QApplication 被回收后再造 QWidget 会直接终止进程。"""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class TableHeaderThemeTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self._old_sheet = self.app.styleSheet()
        self.app.setPalette(light_palette())
        self.app.setStyleSheet(qq_gui.STYLESHEET)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        self.app.setStyleSheet(self._old_sheet)

    def test_stylesheet_has_opaque_header_rules(self):
        self.assertIn("QHeaderView { background: #F7F9FC; }", qq_gui.STYLESHEET)
        self.assertIn("QHeaderView::section", qq_gui.STYLESHEET)
        self.assertIn("QTableCornerButton::section", qq_gui.STYLESHEET)

    def test_workspace_header_is_light_not_black(self):
        host = QWidget()
        host.resize(900, 500)
        host.show()
        self.addCleanup(host.deleteLater)
        panel = pc_workspace.WorkspacePanel(data_dir=self._tmp.name, parent=host)
        panel.resize(880, 480)
        panel.show()
        _app().processEvents()
        self.addCleanup(panel.deleteLater)

        header = panel.table.horizontalHeader()
        self.assertGreater(header.height(), 2)
        pos = header.mapTo(panel, QPoint(0, 0))
        image = panel.grab().toImage()
        # 取表头内靠上的像素：避开文字和底部分隔线，只验证底色。
        for x in (20, header.width() // 2, max(20, header.width() - 20)):
            color = image.pixelColor(pos.x() + x, pos.y() + 2)
            self.assertGreater(color.red(), 200, (x, color.name()))
            self.assertGreater(color.green(), 200, (x, color.name()))
            self.assertGreater(color.blue(), 200, (x, color.name()))
            self.assertNotEqual(color.name(), "#000000")


if __name__ == "__main__":
    unittest.main()
