"""智能中心端口探测线程的生命周期：引用必须留到线程真正结束。

实测崩溃：run() 里 _PROBES.discard(self) 之后线程还没收工，Python 引用归零
→ 销毁运行中的 QThread → Qt qFatal("QThread: Destroyed while thread is still
running") → 进程 0xC0000409（BEX64，出错模块 Qt6Core.dll）。
"""
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:                        # pragma: no cover - 环境缺 PyQt6 时跳过
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


def _slow_probe(port):
    time.sleep(0.2)          # 四个端口 0.8 秒，够观察到"探测进行中"这个窗口
    return True


LEGACY_TARGETS = [
    {"key": "onebot", "label": "OneBot 3072", "port": 3072},
    {"key": "http", "label": "OneBot HTTP 3000", "port": 3000},
    {"key": "dsh", "label": "DSH 10529", "port": 10529},
    {"key": "bridge", "label": "qqbridge 3100", "port": 3100},
]


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class ProbeLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        targets = mock.patch.object(workbench, "_build_status_targets",
                                    return_value=LEGACY_TARGETS)
        targets.start()
        self.addCleanup(targets.stop)

    def _settle(self, tab):
        _spin(lambda: not tab._probe_running and not workbench._PROBES)
        tab.close()
        tab.deleteLater()
        self.app.processEvents()

    def test_running_probe_is_still_held_by_probes(self):
        """探测进行中 _PROBES 必须仍持有该 probe，否则线程会被提前回收。"""
        with mock.patch.object(workbench, "_port_open", _slow_probe):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            try:
                self.assertEqual(len(workbench._PROBES), 1, "构造后应正好有一个探测线程")
                probe = next(iter(workbench._PROBES))
                QTest.qWait(120)
                self.assertIn(probe, workbench._PROBES, "探测还没结束就丢了引用")
                self.assertTrue(tab._probe_running)
                self.assertFalse(probe.isFinished())
            finally:
                self._settle(tab)

    def test_probes_is_empty_after_probe_finished(self):
        """探测结束后 _PROBES 清空，且收尾过程不抛异常。"""
        reported = []
        with mock.patch.object(workbench, "_port_open", lambda port: time.sleep(0.05) or True), \
             mock.patch.object(sys, "excepthook", lambda *a: reported.append(a)):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            try:
                self.assertTrue(_spin(lambda: not tab._probe_running), "探测线程没有收工")
                self.assertTrue(_spin(lambda: not workbench._PROBES))
                self.assertEqual(workbench._PROBES, set())
                self.assertEqual(tab.labels["onebot"].text().split("：")[-1], "在线")
                self.assertEqual(reported, [])
            finally:
                self._settle(tab)
        self.assertEqual(reported, [])

    def test_second_refresh_does_not_start_a_second_probe(self):
        """互斥：探测没回来之前，连点刷新不许再开一个线程。"""
        with mock.patch.object(workbench, "_port_open", _slow_probe):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            try:
                tab.refresh_status()
                tab.refresh_status()
                self.assertTrue(tab._probe_running)
                self.assertEqual(len(workbench._PROBES), 1, "一次刷新只能有一个探测线程")
            finally:
                self._settle(tab)

    def test_bilibili_reference_tab_is_removed(self):
        """智能中心不再保留三个外链组成的 B 站占位页。"""
        with mock.patch.object(workbench, "_port_open", lambda port: False):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            try:
                self.assertTrue(_spin(lambda: not tab._probe_running and not workbench._PROBES))
                inner = tab.findChild(workbench.QTabWidget)
                self.assertIsNotNone(inner)
                titles = [inner.tabText(i) for i in range(inner.count())]
                self.assertEqual(titles, ["记忆核心", "日记", "电脑控制", "QQ 桥接"])
            finally:
                self._settle(tab)

    def test_result_callback_survives_deleted_labels(self):
        """探测回来时标签已经删掉：回调吞掉 RuntimeError，绝不能让 Qt abort。"""
        reported = []
        with mock.patch.object(workbench, "_port_open", lambda port: time.sleep(0.2) or True), \
             mock.patch.object(sys, "excepthook", lambda *a: reported.append(a)):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            try:
                labels = list(tab.labels.values())
                for label in labels:
                    label.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
                self.assertTrue(_spin(lambda: not workbench._PROBES, timeout=6.0),
                                "探测线程没有收工")
                self.assertFalse(tab._probe_running)
                self.assertEqual(reported, [], "槽里漏出了异常，Qt 会直接终止进程")
            finally:
                tab.close()
                tab.deleteLater()
                self.app.processEvents()


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class DiaryPageTests(unittest.TestCase):
    """日记页只读展示每日 txt；写日记只能走 AI，页面上不再有管理员保存按钮。"""

    def setUp(self):
        self.app = _app()
        targets = mock.patch.object(workbench, "_build_status_targets",
                                    return_value=LEGACY_TARGETS)
        targets.start()
        self.addCleanup(targets.stop)

    def test_lists_daily_txt_read_only_and_has_no_manual_save(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "2026-10-28.txt").write_text("昨天的事", encoding="utf-8")
            (folder / "2026-10-29.txt").write_text("今天的事", encoding="utf-8")
            with mock.patch.object(workbench, "_port_open", lambda port: False):
                tab = workbench.WorkbenchTab(diary_dir=folder)
                tab._status_timer.stop()
                try:
                    _spin(lambda: not workbench._PROBES)
                    self.assertTrue(tab.diary_edit.isReadOnly())
                    self.assertEqual(tab.diary_day.count(), 2)
                    self.assertEqual(tab.diary_day.currentText(), "2026-10-29")
                    self.assertEqual(tab.diary_edit.toPlainText(), "今天的事")
                    page = tab.diary_edit.parentWidget()
                    buttons = {b.text() for b in page.findChildren(workbench.QPushButton)}
                    self.assertIn("让 AI 写今天", buttons)
                    self.assertNotIn("保存", buttons)
                    self.assertNotIn("追加今天", buttons)
                    tab.diary_day.setCurrentText("2026-10-28")
                    self.assertEqual(tab.diary_edit.toPlainText(), "昨天的事")
                finally:
                    tab.close()
                    tab.deleteLater()
                    self.app.processEvents()

    def test_ai_button_asks_backend_with_confirmation(self):
        import tempfile
        from pathlib import Path
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "2026-10-29.txt").write_text("旧日记", encoding="utf-8")
            with mock.patch.object(workbench, "_port_open", lambda port: False), \
                 mock.patch.object(workbench.dialogs, "ask_confirm", lambda *a, **k: True), \
                 mock.patch.object(workbench.dialogs, "tell", lambda *a, **k: None):
                api = lambda widget, path, payload=None, **kwargs: calls.append((path, payload, kwargs))
                tab = workbench.WorkbenchTab(api=api, diary_dir=folder)
                tab._status_timer.stop()
                try:
                    tab.ask_ai_diary()
                    self.assertEqual(calls[0][0], "memory/diary/auto")
                    self.assertEqual(calls[0][1]["day"], "2026-10-29")
                    self.assertTrue(calls[0][1]["confirmed"])
                    self.assertTrue(calls[0][1]["force"])
                finally:
                    tab.close()
                    tab.deleteLater()
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
