"""WP13：桥的三处收尾修正 —— 计数口径 / 注释误判 / 目录扫描候选顺序。

用例只碰临时目录，不依赖本机是否装了宿主。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import extension_host as host
from src.plugins import qqagent_bridge as bridge
from src.plugins.manager import PluginManager

HOOK_SOURCE = "module.exports = { hooks: { 'before-context': async () => null } };" + chr(10)

# 只在注释和普通字符串里出现能力名：真代码里一个声明都没有。
NOISY_SOURCE = (
    '// 这个扩展以前用 message.gate 做门禁，已弃用' + chr(10)
    + '/* 备注：message.gate / message.identity 都不再接了 */' + chr(10)
    + "const NOTE = 'message.gate is gone';" + chr(10)
    + 'const BANNER = "message.identity 也不行";' + chr(10)
    + 'module.exports = { setup() {} };' + chr(10))

# 真声明：provider/hook 名是带引号的对象键（host-one.mjs 就是这么读的）。
REAL_SOURCE = (
    'module.exports = {' + chr(10)
    + "  providers: { 'message.gate': async () => null }," + chr(10)
    + '  hooks: { "before-tool": async () => null },' + chr(10)
    + '};' + chr(10))


def write_extension(folder: Path, ident='demo-ext', source=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'plugin.json').write_text(json.dumps({'id': ident, 'name': ident, 'version': '1.0.0'},
                                                   ensure_ascii=False), encoding='utf-8')
    (folder / 'index.js').write_text(source or 'module.exports = { setup() {} };' + chr(10),
                                     encoding='utf-8')
    (folder / 'package.json').write_text('{"type": "commonjs"}' + chr(10), encoding='utf-8')
    return folder


def append_row(data_dir, kind, capability='demo-ext__tool') -> None:
    """用真正的写入口 _record() 造账本行，不手抄 JSON。"""
    host._record({'kind': kind, 'extension': 'demo-ext', 'argument': capability, 'ok': True,
                  'duration_ms': 1, 'timeout_s': 1.0, 'pid': 4321}, 1.0, data_dir, 'sync')


def configure(manager, data_dir, **over):
    base = {'enabled': True, 'hostScript': '', 'nodeExe': 'node', 'sourceDir': '',
            'extensionPath': '', 'catalogPath': '', 'extensionsDir': '', 'autoDiscover': False,
            'maxProfiles': 2, 'provider': '', 'hookName': '', 'event': '', 'ownerOnly': False,
            'ownerIds': '', 'injectPrompt': False, 'timeout': 10, 'preAiBudgetMs': 0,
            'toolAllowlist': '', 'profiles': ''}
    base.update(over)
    manager._settings['qqagent_bridge'] = base
    return manager._plugins['qqagent_bridge']


class StatusRowCountTests(unittest.TestCase):
    """A：calls 保持"账本总行数"，枚举行拆进 list_rows。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / 'data'
        self.data.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_calls_is_total_rows_and_list_rows_carves_out_enumeration(self):
        for _ in range(3):
            append_row(self.data, 'list', capability='')
        append_row(self.data, 'hook', 'before-tool')
        status = bridge.bridge_status(self.data)
        self.assertEqual(status['calls'], 4)          # 总行数口径不变
        self.assertEqual(status['hook_calls'], 1)     # 真实派发次数
        self.assertEqual(status['list_rows'], 3)      # 新字段：枚举行
        self.assertEqual(status['provider_calls'], 0)
        self.assertEqual(status['tool_calls'], 0)

    def test_identity_holds_for_all_four_kinds(self):
        for kind in ('provider', 'hook', 'tool', 'list'):
            append_row(self.data, kind)
        status = bridge.bridge_status(self.data)
        total = (status['provider_calls'] + status['hook_calls']
                 + status['tool_calls'] + status['list_rows'])
        self.assertEqual(status['calls'], total)
        for name in ('provider_calls', 'hook_calls', 'tool_calls', 'list_rows'):
            self.assertIsInstance(status[name], int)

    def test_no_synonym_keys_are_added(self):
        for _ in range(2):
            append_row(self.data, 'list')
        status = bridge.bridge_status(self.data)
        for name in ('providerCalls', 'hookCalls', 'toolCalls', 'listRows', 'enumerations'):
            self.assertNotIn(name, status)

    def test_plugin_status_exposes_the_same_split(self):
        manager = PluginManager(self.data)
        plugin = configure(manager, self.data)
        for _ in range(3):
            append_row(self.data, 'list')
        append_row(self.data, 'tool', 'demo-ext__echo')
        status = plugin.bridge_status()
        self.assertEqual(status['calls'], 4)
        self.assertEqual(status['tool_calls'], 1)
        self.assertEqual(status['list_rows'], 3)
        self.assertIn('capabilities', status)

    def test_empty_ledger_keeps_zero_calls(self):
        status = bridge.bridge_status(self.data)
        self.assertEqual(status['calls'], 0)
        self.assertEqual(status['list_rows'], 0)
        self.assertFalse(status['log_exists'])


class CommentMaskTests(unittest.TestCase):
    """B：注释与普通字符串里的能力名不算声明。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugins = Path(self.tmp.name) / 'plugins'
        self.plugins.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_comment_and_value_strings_are_not_declarations(self):
        write_extension(self.plugins / 'noisy', ident='noisy', source=NOISY_SOURCE)
        items = {item['id']: item for item in bridge.scan_extension_dirs(self.plugins)}
        self.assertNotIn('message.gate', items['noisy']['providers'])
        self.assertNotIn('message.identity', items['noisy']['providers'])
        self.assertEqual(items['noisy']['providers'], [])

    def test_real_key_declaration_is_still_found(self):
        write_extension(self.plugins / 'real', ident='real', source=REAL_SOURCE)
        items = {item['id']: item for item in bridge.scan_extension_dirs(self.plugins)}
        self.assertEqual(items['real']['providers'], ['message.gate'])
        self.assertEqual(items['real']['hooks'], ['before-tool'])

    def test_quoted_key_inside_comment_is_still_ignored(self):
        write_extension(self.plugins / 'tricky', ident='tricky', source=(
            "// { 'message.gate': async () => null } 是旧写法" + chr(10)
            + '/* [\'message.owner-check\'] */' + chr(10)
            + 'module.exports = { setup() {} };' + chr(10)))
        items = {item['id']: item for item in bridge.scan_extension_dirs(self.plugins)}
        self.assertEqual(items['tricky']['providers'], [])

    def test_mask_keeps_length_and_line_count(self):
        code = bridge._code_text(NOISY_SOURCE)
        self.assertEqual(len(code), len(NOISY_SOURCE))
        self.assertEqual(code.count(chr(10)), NOISY_SOURCE.count(chr(10)))

    def test_missing_schema_module_falls_back_to_raw_text(self):
        write_extension(self.plugins / 'real', ident='real', source=REAL_SOURCE)
        with mock.patch.object(bridge, '_schema_module', lambda: None):
            items = {item['id']: item for item in bridge.scan_extension_dirs(self.plugins)}
        self.assertEqual(items['real']['providers'], ['message.gate'])

    def test_broken_mask_falls_back_to_raw_text(self):
        write_extension(self.plugins / 'real', ident='real', source=REAL_SOURCE)
        broken = mock.Mock()
        broken._mask.side_effect = RuntimeError('mask 炸了')
        with mock.patch.object(bridge, '_schema_module', lambda: broken):
            items = {item['id']: item for item in bridge.scan_extension_dirs(self.plugins)}
        self.assertEqual(items['real']['providers'], ['message.gate'])


class DirScanOrderTests(unittest.TestCase):
    """C：无 catalogPath 时，候选在截断之前按工具数降序 → 名字升序。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.data = self.base / 'data'
        self.data.mkdir(parents=True)
        self.plugins = self.base / 'plugins'
        write_extension(self.plugins / 'aaa-plain', ident='aaa-plain', source=HOOK_SOURCE)
        write_extension(self.plugins / 'zzz-tools', ident='zzz-tools', source=HOOK_SOURCE)

    def tearDown(self):
        self.tmp.cleanup()

    def write_cache(self, table):
        (self.data / bridge.TOOL_CACHE_NAME).write_text(
            json.dumps({'generated_at': 0, 'extensions': table}, ensure_ascii=False),
            encoding='utf-8')

    def selected(self, **over):
        manager = PluginManager(self.data)
        plugin = configure(manager, self.data, autoDiscover=True, maxProfiles=1,
                           extensionsDir=str(self.plugins), **over)
        profiles = plugin._profiles()
        return profiles[0] if profiles else {}

    def test_prefers_candidate_with_more_tools(self):
        self.write_cache({'zzz-tools': {'tools': [{'id': 'zzz-tools__one'},
                                                  {'id': 'zzz-tools__two'}],
                                        'dir': '', 'stamp': ''}})
        self.assertEqual([item['id'] for item in bridge.scan_extension_dirs(self.plugins)],
                         ['aaa-plain', 'zzz-tools'])        # 目录序仍是 aaa 在前
        self.assertEqual(self.selected()['extensionId'], 'zzz-tools')

    def test_without_cache_name_order_still_decides(self):
        self.assertEqual(self.selected()['extensionId'], 'aaa-plain')

    def test_empty_shell_drops_behind_tool_bearing_candidate(self):
        self.write_cache({'aaa-plain': {'tools': [], 'dir': '', 'stamp': ''},
                          'zzz-tools': {'tools': [{'id': 'zzz-tools__one'}],
                                        'dir': '', 'stamp': ''}})
        self.assertEqual(self.selected()['extensionId'], 'zzz-tools')

    def test_discovery_path_starts_no_process(self):
        self.write_cache({'zzz-tools': {'tools': [{'id': 'zzz-tools__one'}],
                                        'dir': '', 'stamp': ''}})
        seen = []
        real = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            seen.append(args[:1])
            return await real(*args, **kwargs)

        with mock.patch('asyncio.create_subprocess_exec', spy):
            self.selected()
        self.assertEqual(len(seen), 0)

    def test_catalog_path_keeps_its_own_order(self):
        # 有清单时 _discovered() 直接用 host 的结果，不打工具数、不改序。
        items = [{'id': 'aaa-plain', 'hooks': ['before-context'], 'tools': [], 'file': 'x'},
                 {'id': 'zzz-tools', 'hooks': ['before-context'], 'tools': [1, 2], 'file': 'y'}]
        with mock.patch.object(host, 'discover_extensions', lambda *a, **k: list(items)):
            discovered = bridge._discovered('catalog.json', str(self.plugins), self.data)
        self.assertEqual([item['id'] for item in discovered], ['aaa-plain', 'zzz-tools'])
        self.assertNotIn('_tool_count', discovered[0])


MULTI_SOURCE = (
    'module.exports = {' + chr(10)
    + '  hooks: { "before-llm-messages": async () => null,' + chr(10)
    + '           "after-tool": async () => null },' + chr(10)
    + '};' + chr(10))


class ProfileSelectionTests(unittest.TestCase):
    """自动模式的上限按扩展数算，且同名 hook 不能被一个扩展独吞。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.data = self.base / 'data'
        self.data.mkdir(parents=True)
        self.plugins = self.base / 'plugins'
        write_extension(self.plugins / 'ext-a', ident='ext-a', source=MULTI_SOURCE)
        write_extension(self.plugins / 'ext-b', ident='ext-b', source=MULTI_SOURCE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_hook_from_two_extensions_is_kept(self):
        profiles = bridge.catalog_hook_profiles('', str(self.plugins), limit=8)
        pairs = {(item['extensionId'], item['hookName']) for item in profiles}
        self.assertIn(('ext-a', 'before-llm-messages'), pairs)
        self.assertIn(('ext-b', 'before-llm-messages'), pairs)
        self.assertIn(('ext-a', 'after-tool'), pairs)
        self.assertIn(('ext-b', 'after-tool'), pairs)
        self.assertEqual(len(profiles), 4)

    def test_max_profiles_counts_extensions_not_capabilities(self):
        manager = PluginManager(self.data)
        plugin = configure(manager, self.data, autoDiscover=True, maxProfiles=1,
                           extensionsDir=str(self.plugins))
        discovered = [
            {'extensionId': 'ext-a', 'provider': 'message.owner-check',
             'hookName': '', 'event': 'on_prepare', 'extensionPath': 'a'},
            {'extensionId': 'ext-a', 'provider': '', 'hookName': 'before-llm-messages',
             'event': 'on_pre_ai', 'extensionPath': 'a'},
            {'extensionId': 'ext-b', 'provider': '', 'hookName': 'after-tool',
             'event': 'on_after_tool', 'extensionPath': 'b'},
        ]
        with mock.patch.object(bridge, 'discover_profiles', lambda *a, **k: list(discovered)):
            profiles = plugin._profiles()
        self.assertTrue(profiles)
        self.assertEqual({item['extensionId'] for item in profiles}, {'ext-a'})
        self.assertEqual({item.get('provider') or item.get('hookName') for item in profiles},
                         {'message.owner-check', 'before-llm-messages'})


if __name__ == '__main__':
    unittest.main()
