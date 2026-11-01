"""控制台窗口行为的离线回归测试（需要 PyQt6，无 PyQt6 时整类跳过）。

覆盖三件在真实使用中会坏、但纯模块测试测不到的事：
1. 不透明度必须压到 0.99 以内：Qt 在 opacity == 1.0 时把窗口当作完全不透明，
   会丢失 WA_TranslucentBackground 的逐像素透明，云母片背景随之失效。
2. 背景图按 cover 语义裁剪铺满：宽图上下裁、高图左右裁、比例不变形。
3. 外观设置三项（不透明度、叠加色、背景图）能写盘并原样读回。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from PyQt6.QtGui import QColor, QImage, QPainter
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app():
    # 必须持有模块级引用：QApplication 一旦被垃圾回收，之后构造任何 QWidget
    # 都会以 “Must construct a QApplication before a QWidget” 直接终止进程。
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


_APP = None


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class WindowBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        import qq_gui
        cls.gui = qq_gui

    def setUp(self):
        self.assertIsNotNone(_APP, "QApplication 必须存活到测试结束")
        from unittest.mock import patch
        backend=patch.object(self.gui.MainWindow,'_start_backend')
        backend.start();self.addCleanup(backend.stop)


    def test_transparency_maps_to_window_opacity(self):
        convert = self.gui.window_opacity_from_transparency
        # 0% 透明 = 完全不透明。注意不能给 Qt 1.0：那会丢掉逐像素透明，云母片背板消失。
        self.assertEqual(convert(0.0), 0.999)
        self.assertAlmostEqual(convert(0.30), 0.70, places=4)
        # 100% 透明不再等于 0（那是窗口完全看不见，而且值存在 gui_config 里、
        # 重装也不覆盖用户数据 —— 用户踩过这个坑）。改为夹在下限。
        self.assertAlmostEqual(convert(1.0), self.gui.MIN_WINDOW_OPACITY, places=4)
        # 越界与脏值都被夹住
        self.assertEqual(convert(-1), 0.999)
        self.assertEqual(convert(5), self.gui.MIN_WINDOW_OPACITY)
        self.assertEqual(convert("abc"), 0.999)
        self.assertEqual(convert(None), 0.999)

    def test_backdrop_follows_blur(self):
        pick = self.gui.backdrop_for_blur
        self.assertEqual(pick(0.0), self.gui.BACKDROP_NONE)      # 0% 模糊 = 不要云母片
        self.assertEqual(pick(0.2), self.gui.BACKDROP_ACRYLIC)
        self.assertEqual(pick(0.7), self.gui.BACKDROP_MICA)      # 默认 70%
        self.assertEqual(pick(1.0), self.gui.BACKDROP_MICA)
        self.assertEqual(pick(0.7, available=False), self.gui.BACKDROP_NONE)

    def test_scrim_alpha_grows_with_blur(self):
        alpha = self.gui.scrim_alpha_for_blur
        self.assertEqual(alpha(0.0), 0.0)
        self.assertLess(alpha(0.3), alpha(0.7))
        self.assertLessEqual(alpha(1.0), 1.0)

    def _render_center_and_corner(self, image_w, image_h, frame_w, frame_h, blur=0.0,
                                  explicit=None):
        frame = self.gui.BackgroundFrame()
        frame.resize(frame_w, frame_h)
        # explicit=True 模拟"用户亲手拖过模糊度滑块"，None 表示老调用点（不改这个状态）
        frame.set_blur(blur, explicit)
        image = QImage(image_w, image_h, QImage.Format.Format_RGB32)
        image.fill(QColor("#3366cc"))
        from PyQt6.QtGui import QPixmap
        frame.set_background_pixmap(QPixmap.fromImage(image))
        canvas = QImage(frame_w, frame_h, QImage.Format.Format_RGB32)
        canvas.fill(QColor("#000000"))
        painter = QPainter(canvas)
        frame.render(painter)
        painter.end()
        center = QColor(canvas.pixel(frame_w // 2, frame_h // 2))
        corner = QColor(canvas.pixel(1, 1))
        return center, corner

    def test_wide_image_is_cropped_vertically_and_fills_width(self):
        center, corner = self._render_center_and_corner(400, 100, 200, 200)
        self.assertEqual(center.name(), "#3366cc")
        self.assertEqual(corner.name(), "#3366cc")  # 铺满，无黑边

    def test_tall_image_is_cropped_horizontally_and_fills_height(self):
        center, corner = self._render_center_and_corner(100, 400, 200, 200)
        self.assertEqual(center.name(), "#3366cc")
        self.assertEqual(corner.name(), "#3366cc")

    def test_blur_lights_up_the_image_only_after_the_user_drags_the_slider(self):
        # 语义见 image_scrim_alpha：没显式设过模糊度（默认、老配置）时白蒙层 alpha=0，
        # 背景图按原样显示；只有用户亲手拖过滑块才按 blur*0.85 叠。两种情形都必须测。
        sharp, _ = self._render_center_and_corner(400, 100, 200, 200, blur=0.0)
        untouched, _ = self._render_center_and_corner(400, 100, 200, 200, blur=0.7)
        dragged, _ = self._render_center_and_corner(400, 100, 200, 200, blur=0.7,
                                                    explicit=True)
        self.assertEqual(sharp.name(), "#3366cc")
        # 情形一：没拖过滑块 —— 一个通道都不许变亮，图就是原图
        self.assertEqual(untouched.name(), sharp.name())
        # 情形二：拖过滑块 —— 三个通道都真的变亮，且亮到公式算出的那个值（±2 是取整误差）
        alpha = self.gui.image_scrim_alpha(0.7, True)
        self.assertGreater(alpha, 0.0)
        for got, base in ((dragged.red(), sharp.red()),
                          (dragged.green(), sharp.green()),
                          (dragged.blue(), sharp.blue())):
            self.assertGreater(got, base)
            self.assertLessEqual(abs(got - round(base * (1 - alpha) + 255 * alpha)), 2,
                                 (dragged.getRgb(), alpha))

    def test_color_is_visible_when_no_image(self):
        frame = self.gui.BackgroundFrame()
        frame.resize(80, 60)
        frame.set_background_color("#F4F6F9")
        frame.set_background_pixmap(None)
        frame.set_blur(0.0)
        canvas = QImage(80, 60, QImage.Format.Format_RGB32)
        canvas.fill(QColor("#000000"))
        painter = QPainter(canvas)
        frame.render(painter)
        painter.end()
        self.assertEqual(QColor(canvas.pixel(40, 30)).name(), "#f4f6f9")

    def test_blur_thickens_plain_color_even_when_opaque(self):
        # 0% 透明时窗口不透明，桌面/云母片都看不见，所以"模糊"必须体现在底色上
        def rendered(blur):
            frame = self.gui.BackgroundFrame()
            frame.resize(40, 30)
            frame.set_background_color("#808080")
            frame.set_blur(blur)
            canvas = QImage(40, 30, QImage.Format.Format_RGB32)
            canvas.fill(QColor("#000000"))
            painter = QPainter(canvas)
            frame.render(painter)
            painter.end()
            return QColor(canvas.pixel(20, 15))

        self.assertEqual(rendered(0.0).name(), "#808080")
        self.assertGreater(rendered(1.0).red(), 0x80)

    def test_broken_image_object_falls_back_to_color(self):
        frame = self.gui.BackgroundFrame()
        frame.resize(60, 40)
        frame.set_background_color("#F4F6F9")
        frame.set_blur(0.0)
        from PyQt6.QtGui import QPixmap
        frame.set_background_pixmap(QPixmap())  # null pixmap
        self.assertIsNone(frame._pixmap)
        canvas = QImage(60, 40, QImage.Format.Format_RGB32)
        canvas.fill(QColor("#000000"))
        painter = QPainter(canvas)
        frame.render(painter)
        painter.end()
        self.assertEqual(QColor(canvas.pixel(30, 20)).name(), "#f4f6f9")

    def test_fill_alpha_follows_transparency(self):
        # 容器填充不透明度 = 1 - 窗口透明度；否则调高透明度也看不穿桌面
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            original = self.gui.GUI_CFG
            self.gui.GUI_CFG = Path(tmp) / "gui_config.json"
            try:
                window = self.gui.MainWindow()
                window._apply_appearance({"transparency": 0.0, "blur": 0.7})
                self.assertAlmostEqual(window._container._alpha, 1.0, places=3)
                window._apply_appearance({"transparency": 0.6, "blur": 0.7})
                self.assertAlmostEqual(window._container._alpha, 0.4, places=3)
            finally:
                self.gui.GUI_CFG = original

    def test_appearance_dict_drives_config_and_widgets(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "gui_config.json"
            original = self.gui.GUI_CFG
            self.gui.GUI_CFG = cfg_path
            try:
                window = self.gui.MainWindow()
                window._apply_appearance({
                    "transparency": 0.25,
                    "blur": 0.70,
                    "bg_color": "#F4F6F9",
                    "background_image": "",
                })
                self.assertEqual(window._cfg["transparency"], 0.25)
                self.assertEqual(window._cfg["blur"], 0.70)
                # Qt 的窗口不透明度是 8 位量化（0.75 → 0.74902），所以按 2 位比较
                self.assertAlmostEqual(window.windowOpacity(), 0.75, places=2)
                self.assertEqual(window._container._blur, 0.70)
                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["transparency"], 0.25)
                self.assertEqual(saved["blur"], 0.70)
                self.assertEqual(saved["config_version"], 2)
                # 旧字段同步成 1 - transparency，兼容未升级的读取方
                self.assertAlmostEqual(saved["opacity"], 0.75, places=4)
            finally:
                self.gui.GUI_CFG = original


class AppearanceConfigRoundTripTests(unittest.TestCase):
    """外观三项的持久化：不依赖 PyQt6，任何环境都应运行。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "gui_config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_appearance_fields_round_trip(self):
        from src.gui.config import load_gui_config, save_gui_config
        save_gui_config(self.path, {
            "transparency": 0.55,
            "blur": 0.40,
            "bg_color": "#F4F6F9",
            "background_image": "data/backgrounds/abc.png",
        })
        loaded = load_gui_config(self.path)
        self.assertAlmostEqual(loaded["transparency"], 0.55, places=6)
        self.assertAlmostEqual(loaded["blur"], 0.40, places=6)
        self.assertEqual(loaded["bg_color"], "#F4F6F9")
        self.assertEqual(loaded["background_image"], "data/backgrounds/abc.png")

    def test_legacy_opacity_is_migrated_to_transparency(self):
        # 旧配置只有 opacity = 不透明度；迁移后 transparency = 1 - opacity，
        # 否则老用户的窗口会从"七成不透明"突然变成完全透明。
        from src.gui.config import load_gui_config
        self.path.write_text(json.dumps({"opacity": 0.70}), encoding="utf-8")
        loaded = load_gui_config(self.path)
        self.assertAlmostEqual(loaded["transparency"], 0.30, places=4)
        self.assertAlmostEqual(loaded["blur"], 0.70, places=4)  # 默认模糊度

    def test_legacy_migration_is_not_repeated(self):
        from src.gui.config import load_gui_config, save_gui_config
        save_gui_config(self.path, {"transparency": 0.10})
        loaded = load_gui_config(self.path)
        self.assertAlmostEqual(loaded["transparency"], 0.10, places=4)

    def test_blur_and_transparency_out_of_range_fall_back(self):
        from src.gui.config import DEFAULTS, load_gui_config
        self.path.write_text(json.dumps({"blur": 2.5, "transparency": -1}), encoding="utf-8")
        loaded = load_gui_config(self.path)
        self.assertEqual(loaded["blur"], DEFAULTS["blur"])
        self.assertEqual(loaded["transparency"], DEFAULTS["transparency"])

    def test_written_file_is_lf_only(self):
        from src.gui.config import save_gui_config
        save_gui_config(self.path, {"transparency": 0.0, "blur": 0.7})
        raw = self.path.read_bytes()
        self.assertNotIn(b"\r", raw)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["blur"], 0.7)
        self.assertEqual(data["transparency"], 0.0)

    def test_invalid_background_image_falls_back_to_empty(self):
        from src.gui.config import load_gui_config
        self.path.write_text(json.dumps({"background_image": 42}), encoding="utf-8")
        self.assertEqual(load_gui_config(self.path)["background_image"], "")


if __name__ == "__main__":
    unittest.main()
