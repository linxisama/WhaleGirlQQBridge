"""设置页「获取模型列表 / 测试连接」两个按钮的离线回归测试（需要 PyQt6，缺库时跳过）。

只验证界面接线：请求发到哪个接口、参数是否用当前输入框的值、结果怎么显示、
失败时是否给中文提示。真正的网络探测在 tests/test_ai_endpoints.py 里用假 httpx 覆盖。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QThread
    HAVE_QT = True
except Exception:  # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = None


def _app():
    # 必须持有模块级引用：QApplication 一旦被回收，后面构造任何 QWidget 都会直接终止进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class AiProbeButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        import qq_gui
        cls.gui = qq_gui

    def setUp(self):
        backend = patch.object(self.gui.MainWindow, "_start_backend")
        backend.start()
        self.addCleanup(backend.stop)
        self.tmp = tempfile.TemporaryDirectory()
        original = self.gui.GUI_CFG
        self.gui.GUI_CFG = Path(self.tmp.name) / "gui_config.json"
        self.addCleanup(lambda: setattr(self.gui, "GUI_CFG", original))
        self.window = self.gui.MainWindow()
        self.addCleanup(self.window.deleteLater)
        self.tab = self.window.settings_tab

    def immediate_worker(self):
        """不真开线程的 worker：start() 当场跑完并 emit，测试才是确定的。"""
        class ImmediateProbe(self.gui.ApiProbeWorker):
            def start(self):
                self.run()
        return ImmediateProbe

    def test_tab_exposes_both_buttons_and_editable_model_box(self):
        self.assertEqual(self.tab.btn_models.text(), "获取模型列表")
        self.assertEqual(self.tab.btn_diagnose.text(), "测试连接")
        # 对齐微信端：模型名称是输入框，下拉只负责"挑"（model_pick 只读）
        self.assertIsInstance(self.tab.model_name, self.gui.QLineEdit)
        self.assertIsInstance(self.tab.model_pick, self.gui.QComboBox)
        self.assertFalse(self.tab.model_pick.isEditable())
        # 真正避免卡界面靠的是 QThread 后台线程
        self.assertTrue(issubclass(self.gui.ApiProbeWorker, QThread))

    def test_fill_models_keeps_user_typed_model(self):
        self.tab.model_name.setText("Kimi K2.7 Code")
        self.assertEqual(self.tab.fill_models(["a-model", "b-model"]), 2)
        self.assertEqual(self.tab.model_name.text(), "Kimi K2.7 Code", "不擅自改用户手填的模型")
        self.assertEqual([self.tab.model_pick.itemText(i) for i in range(2)], ["a-model", "b-model"])
        # 用户没填过时直接用列表里的第一个
        self.tab.model_name.setText("")
        self.tab.fill_models(["first", "second"])
        self.assertEqual(self.tab.model_name.text(), "first")

    def test_fetch_models_posts_current_values_and_shows_count(self):
        self.tab.api_url.setText("https://api.moonshot.cn/v1")
        self.tab.api_style.setCurrentIndex(self.tab.api_style.findData("auto"))
        self.tab.api_key.setText("")
        result = {"ok": True, "count": 2, "models": ["kimi-k2", "moonshot-v1-8k"],
                  "message": "已获取 2 个模型"}
        with patch.object(self.gui, "ApiProbeWorker", self.immediate_worker()), \
             patch.object(self.gui, "api_post", return_value=result) as posted:
            self.window._fetch_models()
        self.assertEqual(posted.call_args.args[0], "ai/models")
        payload = posted.call_args.args[1]
        self.assertEqual(payload["api_url"], "https://api.moonshot.cn/v1")
        self.assertEqual(payload["api_key"], "")          # 留空 = 用已保存的密钥
        self.assertEqual(payload["api_style"], "auto")
        self.assertEqual(self.tab.model_pick.count(), 2)
        self.assertIn("已获取 2 个模型", self.tab.probe_lbl.text())
        self.assertTrue(self.tab.btn_models.isEnabled())

    def test_fetch_models_shows_chinese_error_inline(self):
        result = {"ok": False, "status": 403, "models": [], "count": 0,
                  "message": "HTTP 403：密钥没有该模型或该分组的权限（中转站常见：换 key 或换分组/模型）",
                  "detail": "no permission", "suggested_base_url": "https://api.deepseek.com/v1"}
        with patch.object(self.gui, "ApiProbeWorker", self.immediate_worker()), \
             patch.object(self.gui, "api_post", return_value=result):
            self.window._fetch_models()
        text = self.tab.probe_lbl.text()
        self.assertIn("HTTP 403", text)
        self.assertIn("换 key", text)
        self.assertIn("建议把 API 地址填成：https://api.deepseek.com/v1", text)
        self.assertTrue(self.tab.btn_models.isEnabled())

    def test_diagnose_shows_endpoint_style_status_latency_and_problem(self):
        self.tab.api_url.setText("https://api.moonshot.cn/v1")
        self.tab.model_name.setText("Kimi K2.7 Code")
        result = {"ok": False, "url": "https://api.moonshot.cn/v1/chat/completions",
                  "models_url": "https://api.moonshot.cn/v1/models", "api_style": "openai",
                  "style_source": "auto", "model": "Kimi K2.7 Code", "model_listed": False,
                  "models_ok": True, "models_status": 200, "models_count": 37, "models_sample": ["kimi-k2"],
                  "chat_ok": False, "chat_status": 404, "latency_ms": 321, "reply": "",
                  "suggested_models": ["TM ·DeepSeek-V4.1"],
                  "problem": "HTTP 404：地址路径不对（可能少了或多了 /v1） 模型名称不在可用列表里，"
                             "最接近的是：TM ·DeepSeek-V4.1。这就是 403/404 的常见原因，请从列表中选择。"}
        with patch.object(self.gui, "ApiProbeWorker", self.immediate_worker()), \
             patch.object(self.gui, "api_post", return_value=result) as posted:
            self.window._diagnose_ai()
        self.assertEqual(posted.call_args.args[0], "ai/diagnose")
        self.assertEqual(posted.call_args.args[1]["model"], "Kimi K2.7 Code")
        text = self.tab.probe_lbl.text()
        self.assertIn("https://api.moonshot.cn/v1/chat/completions", text)
        self.assertIn("OpenAI 兼容", text)
        self.assertIn("HTTP 404", text)
        self.assertIn("321 ms", text)
        self.assertIn("模型在列表中：否", text)
        self.assertIn("建议改用模型：TM ·DeepSeek-V4.1", text)
        self.assertIn("请从列表中选择", text)
        self.assertTrue(self.tab.btn_diagnose.isEnabled())

    def test_second_click_while_probing_is_ignored(self):
        started = []

        class SlowProbe(self.gui.ApiProbeWorker):
            def start(self):
                started.append(self.path)      # 假装还在跑，不 emit

            def isRunning(self):
                return True

        with patch.object(self.gui, "ApiProbeWorker", SlowProbe), \
             patch.object(self.gui, "api_post", return_value={}) as posted:
            self.window._fetch_models()
            self.window._fetch_models()
            self.window._diagnose_ai()
        self.assertEqual(started, ["ai/models"])
        posted.assert_not_called()
        self.assertFalse(self.tab.btn_models.isEnabled())     # 跑完前按钮保持禁用


if __name__ == "__main__":
    unittest.main()
