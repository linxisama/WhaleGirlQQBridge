# -*- coding: utf-8 -*-
"""src/skill_market.py 的行为测试（WP12a）。

全部用例都在临时目录里造包、造已安装目录，不碰真实 data/ 与真实市场目录；
只在"清单快照分层"与"真实运行时"相关的用例里读取 .tmp/inv/skill-tools.json（只读）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import extension_registry, skill_market


def _write_zip(path, entries):
    """entries: [(name, data|None, external_attr)]；data 为 None 表示目录条目。"""
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data, attr in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = attr
            if data is None:
                info.external_attr = (attr or 0o40755 << 16) | 0x10
                archive.writestr(info, b'')
            else:
                archive.writestr(info, data)


def _skill_manifest(ident, name=None, version='1.0.0', description='demo'):
    return json.dumps({'id': ident, 'name': name or ident, 'version': version,
                       'description': description, 'prompt': 'hello'},
                      ensure_ascii=False).encode('utf-8')


def _good_zip(path, ident='demo-skill', extra=None):
    entries = [(ident + '/skill.json', _skill_manifest(ident), 0o644 << 16),
               (ident + '/index.js', b'export default 1;\n', 0o644 << 16)]
    entries.extend(extra or [])
    _write_zip(path, entries)
    return path


_T1_BODY = b'ctx.registerTool({ name: "demo" });\n'


def _tier_one_zip(path, ident='demo-skill', manifest='skill.json', body=_T1_BODY):
    """能过 skill_manifest.classify 的 T1 包（有 registerTool、无 .. 依赖）。"""
    _write_zip(path, [(ident + '/' + manifest, _skill_manifest(ident), 0o644 << 16),
                      (ident + '/index.js', body, 0o644 << 16)])
    return path


def _snapshot_skill(ident, tier, tools, **extra):
    """清单里一条 skill 记录（字段对齐 skill_manifest._skill_entry 的输出）。"""
    entry = {'id': ident, 'name': ident, 'version': '1.0.0', 'tier': tier,
             'tierReason': 'clean', 'tools': [{'name': name} for name in tools],
             'permissions': [], 'requires': [], 'hostImports': [], 'external': [],
             'usage': [], 'entry': 'index.js',
             'source': {'kind': 'market', 'path': ident + '.zip'}}
    entry.update(extra)
    return entry


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = types.SimpleNamespace(data_root=str(self.root))
        self.extensions = self.root / 'data' / 'qq-only' / 'skill_extensions'
        self.manifest = self.root / 'data' / 'qq-only' / 'skill-tools.json'
        self.market = self.root / 'market'
        self.market.mkdir()
        # extension_registry 的扫描缓存是模块级的，跨用例会串味。
        extension_registry.invalidate()

    def tearDown(self):
        self._tmp.cleanup()

    def _listing(self, path):
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def _seed_manifest(self, skills, **meta):
        """在 runtime 数据目录里放一份清单快照（模拟上一次构建的结论）。"""
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = {'meta': dict({'version': 1, 'generator': 'src/skill_manifest.py',
                                 'writtenAt': 1700000000, 'marketDir': 'D:/old-market',
                                 'overrides': []}, **meta),
                   'skills': skills,
                   'stats': {'skillsTotal': len(skills), 'toolsTotal': 0,
                             'zipsInMarketDir': 7,
                             'tierCounts': {}, 'tierToolCounts': {},
                             'skillsWithExternalHosts': 0, 'distinctExternalHosts': 0}}
        self.manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return payload

    def _read_manifest(self):
        return json.loads(self.manifest.read_text(encoding='utf-8'))


class MarketDirTests(_Case):
    def test_env_wins_over_config_and_default(self):
        env_dir = self.root / 'from-env'
        env_dir.mkdir()
        config = {'qq_only': {'skill_market_dir': str(self.root / 'from-config')}}
        with mock.patch.dict(os.environ, {skill_market.MARKET_ENV: str(env_dir)}):
            self.assertEqual(skill_market.market_dir(config=config), env_dir)

    def test_config_key_used_when_env_missing(self):
        config_dir = self.root / 'from-config'
        config_dir.mkdir()
        env = {key: value for key, value in os.environ.items()
               if key != skill_market.MARKET_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(skill_market.market_dir(
                config={'qq_only': {'skill_market_dir': str(config_dir)}}), config_dir)

    def test_config_object_with_get(self):
        class Config:
            def __init__(self, data):
                self.data = data

            def get(self, key, default=None):
                node = self.data
                for part in key.split('.'):
                    if not isinstance(node, dict) or part not in node:
                        return default
                    node = node[part]
                return node

        target = self.root / 'obj-config'
        target.mkdir()
        env = {key: value for key, value in os.environ.items()
               if key != skill_market.MARKET_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(skill_market.market_dir(
                config=Config({'qq_only': {'skill_market_dir': str(target)}})), target)

    def test_none_when_no_candidate_exists(self):
        env = {key: value for key, value in os.environ.items()
               if key != skill_market.MARKET_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(skill_market, 'DEFAULT_MARKET_DIRS', ()), \
                mock.patch.object(skill_market, '_repo_root',
                                  lambda: self.root / 'no-such-repo'):
            self.assertIsNone(skill_market.market_dir())

    def test_never_creates_market_dir(self):
        missing = self.root / 'not-created'
        with mock.patch.object(skill_market, 'DEFAULT_MARKET_DIRS', (str(missing),)):
            skill_market.market_dir()
        self.assertFalse(missing.exists())


class ListMarketTests(_Case):
    def test_reads_valid_zips(self):
        _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        _good_zip(self.market / 'other-skill.zip', 'other-skill')
        result = skill_market.list_market(market=self.market, runtime=self.runtime)
        self.assertTrue(result['ok'])
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['broken'], 0)
        first = result['items'][0]
        for key in ('id', 'name', 'version', 'description', 'file', 'bytes',
                    'installed', 'tier', 'tools'):
            self.assertIn(key, first)
        self.assertEqual(first['id'], 'demo-skill')
        self.assertFalse(first['installed'])
        self.assertGreater(first['bytes'], 0)

    def test_plugin_json_only_package_is_listed(self):
        _write_zip(self.market / 'legacy.zip',
                   [('legacy/plugin.json', _skill_manifest('legacy'), 0o644 << 16),
                    ('legacy/index.js', b'1', 0o644 << 16)])
        result = skill_market.list_market(market=self.market, runtime=self.runtime)
        self.assertTrue(result['ok'])
        self.assertEqual([item['id'] for item in result['items']], ['legacy'])

    def test_broken_zip_counted_not_fatal(self):
        _good_zip(self.market / 'ok.zip', 'ok-skill')
        (self.market / 'broken.zip').write_bytes(b'not a zip at all')
        _write_zip(self.market / 'nomanifest.zip',
                   [('nomanifest/index.js', b'1', 0o644 << 16)])
        result = skill_market.list_market(market=self.market, runtime=self.runtime)
        self.assertTrue(result['ok'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['broken'], 2)
        self.assertEqual(sorted(result['brokenItems']), ['broken.zip', 'nomanifest.zip'])

    def test_missing_market_dir_reports_error(self):
        result = skill_market.list_market(market=self.root / 'nope', runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['error']['code'], 'market-missing')

    def test_installed_flag_follows_disk(self):
        _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        skill_market.install(self.market / 'demo-skill.zip', runtime=self.runtime)
        result = skill_market.list_market(market=self.market, runtime=self.runtime)
        self.assertTrue(result['items'][0]['installed'])


class ListInstalledTests(_Case):
    def test_empty_root(self):
        result = skill_market.list_installed(runtime=self.runtime)
        self.assertTrue(result['ok'])
        self.assertEqual(result['count'], 0)

    def test_fields_after_install(self):
        _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        skill_market.install(self.market / 'demo-skill.zip', runtime=self.runtime)
        result = skill_market.list_installed(runtime=self.runtime)
        self.assertEqual(result['count'], 1)
        item = result['items'][0]
        self.assertEqual(item['id'], 'demo-skill')
        self.assertEqual(item['version'], '1.0.0')
        self.assertEqual(item['tier'], 'T6-disabled')  # 无 registerTool，如实说
        self.assertFalse(item['enabled'])
        self.assertTrue(item['blocked'])
        self.assertEqual(item['entry'], 'index.js')
        for key in ('path', 'name', 'tier', 'tools', 'known', 'entry', 'blocked'):
            self.assertIn(key, item)

    def test_enabled_reflects_registry(self):
        _tier_one_zip(self.market / 'demo-skill.zip', 'demo-skill')
        installed = skill_market.install(self.market / 'demo-skill.zip',
                                         runtime=self.runtime, enable=True)
        self.assertEqual(installed['tier'], 'T1-ready')
        self.assertTrue(installed['enabled'], installed)
        item = skill_market.list_installed(runtime=self.runtime)['items'][0]
        self.assertTrue(item['enabled'])
        self.assertTrue(item['known'])
        self.assertEqual(item['blocked'], '')


class InstallTests(_Case):
    def test_install_ok(self):
        zip_path = _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['id'], 'demo-skill')
        self.assertEqual(result['files'], 2)
        self.assertGreater(result['bytes'], 0)
        self.assertFalse(result['replaced'])
        self.assertEqual(Path(result['path']), self.extensions / 'demo-skill')
        self.assertTrue((self.extensions / 'demo-skill' / 'skill.json').is_file())
        self.assertTrue((self.extensions / 'demo-skill' / 'index.js').is_file())

    def test_install_exists_without_force(self):
        zip_path = _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        self.assertTrue(skill_market.install(zip_path, runtime=self.runtime)['ok'])
        again = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(again['ok'])
        self.assertEqual(again['error']['code'], 'exists')

    def test_install_force_replaces(self):
        zip_path = _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        skill_market.install(zip_path, runtime=self.runtime)
        forced = skill_market.install(zip_path, runtime=self.runtime, force=True)
        self.assertTrue(forced['ok'], forced['error'])
        self.assertTrue(forced['replaced'])
        self.assertEqual(self._listing(self.extensions), ['demo-skill'])

    def test_traversal_entry_rejected_without_writes(self):
        before = self._listing(self.extensions)
        zip_path = self.market / 'evil.zip'
        _write_zip(zip_path, [('demo-skill/skill.json', _skill_manifest('demo-skill'),
                               0o644 << 16),
                              ('../../evil.txt', b'pwn', 0o644 << 16)])
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'unsafe-path')
        self.assertEqual(self._listing(self.extensions), before)
        self.assertFalse((self.root / 'data' / 'qq-only' / 'evil.txt').exists())

    def test_absolute_entry_rejected_without_writes(self):
        before = self._listing(self.extensions)
        zip_path = self.market / 'abs.zip'
        _write_zip(zip_path, [('demo-skill/skill.json', _skill_manifest('demo-skill'),
                               0o644 << 16),
                              ('/abs-evil.txt', b'pwn', 0o644 << 16)])
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'unsafe-path')
        self.assertEqual(self._listing(self.extensions), before)

    def test_symlink_entry_rejected_without_writes(self):
        before = self._listing(self.extensions)
        zip_path = self.market / 'link.zip'
        _write_zip(zip_path, [('demo-skill/skill.json', _skill_manifest('demo-skill'),
                               0o644 << 16),
                              ('demo-skill/link', b'/etc/passwd', 0o120777 << 16)])
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'symlink')
        self.assertEqual(self._listing(self.extensions), before)

    def test_missing_manifest_leaves_no_target(self):
        zip_path = self.market / 'nomanifest.zip'
        _write_zip(zip_path, [('demo-skill/index.js', b'1', 0o644 << 16)])
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'no-manifest')
        self.assertFalse((self.extensions / 'demo-skill').exists())
        self.assertEqual(self._listing(self.extensions), [])

    def test_plugin_json_only_installs(self):
        """plugin.json 也算清单；结构坏就如实报 T6，装得上但不启用。"""
        zip_path = self.market / 'legacy.zip'
        _write_zip(zip_path, [('legacy/plugin.json', _skill_manifest('legacy'),
                               0o644 << 16),
                              ('legacy/index.js', b'export default 1;\n', 0o644 << 16)])
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['id'], 'legacy')
        self.assertEqual(result['tier'], 'T6-disabled')
        self.assertFalse(result['enabled'])
        self.assertIn('未启用', result['note'])
        self.assertTrue((self.extensions / 'legacy' / 'plugin.json').is_file())

    def test_plugin_json_only_can_be_tier_one(self):
        zip_path = _tier_one_zip(self.market / 'legacy.zip', 'legacy',
                                 manifest='plugin.json')
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['tier'], 'T1-ready')
        self.assertTrue(result['enabled'])
        self.assertEqual(result['note'], '市场安装并启用')

    def test_entry_defaults_to_index_js(self):
        zip_path = _tier_one_zip(self.market / 'demo-skill.zip', 'demo-skill')
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertEqual(result['entry'], 'index.js')
        self.assertEqual(result['manifest']['entry'], 'index.js')

    def test_entry_from_manifest_wins(self):
        manifest = json.loads(_skill_manifest('custom').decode('utf-8'))
        manifest['entry'] = 'main.js'
        zip_path = self.market / 'custom.zip'
        _write_zip(zip_path, [('custom/skill.json',
                               json.dumps(manifest, ensure_ascii=False).encode('utf-8'),
                               0o644 << 16),
                              ('custom/main.js', _T1_BODY, 0o644 << 16),
                              ('custom/index.js', _T1_BODY, 0o644 << 16)])
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertEqual(result['entry'], 'main.js')

    def test_too_many_entries_rejected(self):
        zip_path = self.market / 'many.zip'
        entries = [('demo-skill/skill.json', _skill_manifest('demo-skill'), 0o644 << 16)]
        entries += [('demo-skill/f%d.txt' % index, b'x', 0o644 << 16)
                    for index in range(skill_market.MAX_ENTRIES)]
        _write_zip(zip_path, entries)
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'too-many-entries')
        self.assertFalse((self.extensions / 'demo-skill').exists())

    def test_bad_id_rejected(self):
        zip_path = self.market / 'badid.zip'
        _write_zip(zip_path, [('pkg/skill.json', _skill_manifest('../evil'),
                               0o644 << 16)])
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'bad-id')
        self.assertEqual(self._listing(self.extensions), [])

    def test_bad_zip_rejected(self):
        zip_path = self.market / 'bad.zip'
        zip_path.write_bytes(b'nope')
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'bad-zip')

    def test_midway_failure_leaves_no_half_product(self):
        zip_path = _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        real = shutil.copyfileobj
        calls = {'n': 0}

        def boom(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError('disk full')
            return real(*args, **kwargs)

        with mock.patch.object(skill_market.shutil, 'copyfileobj', boom):
            result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'io-error')
        self.assertFalse((self.extensions / 'demo-skill').exists())
        self.assertEqual(self._listing(self.extensions), [])

    def test_fresh_package_beats_stale_snapshot(self):
        """刚装进去的包必须压过旧快照：旧快照说 T6 无工具，新包自己就是 T1。"""
        self._seed_manifest([_snapshot_skill('demo-skill', 'T6-disabled', [])])
        zip_path = _tier_one_zip(self.market / 'demo-skill.zip', 'demo-skill')
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['tier'], 'T1-ready')
        self.assertEqual(result['tools'], ['demo-skill__demo'])
        item = skill_market.list_installed(runtime=self.runtime)['items'][0]
        self.assertEqual(item['tier'], 'T1-ready')
        self.assertEqual(item['tools'], ['demo-skill__demo'])

    def test_snapshot_only_fills_in_when_package_cannot_be_classified(self):
        """快照只做兜底：包自己分不出层时才拿旧结论顶上。"""
        self._seed_manifest([_snapshot_skill('demo-skill', 'T1-ready', ['demo-skill__demo'])])
        dest = self.extensions / 'demo-skill'
        dest.mkdir(parents=True)
        tier, tools = skill_market._installed_placement(
            dest, 'demo-skill', {}, self.runtime)
        self.assertEqual((tier, tools), ('T1-ready', ['demo-skill__demo']))
        tier, tools = skill_market._installed_placement(
            dest, 'demo-skill', {'tier': 'T2-web-fetch',
                                 'tools': [{'name': 'demo-skill__fetch'}]}, self.runtime)
        self.assertEqual((tier, tools), ('T2-web-fetch', ['demo-skill__fetch']))


class UninstallTests(_Case):
    def test_uninstall_ok(self):
        zip_path = _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        skill_market.install(zip_path, runtime=self.runtime)
        result = skill_market.uninstall('demo-skill', runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertTrue(result['removed'])
        self.assertFalse((self.extensions / 'demo-skill').exists())
        self.assertEqual(self._listing(self.extensions), [])

    def test_uninstall_bad_id(self):
        for bad in ('', '../etc', '/abs', 'a/b', '..'):
            result = skill_market.uninstall(bad, runtime=self.runtime)
            self.assertFalse(result['ok'], bad)
            self.assertEqual(result['error']['code'], 'bad-id', bad)
        self.assertEqual(self._listing(self.root / 'data' / 'qq-only'),
                         [] if not (self.root / 'data' / 'qq-only').exists() else
                         self._listing(self.root / 'data' / 'qq-only'))

    def test_uninstall_missing(self):
        result = skill_market.uninstall('no-such-skill', runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'missing')

    def test_uninstall_leaves_siblings(self):
        for ident in ('keep-skill', 'drop-skill'):
            skill_market.install(_good_zip(self.market / (ident + '.zip'), ident),
                                 runtime=self.runtime)
        skill_market.uninstall('drop-skill', runtime=self.runtime)
        self.assertEqual(self._listing(self.extensions), ['keep-skill'])


class ManifestSyncTests(_Case):
    """装/覆盖/卸载之后，清单与扩展发现缓存里那个 id 的结论必须跟着变。"""

    def test_install_writes_entry_and_keeps_other_skills(self):
        other = _snapshot_skill('other-skill', 'T2-web-fetch', ['other-skill__fetch'],
                                tierReason='web_fetch', entry='main.js')
        self._seed_manifest([other])
        zip_path = _tier_one_zip(self.market / 'demo-skill.zip', 'demo-skill')
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertTrue(result['manifestSynced'], result)
        self.assertEqual(result['manifestAction'], 'added')
        self.assertEqual(result['manifestPath'], str(self.manifest))
        data = self._read_manifest()
        self.assertEqual([item['id'] for item in data['skills']],
                         ['demo-skill', 'other-skill'])
        entry = data['skills'][0]
        self.assertEqual(entry['tier'], 'T1-ready')
        self.assertEqual(entry['tools'], [{'name': 'demo-skill__demo',
                                           'description': 'demo', 'hasSchema': False}])
        self.assertEqual(entry['source']['kind'], 'installed')
        # 别的 skill 逐字节不动，市场侧统计沿用上次全量构建的结论。
        self.assertEqual(data['skills'][1], other)
        self.assertEqual(data['stats']['zipsInMarketDir'], 7)
        self.assertEqual(data['meta']['marketDir'], 'D:/old-market')

    def test_reinstall_replaces_stale_entry(self):
        self._seed_manifest([_snapshot_skill('demo-skill', 'T6-disabled', [])])
        zip_path = _tier_one_zip(self.market / 'demo-skill.zip', 'demo-skill')
        self.assertTrue(skill_market.install(zip_path, runtime=self.runtime)['ok'])
        data = self._read_manifest()
        self.assertEqual(len(data['skills']), 1)
        self.assertEqual(data['skills'][0]['tier'], 'T1-ready')
        self.assertEqual(data['skills'][0]['tools'][0]['name'], 'demo-skill__demo')

    def test_absent_or_broken_manifest_is_left_alone(self):
        """没有清单就没有过时结论可失效：这里不造半份清单，重建交给 ensure_manifest。"""
        zip_path = _tier_one_zip(self.market / 'demo-skill.zip', 'demo-skill')
        result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['manifestAction'], 'absent')
        self.assertFalse(self.manifest.exists())
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text('{not json', encoding='utf-8')
        result = skill_market.install(zip_path, runtime=self.runtime, force=True)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['manifestAction'], 'broken')
        self.assertEqual(self.manifest.read_text(encoding='utf-8'), '{not json')

    def test_uninstall_removes_entry_and_keeps_siblings(self):
        self._seed_manifest([_snapshot_skill('keep-skill', 'T1-ready', ['keep-skill__demo']),
                             _snapshot_skill('drop-skill', 'T1-ready', ['drop-skill__demo'])])
        for ident in ('keep-skill', 'drop-skill'):
            skill_market.install(_tier_one_zip(self.market / (ident + '.zip'), ident),
                                 runtime=self.runtime)
        result = skill_market.uninstall('drop-skill', runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['manifestAction'], 'removed')
        data = self._read_manifest()
        self.assertEqual([item['id'] for item in data['skills']], ['keep-skill'])
        self.assertEqual(data['stats']['skillsTotal'], 1)

    def test_registry_cache_is_invalidated_after_install(self):
        zip_path = _tier_one_zip(self.market / 'demo-skill.zip', 'demo-skill')
        with mock.patch.object(skill_market.extension_registry, 'invalidate') as spy:
            installed = skill_market.install(zip_path, runtime=self.runtime)
        self.assertTrue(installed['ok'], installed['error'])
        self.assertTrue(installed['registryInvalidated'])
        self.assertEqual(spy.call_count, 1)
        with mock.patch.object(skill_market.extension_registry, 'invalidate') as spy:
            removed = skill_market.uninstall('demo-skill', runtime=self.runtime)
        self.assertTrue(removed['ok'], removed['error'])
        self.assertEqual(spy.call_count, 1)

    def test_manifest_failure_does_not_fail_the_install(self):
        zip_path = _tier_one_zip(self.market / 'demo-skill.zip', 'demo-skill')
        with mock.patch.object(skill_market, '_skill_manifest', return_value=None):
            result = skill_market.install(zip_path, runtime=self.runtime)
        self.assertTrue(result['ok'], result['error'])
        self.assertFalse(result['manifestSynced'])
        self.assertEqual(result['manifestError']['code'], 'no-manifest')
        self.assertTrue((self.extensions / 'demo-skill' / 'index.js').is_file())


class SummaryTests(_Case):
    def test_shape(self):
        _good_zip(self.market / 'demo-skill.zip', 'demo-skill')
        skill_market.install(self.market / 'demo-skill.zip', runtime=self.runtime)
        with mock.patch.object(skill_market, 'DEFAULT_MARKET_DIRS', (str(self.market),)), \
                mock.patch.dict(os.environ, {key: value for key, value in
                                             os.environ.items()
                                             if key != skill_market.MARKET_ENV},
                                clear=True):
            result = skill_market.summary(runtime=self.runtime)
        for key in ('installed', 'marketCount', 'eligible', 'blocked', 'marketDir',
                    'error'):
            self.assertIn(key, result)
        self.assertEqual(result['installed'], 1)
        self.assertEqual(result['marketCount'], 1)
        self.assertEqual(result['marketDir'], str(self.market))

    def test_degrades_when_eligibility_raises(self):
        with mock.patch.object(skill_market.skill_tools, 'eligible_skills',
                               side_effect=RuntimeError('boom')):
            result = skill_market.summary(runtime=self.runtime)
        self.assertIsNone(result['eligible'])
        self.assertIsNone(result['blocked'])


if __name__ == '__main__':
    unittest.main()
