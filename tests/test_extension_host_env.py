"""子进程环境拼装：残留键不继承、注入值只由 extra_env 决定、父进程不被改写。"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import extension_host


class _FakeProc:
    def __init__(self, payload):
        self.pid = 4321
        self.returncode = 0
        self._payload = payload

    async def communicate(self):
        return self._payload, b''


def _fake_spawn(payload, bucket):
    async def _spawn(*argv, **kwargs):
        bucket.append(kwargs.get('env') or {})
        return _FakeProc(payload)
    return _spawn


class ChildEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src_dir = Path(self.tmp) / 'host'
        (self.src_dir / 'src').mkdir(parents=True)
        (self.src_dir / 'src' / 'config.js').write_text('// x', encoding='utf-8')
        (self.src_dir / 'host-one.mjs').write_text('// x', encoding='utf-8')
        self.data = Path(self.tmp) / 'data'
        self.data.mkdir()
        self.ext_dir = Path(self.tmp) / 'demo-ext'
        self.ext_dir.mkdir()
        (self.ext_dir / 'index.js').write_text('// x', encoding='utf-8')

    def _run(self, extra_env=None):
        seen = []
        with mock.patch('asyncio.create_subprocess_exec',
                        new=_fake_spawn(b'{"ok": true}\n', seen)):
            result = asyncio.run(extension_host.run_capability(
                node='node', host_script=str(self.src_dir / 'host-one.mjs'),
                extension=str(self.ext_dir), source_dir=str(self.src_dir), capability='tool',
                kind='tool', timeout=5, data_dir=self.data,
                extra_env=extra_env))
        self.assertEqual(len(seen), 1)
        return result, seen[0]

    def test_stale_parent_key_is_not_inherited(self):
        os.environ['QQ_AGENT_CTX'] = '{"chatKey":"stale"}'
        try:
            result, env = self._run()
        finally:
            os.environ.pop('QQ_AGENT_CTX', None)
        self.assertTrue(result['ok'])
        self.assertNotIn('QQ_AGENT_CTX', env)
        self.assertEqual(env['QQ_AGENT_SOURCE'], str(self.src_dir))

    def test_extra_env_supplies_the_value(self):
        result, env = self._run(extra_env={'QQ_AGENT_CTX': '{"chatKey":"k1"}'})
        self.assertTrue(result['ok'])
        self.assertEqual(env['QQ_AGENT_CTX'], '{"chatKey":"k1"}')

    def test_parent_environment_is_untouched(self):
        before = dict(os.environ)
        self._run(extra_env={'QQ_AGENT_CTX': '{"chatKey":"k2"}'})
        self.assertEqual(dict(os.environ), before)

    def test_source_dir_cannot_be_overridden(self):
        with self.assertRaises(ValueError):
            self._run(extra_env={'QQ_AGENT_SOURCE': '/elsewhere'})

    def test_inherited_keys_still_reach_the_child(self):
        os.environ['QQ_AGENT_TEST_MARKER'] = 'kept'
        try:
            _result, env = self._run()
        finally:
            os.environ.pop('QQ_AGENT_TEST_MARKER', None)
        self.assertEqual(env['QQ_AGENT_TEST_MARKER'], 'kept')


if __name__ == '__main__':
    unittest.main()
