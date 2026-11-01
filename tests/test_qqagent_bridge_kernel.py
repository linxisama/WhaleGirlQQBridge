"""桥内核纯逻辑测试：事件映射、payload 契约、返回值落地、清单发现、准入。

不启动 Node，可在没有 QQ Agent 0.4 源码的机器上跑。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.plugins import qqagent_bridge as bridge
from src.plugins.qqagent_bridge import QqAgentBridge

CONTRACT_FIELDS = ('triggerEntries', 'selfId', 'persona', 'messages', 'session', 'response',
                   'toolName', 'argsRaw', 'result', 'store', 'memory')


class StubManager:
    """只实现插件真正用到的那两个接口，避免依赖真实 manager。"""

    def __init__(self, settings, data_dir):
        self.settings = dict(settings)
        self.data_dir = Path(data_dir)

    def get_settings(self, plugin_id):
        return dict(self.settings)


def make_plugin(tmp_dir, **settings):
    base = {'enabled': True, 'hostScript': '', 'nodeExe': 'node', 'sourceDir': '',
            'extensionPath': '', 'catalogPath': '', 'extensionsDir': '', 'autoDiscover': False,
            'maxProfiles': 2, 'provider': '', 'hookName': '', 'event': '', 'ownerOnly': False,
            'ownerIds': '', 'injectPrompt': False, 'timeout': 10, 'preAiBudgetMs': 0,
            'toolAllowlist': '', 'profiles': ''}
    base.update(settings)
    return QqAgentBridge(StubManager(base, tmp_dir))


class EventMappingTests(unittest.TestCase):
    def test_six_market_hooks_map_to_python_events(self):
        self.assertEqual(bridge.HOOK_EVENT, {
            'before-context': 'on_pre_ai',
            'before-llm-messages': 'on_pre_ai',
            'onMessage': 'on_prepare',
            'after-response': 'on_post_reply',
            'before-tool': 'on_before_tool',
            'after-tool': 'on_after_tool',
        })

    def test_profile_event_prefers_hook_then_event_then_provider(self):
        self.assertEqual(bridge._profile_event({'hookName': 'before-tool'}), 'on_before_tool')
        self.assertEqual(bridge._profile_event({'hookName': 'after-tool'}), 'on_after_tool')
        self.assertEqual(bridge._profile_event({'event': 'on_pre_ai'}), 'on_pre_ai')
        self.assertEqual(bridge._profile_event({'provider': 'reply.local-policy'}), 'on_post_reply')
        self.assertEqual(bridge._profile_event({}), '')

    def test_capability_rejects_unknown_names(self):
        self.assertEqual(bridge._capability({'hookName': 'before-context'}), ('hook', 'hook', 'before-context'))
        self.assertEqual(bridge._capability({'provider': 'llm.request-params'}),
                         ('provider', 'llm.request-params', ''))
        self.assertIsNone(bridge._capability({'provider': 'tool.guard'}))
        self.assertIsNone(bridge._capability({'hookName': 'not-a-hook'}))
        self.assertIsNone(bridge._capability({}))

    def test_bridge_hooks_are_coroutine_where_manager_awaits(self):
        self.assertTrue(inspect.iscoroutinefunction(QqAgentBridge.on_prepare))
        self.assertTrue(inspect.iscoroutinefunction(QqAgentBridge.on_post_reply))
        self.assertTrue(inspect.iscoroutinefunction(QqAgentBridge.on_before_tool))
        self.assertTrue(inspect.iscoroutinefunction(QqAgentBridge.on_after_tool))
        self.assertTrue(inspect.iscoroutinefunction(QqAgentBridge.on_pre_ai_async))
        # 同步链路（src/qq_app.py 走 run_on_pre_ai）必须仍是普通函数
        self.assertFalse(inspect.iscoroutinefunction(QqAgentBridge.on_pre_ai))


class PayloadContractTests(unittest.TestCase):
    MESSAGE = {'kind': 'group', 'target': '12345', 'sender_id': '999', 'sender_name': 'Alice',
               'text': '你好', 'message_id': 'm1', 'group_name': '测试群'}

    def test_payload_carries_every_04_field(self):
        conversation = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': '你好'}]
        payload = bridge.stage_payload('on_pre_ai', message=self.MESSAGE, conversation=conversation,
                                       reply=None, tool_name='', args_raw=None, result=None)
        for name in CONTRACT_FIELDS:
            self.assertIn(name, payload)
        self.assertEqual(payload['chatKey'], 'group:12345')
        self.assertEqual(payload['kind'], 'group')
        self.assertEqual(payload['chatId'], '12345')
        self.assertEqual(payload['triggerEntries'][0]['senderId'], '999')
        self.assertEqual(payload['triggerEntries'][0]['text'], '你好')
        self.assertEqual(payload['messages'], conversation)
        # 给不出的字段显式 None，并在 GAP_FIELDS 里写明来源
        for name in ('store', 'memory', 'session'):
            self.assertIsNone(payload[name])
            self.assertIn(name, bridge.GAP_FIELDS)

    def test_tool_payload_shape(self):
        payload = bridge.stage_payload('on_before_tool', message={'tool': 'web_search'},
                                       tool_name='web_search', args_raw={'query': 'x'})
        self.assertEqual(payload['toolName'], 'web_search')
        self.assertEqual(payload['argsRaw'], {'query': 'x'})
        self.assertEqual(payload['arguments'], {'query': 'x'})
        self.assertEqual(payload['tool'], 'web_search')

    def test_apply_hook_result_writes_back_each_contract(self):
        conversation = [{'role': 'user', 'content': 'old'}]
        changes = bridge.apply_hook_result('on_pre_ai', {'messages': [{'role': 'user', 'content': 'new'}]},
                                          conversation=conversation)
        self.assertTrue(changes['changed'])
        self.assertEqual(conversation, [{'role': 'user', 'content': 'new'}])

        reply = bridge.apply_hook_result('on_post_reply', {'response': '改过的回复'}, reply='原回复')
        self.assertEqual(reply['reply'], '改过的回复')

        blocked = bridge.apply_hook_result('on_before_tool', {'block': True, 'reason': 'no'},
                                           arguments={'a': 1})
        self.assertTrue(blocked['block'])
        self.assertEqual(blocked['reason'], 'no')

        args = bridge.apply_hook_result('on_before_tool', {'argsRaw': {'a': 2}}, arguments={'a': 1})
        self.assertEqual(args['arguments'], {'a': 2})

        after = bridge.apply_hook_result('on_after_tool', {'result': {'y': 2}}, result={'y': 1})
        self.assertEqual(after['result'], {'y': 2})

        prompt = bridge.apply_hook_result('on_pre_ai', {'extra_system': '规则'})
        self.assertEqual(prompt['prompt'], '规则')

        untouched = bridge.apply_hook_result('on_pre_ai', None, conversation=conversation)
        self.assertFalse(untouched['changed'])

    def test_owner_check_rules_reach_the_prompt(self):
        """owner-identity 这类 provider 把规则放在 rules 键里；不认它扩展就等于没装。"""
        changes = bridge.apply_hook_result('on_prepare', {'ownerMode': True, 'rules': '只看 QQ 号'})
        self.assertTrue(changes['changed'])
        self.assertEqual(changes['prompt'], '只看 QQ 号')
        empty = bridge.apply_hook_result('on_prepare', {'ownerMode': False, 'rules': '   '})
        self.assertFalse(empty['changed'])
        kept = bridge.apply_hook_result('on_prepare', {'prompt': '钩子给的', 'rules': 'provider 给的'})
        self.assertEqual(kept['prompt'], '钩子给的', '两者都有时先到先得，别来回覆盖')

    def test_trigger_entry_keeps_at_me_flag(self):
        """@ 标记丢了，按"主人在叫我"判定的人设切换就永远不成立。"""
        payload = bridge.stage_payload('on_prepare', message={'sender_id': '10001', 'text': 'hi',
                                                             'at': True})
        entry = payload['triggerEntries'][0]
        self.assertIs(entry['atMe'], True)
        self.assertIs(entry['at'], True)
        self.assertEqual(entry['senderId'], '10001')
        quiet = bridge.stage_payload('on_prepare', message={'sender_id': '10001', 'text': 'hi'})
        self.assertIs(quiet['triggerEntries'][0]['atMe'], False)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ext = self.root / 'extensions'
        self.ext.mkdir()
        self.catalog = self.root / 'catalog.json'

    def tearDown(self):
        self.tmp.cleanup()

    def _zip(self, name):
        path = self.ext / ('%s.zip' % name)
        path.write_bytes(b'PK\x05\x06' + b'\0' * 18)
        return path

    def _entry(self, name, caps=(), tools=()):
        return {'id': name, 'kind': 'plugin', 'file': str(self._zip(name)),
                'capabilities': list(caps), 'tools': list(tools), 'host_imports': []}

    def test_hook_profiles_come_from_v4_manifest_when_catalog_lacks_hooks(self):
        self.catalog.write_text(json.dumps([
            self._entry('ctx-only'),
            self._entry('tool-first'),
            self._entry('plain-provider', caps=['message.owner-check']),
        ]), encoding='utf-8')
        (self.root / 'all-plugins-v4.json').write_text(json.dumps([
            {'id': 'ctx-only', 'extension': {'hooks': ['before-context'], 'providers': [], 'tools': []}},
            {'id': 'tool-first', 'extension': {'hooks': ['before-tool'], 'providers': [], 'tools': []}},
            {'id': 'plain-provider', 'extension': {'hooks': ['after-response']}},
        ]), encoding='utf-8')
        profiles = bridge.catalog_hook_profiles(self.catalog, self.ext, limit=2)
        self.assertEqual(len(profiles), 2)
        # before-tool 能真正改变宿主行为，排最前
        self.assertEqual([item['hookName'] for item in profiles], ['before-tool', 'before-context'])
        self.assertEqual([item['event'] for item in profiles], ['on_before_tool', 'on_pre_ai'])
        self.assertTrue(all(item['enabled'] for item in profiles))

    def test_tool_profiles_and_discovery_order(self):
        self.catalog.write_text(json.dumps([
            self._entry('with-tool', tools=['echo']),
            self._entry('with-hook'),
            self._entry('with-provider', caps=['message.owner-check']),
        ]), encoding='utf-8')
        (self.root / 'all-plugins-v4.json').write_text(json.dumps([
            {'id': 'with-tool', 'extension': {'hooks': [], 'tools': ['echo']}},
            {'id': 'with-hook', 'extension': {'hooks': ['before-context'], 'tools': []}},
        ]), encoding='utf-8')
        tools = bridge.catalog_tool_profiles(self.catalog, self.ext, limit=1)
        self.assertEqual(tools[0]['tools'], ['echo'])
        kinds = [item['kind'] for item in bridge.discover_profiles(self.catalog, self.ext, limit=3)]
        self.assertEqual(kinds, ['provider', 'hook', 'tool'])

    def test_hooks_map_sidecar_is_loaded(self):
        (self.root / 'hooks-map.json').write_text(json.dumps({
            'hooks': {'before-context': {'python': 'on_pre_ai'}}}), encoding='utf-8')
        data = bridge.host.load_hooks_map(self.catalog)
        self.assertEqual(data['hooks']['before-context']['python'], 'on_pre_ai')


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_owner_gate_blocks_strangers(self):
        profiles = [{'extensionPath': str(self.root / 'x.zip'), 'extensionId': 'x',
                     'provider': '', 'hookName': 'onMessage', 'timeout': 5}]
        plugin = make_plugin(self.root, ownerOnly=True, ownerIds='10000',
                             profiles=json.dumps(profiles))
        self.assertEqual(plugin._event_profiles({'sender_id': '999'}, 'on_prepare'), [])
        self.assertEqual(len(plugin._event_profiles({'sender_id': '10000'}, 'on_prepare')), 1)

    def test_disabled_bridge_never_dispatches(self):
        profiles = [{'extensionPath': str(self.root / 'x.zip'), 'hookName': 'before-context'}]
        plugin = make_plugin(self.root, enabled=False, profiles=json.dumps(profiles))
        self.assertEqual(plugin._event_profiles({'sender_id': '1'}, 'on_pre_ai'), [])

    def test_tool_allowlist_blocks_before_spawning_node(self):
        plugin = make_plugin(self.root, toolAllowlist='allowed_only')
        result = plugin.call_tool('not_allowed', {})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['kind'], 'denied')
        self.assertIn('toolAllowlist', result['error']['message'])

    def test_tool_loop_contract_shapes(self):
        """tool_loop.register_plugin_tools 只认 list_tools() 的同步返回值。"""
        plugin = make_plugin(self.root, toolAllowlist='')
        self.assertIsInstance(plugin.list_tools(), list)
        self.assertFalse(asyncio.iscoroutinefunction(plugin.list_tools))
        self.assertFalse(asyncio.iscoroutinefunction(plugin.call_tool))
        self.assertTrue(asyncio.iscoroutinefunction(bridge.QqAgentBridge.refresh_tools))
        self.assertEqual(bridge._strip_tool_prefix('qqagent_bridge__pai-plugins__check'),
                         'pai-plugins__check')
        self.assertEqual(bridge._strip_tool_prefix('web_search'), 'web_search')

    def test_bridge_status_without_log(self):
        status = bridge.bridge_status(self.root)
        self.assertFalse(status['log_exists'])
        self.assertEqual(status['provider_calls'], 0)
        self.assertEqual(status['hook_calls'], 0)
        self.assertEqual(status['tool_calls'], 0)
        plugin = make_plugin(self.root, profiles=json.dumps([]))
        self.assertEqual(plugin.bridge_status()['capabilities'], [])


class AfterToolPayloadTests(unittest.TestCase):
    """工具环传裸字符串，但 0.4 的 after-tool 契约要求 result 是对象。"""

    def test_string_result_is_wrapped_for_the_hook_and_unwrapped_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = make_plugin(tmp)
            profile = {'extensionId': 'token-saver', 'hookName': 'after-tool',
                       'event': 'on_after_tool', 'timeout': 5}
            seen = {}

            async def fake_stage(profile, payload, event):
                seen['payload'] = payload
                return {'ok': True, 'value': {'result': {'content': 'TRIMMED'}}}

            with mock.patch.object(plugin, '_event_profiles', lambda *a, **k: [profile]), \
                    mock.patch.object(plugin, '_stage', fake_stage):
                out = asyncio.run(plugin.on_after_tool('demo__blob', {}, 'A' * 9000, None))

        self.assertEqual(seen['payload']['result'], {'content': 'A' * 9000})
        self.assertEqual(out, 'TRIMMED')

    def test_object_result_still_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = make_plugin(tmp)
            profile = {'extensionId': 'x', 'hookName': 'after-tool',
                       'event': 'on_after_tool', 'timeout': 5}

            async def fake_stage(profile, payload, event):
                return {'ok': True, 'value': {'result': {'a': 2}}}

            with mock.patch.object(plugin, '_event_profiles', lambda *a, **k: [profile]), \
                    mock.patch.object(plugin, '_stage', fake_stage):
                out = asyncio.run(plugin.on_after_tool('demo__blob', {}, {'a': 1}, None))

        self.assertEqual(out, {'a': 2})


class RequestParamsRoutingTests(unittest.TestCase):
    """llm.request-params 只能由 transform_request 拿真实请求体，不能在 pre-AI 空跑。"""

    def test_request_params_provider_is_not_dispatched_on_pre_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles = [{'extensionId': 'token-saver', 'provider': 'llm.request-params',
                         'hookName': '', 'event': 'on_pre_ai', 'timeout': 5}]
            plugin = make_plugin(tmp, profiles=json.dumps(profiles))
            seen = []

            async def fake_stage(profile, payload, event):
                seen.append(event)
                return {'ok': True, 'value': None}

            with mock.patch.object(plugin, '_stage', fake_stage):
                asyncio.run(plugin.on_pre_ai_async({'sender_id': '1'}, [], None))

        self.assertEqual(seen, [])


if __name__ == '__main__':
    unittest.main()
