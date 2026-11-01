"""背景蒙层：有背景图且用户没拖过模糊度滑块时不叠白蒙层。

三条硬约束（改坏任何一条都算回归）：
1. 老配置（只有 blur、没有 blur_explicit）→ 有图也不叠蒙层；
2. 用户拖过滑块 → 按 scrim_alpha_for_blur 的老公式加回蒙层；
3. 没有背景图时，底色渲染与改动前逐像素一致。
"""
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import qq_gui  # noqa: E402

APP = QApplication.instance() or QApplication([])

SOLID = (32, 64, 96)


def solid_pixmap(width=120, height=80):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(*SOLID))
    return QPixmap.fromImage(image)


class ScrimFormulaTests(unittest.TestCase):
    def test_slider_formula_is_unchanged(self):
        self.assertAlmostEqual(qq_gui.scrim_alpha_for_blur(0.70), 0.595, places=6)
        self.assertAlmostEqual(qq_gui.scrim_alpha_for_blur(1.0), 0.85, places=6)
        self.assertEqual(qq_gui.scrim_alpha_for_blur(0.0), 0.0)
        self.assertEqual(qq_gui.scrim_alpha_for_blur(1.5), 0.85)  # 超范围夹回

    def test_no_scrim_until_user_touches_slider(self):
        self.assertEqual(qq_gui.image_scrim_alpha(0.70, False), 0.0)
        self.assertEqual(qq_gui.image_scrim_alpha(1.0, False), 0.0)

    def test_scrim_returns_after_user_drag(self):
        self.assertAlmostEqual(qq_gui.image_scrim_alpha(0.70, True), 0.595, places=6)
        self.assertAlmostEqual(qq_gui.image_scrim_alpha(1.0, True), 0.85, places=6)
        self.assertEqual(qq_gui.image_scrim_alpha(0.70, True, has_image=False), 0.0)


class FramePaintTests(unittest.TestCase):
    def _center(self, frame):
        frame.resize(60, 40)
        frame.show()
        APP.processEvents()
        color = frame.grab().toImage().pixelColor(30, 20)
        frame.close()
        return (color.red(), color.green(), color.blue())

    def test_image_without_explicit_blur_shows_raw_picture(self):
        frame = qq_gui.BackgroundFrame()
        frame.set_background_pixmap(solid_pixmap())
        frame.set_blur(0.70)          # 老调用点：不改"是否显式设置过"
        self.assertEqual(self._center(frame), SOLID)

    def test_image_with_explicit_blur_is_whitened(self):
        frame = qq_gui.BackgroundFrame()
        frame.set_background_pixmap(solid_pixmap())
        frame.set_blur(1.0, True)
        got = self._center(frame)
        expected = tuple(int(c * 0.15 + 255 * 0.85) for c in SOLID)
        for channel, want in zip(got, expected):
            self.assertLessEqual(abs(channel - want), 2, (got, expected))

    def test_no_image_keeps_old_base_fill(self):
        frame = qq_gui.BackgroundFrame()
        frame.set_blur(0.70)
        base = qq_gui.overlay_color(qq_gui.BG, 0.70)
        self.assertEqual(self._center(frame), (base.red(), base.green(), base.blue()))
        self.assertEqual((base.red(), base.green(), base.blue()), (244, 246, 249))


class AppearanceTabTrackingTests(unittest.TestCase):
    def _tab(self, cfg):
        tab = qq_gui.AppearanceTab()
        tab.load(cfg)
        return tab

    def test_old_config_reload_does_not_count_as_user_edit(self):
        tab = self._tab({"blur": 0.70})   # 老配置：没有 blur_explicit
        self.assertFalse(tab._blur_explicit)
        self.assertEqual(tab.blur_slider.value(), 70)
        emitted = []
        tab.appearance_changed.connect(emitted.append)
        tab._apply()
        self.assertIs(emitted[-1]["blur_explicit"], False)
        self.assertAlmostEqual(emitted[-1]["blur"], 0.70)

    def test_user_drag_marks_explicit(self):
        tab = self._tab({"blur": 0.70})
        tab.blur_slider.setValue(30)
        self.assertTrue(tab._blur_explicit)
        self.assertEqual(tab.blur_val.text(), "30%")
        emitted = []
        tab.appearance_changed.connect(emitted.append)
        tab._apply()
        self.assertIs(emitted[-1]["blur_explicit"], True)
        self.assertAlmostEqual(emitted[-1]["blur"], 0.30)

    def test_flag_round_trips_through_config_file(self):
        from src.gui.config import load_gui_config, save_gui_config
        path = pathlib.Path(tempfile.mkdtemp()) / "gui_config.json"
        save_gui_config(path, {"blur": 0.70, "blur_explicit": True, "background_image": "x.png"})
        tab = self._tab(load_gui_config(path))
        self.assertTrue(tab._blur_explicit)
        self.assertEqual(tab.blur_slider.value(), 70)

    def test_apply_appearance_persists_blur_explicit(self):
        import types
        from src.gui.config import load_gui_config

        class StubWindow:
            def __init__(self, cfg_path):
                self._cfg_path = cfg_path
                self._cfg = {"blur": 0.70}
                self.console_tab = types.SimpleNamespace(set_notice=lambda *a, **k: None)
                self.applied = {}

            def setWindowOpacity(self, value):
                pass

            def _sync_frame_margin(self):
                pass

            def _apply_bg(self, color, image="", blur=None, transparency=None,
                          blur_explicit=None):
                self.applied = {"blur_explicit": blur_explicit, "blur": blur}

            def _apply_panel_transparency(self, value):
                pass

            def _apply_backdrop(self):
                pass

        path = pathlib.Path(tempfile.mkdtemp()) / "gui_config.json"
        stub = StubWindow(path)
        qq_gui.MainWindow._apply_appearance(stub, {"blur": 1.0, "blur_explicit": True})
        self.assertIs(stub.applied["blur_explicit"], True, "_apply_bg 必须收到显式标记")
        self.assertIs(load_gui_config(path)["blur_explicit"], True, "重启后要记得用户动过滑块")


if __name__ == "__main__":
    unittest.main()
