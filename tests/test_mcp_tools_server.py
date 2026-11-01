#!/usr/bin/env python3
"""tools/whale_tools_mcp.py 的单元测试。

默认路径全部用替身，不需要 node；只有末尾三个用例起真子进程验 stdio 行为。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools import whale_tools_mcp as server  # noqa: E402

SCRIPT = REPO / 'tools' / 'whale_tools_mcp.py'


def counts_of(tools) -> dict:
    return {'tools': len(tools),
            'extensionTools': sum(1 for item in tools if item['kind'] == 'extension'),
            'skillTools': sum(1 for item in tools if item['kind'] == 'skill'),
            'writeTools': sum(1 for item in tools if item['write']),
            'extensions': len({item['extension'] for item in tools})}


def payload_of(reply) -> dict:
    return json.loads(reply['result']['content'][0]['text'])


class StateCase(unittest.TestCase):
    """一份临时 data_dir + 手工工具面，绝不碰仓库 data/。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = server.build_state(data_dir=self.root)
        self.state['paths'] = {'node': 'node.exe', 'host_script': 'host-one.mjs', 'source_dir': 'src'}

    def use_tools(self, tools):
        self.state['catalog'] = {'tools': tools, 'errors': [], 'counts': counts_of(tools)}
        return tools

    def call(self, message):
        return asyncio.run(server.handle(message, self.state))

    def tool_call(self, name, arguments, identifier=7):
        return self.call({'jsonrpc': '2.0', 'id': identifier, 'method': 'tools/call',
                          'params': {'name': name, 'arguments': arguments}})

    def fake_exec(self, calls, result=None, error=None):
        async def handler(state, entry, arguments):
            calls.append((entry['id'], dict(arguments)))
            if error is not None:
                return {'ok': False, 'tool': entry['id'], 'error': error}
            return {'ok': True, 'tool': entry['id'], 'result': result or {'echo': arguments.get('text', '')}}
        original = server.exec_tool
        server.exec_tool = handler
        self.addCleanup(setattr, server, 'exec_tool', original)

    def audit_lines(self):
        path = server.audit_path(self.root)
        if not path.is_file():
            return []
        text = path.read_text(encoding='utf-8')
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class HandshakeTests(StateCase):
    def test_initialize_echoes_client_protocol_version(self):
        reply = self.call({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                           'params': {'protocolVersion': '2024-11-05',
                                      'clientInfo': {'name': 'claude-desktop', 'version': '1'}}})
        self.assertEqual(reply['id'], 1)
        self.assertEqual(reply['jsonrpc'], '2.0')
        self.assertEqual(reply['result']['protocolVersion'], '2024-11-05')
        self.assertEqual(reply['result']['serverInfo'],
                         {'name': 'whale_tools', 'version': '1.0.0'})
        self.assertIn('tools', reply['result']['capabilities'])
        self.assertTrue(reply['result']['instructions'])

    def test_initialize_falls_back_to_default_protocol_version(self):
        reply = self.call({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {}})
        self.assertEqual(reply['result']['protocolVersion'], '2025-06-18')

    def test_initialize_remembers_client_name_for_audit(self):
        self.call({'jsonrpc': '2.0', 'id': 3, 'method': 'initialize',
                   'params': {'clientInfo': {'name': 'dsh/desktop'}}})
        self.assertEqual(self.state['client'], 'dsh/desktop')

    def test_notifications_are_silent_and_idless_messages_answered_only_for_methods(self):
        self.assertIsNone(self.call({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        self.assertIsNone(self.call({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                     'params': {'requestId': 1}}))
        self.assertIsNone(self.call({'jsonrpc': '2.0', 'method': 'no/such/method'}))

    def test_ping_answers_empty_result(self):
        reply = self.call({'jsonrpc': '2.0', 'id': 9, 'method': 'ping'})
        self.assertEqual(reply['result'], {})

    def test_unknown_method_returns_minus_32601(self):
        reply = self.call({'jsonrpc': '2.0', 'id': 11, 'method': 'resources/list'})
        self.assertEqual(reply['error']['code'], -32601)
        self.assertIn('resources/list', reply['error']['message'])
        self.assertNotIn('result', reply)


class MetaToolsTests(StateCase):
    def test_tools_list_shape(self):
        reply = self.call({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'})
        tools = reply['result']['tools']
        self.assertEqual([item['name'] for item in tools],
                         ['tools_list', 'tools_call', 'tools_status'])
        for item in tools:
            self.assertTrue(item['description'])
            schema = item['inputSchema']
            self.assertEqual(schema['type'], 'object')
            self.assertIsInstance(schema['properties'], dict)
            self.assertIs(schema['additionalProperties'], False)
        self.assertEqual(sorted(tools[1]['inputSchema']['properties']),
                         ['arguments', 'confirm', 'tool'])
        self.assertEqual(tools[1]['inputSchema']['required'], ['tool'])

    def test_unknown_meta_tool_returns_structured_error(self):
        reply = self.tool_call('tools_nope', {})
        self.assertTrue(reply['result']['isError'])
        body = payload_of(reply)
        self.assertEqual(body['error']['code'], 'unknown-tool')
        self.assertNotIn('audited', body)

    def test_extra_arguments_are_rejected(self):
        reply = self.tool_call('tools_status', {'nope': 1})
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(payload_of(reply)['error']['code'], 'invalid-argument')
        self.assertIn('nope', payload_of(reply)['error']['message'])


class ToolsListTests(StateCase):
    def setUp(self):
        super().setUp()
        self.use_tools([
            server._tool_entry('exta__probe', 'exta', '回显参数', 'extension', str(self.root / 'exta')),
            server._tool_entry('exta__open_url', 'exta', '打开网址', 'extension', str(self.root / 'exta')),
            dict(server._tool_entry('sk1__read', 'sk1', '读一段文本', 'skill',
                                    str(self.root / 'sk1')), enabled=False),
        ])

    def test_tools_list_reports_counts_and_write_flags(self):
        body = payload_of(self.tool_call('tools_list', {}))
        self.assertTrue(body['ok'])
        self.assertEqual(body['total'], 3)
        self.assertEqual(body['counts'],
                         {'tools': 3, 'extensionTools': 2, 'skillTools': 1,
                          'writeTools': 1, 'extensions': 2})
        rows = {item['id']: item for item in body['tools']}
        self.assertEqual(sorted(rows), ['exta__open_url', 'exta__probe', 'sk1__read'])
        self.assertEqual(sorted(rows['exta__probe']),
                         ['description', 'enabled', 'extension', 'id', 'write'])
        self.assertFalse(rows['exta__probe']['write'])
        self.assertTrue(rows['exta__open_url']['write'])
        self.assertFalse(rows['sk1__read']['enabled'])
        self.assertTrue(rows['exta__probe']['enabled'])

    def test_tools_list_filters_by_needle_and_write_flag(self):
        body = payload_of(self.tool_call('tools_list', {'filter': 'EXT', 'write': True}))
        self.assertEqual([item['id'] for item in body['tools']], ['exta__open_url'])
        self.assertEqual(body['matched'], 1)
        self.assertEqual(body['total'], 3)
        only_read = payload_of(self.tool_call('tools_list', {'write': False}))
        self.assertEqual([item['id'] for item in only_read['tools']], ['exta__probe', 'sk1__read'])

    def test_tools_list_limit_is_bounded(self):
        body = payload_of(self.tool_call('tools_list', {'limit': 10 ** 6}))
        self.assertEqual(body['shown'], 3)
        tiny = payload_of(self.tool_call('tools_list', {'limit': 1}))
        self.assertEqual(tiny['shown'], 1)
        self.assertEqual(tiny['matched'], 3)


class ConfirmGateTests(StateCase):
    def setUp(self):
        super().setUp()
        self.use_tools([
            server._tool_entry('exta__probe', 'exta', '', 'extension', str(self.root / 'exta')),
            server._tool_entry('exta__open_url', 'exta', '', 'extension', str(self.root / 'exta')),
        ])

    def test_read_tool_runs_without_confirm_and_is_audited(self):
        calls = []
        self.fake_exec(calls, result={'echo': '你好'})
        body = payload_of(self.tool_call('tools_call', {'tool': 'exta__probe',
                                                       'arguments': {'text': '你好'}}))
        self.assertTrue(body['ok'])
        self.assertFalse(body['write'])
        self.assertEqual(body['result'], {'echo': '你好'})
        self.assertTrue(body['audited'])
        self.assertEqual(calls, [('exta__probe', {'text': '你好'})])
        lines = self.audit_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['tool'], 'exta__probe')
        self.assertFalse(lines[0]['write'])
        self.assertFalse(lines[0]['confirm'])
        self.assertTrue(lines[0]['ok'])
        self.assertEqual(lines[0]['code'], '')

    def test_write_tool_blocked_without_confirm_and_never_executed(self):
        calls = []
        self.fake_exec(calls)
        reply = self.tool_call('tools_call', {'tool': 'exta__open_url',
                                             'arguments': {'url': 'https://example.com'}})
        self.assertTrue(reply['result']['isError'])
        body = payload_of(reply)
        self.assertEqual(body['error'], {'code': 'confirm-required',
                                         'message': server.CONFIRM_HINT})
        self.assertFalse(body['ok'])
        self.assertTrue(body['write'])
        self.assertFalse(body['confirm'])
        self.assertEqual(calls, [])
        lines = self.audit_lines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0]['ok'])
        self.assertEqual(lines[0]['code'], 'confirm-required')
        self.assertTrue(lines[0]['write'])

    def test_write_tool_runs_when_confirm_true(self):
        calls = []
        self.fake_exec(calls, result={'opened': True})
        body = payload_of(self.tool_call('tools_call', {'tool': 'exta__open_url',
                                                       'confirm': True,
                                                       'arguments': {'url': 'https://example.com'}}))
        self.assertTrue(body['ok'])
        self.assertTrue(body['confirm'])
        self.assertEqual(body['result'], {'opened': True})
        self.assertEqual(calls, [('exta__open_url', {'url': 'https://example.com'})])
        self.assertTrue(self.audit_lines()[0]['confirm'])

    def test_confirm_must_be_real_true(self):
        calls = []
        self.fake_exec(calls)
        body = payload_of(self.tool_call('tools_call', {'tool': 'exta__open_url',
                                                       'confirm': 'true'}))
        self.assertEqual(body['error']['code'], 'confirm-required')
        self.assertEqual(calls, [])

    def test_unknown_tool_is_structured_and_audited(self):
        reply = self.tool_call('tools_call', {'tool': 'ghost__tool'})
        self.assertTrue(reply['result']['isError'])
        body = payload_of(reply)
        self.assertEqual(body['error']['code'], 'unknown-tool')
        self.assertIn('ghost__tool', body['error']['message'])
        self.assertEqual(self.audit_lines()[0]['tool'], 'ghost__tool')

    def test_failed_tool_call_is_audited_with_code(self):
        self.fake_exec([], error={'code': 'tool-error', 'message': '炸了'})
        reply = self.tool_call('tools_call', {'tool': 'exta__probe'})
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(payload_of(reply)['error']['code'], 'tool-error')
        self.assertEqual(self.audit_lines()[0]['code'], 'tool-error')

    def test_uninstalled_skill_tool_is_not_executed(self):
        self.use_tools([dict(server._tool_entry('sk9__read', 'sk9', '', 'skill',
                                               str(self.root / 'sk9')), enabled=False)])
        body = payload_of(self.tool_call('tools_call', {'tool': 'sk9__read'}))
        self.assertFalse(body['ok'])
        self.assertEqual(body['error']['code'], 'not-installed')
        self.assertEqual(self.audit_lines()[0]['code'], 'not-installed')


class AuditTests(StateCase):
    def test_audit_path_sits_under_qq_only(self):
        path = server.audit_path(self.root)
        self.assertEqual(path, Path(self.root) / 'qq-only' / 'mcp_tool_calls.jsonl')

    def test_audit_record_has_every_field(self):
        ok = server.record_call(self.root, tool='a__b', write=True, confirm=True, ok=False,
                                code='boom', duration_ms=12, client='dsh')
        self.assertTrue(ok)
        line = self.audit_lines()[0]
        self.assertEqual(sorted(line), ['client', 'code', 'confirm', 'duration_ms', 'ok',
                                        'tool', 'ts', 'write'])
        self.assertEqual(line['tool'], 'a__b')
        self.assertEqual(line['code'], 'boom')
        self.assertEqual(line['duration_ms'], 12)
        self.assertEqual(line['client'], 'dsh')
        self.assertIsInstance(line['ts'], float)

    def test_audit_rotates_to_previous_file(self):
        original = server.AUDIT_MAX_BYTES
        server.AUDIT_MAX_BYTES = 1
        self.addCleanup(setattr, server, 'AUDIT_MAX_BYTES', original)
        server.record_call(self.root, tool='a__one', write=False, confirm=False, ok=True,
                           code='', duration_ms=1, client='')
        server.record_call(self.root, tool='a__two', write=False, confirm=False, ok=True,
                           code='', duration_ms=1, client='')
        path = server.audit_path(self.root)
        previous = path.with_name('mcp_tool_calls.previous.jsonl')
        self.assertTrue(previous.is_file())
        kept = [json.loads(line) for line in previous.read_text(encoding='utf-8').splitlines() if line.strip()]
        current = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        self.assertEqual([item['tool'] for item in kept], ['a__one'])
        self.assertEqual([item['tool'] for item in current], ['a__two'])

    def test_audit_failure_only_marks_unaudited(self):
        blocked = Path(self.temp.name) / 'blocked'
        blocked.write_text('x', encoding='utf-8')
        state = server.build_state(data_dir=blocked / 'inner')
        state['paths'] = {'node': 'node.exe', 'host_script': 'h', 'source_dir': 's'}
        state['catalog'] = {'tools': [server._tool_entry('exta__probe', 'exta', '', 'extension', 'nope')],
                            'errors': [], 'counts': counts_of([])}
        self.assertFalse(server.record_call(blocked / 'inner', tool='a__b', write=False,
                                            confirm=False, ok=True, code='',
                                            duration_ms=0, client=''))

        async def fake(state_arg, entry, arguments):
            return {'ok': True, 'result': None}

        original = server.exec_tool
        server.exec_tool = fake
        self.addCleanup(setattr, server, 'exec_tool', original)
        body = payload_of(asyncio.run(server.handle(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': 'tools_call', 'arguments': {'tool': 'exta__probe'}}}, state)))
        self.assertTrue(body['ok'])
        self.assertFalse(body['audited'])


class WriteClassifierTests(unittest.TestCase):
    def test_dangerous_and_workspace_and_pc_tools_are_write(self):
        for ident in ('service', 'exta__open_path', 'exta__open_url', 'exta__type_text',
                      'exta__hotkey', 'exta__screenshot', 'workspace__write',
                      'workspace__append', 'pc_control__pc_screenshot', 'pc_control__pc_click',
                      'pc_control__pc_move', 'pc_control__pc_clipboard_write',
                      'pc_control__pc_open_url'):
            self.assertTrue(server.is_write(ident), ident)

    def test_read_tools_are_not_write(self):
        for ident in ('exta__probe', 'sk1__read', 'workspace__read', 'workspace__list',
                      'pc_control__pc_status', 'pc_control__pc_clipboard_read',
                      'pc_control__pc_active_window', '', None):
            self.assertFalse(server.is_write(ident), ident)

    def test_write_tools_constant_matches_documented_sources(self):
        self.assertEqual(server.WRITE_TOOLS & server.DANGEROUS, server.DANGEROUS)
        self.assertIn('workspace__write', server.WRITE_TOOLS)
        self.assertIn('workspace__append', server.WRITE_TOOLS)
        self.assertFalse(server.PC_CONTROL_READONLY & server.WRITE_TOOLS)
        self.assertEqual(len(server.WRITE_TOOLS), 16)


class CatalogTests(StateCase):
    def test_collect_tools_merges_extension_and_skill_tools(self):
        original_installed = server.qqagent_host.list_installed
        original_eligible = server.skill_tools.eligible_skills
        self.addCleanup(setattr, server.qqagent_host, 'list_installed', original_installed)
        self.addCleanup(setattr, server.skill_tools, 'eligible_skills', original_eligible)
        server.qqagent_host.list_installed = lambda host_root: [
            {'id': 'exta', 'dir': 'D:/x/exta', 'tools': ['probe', 'exta__open_url', '']},
            {'id': 'exta', 'dir': 'D:/x/exta', 'tools': ['probe']},
            {'id': '', 'dir': 'D:/x/none', 'tools': ['x']},
        ]
        server.skill_tools.eligible_skills = lambda *a, **k: {'eligible': [
            {'id': 'sk1', 'tools': [{'full': 'sk1__read', 'prefix': 'sk1', 'raw': 'read',
                                     'description': '读一段文本'}]},
            {'id': '', 'tools': [{'full': 'sk1__read', 'prefix': 'sk1', 'raw': 'read'}]},
        ]}
        plan = server.collect_tools(self.root)
        self.assertEqual([item['id'] for item in plan['tools']],
                         ['exta__probe', 'exta__open_url', 'sk1__read'])
        self.assertEqual(plan['counts'], {'tools': 3, 'extensionTools': 2, 'skillTools': 1,
                                          'writeTools': 1, 'extensions': 2})
        self.assertEqual(plan['errors'], [])
        self.assertEqual(plan['tools'][2]['description'], '读一段文本')
        self.assertFalse(plan['tools'][2]['enabled'])

    def test_collect_tools_survives_broken_sources(self):
        original_installed = server.qqagent_host.list_installed
        original_eligible = server.skill_tools.eligible_skills
        self.addCleanup(setattr, server.qqagent_host, 'list_installed', original_installed)
        self.addCleanup(setattr, server.skill_tools, 'eligible_skills', original_eligible)

        def boom(*args, **kwargs):
            raise RuntimeError('宿主目录读不了')

        server.qqagent_host.list_installed = boom
        server.skill_tools.eligible_skills = boom
        plan = server.collect_tools(self.root)
        self.assertEqual(plan['tools'], [])
        self.assertEqual(len(plan['errors']), 2)
        self.assertIn('宿主目录读不了', plan['errors'][0])

    def test_status_payload_reports_paths_and_counts(self):
        self.use_tools([server._tool_entry('exta__probe', 'exta', '', 'extension', 'p')])
        status = server.status_payload(self.state)
        self.assertTrue(status['ok'])
        self.assertEqual(status['server'], 'whale_tools')
        self.assertEqual(status['protocolVersion'], '2025-06-18')
        self.assertEqual(status['node'], 'node.exe')
        self.assertEqual(status['hostScript'], 'host-one.mjs')
        self.assertEqual(status['dataDir'], str(self.root))
        self.assertEqual(status['counts']['tools'], 1)
        self.assertEqual(status['audit'], str(server.audit_path(self.root)))
        self.assertEqual(status['metaTools'], ['tools_list', 'tools_call', 'tools_status'])
        self.assertEqual(sorted(status['writeTools']), sorted(server.WRITE_TOOLS))
        self.assertFalse(status['hostScriptFound'])

    def test_state_reads_data_dir_from_env(self):
        os.environ[server.DATA_DIR_ENV] = str(self.root)
        self.addCleanup(os.environ.pop, server.DATA_DIR_ENV, None)
        state = server.build_state()
        self.assertEqual(state['data_dir'], self.root)


class DataDirResolutionTests(unittest.TestCase):
    """data_dir 取值优先级：显式 > WHALE_TOOLS_DATA_DIR > WHALE_QQ_HOME/data > 仓库 data/。

    登记桥 src/dsh_memory_bridge.py:84 只传 WHALE_QQ_HOME，漏认它会静默写错位置。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (server.DATA_DIR_ENV, server.HOME_ENV):
            saved = os.environ.pop(name, None)
            if saved is not None:
                self.addCleanup(os.environ.__setitem__, name, saved)

    def test_data_dir_follows_whale_qq_home(self):
        bot_home = self.root / 'bot'
        os.environ[server.HOME_ENV] = str(bot_home)
        state = server.build_state()
        self.assertEqual(state['data_dir'], bot_home / 'data')
        self.assertEqual(server.audit_path(state['data_dir']),
                         bot_home / 'data' / 'qq-only' / 'mcp_tool_calls.jsonl')

    def test_whale_tools_data_dir_wins_over_bot_home(self):
        os.environ[server.HOME_ENV] = str(self.root / 'bot')
        os.environ[server.DATA_DIR_ENV] = str(self.root / 'dedicated')
        self.assertEqual(server.build_state()['data_dir'], self.root / 'dedicated')

    def test_data_dir_falls_back_to_repo_data(self):
        self.assertEqual(server.build_state()['data_dir'],
                         Path(server.extension_host.default_data_dir()))

    def test_explicit_argument_beats_every_env(self):
        os.environ[server.HOME_ENV] = str(self.root / 'bot')
        os.environ[server.DATA_DIR_ENV] = str(self.root / 'dedicated')
        state = server.build_state(data_dir=self.root / 'explicit')
        self.assertEqual(state['data_dir'], self.root / 'explicit')

    def test_blank_env_values_are_ignored(self):
        os.environ[server.HOME_ENV] = '   '
        os.environ[server.DATA_DIR_ENV] = ''
        self.assertEqual(server.build_state()['data_dir'],
                         Path(server.extension_host.default_data_dir()))

    def test_status_data_dir_equals_audit_directory(self):
        bot_home = self.root / 'bot'
        os.environ[server.HOME_ENV] = str(bot_home)
        state = server.build_state()
        state['paths'] = {'node': '', 'host_script': '', 'source_dir': ''}
        state['catalog'] = {'tools': [], 'errors': [], 'counts': counts_of([])}
        status = server.status_payload(state)
        self.assertEqual(status['dataDir'], str(bot_home / 'data'))
        self.assertEqual(status['audit'], str(Path(status['dataDir']) / 'qq-only'
                                              / 'mcp_tool_calls.jsonl'))
        self.assertTrue(server.record_call(state['data_dir'], tool='a__b', write=False,
                                           confirm=False, ok=True, code='',
                                           duration_ms=1, client=''))
        self.assertTrue(Path(status['audit']).is_file())
        self.assertEqual(Path(status['audit']).parent, Path(status['dataDir']) / 'qq-only')


class StdioProcessTests(unittest.TestCase):
    """真子进程：验坏行不崩、UTF-8 往返、--list 诊断。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_server(self, payload: bytes, *extra):
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONIOENCODING', 'PYTHONUTF8')}
        return subprocess.run([sys.executable, str(SCRIPT), '--data-dir', str(self.root), *extra],
                              input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=180)

    def test_bad_json_lines_do_not_crash_the_loop(self):
        payload = (b'not json at all\n\n[1,2,3]\n123\n"text"\n'
                   b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        proc = self.run_server(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertNotIn(b'Traceback', proc.stderr)
        lines = [line for line in proc.stdout.decode('utf-8').splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        reply = json.loads(lines[0])
        self.assertEqual(reply['id'], 1)
        self.assertEqual(reply['result']['serverInfo']['name'], 'whale_tools')

    def test_utf8_chinese_roundtrip_over_stdio(self):
        payload = (json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                               'params': {'clientInfo': {'name': '鲸鱼娘客户端'}}},
                              ensure_ascii=False) + '\n'
                   + json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': '中文/方法',
                                 'params': {}}, ensure_ascii=False) + '\n').encode('utf-8')
        proc = self.run_server(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        text = proc.stdout.decode('utf-8')
        self.assertIn('中文/方法', text)
        replies = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'whale_tools')
        self.assertEqual(replies[1]['error']['code'], -32601)

    def test_list_diagnostic_mode_prints_one_json_object(self):
        proc = self.run_server(b'', '--list')
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        lines = [line for line in proc.stdout.decode('utf-8').splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        status = json.loads(lines[0])
        self.assertEqual(status['server'], 'whale_tools')
        for key in ('node', 'nodeFound', 'hostScript', 'sourceDir', 'hostRoot',
                    'dataDir', 'counts', 'audit', 'writeTools', 'metaTools'):
            self.assertIn(key, status)
        self.assertEqual(status['dataDir'], str(self.root))
        self.assertEqual(status['writeTools'], sorted(server.WRITE_TOOLS))


if __name__ == '__main__':
    unittest.main()
