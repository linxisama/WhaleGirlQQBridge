"""控制台端点文件：控制台把**真实监听的端口**写进去，外部 MCP 运行期跟着它走。

要消灭的 footgun：用户用 --port / WHALEGIRL_PORT 改了控制台端口，而 DSH profile 里
写死的旧地址还在，MCP 工具就静默打到没人监听的端口。所以端口不再由登记侧保存，
而是控制台启动时落盘 + 运行期读取，采不采用由 pid 探活决定。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src import bot_control_api as api  # noqa: E402

LOGGER = 'src.bot_control_api'


class TempDataCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / 'data'

    @property
    def endpoint(self) -> Path:
        return self.data / 'qq-only' / 'console_endpoint.json'

    def seed(self, payload):
        """直接落一份端点文件（不经过 write_endpoint，方便造脏数据）。"""
        self.endpoint.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.endpoint.write_text(text, encoding='utf-8')


class WriteEndpointTest(TempDataCase):
    def test_writes_the_documented_shape(self):
        path = api.write_endpoint(28671, self.data, pid=4321, started_at='2026-01-01T08:00:00+08:00')
        self.assertEqual(path, self.endpoint)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                         {'version': 1, 'host': '127.0.0.1', 'port': 28671, 'pid': 4321,
                          'started_at': '2026-01-01T08:00:00+08:00'})

    def test_defaults_are_current_pid_and_iso_started_at(self):
        item = json.loads(api.write_endpoint(28671, self.data).read_text(encoding='utf-8'))
        self.assertEqual(item['pid'], os.getpid())
        self.assertIn('T', item['started_at'])
        self.assertEqual(item['version'], 1)

    def test_atomic_write_leaves_no_tmp_behind(self):
        api.write_endpoint(28671, self.data, pid=4321)
        self.assertFalse((self.endpoint.parent / (self.endpoint.name + '.tmp')).exists())

    def test_rewrite_replaces_previous_content(self):
        api.write_endpoint(28671, self.data, pid=1)
        api.write_endpoint(28674, self.data, pid=2)
        item = json.loads(self.endpoint.read_text(encoding='utf-8'))
        self.assertEqual((item['port'], item['pid']), (28674, 2))

    def test_invalid_port_writes_nothing(self):
        for raw in ('abc', 0, 70000, '', None):
            with self.subTest(raw=raw):
                with mock.patch.object(api, '_LOG') as log:
                    self.assertIsNone(api.write_endpoint(raw, self.data))
                log.warning.assert_called()
                self.assertFalse(self.endpoint.exists())

    def test_write_failure_is_logged_and_never_raises(self):
        blocker = Path(self._tmp.name) / 'blocker'
        blocker.write_text('not a directory', encoding='utf-8')
        with self.assertLogs(LOGGER, level='WARNING'):
            self.assertIsNone(api.write_endpoint(28671, blocker / 'data'))


class EndpointUrlTest(TempDataCase):
    def resolve(self, alive=True):
        return api._endpoint_url(data_dir=self.data, is_alive=lambda pid: alive)

    def test_uses_endpoint_port_when_pid_is_alive(self):
        self.seed({'version': 1, 'host': '127.0.0.1', 'port': 28671, 'pid': 4321})
        self.assertEqual(self.resolve(alive=True), 'http://127.0.0.1:28671')

    def test_dead_pid_is_not_adopted(self):
        self.seed({'version': 1, 'port': 28671, 'pid': 4321})
        self.assertIsNone(self.resolve(alive=False))

    def test_invalid_port_is_not_adopted(self):
        for raw in ('abc', 0, 70000, '', None):
            with self.subTest(raw=raw):
                self.seed({'version': 1, 'port': raw, 'pid': 4321})
                self.assertIsNone(self.resolve())

    def test_corrupt_file_is_ignored(self):
        for text in ('{', '[]', '"x"', 'null', '{"port": }'):
            with self.subTest(text=text):
                self.seed(text)
                self.assertIsNone(self.resolve())

    def test_missing_file_is_ignored(self):
        self.assertIsNone(self.resolve())

    def test_alive_check_that_raises_counts_as_dead(self):
        self.seed({'version': 1, 'port': 28671, 'pid': 4321})

        def boom(pid):
            raise RuntimeError('探活炸了')

        self.assertIsNone(api._endpoint_url(data_dir=self.data, is_alive=boom))

    def test_missing_pid_field_is_not_adopted(self):
        """没有 pid 就无从判断控制台还在不在，一律不采用（探活都不用调）。"""
        self.seed({'version': 1, 'port': 28671})
        seen = []
        self.assertIsNone(api._endpoint_url(data_dir=self.data,
                                            is_alive=lambda pid: seen.append(pid) or True))
        self.assertEqual(seen, [], '没有 pid 就不该去探活')

    def test_foreign_host_is_forced_back_to_loopback(self):
        self.seed({'version': 1, 'host': '10.0.0.9', 'port': 28671, 'pid': 4321})
        self.assertEqual(self.resolve(), 'http://127.0.0.1:28671')


class PidAliveTest(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(api._pid_alive(os.getpid()))

    def test_unusable_pids_are_dead(self):
        for raw in (0, -1, 0x7FFFFFFF, 'abc', '', None):
            with self.subTest(raw=raw):
                self.assertFalse(api._pid_alive(raw))


class BaseUrlPriorityTest(TempDataCase):
    """四级优先级：WHALE_BOT_URL > WHALEGIRL_PORT > 端点文件 > 27499。"""

    def seed_live(self, port=28671):
        self.seed({'version': 1, 'host': '127.0.0.1', 'port': port, 'pid': os.getpid()})

    def base_url(self, **env):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ('WHALE_BOT_URL', 'WHALEGIRL_PORT'):
                os.environ.pop(key, None)
            for key, value in env.items():
                os.environ[key] = str(value)
            with mock.patch.object(api, 'data_dir_candidates', return_value=[self.data]):
                return api.base_url()

    def test_endpoint_is_followed_when_env_is_clean(self):
        self.seed_live()
        self.assertEqual(self.base_url(), 'http://127.0.0.1:28671')

    def test_dead_pid_falls_back_to_default_port(self):
        self.seed({'version': 1, 'host': '127.0.0.1', 'port': 28671, 'pid': 0x7FFFFFFF})
        self.assertEqual(self.base_url(), 'http://127.0.0.1:27499')

    def test_console_port_env_beats_the_endpoint(self):
        self.seed_live(28671)
        self.assertEqual(self.base_url(WHALEGIRL_PORT=28674), 'http://127.0.0.1:28674')

    def test_explicit_url_beats_everything(self):
        self.seed_live(28671)
        self.assertEqual(self.base_url(WHALE_BOT_URL='http://127.0.0.1:19999/',
                                       WHALEGIRL_PORT=28674), 'http://127.0.0.1:19999')

    def test_broken_endpoint_degrades_without_raising(self):
        self.seed('{oops')
        self.assertEqual(self.base_url(), 'http://127.0.0.1:27499')


class GuiWiringTest(unittest.TestCase):
    """两条启动路径（子进程 / 进程内 uvicorn）都要写端点文件。"""

    def test_both_start_paths_write_the_endpoint(self):
        text = (REPO / 'qq_gui.py').read_text(encoding='utf-8')
        self.assertIn('from src.bot_control_api import write_endpoint', text)
        self.assertEqual(text.count('self._write_console_endpoint()'), 2)


if __name__ == '__main__':
    unittest.main()
