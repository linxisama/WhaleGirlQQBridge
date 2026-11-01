"""透明度拉满 → 窗口看不见；重装也修不好（真机 2026-10-17）。

用户原话："现在这个透明度拉满了重装也没有办法解决"。

根因两条，缺一不可：
1. `window_opacity_from_transparency(1.0)` = `1.0 - 1.0` = **0.0** —— 滑块拉到 100% 时
   窗口真的全透明，等于把窗口关掉了（看不见也点不到）；
2. 这个值存在 `%LOCALAPPDATA%\\WhaleGirlQQ\\data\\gui_config.json`，
   **重装不覆盖用户数据**，所以"重装也没用"。

修法：不透明度加下限 0.35、滑块上限 80%、读取旧配置时夹回、
并给一条不依赖界面的自救通道 `WhaleGirlQQ.exe --reset-ui`。
"""
import json
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.desktop_runtime import APPEARANCE_KEYS, reset_appearance  # noqa: E402

APP = QApplication.instance() or QApplication([])


class OpacityFloorTests(unittest.TestCase):
    def test_max_slider_never_makes_window_invisible(self):
        import qq_gui

        self.assertGreaterEqual(qq_gui.MIN_WINDOW_OPACITY, 0.3,
                                "不透明度下限太低就等于把窗口关掉")
        for percent in (0, 20, 50, 80, 100):
            opacity = qq_gui.window_opacity_from_transparency(percent / 100)
            self.assertGreaterEqual(opacity, qq_gui.MIN_WINDOW_OPACITY,
                                    f"{percent}% 时窗口不能低于下限")
            self.assertLessEqual(opacity, 1.0)

    def test_slider_range_is_capped(self):
        import qq_gui

        tab = qq_gui.AppearanceTab()
        self.assertEqual(tab.transparency_slider.maximum(), qq_gui.MAX_TRANSPARENCY_PERCENT)
        self.assertLessEqual(qq_gui.MAX_TRANSPARENCY_PERCENT, 80,
                             "滑到最大也不能淡到看不见")

    def test_legacy_full_transparency_is_clamped_on_load(self):
        import qq_gui

        tab = qq_gui.AppearanceTab()
        tab.load({"transparency": 1.0, "panel_transparency": 0.7, "blur": 0.7,
                  "bg_color": "#F4F6F9"})
        self.assertEqual(tab.transparency_slider.value(), qq_gui.MAX_TRANSPARENCY_PERCENT,
                         "旧的 100% 配置要在读取时夹回上限")
        opacity = qq_gui.window_opacity_from_transparency(tab.transparency_slider.value() / 100)
        self.assertGreaterEqual(opacity, qq_gui.MIN_WINDOW_OPACITY)


class ResetAppearanceTests(unittest.TestCase):
    def test_reset_only_touches_appearance_keys(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = pathlib.Path(tmp.name) / "gui_config.json"
        target.write_text(json.dumps({
            "transparency": 1.0, "panel_transparency": 0.72, "blur": 0.7,
            "bg_color": "#F4F6F9", "background_image": "data/x.png",
            "geometry": {"w": 1334, "h": 730}, "config_version": 1,
        }), encoding="utf-8")
        removed = reset_appearance(target)
        remaining = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(set(removed), set(APPEARANCE_KEYS) - {"safe_mode"})
        self.assertEqual(remaining, {"config_version": 1},
                         "只清外观键，其它设置与原文件结构不动")

    def test_reset_is_safe_when_file_missing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(reset_appearance(pathlib.Path(tmp.name) / "nope.json"), [])

    def test_launcher_exposes_reset_ui_flag(self):
        source = open("gui_launcher.py", encoding="utf-8").read()
        self.assertIn("--reset-ui", source, "要有一条不依赖界面的自救通道")
        self.assertIn("reset_appearance", source)


if __name__ == "__main__":
    unittest.main()
