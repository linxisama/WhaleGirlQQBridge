"""默认账号的 running 必须如实反映后端是否在跑，url 不能指向没在监听的端口。

背景：/api/accounts 里默认账号原先恒为 running:true ——
{id:"default", port:27499, running:true, url:"http://127.0.0.1:27499"}，
而同进程 /api/status 报 running:false，27499 根本没有监听。

消费方（改前已核过）：qq_gui.py:6394 用 running 显示"运行中/未启动"，web 控制台
src/web/static/qq.js 只读 /api/status；没有任何消费方拿 url 当"一定能连上"的保证。
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
from src.qq_app import create_app


@contextmanager
def console_env(value=None):
    """把 WHALEGIRL_PORT 摘干净，用例不受 shell 影响。"""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop('WHALEGIRL_PORT', None)
        if value is not None:
            os.environ['WHALEGIRL_PORT'] = str(value)
        yield


def server(port, running):
    return SimpleNamespace(port=port, running=running)


class AccountsRunningStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        (self.root / 'config.qq.yaml').write_text('ai:\n  model: demo\nqq:\n  mode: mock\n',
                                                 encoding='utf-8')
        (self.root / 'data' / 'qq-only').mkdir(parents=True)

    def client(self, servers=None, bot=None, port=None):
        """不跑 lifespan：注册表显式给定，免得现场建 RuntimeRegistry 去写真实 DATA_ROOT。"""
        app = create_app()
        app.state.account_book = AccountBook(self.root)
        app.state.registry = SimpleNamespace(servers=dict(servers or {}))
        if port is not None:
            app.state.console_port = port
        if bot is not None:
            app.state.bot = bot
        return app, TestClient(app)

    def item(self, client, account_id='default'):
        return [entry for entry in client.get('/api/accounts').json()['items']
                if entry['id'] == account_id][0]

    def test_stopped_runtime_is_not_running(self):
        app, client = self.client(bot=SimpleNamespace(running=False), port=28661)
        with console_env(29999):
            entry = self.item(client)
        self.assertFalse(entry['running'], '后端停着却报 running:true 就是这次的缺陷')
        self.assertEqual(entry['port'], 28661)
        self.assertEqual(entry['url'], '', '没在跑就不要给出一个连不上的地址')

    def test_running_runtime_is_running(self):
        app, client = self.client(bot=SimpleNamespace(running=True), port=28662)
        with console_env(29999):
            entry = self.item(client)
        self.assertTrue(entry['running'])
        self.assertEqual(entry['url'], 'http://127.0.0.1:28662')

    def test_running_agrees_with_api_status(self):
        """/api/status 报的就是 bot.running：两个接口必须是同一个答案。"""
        app, client = self.client(port=28663)
        with console_env(29999):
            for flag in (True, False, True):
                app.state.bot = SimpleNamespace(running=flag)
                self.assertEqual(self.item(client)['running'], flag)

    def test_runtime_without_a_running_attribute_is_not_running(self):
        app, client = self.client(bot=SimpleNamespace(), port=28664)
        with console_env(29999):
            entry = self.item(client)
        self.assertFalse(entry['running'])
        self.assertEqual(entry['url'], '')

    def test_no_runtime_attached_reports_the_console_backend(self):
        """未走 lifespan 的进程内应用没有 bot：应答这个请求的就是控制台后端本身。"""
        app, client = self.client(port=28665)
        self.assertFalse(hasattr(app.state, 'bot'))
        with console_env(29999):
            entry = self.item(client)
        self.assertTrue(entry['running'])
        self.assertEqual(entry['url'], 'http://127.0.0.1:28665')

    def test_registry_server_running_state_wins(self):
        app, client = self.client(servers={'default': server(28666, True)},
                                  bot=SimpleNamespace(running=False), port=28665)
        entry = self.item(client)
        self.assertTrue(entry['running'])
        self.assertEqual(entry['port'], 28666)
        self.assertEqual(entry['url'], 'http://127.0.0.1:28666')

    def test_stopped_registry_server_is_not_running(self):
        app, client = self.client(servers={'default': server(28667, False)}, port=28665)
        entry = self.item(client)
        self.assertFalse(entry['running'])
        self.assertEqual(entry['port'], 28667)
        self.assertEqual(entry['url'], '')

    def test_other_accounts_without_a_server_are_not_running(self):
        app, client = self.client(bot=SimpleNamespace(running=True), port=28665)
        account_id = client.post('/api/accounts', json={'label': '小号'}).json()['item']['id']
        entry = self.item(client, account_id)
        self.assertFalse(entry['running'], '别的账号没有后端就不能跟着默认账号一起"在跑"')
        self.assertEqual(entry['port'], 0)
        self.assertEqual(entry['url'], '')


if __name__ == '__main__':
    unittest.main()
