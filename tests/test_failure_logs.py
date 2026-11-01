"""失败日志：技能/扩展没跑起来的原因写进结构化日志（source='skill' / 'extension'）。

四条不变量：
1. 只在失败出口落一条 error；成功一条都不落。
2. 同一 (source, 目标, 代码) 60 秒内只落一条；换工具名或换 code 各自再落一条。
3. 写日志本身出任何事都不影响返回值，也不许抛。
4. data_dir 缺省不崩；所有用例把 AppLog 钉在 tempfile 里，绝不写仓库根的 data/。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import app_log, extension_host, skill_tools

REPO = Path(__file__).resolve().parents[1]
EXT_ID = 'demo-ext'
TOOL = 'demo-ext__write'
#: 技能失败出口的返回形状（改动前就有的键）：调用方看到的必须还是这三个键。
SKILL_FAIL_FIELDS = {'ok', 'tool', 'error'}
#: 扩展 _failure 的返回形状（与改前的字面量逐字一致）。
EXT_FAILURE_FIELDS = {'ok', 'error', 'kind', 'value', 'raw', 'timed_out', 'duration_ms',
                      'extension', 'stderr', 'stderr_tail', 'cwd'}
#: 扩展 call_tool 的失败基线形状。
CALL_FAIL_FIELDS = {'ok', 'value', 'tool', 'extension', 'duration_ms', 'error', 'raw'}
_DEFAULT = object()


class _LogCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        # 去重表是模块级的：每个用例从空表起跑（窗口行为另有用例专测）。
        skill_tools._FAILURE_LOG_AT.clear()
        extension_host._FAILURE_LOG_AT.clear()
        self.log = app_log.default_app_log(self.data_dir)
        self.log.clear()

    def tearDown(self):
        self.log.clear()
        self._tmp.cleanup()

    def rows(self, source=''):
        return self.log.tail(limit=200, source=source)

    def skill_handler(self, *, data_dir=_DEFAULT, required_context=()):
        return skill_tools.build_tool_handler(
            node='node', host_script=str(REPO / 'host-one.mjs'), source_dir=str(REPO),
            extension=str(REPO / 'ext' / 'index.js'), extension_id=EXT_ID,
            mapping={'write': TOOL, 'read': '%s__read' % EXT_ID}, timeout=0.2,
            data_dir=self.data_dir if data_dir is _DEFAULT else data_dir,
            required_context=required_context)


class SkillFailureLogTest(_LogCase):
    def test_missing_context_writes_one_skill_error(self):
        handler = self.skill_handler(required_context=('qq_user_id',))
        out = asyncio.run(handler('write', {}))
        self.assertEqual(set(out), SKILL_FAIL_FIELDS)
        self.assertEqual(out['error']['code'], 'missing-context')
        rows = self.rows('skill')
        self.assertEqual(len(rows), 1)
        entry = rows[0]
        self.assertEqual(entry['level'], 'error')
        self.assertEqual(entry['source'], 'skill')
        self.assertEqual(entry['code'], 'missing-context')
        self.assertIn(TOOL, entry['message'])
        self.assertIn('qq_user_id', entry['message'])
        self.assertEqual(entry['extra'], {'tool': TOOL, 'extension': EXT_ID})
        detail = json.loads(entry['detail'])
        self.assertEqual(detail['error']['code'], 'missing-context')
        self.assertEqual(detail['tool'], TOOL)

    def test_timeout_writes_skill_error(self):
        handler = self.skill_handler()
        boom = mock.AsyncMock(side_effect=asyncio.TimeoutError)
        with mock.patch.object(skill_tools.extension_host, 'call_tool', boom):
            out = asyncio.run(handler('write', {}))
        self.assertEqual(set(out), SKILL_FAIL_FIELDS)
        self.assertEqual(out['error']['code'], 'timeout')
        rows = self.rows('skill')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], 'timeout')
        self.assertEqual(rows[0]['level'], 'error')
        self.assertIn(TOOL, rows[0]['message'])

    def test_handler_error_writes_skill_error(self):
        handler = self.skill_handler()
        boom = mock.AsyncMock(side_effect=RuntimeError('boom'))
        with mock.patch.object(skill_tools.extension_host, 'call_tool', boom):
            out = asyncio.run(handler('write', {}))
        self.assertEqual(set(out), SKILL_FAIL_FIELDS)
        self.assertEqual(out['error']['code'], 'handler-error')
        rows = self.rows('skill')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], 'handler-error')
        self.assertIn('RuntimeError: boom', rows[0]['detail'])

    def test_extension_reported_failure_writes_skill_error(self):
        handler = self.skill_handler()
        failed = mock.AsyncMock(return_value={'ok': False, 'error': 'tool 不在该扩展的白名单里'})
        with mock.patch.object(skill_tools.extension_host, 'call_tool', failed):
            out = asyncio.run(handler('write', {}))
        self.assertEqual(out['error']['code'], 'not-allowed')
        rows = self.rows('skill')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], 'not-allowed')
        self.assertEqual(rows[0]['extra']['tool'], TOOL)

    def test_success_writes_nothing(self):
        handler = self.skill_handler()
        good = mock.AsyncMock(return_value={'ok': True, 'value': {'v': 1}})
        with mock.patch.object(skill_tools.extension_host, 'call_tool', good):
            out = asyncio.run(handler('write', {'a': 1}))
        self.assertEqual(out, {'ok': True, 'tool': TOOL, 'result': {'v': 1}})
        self.assertEqual(self.rows('skill'), [])

    def test_same_failure_three_times_logs_once(self):
        handler = self.skill_handler(required_context=('qq_user_id',))
        for _ in range(3):
            out = asyncio.run(handler('write', {}))
            self.assertEqual(out['error']['code'], 'missing-context')
        self.assertEqual(len(self.rows('skill')), 1)

    def test_other_tool_adds_one_more(self):
        handler = self.skill_handler(required_context=('qq_user_id',))
        asyncio.run(handler('write', {}))
        asyncio.run(handler('write', {}))
        asyncio.run(handler('read', {}))
        rows = self.rows('skill')
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['extra']['tool'] for row in rows},
                         {TOOL, '%s__read' % EXT_ID})

    def test_other_code_adds_one_more(self):
        handler = self.skill_handler(required_context=('qq_user_id',))
        asyncio.run(handler('write', {}))
        boom = mock.AsyncMock(side_effect=RuntimeError('boom'))
        with mock.patch.object(skill_tools.extension_host, 'call_tool', boom):
            plain = self.skill_handler()
            asyncio.run(plain('write', {}))
        rows = self.rows('skill')
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['code'] for row in rows}, {'missing-context', 'handler-error'})

    def test_log_write_failure_keeps_return_value(self):
        handler = self.skill_handler(required_context=('qq_user_id',))
        with mock.patch.object(app_log.AppLog, 'error', side_effect=RuntimeError('disk full')):
            out = asyncio.run(handler('write', {}))
        self.assertEqual(set(out), SKILL_FAIL_FIELDS)
        self.assertFalse(out['ok'])
        self.assertEqual(out['error']['code'], 'missing-context')
        self.assertIn('qq_user_id', out['error']['message'])
        self.assertEqual(self.rows('skill'), [])

    def test_missing_data_dir_does_not_crash(self):
        seen = []

        def fake_default(data_dir=None):
            seen.append(data_dir)
            return self.log

        handler = self.skill_handler(data_dir=None, required_context=('qq_user_id',))
        with mock.patch.object(app_log, 'default_app_log', side_effect=fake_default):
            out = asyncio.run(handler('write', {}))
        self.assertEqual(out['error']['code'], 'missing-context')
        self.assertEqual(seen, [None])
        self.assertEqual(len(self.rows('skill')), 1)


class ExtensionFailureLogTest(_LogCase):
    def kwargs(self, **over):
        base = {'node': 'node', 'host_script': 'host.mjs', 'extension': 'index.js',
                'source_dir': 'src', 'capability': 'chat', 'kind': 'provider',
                'extension_id': EXT_ID, 'data_dir': self.data_dir}
        base.update(over)
        return base

    def test_failure_writes_one_extension_error(self):
        out = extension_host._failure(self.kwargs(), 'sync-timeout')
        self.assertEqual(set(out), EXT_FAILURE_FIELDS)
        self.assertEqual(out['error'], 'sync-timeout')
        rows = self.rows('extension')
        self.assertEqual(len(rows), 1)
        entry = rows[0]
        self.assertEqual(entry['level'], 'error')
        self.assertEqual(entry['source'], 'extension')
        self.assertEqual(entry['code'], 'sync-timeout')
        self.assertEqual(entry['extra'], {'capability': 'chat', 'kind': 'provider'})
        self.assertIn('chat', entry['message'])
        self.assertEqual(json.loads(entry['detail'])['error'], 'sync-timeout')

    def test_failure_dedup_window_and_keys(self):
        for _ in range(3):
            extension_host._failure(self.kwargs(), 'sync-timeout')
        self.assertEqual(len(self.rows('extension')), 1)
        extension_host._failure(self.kwargs(capability='vision'), 'sync-timeout')
        self.assertEqual(len(self.rows('extension')), 2)
        extension_host._failure(self.kwargs(), 'RuntimeError: explode')
        rows = self.rows('extension')
        self.assertEqual(len(rows), 3)
        self.assertEqual({row['code'] for row in rows}, {'sync-timeout', ''})

    def test_call_tool_whitelist_failure_writes(self):
        out = asyncio.run(extension_host.call_tool(
            node='node', host_script='host.mjs', extension='index.js', source_dir='src',
            tool_id=TOOL, arguments={}, data_dir=self.data_dir, extension_id=EXT_ID,
            allowed_ids=['%s__other' % EXT_ID]))
        self.assertEqual(set(out), CALL_FAIL_FIELDS)
        self.assertEqual(out['error'], 'tool 不在该扩展的白名单里')
        rows = self.rows('extension')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], 'not-allowed')
        self.assertEqual(rows[0]['extra'], {'capability': TOOL, 'kind': 'tool'})

    def test_call_tool_not_owned_writes(self):
        out = asyncio.run(extension_host.call_tool(
            node='node', host_script='host.mjs', extension='index.js', source_dir='src',
            tool_id='other__write', arguments={}, data_dir=self.data_dir,
            extension_id=EXT_ID))
        self.assertEqual(set(out), CALL_FAIL_FIELDS)
        self.assertEqual(out['error'], 'tool id 不属于这个扩展')
        rows = self.rows('extension')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], 'not-owned')
        self.assertEqual(rows[0]['extra']['capability'], 'other__write')

    def test_call_tool_log_write_failure_keeps_return_value(self):
        with mock.patch.object(app_log.AppLog, 'error', side_effect=RuntimeError('disk full')):
            out = asyncio.run(extension_host.call_tool(
                node='node', host_script='host.mjs', extension='index.js', source_dir='src',
                tool_id=TOOL, arguments={}, data_dir=self.data_dir, extension_id=EXT_ID,
                allowed_ids=['%s__other' % EXT_ID]))
        self.assertEqual(set(out), CALL_FAIL_FIELDS)
        self.assertFalse(out['ok'])
        self.assertEqual(out['error'], 'tool 不在该扩展的白名单里')
        self.assertEqual(self.rows('extension'), [])

    def test_missing_data_dir_does_not_crash(self):
        seen = []

        def fake_default(data_dir=None):
            seen.append(data_dir)
            return self.log

        with mock.patch.object(app_log, 'default_app_log', side_effect=fake_default):
            out = extension_host._failure(self.kwargs(data_dir=None), 'sync-timeout')
        self.assertEqual(out['error'], 'sync-timeout')
        self.assertEqual(seen, [None])
        self.assertEqual(len(self.rows('extension')), 1)


if __name__ == '__main__':
    unittest.main()
