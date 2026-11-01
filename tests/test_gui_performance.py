"""控制台性能回归：主线程不许被同步端口探测或整表重建拖住。

钉住 2026-10 那次「控制台特别特别卡」的两个实测根因：

1. WorkbenchTab 在主线程同步 connect 4 个本地端口。create_connection 打一个
   没在监听、且 SYN 被防火墙截掉的端口不会立刻失败，要挂满 0.8 秒超时；
   四个串起来实测把窗口冻了 3.2 秒（后台采样：20 秒空闲里 34% 时间主线程
   完全冻住，两次各 3.29 秒）。现在探测跑在 _PortProbe 线程里，标签文案和
   配色一格没改，只是更新时刻晚到。
2. PluginTab.refresh 每次切回插件页都删掉再建 63 行控件（实测约 75 毫秒）。
   数据没变时不该重建。

两个用例都不连后台、不构造 MainWindow，几秒内跑完。
"""
import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:                    # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

from src.gui import workbench

_APP = None


def _app():
    # 必须持有模块级引用：QApplication 被回收后再建 QWidget 会直接终止进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _spin(predicate, timeout=8.0):
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def _settle(tab):
    """等探测线程收工再销毁对象：QThread 还在跑时销毁它会让 Qt 直接终止进程。"""
    _spin(lambda: not tab._probe_running and not workbench._PROBES)
    tab.close()
    tab.deleteLater()
    _app().processEvents()


LEGACY_TARGETS = [
    {"key": "onebot", "label": "OneBot 3072", "port": 3072},
    {"key": "http", "label": "OneBot HTTP 3000", "port": 3000},
    {"key": "dsh", "label": "DSH 10529", "port": 10529},
    {"key": "bridge", "label": "qqbridge 3100", "port": 3100},
]


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class WorkbenchStatusTests(unittest.TestCase):
    def setUp(self):
        targets = mock.patch.object(workbench, "_build_status_targets",
                                    return_value=LEGACY_TARGETS)
        targets.start()
        self.addCleanup(targets.stop)

    def test_port_probe_runs_off_the_gui_thread(self):
        ports = []
        on_main = []

        def slow_probe(port):
            ports.append(port)
            on_main.append(threading.current_thread() is threading.main_thread())
            time.sleep(0.4)                      # 连不通的端口要等满超时
            return port in (3072, 3000)

        with mock.patch.object(workbench, "_port_open", slow_probe):
            start = time.perf_counter()
            tab = workbench.WorkbenchTab()
            construct_ms = (time.perf_counter() - start) * 1000
            tab._status_timer.stop()
            try:
                self.assertLess(construct_ms, 500,
                                "构造时同步等了端口探测：%.0f 毫秒" % construct_ms)
                start = time.perf_counter()
                tab.refresh_status()
                again_ms = (time.perf_counter() - start) * 1000
                self.assertLess(again_ms, 200, "刷新同步等了端口探测：%.0f 毫秒" % again_ms)
                self.assertTrue(_spin(lambda: not tab._probe_running
                                      and tab.labels["onebot"].text().endswith("在线")),
                                "端口探测结果没回到标签上")
                self.assertTrue(tab.labels["http"].text().endswith("在线"))
                for key in ("dsh", "bridge"):
                    self.assertTrue(tab.labels[key].text().endswith("离线"),
                                    "%s 标签文案不对：%s" % (key, tab.labels[key].text()))
                self.assertEqual(set(ports), {3072, 3000, 10529, 3100})
                self.assertFalse(any(on_main), "端口探测跑在了 GUI 线程上，窗口会冻住")
            finally:
                _settle(tab)

    def test_probe_result_is_dropped_after_window_closed(self):
        """探测回来的那一刻窗口可能已经关了：结果要丢弃，不能让 Qt 终止进程。

        这条钉的是实测过的崩溃：线程里回包，槽里操作已删除的 QLabel 抛
        RuntimeError，未捕获的异常会让 Qt 直接 abort（进程退出码 0xC0000409）。
        """
        with mock.patch.object(workbench, "_port_open", lambda port: time.sleep(0.3) or True):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            tab.close()
            tab.deleteLater()
            _app().sendPostedEvents(None, QEvent.Type.DeferredDelete)
            _app().processEvents()
            # 探测线程还在跑，回包会打到已删除的 QLabel 上；跑到这里不炸就算通过
            self.assertTrue(_spin(lambda: not workbench._PROBES, timeout=6.0),
                            "探测线程没有收工")

    def test_bridge_report_waits_for_probe_result(self):
        with mock.patch.object(workbench, "_port_open", lambda port: port in (3072, 3000)):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            try:
                self.assertTrue(_spin(lambda: not tab._probe_running
                                      and tab.labels["onebot"].text().endswith("在线")))
                with mock.patch.object(workbench.dialogs, "tell") as tell:
                    tab._after_start()
                    tell.assert_not_called()     # 探测还在后台，回包前不许下结论
                    self.assertTrue(_spin(lambda: tell.called), "探测回来了也没弹启动结果")
                    self.assertEqual(tell.call_args.args[1], "已启动")
            finally:
                _settle(tab)


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class PluginRefreshTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        import qq_gui
        self.gui = qq_gui

    @staticmethod
    def _payload(count=8):
        return [{"id": "p%d" % i, "name": "插件%d" % i, "enabled": True,
                 "category": "utility", "description": "说明"} for i in range(count)]

    @staticmethod
    def _api(plugins):
        def fake_get(path, *args, **kwargs):
            if path == "plugins":
                return {"success": True, "plugins": plugins}
            if path == "plugins/custom":
                return {"success": True, "plugins": []}
            raise AssertionError("插件页刷到了意外的接口：" + str(path))
        return fake_get

    def test_unchanged_payload_reuses_rows(self):
        payload = self._payload()
        with mock.patch.object(self.gui, "api_get", self._api(payload)):
            tab = self.gui.PluginTab()
            try:
                tab.notice.setText("")
                tab.refresh()
                self.assertEqual(len(tab._rows), 8)
                self.assertIn("列表已更新", tab.notice.text())
                first = {pid: id(row["frame"]) for pid, row in tab._rows.items()}
                tab.refresh()
                self.assertEqual({pid: id(row["frame"]) for pid, row in tab._rows.items()}, first,
                                 "数据没变却重建了整页控件")
                payload[0]["name"] = "改过的名字"
                tab.refresh()
                self.assertNotEqual(id(tab._rows["p0"]["frame"]), first["p0"],
                                    "数据变了却沿用旧行，界面会停在旧内容上")
            finally:
                tab.close()
                tab.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
