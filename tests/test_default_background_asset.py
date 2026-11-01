# -*- coding: utf-8 -*-
"""默认背景资产与「恢复内置背景」按钮下线后的回归（W10-背景）。

用户裁决：删掉外观页的「恢复 Mio 浅色」按钮，默认背景换成 assets/backgrounds/default.jpg。
这三件事都必须有断言盯着，否则将来换资产容易漏掉其中一环。
"""
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.gui import appearance

ROOT = Path(__file__).resolve().parent.parent
ASSET = ROOT.joinpath(*appearance.DEFAULT_BACKGROUND)


class DefaultBackgroundTests(unittest.TestCase):
    def test_default_asset_exists_and_is_an_allowed_image(self):
        self.assertTrue(ASSET.is_file(), f"默认背景资产不存在：{ASSET}")
        self.assertIn(ASSET.suffix.lower().lstrip("."), appearance.ALLOWED_EXTS)
        self.assertGreater(ASSET.stat().st_size, 10_000, "默认背景太小，可能被换成了占位图")

    def test_default_asset_is_loadable_by_qt(self):
        from PyQt6.QtGui import QGuiApplication, QImage
        app = QGuiApplication.instance() or QGuiApplication([])
        image = QImage(str(ASSET))
        self.assertFalse(image.isNull())
        self.assertGreaterEqual(image.width(), 1280)
        self.assertGreaterEqual(image.height(), 720)
        self.assertIsNotNone(app)

    def test_ensure_default_background_copies_into_user_dir(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        cfg = {"background_image": ""}
        appearance.ensure_default_background(cfg, root, bundle=ROOT)
        rel = cfg.get("background_image") or ""
        self.assertTrue(rel.startswith("data/backgrounds/"), rel)
        resolved = appearance.resolve_background(rel, root)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.read_bytes(), ASSET.read_bytes())

    def test_custom_and_opt_out_are_not_overwritten(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        custom = {"background_image": "data/backgrounds/custom.png"}
        appearance.ensure_default_background(custom, root, bundle=ROOT)
        self.assertEqual(custom["background_image"], "data/backgrounds/custom.png")
        opt_out = {"background_image": "", "default_background": False}
        appearance.ensure_default_background(opt_out, root, bundle=ROOT)
        self.assertEqual(opt_out["background_image"], "")

    def test_restore_builtin_background_button_is_gone(self):
        source = (ROOT / "qq_gui.py").read_text("utf-8")
        self.assertIsNone(re.search(r"恢复\s*Mio", source), "「恢复 Mio 浅色」按钮应当已经删掉")
        self.assertNotIn("_restore_mio", source)
        self.assertNotIn("mio_light.png", source)
        self.assertNotIn("mio_light.png", (ROOT / "src" / "gui" / "appearance.py").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
