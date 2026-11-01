import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication,QWidget
import qq_gui as gui

class DesktopReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def spin(self, predicate, seconds=3):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            self.app.processEvents()
            if predicate():return True
            time.sleep(.01)
        return False

    def test_result_waits_for_thread_exit_before_release(self):
        release=threading.Event();entered=threading.Event();calls=[]
        class SlowExit(gui.ApiTask):
            def run(self):
                self.result={'success':True};self.completed.emit('test',self.result)
                entered.set();release.wait(300)
        owner=QWidget();holder=[];dispatcher=gui._ApiDispatcher(owner)
        task=SlowExit('test','test');holder.append(task);dispatcher.watch(task,calls.append,holder)
        task.start()
        try:
            self.assertTrue(entered.wait(1));self.app.processEvents()
            self.assertEqual(holder,[task]);self.assertEqual(calls,[])
        finally:
            release.set();task.wait(4000)
        self.assertTrue(self.spin(lambda:len(calls)==1))
        self.assertEqual(holder,[]);owner.deleteLater()

    def test_slow_status_and_action_leave_event_loop_responsive(self):
        release=threading.Event();entered=threading.Event()
        with tempfile.TemporaryDirectory() as tmp, patch.object(gui,'GUI_CFG',Path(tmp)/'gui.json'),patch.object(sys,'argv',['qq','--no-backend']),patch.object(gui.UiHangWatchdog,'start'):
            window=gui.MainWindow();window._timer.stop()
            def slow(*a,**kw):entered.set();release.wait(300);return {'success':True}
            with patch.object(gui,'api_get',side_effect=slow),patch.object(gui,'api_post',side_effect=slow):
                try:
                    window._action('start');window._refresh()
                    self.assertTrue(entered.wait(1))
                    ticks=[];QTimer.singleShot(0,lambda:ticks.append(True))
                    self.assertTrue(self.spin(lambda:bool(ticks)))
                    self.assertTrue(window._busy)
                    window.show();self.assertFalse(window.close())
                finally:
                    release.set()
                    self.assertTrue(self.spin(lambda:not window._busy and not getattr(window,'_status_pending',False)))
            self.assertTrue(window.close());window.deleteLater()

    def test_restored_offscreen_geometry_fits_available_screen(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gui,'GUI_CFG',Path(tmp)/'gui.json'),patch.object(sys,'argv',['qq','--no-backend']),patch.object(gui.UiHangWatchdog,'start'),patch.object(gui,'api_get',return_value={}):
            window=gui.MainWindow();window._timer.stop()
            window.resize(5000,4000);window.move(9000,9000);window._fit_screen()
            area=window.screen().availableGeometry()
            self.assertTrue(area.contains(window.geometry()))
            window.resize(850,550);self.assertEqual(window.width(),850)
            window.close();window.deleteLater()

    def test_late_backdrop_callback_ignores_destroyed_window(self):
        # 100ms 的一次性云母回调落在窗口销毁之后时，不得再碰 winId（否则 PyQt 槽里抛 RuntimeError 会直接 abort 进程）
        with tempfile.TemporaryDirectory() as tmp, patch.object(gui,'GUI_CFG',Path(tmp)/'gui.json'),patch.object(sys,'argv',['qq','--no-backend']),patch.object(gui.UiHangWatchdog,'start'),patch.object(gui,'api_get',return_value={}):
            calls=[]
            with patch.object(gui,'apply_mica',side_effect=lambda *a,**kw: calls.append(a)):
                window=gui.MainWindow();window._timer.stop()
                window.deleteLater()
                self.app.sendPostedEvents(None,gui.QEvent.Type.DeferredDelete)
                end=time.monotonic()+.5
                while time.monotonic()<end:
                    self.app.processEvents();time.sleep(.01)
            self.assertEqual(calls,[])

    def test_local_requests_disable_environment_proxies(self):
        with patch.object(gui.requests,'get') as request:
            gui.local_request('get','http://127.0.0.1:8090/api/runtime',timeout=1)
        self.assertEqual(request.call_args.kwargs['proxies'],{'http':None,'https':None,'all':None})
