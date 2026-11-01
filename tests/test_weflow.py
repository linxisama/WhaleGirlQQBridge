"""WeFlow 只读客户端 + 接口测试（用本地假服务器，不依赖真 WeFlow）。"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from fastapi.testclient import TestClient

from src.weflow_client import WeFlowClient, WeFlowError


class _Handler(BaseHTTPRequestHandler):
    token = 'tok-1'
    routes = {}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if self.headers.get('Authorization') != 'Bearer ' + self.token:
            return self._reply({'success': False, 'message': 'unauthorized'}, 401)
        handler = self.routes.get(parsed.path)
        if handler is None:
            return self._reply({'message': 'not found'}, 404)
        self._reply(handler(parse_qs(parsed.query)), 200)

    def _reply(self, payload, status):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class WeFlowClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _Handler.routes = {
            '/api/v1/health': lambda q: {'status': 'ok'},
            '/api/v1/sessions': lambda q: {'success': True, 'sessions': [
                {'username': 'wxid_a', 'displayName': '张三', 'type': 1}]},
            '/api/v1/messages': lambda q: {'success': True, 'count': 1, 'messages': [
                {'localId': 1, 'content': '你好', 'createTime': 1738713600}]},
            '/api/v1/contacts': lambda q: {'success': True, 'contacts': [
                {'username': 'wxid_b', 'displayName': '李四'}]},
            '/api/v1/group-members': lambda q: {'success': True, 'members': [
                {'wxid': 'wxid_c', 'displayName': '王五'}]},
        }
        self.base = f'http://127.0.0.1:{self.port}'

    def test_health_and_status(self):
        client = WeFlowClient(self.base, 'tok-1')
        self.assertEqual(client.health()['status'], 'ok')
        self.assertTrue(client.status()['ok'])

    def test_wrong_token_is_readable(self):
        client = WeFlowClient(self.base, 'bad')
        with self.assertRaises(WeFlowError) as caught:
            client.health()
        self.assertIn('Token', str(caught.exception))

    def test_sessions_and_messages(self):
        client = WeFlowClient(self.base, 'tok-1')
        sessions = client.sessions()
        self.assertEqual(sessions[0]['displayName'], '张三')
        messages = client.messages('wxid_a')
        self.assertEqual(messages['messages'][0]['content'], '你好')

    def test_unreachable_is_readable(self):
        client = WeFlowClient('http://127.0.0.1:9', 'tok-1', timeout=1)
        self.assertFalse(client.status()['ok'])


class WeFlowApiTests(unittest.TestCase):
    def setUp(self):
        import tempfile, pathlib
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        (root / 'config.qq.yaml').write_text('qq_only:\n  mode: mock\n', encoding='utf-8')
        (root / 'data' / 'qq-only').mkdir(parents=True)
        from src.qq_app import create_app
        self.app = create_app()

        class _Config:
            def get(self, key, default=None):
                return 'http://127.0.0.1:9' if key == 'weflow.base_url' else default

        class _Bot:
            config = _Config()

        self.app.state.bot = _Bot()      # 不跑 lifespan，直接给路由一个最小运行时
        self.client = TestClient(self.app)

    def test_status_returns_ok_false_when_unreachable(self):
        response = self.client.get('/api/weflow/status')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])

    def test_sessions_maps_error_to_502(self):
        response = self.client.get('/api/weflow/sessions')
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()['success'])


if __name__ == '__main__':
    unittest.main()
