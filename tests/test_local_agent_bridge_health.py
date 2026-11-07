"""local_agent_bridge 的探活 / 缓存 / 状态分级 / 问题文案。"""
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import local_agent_bridge as local

BOT_ENTRY = """
- insert:
    - id: mcp-whale-bot
      name: "@local/whale-bot"
      config:
        command: '{command}'
        args:
          - '{script}'
        env:
          WHALE_BOT_URL: '{url}'
{extra}
"""
REPO = Path(__file__).resolve().parents[1]
BOT_SCRIPT = REPO / 'tools' / 'whale_bot_mcp.py'


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / '.dsh'
        self.profile_dir = self.home / 'profiles' / 'desktop'
        local.clear_cache()

    def tearDown(self):
        local.clear_cache()
        self.tmp.cleanup()

    def write_patch(self, command=sys.executable, script=str(BOT_SCRIPT),
                    url='http://127.0.0.1:1', extra='          PYTHONUTF8: "1"'):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        text = BOT_ENTRY.format(command=command, script=script, url=url, extra=extra)
        (self.profile_dir / 'cordis.patch.yml').write_text(text, encoding='utf-8')

    # a) 没有 cordis.patch.yml 也不崩
    def test_missing_patch_does_not_crash(self):
        status = local.status(self.home)
        self.assertFalse(status['patch_exists'])
        self.assertFalse(status['bot_ready'])
        self.assertEqual(status['bot_state'], 'broken')
        self.assertIsNone(status['bot_alive'])       # 没登记，无从探活
        self.assertTrue(status['bot_problems'])

    # b) 探活超时 / 连不上：bot_alive=False 且不抛
    def test_probe_timeout_reports_dead_without_raising(self):
        self.write_patch()
        with mock.patch.object(local, 'urlopen', side_effect=socket.timeout('timed out')):
            status = local.status(self.home)
        self.assertFalse(status['bot_alive'])
        self.assertEqual(status['bot_state'], 'degraded')   # 配置齐了，只是控制台没起来
        self.assertTrue(status['bot_ready'])                # 能启动 -> 仍算 ready 级配置
        self.assertIn('控制台探活失败', status['bot_alive_hint'])

    def test_probe_refused_connection_reports_dead(self):
        self.write_patch()
        with mock.patch.object(local, 'urlopen', side_effect=ConnectionRefusedError('refused')):
            self.assertFalse(local.status(self.home)['bot_alive'])

    def test_probe_http_error_counts_as_alive(self):
        from urllib.error import HTTPError
        self.write_patch()
        with mock.patch.object(local, 'urlopen', side_effect=HTTPError('u', 404, 'no', {}, None)):
            self.assertTrue(local.status(self.home)['bot_alive'])

    def test_probe_timeout_is_bounded(self):
        self.write_patch()
        seen = {}

        class _Resp:
            def read(self, *a):
                return b''

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(request, timeout=None):
            seen['timeout'] = timeout
            return _Resp()

        with mock.patch.object(local, 'urlopen', fake):
            local.status(self.home)
        self.assertIsNotNone(seen['timeout'])
        self.assertLessEqual(seen['timeout'], 1.5)

    def test_unconfigured_url_gives_none(self):
        self.write_patch(url='')
        self.assertIsNone(local.status(self.home)['bot_alive'])
        self.assertEqual(local.status(self.home, refresh=True)['bot_state'], 'ready')

    # c) 缓存生效：TTL 内只扫一次盘
    def test_cache_hits_within_ttl(self):
        self.write_patch()
        calls = []
        real = local._scan

        def counting(*a, **k):
            calls.append(a)
            return real(*a, **k)

        with mock.patch.object(local, '_scan', counting):
            local.status(self.home)
            local.status(self.home)
            local.status(self.home)
        self.assertEqual(len(calls), 1)

    def test_cache_result_is_not_shared_mutable(self):
        self.write_patch()
        first = local.status(self.home)
        first['bot_problems'].append('外部改动')
        first['bot_tool_names'].append('外部改动')
        second = local.status(self.home)
        self.assertNotIn('外部改动', second['bot_problems'])
        self.assertNotIn('外部改动', second['bot_tool_names'])

    def test_cache_expires_after_ttl(self):
        self.write_patch()
        calls = []
        real = local._scan

        def counting(*a, **k):
            calls.append(a)
            return real(*a, **k)

        with mock.patch.object(local, '_scan', counting):
            local.status(self.home)
            with mock.patch.object(local, 'STATUS_TTL_SECONDS', 0.0):
                local.status(self.home)
        self.assertEqual(len(calls), 2)

    # d) refresh=True 绕过缓存
    def test_refresh_bypasses_cache(self):
        self.write_patch()
        calls = []
        real = local._scan

        def counting(*a, **k):
            calls.append(a)
            return real(*a, **k)

        with mock.patch.object(local, '_scan', counting):
            local.status(self.home)
            local.status(self.home)
            local.status(self.home, refresh=True)
            local.status(self.home, refresh=True)
        self.assertEqual(len(calls), 3)

    def test_cache_is_per_home(self):
        other = Path(self.tmp.name) / '.dsh2'
        self.write_patch()
        local.status(self.home)
        status = local.status(other)
        self.assertFalse(status['patch_exists'])
        self.assertNotEqual(status['dsh_home'], str(self.home))

    # e) bot_problems 每条都可操作
    def test_every_problem_is_actionable(self):
        self.write_patch(command='C:/nope/python.exe', script='C:/nope/bot.py', extra='')
        status = local.status(self.home)
        self.assertFalse(status['bot_ready'])
        self.assertEqual(status['bot_state'], 'broken')
        self.assertGreaterEqual(len(status['bot_problems']), 3)
        for item in status['bot_problems']:
            self.assertTrue(item.strip())
            self.assertIn('（', item, item)
            self.assertIn('）', item, item)

    def test_utf8_problem_mentions_env_fix(self):
        # 脚本自己没锁 UTF-8、env 里也没 PYTHONUTF8 时才该报，并给出改 env 的具体动作
        bare = self.profile_dir / 'bare_mcp.py'
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        bare.write_text("TOOLS = [{'name': 'x'}]\n", encoding='utf-8')
        self.write_patch(script=str(bare), extra='')
        problems = local.status(self.home)['bot_problems']
        self.assertTrue(any('PYTHONUTF8' in item for item in problems), problems)

    def test_utf8_env_silences_problem(self):
        bare = self.profile_dir / 'bare_mcp.py'
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        bare.write_text("TOOLS = [{'name': 'x'}]\n", encoding='utf-8')
        self.write_patch(script=str(bare))
        problems = local.status(self.home)['bot_problems']
        self.assertFalse(any('UTF-8' in item for item in problems), problems)

    def test_ready_when_probe_ok(self):
        self.write_patch()
        with mock.patch.object(local, 'urlopen') as fake:
            fake.return_value.__enter__.return_value.read.return_value = b''
            status = local.status(self.home)
        self.assertTrue(status['bot_alive'])
        self.assertEqual(status['bot_state'], 'ready')
        self.assertTrue(status['bot_ready'])
        self.assertEqual(status['bot_problems'], [])


if __name__ == '__main__':
    unittest.main()
