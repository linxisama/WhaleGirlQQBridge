"""系统原生文件对话框策略的回归测试（2026-10，用户真机反馈后）。

真机问题：安全模式把 AA_DontUseNativeDialogs 打开，用户拿到 Qt 自绘的文件框——
列表行点不动、没有快速访问栏、没有新建文件夹和搜索框。用户原话
「文件的选择窗口不对劲，这个不能点击选择」，要的是资源管理器那一套。

钉住三件事：
1. should_use_native_dialogs 任何模式都返回 True（只有 WHALEGIRL_NATIVE_DIALOGS=0 例外）；
2. open_file/open_files/save_file/choose_dir 常规路径一律调系统静态方法，不碰 _styled；
3. 起始目录会记住上次选中的那个（src/gui/file_dialog_state.py）。
"""
import json
import os
import pathlib
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from src.gui import file_dialog_state, file_dialogs  # noqa: E402
from src.gui.qt_locale import should_use_native_dialogs  # noqa: E402

APP = QApplication.instance() or QApplication([])
ROOT = pathlib.Path(__file__).resolve().parents[1]


class AlwaysNativePolicyTests(unittest.TestCase):
    def test_safe_mode_no_longer_falls_back_to_styled(self):
        self.assertTrue(should_use_native_dialogs(True), "安全模式也必须用系统对话框")
        self.assertTrue(should_use_native_dialogs(False))

    def test_env_still_can_force_the_styled_fallback(self):
        self.assertFalse(should_use_native_dialogs(True, {"WHALEGIRL_NATIVE_DIALOGS": "0"}))
        self.assertFalse(should_use_native_dialogs(False, {"WHALEGIRL_NATIVE_DIALOGS": "off"}))
        self.assertTrue(should_use_native_dialogs(False, {"WHALEGIRL_NATIVE_DIALOGS": "1"}))
        # 空白值等于没设：仍然走原生
        self.assertTrue(should_use_native_dialogs(True, {"WHALEGIRL_NATIVE_DIALOGS": " "}))

    def test_policy_source_has_no_safe_mode_shortcut(self):
        text = (ROOT / "src" / "gui" / "qt_locale.py").read_text(encoding="utf-8")
        self.assertNotIn("return not safe_mode", text,
                         "安全模式不能再把文件对话框退回自绘")
        self.assertIn("WHALEGIRL_NATIVE_DIALOGS", text, "排查用的环境变量覆盖要留着")


class NativeRoutingTests(unittest.TestCase):
    """原生可用时四个入口都必须调系统静态方法，一条都不能落到自绘。"""

    def test_every_entry_goes_native(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("常规路径不允许走 _styled 自绘后备")

        with mock.patch.object(file_dialogs, "native_dialogs_enabled", return_value=True), \
             mock.patch.object(file_dialogs, "_styled", side_effect=boom), \
             mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=("C:/a.png", "图片")) as native, \
             mock.patch.object(QFileDialog, "getOpenFileNames",
                               return_value=(["C:/a.png"], "")) as native_many, \
             mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=("C:/p.yaml", "")) as native_save, \
             mock.patch.object(QFileDialog, "getExistingDirectory",
                               return_value="C:/plugins") as native_dir:
            self.assertEqual(file_dialogs.open_file(None, "选择背景图片", "C:/pics", "图片 (*.png)"),
                             ("C:/a.png", "图片"))
            self.assertEqual(file_dialogs.open_files(None, "t"), (["C:/a.png"], ""))
            self.assertEqual(file_dialogs.save_file(None, "t"), ("C:/p.yaml", ""))
            self.assertEqual(file_dialogs.choose_dir(None, "t"), "C:/plugins")
        native.assert_called_once()
        native_many.assert_called_once()
        native_save.assert_called_once()
        native_dir.assert_called_once()

    def test_force_light_still_uses_the_styled_fallback(self):
        """排查路径不能被删：_styled 仍会关掉原生并套浅色。"""
        dialog = file_dialogs._styled(QFileDialog(None, "选择背景图片"))
        self.assertTrue(dialog.testOption(QFileDialog.Option.DontUseNativeDialog))
        self.assertEqual(dialog.labelText(QFileDialog.DialogLabel.Accept), "打开")


class LastDirectoryTests(unittest.TestCase):
    """<data>/qq-only/file_dialog_last.json 的读写与静默回退。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = pathlib.Path(self._tmp.name) / "data"
        patcher = mock.patch.object(file_dialog_state, "_console_data_dir",
                                    return_value=self.data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_state_file_lives_under_the_data_dir(self):
        path = file_dialog_state.state_path()
        self.assertEqual(path, self.data / "qq-only" / "file_dialog_last.json")

    def test_remember_then_read_back_a_directory(self):
        chosen = pathlib.Path(self._tmp.name) / "pics"
        chosen.mkdir()
        self.assertTrue(file_dialog_state.remember(str(chosen)))
        self.assertEqual(file_dialog_state.last_directory("/fallback"), str(chosen))

    def test_remember_stores_the_parent_of_a_file(self):
        chosen = pathlib.Path(self._tmp.name) / "pics"
        chosen.mkdir()
        image = chosen / "a.png"
        image.write_bytes(b"png")
        self.assertTrue(file_dialog_state.remember(str(image)))
        self.assertEqual(file_dialog_state.last_directory(), str(chosen))

    def test_missing_state_falls_back_to_pictures(self):
        self.assertEqual(file_dialog_state.last_directory("C:/Users/x/Pictures"),
                         "C:/Users/x/Pictures")
        self.assertEqual(file_dialog_state.last_directory(), "")

    def test_corrupt_state_is_silent(self):
        path = file_dialog_state.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(file_dialog_state.last_directory("C:/fallback"), "C:/fallback")

    def test_stale_directory_is_ignored(self):
        path = file_dialog_state.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"directory": str(self.data / "gone")}), encoding="utf-8")
        self.assertEqual(file_dialog_state.last_directory("C:/fallback"), "C:/fallback")

    def test_unwritable_target_does_not_raise(self):
        self.assertFalse(file_dialog_state.remember(""))


class PickImageUsesLastDirectoryTests(unittest.TestCase):
    """qq_gui 的「选择背景图片」用上次目录，并在选完后记住。"""

    def test_pick_image_starts_from_the_remembered_directory(self):
        import qq_gui

        applied = []

        class _Tab:
            def _apply_background_file(self, path):
                applied.append(path)

        with mock.patch.object(qq_gui.file_dialog_state, "last_directory",
                               return_value="D:/pics") as remembered, \
             mock.patch.object(qq_gui.file_dialog_state, "remember",
                               return_value=True) as saved, \
             mock.patch.object(qq_gui.file_dialogs, "open_file",
                               return_value=("D:/pics/bg.png", "")) as picker:
            qq_gui.AppearanceTab._pick_image(_Tab())

        self.assertEqual(picker.call_args.args[2], "D:/pics", "起始目录要用上次选中的那个")
        self.assertIn("Pictures", remembered.call_args.args[0], "没记录时回退到 图片 目录")
        saved.assert_called_once_with("D:/pics/bg.png")
        self.assertEqual(applied, ["D:/pics/bg.png"])


if __name__ == "__main__":
    unittest.main()
