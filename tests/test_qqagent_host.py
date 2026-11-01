"""QQ Agent 宿主根 / 扩展安装 / 加载探测的离线测试。

重点不是"能不能装"，而是三条边界：
- host_root 的判定标准只有一个（src/config.js + src/plugin-loader.js 都在）；
- 装扩展前必须挡住路径穿越与软链，已装的不许静默覆盖；
- 探测在 node 缺失时给结构化错误，绝不抛异常。
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import qqagent_host as qh


def _make_host(root: Path, extras=(), scripts=()) -> Path:
    """造一个最小可用 host_root（判定只认两个 marker）。"""
    (root / 'src').mkdir(parents=True, exist_ok=True)
    (root / 'src' / 'config.js').write_text('export const DATA_DIR = 1;\n', encoding='utf-8')
    (root / 'src' / 'plugin-loader.js').write_text('export const loadPlugins = 1;\n', encoding='utf-8')
    for name in extras:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{}\n', encoding='utf-8')
    for name in scripts:
        (root / name).write_text('// host\n', encoding='utf-8')
    return root


def _zip(path: Path, entries) -> Path:
    with zipfile.ZipFile(str(path), 'w') as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return path


def _plugin_entries(ident: str, index_js: str = 'export const hooks = {};\n'):
    return [
        (ident + '/plugin.json', json.dumps(
            {'id': ident, 'name': '演示扩展', 'version': '1.0.0',
             'capabilities': ['account.read']}, ensure_ascii=False)),
        (ident + '/index.js', index_js),
    ]


class ResolveHostRootTest(unittest.TestCase):
    def test_显式参数优先于全部候选(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            explicit = _make_host(base / 'explicit')
            fallback = _make_host(base / 'fallback')
            data_dir = base / 'data'
            _make_host(data_dir / 'qq-only' / 'qqagent_host')
            with mock.patch.object(qh, 'DEV_HOST_CANDIDATES', (fallback,)):
                self.assertEqual(qh.resolve_host_root(explicit=str(explicit), data_dir=data_dir),
                                 explicit.resolve())

    def test_显式参数不可用时继续往下走(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            broken = base / 'broken'
            broken.mkdir()
            fallback = _make_host(base / 'fallback')
            with mock.patch.object(qh, 'DEV_HOST_CANDIDATES', (fallback,)):
                self.assertEqual(qh.resolve_host_root(explicit=str(broken), data_dir=base / 'data'),
                                 fallback.resolve())

    def test_没有可用候选时返回None(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(qh, 'DEV_HOST_CANDIDATES', (Path(temp) / 'nope',)):
                self.assertIsNone(qh.resolve_host_root(explicit=None, data_dir=Path(temp) / 'data'))

    def test_控制台自持目录在开发态之前(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            owned = _make_host(base / 'data' / 'qq-only' / 'qqagent_host')
            dev = _make_host(base / 'dev')
            with mock.patch.object(qh, 'DEV_HOST_CANDIDATES', (dev,)):
                self.assertEqual(qh.resolve_host_root(data_dir=base / 'data'), owned.resolve())

    def test_判定只看两个marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'half'
            root.mkdir()
            (root / 'src').mkdir()
            (root / 'src' / 'config.js').write_text('', encoding='utf-8')
            self.assertFalse(qh.is_host_root(root))
            self.assertIn('src/plugin-loader.js', qh.host_root_problems(root))


class EnsureHostRootTest(unittest.TestCase):
    def _source(self, base: Path) -> Path:
        source = _make_host(base / 'source')
        (source / 'package.json').write_text('{"name": "qq-agent"}\n', encoding='utf-8')
        (source / 'node_modules' / 'ws').mkdir(parents=True)
        (source / 'node_modules' / 'ws' / 'package.json').write_text('{}\n', encoding='utf-8')
        return source

    def test_复制最小集合并做冒烟(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = self._source(base)
            target = base / 'data' / 'qq-only' / 'qqagent_host'
            fake = {'ok': True, 'node': 'node', 'duration_ms': 3, 'exports': 2}
            with mock.patch.object(qh, 'compat_dir', return_value=None), \
                    mock.patch.object(qh, '_node_smoke', return_value=fake):
                outcome = qh.ensure_host_root(target=target, source=source)
            self.assertTrue(outcome['ok'])
            self.assertGreater(outcome['copied'], 0)
            self.assertTrue((target / 'src' / 'config.js').is_file())
            self.assertTrue((target / 'node_modules' / 'ws' / 'package.json').is_file())
            self.assertTrue((target / 'plugins').is_dir())
            self.assertEqual(outcome['smoke'], fake)

    def test_没有显式源时回退到随包载荷(self):
        # 用户机上没有 .tmp/qq-agent-run：resolve_host_root 给不出源时必须走 runtime_source_candidates，
        # 否则手工调 ensure_host_root(target) 会得到 source-missing。
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            payload = self._source(base)
            target = base / 'host'
            fake = {'ok': True, 'node': 'node', 'duration_ms': 2, 'exports': 3}
            with mock.patch.object(qh, 'resolve_host_root', return_value=None), \
                    mock.patch.object(qh, 'runtime_source_candidates', return_value=[payload]), \
                    mock.patch.object(qh, 'compat_dir', return_value=None), \
                    mock.patch.object(qh, '_node_smoke', return_value=fake):
                outcome = qh.ensure_host_root(target=target)
            self.assertTrue(outcome['ok'], outcome.get('error'))
            self.assertEqual(Path(outcome['source']), payload)
            self.assertGreater(outcome['copied'], 0)
            self.assertTrue((target / 'src' / 'config.js').is_file())

    def test_连载荷都缺时仍报source_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'host'
            with mock.patch.object(qh, 'resolve_host_root', return_value=None), \
                    mock.patch.object(qh, 'runtime_source_candidates', return_value=[]):
                outcome = qh.ensure_host_root(target=target)
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['error']['code'], 'source-missing')

    def test_幂等_第二次全部跳过(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = self._source(base)
            target = base / 'host'
            fake = {'ok': True, 'node': 'node', 'duration_ms': 1}
            with mock.patch.object(qh, 'compat_dir', return_value=None), \
                    mock.patch.object(qh, '_node_smoke', return_value=fake):
                first = qh.ensure_host_root(target=target, source=source)
                second = qh.ensure_host_root(target=target, source=source)
            self.assertTrue(first['ok'] and second['ok'])
            self.assertEqual(second['copied'], 0)
            self.assertEqual(second['skipped'], first['copied'])

    def test_源缺料时不落半个宿主根(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'source'
            (source / 'src').mkdir(parents=True)
            (source / 'src' / 'config.js').write_text('', encoding='utf-8')
            target = base / 'host'
            with mock.patch.object(qh, 'compat_dir', return_value=None):
                outcome = qh.ensure_host_root(target=target, source=source)
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['error']['code'], 'source-incomplete')
            self.assertIn('src/plugin-loader.js', outcome['error']['missing'])
            self.assertFalse((target / 'src').exists())

    def test_源完全不存在(self):
        with tempfile.TemporaryDirectory() as temp:
            outcome = qh.ensure_host_root(target=Path(temp) / 'host',
                                          source=Path(temp) / 'ghost')
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['error']['code'], 'source-incomplete')

    def test_冒烟失败会带原因(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = self._source(base)
            failed = {'ok': False, 'error': {'code': 'smoke-failed', 'message': 'boom'}}
            with mock.patch.object(qh, 'compat_dir', return_value=None), \
                    mock.patch.object(qh, '_node_smoke', return_value=failed):
                outcome = qh.ensure_host_root(target=base / 'host', source=source)
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['error']['code'], 'smoke-failed')


class InstallExtensionTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.host = _make_host(self.base / 'host')

    def tearDown(self):
        self._temp.cleanup()

    def test_单层目录被剥掉并装到plugins下(self):
        archive = _zip(self.base / 'demo.zip', _plugin_entries('demo'))
        outcome = qh.install_extension(archive, self.host)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(outcome['id'], 'demo')
        folder = self.host / 'plugins' / 'demo'
        self.assertTrue((folder / 'plugin.json').is_file())
        self.assertTrue((folder / 'index.js').is_file())
        self.assertFalse((folder / 'demo').exists())

    def test_路径穿越被拒(self):
        archive = _zip(self.base / 'evil.zip', [('../evil.js', 'x'), ('demo/index.js', 'x')])
        outcome = qh.install_extension(archive, self.host)
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['error']['code'], 'zip-unsafe-path')
        self.assertFalse((self.base / 'evil.js').exists())
        self.assertFalse((self.host / 'plugins' / 'demo').exists())

    def test_绝对路径被拒(self):
        archive = _zip(self.base / 'abs.zip', [('/etc/passwd', 'x')])
        outcome = qh.install_extension(archive, self.host)
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['error']['code'], 'zip-unsafe-path')

    def test_软链被拒(self):
        archive = self.base / 'link.zip'
        with zipfile.ZipFile(str(archive), 'w') as handle:
            info = zipfile.ZipInfo('demo/index.js')
            info.external_attr = (0o120777 << 16)
            handle.writestr(info, '/etc/passwd')
        outcome = qh.install_extension(archive, self.host)
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['error']['code'], 'zip-symlink')

    def test_已存在不覆盖_force才替换(self):
        archive = _zip(self.base / 'demo.zip', _plugin_entries('demo'))
        self.assertTrue(qh.install_extension(archive, self.host)['ok'])
        marker = self.host / 'plugins' / 'demo' / 'index.js'
        marker.write_text('// 用户改过\n', encoding='utf-8')
        again = qh.install_extension(archive, self.host)
        self.assertFalse(again['ok'])
        self.assertEqual(again['error']['code'], 'exists')
        self.assertEqual(marker.read_text(encoding='utf-8'), '// 用户改过\n')
        forced = qh.install_extension(archive, self.host, force=True)
        self.assertTrue(forced['ok'])
        self.assertNotEqual(marker.read_text(encoding='utf-8'), '// 用户改过\n')

    def test_解压总量上限(self):
        archive = self.base / 'big.zip'
        with zipfile.ZipFile(str(archive), 'w') as handle:
            handle.writestr('demo/index.js', 'x')
        with mock.patch.object(qh, 'MAX_ARCHIVE_BYTES', 0):
            outcome = qh.install_extension(archive, self.host)
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['error']['code'], 'zip-too-large')

    def test_拒绝装进只读基线(self):
        real = qh.PROTECTED_ROOTS[0]
        with mock.patch.object(qh, 'is_protected', return_value=True):
            archive = _zip(self.base / 'demo.zip', _plugin_entries('demo'))
            outcome = qh.install_extension(archive, self.host)
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['error']['code'], 'host-root-protected')
        self.assertTrue(str(real))

    def test_列表与卸载(self):
        archive = _zip(self.base / 'demo.zip', _plugin_entries(
            'demo', index_js="import { DATA_DIR } from '../../src/config.js';\n"
                              "export const hooks = { onMessage: 1 };\n"
                              "export const providers = { 'chat.admit': 1 };\n"))
        self.assertTrue(qh.install_extension(archive, self.host)['ok'])
        installed = qh.list_installed(self.host)
        self.assertEqual([item['id'] for item in installed], ['demo'])
        entry = installed[0]
        self.assertEqual(entry['version'], '1.0.0')
        self.assertEqual(entry['hostImports'], ['../../src/config.js'])
        self.assertIn('onMessage', entry['hooks'])
        self.assertIn('chat.admit', entry['providers'])
        self.assertTrue(qh.uninstall_extension('demo', self.host)['ok'])
        self.assertEqual(qh.list_installed(self.host), [])
        missing = qh.uninstall_extension('demo', self.host)
        self.assertEqual(missing['error']['code'], 'not-installed')

    def test_读清单不落盘(self):
        archive = _zip(self.base / 'demo.zip', _plugin_entries('demo'))
        peek = qh.read_archive_manifest(archive)
        self.assertEqual(peek['id'], 'demo')
        self.assertEqual(peek['files'], 2)
        self.assertFalse((self.host / 'plugins' / 'demo').exists())


class ImportProbeTest(unittest.TestCase):
    def test_node缺失给结构化错误(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            host = _make_host(base / 'host')
            folder = host / 'plugins' / 'demo'
            folder.mkdir(parents=True)
            with mock.patch.object(qh, 'BUNDLED_NODE_DIR', base / 'no-node'), \
                    mock.patch.object(qh.shutil, 'which', return_value=None), \
                    mock.patch.object(qh, '_common_node_paths', return_value=()):
                outcome = qh.import_probe(folder, host)
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['error']['code'], 'node-missing')
            self.assertEqual(outcome['tools'], [])

    def test_宿主脚本缺失给结构化错误(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            host = _make_host(base / 'host')
            folder = host / 'plugins' / 'demo'
            folder.mkdir(parents=True)
            with mock.patch.object(qh, 'find_host_script', return_value=None):
                outcome = qh.import_probe(folder, host, node='node')
            self.assertEqual(outcome['error']['code'], 'host-script-missing')

    def test_宿主根不可用(self):
        with tempfile.TemporaryDirectory() as temp:
            outcome = qh.import_probe(Path(temp), Path(temp) / 'ghost', node='node')
            self.assertEqual(outcome['error']['code'], 'host-root-invalid')

    def test_加载失败原样带回错误(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            host = _make_host(base / 'host')
            folder = host / 'plugins' / 'demo'
            folder.mkdir(parents=True)
            run = {'ok': False, 'error': 'ERR_MODULE_NOT_FOUND: nope', 'raw': {},
                   'duration_ms': 12, 'timed_out': False}
            with mock.patch.object(qh, 'find_host_script', return_value=base / 'host-one.mjs'), \
                    mock.patch.object(qh.extension_host, 'run_capability_sync', return_value=run):
                outcome = qh.import_probe(folder, host, node='node')
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['error']['code'], 'load-failed')
            self.assertIn('ERR_MODULE_NOT_FOUND', outcome['error']['message'])

    def test_超时有独立错误码(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            host = _make_host(base / 'host')
            folder = host / 'plugins' / 'demo'
            folder.mkdir(parents=True)
            run = {'ok': False, 'error': 'timeout after 20s', 'raw': {}, 'duration_ms': 20000,
                   'timed_out': True}
            with mock.patch.object(qh, 'find_host_script', return_value=base / 'host-one.mjs'), \
                    mock.patch.object(qh.extension_host, 'run_capability_sync', return_value=run):
                outcome = qh.import_probe(folder, host, node='node')
            self.assertEqual(outcome['error']['code'], 'timeout')

    def test_成功时只留本扩展自己的工具(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            host = _make_host(base / 'host')
            folder = host / 'plugins' / 'demo'
            folder.mkdir(parents=True)
            (folder / 'index.js').write_text('export const tools = 1; // inputSchema\n',
                                             encoding='utf-8')
            raw = {'ok': True, 'extension': {'id': 'demo', 'tools': ['demo__ping'],
                                             'loadError': '', 'hooks': [], 'providers': []},
                   'tools': [{'id': 'send_message', 'name': '发送', 'description': '宿主自带'},
                             {'id': 'demo__ping', 'name': '探活', 'description': '扩展工具'}]}
            run = {'ok': True, 'raw': raw, 'duration_ms': 8, 'timed_out': False, 'error': ''}
            with mock.patch.object(qh, 'find_host_script', return_value=base / 'host-one.mjs'), \
                    mock.patch.object(qh.extension_host, 'run_capability_sync', return_value=run):
                outcome = qh.import_probe(folder, host, node='node')
            self.assertTrue(outcome['ok'])
            self.assertEqual([tool['name'] for tool in outcome['tools']], ['探活'])
            self.assertTrue(outcome['tools'][0]['hasSchema'])


class HostConfigTests(unittest.TestCase):
    """宿主自己的 config.json：扩展开关与扩展配置的唯一落点。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = _make_host(Path(self._tmp.name) / 'host')

    def _config(self):
        path = self.root / 'data-host-one' / 'config.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def test_set_enabled_writes_host_switch(self):
        first = qh.set_extension_enabled(self.root, 'demo', True)
        self.assertTrue(first['ok'], first.get('error'))
        self.assertTrue(first['written']['enabled'])
        self.assertIs(self._config()['skills']['demo']['enabled'], True)
        second = qh.set_extension_enabled(self.root, 'demo', False)
        self.assertTrue(second['ok'], second.get('error'))
        self.assertIs(self._config()['skills']['demo']['enabled'], False)
        self.assertEqual(list((self.root / 'data-host-one').glob('*.tmp')), [],
                         '原子写不许留下临时文件')

    def test_enable_keeps_other_skills_and_keys(self):
        qh._write_host_skill(self.root, 'other', {'enabled': True, 'ids': '10001'})
        qh.set_extension_enabled(self.root, 'demo', True)
        skills = self._config()['skills']
        self.assertEqual(skills['other']['ids'], '10001')
        self.assertIs(skills['other']['enabled'], True)

    def test_only_missing_keeps_user_values(self):
        qh._write_host_skill(self.root, 'demo', {'ids': '10001'})
        out = qh._write_host_skill(self.root, 'demo', {'ids': '99999'}, only_missing=True)
        self.assertTrue(out['ok'], out.get('error'))
        self.assertEqual(out['written'], {})
        self.assertEqual(self._config()['skills']['demo']['ids'], '10001')

    def test_seed_owner_settings_matches_manifest_keys(self):
        manifest = {'settings': {'ids': ''},
                    'configSchema': {'ownerIds': {'type': 'string'}, 'scope': {}}}
        self.assertEqual(qh.owner_setting_keys(manifest), ['ids', 'ownerIds'])
        out = qh.seed_owner_settings(self.root, 'demo', ['10001', ' 10002 ', '10001'], manifest)
        self.assertTrue(out['ok'], out.get('error'))
        skills = self._config()['skills']['demo']
        self.assertEqual(skills['ids'], '10001,10002')
        self.assertEqual(skills['ownerIds'], '10001,10002')
        self.assertNotIn('scope', skills, '清单没声明的键不许顺手写进去')

    def test_seed_skips_without_owner_or_keys(self):
        self.assertTrue(qh.seed_owner_settings(self.root, 'demo', [], {'settings': {'ids': ''}})['ok'])
        self.assertTrue(qh.seed_owner_settings(self.root, 'demo', ['10001'], {'settings': {}})['ok'])
        self.assertFalse((self.root / 'data-host-one' / 'config.json').exists(),
                         '没东西可写就不该建文件')

    def test_corrupt_config_is_replaced(self):
        path = self.root / 'data-host-one' / 'config.json'
        path.parent.mkdir(parents=True)
        path.write_text('{not json', encoding='utf-8')
        out = qh.set_extension_enabled(self.root, 'demo', True)
        self.assertTrue(out['ok'], out.get('error'))
        self.assertIs(self._config()['skills']['demo']['enabled'], True)

    def test_bad_id_or_missing_root_is_reported(self):
        self.assertFalse(qh.set_extension_enabled(None, 'demo', True)['ok'])
        bad = qh.set_extension_enabled(self.root, '../逃逸', True)
        self.assertFalse(bad['ok'])
        self.assertIn('非法', bad['error'])


if __name__ == '__main__':
    unittest.main()
