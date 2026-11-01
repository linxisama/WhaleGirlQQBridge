"""真实市场扩展的端到端用例：证明桥能把扩展的返回值读回并影响调用方入参。

这些用例直接跑 D:/聊天机器人/插件市场 里的真扩展（zip 由 host-one.mjs 解包到临时目录），
不用夹具、不打桩。市场目录/0.4 运行副本/node 任一缺失则整类 skip。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src import extension_host as host
from src import extension_registry as ext_registry
from src.plugins import qqagent_bridge as bridge
from src.plugins.manager import PluginManager

REPO = Path(__file__).resolve().parents[1]
MARKET = Path('D:/聊天机器人/插件市场/QQAgent兼容层')
CATALOG = MARKET / 'catalog.json'
HOST_SCRIPT = MARKET / 'host-one.mjs'
SOURCE_DIR = REPO / '.tmp' / 'qq-agent-run'
ZIP_DIR = REPO / '.tmp' / 'external-market'
NODE = shutil.which('node')
READY = bool(NODE and HOST_SCRIPT.is_file() and CATALOG.is_file() and SOURCE_DIR.is_dir())

EXTENSIONS = {}
if READY:
    EXTENSIONS = {item['id']: item for item in host.discover_extensions(CATALOG)}

MESSAGE = {'kind': 'group', 'target': '12345', 'sender_id': '10000', 'sender_name': 'Alice',
           'text': '你好', 'message_id': 'm1', 'self_id': '10000'}


def have(extension_id):
    return unittest.skipUnless(extension_id in EXTENSIONS, '市场里没有扩展 %s' % extension_id)


@unittest.skipUnless(READY, '需要 node + 市场 catalog.json + host-one.mjs + qq-agent-run')
class MarketEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = self.manager._plugins['qqagent_bridge']

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, profile, enable=True, **extra):
        # 市场扩展在扩展管理里默认停用；真实用户必须先启用。桥现在对钩子面也执行这条门禁，
        # 所以用例照同样的规则走，而不是靠"未登记的 id 才放行"的旧行为。
        ident = str(profile.get('extensionId') or '').strip()
        if ident and enable:
            ext_registry.set_enabled(ident, True, self.root)
        base = {'enabled': True, 'hostScript': str(HOST_SCRIPT), 'nodeExe': NODE,
                'sourceDir': str(SOURCE_DIR), 'extensionPath': '', 'catalogPath': '',
                'extensionsDir': '', 'autoDiscover': False, 'maxProfiles': 2, 'provider': '',
                'hookName': '', 'event': '', 'ownerOnly': False, 'ownerIds': '',
                'injectPrompt': False, 'timeout': 25, 'preAiBudgetMs': 0, 'toolAllowlist': ''}
        base.update(extra)
        base['profiles'] = json.dumps([dict({'timeout': 25}, **profile)], ensure_ascii=False)
        self.manager._settings['qqagent_bridge'] = base
        return self.plugin

    @have('thinking-toggle-pro')
    def test_request_params_provider_rewrites_body(self):
        profile = {'extensionPath': str(EXTENSIONS['thinking-toggle-pro']['file']),
                   'extensionId': 'thinking-toggle-pro', 'provider': 'llm.request-params'}
        plugin = self.configure(profile)
        body = {'model': 'deepseek-reasoner', 'temperature': 0.9, 'stream': False,
                'messages': [{'role': 'user', 'content': 'SENTINEL-USER'}]}
        out = asyncio.run(plugin.transform_request(body, {'chatKey': 'group:1',
                                                          'model': 'deepseek-reasoner'}))
        self.assertIsInstance(out['body'], dict)
        self.assertTrue(out['omitTemperature'], out)
        added = set(out['body']) - set(body)
        self.assertTrue(added, '扩展没有改写 body：%s' % json.dumps(out['body'], ensure_ascii=False))
        self.assertEqual(out['body']['messages'][0]['content'], 'SENTINEL-USER')
        self.assertEqual(len(body), 4, '调用方传入的 body 被就地改写')
        status = plugin.bridge_status()
        self.assertGreaterEqual(status['provider_calls'], 1)
        self.assertIn('thinking-toggle-pro|provider|llm.request-params',
                      [c['extensionId'] + '|' + c['kind'] + '|' + c['capability']
                       for c in status['capabilities']])

    @have('search-tuner')
    def test_before_tool_hook_blocks_real_dispatch(self):
        profile = {'extensionPath': str(EXTENSIONS['search-tuner']['file']),
                   'extensionId': 'search-tuner', 'hookName': 'before-tool'}
        plugin = self.configure(profile)
        result = asyncio.run(plugin.on_before_tool('web_search', {'query': '今天天气'}, None))
        self.assertTrue(result.get('block'), result)
        self.assertTrue(str(result.get('reason') or '').strip())
        self.assertGreaterEqual(plugin.bridge_status()['hook_calls'], 1)

    @have('pai-plugins')
    def test_before_context_hook_reads_full_payload(self):
        profile = {'extensionPath': str(EXTENSIONS['pai-plugins']['file']),
                   'extensionId': 'pai-plugins', 'hookName': 'before-context'}
        plugin = self.configure(profile)
        message = dict(MESSAGE)
        conversation = [{'role': 'user', 'content': '你好'}]
        asyncio.run(plugin.on_pre_ai_async(message, conversation, None))
        stored = message.get('_qqagent_provider') or []
        self.assertTrue(stored, '扩展返回值没有挂回消息')
        value = stored[0]['value']
        self.assertIsInstance(value, dict)
        self.assertEqual(value['triggerEntries'][0]['text'], '你好')
        self.assertEqual(value['triggerEntries'][0]['senderId'], '10000')

    @have('owner-identity')
    def test_owner_check_provider_returns_contract_shape(self):
        profile = {'extensionPath': str(EXTENSIONS['owner-identity']['file']),
                   'extensionId': 'owner-identity', 'provider': 'message.owner-check'}
        plugin = self.configure(profile)
        message = dict(MESSAGE)
        asyncio.run(plugin.on_prepare(message, None))
        stored = message.get('_qqagent_provider') or []
        self.assertTrue(stored)
        self.assertIn('ownerMode', stored[0]['value'])

    def test_discovery_sees_hook_and_tool_capable_extensions(self):
        hooks = bridge.catalog_hook_profiles(CATALOG, ZIP_DIR, limit=5)
        self.assertEqual(len(hooks), 5)
        for item in hooks:
            self.assertIn(item['hookName'], bridge.HOOK_EVENT)
            self.assertTrue(Path(item['extensionPath']).exists())
        tools = bridge.catalog_tool_profiles(CATALOG, ZIP_DIR, limit=5)
        self.assertTrue(tools, '自动发现看不到任何 tool 型扩展')
        mixed = bridge.discover_profiles(CATALOG, ZIP_DIR, limit=6)
        kinds = {item['kind'] for item in mixed}
        self.assertEqual(kinds, {'provider', 'hook', 'tool'})
        discovered = host.discover_extensions(CATALOG)
        self.assertGreater(sum(1 for item in discovered if item['hooks']), 250)
        self.assertGreater(sum(1 for item in discovered if item['tools']), 150)


    @have('pai-plugins')
    def test_tool_loop_sees_only_extension_owned_tools(self):
        """裸 capability='' 会把宿主自带工具一起带回来，桥必须只注册扩展自己的。"""
        from src import tool_loop

        ext_registry.set_enabled('pai-plugins', True, self.root)
        profile = {'extensionPath': str(EXTENSIONS['pai-plugins']['file']),
                   'extensionId': 'pai-plugins'}
        plugin = self.configure(profile)
        cache = Path(self.root) / 'qqagent_bridge_tools.json'
        if cache.is_file():
            cache.unlink()
        names = [tool['name'] for tool in plugin.list_tools()]
        self.assertIn('pai-plugins__check', names)
        self.assertTrue(all(name.startswith('pai-plugins__') for name in names), names[:5])

        registry = tool_loop.ToolRegistry()
        self.assertGreaterEqual(tool_loop.register_plugin_tools(registry, self.manager), 1)
        self.assertTrue(all(name.startswith('qqagent_bridge__pai-plugins__')
                            for name in registry.names()), registry.names()[:5])
        spec = registry.get('qqagent_bridge__pai-plugins__check')
        self.assertIsNotNone(spec)
        result = spec.handler(spec.raw_name, {})
        self.assertTrue(result['ok'], result)
        self.assertIn('value', result)

    @have('pai-plugins')
    def test_registry_disabled_extension_exposes_no_tools(self):
        """在扩展管理里停用的扩展不得再向模型暴露工具，冷/热两次调用都必须是空。"""
        ext_registry.set_enabled('pai-plugins', False, self.root)
        profile = {'extensionPath': str(EXTENSIONS['pai-plugins']['file']),
                   'extensionId': 'pai-plugins'}
        plugin = self.configure(profile, enable=False)
        self.assertEqual(plugin.list_tools(), [])
        self.assertEqual(plugin.list_tools(), [])
        self.assertFalse((Path(self.root) / 'qqagent_bridge_tools.json').is_file())


if __name__ == '__main__':
    unittest.main()
