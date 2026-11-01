"""E2 验收：外部 MCP server 的工具并进现有工具面。

测试全程只用 tests/fixtures/fake_mcp_server.py（禁改），每个用例一套临时 data 根，
tearDown 里 mcp_tools.stop_all() 收干净子进程。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src import mcp_client, mcp_tools, skill_tools, tool_loop

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / 'tests' / 'fixtures' / 'fake_mcp_server.py'


def _alive(pid: int) -> bool:
    """进程还在吗（Windows 用 GetExitCodeProcess，别用 os.kill(pid, 0)——那会真杀）。"""
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _kill(pid: int) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/PID', str(int(pid))],
                       capture_output=True, check=False)
    else:
        os.kill(int(pid), 15)


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _entry(ident, *, command=None, args=None, enabled=True, env=None):
    item = {'id': ident, 'name': ident, 'command': command or sys.executable,
            'args': list(args if args is not None else [str(FIXTURE)]),
            'env': dict(env or {}), 'cwd': None}
    if enabled is not None:
        item['enabled'] = enabled
    return item


class _Runtime:
    """eligible_skills 只吃 runtime.data_root / runtime.root；register_skills 要工具表。"""

    def __init__(self, root):
        self.data_root = str(root)
        self.root = str(root)
        self.config = {}
        self.tool_registry = tool_loop.ToolRegistry()


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='e2-mcp-tools-'))
        self.root = self.tmp / 'data'
        self.qq = self.root / 'qq-only'
        self.qq.mkdir(parents=True)
        self.log = self.qq / mcp_tools.CALL_LOG
        self.addCleanup(mcp_tools.stop_all)

    def register(self, *servers):
        payload = {'version': 1, 'servers': list(servers)}
        (self.qq / mcp_tools.MCP_INDEX).write_text(
            json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    def provider(self, **kwargs):
        kwargs.setdefault('enumerate_timeout', 8.0)
        return mcp_tools.McpToolProvider(self.root, **kwargs)

    def ids(self, provider):
        return [item['id'] for item in provider.tools()]


class EnumerateTest(ProviderCase):
    def test_fixture_tools_prefixed_and_chinese_echo(self):
        pid_file = self.qq / 'fake.pid'
        self.register(_entry('fake', env={'MCP_FAKE_PID_FILE': str(pid_file)}))
        provider = self.provider()
        self.assertEqual(provider.load(), {'servers': 1, 'enabled': 1, 'errors': []})
        self.assertFalse(pid_file.exists(), '载体 import/load 阶段不许起子进程')

        tools = provider.tools()
        ids = [item['id'] for item in tools]
        self.assertIn('fake__echo_cn', ids)
        self.assertEqual(len(ids), 4, ids)
        for item in tools:
            self.assertRegex(item['id'], r'^[a-zA-Z0-9_-]{1,64}$')
            self.assertEqual(item['server'], 'fake')
        echo = next(item for item in tools if item['id'] == 'fake__echo_cn')
        self.assertEqual(echo['name'], 'echo_cn')
        self.assertEqual(echo['parameters']['properties']['text'], {'type': 'string'})
        self.assertFalse(echo['write_hint'])
        self.assertTrue(pid_file.exists(), 'tools() 之后子进程才该在')

        outcome = provider.call('fake__echo_cn', {'text': '你好'})
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(outcome['server'], 'fake')
        self.assertEqual(outcome['name'], 'echo_cn')
        self.assertIn('回显：你好', json.dumps(outcome['content'], ensure_ascii=False))

    def test_disabled_server_is_invisible_and_flips_back(self):
        self.register(_entry('fake', enabled=False))
        provider = self.provider()
        self.assertEqual(provider.tools(), [])
        row = provider.status()['servers'][0]
        self.assertFalse(row['enabled'])
        self.assertEqual(row['tools'], 0)

        self.register(_entry('fake', enabled=True))
        self.assertEqual(provider.load()['enabled'], 1)
        self.assertIn('fake__echo_cn', self.ids(provider))

    def test_cached_provider_notices_enabled_flip(self):
        self.register(_entry('fake', enabled=False))
        cached = mcp_tools.provider_for(self.root, enumerate_timeout=8.0)
        self.assertEqual(cached.tools(), [])
        time.sleep(0.02)                      # 让 mtime 真的变一代
        self.register(_entry('fake', enabled=True))
        self.assertIn('fake__echo_cn', [item['id'] for item in cached.tools()])

    def test_missing_enabled_field_counts_as_disabled(self):
        self.register(_entry('fake', enabled=None))
        provider = self.provider()
        self.assertEqual(provider.load()['enabled'], 0)
        self.assertEqual(provider.tools(), [])

    def test_bad_server_only_hurts_itself(self):
        self.register(_entry('fake'), _entry('ghost', command='no-such-exe-e2-xyz'))
        provider = self.provider(enumerate_timeout=3.0)
        started = time.time()
        ids = self.ids(provider)
        first = time.time() - started
        self.assertTrue(ids)
        self.assertTrue(all(one.startswith('fake__') for one in ids), ids)
        rows = {row['id']: row for row in provider.status()['servers']}
        self.assertEqual(rows['fake']['state'], 'running')
        self.assertIn(rows['ghost']['state'], ('unavailable', 'error'))
        self.assertTrue(rows['ghost']['error'])
        started = time.time()
        self.assertEqual(self.ids(provider), ids)
        self.assertLess(time.time() - started, 2.0, '坏 server 不许每次枚举都重试')
        self.assertLess(first, 20.0)

    def test_slow_handshake_is_bounded(self):
        self.register(_entry('sleeper', args=[str(FIXTURE), '--handshake-sleep', '30']))
        provider = self.provider(enumerate_timeout=1.5)
        started = time.time()
        self.assertEqual(provider.tools(), [])
        self.assertLess(time.time() - started, 12.0)
        row = provider.status()['servers'][0]
        self.assertIn(row['state'], ('unavailable', 'error'))
        self.assertIn('handshake-timeout', row['error'])


class CallTest(ProviderCase):
    def test_killed_server_returns_code_and_stays_responsive(self):
        pid_file = self.qq / 'fake.pid'
        self.register(_entry('fake', env={'MCP_FAKE_PID_FILE': str(pid_file)}))
        provider = self.provider()
        self.assertTrue(provider.call('fake__echo_cn', {'text': 'a'})['ok'])
        pid = int(pid_file.read_text().strip())
        _kill(pid)
        self.assertTrue(_wait_dead(pid), '没杀掉 fixture：%d' % pid)

        started = time.time()
        outcome = provider.call('fake__echo_cn', {'text': 'b'})
        self.assertLess(time.time() - started, 15.0)
        self.assertIn('ok', outcome)
        if outcome['ok']:                       # 允许一次重连成功
            self.assertIn('b', json.dumps(outcome['content'], ensure_ascii=False))
        else:                                   # 或者结构化报错，但必须带 code
            self.assertTrue(outcome['code'])
            self.assertIn(outcome['code'], tuple(mcp_client.ERROR_CODES) +
                          ('unknown-tool', 'unavailable', 'error'))
        started = time.time()
        again = provider.call('fake__echo_cn', {'text': 'c'})
        self.assertLess(time.time() - started, 15.0, '第二次调用不许卡死')
        self.assertFalse(again['ok'] and not again.get('audited'))

    def test_unknown_tool_is_structured(self):
        self.register(_entry('fake'))
        provider = self.provider()
        outcome = provider.call('nope__missing', {})
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['code'], 'unknown-tool')
        self.assertFalse(outcome['audited'] is None)


class AuditTest(ProviderCase):
    def test_call_appends_one_audit_line(self):
        self.register(_entry('fake'))
        provider = self.provider()
        self.assertTrue(provider.call('fake__echo_cn', {'text': '审计'})['ok'])
        self.assertTrue(provider.call('fake__slow', {})['ok'])
        rows = [json.loads(line) for line in
                self.log.read_text(encoding='utf-8').splitlines() if line.strip()]
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(set(rows[-1]), {'ts', 'tool', 'write', 'confirm', 'ok', 'code',
                                         'duration_ms', 'client'})
        self.assertEqual(rows[-1]['tool'], 'fake__slow')
        self.assertEqual(rows[-1]['client'], 'mcp-client')
        self.assertTrue(rows[-1]['ok'])
        self.assertEqual(rows[-1]['code'], '')
        self.assertFalse(rows[-1]['write'])
        self.assertFalse(rows[-1]['confirm'])
        self.assertIsInstance(rows[-1]['duration_ms'], int)


    def test_audit_marks_application_error_as_failure(self):
        self.register(_entry('fake'))
        provider = self.provider()
        outcome = provider.call('fake__boom', {})
        # call() 的返回语义不变：传输成功就是 ok:true，应用级失败靠 isError 表达
        self.assertTrue(outcome['ok'], outcome)
        self.assertTrue(outcome['isError'])
        self.assertTrue(outcome['audited'])
        row = json.loads(self.log.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(row['tool'], 'fake__boom')
        self.assertFalse(row['ok'], '应用级失败必须记成 ok:false：%s' % row)
        self.assertEqual(row['code'], '', 'code 沿用既有约定：成功链路的 code 恒为空串')

    def test_reconnect_writes_single_audit_line(self):
        pid_file = self.qq / 'fake.pid'
        self.register(_entry('fake', env={'MCP_FAKE_PID_FILE': str(pid_file)}))
        provider = self.provider()
        self.assertTrue(provider.call('fake__echo_cn', {'text': 'one'})['ok'])
        before = self.log.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(before), 1, before)
        old_pid = int(pid_file.read_text().strip())
        _kill(old_pid)
        self.assertTrue(_wait_dead(old_pid), '没杀掉 fixture：%d' % old_pid)
        outcome = provider.call('fake__echo_cn', {'text': 'two'})
        rows = [json.loads(line) for line in
                self.log.read_text(encoding='utf-8').splitlines() if line.strip()]
        self.assertEqual(len(rows), 2, '一次 call 只许一行审计（重连不许追加）：%s' % rows)
        self.assertEqual(rows[-1]['tool'], 'fake__echo_cn')
        if outcome['ok']:                     # 断链重连成功：pid 必须换了
            self.assertIs(outcome['audited'], True)
            self.assertTrue(rows[-1]['ok'])
            deadline = time.time() + 5.0
            new_pid = old_pid
            while time.time() < deadline and new_pid == old_pid:
                new_pid = int(pid_file.read_text().strip() or old_pid)
                time.sleep(0.05)
            self.assertNotEqual(new_pid, old_pid, '重连没有换新进程')
        else:                                 # 或结构化报错，同样只有一行
            self.assertTrue(rows[-1]['code'])
            self.assertFalse(rows[-1]['ok'])

class StopTest(ProviderCase):
    def test_stop_all_reaps_children(self):
        pid_file = self.qq / 'fake.pid'
        self.register(_entry('fake', env={'MCP_FAKE_PID_FILE': str(pid_file)}))
        provider = self.provider()
        self.assertTrue(provider.call('fake__echo_cn', {'text': 'x'})['ok'])
        pid = int(pid_file.read_text().strip())
        self.assertTrue(_alive(pid))
        report = provider.stop_all()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['servers'], ['fake'])
        self.assertTrue(_wait_dead(pid), 'stop_all 之后 fixture 还在：%d' % pid)
        self.assertEqual(provider.status()['tools'], 0)


class SkillToolsWiringTest(ProviderCase):
    def test_counts_identical_without_external_server(self):
        runtime = _Runtime(self.tmp)
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            live = skill_tools.eligible_skills(runtime=runtime)
            with mock.patch.object(skill_tools, '_mcp_tools', lambda runtime=None: []):
                plain = skill_tools.eligible_skills(runtime=runtime)
        self.assertEqual(live['counts'], plain['counts'])
        self.assertEqual(live['mcpTools'], [])
        self.assertNotIn('mcpTools', live['counts'])

    def test_repo_data_counts_identical(self):
        runtime = _Runtime(REPO)
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            live = skill_tools.eligible_skills(runtime=runtime)['counts']
            with mock.patch.object(skill_tools, '_mcp_tools', lambda runtime=None: []):
                plain = skill_tools.eligible_skills(runtime=runtime)['counts']
        self.assertEqual(live, plain)

    def test_counts_grow_by_external_tool_count(self):
        runtime = _Runtime(self.tmp)
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            base = skill_tools.eligible_skills(runtime=runtime)['counts']
        self.register(_entry('fake'))
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            plan = skill_tools.eligible_skills(runtime=runtime)
        external = plan['mcpTools']
        self.assertEqual(len(external), 4)
        self.assertEqual(plan['counts']['tools'], base['tools'] + 4)
        self.assertEqual(plan['counts']['skillTools'], base['tools'])
        self.assertEqual(plan['counts']['mcpTools'], 4)
        for item in external:
            self.assertRegex(item['id'], r'^[a-zA-Z0-9_-]{1,64}$')
            self.assertTrue(item['id'].startswith('fake__'))

    def test_register_skills_gives_callable_mcp_tools(self):
        runtime = _Runtime(self.tmp)
        self.register(_entry('fake'))
        args = dict(node='nope-node', host_script=str(self.tmp / 'host.js'),
                    source_dir=str(self.tmp), limit=0)
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            report = skill_tools.register_skills(runtime, **args)
        self.assertEqual(report['counts']['mcpTools'], 4)
        names = sorted(one for one in runtime.tool_registry.names()
                       if one.startswith('fake__'))
        self.assertEqual(names, ['fake__boom', 'fake__echo_cn',
                                 'fake__emit_bad_json', 'fake__slow'])
        spec = runtime.tool_registry.get('fake__echo_cn')
        outcome = asyncio.run(spec.handler('fake__echo_cn', {'text': '注册后调用'}))
        self.assertTrue(outcome['ok'], outcome)
        self.assertIn('注册后调用', json.dumps(outcome, ensure_ascii=False))
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            skill_tools.register_skills(runtime, **args)
        again = sorted(one for one in runtime.tool_registry.names()
                       if one.startswith('fake__'))
        self.assertEqual(again, names, '重注册不许出现 __2 后缀')

    def test_handler_delegates_only_mcp_ids(self):
        self.register(_entry('fake'))
        handler = skill_tools.build_tool_handler(
            node=sys.executable, host_script=str(self.tmp / 'missing-host.js'),
            source_dir=str(self.tmp), extension='', extension_id='fake',
            mapping={}, timeout=5.0, data_dir=str(self.qq))
        self.assertIsNone(skill_tools._mcp_delegate(str(self.qq), 'plain_tool', {}))
        outcome = asyncio.run(handler('fake__echo_cn', {'text': '委派'}))
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(outcome['tool'], 'fake__echo_cn')
        self.assertIn('委派', json.dumps(outcome['result'], ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
