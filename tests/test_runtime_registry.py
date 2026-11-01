"""多账号并发（切片 2）：每个账号起自己的后台端口，互不影响。

只验证"能起来、能响应、能单独停"，不连 QQ、不启动机器人。
"""
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import requests

from src import runtime_registry
from src.account_book import AccountBook
from src.runtime_registry import RuntimeRegistry


class RuntimeRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        (self.root / 'data' / 'qq-only').mkdir(parents=True)
        (self.root / 'config.qq.yaml').write_text(
            'ai:\n  model: demo\nqq:\n  mode: mock\nqq_only:\n  mode: mock\n  auto_start: false\n',
            encoding='utf-8')
        self.book = AccountBook(self.root)
        self.registry = RuntimeRegistry(self.root, self.book)
        self.addCleanup(self.registry.close)

    def test_default_account_uses_8090_without_starting(self):
        self.assertEqual(self.registry._port_for('default'), 8090)

    def test_second_account_starts_own_port_and_answers(self):
        created = self.book.add('小号')
        server = self.registry.ensure(created['id'])
        self.addCleanup(self.registry.stop, created['id'])
        self.assertNotEqual(server.port, 8090, '非默认账号不能占用 8090')
        self.assertTrue(server.running, server.error)
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            try:
                response = requests.get(f'http://127.0.0.1:{server.port}/api/status',
                                        timeout=2, proxies={'http': None, 'https': None})
                if response.status_code == 200:
                    status = response.json()
                    break
            except Exception:
                time.sleep(0.2)
        self.assertIsNotNone(status, '账号后台没有在 30 秒内响应')
        self.assertIn('running', status)

    def test_stop_one_account_does_not_touch_others(self):
        first = self.book.add('A')
        second = self.book.add('B')
        server_a = self.registry.ensure(first['id'])
        server_b = self.registry.ensure(second['id'])
        self.addCleanup(self.registry.stop, second['id'])
        self.assertNotEqual(server_a.port, server_b.port)
        self.registry.stop(first['id'])
        self.assertFalse(server_a.running)
        self.assertTrue(server_b.running, '停 A 不能影响 B')
        self.assertIn(second['id'], self.registry.servers)

    def test_status_reports_url_and_port(self):
        created = self.book.add('C')
        server = self.registry.ensure(created['id'])
        self.addCleanup(self.registry.stop, created['id'])
        entry = next(item for item in self.registry.status()
                     if item['account_id'] == created['id'])
        self.assertEqual(entry['url'], f"http://127.0.0.1:{server.port}")


class FreePortFallbackTests(unittest.TestCase):
    def test_reserved_range_falls_back_to_ephemeral_port(self):
        class ReservedSocket:
            def __init__(self):
                self.port = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def bind(self, address):
                if address[1] == 0:
                    self.port = 45678
                    return
                raise OSError(10013, 'reserved by Windows')

            def getsockname(self):
                return ('127.0.0.1', self.port)

        with mock.patch.object(runtime_registry.socket, 'socket', ReservedSocket):
            self.assertEqual(runtime_registry._free_port(8091), 45678)


if __name__ == '__main__':
    unittest.main()
