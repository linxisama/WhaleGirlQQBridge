"""DSH 操作机器人：MCP 工具 → 本机控制台 HTTP 接口。"""
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'whale_bot_mcp.py'
ORIGINAL_TOOLS = {'bot_status', 'bot_start', 'bot_stop', 'bot_plugins', 'bot_set_plugin',
                  'bot_personas', 'bot_activate_persona', 'bot_send_text'}
MARKET_TOOLS = {'bot_market_list', 'bot_market_install', 'bot_extensions',
                'bot_set_extension', 'bot_bridge_status', 'bot_skill_tools'}
MARKET_ITEMS = [
    {'id': 'weather', 'name': '天气', 'description': '天气查询扩展', 'category': 'tool',
     'categoryName': '工具', 'version': '1.0.0', 'author': 'whale',
     'downloadUrl': 'http://127.0.0.1/x.zip'},
    {'id': 'persona_pack', 'name': '人设包', 'description': '额外人设', 'category': 'persona',
     'categoryName': '人设', 'version': '2.0.0', 'author': 'whale'},
    {'id': 'tool_extra', 'name': '附加工具', 'description': '别的东西', 'category': 'tool',
     'categoryName': '工具', 'version': '5.0.0', 'author': 'whale'},
]


class FakeBot(BaseHTTPRequestHandler):
    calls = None
    market = False

    def log_message(self, *args):
        pass

    @classmethod
    def record(cls, method, path, body):
        if cls.calls is None:
            cls.calls = []
        cls.calls.append((method, path, body))

    def _json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _market(self, payload):
        if type(self).market:
            self._json(200, payload)
        else:
            self._json(404, {'detail': '未知路径'})

    def do_GET(self):
        type(self).record('GET', self.path, None)
        if self.path == '/api/runtime':
            self._json(200, {'app': 'whale', 'version': '5.0.0', 'data_root': 'C:/data'})
        elif self.path == '/api/status':
            self._json(200, {'running': True, 'reply_paused': False, 'mode': 'onebot',
                             'account': '10001', 'model': 'glm', 'ai_configured': True,
                             'onebot': {'connected': True, 'online': True, 'nickname': '小鲸'},
                             'qqbridge': {}, 'usage': {'saved_tokens': 12},
                             'messages': [{'time': 1, 'sender': 'u', 'text': 'hi',
                                           'state': 'sent', 'reply': 'hello',
                                           'kind': 'group'}]})
        elif self.path == '/api/plugins':
            self._json(200, [{'id': 'ambient_reply', 'name': '随机接话', 'enabled': True,
                              'category': 'social', 'description': 'x',
                              'schema': {'big': 'y'}}])
        elif self.path == '/api/personas':
            self._json(200, {'active': 'whale', 'locked': None,
                             'personas': [{'name': 'whale', 'display_name': '鲸鱼娘'}]})
        elif self.path == '/api/qqagent/extensions':
            self._market({'items': [{'id': 'weather', 'name': '天气', 'version': '1.0.0',
                                     'enabled': True, 'source': 'market'},
                                    {'id': 'local_x', 'manifest': {'name': '本地扩展'}}]})
        elif self.path == '/api/qqagent/bridge/status':
            self._market({'loaded': True, 'enabled': True, 'ready': True, 'profiles': 2,
                          'providers': ['openai', 'deepseek'], 'hooks': ['before_request'],
                          'missing': [], 'extensionsDir': 'C:/ext',
                          'catalogPath': 'C:/catalog.json'})
        elif self.path == '/api/qqagent/bridge/calls':
            self._market({'total': 3, 'success': 2, 'failed': 1,
                          'recent': [{'at': 1, 'tool': 'x', 'ok': True}]})
        elif self.path == '/api/qqagent/skills':
            self._market({'tools': [
                {'id': 'kb', 'tools': [{'name': 'kb_search', 'description': 's'},
                                       {'name': 'kb_put', 'description': 'p'}]},
                {'name': 'calc', 'description': 'c'}]})
        else:
            self._json(404, {'detail': '未知路径'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
        type(self).record('POST', self.path, body)
        if self.path == '/api/start':
            self._json(200, {'success': True, 'message': 'ok'})
        elif self.path == '/api/stop':
            self._json(200, {'success': True, 'message': 'stopped'})
        elif self.path == '/api/plugins/' or self.path.startswith('/api/plugins/'):
            self._json(200, {'success': True, 'message': 'saved'})
        elif self.path.startswith('/api/personas/'):
            self._json(200, {'success': True, 'active': 'whale2'})
        elif self.path == '/api/onebot/action':
            self._json(200, {'success': True, 'data': {'message_id': 7}})
        elif self.path == '/api/qqagent/market':
            self._market({'items': MARKET_ITEMS, 'categories': {'tool': '工具'},
                          'fetchedAt': '2026-10-13T00:00:00Z'})
        elif self.path == '/api/qqagent/market/install':
            self._market({'extension': {'id': body.get('id'), 'enabled': True}})
        elif self.path.startswith('/api/qqagent/extensions/'):
            self._market({'enabled': body.get('enabled')})
        else:
            self._json(404, {'detail': '未知路径'})


class FullBot(FakeBot):
    """控制台已实现市场/扩展端点。"""
    calls = None
    market = True


class LegacyBot(FakeBot):
    """控制台还没实现市场/扩展端点：必须回 not_implemented，不能崩。"""
    calls = None
    market = False


class BotMcpTests(unittest.TestCase):
    CLEAN_KEYS = ('PYTHONIOENCODING', 'PYTHONUTF8', 'PYTHONLEGACYWINDOWSSTDIO')

    @classmethod
    def setUpClass(cls):
        cls.servers = {}
        for name, handler in (('full', FullBot), ('legacy', LegacyBot)):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.servers[name] = (server, 'http://127.0.0.1:' + str(server.server_address[1]))
        cls.url = cls.servers['full'][1]
        cls.legacy_url = cls.servers['legacy'][1]

    @classmethod
    def tearDownClass(cls):
        for server, _url in cls.servers.values():
            server.shutdown()
            server.server_close()

    def setUp(self):
        FullBot.calls = []
        LegacyBot.calls = []

    def session(self, messages, url=None, clean_env=False):
        payload = '\n'.join(json.dumps(m, ensure_ascii=False) for m in messages) + '\n'
        env = dict(os.environ, WHALE_BOT_URL=url or self.url)
        if clean_env:
            for key in self.CLEAN_KEYS:
                env.pop(key, None)
        else:
            env['PYTHONIOENCODING'] = 'utf-8'
        result = subprocess.run([sys.executable, str(SCRIPT)], input=payload.encode('utf-8'),
                                capture_output=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return [json.loads(line) for line in result.stdout.decode('utf-8').splitlines()
                if line.strip()]

    def call(self, name, arguments=None, url=None):
        replies = self.session([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': name, 'arguments': arguments or {}}}], url=url)
        self.assertEqual(len(replies), 2, replies)
        return replies[-1]

    @staticmethod
    def payload(reply):
        return json.loads(reply['result']['content'][0]['text'])

    def tools_list(self):
        return self.session([{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                              'params': {}},
                             {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}])

    # ---- 握手 / 清单 / schema -------------------------------------------
    def test_tools_list_exposes_original_and_market_tools(self):
        replies = self.tools_list()
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'whale_bot')
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        tools = replies[1]['result']['tools']
        names = [tool['name'] for tool in tools]
        self.assertEqual(len(names), len(set(names)), '工具名重复')
        self.assertTrue(ORIGINAL_TOOLS.issubset(set(names)), set(names) - ORIGINAL_TOOLS)
        self.assertTrue(MARKET_TOOLS.issubset(set(names)), MARKET_TOOLS - set(names))

    def test_every_tool_schema_is_valid(self):
        tools = self.tools_list()[1]['result']['tools']
        self.assertTrue(ORIGINAL_TOOLS.issubset({tool['name'] for tool in tools}))
        for tool in tools:
            self.assertEqual(set(tool), {'name', 'description', 'inputSchema'}, tool['name'])
            self.assertTrue(tool['name'] and len(tool['name']) <= 64, tool['name'])
            self.assertGreaterEqual(len(tool['description']), 8, tool['name'])
            schema = tool['inputSchema']
            self.assertEqual(schema['type'], 'object', tool['name'])
            self.assertIsInstance(schema['properties'], dict, tool['name'])
            self.assertIs(schema['additionalProperties'], False, tool['name'])
            for key in schema.get('required') or []:
                self.assertIn(key, schema['properties'], tool['name'])
        by_name = {tool['name']: tool['inputSchema'] for tool in tools}
        self.assertEqual(by_name['bot_market_list']['properties']['limit']['maximum'], 50)
        self.assertIn('confirm', by_name['bot_market_install'].get('required') or [])
        self.assertIn('enabled', by_name['bot_set_extension'].get('required') or [])

    def test_unknown_method_returns_32601(self):
        replies = self.session([{'jsonrpc': '2.0', 'id': 9, 'method': 'resources/list'}])
        self.assertEqual(replies[0]['error']['code'], -32601)
        self.assertIn('resources/list', replies[0]['error']['message'])

    def test_unknown_tool_is_an_error_result(self):
        reply = self.call('bot_reboot')
        self.assertTrue(reply['result']['isError'])
        self.assertIn('未知工具', reply['result']['content'][0]['text'])

    # ---- 原有工具 ------------------------------------------------------
    def test_status_summarizes_without_secrets(self):
        data = self.payload(self.call('bot_status', {'include_recent': True, 'recent_limit': 1}))
        self.assertTrue(data['running'])
        self.assertEqual(data['connection']['onebot']['nickname'], '小鲸')
        self.assertEqual(data['recent'][0]['text'], 'hi')
        self.assertNotIn('messages', data)
        self.assertNotIn('api_key', json.dumps(data))

    def test_start_stop_and_plugin_toggle(self):
        self.assertTrue(self.payload(self.call('bot_start'))['success'])
        self.assertTrue(self.payload(self.call('bot_stop'))['success'])
        result = self.payload(self.call('bot_set_plugin', {'id': 'ambient_reply',
                                                           'enabled': False}))
        self.assertTrue(result['success'])
        paths = [call[1] for call in FullBot.calls]
        self.assertIn('/api/start', paths)
        self.assertIn('/api/stop', paths)
        plugin_call = next(call for call in FullBot.calls
                           if call[1] == '/api/plugins/ambient_reply')
        self.assertEqual(plugin_call[2], {'enabled': False})

    def test_personas_and_activate(self):
        data = self.payload(self.call('bot_personas'))
        self.assertEqual(data['active'], 'whale')
        self.assertTrue(self.payload(self.call('bot_activate_persona',
                                               {'name': 'whale2'}))['success'])

    def test_send_text_uses_group_action(self):
        data = self.payload(self.call('bot_send_text', {'target_type': 'group',
                                                        'target_id': '123456',
                                                        'text': '测试'}))
        self.assertTrue(data['sent'])
        action = next(call for call in FullBot.calls if call[1] == '/api/onebot/action')
        self.assertEqual(action[2]['action'], 'send_group_msg')
        self.assertEqual(action[2]['params']['group_id'], 123456)

    def test_invalid_arguments_are_rejected_before_http(self):
        reply = self.call('bot_send_text', {'target_type': 'group', 'target_id': 'abc',
                                            'text': 'x'})
        self.assertTrue(reply['result']['isError'])
        self.assertFalse(FullBot.calls)

    def test_connection_error_is_explained(self):
        reply = self.call('bot_status', url='http://127.0.0.1:9')
        self.assertTrue(reply['result']['isError'])
        self.assertIn('连不上机器人控制台', reply['result']['content'][0]['text'])

    # ---- 编码回归：DSH 侧 server unavailable 的真因 ----------------------
    def test_stdout_is_utf8_with_plain_windows_env(self):
        """不靠父进程的 PYTHONUTF8/PYTHONIOENCODING，中文也必须按 UTF-8 出线。"""
        replies = self.session([{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                 'params': {}},
                                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                 'params': {'name': 'bot_personas', 'arguments': {}}}],
                               clean_env=True)
        self.assertIn('鲸鱼娘', replies[1]['result']['content'][0]['text'])
        self.assertIn('instructions', replies[0]['result'])

    # ---- 扩展市场 / 能力桥（端点已实现）--------------------------------
    def test_market_list_filters_and_caps_limit(self):
        data = self.payload(self.call('bot_market_list', {'category': 'tool',
                                                          'keyword': 'weather',
                                                          'limit': 5}))
        self.assertTrue(data['ok'], data)
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['items'][0]['id'], 'weather')
        self.assertTrue(data['items'][0]['installable'])
        posted = next(call for call in FullBot.calls if call[1] == '/api/qqagent/market')
        self.assertEqual(posted[2], {'refresh': False})

    def test_market_list_rejects_limit_over_50(self):
        data = self.payload(self.call('bot_market_list', {'limit': 51}))
        self.assertFalse(data['ok'])
        self.assertEqual(data['error']['code'], 'invalid_argument')
        self.assertNotIn('/api/qqagent/market', [call[1] for call in FullBot.calls])

    def test_market_install_requires_confirm(self):
        data = self.payload(self.call('bot_market_install', {'extension_id': 'weather'}))
        self.assertFalse(data['ok'])
        self.assertEqual(data['error']['code'], 'confirmation_required')
        self.assertNotIn('/api/qqagent/market/install', [call[1] for call in FullBot.calls])

    def test_market_install_with_confirm_posts(self):
        data = self.payload(self.call('bot_market_install', {'extension_id': 'weather',
                                                             'confirm': True}))
        self.assertTrue(data['ok'], data)
        self.assertTrue(data['installed'])
        self.assertEqual(data['source'], 'market')
        posted = next(call for call in FullBot.calls
                      if call[1] == '/api/qqagent/market/install')
        self.assertEqual(posted[2], {'id': 'weather'})

    def test_extensions_and_toggle(self):
        data = self.payload(self.call('bot_extensions'))
        self.assertTrue(data['ok'], data)
        self.assertEqual(data['count'], 2)
        self.assertTrue(data['items'][0]['enabled'])
        self.assertIsNone(data['items'][1]['enabled'])
        toggled = self.payload(self.call('bot_set_extension', {'id': 'weather',
                                                               'enabled': False}))
        self.assertTrue(toggled['ok'], toggled)
        self.assertEqual(toggled['action'], 'disable')
        self.assertFalse(toggled['enabled'])
        posted = next(call for call in FullBot.calls
                      if call[1] == '/api/qqagent/extensions/weather')
        self.assertEqual(posted[2], {'enabled': False})

    def test_bridge_status_reports_calls(self):
        data = self.payload(self.call('bot_bridge_status'))
        self.assertTrue(data['ok'], data)
        self.assertTrue(data['loaded'])
        self.assertEqual(data['providers'], ['openai', 'deepseek'])
        self.assertTrue(data['calls_available'])
        self.assertEqual(data['calls']['total'], 3)
        self.assertEqual(len(data['calls']['recent']), 1)

    def test_skill_tools_lists_tools(self):
        data = self.payload(self.call('bot_skill_tools'))
        self.assertTrue(data['ok'], data)
        self.assertEqual(data['count'], 3)
        self.assertEqual(data['tools'][0], {'skill': 'kb', 'name': 'kb_search',
                                            'description': 's'})

    # ---- 端点缺失：必须结构化 not_implemented，不能抛栈 ------------------
    def test_missing_endpoints_return_not_implemented(self):
        cases = [('bot_market_list', {}),
                 ('bot_market_install', {'extension_id': 'weather', 'confirm': True}),
                 ('bot_extensions', {}),
                 ('bot_set_extension', {'id': 'weather', 'enabled': True}),
                 ('bot_bridge_status', {}),
                 ('bot_skill_tools', {})]
        for name, arguments in cases:
            reply = self.call(name, arguments, url=self.legacy_url)
            self.assertFalse(reply['result']['isError'], name)
            data = self.payload(reply)
            self.assertFalse(data['ok'], name)
            self.assertEqual(data['error']['code'], 'not_implemented', name)
            self.assertTrue(data['error']['endpoints'], name)
        self.assertTrue(LegacyBot.calls)

    def test_console_down_is_reported_per_tool_kind(self):
        payload = self.payload(self.call('bot_extensions', url='http://127.0.0.1:9'))
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error']['code'], 'console_unavailable')


if __name__ == '__main__':
    unittest.main()
