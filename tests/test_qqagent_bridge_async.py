"""桥的真实进程集成测试：真 host-one.mjs + 夹具扩展 + 真 PluginManager。

需要 node、host-one.mjs、QQ Agent 0.4 运行副本三者都在；缺任何一个整类跳过。
这些用例证明的是"manager 真的 await 了桥" —— 旧的 sync 实现会因为
TypeError 被 manager 吞掉，此时 _qqagent_provider 不会有任何记录。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from src import extension_host as host
from src.plugins import qqagent_bridge as bridge
from src.plugins.manager import PluginManager
from src.plugins.qqagent_bridge import QqAgentBridge

REPO = Path(__file__).resolve().parents[1]
HOST_SCRIPT = Path('D:/聊天机器人/插件市场/QQAgent兼容层/host-one.mjs')
SOURCE_DIR = REPO / '.tmp' / 'qq-agent-run'
FIXTURE = REPO / 'tests' / 'fixtures' / 'qqagent_bridge_fixture'
NODE = shutil.which('node')
READY = bool(NODE and HOST_SCRIPT.is_file() and SOURCE_DIR.is_dir() and FIXTURE.is_dir())

MESSAGE = {'kind': 'group', 'target': '12345', 'sender_id': '999', 'sender_name': 'Alice',
           'text': '你好', 'message_id': 'm1', 'self_id': '10000'}


class NoteRuntime:
    """manager 只用到 runtime.note。"""

    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(str(text))


def settings_for(profiles, data_dir, **extra):
    base = {'enabled': True, 'hostScript': str(HOST_SCRIPT), 'nodeExe': NODE or 'node',
            'sourceDir': str(SOURCE_DIR), 'extensionPath': '', 'catalogPath': '',
            'extensionsDir': '', 'autoDiscover': False, 'maxProfiles': 2, 'provider': '',
            'hookName': '', 'event': '', 'ownerOnly': False, 'ownerIds': '', 'injectPrompt': False,
            'timeout': 20, 'preAiBudgetMs': 0, 'toolAllowlist': '',
            'profiles': json.dumps(profiles, ensure_ascii=False)}
    base.update(extra)
    return base


def fixture_profile(**over):
    profile = {'extensionPath': str(FIXTURE), 'extensionId': 'qqagent_bridge_fixture', 'timeout': 20}
    profile.update(over)
    return profile


@unittest.skipUnless(READY, '需要 node + host-one.mjs + qq-agent-run 运行副本 + 夹具扩展')
class ManagerDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = self.manager._plugins['qqagent_bridge']
        self.runtime = NoteRuntime()

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, profiles, **extra):
        self.manager._settings['qqagent_bridge'] = settings_for(profiles, self.root, **extra)
        return self.plugin

    def assert_bridge_quiet(self):
        self.assertFalse([n for n in self.runtime.notes if 'QQ Agent' in n], self.runtime.notes)

    def test_manager_awaits_on_prepare(self):
        self.configure([fixture_profile(hookName='onMessage')])
        message = dict(MESSAGE)
        asyncio.run(self.manager.run_on_prepare(message, self.runtime))
        self.assert_bridge_quiet()
        stored = message.get('_qqagent_provider') or []
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0]['ok'])
        self.assertEqual(stored[0]['value'], {'kind': 'group', 'chatKey': 'group:12345'})

    def test_manager_awaits_on_post_reply(self):
        self.configure([fixture_profile(hookName='after-response')])
        message = dict(MESSAGE)
        reply = asyncio.run(self.manager.run_on_post_reply(message, '原文', self.runtime))
        self.assert_bridge_quiet()
        self.assertIsInstance(reply, str)
        self.assertIn('|FIXTURE', reply)
        self.assertTrue((message.get('_qqagent_provider') or [])[0]['ok'])

    def test_before_llm_messages_rewrites_real_conversation(self):
        self.configure([fixture_profile(hookName='before-llm-messages')])
        message = dict(MESSAGE)
        conversation = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': '你好'}]
        asyncio.run(self.manager.run_on_pre_ai_async(message, conversation, self.runtime))
        self.assert_bridge_quiet()
        self.assertEqual(conversation[-1]['content'], 'FIXTURE-INJECTED')
        self.assertEqual(len(conversation), 3)

    def test_before_tool_blocks_and_rewrites_arguments(self):
        self.configure([fixture_profile(hookName='before-tool')])
        blocked = asyncio.run(self.manager.run_on_before_tool('web_search', {'url': 'blocked'}, None))
        self.assertTrue(blocked['block'])
        self.assertEqual(blocked['reason'], 'fixture-blocked')
        passed = asyncio.run(self.manager.run_on_before_tool('web_search', {'url': 'ok'}, None))
        self.assertFalse(passed['block'])
        self.assertTrue(passed['arguments']['fixture'])

    def test_after_tool_replaces_output(self):
        self.configure([fixture_profile(hookName='after-tool')])
        result = asyncio.run(self.manager.run_on_after_tool('web_search', {'q': 1}, {'a': 1}, None))
        self.assertEqual(result, {'a': 1, 'fixture': True})

    def test_owner_chat_only_skips_strangers(self):
        self.configure([fixture_profile(hookName='onMessage')], ownerOnly=True, ownerIds='10000')
        stranger = dict(MESSAGE, sender_id='999')
        asyncio.run(self.manager.run_on_prepare(stranger, self.runtime))
        self.assertIsNone(stranger.get('_qqagent_provider'))
        owner = dict(MESSAGE, sender_id='10000')
        asyncio.run(self.manager.run_on_prepare(owner, self.runtime))
        self.assertTrue(owner.get('_qqagent_provider'))


@unittest.skipUnless(READY, '需要 node + host-one.mjs + 0.4 运行副本')
class ProcessIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_capability(self, capability, timeout=20, payload=None, kind='provider'):
        return asyncio.run(host.run_capability(
            node=NODE, host_script=HOST_SCRIPT, extension=FIXTURE, source_dir=SOURCE_DIR,
            capability=capability, payload=payload or {}, timeout=timeout, data_dir=self.data,
            kind=kind, extension_id='qqagent_bridge_fixture'))

    def test_timeout_kills_process_and_keeps_loop_alive(self):
        async def scenario():
            ticks = []
            started = time.monotonic()

            async def ticker():
                while True:
                    await asyncio.sleep(0.25)
                    ticks.append(time.monotonic() - started)

            task = asyncio.ensure_future(ticker())
            try:
                result = await host.run_capability(
                    node=NODE, host_script=HOST_SCRIPT, extension=FIXTURE, source_dir=SOURCE_DIR,
                    capability='fixture.slow', payload={}, timeout=2, data_dir=self.data,
                    kind='provider', extension_id='qqagent_bridge_fixture')
            finally:
                task.cancel()
            return result, ticks, time.monotonic() - started

        result, ticks, elapsed = asyncio.run(scenario())
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['ok'])
        self.assertIn('timeout', str(result['error']))
        self.assertLess(elapsed, 8.0)
        # 事件循环在子进程等待期间照常调度：至少 3 次 tick，且没有一次被卡住超过 1 秒
        self.assertGreaterEqual(len(ticks), 3, ticks)
        self.assertLess(max(b - a for a, b in zip([0.0] + ticks, ticks + [elapsed])), 1.0, ticks)

    def test_tools_are_enumerated_without_host_builtins(self):
        listed = asyncio.run(host.list_tools(node=NODE, host_script=HOST_SCRIPT, extension=FIXTURE,
                                             source_dir=SOURCE_DIR, timeout=20, data_dir=self.data,
                                             extension_id='qqagent_bridge_fixture'))
        self.assertTrue(listed['ok'], listed.get('error'))
        ids = [tool['id'] for tool in listed['tools']]
        self.assertIn('qqagent_bridge_fixture__echo', ids)
        self.assertNotIn('list_stickers', ids)

    def test_tool_call_roundtrip(self):
        result = asyncio.run(host.call_tool(node=NODE, host_script=HOST_SCRIPT, extension=FIXTURE,
                                            source_dir=SOURCE_DIR, tool_id='qqagent_bridge_fixture__echo',
                                            arguments={'text': 'hi'}, timeout=20, data_dir=self.data,
                                            extension_id='qqagent_bridge_fixture'))
        self.assertTrue(result['ok'], result.get('error'))
        self.assertEqual(result['value']['echoed'], 'hi')

    def test_every_call_is_recorded_in_jsonl(self):
        self.run_capability('fixture.echo', payload={'text': 'x'})
        rows = host.read_log(self.data)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for key in ('ts', 'kind', 'extension', 'ok', 'duration_ms'):
            self.assertIn(key, row)
        self.assertTrue(row['ok'])
        status = bridge.bridge_status(self.data)
        self.assertEqual(status['provider_calls'], 1)
        self.assertEqual(status['provider_failures'], 0)
        self.assertTrue(status['log_exists'])


@unittest.skipUnless(READY, '需要 node + host-one.mjs + qq-agent-run 运行副本 + 夹具扩展')
class SyncFallbackTests(unittest.TestCase):
    """src/qq_app.py:1666 目前走同步 run_on_pre_ai，桥必须留一条不阻塞的同步兜底。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = self.manager._plugins['qqagent_bridge']

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, budget):
        self.manager._settings['qqagent_bridge'] = settings_for(
            [fixture_profile(hookName='before-context')], self.root, preAiBudgetMs=budget)
        return self.plugin

    def test_sync_path_dispatches_when_budget_allows(self):
        plugin = self.configure(3000)
        message = dict(MESSAGE)
        plugin.on_pre_ai(message, [{'role': 'user', 'content': '你好'}], None)
        stored = message.get('_qqagent_provider') or []
        self.assertTrue(stored, '同步链路的返回值没有挂回消息')
        self.assertTrue(stored[0]['ok'])

    def test_budget_zero_skips_the_process(self):
        plugin = self.configure(0)
        message = dict(MESSAGE)
        plugin.on_pre_ai(message, [{'role': 'user', 'content': '你好'}], None)
        self.assertIsNone(message.get('_qqagent_provider'))
        self.assertEqual(bridge.bridge_status(self.root)['calls'], 0)


@unittest.skipUnless(READY, '需要 node + host-one.mjs + qq-agent-run 运行副本 + 夹具扩展')
class ToolLoopIntegrationTests(unittest.TestCase):
    """WP2 的工具环只认同步 list_tools()/call_tool(name, arguments)，这里按它的真实用法跑。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = self.manager._plugins['qqagent_bridge']

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self):
        self.manager._settings['qqagent_bridge'] = settings_for([fixture_profile()], self.root,
                                                                preAiBudgetMs=0)
        return self.plugin

    def test_refresh_then_register_and_invoke(self):
        from src import tool_loop

        plugin = self.configure()
        refreshed = asyncio.run(plugin.refresh_tools())
        self.assertTrue(refreshed['ok'], refreshed.get('error'))
        self.assertIn('qqagent_bridge_fixture__echo', [tool['name'] for tool in refreshed['tools']])
        self.assertTrue((self.root / 'qqagent_bridge_tools.json').is_file())

        registry = tool_loop.ToolRegistry()
        self.assertGreaterEqual(tool_loop.register_plugin_tools(registry, self.manager), 1)
        spec = registry.get('qqagent_bridge__qqagent_bridge_fixture__echo')
        self.assertIsNotNone(spec, registry.describe())
        value = spec.handler('qqagent_bridge_fixture__echo', {'text': 'hi'})
        self.assertTrue(value['ok'], value)
        self.assertEqual(value['value']['echoed'], 'hi')

    def test_disabled_bridge_offers_no_tools(self):
        self.configure()
        self.manager._settings['qqagent_bridge']['enabled'] = False
        self.assertEqual(self.plugin.list_tools(), [])
        self.assertEqual(self.plugin.call_tool('x', {})['error']['kind'], 'disabled')


if __name__ == '__main__':
    unittest.main()
