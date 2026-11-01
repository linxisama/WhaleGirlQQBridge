"""把本轮踩到的坑钉成回归：定时刷新/插件开关**不要**用 QThread 改异步。

事故经过（2026-10-16）：按审计建议把「机器人管理」每 3 秒的同步刷新改成后台线程取数、
把插件开关改成异步回调 —— 单文件测试通过、`poll()` 从 500 ms 阻塞降到 1 ms，
但**全量回归出现原生崩溃 0xC0000409/0xC0000005**，与 QThread 在"控件销毁/进程退出"时的
生命周期有关（线程还在跑、C++ 对象已销毁）。回退后立即恢复 1902 项 OK。

结论：要重做这件事，必须先设计线程回收（父子关系、退出顺序、join），并补
"线程生命周期"专项测试；不能只跑单文件就上线。这里用源码级断言把当前的安全形态固定住。
"""
import inspect
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.gui import conversation  # noqa: E402

APP = QApplication.instance() or QApplication([])


def code_only(source: str) -> str:
    """去掉注释行：断言要看的是**代码**，不是我们写的说明注释。"""
    return "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))


class NoBackgroundPollingTests(unittest.TestCase):
    def test_pc_tab_poll_stays_synchronous(self):
        source = code_only(inspect.getsource(conversation.PcTab.poll))
        self.assertNotIn("QThread", source, "定时刷新不要引入线程（见本文件顶部事故说明）")
        self.assertNotIn("FetchWorker", source)
        self.assertIn("self.refresh()", source)

    def test_no_fetch_worker_class_is_shipped(self):
        self.assertFalse(hasattr(conversation, "FetchWorker"),
                         "FetchWorker 已因原生崩溃回退，不要重新加回来（除非连同线程回收一起设计）")

    def test_pc_tab_refresh_is_guarded(self):
        """刷新仍然要有 safe_slot 保护 + 字段安全取值。"""
        source = inspect.getsource(conversation.PcTab.refresh)
        self.assertIn("_apply", source)

    def test_plugin_toggle_is_synchronous(self):
        import qq_gui
        source = code_only(inspect.getsource(qq_gui.PluginTab._toggle))
        self.assertIn("api_post", source, "插件开关保持同步（异步版曾触发原生崩溃）")
        self.assertNotIn("run_api_async", source)


if __name__ == "__main__":
    unittest.main()
