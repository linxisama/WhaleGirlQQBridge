"""mcp_client 验收测试：每条都真起子进程跑 tests/fixtures/fake_mcp_server.py，不 mock subprocess。"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src import mcp_client as mc

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'fake_mcp_server.py'
PYTHON = sys.executable
ENV = {'PYTHONIOENCODING': 'utf-8'}

STILL_ACTIVE = 259


def pid_alive(pid: int) -> bool:
    """进程是否还活着：Windows 走 OpenProcess，别的平台走 signal 0。"""
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


class McpClientTestBase(unittest.TestCase):
    def make(self, *extra, timeout: float = 20.0, name: str = ''):
        client = mc.McpClient(PYTHON, [str(FIXTURE), *extra], timeout=timeout,
                              name=name or 'fake', env=ENV)
        self.addCleanup(client.close)
        return client

    def started(self, *extra, timeout: float = 20.0):
        client = self.make(*extra, timeout=timeout)
        result = client.start()
        self.assertTrue(result['ok'], result)
        return client

    @staticmethod
    def texts(payload) -> str:
        return ''.join(str(item.get('text') or '') for item in payload['content']
                       if isinstance(item, dict))


# ── 握手 ────────────────────────────────────────────────────────────────────

class HandshakeTests(McpClientTestBase):
    def test_start_returns_handshake_facts(self):
        client = self.started()
        result = client.start()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertEqual(result['serverInfo'].get('name'), 'fake-mcp')
        self.assertGreater(result['pid'], 0)
        self.assertTrue(client.running)

    def test_second_start_does_not_respawn(self):
        client = self.started()
        first_pid = client.pid
        again = client.start()
        self.assertTrue(again['ok'], again)
        self.assertTrue(again.get('already_started'), again)
        self.assertEqual(again['pid'], first_pid)
        self.assertEqual(client.pid, first_pid)

    def test_protocol_version_round_trips(self):
        # fixture 把客户端发来的 protocolVersion 原样回带 → 相等即证明客户端真的发了 2025-06-18
        self.assertEqual(mc.PROTOCOL_VERSION, '2025-06-18')
        client = self.make()
        result = client.start()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['protocolVersion'], mc.PROTOCOL_VERSION)
        self.assertEqual(client.protocol_version, '2025-06-18')
        self.assertEqual(client.server_info.get('version'), '0.1')

    def test_client_version_reads_version_txt(self):
        version = mc.client_version()
        self.assertTrue(version and version[0].isdigit(), version)
        self.assertNotEqual(version, mc.FALLBACK_CLIENT_VERSION)

    def test_handshake_timeout_kills_child(self):
        client = self.make('--handshake-sleep', '5', timeout=0.5)
        started = time.monotonic()
        result = client.start()
        elapsed = time.monotonic() - started
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'handshake-timeout')
        self.assertLess(elapsed, 4.0)
        self.assertTrue(result.get('stopped'))
        self.assertTrue(wait_dead(result['pid']), result)
        self.assertFalse(client.running)

    def test_peer_exits_immediately_reports_stderr(self):
        client = self.make('--exit-immediately')
        result = client.start()
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'handshake-failed')
        self.assertIn('--exit-immediately', result['detail'])
        self.assertTrue(wait_dead(result['pid']), result)

    def test_command_not_found_is_spawn_failed(self):
        client = mc.McpClient('definitely-not-a-real-binary-xyz', [], timeout=1.0, name='ghost')
        result = client.start()
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'spawn-failed')
        self.assertEqual(result['pid'], 0)
        self.assertFalse(client.running)


# ── 工具列表与调用 ──────────────────────────────────────────────────────────

class ToolsTests(McpClientTestBase):
    def test_list_tools_shape(self):
        client = self.started()
        result = client.list_tools()
        self.assertTrue(result['ok'], result)
        names = [item['name'] for item in result['tools']]
        self.assertEqual(sorted(names), ['boom', 'echo_cn', 'emit_bad_json', 'slow'])
        for item in result['tools']:
            self.assertEqual(set(item), {'name', 'description', 'inputSchema'})
            self.assertIsInstance(item['description'], str)
            self.assertIsInstance(item['inputSchema'], dict)

    def test_call_tool_ok(self):
        client = self.started()
        result = client.call_tool('echo_cn', {'text': '你好'})
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['isError'])
        self.assertEqual(self.texts(result), '回显：你好')
        self.assertIn('duration_ms', result)
        self.assertEqual(result['raw']['isError'], False)

    def test_call_tool_is_error_passthrough(self):
        client = self.started()
        result = client.call_tool('boom')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['isError'])
        self.assertIn('boom', self.texts(result))

    def test_call_tool_timeout_then_still_usable(self):
        client = self.started(timeout=0.8)
        slow = client.call_tool('slow')
        self.assertFalse(slow['ok'], slow)
        self.assertEqual(slow['code'], 'timeout')
        self.assertIn('tools/call:slow', slow['message'])
        self.assertGreaterEqual(slow['duration_ms'], 700)
        time.sleep(3.2)      # fixture 是单线程的：等慢工具真的回完，再看客户端还活着
        again = client.call_tool('echo_cn', {'text': '超时后还能用'})
        self.assertTrue(again['ok'], again)
        self.assertEqual(self.texts(again), '回显：超时后还能用')

    def test_call_tool_unknown_tool_is_server_error(self):
        client = self.started()
        result = client.call_tool('nope')
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'server-error')
        self.assertIn('未知工具', result['message'])

    def test_bad_arguments_type_rejected_locally(self):
        client = self.started()
        result = client.call_tool('echo_cn', ['not', 'a', 'dict'])
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'internal')

    def test_unserializable_arguments_do_not_raise(self):
        client = self.started()
        result = client.call_tool('echo_cn', {'text': {1, 2}})
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'internal')
        self.assertTrue(client.running)
        follow = client.call_tool('echo_cn', {'text': '还是好的'})
        self.assertTrue(follow['ok'], follow)

    def test_notifications_are_skipped(self):
        client = self.started()
        result = client.call_tool('echo_cn', {'text': '甲'})
        self.assertTrue(result['ok'], result)
        self.assertGreaterEqual(client.stats['notifications'], 1)

    def test_bad_json_line_is_skipped(self):
        client = self.started()
        result = client.call_tool('emit_bad_json')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.texts(result), '坏行之后仍然回包')
        self.assertEqual(client.stats['bad_json'], 1)
        self.assertIn('这不是一行 JSON', client.bad_lines[0])
        follow = client.call_tool('echo_cn', {'text': '坏行之后'})
        self.assertTrue(follow['ok'], follow)
        self.assertEqual(self.texts(follow), '回显：坏行之后')

    def test_utf8_roundtrip(self):
        client = self.started()
        text = '中文往返 · 鲸鱼娘 🐋'
        result = client.call_tool('echo_cn', {'text': text})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.texts(result), '回显：' + text)

    def test_concurrent_calls(self):
        client = self.started()
        results: dict = {}

        def worker(tag: str) -> None:
            results[tag] = client.call_tool('echo_cn', {'text': tag})

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in ('并发甲', '并发乙')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(results), ['并发乙', '并发甲'])
        for tag, payload in results.items():
            self.assertTrue(payload['ok'], payload)
            self.assertEqual(self.texts(payload), '回显：' + tag)


# ── 收尾 ────────────────────────────────────────────────────────────────────

class LifecycleTests(McpClientTestBase):
    def test_stop_reaps_process(self):
        client = self.started()
        pid = client.pid
        self.assertTrue(pid_alive(pid))
        result = client.stop()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['stopped'], result)
        self.assertEqual(result['pid'], pid)
        self.assertIsNotNone(client._proc.poll())
        self.assertTrue(wait_dead(pid), result)
        self.assertFalse(client.running)

    def test_stop_is_idempotent(self):
        client = self.started()
        first = client.stop()
        second = client.stop()
        self.assertTrue(first['ok'] and second['ok'], (first, second))
        self.assertFalse(second['stopped'])
        self.assertTrue(second['already_stopped'])

    def test_call_after_stop_reports_not_running(self):
        client = self.started()
        client.stop()
        result = client.call_tool('echo_cn', {'text': '已经关了'})
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'not-running')

    def test_context_manager_closes(self):
        with self.make() as client:
            self.assertTrue(client.start()['ok'])
            pid = client.pid
        self.assertIsNotNone(client._proc.poll())
        self.assertTrue(wait_dead(pid))

    @unittest.skipUnless(os.name == 'nt', 'Windows .cmd 垫片才有孤儿树问题')
    def test_stop_kills_cmd_shim_tree(self):
        base = tempfile.mkdtemp(prefix='mcp shim-')
        self.addCleanup(lambda: __import__('shutil').rmtree(base, ignore_errors=True))
        shim = os.path.join(base, 'shim mcp.cmd')
        body = '@echo off' + chr(13) + chr(10) + '"%MCP_FAKE_PY%" "%MCP_FAKE_SCRIPT%" %*' + chr(13) + chr(10)
        with open(shim, 'w', encoding='ascii', newline='') as stream:
            stream.write(body)
        pid_file = os.path.join(base, 'child.pid')
        env = dict(ENV)
        env.update({'MCP_FAKE_PY': PYTHON, 'MCP_FAKE_SCRIPT': str(FIXTURE),
                    'MCP_FAKE_PID_FILE': pid_file})
        client = mc.McpClient(shim, ['--ignore-eof'], timeout=1.0, name='shim', env=env)
        self.addCleanup(client.close)
        result = client.start()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['command'].lower().endswith('.cmd'), result)
        child = int(Path(pid_file).read_text(encoding='utf-8').strip())
        self.assertNotEqual(child, result['pid'])
        self.assertTrue(pid_alive(child))
        stopped = client.stop()
        self.assertTrue(stopped['killed'], stopped)
        self.assertTrue(wait_dead(child, 10.0), (child, stopped))


if __name__ == '__main__':
    unittest.main()
