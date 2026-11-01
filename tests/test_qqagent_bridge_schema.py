"""工具参数 schema：源码声明优先，缺失/脏数据一律降级成合法空 schema（不改既有行为）。"""
from __future__ import annotations

import json
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src import extension_host as host
from src.plugins import qqagent_bridge as bridge
from src.plugins.manager import PluginManager

EMPTY = {'type': 'object', 'properties': {}}
REAL = {'type': 'object',
        'properties': {'query': {'type': 'string', 'description': '要查的名字'}},
        'required': ['query']}


def stub_module(tools, explode=False):
    """假的 src/tool_schema.py：同事模块落地前用它验证消费逻辑。"""
    module = types.SimpleNamespace()

    def extract_tools(directory, entry_files=None):
        if explode:
            raise ValueError('boom')
        return dict(tools)

    module.extract_tools = extract_tools
    module.schema_for = lambda directory, tool_id, fallback=None: dict(
        (tools.get(tool_id) or {}).get('parameters') or fallback or {})
    module.tool_catalog = lambda directory: list(tools.values())
    return module


def settings(profile_extra=None, **extra):
    base = {'enabled': True, 'hostScript': '', 'nodeExe': 'node', 'sourceDir': '',
            'extensionPath': '', 'catalogPath': '', 'extensionsDir': '', 'autoDiscover': False,
            'maxProfiles': 2, 'provider': '', 'hookName': '', 'event': '', 'ownerOnly': False,
            'ownerIds': '', 'injectPrompt': False, 'timeout': 10, 'preAiBudgetMs': 0,
            'toolAllowlist': '', 'profiles': ''}
    base.update(extra)
    if profile_extra is not None:
        base['profiles'] = json.dumps([profile_extra], ensure_ascii=False)
    return base


class SchemaCleaningTests(unittest.TestCase):
    def test_valid_object_schema_kept(self):
        self.assertEqual(bridge._clean_schema(dict(REAL)), REAL)

    def test_string_payload_degrades(self):
        self.assertEqual(bridge._clean_schema('query:string'), EMPTY)

    def test_none_and_missing_type_degrade(self):
        self.assertEqual(bridge._clean_schema(None), EMPTY)
        self.assertEqual(bridge._clean_schema({'properties': {'a': {'type': 'string'}}}), EMPTY)
        self.assertEqual(bridge._clean_schema({'type': 'array', 'properties': {}}), EMPTY)

    def test_properties_not_dict_degrades(self):
        self.assertEqual(bridge._clean_schema({'type': 'object', 'properties': ['query']}), EMPTY)

    def test_dirty_property_values_dropped(self):
        dirty = {'type': 'object',
                 'properties': {'query': {'type': 'string'}, 'bad': 'x', '': {'type': 'string'}}}
        self.assertEqual(bridge._clean_schema(dirty),
                         {'type': 'object', 'properties': {'query': {'type': 'string'}}})

    def test_required_and_additional_properties_cleaned(self):
        cleaned = bridge._clean_schema({'type': 'object', 'properties': {'a': {'type': 'string'}},
                                        'required': ['a', 'ghost'], 'additionalProperties': 'no'})
        self.assertEqual(cleaned['required'], ['a'])
        self.assertNotIn('additionalProperties', cleaned)

    def test_schema_module_missing_returns_none(self):
        module = bridge._schema_module()
        # 同事的模块存在时这里不该再判定"缺失"，只断言不会抛错。
        self.assertTrue(module is None or hasattr(module, 'extract_tools'))


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = self.manager._plugins['qqagent_bridge']
        self.ext = self.root / 'demo-ext'
        (self.ext / 'lib').mkdir(parents=True)
        (self.ext / 'index.js').write_text('// demo', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def enrich(self, tools, module):
        with mock.patch.object(bridge, '_schema_module', lambda: module):
            return self.plugin._enrich_tools(
                tools, {'extension': str(self.ext), 'extension_id': 'demo-ext'})

    def test_declared_schema_wins(self):
        module = stub_module({'ping': {'id': 'ping', 'description': '源码里的说明',
                                       'parameters': dict(REAL)}})
        tools, directory, stamp = self.enrich(
            [{'id': 'demo-ext__ping', 'name': 'ping', 'description': ''}], module)
        self.assertEqual(tools[0]['parameters'], REAL)
        self.assertEqual(tools[0]['description'], '源码里的说明')
        self.assertEqual(directory, str(self.ext))
        self.assertEqual(stamp, bridge._dir_stamp(str(self.ext)))

    def test_declaration_keyed_by_full_tool_name(self):
        module = stub_module({'demo-ext__ping': {'parameters': dict(REAL)}})
        tools, _directory, _stamp = self.enrich([{'id': 'demo-ext__ping'}], module)
        self.assertEqual(tools[0]['parameters'], REAL)

    def test_missing_schema_module_keeps_old_behaviour(self):
        tools, directory, stamp = self.enrich(
            [{'id': 'demo-ext__ping', 'name': 'ping', 'description': '宿主给的说明'}], None)
        self.assertEqual(tools[0]['parameters'], EMPTY)
        self.assertEqual(tools[0]['description'], '宿主给的说明')
        self.assertEqual(directory, str(self.ext))

    def test_extractor_raising_falls_back(self):
        tools, _directory, _stamp = self.enrich([{'id': 'demo-ext__ping'}],
                                                stub_module({}, explode=True))
        self.assertEqual(tools[0]['parameters'], EMPTY)

    def test_declared_without_parameters_degrades(self):
        module = stub_module({'ping': {'description': '无参数工具'}})
        tools, _directory, _stamp = self.enrich([{'id': 'demo-ext__ping'}], module)
        self.assertEqual(tools[0]['parameters'], EMPTY)
        self.assertEqual(tools[0]['description'], '无参数工具')

    def test_missing_extension_dir_is_silent(self):
        tools, directory, stamp = self.enrich([{'id': 'demo-ext__ping'}],
                                              stub_module({'ping': {'parameters': dict(REAL)}}))
        with mock.patch.object(bridge, '_schema_module', lambda: stub_module({'ping': {}})):
            tools, directory, stamp = self.plugin._enrich_tools(
                [{'id': 'demo-ext__ping'}], {'extension': str(self.root / 'nope'),
                                            'extension_id': 'demo-ext'})
        self.assertEqual(tools[0]['parameters'], EMPTY)
        self.assertEqual(directory, '')


class CachedListingTests(unittest.TestCase):
    """list_tools 的缓存契约：目录指纹一致时不得起 node 子进程。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = self.manager._plugins['qqagent_bridge']
        self.ext = self.root / 'demo-ext'
        (self.ext / 'lib').mkdir(parents=True)
        (self.ext / 'index.js').write_text('// demo', encoding='utf-8')
        self.host_script = self.root / 'host-one.mjs'
        self.host_script.write_text('// host', encoding='utf-8')
        self.manager._settings['qqagent_bridge'] = settings(
            {'extensionPath': str(self.ext), 'timeout': 10}, hostScript=str(self.host_script),
            sourceDir=str(self.root))
        self.stamp = bridge._dir_stamp(str(self.ext))
        self.tool = {'id': 'demo-ext__ping', 'name': 'ping', 'description': '源码说明',
                     'parameters': dict(REAL)}

    def tearDown(self):
        self.tmp.cleanup()

    def boom(*_args, **_kwargs):
        raise AssertionError('cache 命中时不许起 node 子进程')

    def test_cache_hit_serves_schema_without_node(self):
        self.plugin._write_tool_cache({'demo-ext': {'tools': [dict(self.tool)],
                                                   'dir': str(self.ext), 'stamp': self.stamp}})
        with mock.patch.object(host, 'run_capability_sync', self.boom):
            tools = self.plugin.list_tools()
        self.assertEqual(tools, [{'name': 'demo-ext__ping', 'description': '源码说明',
                                  'parameters': REAL}])

    def test_stamp_change_reenriches_without_node(self):
        self.plugin._write_tool_cache({'demo-ext': {'tools': [{'id': 'demo-ext__ping',
                                                              'name': 'ping',
                                                              'description': ''}],
                                                   'dir': str(self.ext), 'stamp': '0-0'}})
        module = stub_module({'ping': {'description': '源码说明', 'parameters': dict(REAL)}})
        with mock.patch.object(bridge, '_schema_module', lambda: module), \
                mock.patch.object(host, 'run_capability_sync', self.boom):
            tools = self.plugin.list_tools()
        self.assertEqual(tools[0]['parameters'], REAL)
        entry = self.plugin._load_tool_cache()['demo-ext']
        self.assertEqual(entry['stamp'], self.stamp)

    def test_legacy_cache_format_upgraded(self):
        path = self.plugin._tool_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'generated_at': 0, 'extensions': {
            'demo-ext': [{'id': 'demo-ext__ping', 'name': 'ping',
                          'description': '旧缓存', 'parameters': dict(REAL)}]}}),
            encoding='utf-8')
        with mock.patch.object(host, 'run_capability_sync', self.boom):
            tools = self.plugin.list_tools()
        self.assertEqual(tools, [{'name': 'demo-ext__ping', 'description': '旧缓存',
                                  'parameters': REAL}])

    def test_disabled_bridge_lists_nothing(self):
        self.manager._settings['qqagent_bridge'] = settings(enabled=False)
        self.assertEqual(self.plugin.list_tools(), [])

    def test_dirty_cached_parameters_cleaned(self):
        self.plugin._write_tool_cache({'demo-ext': {'tools': [{'id': 'demo-ext__ping',
                                                              'name': 'ping',
                                                              'description': '',
                                                              'parameters': 'query:string'}],
                                                   'dir': str(self.ext), 'stamp': self.stamp}})
        with mock.patch.object(host, 'run_capability_sync', self.boom):
            tools = self.plugin.list_tools()
        self.assertEqual(tools[0]['parameters'], EMPTY)


class RealSourceSchemaTests(unittest.TestCase):
    """真扩展源码上的声明：需要同事的 src/tool_schema.py 已落地，否则 skip。"""

    PLUGINS = Path('D:/聊天机器人/qq机器人/.tmp/qq-agent-run/plugins')
    CASES = {'account-appearance': 'target', 'bili-browse': 'target',
             'bili-dyn-notify': 'action'}

    def setUp(self):
        self.module = bridge._schema_module()

    def test_real_declarations_surface(self):
        if self.module is None:
            self.skipTest('src/tool_schema.py 还没落地')
        if not self.PLUGINS.is_dir():
            self.skipTest('没有 0.4 运行副本的 plugins 目录')
        seen = 0
        for ident, prop in self.CASES.items():
            directory = self.PLUGINS / ident
            if not directory.is_dir():
                continue
            table = self.module.extract_tools(str(directory)) or {}
            for entry in table.values():
                parameters = bridge._clean_schema((entry or {}).get('parameters'))
                if prop in parameters['properties']:
                    seen += 1
                    break
        self.assertGreater(seen, 0, '真扩展的 parameters 声明没被解析出来')


if __name__ == '__main__':
    unittest.main()
