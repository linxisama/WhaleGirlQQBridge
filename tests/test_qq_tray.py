"""托盘必须保留机器人，显式退出才停止；全程不向QQ发送测试消息。"""
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication,QWidget,QSystemTrayIcon
from PyQt6.QtNetwork import QLocalSocket
from PyQt6.QtTest import QTest
import qq_gui as gui
from src.gui.tray import RobotTray,ConsoleInstance
from src.connection_status import connection_status
from src.qq_transports import OneBotQQ,QqBridge


class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old_quit=self.app.quitOnLastWindowClosed()
        self.patches=[patch.object(gui,'AUTO_BACKEND',False),patch.object(gui.UiHangWatchdog,'start')]
        for p in self.patches:p.start()
        self.window=gui.MainWindow(config_path=Path(self.tmp.name)/'gui.json')
        self.window._timer.stop();self.window.pc_tab.timer.stop()
    def tearDown(self):
        self.window._quitting=True;self.window.close();self.window.deleteLater();self.app.processEvents()
        self.app.setQuitOnLastWindowClosed(self.old_quit)
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_closing_hides_keeps_backend_timers_and_can_restore_maximized(self):
        w=self.window;w._tray.enabled=True;w._backend_proc=Mock();w._timer.start(3000);w.showMaximized();self.app.processEvents()
        with patch.object(QSystemTrayIcon,'isSystemTrayAvailable',return_value=True):self.assertFalse(w.close())
        self.assertFalse(w.isVisible());w._backend_proc.terminate.assert_not_called();self.assertTrue(w._timer.isActive());self.assertFalse(w.hang_watchdog._stop)
        w._tray.show_window();self.app.processEvents();self.assertTrue(w.isVisible());self.assertTrue(w.isMaximized())
    def test_long_operation_can_be_hidden_without_cancelling(self):
        w=self.window;w._tray.enabled=True;w._busy=True;w.show()
        with patch.object(QSystemTrayIcon,'isSystemTrayAvailable',return_value=True):self.assertFalse(w.close())
        self.assertFalse(w.isVisible());self.assertTrue(w._busy);w._busy=False
    def test_unavailable_tray_uses_original_close(self):
        w=self.window;w._tray.enabled=True;w.show()
        self.app.setQuitOnLastWindowClosed(False)
        with patch.object(QSystemTrayIcon,'isSystemTrayAvailable',return_value=False):self.assertTrue(w.close())
        self.assertTrue(w.hang_watchdog._stop)
        self.assertTrue(self.app.quitOnLastWindowClosed())
    def test_actions_follow_running_disconnected_and_busy_states(self):
        tray=self.window._tray;tray.update({'running':False});self.assertTrue(tray.start_action.isEnabled());self.assertFalse(tray.stop_action.isEnabled())
        with patch.object(self.window,'_action') as action:tray.start_action.trigger();action.assert_called_once_with('start')
        tray.update({'running':True});self.assertFalse(tray.start_action.isEnabled());self.assertTrue(tray.stop_action.isEnabled())
        with patch.object(self.window,'_action') as action:tray.stop_action.trigger();action.assert_called_once_with('stop')
        self.window._busy=True;tray.refresh_actions();self.assertFalse(tray.stop_action.isEnabled());self.window._busy=False
        tray.update({'success':False});self.assertFalse(tray.start_action.isEnabled());self.assertFalse(tray.stop_action.isEnabled());self.assertTrue(tray.exit_action.isEnabled())
    def test_exit_calls_stop_then_terminates_owned_backend_and_quits(self):
        w=self.window;w._tray.enabled=True;w._backend_proc=Mock()
        with patch.object(gui,'run_api_async') as run,patch.object(QApplication,'quit') as quit_app:
            w._tray.exit_action.trigger();self.assertTrue(w._quitting);w._backend_proc.terminate.assert_not_called()
            self.assertEqual(run.call_args.args[1],'stop');self.assertFalse(w._tray.exit_action.isEnabled())
            run.call_args.kwargs['callback']({'success':True});w._backend_proc.terminate.assert_called_once();quit_app.assert_called_once()
            self.assertFalse(w._tray.icon.isVisible())
        w._backend_proc=None
    def test_busy_exit_keeps_window_and_does_not_discard_operation(self):
        w=self.window;w._busy=True
        with patch.object(gui,'run_api_async') as run:w._request_exit()
        self.assertFalse(w._quitting);run.assert_not_called();self.assertTrue(w.isVisible());w._busy=False
    def test_failed_stop_of_reused_backend_keeps_console_for_recovery(self):
        w=self.window;w._backend_proc=None;w._quitting=True
        with patch.object(QApplication,'quit') as quit_app:w._exit_stopped({'success':False,'message':'connection lost'})
        quit_app.assert_not_called();self.assertFalse(w._quitting);self.assertTrue(w.isVisible())
        self.assertTrue(w._timer.isActive());self.assertTrue(w.pc_tab.timer.isActive())
    def test_menu_is_opaque_and_has_three_controls_plus_exit(self):
        tray=self.window._tray
        self.assertEqual([a.text() for a in tray.menu.actions() if not a.isSeparator()],['显示控制台','开启机器人','停止机器人','强制退出控制台（后台继续运行）','退出程序'])
        tray.menu.show();self.app.processEvents();color=tray.menu.grab().toImage().pixelColor(4,4)
        self.assertEqual(color.alpha(),255);self.assertGreater(color.lightness(),220);tray.menu.hide()
    def test_single_instance_restore_and_invalid_commands(self):
        w=self.window;w._tray.show_window=Mock();server=ConsoleInstance(w,Path(self.tmp.name));self.assertTrue(server.listening)
        try:
            self.assertTrue(ConsoleInstance.forward(Path(self.tmp.name)));QTest.qWait(40);w._tray.show_window.assert_called_once()
            w._tray.show_window.reset_mock();peer=QLocalSocket();peer.connectToServer(server.name(Path(self.tmp.name)));self.assertTrue(peer.waitForConnected(100))
            peer.write(b'exec');peer.flush();QTest.qWait(30);w._tray.show_window.assert_not_called()
        finally:server.server.close()
    def test_single_instance_partial_show_request(self):
        w=self.window;w._tray.show_window=Mock();server=ConsoleInstance(w,Path(self.tmp.name));peer=QLocalSocket()
        try:
            peer.connectToServer(server.name(Path(self.tmp.name)));self.assertTrue(peer.waitForConnected(100));peer.write(b'sh');peer.flush();QTest.qWait(20);w._tray.show_window.assert_not_called()
            peer.write(b'ow');peer.flush()
            # 本地套接字读取在满载机器上可能晚于 30ms 才到；轮询等它，而不是赌固定延时。
            deadline=time.monotonic()+10
            while time.monotonic()<deadline and not w._tray.show_window.called:
                QTest.qWait(50)
            w._tray.show_window.assert_called_once()
        finally:peer.abort();server.server.close()
    def test_existing_console_without_ack_does_not_allow_duplicate(self):
        server=ConsoleInstance(self.window,Path(self.tmp.name))
        server.server.newConnection.disconnect(server.accept)
        try:self.assertTrue(ConsoleInstance.forward(Path(self.tmp.name)))
        finally:server.server.close()
    def test_connection_card_uses_bridge_account_not_onebot(self):
        with patch.object(self.window.console_tab.card_conn,'update') as update:
            self.window.console_tab.refresh({'mode':'qqbridge','qqbridge':{'online':True,'connected':True,'self_id':'999','nickname':'鲸鱼'},'onebot':{'online':False}})
        self.assertIn('直连在线',update.call_args.args[0]);self.assertIn('鲸鱼',update.call_args.kwargs['detail']);self.assertIn('999',update.call_args.kwargs['detail'])
    def test_status_failure_disables_stale_tray_actions(self):
        self.window._tray.update({'running':True});self.window._status_done({'success':False,'message':'backend offline'})
        self.assertFalse(self.window._tray.stop_action.isEnabled())


class ConnectionTests(unittest.TestCase):
    def test_official_authentication_does_not_claim_receive_ready(self):
        state=connection_status(dict(mode='official',account='app123',official={'authenticated':True,'ws_connected':False}))
        self.assertFalse(state['receive_ready']);self.assertTrue(state['send_ready']);self.assertIn('未实际发送验证',state['detail']);self.assertIn('app123',state['detail'])
    def test_socket_without_logged_account_is_not_send_ready(self):
        state=connection_status(dict(mode='qqbridge',qqbridge={'connected':True,'online':True,'self_id':''}))
        self.assertFalse(state['send_ready']);self.assertIn('等待 QQ 登录',state['title'])
    def test_none_status_and_bad_substatus_are_safe(self):
        for value in (None,{},dict(mode='official',official=[]),dict(mode='qqbridge',qqbridge='bad')):
            self.assertIsInstance(connection_status(value)['detail'],str)


class AccountProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_and_name_sync_even_when_status_lookup_fails(self):
        for peer in (OneBotQQ(),QqBridge()):
            peer.self_id='old';peer.online=True
            peer.call=AsyncMock(side_effect=[{'user_id':999,'nickname':'新账号'},TimeoutError()])
            with self.assertRaises(TimeoutError):await peer.probe()
            self.assertEqual(peer.self_id,'999');self.assertEqual(peer.nickname,'新账号');self.assertFalse(peer.online)
    async def test_successful_probe_updates_account_and_ready(self):
        for peer in (OneBotQQ(),QqBridge()):
            peer.call=AsyncMock(side_effect=[{'user_id':999,'nickname':'鲸鱼'},{'online':True,'good':True}]);await peer.probe()
            self.assertTrue(peer.online);self.assertEqual(peer.nickname,'鲸鱼')
