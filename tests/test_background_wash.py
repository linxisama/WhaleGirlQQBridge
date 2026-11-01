"""有背景图时界面不许发白：面板白底要变成能透出照片的玻璃底。

三条硬约束（改坏任何一条都算回归）：
1. 有背景图 + 用户没拖过模糊度滑块 → 图片层本身不被白色叠加（原图画什么就是什么）；
2. 有背景图时面板/表格/文本区的白底必须比"无图老口径"薄，且「面板透明度」滑块
   两端的语义不变（0 = 面板完全不透明，100% = 完全透明），滑块不能变成空操作；
3. 没有背景图时口径与改动前逐位一致：白色通道还是 round(255 * (1 - 滑块))。
"""
import os
import pathlib
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import qq_gui  # noqa: E402

APP = QApplication.instance() or QApplication([])

SOLID = (32, 64, 96)


def solid_pixmap(width=120, height=80):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(*SOLID))
    return QPixmap.fromImage(image)


def white_alphas(sheet: str):
    """样式表里所有 rgba(255, 255, 255, N) 的 N。"""
    return {int(m) for m in re.findall(r"rgba\(\s*255\s*,\s*255\s*,\s*255\s*,\s*(\d+)\s*\)", sheet)}


class PanelAlphaFormulaTests(unittest.TestCase):
    def test_no_image_keeps_the_old_linear_mapping(self):
        for panel in (0.0, 0.2, 0.35, 0.65, 1.0):
            self.assertAlmostEqual(qq_gui.panel_alpha(panel, False), 1.0 - panel, places=9)
        # 老口径下 smoke 用的 65% 滑块仍然是 89（组件级 alpha 89 的断言照旧成立）
        self.assertEqual(round(255 * qq_gui.panel_alpha(0.65, False)), 89)

    def test_image_makes_the_white_thinner(self):
        # 滑块两端语义不变：0 = 面板完全不透明，100% = 完全透明
        self.assertEqual(round(255 * qq_gui.panel_alpha(0.0, True)), 255)
        self.assertEqual(round(255 * qq_gui.panel_alpha(1.0, True)), 0)
        default_image = round(255 * qq_gui.panel_alpha(0.35, True))
        default_plain = round(255 * qq_gui.panel_alpha(0.35, False))
        self.assertEqual(default_plain, 166)          # 改动前的用户默认状态
        self.assertLess(default_image, default_plain)
        # 默认档必须真的薄下来（改前 166/255 = 65% 白，整片区域白化 65%）
        self.assertLessEqual(default_image, 120, default_image)

    def test_slider_is_not_a_no_op_with_an_image(self):
        low = round(255 * qq_gui.panel_alpha(0.20, True))
        high = round(255 * qq_gui.panel_alpha(0.80, True))
        self.assertGreater(low, high)
        self.assertGreater(low - high, 40, (low, high))  # 仍有可感知的跨度

    def test_bad_values_are_clamped(self):
        self.assertAlmostEqual(qq_gui.panel_alpha(-3, True), 1.0)
        self.assertAlmostEqual(qq_gui.panel_alpha(9, True), 0.0)
        self.assertAlmostEqual(qq_gui.panel_alpha("坏值", True), 1.0)


class ImageLayerTests(unittest.TestCase):
    """① 图片层本身不许被白色叠加。"""

    def _center(self, frame):
        frame.resize(60, 40)
        frame.show()
        APP.processEvents()
        color = frame.grab().toImage().pixelColor(30, 20)
        frame.close()
        return (color.red(), color.green(), color.blue())

    def test_image_layer_is_drawn_raw(self):
        frame = qq_gui.BackgroundFrame()
        frame.set_background_pixmap(solid_pixmap())
        frame.set_blur(0.70)                     # 用户没拖过滑块
        self.assertTrue(frame.has_background_image())
        self.assertEqual(qq_gui.image_scrim_alpha(0.70, False, True), 0.0)
        self.assertEqual(self._center(frame), SOLID)

    def test_without_image_everything_is_as_before(self):
        frame = qq_gui.BackgroundFrame()
        frame.set_blur(0.70)
        self.assertFalse(frame.has_background_image())
        base = qq_gui.overlay_color(qq_gui.BG, 0.70)
        self.assertEqual(self._center(frame), (base.red(), base.green(), base.blue()))
        self.assertEqual((base.red(), base.green(), base.blue()), (244, 246, 249))


class WindowPanelGlassTests(unittest.TestCase):
    """②③ 真窗口：有图时面板底色是玻璃，无图时口径一分不差。"""

    @classmethod
    def setUpClass(cls):
        import qq_gui as gui
        from src.gui import agent_settings
        cls.gui = gui
        cls.agent_settings = agent_settings
        cls._tmp = tempfile.TemporaryDirectory(prefix="bg-wash-")
        cls.root = pathlib.Path(cls._tmp.name)
        cls._cfg_backup = gui.GUI_CFG
        cls._sandbox_cfg = cls.root / "config.qq.yaml"
        cls._sandbox_cfg.write_text("ai:\n  api_url: \"\"\n", encoding="utf-8")
        agent_settings.set_config_path(cls._sandbox_cfg)
        gui.GUI_CFG = cls.root / "gui_config.json"
        # 背景图必须落在仓库根目录内（appearance.resolve_background 的约定），用完删掉
        cls._img_dir = pathlib.Path(tempfile.mkdtemp(prefix="bg-wash-img-", dir=str(gui.ROOT)))
        cls.image = cls._img_dir / "wash.png"
        solid_pixmap(400, 240).save(str(cls.image))
        cls.image_rel = str(cls.image.relative_to(pathlib.Path(gui.ROOT)))

    @classmethod
    def tearDownClass(cls):
        cls.agent_settings.set_config_path(None)
        cls.gui.GUI_CFG = cls._cfg_backup
        import shutil
        shutil.rmtree(cls._img_dir, ignore_errors=True)
        cls._tmp.cleanup()

    def setUp(self):
        backend = patch.object(self.gui.MainWindow, "_start_backend")
        backend.start()
        self.addCleanup(backend.stop)
        self.window = self.gui.MainWindow()
        self.addCleanup(self._dispose, self.window)

    def _dispose(self, window):
        try:
            window._quitting = True
            for timer in (getattr(window, "_timer", None),
                          getattr(getattr(window, "pc_tab", None), "timer", None),
                          getattr(getattr(window, "workbench_tab", None), "_status_timer", None),
                          getattr(getattr(window, "hang_watchdog", None), "_timer", None),
                          getattr(window, "_hang_notice_timer", None)):
                if timer is not None:
                    timer.stop()
            window.hide()
            window.deleteLater()
            APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()
        except RuntimeError:
            pass

    def _appearance(self, **values):
        body = {"panel_transparency": 0.35, "transparency": 0.0, "blur": 0.7,
                "blur_explicit": False, "bg_color": "#F4F6F9", "background_image": ""}
        body.update(values)
        self.window._apply_appearance(body)
        APP.processEvents()

    def test_panel_background_is_translucent_glass_with_an_image(self):
        self._appearance(background_image=self.image_rel)
        self.assertTrue(self.window._container.has_background_image())
        alphas = white_alphas(self.window.styleSheet())
        self.assertTrue(alphas, "面板样式表里应当有白色 rgba")
        for alpha in alphas:
            self.assertLess(alpha, 255, "有背景图时面板白底不许是不透明白")
        self.assertLessEqual(max(alphas), 120, alphas)
        # 文本区（QTextEdit）跟着同一个口径走，不能比主面板更白
        view_alphas = white_alphas(self.window.console_tab.msg_view.styleSheet())
        self.assertEqual(view_alphas, alphas)
        # 记忆页的表格：解析它自己的样式表
        table_alphas = {int(round(255 * float(m))) for m in
                        re.findall(r"rgba\(255,255,255,([0-9.]+)\)",
                                   self.window.memory_tab.styleSheet())}
        self.assertTrue(table_alphas)
        self.assertLessEqual(max(table_alphas), 120, table_alphas)

    def test_slider_still_drives_the_panels_with_an_image(self):
        self._appearance(background_image=self.image_rel, panel_transparency=1.0)
        self.assertEqual(white_alphas(self.window.styleSheet()), {0})
        self._appearance(background_image=self.image_rel, panel_transparency=0.0)
        self.assertEqual(white_alphas(self.window.styleSheet()), {255})

    def test_no_image_is_byte_identical_to_the_old_rule(self):
        self._appearance(background_image="")
        self.assertFalse(self.window._container.has_background_image())
        self.assertEqual(white_alphas(self.window.styleSheet()), {166})
        self.assertEqual(white_alphas(self.window.console_tab.msg_view.styleSheet()), {166})
        self._appearance(background_image="", panel_transparency=0.65)
        self.assertEqual(white_alphas(self.window.styleSheet()), {89})

    def test_nested_welcome_card_does_not_double_the_white(self):
        self._appearance(background_image=self.image_rel)
        sheet = self.window.assistant_tab.styleSheet()
        self.assertIn("QFrame#agentWelcome {background:transparent;", sheet)
        self.assertNotIn("QFrame#agentWelcome {background:rgba(255,255,255,", sheet)


if __name__ == "__main__":
    unittest.main()
