"""WP1c：桥要默认找得到"真宿主根"，并把三个路径默认值派生出来。

这些用例只用临时目录 + monkeypatch，不依赖本机是否装过宿主；需要真 node 的那条
显式 skip。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.plugins import qqagent_bridge as bridge
from src.plugins.manager import PluginManager

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
RUNTIME_HOST = REPO / 'runtime' / 'qqagent-host'


def fake_host_root(parent: Path, with_plugins=True) -> Path:
    """造一个只有 marker 的假宿主根（不复制 node_modules）。"""
    root = Path(parent) / 'hostroot'
    (root / 'src').mkdir(parents=True, exist_ok=True)
    (root / 'src' / 'config.js').write_text('module.exports = {}\n', encoding='utf-8')
    (root / 'src' / 'plugin-loader.js').write_text('module.exports = {}\n', encoding='utf-8')
    (root / 'host-one.mjs').write_text('// stub\n', encoding='utf-8')
    if with_plugins:
        (root / 'plugins').mkdir(exist_ok=True)
    return root


def write_extension(folder: Path, ident='demo-ext', source=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'plugin.json').write_text(json.dumps({'id': ident, 'name': ident, 'version': '1.0.0'},
                                                   ensure_ascii=False), encoding='utf-8')
    (folder / 'index.js').write_text(source or 'module.exports = { setup() {} };\n', encoding='utf-8')
    # 运行时宿主的 package.json 是 type=module，扩展目录得自己声明 CommonJS。
    (folder / 'package.json').write_text('{"type": "commonjs"}\n', encoding='utf-8')
    return folder


class HostRootDerivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_derives_three_paths_when_user_filled_nothing(self):
        root = fake_host_root(self.base)
        with mock.patch('src.qqagent_host.resolve_host_root', lambda **kw: root):
            resolved = bridge._resolve_paths({}, self.base / 'data')
        self.assertEqual(resolved['host_root'], str(root))
        self.assertEqual(resolved['source_dir'], str(root))
        self.assertEqual(resolved['host_script'], str(root / 'host-one.mjs'))
        self.assertEqual(resolved['extensions_dir'], str(root / 'plugins'))

    def test_user_values_are_never_overwritten(self):
        root = fake_host_root(self.base)
        conf = {'sourceDir': 'D:/custom/src', 'hostScript': 'D:/custom/host.mjs',
                'extensionsDir': 'D:/custom/plugins'}
        with mock.patch('src.qqagent_host.resolve_host_root', lambda **kw: root):
            resolved = bridge._resolve_paths(conf, self.base / 'data')
        self.assertEqual(resolved['source_dir'], 'D:/custom/src')
        self.assertEqual(resolved['host_script'], 'D:/custom/host.mjs')
        self.assertEqual(resolved['extensions_dir'], 'D:/custom/plugins')

    def test_explicit_host_root_in_conf_wins_without_resolving(self):
        root = fake_host_root(self.base)

        def boom(**kw):
            raise AssertionError('显式宿主根已够用，不该再问 resolve_host_root')

        with mock.patch('src.qqagent_host.resolve_host_root', boom):
            resolved = bridge._resolve_paths({'hostRoot': str(root)}, self.base / 'data')
        self.assertEqual(resolved['source_dir'], str(root))
        self.assertEqual(resolved['extensions_dir'], str(root / 'plugins'))

    def test_no_host_root_keeps_empty_strings_and_never_raises(self):
        with mock.patch('src.qqagent_host.resolve_host_root', lambda **kw: None):
            resolved = bridge._resolve_paths({}, self.base / 'data')
        self.assertEqual(resolved, {'host_root': '', 'source_dir': '', 'host_script': '',
                                   'extensions_dir': ''})
        with mock.patch('src.qqagent_host.resolve_host_root',
                        side_effect=RuntimeError('qqagent_host 正在被改')):
            resolved = bridge._resolve_paths({}, self.base / 'data')
        self.assertEqual(resolved['host_root'], '')
        self.assertEqual(resolved['source_dir'], '')

    def test_host_root_ignores_dir_without_markers(self):
        bare = self.base / 'not-a-host'
        bare.mkdir()
        with mock.patch('src.qqagent_host.resolve_host_root', lambda **kw: None):
            self.assertEqual(bridge._host_root({'sourceDir': str(bare)}), '')


    def test_host_script_falls_back_to_repo_runtime_payload(self):
        root = fake_host_root(self.base)
        (root / 'host-one.mjs').unlink()
        with mock.patch('src.qqagent_host.resolve_host_root', lambda **kw: root):
            resolved = bridge._resolve_paths({}, self.base / 'data')
        self.assertEqual(resolved['source_dir'], str(root))
        if bridge.RUNTIME_HOST_SCRIPT.is_file():
            self.assertEqual(resolved['host_script'], str(bridge.RUNTIME_HOST_SCRIPT))
        else:
            self.assertEqual(resolved['host_script'], '')

    def test_real_repo_derivation_needs_no_user_input(self):
        """真机上不 monkeypatch：宿主根存在时三条默认值必须都是能用的路径。"""
        root = bridge._host_root({})
        if not root:
            self.skipTest('本机没有兼容宿主根')
        resolved = bridge._resolve_paths({})
        self.assertEqual(resolved['host_root'], root)
        self.assertTrue(Path(resolved['source_dir']).is_dir(), resolved)
        self.assertTrue(Path(resolved['host_script']).is_file(), resolved)
        self.assertTrue(Path(resolved['extensions_dir']).is_dir(), resolved)


class SchemaDirPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.data = self.base / 'data'
        self.ident = 'demo-ext'
        self.source_dir = self.base / 'hostroot'
        (self.source_dir / 'plugins').mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_priority_order(self):
        conf = {'extension_id': self.ident, 'source_dir': str(self.source_dir)}
        self.assertEqual(bridge._schema_dir(conf, self.data), '')

        legacy = write_extension(self.data / 'qqagent_extensions' / self.ident)
        self.assertEqual(bridge._schema_dir(conf, self.data), str(legacy))

        console = write_extension(self.data / 'qq-only' / 'qqagent_host' / 'plugins' / self.ident)
        self.assertEqual(bridge._schema_dir(conf, self.data), str(console),
                         '控制台准备的宿主落点必须优先于历史 qqagent_extensions')

        source = write_extension(self.source_dir / 'plugins' / self.ident)
        self.assertEqual(bridge._schema_dir(conf, self.data), str(source))

        own = write_extension(self.base / 'own-ext')
        self.assertEqual(bridge._schema_dir({'extension': str(own), 'extension_id': 'other'},
                                            self.data), str(own))

    def test_console_install_path_yields_real_tool_schema(self):
        module = bridge._schema_module()
        if module is None:
            self.skipTest('src/tool_schema.py 不在')
        folder = self.data / 'qq-only' / 'qqagent_host' / 'plugins' / self.ident
        write_extension(folder, source=(
            'module.exports = {\n'
            '  setup(api) {\n'
            '    api.registerTool({\n'
            '      id: "echo",\n'
            '      name: "echo",\n'
            '      description: "回显",\n'
            '      parameters: { type: "object",\n'
            '        properties: { text: { type: "string", description: "要回显的内容" } },\n'
            '        required: ["text"] },\n'
            '      execute: async (args) => ({ echoed: args.text }),\n'
            '    });\n'
            '  },\n'
            '};\n'))
        directory = bridge._schema_dir({'extension_id': self.ident}, self.data)
        self.assertTrue(directory.endswith(str(Path('qq-only') / 'qqagent_host' / 'plugins' / self.ident)),
                        directory)
        table = module.extract_tools(directory)
        self.assertIn('echo', table)
        parameters = bridge._clean_schema(table['echo'].get('parameters'))
        self.assertEqual(sorted(parameters['properties']), ['text'])
        self.assertEqual(parameters['required'], ['text'])


class DirectoryDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.plugins = self.base / 'hostroot' / 'plugins'
        self.plugins.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_reads_manifests_and_capabilities(self):
        write_extension(self.plugins / 'hooky', ident='hooky', source=(
            'module.exports = { hooks: { "before-context": async (p) => ({ ok: true }) } };\n'))
        write_extension(self.plugins / 'providery', ident='providery', source=(
            'module.exports = { providers: { "message.owner-check": async () => null } };\n'))
        (self.plugins / 'no-manifest').mkdir()

        items = {item['id']: item for item in bridge.scan_extension_dirs(self.plugins)}
        self.assertEqual(sorted(items), ['hooky', 'providery'])
        self.assertEqual(items['hooky']['hooks'], ['before-context'])
        self.assertEqual(items['providery']['providers'], ['message.owner-check'])
        self.assertTrue(Path(items['hooky']['file']).is_dir())

    def test_discover_profiles_without_catalog(self):
        write_extension(self.plugins / 'hooky', ident='hooky', source=(
            'module.exports = { hooks: { "before-context": async () => null } };\n'))
        profiles = bridge.discover_profiles('', str(self.plugins), 2)
        self.assertTrue(profiles, '没有 catalog.json 时也必须能发现已安装扩展')
        hooks = [item for item in profiles if item.get('hookName')]
        self.assertTrue(hooks)
        self.assertEqual(hooks[0]['hookName'], 'before-context')

    def test_profiles_uses_derived_extensions_dir(self):
        write_extension(self.plugins / 'hooky', ident='hooky', source=(
            'module.exports = { hooks: { "before-context": async () => null } };\n'))
        root = self.plugins.parent
        manager = PluginManager(self.base / 'data')
        plugin = manager._plugins['qqagent_bridge']
        manager._settings['qqagent_bridge'] = {
            'enabled': True, 'autoDiscover': True, 'maxProfiles': 2,
            'hostScript': '', 'sourceDir': '', 'extensionsDir': '', 'profiles': ''}
        with mock.patch('src.qqagent_host.resolve_host_root', lambda **kw: root):
            profiles = plugin._profiles()
        self.assertTrue(any(item.get('hookName') == 'before-context' for item in profiles), profiles)
        self.assertEqual(str(plugin._settings_for(profiles[0])['extensions_dir']), str(self.plugins))


class ImportIsolationTests(unittest.TestCase):
    def test_import_does_not_pull_qqagent_host(self):
        code = ('import sys, src.plugins.qqagent_bridge as b; '
                'print(sorted(name for name in sys.modules '
                "if name.endswith('qqagent_host')))")
        out = subprocess.run([sys.executable, '-c', code], cwd=str(REPO),
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-800:])
        self.assertEqual(out.stdout.strip().splitlines()[-1], '[]')


@unittest.skipUnless(NODE and (RUNTIME_HOST / 'host-one.mjs').is_file(),
                     '需要 node + runtime/qqagent-host')
class RealNodeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.plugins = self.base / 'hostroot' / 'plugins'
        self.plugins.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_directory_extension_lists_and_calls_tool(self):
        ident = 'wp1c-demo'
        write_extension(self.plugins / ident, ident=ident, source=(
            'module.exports = {\n'
            '  setup(api) {\n'
            '    api.registerTool({ id: "echo", name: "echo", description: "回显",\n'
            '      parameters: { type: "object", properties: { text: { type: "string" } } },\n'
            '      execute: async (args) => ({ echoed: args.text }) });\n'
            '  },\n'
            '};\n'))
        listing = asyncio.run(bridge.list_tools(
            str(RUNTIME_HOST / 'host-one.mjs'), str(RUNTIME_HOST),
            str(self.plugins / ident), extension_id=ident, node=NODE, timeout=60,
            data_dir=self.base / 'data'))
        names = [tool['id'] for tool in listing.get('tools') or []]
        self.assertIn(ident + '__echo', names, listing)
        result = asyncio.run(bridge.call_tool(
            ident + '__echo', {'text': 'hi'},
            host_script=str(RUNTIME_HOST / 'host-one.mjs'), source_dir=str(RUNTIME_HOST),
            extension=str(self.plugins / ident), extension_id=ident, node=NODE,
            timeout=60, data_dir=self.base / 'data'))
        self.assertTrue(result.get('ok'), result)
        self.assertEqual((result.get('value') or {}).get('echoed'), 'hi')


if __name__ == '__main__':
    unittest.main()
