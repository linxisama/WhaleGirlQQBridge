
"""W5a 核心测试：profile 校验、后端构造、工作区绑定、profile 文件存取、注册表。

全部落在 tempfile 临时 data_dir，绝不碰仓库 data/。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from src import agent_backends as ab
from src import agent_workspace as aw


class BackendTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix='ab-core-'))
        self.data = self.base / 'data'
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        shutil.rmtree(str(self.base), ignore_errors=True)

    def backend(self, backend_id: str = 'b1', profile=None, **kwargs):
        payload = {'command': sys.executable, 'timeout': 30}
        payload.update(profile or {})
        payload = {key: value for key, value in payload.items() if value is not None}
        return ab.CliBackend(backend_id, payload, data_dir=self.data, **kwargs)


# ── profile 校验 ────────────────────────────────────────────────────────────

class ProfileValidationTests(unittest.TestCase):
    def valid(self, **overrides):
        payload = {'command': 'python', 'args': [], 'timeout': 5}
        payload.update(overrides)
        return ab.validate_profile('cli', payload)

    def assertBad(self, result, needle: str = ''):
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'bad_profile')
        self.assertIsInstance(result['message'], str)
        if needle:
            self.assertIn(needle, result['message'])

    def test_command_required(self):
        self.assertBad(ab.validate_profile('cli', {}), 'command')
        self.assertBad(ab.validate_profile('cli', {'command': '   '}), 'command')

    def test_command_must_be_str(self):
        self.assertBad(ab.validate_profile('cli', {'command': 123}), 'command')
        self.assertBad(ab.validate_profile('cli', {'command': None}), 'command')

    def test_command_control_chars_rejected(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'py\x00thon'}), '控制字符')

    def test_args_must_be_list(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python', 'args': '-x -y'}),
                       'list')
        self.assertBad(ab.validate_profile('cli', {'command': 'python', 'args': ('-x',)}),
                       'list')

    def test_args_element_must_be_str(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python', 'args': ['ok', 3]}),
                       '字符串')

    def test_args_control_chars_rejected(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python', 'args': ['a\nb']}),
                       '控制字符')

    def test_unknown_profile_key_rejected(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python', 'work_dir': '.'}),
                       'work_dir')

    def test_workdir_must_be_str_and_exist(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python', 'workdir': 7}),
                       'workdir')
        missing = os.path.join(tempfile.gettempdir(), 'ab-no-such-dir-xyz')
        self.assertBad(ab.validate_profile('cli', {'command': 'python',
                                                   'workdir': missing}), 'workdir')

    def test_workdir_normalized_to_absolute(self):
        result = ab.validate_profile('cli', {'command': 'python', 'workdir': '.'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['profile']['workdir'], os.path.abspath('.'))

    def test_env_value_must_be_str(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python',
                                                   'env': {'A': 1}}), 'env[A]')

    def test_env_key_must_be_identifier(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python',
                                                   'env': {'1BAD': 'x'}}), 'env 键名非法')
        self.assertBad(ab.validate_profile('cli', {'command': 'python',
                                                   'env': {'A-B': 'x'}}), 'env 键名非法')

    def test_env_denied_injection_keys(self):
        for key in ('PATH', 'path', 'PATHEXT', 'PYTHONPATH', 'NODE_OPTIONS',
                    'LD_PRELOAD', 'PSModulePath'):
            self.assertBad(ab.validate_profile('cli', {'command': 'python',
                                                       'env': {key: 'x'}}), key)

    def test_env_control_chars_rejected(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python',
                                                   'env': {'A': 'x\ry'}}), '控制字符')

    def test_timeout_validation(self):
        for value in ('abc', 0, -5, True, float('inf'), float('nan')):
            self.assertBad(ab.validate_profile('cli', {'command': 'python',
                                                       'timeout': value}), 'timeout')

    def test_timeout_clamped_to_1_600(self):
        self.assertEqual(ab.validate_profile('cli', {'command': 'python',
                                                     'timeout': 0.2})['profile']['timeout'], 1.0)
        self.assertEqual(ab.validate_profile('cli', {'command': 'python',
                                                     'timeout': 9999})['profile']['timeout'], 600.0)
        self.assertEqual(ab.validate_profile('cli', {'command': 'python',
                                                     'timeout': 30})['profile']['timeout'], 30.0)

    def test_protocol_shape(self):
        self.assertBad(ab.validate_profile('cli', {'command': 'python', 'protocol': ''}),
                       'protocol')
        self.assertBad(ab.validate_profile('cli', {'command': 'python', 'protocol': '9x'}),
                       'protocol')
        self.assertEqual(ab.validate_profile('cli', {'command': 'python',
                                                     'protocol': 'jsonl'})['profile']['protocol'],
                         'jsonl')

    def test_valid_profile_normalized_has_all_keys(self):
        result = self.valid()
        self.assertTrue(result['ok'], result)
        self.assertEqual(sorted(result['profile']), sorted(ab.PROFILE_KEYS))
        self.assertEqual(result['profile']['protocol'], ab.DEFAULT_PROTOCOL)

    def test_kind_rejected(self):
        for kind in ('other', '', None):
            result = ab.validate_profile(kind, {'command': 'python'})
            self.assertFalse(result['ok'])
            self.assertEqual(result['code'], 'bad_profile')

    def test_profile_must_be_dict(self):
        self.assertBad(ab.validate_profile('cli', ['command']))


# ── 后端构造与只读状态 ──────────────────────────────────────────────────────

class BackendConstructionTests(BackendTestBase):
    def test_base_class_is_abstract(self):
        with self.assertRaises(TypeError):
            ab.AgentBackend('b1', {'command': sys.executable}, data_dir=self.data)

    def test_invalid_backend_id_returns_bad_profile_everywhere(self):
        backend = ab.CliBackend('bad id!', {'command': sys.executable}, data_dir=self.data)
        for name in ('probe', 'status', 'workspace', 'cancel', 'stop'):
            result = getattr(backend, name)()
            self.assertFalse(result['ok'], name)
            self.assertEqual(result['code'], 'bad_profile', name)
        self.assertEqual(backend.start('x')['code'], 'bad_profile')

    def test_missing_data_dir_rejected(self):
        for value in (None, '', '   '):
            backend = ab.CliBackend('b1', {'command': sys.executable}, data_dir=value)
            result = backend.status()
            self.assertFalse(result['ok'])
            self.assertEqual(result['code'], 'bad_profile')

    def test_bad_profile_surfaces_as_dict_not_exception(self):
        backend = ab.CliBackend('b1', {'command': ''}, data_dir=self.data)
        result = backend.start('x')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'bad_profile')

    def test_status_idle_shape(self):
        backend = self.backend('idle1')
        status = backend.status()
        self.assertTrue(status['ok'], status)
        self.assertEqual(status['state'], 'idle')
        self.assertFalse(status['running'])
        self.assertIsNone(status['pid'])
        self.assertEqual(status['runs'], 0)
        self.assertIsNone(status['last'])
        self.assertEqual(status['kind'], 'cli')
        self.assertEqual(status['id'], 'idle1')
        self.assertTrue(status['workspace'].endswith(os.path.join('agent_workspace', 'idle1')))
        self.assertFalse(Path(status['workspace']).exists())   # 只读方法不建目录

    def test_timeout_property_clamped(self):
        backend = self.backend('t1', {'timeout': 9999})
        self.assertEqual(backend.timeout, 600.0)

    def test_make_backend_ok_and_unknown_kind(self):
        result = ab.make_backend('mk1', 'cli', {'command': sys.executable},
                                 data_dir=self.data)
        self.assertTrue(result['ok'], result)
        self.assertIsInstance(result['backend'], ab.CliBackend)
        self.assertEqual(result['profile']['timeout'], ab.DEFAULT_TIMEOUT)
        bad = ab.make_backend('mk2', 'nope', {'command': sys.executable}, data_dir=self.data)
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['code'], 'bad_profile')

    def test_make_backend_propagates_bad_profile(self):
        result = ab.make_backend('mk3', 'cli', {'command': ''}, data_dir=self.data)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'bad_profile')

    def test_dsh_profile_defaults(self):
        backend = ab.DshBackend('d1', data_dir=self.data)
        self.assertEqual(backend.kind, 'dsh')
        self.assertEqual(backend.profile['command'], 'dsh')
        self.assertEqual(backend.profile['args'], [])
        self.assertIsInstance(backend, ab.CliBackend)

    def test_dsh_profile_override(self):
        backend = ab.DshBackend('d2', {'command': sys.executable, 'args': ['-V']},
                                data_dir=self.data)
        self.assertEqual(backend.profile['command'], sys.executable)
        self.assertEqual(backend.profile['args'], ['-V'])


# ── 工作区绑定 ──────────────────────────────────────────────────────────────

class WorkspaceBindingTests(BackendTestBase):
    def test_workspace_creates_and_binds_backend_id(self):
        backend = self.backend('ws1')
        result = backend.workspace()
        self.assertTrue(result['ok'], result)
        expected = aw.root(self.data, 'ws1')
        self.assertEqual(result['path'], str(expected))
        self.assertTrue(expected.is_dir())
        self.assertTrue(result['created'])

        written = aw.write_text(self.data, 'ws1', 'notes.md', 'hello workspace')
        self.assertTrue(written['ok'], written)
        self.assertEqual(aw.read_text(self.data, 'ws1', 'notes.md')['text'], 'hello workspace')
        listed = aw.list_dir(self.data, 'ws1')
        self.assertEqual([item['name'] for item in listed['entries']], ['notes.md'])

    def test_workspace_idempotent(self):
        backend = self.backend('ws2')
        first = backend.workspace()
        second = backend.workspace()
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertTrue(second['exists'])
        self.assertEqual(first['path'], second['path'])

    def test_workspace_escape_still_blocked_by_agent_workspace(self):
        backend = self.backend('ws3')
        self.assertTrue(backend.workspace()['ok'])
        escaped = aw.write_text(self.data, 'ws3', '../escape.txt', 'nope')
        self.assertFalse(escaped['ok'])
        self.assertIn(escaped['error']['code'], ('denied', 'invalid'))
        self.assertFalse((self.base / 'escape.txt').exists())

    def test_workspace_never_touches_repo_data(self):
        backend = self.backend('ws4')
        path = Path(backend.workspace()['path'])
        self.assertTrue(path.is_dir())
        repo_data = Path(__file__).resolve().parents[1] / 'data'
        try:
            path.relative_to(repo_data)
        except ValueError:
            pass
        else:
            self.fail('workspace 落到了仓库 data/ 里：%s' % path)


# ── profile 文件存取 ────────────────────────────────────────────────────────

class ProfilesFileTests(BackendTestBase):
    def path(self) -> Path:
        return self.base / 'profiles' / 'agent_backends.json'

    def document(self) -> dict:
        return {'version': ab.SCHEMA_VERSION,
                'backends': [{'id': 'p1', 'kind': 'cli',
                              'profile': {'command': sys.executable, 'args': ['-V'],
                                          'timeout': 7}},
                             {'id': 'p2', 'kind': 'dsh',
                              'profile': {'command': 'dsh', 'protocol': 'none'}}]}

    def write(self, payload) -> Path:
        target = self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                          encoding='utf-8')
        return target

    def test_load_missing_file_is_empty_not_error(self):
        result = ab.load_profiles(self.path())
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['profiles'], {})
        self.assertFalse(result['exists'])

    def test_load_roundtrip(self):
        target = self.write(self.document())
        result = ab.load_profiles(target)
        self.assertTrue(result['ok'], result)
        self.assertEqual(sorted(result['profiles']), ['p1', 'p2'])
        self.assertEqual(result['profiles']['p1']['profile']['args'], ['-V'])
        self.assertEqual(result['profiles']['p1']['profile']['timeout'], 7.0)
        self.assertEqual(result['profiles']['p2']['kind'], 'dsh')

    def test_load_bad_json(self):
        result = ab.load_profiles(self.write('{not json'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'bad_profile_file')
        self.assertEqual(result['profiles'], {})

    def test_load_bad_utf8(self):
        target = self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'\xff\xfe\x00bad')
        result = ab.load_profiles(target)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'bad_profile_file')

    def test_load_rejects_bad_entry_profile(self):
        target = self.write({'version': 1, 'backends': [
            {'id': 'p1', 'kind': 'cli', 'profile': {'command': 'python', 'args': 'oops'}}]})
        result = ab.load_profiles(target)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'bad_profile_file')
        self.assertIn('profile', result['message'])

    def test_load_rejects_duplicate_ids(self):
        target = self.write({'version': 1, 'backends': [
            {'id': 'dup', 'kind': 'cli', 'profile': {'command': 'python'}},
            {'id': 'dup', 'kind': 'cli', 'profile': {'command': 'python'}}]})
        result = ab.load_profiles(target)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'bad_profile_file')
        self.assertIn('重复', result['message'])

    def test_load_rejects_bad_shape(self):
        for payload in ({'version': 1}, {'version': 2, 'backends': []},
                        {'version': 1, 'backends': {}}, {'version': 1, 'backends': [1]},
                        {'version': 1, 'backends': [{'id': 'bad id', 'profile': {}}]},
                        {'version': 1, 'backends': [{'id': 'ok', 'profile': {}}]},
                        'null'):
            result = ab.load_profiles(self.write(payload))
            self.assertFalse(result['ok'], payload)
            self.assertEqual(result['code'], 'bad_profile_file', payload)

    def test_save_then_load_roundtrip_is_atomic(self):
        target = self.path()
        saved = ab.save_profiles(target, {
            'a1': {'kind': 'cli', 'profile': {'command': sys.executable, 'timeout': 9}},
            'a2': {'command': 'dsh', 'kind': 'dsh'},
        })
        self.assertTrue(saved['ok'], saved)
        self.assertEqual(saved['count'], 2)
        self.assertTrue(target.exists())
        leftovers = [item.name for item in target.parent.iterdir() if '.tmp-' in item.name]
        self.assertEqual(leftovers, [])
        loaded = ab.load_profiles(target)
        self.assertTrue(loaded['ok'], loaded)
        self.assertEqual(sorted(loaded['profiles']), ['a1', 'a2'])
        self.assertEqual(loaded['profiles']['a2']['kind'], 'dsh')
        self.assertEqual(loaded['profiles']['a1']['profile']['timeout'], 9.0)

    def test_save_rejects_bad_input(self):
        for profiles in ([], 'x', {'bad id': {'command': 'python'}},
                         {'ok1': {'command': ''}}, {'ok2': {'args': 'nope',
                                                            'command': 'python'}}):
            result = ab.save_profiles(self.path(), profiles)
            self.assertFalse(result['ok'], profiles)
            self.assertEqual(result['code'], 'bad_profile', profiles)

    def test_save_then_load_bad_file_recovers_empty(self):
        target = self.path()
        self.assertTrue(ab.save_profiles(target, {'x1': {'command': 'python'}})['ok'])
        target.write_text('{"version": 1, "backends": [', encoding='utf-8')
        broken = ab.load_profiles(target)
        self.assertFalse(broken['ok'])
        self.assertEqual(broken['profiles'], {})
        # 坏文件之后仍能原子重建
        self.assertTrue(ab.save_profiles(target, {'x1': {'command': 'python'}})['ok'])
        self.assertTrue(ab.load_profiles(target)['ok'])


if __name__ == '__main__':
    unittest.main()
