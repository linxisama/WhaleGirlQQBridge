"""文件对话框语言/主题 与 Qt 中文翻译的回归测试。

真机问题（用户截图，1.6.9）：安全模式下 `AA_DontUseNativeDialogs` 让所有文件对话框
退回 Qt 自绘版，而 Qt 的 zh_CN 翻译没加载、颜色跟随系统 → 弹出**英文 + 深色**的
「选择背景图片」窗口，和中文浅色界面完全不一致。

修法（2026-10 再次改写）：**常规路径永远用系统对话框**——安全模式也不再退回自绘，
因为自绘那套用户根本点不动（列表行没有系统选择行为、没有快速访问栏、没有新建文件夹）。
自绘只剩两处排查入口：`force_light=True` 与 `WHALEGIRL_NATIVE_DIALOGS=0`，
那时仍必须加载 zh_CN 翻译 + 套浅色配色（`src/gui/qt_locale.py` + `src/gui/file_dialogs.py`）。
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog  # noqa: E402

from src.gui import dialogs, file_dialogs  # noqa: E402
from src.gui.qt_locale import (candidate_dirs, ensure_chinese, find_translation,  # noqa: E402
                               light_palette, should_use_native_dialogs,
                               translation_status)

APP = QApplication.instance() or QApplication([])
# 装上 Qt 中文翻译 + 兜底翻译：下面关于文件对话框标签、标准按钮的断言都依赖它，
# 真实程序在 main() 里也是这么装的（顺序与真实启动一致）。
ensure_chinese(APP)


class NativeDialogPolicyTests(unittest.TestCase):
    def test_native_always_by_default_including_safe_mode(self):
        self.assertTrue(should_use_native_dialogs(False), "普通模式应当用系统对话框")
        # 用户真机反馈：安全模式下弹出的自绘文件框点不动、认不出，所以安全模式也用原生。
        self.assertTrue(should_use_native_dialogs(True), "安全模式也用系统对话框")

    def test_env_can_force_either_way(self):
        self.assertTrue(should_use_native_dialogs(True, {"WHALEGIRL_NATIVE_DIALOGS": "1"}))
        self.assertFalse(should_use_native_dialogs(False, {"WHALEGIRL_NATIVE_DIALOGS": "0"}))

    def test_empty_env_keeps_the_default(self):
        self.assertTrue(should_use_native_dialogs(False, {}))
        self.assertTrue(should_use_native_dialogs(True, {"WHALEGIRL_NATIVE_DIALOGS": " "}))
        self.assertTrue(should_use_native_dialogs(False, {"WHALEGIRL_NATIVE_DIALOGS": "on"}))
        self.assertFalse(should_use_native_dialogs(False, {"WHALEGIRL_NATIVE_DIALOGS": "off"}))


class TranslationTests(unittest.TestCase):
    def test_translation_file_is_found_in_the_environment(self):
        found = find_translation()
        self.assertIsNotNone(found, "开发环境里应当能找到 qtbase_zh_CN.qm")
        self.assertEqual(found.name, "qtbase_zh_CN.qm")

    def test_ensure_chinese_installs_the_translator(self):
        status = ensure_chinese(APP)
        self.assertTrue(status.get("loaded"), status)
        self.assertTrue(str(status.get("path", "")).endswith("qtbase_zh_CN.qm"))
        self.assertTrue(getattr(APP, "_whalegirl_translator", None) is not None,
                        "翻译器要挂在 app 上，避免被回收")
        self.assertTrue(translation_status().get("loaded"))

    def test_missing_file_reports_a_reason_instead_of_raising(self):
        import src.gui.qt_locale as module
        saved = module._loaded
        module._loaded = {}
        try:
            with mock.patch.object(module, "candidate_dirs", return_value=[]):
                status = module.ensure_chinese(APP)
        finally:
            module._loaded = saved
        self.assertFalse(status["loaded"])
        self.assertIn("没找到", status["reason"])

    def test_candidate_dirs_are_unique(self):
        dirs = [str(item) for item in candidate_dirs()]
        self.assertEqual(len(dirs), len(set(dirs)), "候选目录不该重复")

    def test_light_palette_is_the_app_light_colour(self):
        from PyQt6.QtGui import QPalette
        palette = light_palette()
        self.assertEqual(palette.color(QPalette.ColorRole.Window).name(), "#f4f6f9")
        self.assertEqual(palette.color(QPalette.ColorRole.WindowText).name(), "#26364b")


class FileDialogRoutingTests(unittest.TestCase):
    """系统对话框可用时走原生；不可用时走"自绘 + 强制浅色/中文"。"""

    def setUp(self):
        self.parent = None

    def test_native_path_calls_the_platform_dialog(self):
        with mock.patch.object(file_dialogs, "native_dialogs_enabled", return_value=True), \
             mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=("C:/a.png", "图片")) as native, \
             mock.patch.object(QFileDialog, "getOpenFileNames",
                               return_value=(["C:/a.png", "C:/b.png"], "")) as native_many, \
             mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=("C:/p.yaml", "")) as native_save, \
             mock.patch.object(QFileDialog, "getExistingDirectory",
                               return_value="C:/plugins") as native_dir:
            self.assertEqual(file_dialogs.open_file(self.parent, "标题", "", "图片 (*.png)", force_light=False),
                             ("C:/a.png", "图片"))
            self.assertEqual(file_dialogs.open_files(self.parent, "标题", "", "", force_light=False),
                             (["C:/a.png", "C:/b.png"], ""))
            self.assertEqual(file_dialogs.save_file(self.parent, "标题", "", "", force_light=False),
                             ("C:/p.yaml", ""))
            self.assertEqual(file_dialogs.choose_dir(self.parent, "标题", "", force_light=False), "C:/plugins")
        native.assert_called_once()
        native_many.assert_called_once()
        native_save.assert_called_once()
        native_dir.assert_called_once()

    def test_file_dialogs_default_to_native(self):
        with mock.patch.object(file_dialogs, "native_dialogs_enabled", return_value=True), \
             mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=("C:/default.png", "")) as native:
            self.assertEqual(file_dialogs.open_file(self.parent, "标题", "", ""),
                             ("C:/default.png", ""))
        native.assert_called_once()

    def test_message_dialogs_use_the_light_custom_body(self):
        with mock.patch.object(dialogs._MessageDialog, "exec",
                               return_value=QDialog.DialogCode.Accepted) as shown:
            dialogs.tell(None, "已启动", "qqbridge 已启动，OneBot 连接在线。")
        shown.assert_called_once()
        box = dialogs._MessageDialog(
            None, "已启动", "qqbridge 已启动，OneBot 连接在线。", kind="info")
        stylesheet = box.styleSheet()
        self.assertIn("#F4F6F9", stylesheet, "提示框必须是浅色，不能跟随系统深色")
        self.assertIn("#1F2733", stylesheet, "正文必须有深色文字")
        box.deleteLater()

    def test_styled_path_forces_non_native_and_light_palette(self):
        seen = {}

        class FakeDialog:
            # 假对话框要把枚举暴露出来：模块里的 _styled() 会用 QFileDialog.Option.*
            Option = QFileDialog.Option
            FileMode = QFileDialog.FileMode
            AcceptMode = QFileDialog.AcceptMode
            DialogLabel = QFileDialog.DialogLabel

            def __init__(self, *args, **kwargs):
                seen["args"] = args[1:]

            def setLabelText(self, label, text):
                seen.setdefault("labels", []).append((label, text))

            def setPalette(self, palette):
                seen["palette"] = palette

            def setStyleSheet(self, stylesheet):
                seen['stylesheet'] = stylesheet

            def setOption(self, option, value):
                seen.setdefault("options", []).append((option, value))

            def setFileMode(self, mode):
                seen["mode"] = mode

            def setAcceptMode(self, mode):
                seen["accept"] = mode

            def exec(self):
                return 1

            def selectedFiles(self):
                return ["C:/picked.png"]

            def selectedNameFilter(self):
                return "图片"

        with mock.patch.object(file_dialogs, "native_dialogs_enabled", return_value=False), \
             mock.patch.object(file_dialogs, "QFileDialog", FakeDialog):
            path, chosen = file_dialogs.open_file(self.parent, "选择背景图片", "", "图片 (*.png)")

        self.assertEqual(path, "C:/picked.png")
        self.assertEqual(chosen, "图片")
        self.assertIn((QFileDialog.Option.DontUseNativeDialog, True), seen["options"])
        self.assertEqual(seen["palette"].color(
            __import__("PyQt6.QtGui", fromlist=["QPalette"]).QPalette.ColorRole.Window).name(),
            "#f4f6f9", "自绘对话框必须是浅色（否则跟随系统深色，和界面不一致）")
        self.assertEqual(seen["mode"], QFileDialog.FileMode.ExistingFile)

    def test_cancel_returns_empty_everywhere(self):
        class CancelledDialog:
            Option = QFileDialog.Option
            FileMode = QFileDialog.FileMode
            AcceptMode = QFileDialog.AcceptMode
            DialogLabel = QFileDialog.DialogLabel

            def __init__(self, *args, **kwargs):
                pass

            def setLabelText(self, *_):
                pass

            def setPalette(self, *_):
                pass

            def setStyleSheet(self, *_):
                pass

            def setOption(self, *_):
                pass

            def setFileMode(self, *_):
                pass

            def setAcceptMode(self, *_):
                pass

            def exec(self):
                return 0

        with mock.patch.object(file_dialogs, "native_dialogs_enabled", return_value=False), \
             mock.patch.object(file_dialogs, "QFileDialog", CancelledDialog):
            self.assertEqual(file_dialogs.open_file(self.parent, "t"), ("", ""))
            self.assertEqual(file_dialogs.open_files(self.parent, "t"), ([], ""))
            self.assertEqual(file_dialogs.save_file(self.parent, "t"), ("", ""))
            self.assertEqual(file_dialogs.choose_dir(self.parent, "t"), "")


class StyledDialogLabelTests(unittest.TestCase):
    """自绘对话框的五个标签必须是中文。

    真机问题（用户截图，1.6.10 之后）：Qt 6.11 里标签键是 `&Look in:` / `Files of &type:`，
    而 qtbase_zh_CN.qm 里的键没有 `&` → 这两个标签回落英文，
    对话框变成"位置/文件类型"英文、其余中文的混合体。
    """

    def test_all_labels_are_chinese_for_each_accept_text(self):
        dialog = file_dialogs._styled(QFileDialog(None, "选择背景图片"), accept_text="打开")
        self.assertEqual(dialog.labelText(QFileDialog.DialogLabel.LookIn), "位置：")
        self.assertEqual(dialog.labelText(QFileDialog.DialogLabel.FileName), "文件名称：")
        self.assertEqual(dialog.labelText(QFileDialog.DialogLabel.FileType), "文件类型：")
        self.assertEqual(dialog.labelText(QFileDialog.DialogLabel.Accept), "打开")
        self.assertEqual(dialog.labelText(QFileDialog.DialogLabel.Reject), "取消")

    def test_save_and_directory_use_their_own_accept_text(self):
        saving = file_dialogs._styled(QFileDialog(None, "保存人设到文件"), accept_text="保存")
        self.assertEqual(saving.labelText(QFileDialog.DialogLabel.Accept), "保存")
        folder = file_dialogs._styled(QFileDialog(None, "选择插件使用的文件夹"),
                                      accept_text="选择文件夹")
        self.assertEqual(folder.labelText(QFileDialog.DialogLabel.Accept), "选择文件夹")

    def test_real_calls_use_chinese_accept_text(self):
        seen = []

        class SpyDialog:
            Option = QFileDialog.Option
            FileMode = QFileDialog.FileMode
            AcceptMode = QFileDialog.AcceptMode
            DialogLabel = QFileDialog.DialogLabel

            def __init__(self, *args, **kwargs):
                pass

            def setPalette(self, *_):
                pass

            def setStyleSheet(self, *_):
                pass

            def setOption(self, *_):
                pass

            def setFileMode(self, *_):
                pass

            def setAcceptMode(self, *_):
                pass

            def setLabelText(self, label, text):
                seen.append((str(label).split('.')[-1], text))

            def exec(self):
                return 0

        with mock.patch.object(file_dialogs, "native_dialogs_enabled", return_value=False), \
             mock.patch.object(file_dialogs, "QFileDialog", SpyDialog):
            file_dialogs.save_file(None, "t")
            file_dialogs.choose_dir(None, "t")
        accepts = [text for name, text in seen if name == "Accept"]
        self.assertIn("保存", accepts)
        self.assertIn("选择文件夹", accepts)
        self.assertTrue(all(text == "取消" for name, text in seen if name == "Reject"))

    def test_fallback_translator_fills_the_ampersand_gap(self):
        # 兜底翻译器只覆盖文件对话框上下文，且能补上 Qt 自带翻译对不上的带 & 键
        self.assertTrue(getattr(APP, "_whalegirl_fallback", None) is not None)
        self.assertEqual(APP.translate("QFileDialog", "&Look in:"), "位置：")
        self.assertEqual(APP.translate("QFileDialog", "Files of &type:"), "文件类型：")
        self.assertEqual(APP.translate("QFileDialog", "Back"), "后退")
        # 别的上下文不抢译文：拿不到翻译时 Qt 原样返回源文本
        self.assertEqual(APP.translate("SomeOtherContext", "&Look in:"), "&Look in:")

    def test_fallback_does_not_block_the_official_translations(self):
        """兜底翻译器未命中时必须让官方译文继续生效。

        踩过的坑：`translate()` 里返回 `""` 会造出一个"空但非 null"的 QString，
        Qt 的查找循环按 `!result.isNull()` 判定 → 直接采信并停止，
        于是 QMessageBox 的"确定/取消"、Qt 自带译文全部失效（实测）。
        正确做法是返回 None（null QString）。
        """
        from PyQt6.QtWidgets import QMessageBox
        self.assertEqual(APP.translate("QPlatformTheme", "OK"), "确定")
        self.assertEqual(APP.translate("QFileDialog", "File &name:"), "文件名称：")
        box = QMessageBox()
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        self.assertEqual({button.text() for button in box.buttons()}, {"确定", "取消"})

    def test_standard_buttons_are_chinese_too(self):
        """报错弹窗的按钮也应当是中文（用户之前的截图里是英文 OK）。"""
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox()
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        texts = {button.text() for button in box.buttons()}
        self.assertIn("确定", texts)
        self.assertIn("取消", texts)


class SourceGuardsTests(unittest.TestCase):
    """钉住"不再无条件禁用系统对话框"这件事（这次真机问题的根因）。"""

    def test_main_prefers_native_dialogs_with_config_override(self):
        """策略：默认用系统原生文件对话框；native_file_dialogs=false 才退回自绘。"""
        import inspect
        import qq_gui
        source = inspect.getsource(qq_gui.main)
        self.assertIn("AA_DontUseNativeDialogs", source)
        self.assertIn("not use_native", source)
        self.assertIn("native_file_dialogs", source)

    def test_all_file_dialog_calls_go_through_the_wrapper(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        offenders = []
        for name in ("qq_gui.py", "src/gui/stickers.py", "src/gui/conversation.py"):
            text = (root / name).read_text(encoding="utf-8")
            for match in re.finditer(r"QFileDialog\.(get\w+)", text):
                offenders.append(f"{name}: {match.group(0)}")
        self.assertEqual(offenders, [], f"这些调用点应当改走 file_dialogs：{offenders}")


if __name__ == "__main__":
    unittest.main()
