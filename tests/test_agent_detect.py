"""W9a：只读探测本机 DSH（src/agent_detect.py）+ {prompt} 占位 + detect 端点。

三类断言：
1. agent_detect 的契约：永不抛、默认不起进程、item 可直接喂 validate_profile；
2. CliBackend 的 {prompt} 占位：prompt 进 argv、stdin 不再写、超长拒绝、
   没有占位时行为与今天完全一致（回归）；
3. GET /api/agent/backends/detect：200 / probe 开关 / 转发头 403 / 路由已注册。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI

from src import agent_backend_api as api
from src import agent_detect
from src.agent_backends import MAX_ARG_LENGTH, CliBackend, validate_profile
from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root

FAKE_CMD = 'C:/fake-npm/dsh.cmd'
NO_HOME = 'C:/definitely/missing/dsh'


def make_home(root, profiles=('web', 'headless', 'desktop', 'node_modules')):
    """造一个假 DSH_HOME：profiles 下若干目录 + 一个同名的普通文件。"""
    home = os.path.join(root, 'dsh-home')
    base = os.path.join(home, 'profiles')
    os.makedirs(base, exist_ok=True)
    for name in profiles:
        os.makedirs(os.path.join(base, name), exist_ok=True)
    with open(os.path.join(base, 'not-a-dir'), 'w', encoding='utf-8') as handle:
        handle.write('x')
    return home


def fake_exists(extra):
    """真实 FS + 一批凭空存在的路径。"""
    paths = set(extra)

    def _exists(path):
        return str(path) in paths or os.path.exists(path)

    return _exists


class DshHomeTests(unittest.TestCase):
    def test_dsh_home_defaults_to_dot_dsh_under_home(self):
        home = agent_detect.dsh_home({})
        self.assertTrue(os.path.isabs(home))
        self.assertEqual('.dsh', os.path.basename(home))

    def test_dsh_home_prefers_env(self):
        home = agent_detect.dsh_home({'DSH_HOME': 'C:/custom/dsh'})
        self.assertEqual(os.path.abspath('C:/custom/dsh'), home)

    def test_dsh_home_blank_env_falls_back(self):
        self.assertEqual(agent_detect.dsh_home({'DSH_HOME': '   '}),
                         agent_detect.dsh_home({}))

    def test_dsh_home_survives_broken_env(self):
        class Broken:
            def get(self, name, default=None):
                raise RuntimeError('boom')

        home = agent_detect.dsh_home(Broken())
        self.assertTrue(os.path.isabs(home), home)


class DshProfilesTests(unittest.TestCase):
    def test_headless_first_then_alphabetical(self):
        with tempfile.TemporaryDirectory() as temp:
            home = make_home(temp)
            self.assertEqual(['headless', 'desktop', 'web'],
                             agent_detect.dsh_profiles(home))

    def test_missing_root_and_profiles_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual([], agent_detect.dsh_profiles(temp))
        self.assertEqual([], agent_detect.dsh_profiles(''))
        self.assertEqual([], agent_detect.dsh_profiles(None))
        self.assertEqual([], agent_detect.dsh_profiles('C:/definitely/missing/dsh'))


class DetectShapeTests(unittest.TestCase):
    def test_item_keys_are_complete_and_json_safe(self):
        result = agent_detect.detect(which=lambda name: FAKE_CMD,
                                     env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME},
                                     exists=fake_exists([FAKE_CMD]))
        self.assertTrue(result['ok'])
        self.assertEqual(1, len(result['items']))
        item = result['items'][0]
        self.assertEqual(list(agent_detect.ITEM_KEYS), list(item))
        self.assertEqual('dsh', item['kind'])
        self.assertEqual('which', item['source'])
        self.assertEqual(os.path.abspath(FAKE_CMD), item['command'])
        json.dumps(result)          # 必须可 json 序列化
        self.assertIsInstance(result['elapsed_ms'], int)

    def test_no_profile_emits_default_item_with_placeholder_only(self):
        result = agent_detect.detect(which=lambda name: FAKE_CMD,
                                     env={'APPDATA': '', 'LOCALAPPDATA': '',
                                          'DSH_HOME': 'C:/definitely/missing/dsh'},
                                     exists=fake_exists([FAKE_CMD]))
        item = result['items'][0]
        self.assertEqual('dsh-default', item['id'])
        self.assertEqual('', item['profile'])
        self.assertEqual(['{prompt}'], item['args'])

    def test_profiles_become_one_item_each_with_profile_args(self):
        with tempfile.TemporaryDirectory() as temp:
            home = make_home(temp)
            result = agent_detect.detect(which=lambda name: FAKE_CMD,
                                         env={'APPDATA': '', 'LOCALAPPDATA': '',
                                              'DSH_HOME': home},
                                         exists=fake_exists([FAKE_CMD]))
            self.assertEqual(['dsh-headless', 'dsh-desktop', 'dsh-web'],
                             [item['id'] for item in result['items']])
            self.assertEqual(['--profile', 'headless', '{prompt}'],
                             result['items'][0]['args'])
            self.assertEqual(['--profile', 'web', '{prompt}'],
                             result['items'][2]['args'])

    def test_items_feed_validate_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            home = make_home(temp, profiles=('headless',))
            result = agent_detect.detect(which=lambda name: FAKE_CMD,
                                         env={'APPDATA': '', 'LOCALAPPDATA': '',
                                              'DSH_HOME': home},
                                         exists=fake_exists([FAKE_CMD]))
        item = result['items'][0]
        profile = {'command': item['command'], 'args': list(item['args']),
                   'workdir': item['workdir'], 'env': {}, 'timeout': 120.0,
                   'protocol': 'none'}
        checked = validate_profile(item['kind'], profile)
        self.assertTrue(checked['ok'], checked)
        self.assertEqual('dsh', checked['kind'])

    def test_item_ids_are_unique_and_charset_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            home = make_home(temp, profiles=('headless', 'my profile!', '中文'))
            result = agent_detect.detect(which=lambda name: FAKE_CMD,
                                         env={'APPDATA': '', 'LOCALAPPDATA': '',
                                              'DSH_HOME': home},
                                         exists=fake_exists([FAKE_CMD]))
        ids = [item['id'] for item in result['items']]
        self.assertEqual(len(ids), len(set(ids)))
        for identifier in ids:
            self.assertRegex(identifier, r'^[A-Za-z0-9_-]{1,64}$')


class DetectCandidateTests(unittest.TestCase):
    def test_dedupes_same_absolute_path_from_two_sources(self):
        # which 与 %APPDATA%/npm 指向同一个文件（大小写不同）：只留一条命令。
        appdata = os.path.dirname(os.path.dirname(FAKE_CMD))
        result = agent_detect.detect(
            which=lambda name: FAKE_CMD,
            env={'APPDATA': appdata, 'LOCALAPPDATA': '',
                 'DSH_HOME': 'C:/definitely/missing/dsh'},
            exists=fake_exists([FAKE_CMD, FAKE_CMD.lower(), FAKE_CMD.upper()]))
        self.assertEqual(1, len(result['items']), result['items'])
        self.assertEqual('which', result['items'][0]['source'])

    def test_directory_candidate_finds_entry_inside(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = os.path.join(temp, 'Programs', 'DeepSeek Harness', 'resources',
                                  'app.asar', 'dsh')
            os.makedirs(bundle)
            entry = os.path.join(bundle, 'dsh.cmd')
            with open(entry, 'w', encoding='utf-8') as handle:
                handle.write('@echo off\r\n')
            result = agent_detect.detect(which=lambda name: None,
                                         env={'APPDATA': '', 'LOCALAPPDATA': temp,
                                              'DSH_HOME': NO_HOME})
        self.assertEqual(1, len(result['items']))
        self.assertEqual(os.path.abspath(entry), result['items'][0]['command'])
        self.assertEqual('localappdata-asar', result['items'][0]['source'])

    def test_zero_candidates_reports_reason_and_scanned(self):
        result = agent_detect.detect(which=lambda name: None,
                                     env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME})
        self.assertTrue(result['ok'])
        self.assertEqual([], result['items'])
        self.assertTrue(result['reason'])
        self.assertIn('已扫描', result['reason'])
        self.assertTrue(result['scanned'], '至少要记录扫过的 DSH 家目录')
        self.assertTrue(any(path.endswith('profiles') for path in result['scanned']),
                        result['scanned'])

    def test_dsh_items_come_with_workdir_and_source(self):
        result = agent_detect.detect(which=lambda name: FAKE_CMD,
                                     env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME},
                                     exists=fake_exists([FAKE_CMD]))
        self.assertEqual('', result['items'][0]['workdir'])
        self.assertTrue(result['items'][0]['label'])


class DetectRobustnessTests(unittest.TestCase):
    def test_default_does_not_spawn_any_process(self):
        with mock.patch.object(agent_detect.subprocess, 'run',
                               side_effect=AssertionError('默认不许起进程')):
            result = agent_detect.detect(which=lambda name: FAKE_CMD,
                                         env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME},
                                         exists=fake_exists([FAKE_CMD]))
        self.assertEqual(1, len(result['items']))
        self.assertEqual('', result['items'][0]['version'])

    def test_never_raises_when_which_explodes(self):
        def boom(name):
            raise RuntimeError('which 炸了')

        result = agent_detect.detect(which=boom,
                                     env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME})
        self.assertTrue(result['ok'])
        self.assertEqual([], result['items'])

    def test_never_raises_when_exists_explodes(self):
        def boom(path):
            raise RuntimeError('exists 炸了')

        result = agent_detect.detect(which=lambda name: FAKE_CMD,
                                     env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME},
                                     exists=boom)
        self.assertTrue(result['ok'])
        self.assertEqual([], result['items'])
        self.assertIn('探测异常', result['reason'])


class DetectProbeTests(unittest.TestCase):
    def test_probe_fills_version_first_line(self):
        fake = SimpleNamespace(stdout='0.1.5-rc.3\nnoise\n', stderr='', returncode=0)
        with mock.patch.object(agent_detect.subprocess, 'run', return_value=fake) as run:
            result = agent_detect.detect(probe=True, which=lambda name: FAKE_CMD,
                                         env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME},
                                         exists=fake_exists([FAKE_CMD]))
        self.assertEqual('0.1.5-rc.3', result['items'][0]['version'])
        self.assertEqual('', result['items'][0]['reason'])
        argv = run.call_args[0][0]
        self.assertEqual([os.path.abspath(FAKE_CMD), '--version'], argv)

    def test_probe_timeout_keeps_item_and_reason(self):
        with mock.patch.object(agent_detect.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('dsh', 15)):
            result = agent_detect.detect(probe=True, which=lambda name: FAKE_CMD,
                                         env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME},
                                         exists=fake_exists([FAKE_CMD]))
        item = result['items'][0]
        self.assertEqual('', item['version'])
        self.assertIn('超过', item['reason'])
        self.assertTrue(item['ok'])

    def test_probe_nonzero_exit_keeps_version_and_reason(self):
        fake = SimpleNamespace(stdout='0.1.5\n', stderr='', returncode=3)
        with mock.patch.object(agent_detect.subprocess, 'run', return_value=fake):
            result = agent_detect.detect(probe=True, which=lambda name: FAKE_CMD,
                                         env={'APPDATA': '', 'LOCALAPPDATA': '', 'DSH_HOME': NO_HOME},
                                         exists=fake_exists([FAKE_CMD]))
        item = result['items'][0]
        self.assertEqual('0.1.5', item['version'])
        self.assertIn('退出码 3', item['reason'])

    def test_probe_caps_at_three_commands(self):
        paths = ['C:/c%d/dsh.cmd' % index for index in range(4)]
        entries = [(path, 'which') for path in paths]
        fake = SimpleNamespace(stdout='9.9.9\n', stderr='', returncode=0)
        with mock.patch.object(agent_detect, '_candidate_paths', return_value=entries), \
                mock.patch.object(agent_detect.subprocess, 'run', return_value=fake) as run:
            result = agent_detect.detect(probe=True,
                                         env={'APPDATA': '', 'LOCALAPPDATA': '',
                                              'DSH_HOME': 'C:/definitely/missing/dsh'},
                                         exists=lambda path: str(path) in paths)
        self.assertEqual(4, len(result['items']))
        self.assertEqual(agent_detect.MAX_PROBE_CANDIDATES, run.call_count)
        self.assertEqual('9.9.9', result['items'][0]['version'])
        self.assertEqual('', result['items'][3]['version'])


class PromptPlaceholderTests(unittest.TestCase):
    """{prompt}：进 argv、不写 stdin、超长拒绝；没有占位时行为不变。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data_dir = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def backend(self, backend_id, args):
        return CliBackend(backend_id, {'command': sys.executable, 'args': list(args),
                                       'timeout': 30}, data_dir=self.data_dir)

    def test_build_argv_substitutes_placeholder(self):
        backend = self.backend('ph', ['--profile', 'headless', '{prompt}'])
        argv = backend._build_argv(sys.executable, (), prompt='run the tests')
        self.assertEqual([sys.executable, '--profile', 'headless', 'run the tests'], argv)

    def test_build_argv_keeps_literal_without_prompt(self):
        backend = self.backend('ph2', ['{prompt}'])
        self.assertEqual([sys.executable, '{prompt}'],
                         backend._build_argv(sys.executable, ()))
        self.assertTrue(backend._uses_prompt_placeholder())

    def test_start_passes_prompt_as_argv_and_keeps_stdin_closed(self):
        script = ("import sys;"
                  "data=sys.stdin.read();"
                  "print('ARGV=%r' % (sys.argv[1:],));"
                  "print('STDIN=%r' % (data,))")
        backend = self.backend('argv-run', ['-c', script, '{prompt}'])
        result = backend.start('hello-argv')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['prompt_in_argv'])
        self.assertIn("ARGV=['hello-argv']", result['stdout'])
        self.assertIn("STDIN=''", result['stdout'])
        self.assertEqual([sys.executable, '-c', script, 'hello-argv'],
                         [str(part) for part in result['argv']])

    def test_start_without_placeholder_still_writes_stdin(self):
        script = ("import sys;"
                  "print('STDIN=%r' % (sys.stdin.read(),));"
                  "print('ARGV=%r' % (sys.argv[1:],))")
        backend = self.backend('stdin-run', ['-c', script])
        result = backend.start('hello-stdin')
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['prompt_in_argv'])
        self.assertIn("STDIN='hello-stdin'", result['stdout'])
        self.assertIn('ARGV=[]', result['stdout'])

    def test_start_rejects_too_long_prompt_with_placeholder(self):
        backend = self.backend('long', ['-c', 'print(1)', '{prompt}'])
        started = time.time()
        result = backend.start('x' * (MAX_ARG_LENGTH + 1))
        self.assertFalse(result['ok'])
        self.assertEqual('bad_profile', result['code'])
        self.assertIn(str(MAX_ARG_LENGTH), result['message'])
        self.assertLess(time.time() - started, 5.0, '超长 prompt 不许起进程')

    def test_long_prompt_without_placeholder_is_still_allowed(self):
        script = "import sys;print(len(sys.stdin.read()))"
        backend = self.backend('long-stdin', ['-c', script])
        result = backend.start('y' * (MAX_ARG_LENGTH + 50))
        self.assertTrue(result['ok'], result)
        self.assertIn(str(MAX_ARG_LENGTH + 50), result['stdout'])


class DetectApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api._reset_for_tests()
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.bot.close()
        await asyncio_sleep()
        self.temp.cleanup()

    async def test_detect_route_is_registered(self):
        paths = [getattr(route, 'path', '') for route in self.app.routes]
        self.assertIn('/api/agent/backends/detect', paths)

    async def test_detect_defaults_to_no_probe(self):
        response = await self.client.get('/api/agent/backends/detect')
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertFalse(body['probe'])
        self.assertIsInstance(body['items'], list)
        self.assertIsInstance(body['scanned'], list)
        self.assertIsInstance(body['elapsed_ms'], int)
        for item in body['items']:
            self.assertEqual(list(agent_detect.ITEM_KEYS), list(item))

    async def test_detect_probe_flag_runs_version_and_fills_it(self):
        response = await self.client.get('/api/agent/backends/detect?probe=1')
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body['probe'])
        for item in body['items']:
            self.assertTrue(item['version'] or item['reason'], item)

    async def test_detect_rejects_forwarded_requests(self):
        # 宿主中间件先拦一遍（403），router 自己的 _require_local 也要拦一遍。
        forwarded = await self.client.get('/api/agent/backends/detect',
                                         headers={'x-forwarded-for': '10.0.0.9'})
        self.assertEqual(403, forwarded.status_code, forwarded.text)
        bare = FastAPI()
        bare.state.bot = self.bot
        api.install_agent_backend_routes(bare)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=bare),
                                   base_url='http://127.0.0.1')
        try:
            response = await client.get('/api/agent/backends/detect',
                                        headers={'x-forwarded-for': '10.0.0.9'})
        finally:
            await client.aclose()
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual('auth_denied', response.json()['code'])


async def asyncio_sleep():
    import asyncio
    await asyncio.sleep(.05)


if __name__ == '__main__':
    unittest.main()
