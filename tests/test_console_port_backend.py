"""后端侧端口真相源：src/qq_app.py 的 console_port() 与 /api/accounts 的默认账号端口。

控制台端口由 qq_gui.resolve_port() 决定，进程内启动时写进 app.state.console_port，
子进程启动时写成 WHALEGIRL_PORT —— 两条路都要被后端认出；都不知道才回落到 27499。
/api/accounts 里"默认账号"原先恒定报 8090，这里钉住它跟随 console_port。
"""
import os
import pathlib
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from src.account_book import AccountBook
from src.qq_app import console_port, create_app


@contextmanager
def console_env(value=None):
    """把 WHALEGIRL_PORT 从环境里摘干净，用例不受 shell 影响。"""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop('WHALEGIRL_PORT', None)
        if value is not None:
            os.environ['WHALEGIRL_PORT'] = str(value)
        yield


def fake_app(state_port=None, has_state_port=True):
    state = SimpleNamespace()
    if has_state_port:
        state.console_port = state_port
    return SimpleNamespace(state=state)


class ConsolePortFunctionTests(unittest.TestCase):
    def test_state_port_wins_over_env(self):
        with console_env(29999):
            self.assertEqual(console_port(fake_app(28651)), 28651)

    def test_env_used_when_state_has_none(self):
        with console_env(28652):
            self.assertEqual(console_port(fake_app()), 28652)
            self.assertEqual(console_port(fake_app(None)), 28652)

    def test_fallback_is_27499_when_nothing_known(self):
        with console_env(None):
            self.assertEqual(console_port(fake_app()), 27499)

    def test_bad_state_value_falls_through_to_env(self):
        with console_env(28653):
            self.assertEqual(console_port(fake_app('abc')), 28653)

    def test_bad_env_values_degrade_to_27499(self):
        for raw in ('abc', '0', '70000', '-1', ''):
            with self.subTest(raw=raw), console_env(raw):
                self.assertEqual(console_port(fake_app()), 27499)


class AccountsDefaultPortTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        (self.root / 'config.qq.yaml').write_text('ai:\n  model: demo\nqq:\n  mode: mock\n',
                                                 encoding='utf-8')
        (self.root / 'data' / 'qq-only').mkdir(parents=True)

    def client(self):
        """不跑 lifespan：拿到的就是"控制台刚打开、注册表还是空"的那个状态。"""
        app = create_app()
        app.state.account_book = AccountBook(self.root)
        return app, TestClient(app)

    def default_item(self, payload):
        return [item for item in payload['items'] if item['id'] == 'default'][0]

    def test_default_account_port_follows_console_port(self):
        app, client = self.client()
        self.assertFalse(hasattr(app.state, 'registry'))
        with console_env(28654):
            item = self.default_item(client.get('/api/accounts').json())
        self.assertEqual(item['port'], 28654)
        self.assertEqual(item['url'], 'http://127.0.0.1:28654')
        self.assertTrue(item['running'])

    def test_default_account_port_uses_state_console_port(self):
        app, client = self.client()
        app.state.console_port = 28655
        with console_env(29999):
            item = self.default_item(client.get('/api/accounts').json())
        self.assertEqual(item['port'], 28655)

    def test_live_default_server_port_wins(self):
        app, client = self.client()
        app.state.registry = SimpleNamespace(
            servers={'default': SimpleNamespace(port=28666, running=True)})
        with console_env(28654):
            item = self.default_item(client.get('/api/accounts').json())
        self.assertEqual(item['port'], 28666)
        self.assertEqual(item['url'], 'http://127.0.0.1:28666')

    def test_other_accounts_without_server_stay_zero(self):
        app, client = self.client()
        account_id = client.post('/api/accounts', json={'label': '小号'}).json()['item']['id']
        app.state.registry = SimpleNamespace(servers={})
        with console_env(28654):
            items = {item['id']: item for item in client.get('/api/accounts').json()['items']}
        self.assertEqual(items['default']['port'], 28654)
        self.assertEqual(items[account_id]['port'], 0)


if __name__ == '__main__':
    unittest.main()
