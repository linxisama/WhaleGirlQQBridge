"""W9b：弹出窗口黑屏回归 + 侧栏直出四个面板。

三件事锁死：
1. 弹层规则（QMenu / QToolTip / QMessageBox / 下拉列表 / 文件对话框）必须写在
   qq_gui.STYLESHEET 里，DIALOG_STYLESHEET 才能继承；main() 必须在建窗口前把
   应用级样式装到 QApplication 上（无父窗口的弹窗继承不到窗口级样式）。
2. 客观亮度：离屏把同一个 QMenu 渲染成图片算平均亮度。改前的样子（只有窗口那条
   `QWidget { background: transparent }`、菜单没有任何规则）是 6.x（黑），改后 >200（白）。
3. 侧栏：机器人管理 / 人设 / 插件 / 外观四页直接占入口，不再有「更多面板」弹出菜单。

所有落盘目标都在临时目录，不碰仓库里的 config.qq.yaml / gui_config.json。
"""
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtWidgets import QApplication, QMenu, QWidget
    HAVE_QT = True
except Exception:                   # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = None

# 改前 MainWindow 用的样式表长这样：只有一条"背景透明"的 QWidget 规则，没有任何
# QMenu 规则。弹出菜单作为窗口的子对象继承了透明背景，在 Windows 上就是一块纯黑矩形。
_PRE_FIX_WINDOW_SHEET = ('QWidget { background: transparent; color: #1F2733; '
                         'font-family: "Microsoft YaHei"; font-size: 13px; }')

_MENU_ITEMS = ("机器人管理", "人设", "插件", "外观")


def _app():
    """必须持有模块级引用：QApplication 被回收后再造 QWidget 会直接终止进程。"""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _flush_deletes() -> None:
    app = _app()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _brightness(image) -> float:
    """平均亮度（0-255，Rec.601 权重）；透明像素按它的 RGB 值算，不做 alpha 混合。"""
    total = 0.0
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            total += (color.red() * 299 + color.green() * 587 + color.blue() * 114) / 1000.0
            count += 1
    return total / max(count, 1)


def _render_menu(host_sheet, put_style):
    """按"父窗口样式 + 是否套 menu 样式"离屏渲染一个 QMenu，返回 (亮度, 图片)。"""
    app = _app()
    host = QWidget()
    menu = QMenu(host)
    host.setStyleSheet(host_sheet)
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    for name in _MENU_ITEMS:
        menu.addAction(name + "…")
    if put_style:
        from src.gui.menu_style import style_menu
        style_menu(menu)
    menu.show()
    app.processEvents()
    menu.adjustSize()
    app.processEvents()
    image = menu.grab().toImage()
    brightness = _brightness(image)
    menu.hide()
    host.deleteLater()
    _flush_deletes()
    return brightness, image


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class MenuStylesheetTextTests(unittest.TestCase):
    """纯字符串层面：规则齐不齐、有没有进 STYLESHEET、main() 装没装。"""

    @classmethod
    def setUpClass(cls):
        _app()
        import qq_gui
        cls.gui = qq_gui
        from src.gui import menu_style
        cls.menu_style = menu_style

    def test_menu_stylesheet_is_a_plain_string(self):
        sheet = self.menu_style.menu_stylesheet()
        self.assertIsInstance(sheet, str)
        for token in ("QMenu {", "QMenu::item", "QMenu::item:selected",
                      "QMenu::item:disabled", "QMenu::separator"):
            self.assertIn(token, sheet, token + " 规则缺失")

    def test_menu_stylesheet_keeps_background_white_and_text_dark(self):
        sheet = self.menu_style.menu_stylesheet()
        self.assertIn("background-color", sheet, "菜单必须有实心底色，透明就是黑屏")
        self.assertIn("#FFFFFF", sheet.upper())

    def test_menu_stylesheet_accepts_theme_overrides(self):
        sheet = self.menu_style.menu_stylesheet(card="#000000", text="#FFFFFF")
        self.assertIn("#000000", sheet)
        self.assertIn("#FFFFFF", sheet)
        # 不传的键仍走默认值，不会被 None 写坏
        self.assertIn("#D3DAE3", self.menu_style.menu_stylesheet(card=None))

    def test_menu_style_module_does_not_need_qt(self):
        source = Path(self.menu_style.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith(("import PyQt6", "from PyQt6")),
                             "menu_style 不能在模块级依赖 Qt：" + stripped)

    def test_stylesheet_carries_menu_rules(self):
        for token in ("QMenu {", "QMenu::item:selected", "QMenu::item:disabled",
                      "QMenu::separator"):
            self.assertIn(token, self.gui.STYLESHEET, token + " 必须写进 STYLESHEET")

    def test_stylesheet_carries_tooltip_rule(self):
        self.assertIn("QToolTip {", self.gui.STYLESHEET)

    def test_stylesheet_carries_messagebox_rules(self):
        for token in ("QMessageBox {", "QMessageBox QLabel", "QDialog {"):
            self.assertIn(token, self.gui.STYLESHEET, token + " 规则缺失（无父窗口弹窗靠它）")

    def test_stylesheet_carries_combo_popup_and_file_dialog_rules(self):
        sheet = self.gui.STYLESHEET
        self.assertIn("QComboBox QAbstractItemView", sheet)
        self.assertIn("selection-background-color", sheet, "下拉列表选中项要显式给底色")
        for token in ("QFileDialog QListView", "QFileDialog QLineEdit"):
            self.assertIn(token, sheet, token + " 不写就是黑底输入框")

    def test_dialog_stylesheet_inherits_popup_rules(self):
        dialog = self.gui.DIALOG_STYLESHEET
        self.assertTrue(dialog.startswith(self.gui.STYLESHEET), "DIALOG_STYLESHEET 必须继承 STYLESHEET")
        for token in ("QMenu {", "QToolTip {", "QMessageBox {"):
            self.assertIn(token, dialog)

    def test_main_installs_app_stylesheet_before_window(self):
        """源码级：app.setStyleSheet 必须在 MainWindow() 之前，且装的是弹层规则。"""
        source = inspect.getsource(self.gui.main)
        install_at = source.find("app.setStyleSheet(")
        window_at = source.find("MainWindow()")
        self.assertGreater(install_at, -1, "main() 里没有应用级 setStyleSheet")
        self.assertGreater(window_at, -1)
        self.assertLess(install_at, window_at, "应用级样式必须在建窗口之前装")
        self.assertIn("POPUP_STYLESHEET", source[install_at:install_at + 80])

    def test_popup_stylesheet_is_part_of_stylesheet(self):
        self.assertIn(self.gui.POPUP_STYLESHEET.strip()[:40], self.gui.STYLESHEET)


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class MenuRenderBrightnessTests(unittest.TestCase):
    """客观证据：同一个 QMenu，改前黑、改后白。"""

    @classmethod
    def setUpClass(cls):
        _app()

    def test_menu_without_style_renders_dark(self):
        brightness, _image = _render_menu(_PRE_FIX_WINDOW_SHEET, put_style=False)
        self.assertLess(brightness, 100.0,
                        "改前的菜单应当是暗的（实际 %.1f），否则这个对照组没意义" % brightness)

    def test_styled_menu_renders_light(self):
        brightness, image = _render_menu(_PRE_FIX_WINDOW_SHEET, put_style=True)
        self.assertGreater(brightness, 200.0, "套上样式的菜单必须是浅色，实际亮度 %.1f" % brightness)
        self.assertGreater(image.width(), 80)
        self.assertGreater(image.height(), 40)

    def test_style_menu_is_idempotent(self):
        from src.gui.menu_style import style_menu
        menu = QMenu()
        style_menu(menu)
        once = menu.styleSheet()
        style_menu(menu)
        style_menu(menu)
        self.assertEqual(menu.styleSheet(), once, "重复调用不该改结果")
        self.assertIn("background", once)
        menu.deleteLater()
        _flush_deletes()

    def test_style_menu_tolerates_none_and_foreign_objects(self):
        from src.gui.menu_style import style_menu
        style_menu(None)
        style_menu(object())        # 没有 setStyleSheet 就当没这回事

    def test_tray_menu_uses_shared_style(self):
        from PyQt6.QtGui import QIcon
        from src.gui.tray import RobotTray

        class _StubWindow(QWidget):
            def __init__(self):
                super().__init__()
                self._quitting = False
                self._busy = False

            def _request_exit(self):
                pass

        window = _StubWindow()
        tray = RobotTray(window, QIcon(), available=False)
        self.assertIn("QMenu", tray.menu.styleSheet())
        self.assertIn("background-color", tray.menu.styleSheet())
        tray.menu.deleteLater()
        window.deleteLater()
        _flush_deletes()

    def test_pet_window_source_styles_its_menu(self):
        import src.pet_window as pet_window
        source = Path(pet_window.__file__).read_text(encoding="utf-8")
        self.assertIn("style_menu(menu)", source, "桌宠右键菜单也要套同一份样式")


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class SidebarEntryTests(unittest.TestCase):
    """侧栏：四个页面直出，不再有「更多面板」。"""

    @classmethod
    def setUpClass(cls):
        _app()
        import qq_gui
        from src.gui import agent_settings
        cls.gui = qq_gui
        cls.agent_settings = agent_settings
        cls._root_tmp = tempfile.TemporaryDirectory(prefix="w9b-gui-")
        sandbox = Path(cls._root_tmp.name) / "config.qq.yaml"
        sandbox.write_text('ai:\n  api_url: ""\n', encoding="utf-8")
        agent_settings.set_config_path(sandbox)

    @classmethod
    def tearDownClass(cls):
        cls.agent_settings.set_config_path(None)
        _flush_deletes()
        cls._root_tmp.cleanup()

    def setUp(self):
        backend = patch.object(self.gui.MainWindow, "_start_backend")
        backend.start()
        self.addCleanup(backend.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="w9b-win-")
        self.addCleanup(self.tmp.cleanup)
        original = self.gui.GUI_CFG
        self.gui.GUI_CFG = Path(self.tmp.name) / "gui_config.json"
        self.addCleanup(lambda: setattr(self.gui, "GUI_CFG", original))

    def _dispose(self, window):
        try:
            window._quitting = True
            watchdog = getattr(window, "hang_watchdog", None)
            for timer in (getattr(window, "_timer", None),
                          getattr(getattr(window, "pc_tab", None), "timer", None),
                          getattr(watchdog, "_timer", None)):
                if timer is not None:
                    timer.stop()
            window.hide()
            window.deleteLater()
            _flush_deletes()
        except RuntimeError:
            pass

    def make_window(self):
        window = self.gui.MainWindow()
        self.addCleanup(self._dispose, window)
        return window

    def nav_texts(self, window):
        items = window.nav_list
        return [items.item(row).text() for row in range(items.count())]

    def test_four_pages_are_direct_sidebar_rows(self):
        window = self.make_window()
        texts = self.nav_texts(window)
        self.assertEqual(self.gui.HIDDEN_NAV_ENTRIES, ())
        for name in _MENU_ITEMS:
            self.assertIn(name, texts, name + " 必须直接出现在侧栏")

    def test_no_more_panel_row(self):
        window = self.make_window()
        self.assertNotIn("更多面板", self.nav_texts(window))
        self.assertEqual([row for row, target in window._nav_rows if target == "more"], [])

    def test_navigation_still_reaches_the_four_pages(self):
        window = self.make_window()
        for name in _MENU_ITEMS:
            window._assistant_navigate(name)
            self.assertEqual(window.tabs.tabText(window.tabs.currentIndex()), name)

    def test_title_groups_keep_existing_names(self):
        window = self.make_window()
        texts = self.nav_texts(window)
        # 分组标题沿用文件里原有的名字，没自创新分组
        self.assertIn("工作空间", texts)
        self.assertIn("管理", texts)
        self.assertLess(texts.index("工作空间"), texts.index("机器人管理"))
        for name in ("人设", "插件", "外观"):
            self.assertGreater(texts.index(name), texts.index("管理"), name + " 归「管理」组")


if __name__ == "__main__":
    unittest.main()
