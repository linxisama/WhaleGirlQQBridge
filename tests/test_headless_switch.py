"""一个控制台控制多个 QQ：管理器账号 → 无窗口后端 → 切 BASE_URL。"""
import json
import os
import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
import qq_gui  # noqa: E402
from src import gateway_link  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    token = 'tok'
    seen = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        _Handler.seen = {'path': self.path, 'body': body,
                         'auth': self.headers.get('Authorization')}
        data = json.dumps({'port': 8123, 'state': 'starting'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class HeadlessSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_start_headless_bot_sends_flag_and_returns_port(self):
        result = gateway_link.start_headless_bot(
            {'base_url': self.base, 'token': 'tok'}, 'acc1', 'D:/bot')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['port'], 8123)
        self.assertEqual(_Handler.seen['body'], {'source': 'D:/bot', 'headless': True})
        self.assertEqual(_Handler.seen['path'], '/v1/accounts/acc1/bot')

    def test_console_switch_sets_base_url(self):
        window = qq_gui.MainWindow()
        self.addCleanup(window.close)
        old = qq_gui.BASE_URL
        self.addCleanup(setattr, qq_gui, 'BASE_URL', old)
        window._manager_backend_ready('acc1', {'ok': True, 'port': 8123})
        self.assertEqual(window.base_url, 'http://127.0.0.1:8123')
        self.assertIn('8123', window.account_hint.text())

    def test_failed_switch_shows_reason(self):
        window = qq_gui.MainWindow()
        self.addCleanup(window.close)
        window._manager_backend_ready('acc1', {'ok': False, 'message': 'QQ 未在线'})
        self.assertIn('QQ 未在线', window.account_hint.text())


if __name__ == '__main__':
    unittest.main()
