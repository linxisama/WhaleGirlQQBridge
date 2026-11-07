"""连接链路健壮性：管理器探活、连接复用、控制台客户端错误口径。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import gateway_link
from src import local_agent_bridge as bridge
from src.gui.console_client import ConsoleApiClient, ConsoleApiError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b'x'):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = content

    def json(self):
        return self._payload


class FakeSession:
    """记录调用并返回预置响应/异常的假 session。"""

    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        pass


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self._orig = gateway_link._SESSION
        gateway_link._SESSION = None

    def tearDown(self):
        gateway_link._SESSION = self._orig

    def _use(self, session):
        gateway_link._SESSION = session

    def test_ok_probe_reports_status_and_elapsed(self):
        self._use(FakeSession(FakeResponse(200)))
        result = gateway_link.probe('http://127.0.0.1:1/')
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'ok')
        self.assertIn('elapsed_ms', result)
        self.assertEqual(result['http_status'], 200)

    def test_trailing_slash_is_stripped(self):
        session = FakeSession(FakeResponse(200))
        self._use(session)
        gateway_link.probe('http://127.0.0.1:1/')
        self.assertEqual(session.calls[0][1], 'http://127.0.0.1:1/v1/accounts')

    def test_unauthorized_is_distinguished_from_unreachable(self):
        self._use(FakeSession(FakeResponse(401)))
        self.assertEqual(gateway_link.probe('http://h')['status'], 'unauthorized')
        self._use(FakeSession(error=OSError('refused')))
        self.assertEqual(gateway_link.probe('http://h')['status'], 'unreachable')

    def test_http_error_status(self):
        self._use(FakeSession(FakeResponse(500)))
        result = gateway_link.probe('http://h')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'http_error')

    def test_empty_url_is_no_config(self):
        result = gateway_link.probe('')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no_config')

    def test_probe_never_raises(self):
        self._use(FakeSession(error=RuntimeError('boom')))
        result = gateway_link.probe('http://h')
        self.assertFalse(result['ok'])

    def test_token_becomes_bearer_header(self):
        session = FakeSession(FakeResponse(200))
        self._use(session)
        gateway_link.probe('http://h', token='abc')
        self.assertEqual(session.calls[0][2]['headers']['Authorization'], 'Bearer abc')

    def test_no_token_sends_no_header(self):
        session = FakeSession(FakeResponse(200))
        self._use(session)
        gateway_link.probe('http://h')
        self.assertEqual(session.calls[0][2]['headers'], {})


class DescribeFailureTests(unittest.TestCase):
    def test_every_status_has_a_sentence(self):
        for status in ('ok', 'no_config', 'unauthorized', 'http_error', 'unreachable'):
            text = gateway_link.describe_failure({'status': status, 'message': 'x'})
            self.assertTrue(text.strip(), status)

    def test_unknown_status_falls_back_to_message(self):
        self.assertEqual(gateway_link.describe_failure({'status': '?', 'message': '细节'}), '细节')

    def test_empty_result_does_not_crash(self):
        self.assertIsInstance(gateway_link.describe_failure({}), str)


class SessionReuseTests(unittest.TestCase):
    def setUp(self):
        self._orig = gateway_link._SESSION
        gateway_link.reset_session()

    def tearDown(self):
        gateway_link._SESSION = self._orig

    def test_session_is_reused(self):
        first = gateway_link._session()
        self.assertIs(gateway_link._session(), first)

    def test_session_does_not_read_proxy_env(self):
        self.assertFalse(gateway_link._session().trust_env)

    def test_reset_drops_the_session(self):
        first = gateway_link._session()
        gateway_link.reset_session()
        self.assertIsNot(gateway_link._session(), first)

    def test_fetch_accounts_uses_shared_session(self):
        session = FakeSession(FakeResponse(200, {'items': [1], 'active': 'a'}))
        gateway_link._SESSION = session
        result = gateway_link.fetch_accounts({'base_url': 'http://h', 'token': 't'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'], [1])
        self.assertEqual(len(session.calls), 1)

    def test_fetch_accounts_still_reports_missing_config(self):
        result = gateway_link.fetch_accounts({})
        self.assertFalse(result['ok'])
        self.assertEqual(result['message'], '没有管理器连接信息')

    def test_start_headless_bot_uses_shared_session(self):
        session = FakeSession(FakeResponse(200, {'port': 27499}))
        gateway_link._SESSION = session
        result = gateway_link.start_headless_bot({'base_url': 'http://h', 'token': 't'}, '1', 'qq')
        self.assertTrue(result['ok'])
        self.assertEqual(result['port'], 27499)


class BridgeInvalidateTests(unittest.TestCase):
    def setUp(self):
        bridge.clear_cache()

    def tearDown(self):
        bridge.clear_cache()

    def _seed(self):
        with bridge._CACHE_LOCK:
            bridge._CACHE[('h1', 'desktop')] = (0.0, {'a': 1})
            bridge._CACHE[('h1', 'other')] = (0.0, {'a': 2})
            bridge._CACHE[('h2', 'desktop')] = (0.0, {'a': 3})

    def test_invalidate_one_profile(self):
        self._seed()
        bridge.invalidate(home='h1', profile='desktop')
        with bridge._CACHE_LOCK:
            self.assertNotIn(('h1', 'desktop'), bridge._CACHE)
            self.assertIn(('h1', 'other'), bridge._CACHE)

    def test_invalidate_whole_home(self):
        self._seed()
        bridge.invalidate(home='h1')
        with bridge._CACHE_LOCK:
            self.assertEqual([k for k in bridge._CACHE if k[0] == 'h1'], [])
            self.assertIn(('h2', 'desktop'), bridge._CACHE)

    def test_invalidate_all(self):
        self._seed()
        bridge.invalidate()
        with bridge._CACHE_LOCK:
            self.assertEqual(dict(bridge._CACHE), {})

    def test_invalidate_unknown_key_is_noop(self):
        self._seed()
        bridge.invalidate(home='nope')
        with bridge._CACHE_LOCK:
            self.assertEqual(len(bridge._CACHE), 3)


class ConsoleClientTests(unittest.TestCase):
    def test_empty_detail_is_not_a_failure(self):
        client = ConsoleApiClient(lambda p: {'success': True, 'detail': ''}, lambda p, d: {})
        self.assertTrue(client.get('/api/x')['success'])

    def test_detail_with_text_still_raises(self):
        client = ConsoleApiClient(lambda p: {'detail': '令牌过期'}, lambda p, d: {})
        with self.assertRaises(ConsoleApiError) as caught:
            client.get('/api/x')
        self.assertIn('令牌过期', str(caught.exception))

    def test_success_false_without_detail_uses_default_text(self):
        client = ConsoleApiClient(lambda p: {'success': False}, lambda p, d: {})
        with self.assertRaises(ConsoleApiError) as caught:
            client.get('/api/x')
        self.assertIn('请求未完成', str(caught.exception))

    def test_last_error_records_and_clears(self):
        state = {'fail': True}
        def get(path):
            return {'detail': '坏了'} if state['fail'] else {'success': True}
        client = ConsoleApiClient(get, lambda p, d: {})
        with self.assertRaises(ConsoleApiError):
            client.get('/api/x')
        self.assertIn('坏了', client.last_error())
        state['fail'] = False
        client.get('/api/x')
        self.assertEqual(client.last_error(), '')

    def test_transport_exception_becomes_console_error(self):
        def boom(path):
            raise OSError('connection reset')
        client = ConsoleApiClient(boom, lambda p, d: {})
        with self.assertRaises(ConsoleApiError) as caught:
            client.get('/api/x')
        self.assertIn('connection reset', str(caught.exception))
        self.assertIn('OSError', client.last_error())

    def test_empty_result_still_rejected(self):
        client = ConsoleApiClient(lambda p: {}, lambda p, d: {})
        with self.assertRaises(ConsoleApiError):
            client.get('/api/x')

    def test_bad_path_rejected(self):
        client = ConsoleApiClient(lambda p: {}, lambda p, d: {})
        with self.assertRaises(ConsoleApiError):
            client.get('x/y')

    def test_post_passes_payload_through(self):
        seen = {}
        def post(path, data):
            seen['path'], seen['data'] = path, data
            return {'success': True}
        client = ConsoleApiClient(lambda p: {}, post)
        client.post('/api/a/b', {'k': 1})
        self.assertEqual(seen['path'], 'a/b')
        self.assertEqual(seen['data'], {'k': 1})

    def test_post_without_data_sends_empty_dict(self):
        seen = {}
        def post(path, data):
            seen['data'] = data
            return {'success': True}
        ConsoleApiClient(lambda p: {}, post).post('/api/a')
        self.assertEqual(seen['data'], {})


if __name__ == '__main__':
    unittest.main()