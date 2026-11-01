"""进程外 MCP 客户端（src/bot_control_api.py）的默认地址。

控制台端口可配置后，写死 27499 会让 MCP 的 whale_bot 工具打到没人监听的端口。
规则：WHALE_BOT_URL 优先（行为一字不变），其次跟 WHALEGIRL_PORT，再其次是控制台
启动时写下的端点文件，最后兜底 27499。
"""
import json
import os
import unittest
from contextlib import contextmanager
from unittest import mock

from src import bot_control_api


@contextmanager
def clean_env(**values):
    """只留本用例要设的两个变量，其余（含 shell 里的同名变量）先摘掉。

    同时把端点文件换成空候选：这些用例断言的是"环境变量这一层"的结果，
    机器上真开着一个控制台时也不该被它的端点文件带偏（端点文件另有专门用例）。
    """
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop('WHALE_BOT_URL', None)
        os.environ.pop('WHALEGIRL_PORT', None)
        for key, value in values.items():
            os.environ[key] = str(value)
        with mock.patch.object(bot_control_api, 'data_dir_candidates', return_value=[]):
            yield


class BaseUrlTests(unittest.TestCase):
    def test_explicit_url_wins_unchanged(self):
        with clean_env(WHALE_BOT_URL='http://127.0.0.1:19999', WHALEGIRL_PORT=28651):
            self.assertEqual(bot_control_api.base_url(), 'http://127.0.0.1:19999')

    def test_explicit_url_keeps_its_trailing_slash_trimmed(self):
        with clean_env(WHALE_BOT_URL='http://127.0.0.1:19999/'):
            self.assertEqual(bot_control_api.base_url(), 'http://127.0.0.1:19999')

    def test_follows_console_port_env(self):
        with clean_env(WHALEGIRL_PORT=28651):
            self.assertEqual(bot_control_api.base_url(), 'http://127.0.0.1:28651')

    def test_fallback_is_27499_when_nothing_set(self):
        with clean_env():
            self.assertEqual(bot_control_api.base_url(), 'http://127.0.0.1:27499')

    def test_empty_url_falls_through_to_console_port(self):
        with clean_env(WHALE_BOT_URL='', WHALEGIRL_PORT=28651):
            self.assertEqual(bot_control_api.base_url(), 'http://127.0.0.1:28651')

    def test_bad_port_env_degrades_to_27499_without_raising(self):
        for raw in ('abc', '0', '70000', ''):
            with self.subTest(raw=raw), clean_env(WHALEGIRL_PORT=raw):
                self.assertEqual(bot_control_api.base_url(), 'http://127.0.0.1:27499')


class EndpointFileTests(unittest.TestCase):
    """控制台改端口后不再需要重跑登记：跟着端点文件走，控制台关了自动退回默认端口。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / 'data'

    def base_url(self, **env):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ('WHALE_BOT_URL', 'WHALEGIRL_PORT'):
                os.environ.pop(key, None)
            for key, value in env.items():
                os.environ[key] = str(value)
            with mock.patch.object(bot_control_api, 'data_dir_candidates',
                                   return_value=[self.data]):
                return bot_control_api.base_url()

    def seed(self, port=28671, pid=None):
        bot_control_api.write_endpoint(port, self.data,
                                       pid=os.getpid() if pid is None else pid)

    def seed_raw(self, **fields):
        """绕过写入侧校验，造一份"文件里就写着坏端口"的端点文件。"""
        target = bot_control_api.endpoint_path(self.data)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = dict({'version': 1, 'host': '127.0.0.1', 'port': 28671,
                        'pid': os.getpid()}, **fields)
        target.write_text(json.dumps(payload), encoding='utf-8')

    def test_follows_endpoint_file_when_no_env(self):
        self.seed()
        self.assertEqual(self.base_url(), 'http://127.0.0.1:28671')

    def test_console_port_env_still_beats_endpoint_file(self):
        self.seed(28671)
        self.assertEqual(self.base_url(WHALEGIRL_PORT=28674), 'http://127.0.0.1:28674')

    def test_explicit_url_still_beats_endpoint_file(self):
        self.seed()
        self.assertEqual(self.base_url(WHALE_BOT_URL='http://127.0.0.1:19999'),
                         'http://127.0.0.1:19999')

    def test_dead_console_pid_is_not_adopted(self):
        self.seed(pid=0x7FFFFFFF)
        self.assertEqual(self.base_url(), 'http://127.0.0.1:27499')

    def test_bad_port_in_endpoint_file_is_not_adopted(self):
        for raw in (70000, 0, 'abc'):
            with self.subTest(raw=raw):
                self.seed_raw(port=raw)
                self.assertEqual(self.base_url(), 'http://127.0.0.1:27499')


if __name__ == '__main__':
    unittest.main()
