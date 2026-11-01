"""模型选择对齐微信端（可输入 + 只读下拉），以及模型参数对齐微信端那套。

背景（用户 2026-10-17）：微信端控制台的模型是「输入框 + 下拉挑选」两件套，
QQ 端原来是一个可编辑下拉兼任两件事（下拉项和输入内容耦合）；
而且 QQ 端的模型参数明显偏紧（max_tokens=60、超时 10 秒），也是"回复半句话/超时"的来源。
"""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLineEdit  # noqa: E402

APP = QApplication.instance() or QApplication([])

SETTINGS = {"mode": "official", "api_url": "https://example.test/v1/chat/completions",
            "model": "TM ·Auto", "api_style": "auto", "groups": [], "listen_private": True,
            "billing_mode": "per_call"}


class ModelPickerTests(unittest.TestCase):
    def tab(self):
        import qq_gui
        from src.gui import diagnostics

        diagnostics.install(__import__("pathlib").Path(tempfile.mkdtemp()),
                            lambda title, message: None)
        tab = qq_gui.SettingsTab()
        tab.populate(dict(SETTINGS))
        return tab

    def test_model_name_is_a_text_field(self):
        tab = self.tab()
        self.assertIsInstance(tab.model_name, QLineEdit,
                              "模型名称要是输入框（和微信端一致），下拉只负责挑")
        self.assertEqual(tab.model_name.text(), "TM ·Auto", "回填已保存的模型")

    def test_fill_models_shows_picker_and_keeps_current(self):
        tab = self.tab()
        count = tab.fill_models(["TM ·Auto", "TM ·Fast", "TM ·Deep"])
        self.assertEqual(count, 3)
        self.assertFalse(tab.model_pick.isHidden(), "取到列表后下拉要显示出来")
        self.assertEqual(tab.model_pick.currentText(), "TM ·Auto", "当前模型应当在列表中被选中")
        self.assertEqual(tab.model_name.text(), "TM ·Auto", "不要擅自改用户已填的模型")

    def test_picking_writes_into_text_field(self):
        tab = self.tab()
        tab.fill_models(["TM ·Auto", "TM ·Fast", "TM ·Deep"])
        tab.model_pick.setCurrentIndex(2)
        APP.processEvents()
        self.assertEqual(tab.model_name.text(), "TM ·Deep")
        self.assertEqual(tab.collect()["model"], "TM ·Deep", "保存时取输入框的值")

    def test_manual_text_is_saved(self):
        tab = self.tab()
        tab.model_name.setText("自定义-model")
        self.assertEqual(tab.collect()["model"], "自定义-model",
                         "手填的 model id 不能被下拉列表覆盖")


class WeChatAlignedParamsTests(unittest.TestCase):
    """QQ 端模型参数应当跟微信端那套对齐（微信端实测可用）。"""

    def test_defaults_are_not_tiny(self):
        from src.config_manager import DEFAULT_CONFIG

        ai = DEFAULT_CONFIG["ai"]
        self.assertGreaterEqual(int(ai["max_tokens"]), 200,
                                "默认输出上限太小会让回复只有半句话")

    def test_billing_mode_default_and_both_supported(self):
        from src.config_manager import DEFAULT_CONFIG

        self.assertEqual(DEFAULT_CONFIG["ai"]["billing_mode"], "per_token")
        source = open("qq_gui.py", encoding="utf-8").read()
        self.assertIn('"per_call"', source)
        self.assertIn("计费方式", source)


if __name__ == "__main__":
    unittest.main()
