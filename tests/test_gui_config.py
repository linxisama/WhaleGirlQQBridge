import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from src.gui.config import DEFAULTS, load_gui_config, save_gui_config, sanitize

class GuiConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "gui_cfg.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sanitize_top_level_not_dict(self):
        self.assertEqual(sanitize([1, 2]), DEFAULTS)
        self.assertEqual(sanitize(None), DEFAULTS)

    def test_sanitize_opacity(self):
        self.assertEqual(sanitize({"opacity": "abc"})["opacity"], DEFAULTS["opacity"])
        self.assertEqual(sanitize({"opacity": 0.2})["opacity"], DEFAULTS["opacity"])
        self.assertEqual(sanitize({"opacity": 1.5})["opacity"], DEFAULTS["opacity"])
        self.assertEqual(sanitize({"opacity": 0.3})["opacity"], 0.3)
        self.assertEqual(sanitize({"opacity": 1.0})["opacity"], 1.0)

    def test_sanitize_bg_color(self):
        for bad in ("#123", "red", "#GGGGGG", ""):
            self.assertEqual(sanitize({"bg_color": bad})["bg_color"], DEFAULTS["bg_color"])
        self.assertEqual(sanitize({"bg_color": "#FF0000"})["bg_color"], "#FF0000")

    def test_sanitize_geometry(self):
        self.assertEqual(sanitize({"geometry": "x"})["geometry"], {})
        self.assertEqual(sanitize({"geometry": {"w": "1"}})["geometry"], {})
        self.assertEqual(
            sanitize({"geometry": {"w": 100, "h": 200, "x": 0, "y": 0}})["geometry"],
            {"w": 100, "h": 200, "x": 0, "y": 0},
        )
        self.assertEqual(
            sanitize({"geometry": {"w": 100, "h": 200, "x": 3.5, "y": 1}})["geometry"],
            {"w": 100, "h": 200, "y": 1},
        )

    def test_sanitize_background_image(self):
        for bad in (123, None, ["a"]):
            self.assertEqual(sanitize({"background_image": bad})["background_image"], "")
        self.assertEqual(sanitize({"background_image": "x.png"})["background_image"], "x.png")

    def test_sanitize_unknown_fields_kept(self):
        cfg = {"opacity": 0.5, "bg_color": "#000000", "geometry": {}, "background_image": "", "custom_key": 42}
        out = sanitize(cfg)
        self.assertEqual(out["custom_key"], 42)
        self.assertEqual(out["opacity"], 0.5)

    def test_sanitize_corrupt_known_fields_independent(self):
        cfg = {"opacity": 99, "bg_color": "bad", "geometry": "bad", "background_image": 1, "custom_key": "kept"}
        out = sanitize(cfg)
        self.assertEqual(out["opacity"], DEFAULTS["opacity"])
        self.assertEqual(out["bg_color"], DEFAULTS["bg_color"])
        self.assertEqual(out["geometry"], {})
        self.assertEqual(out["background_image"], "")
        self.assertEqual(out["custom_key"], "kept")

    def test_sanitize_returns_deep_copy(self):
        out1 = sanitize(None)
        out1["geometry"]["w"] = 999
        out2 = sanitize(None)
        self.assertNotIn("w", out2["geometry"])

    def test_load_missing_file(self):
        out = load_gui_config(self.path)
        self.assertEqual(out, DEFAULTS)

    def test_load_empty_file(self):
        open(self.path, "w").close()
        out = load_gui_config(self.path)
        self.assertEqual(out, DEFAULTS)

    def test_load_bad_json(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{invalid}")
        out = load_gui_config(self.path)
        self.assertEqual(out, DEFAULTS)

    def test_load_null_or_list(self):
        for text in ("null", "[1]"):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
            self.assertEqual(load_gui_config(self.path), DEFAULTS)

    def test_save_and_load_roundtrip(self):
        cfg = {
            "opacity": 0.9,
            "bg_color": "#123456",
            "geometry": {"w": 1, "h": 2, "x": 3, "y": 4},
            "background_image": "",
            "custom_key": "hi",
        }
        save_gui_config(self.path, cfg)
        loaded = load_gui_config(self.path)
        # 外观改版后 opacity 只是旧字段的兼容副本：写盘时按 1 - transparency 重算。
        # 传进来的 opacity=0.9 属于旧写法，会先被 migrate 换算成 transparency=0.1，
        # 所以这里的 opacity 回来仍然是 0.9（1 - 0.1），transparency 是 0.1。
        self.assertEqual(loaded["bg_color"], "#123456")
        self.assertEqual(loaded["geometry"], {"w": 1, "h": 2, "x": 3, "y": 4})
        self.assertEqual(loaded["background_image"], "")
        self.assertEqual(loaded["custom_key"], "hi")
        self.assertAlmostEqual(loaded["transparency"], 0.1, places=4)
        self.assertAlmostEqual(loaded["opacity"], 0.9, places=4)

    def test_save_with_transparency_keeps_opacity_in_sync(self):
        save_gui_config(self.path, {"transparency": 0.25, "blur": 0.7})
        loaded = load_gui_config(self.path)
        self.assertAlmostEqual(loaded["transparency"], 0.25, places=4)
        self.assertAlmostEqual(loaded["opacity"], 0.75, places=4)

    def test_save_atomic_success_no_tmp_left_and_lf(self):
        # 传副本：save 不该就地改调用方的字典（曾经会改，导致 DEFAULTS 被污染）
        save_gui_config(self.path, dict(DEFAULTS))
        with open(self.path, "rb") as f:
            data = f.read()
        self.assertNotIn(b"\r", data)
        content = data.decode("utf-8")
        self.assertTrue(content.endswith("\n"))
        self.assertEqual(
            sorted(os.listdir(self.tmp.name)),
            ["gui_cfg.json"],
        )

    def test_save_failure_keeps_original_and_raises(self):
        save_gui_config(self.path, {"opacity": 0.9})
        original = open(self.path, "rb").read()
        with mock.patch("src.gui.config.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                save_gui_config(self.path, {"opacity": 0.3})
        self.assertEqual(open(self.path, "rb").read(), original)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["gui_cfg.json"])

if __name__ == "__main__":
    unittest.main()
