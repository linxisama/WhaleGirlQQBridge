"""窗框 / 最大化 / 孤儿后台 的回归测试（真机 2026-10-17）。

用户反馈两件事：
1. 「新版有白框框，不能最大化最小化和等比例扩大」
   - 白框：无边框窗口外层留了 6px 边距，容器只铺在里面；「窗口透明度 0%」时窗口几乎
     不透明，这 6px 露出窗口默认底色 → 一圈白边；同时容器不铺满，边缘缩放的命中区也没了；
   - 最大化：按钮用自己记的 `_maximized`，跟真实窗口状态不同步 → 点了"没反应"。
2. 「修复过程中一直弹『清理上次留下的后台』」
   - 控制台崩溃/被强杀时子进程（uvicorn）活下来占着 8090；
   - 现在启动后台时把控制台 PID 传下去，后台发现父进程没了会自己退出。
"""
import ast
import os
import pathlib
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.qq_app import _parent_process_alive  # noqa: E402

APP = QApplication.instance() or QApplication([])
GUI = pathlib.Path(r"D:\聊天机器人\qq机器人\qq_gui.py")


class FrameMarginTests(unittest.TestCase):
    """白框的根因与修法（源码级约束 + 真实窗口行为）。"""

    def test_margin_follows_opacity(self):
        source = GUI.read_text(encoding="utf-8")
        self.assertIn("def _sync_frame_margin", source, "需要有按不透明度调节边距的方法")
        block = source.split("def _sync_frame_margin", 1)[1][:1200]
        self.assertIn("windowOpacity() >= 0.995", block, "不透明时应当把边距归零")
        self.assertIn("margin = 0 if opaque else 6", block)

    def test_margin_is_synced_on_show_and_appearance(self):
        source = GUI.read_text(encoding="utf-8")
        show_block = source.split("def showEvent", 1)[1][:900]
        self.assertIn("_sync_frame_margin()", show_block, "显示时要同步一次边距")
        apply_block = source.split("def _apply_appearance", 1)[1][:1200]
        self.assertIn("_sync_frame_margin()", apply_block, "改外观（透明度）时也要同步")

    def test_real_window_margin(self):
        import tempfile

        import qq_gui
        from src.gui import diagnostics

        diagnostics.install(pathlib.Path(tempfile.mkdtemp()), lambda title, message: None)
        cfg = pathlib.Path(tempfile.mkdtemp()) / "gui_config.json"
        cfg.write_text('{"transparency": 0.0}', encoding="utf-8")
        window = qq_gui.MainWindow(config_path=cfg)
        try:
            window.show()
            APP.processEvents()
            margins = window.layout().contentsMargins()
            self.assertEqual(margins.left(), 0,
                             "窗口不透明时不该留边距（否则就是那圈白框）")
            container = window._container.geometry()
            self.assertEqual(container.left(), 0, "容器要铺满窗口，边缘缩放才有效")
            self.assertEqual(container.width(), window.rect().width())
        finally:
            window.close()
            APP.processEvents()


class MaximizeStateTests(unittest.TestCase):
    def test_toggle_uses_real_state(self):
        source = GUI.read_text(encoding="utf-8")
        block = source.split("def _toggle_max", 1)[1][:600]
        self.assertIn("isMaximized()", block, "应当按窗口真实状态判断，而不是自己记的布尔值")
        self.assertNotIn("self._maximized = not self._maximized", block,
                         "旧的取反标记会让按钮看起来点不动")

    def test_glyph_syncs(self):
        source = GUI.read_text(encoding="utf-8")
        self.assertIn("def _sync_max_glyph", source)
        self.assertIn("def changeEvent", source, "系统发起的最大化（双击标题栏）也要同步")


class OrphanBackendTests(unittest.TestCase):
    def test_spawn_passes_parent_pid(self):
        source = GUI.read_text(encoding="utf-8")
        self.assertIn("WHALEGIRL_PARENT_PID", source, "启动后台时要传控制台 PID")
        self.assertIn("env=child_env", source)

    def test_parent_check_logic(self):
        self.assertTrue(_parent_process_alive(os.getpid()), "活着的进程应当判定为存活")
        self.assertFalse(_parent_process_alive(999999), "不存在的 PID 应当判定为已退出")
        self.assertTrue(_parent_process_alive(0), "拿不到明确信息时宁可认为活着，不误杀")

    def test_backend_watchdog_registered(self):
        source = pathlib.Path(r"D:\聊天机器人\qq机器人\src\qq_app.py").read_text(encoding="utf-8")
        self.assertIn("async def _watch_parent", source)
        create = source.split("def create_app", 1)[1][:4000]
        self.assertIn("_watch_parent(_parent_pid, app)", create,
                      "create_app 里要真的挂上看护任务")


if __name__ == "__main__":
    unittest.main()
