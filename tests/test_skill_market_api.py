# -*- coding: utf-8 -*-
"""WP12b：技能市场七个端点的真机测试（TestClient + 临时 data + 替身 runtime）。

全部端点都真打 HTTP：读端点 GET/POST 双挂，写端点走 POST；
runtime 用 types.SimpleNamespace 替身（data_root 指向临时目录），
extension_registry 的启用态落在临时 data/qq-only 里，不碰真实用户数据。
"""
import asyncio
import json
import os
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from src import extension_registry, skill_market
from src.qq_app import create_app

_T1_BODY = b'ctx.registerTool({ name: "demo" });\n'
_T4_BODY = (b'const llm = require("../../src/llm.js");\n'
            b'ctx.registerTool({ name: "deep" });\n')


def _manifest(ident, **extra):
    data = {'id': ident, 'name': ident, 'version': '1.0.0',
            'description': 'demo', 'prompt': 'hello'}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False).encode('utf-8')


def _write_zip(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data, attr in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = attr or (0o644 << 16)
            archive.writestr(info, data)


def _skill_zip(path, ident, body=_T1_BODY, manifest='skill.json'):
    _write_zip(path, [(ident + '/' + manifest, _manifest(ident), 0o644 << 16),
                      (ident + '/index.js', body, 0o644 << 16)])
    return path


class _Case(unittest.TestCase):
    def setUp(self):
        extension_registry.invalidate()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / 'data'
        (self.data / 'qq-only').mkdir(parents=True)
        self.extensions = self.data / 'qq-only' / 'skill_extensions'
        self.market = self.root / 'market'
        self.market.mkdir()
        env = mock.patch.dict(os.environ, {skill_market.MARKET_ENV: str(self.market)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(extension_registry.invalidate)

    def _client(self):
        # data_root 给 skill 工具，plugins.data_dir 给 extension_registry：都在临时目录里
        app = create_app()
        app.state.bot = types.SimpleNamespace(
            data_root=str(self.root),
            plugins=types.SimpleNamespace(data_dir=str(self.data)))
        return TestClient(app)

    def _post(self, client, url, payload=None):
        return client.post(url, json=payload if payload is not None else {})


class MarketReadTests(_Case):
    def test_market_get_and_post(self):
        _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        client = self._client()
        got = client.get('/api/skills/market')
        self.assertEqual(got.status_code, 200)
        data = got.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['count'], 1)
        self.assertEqual(Path(data['market']), self.market)
        item = data['items'][0]
        for key in ('id', 'name', 'version', 'description', 'file', 'bytes',
                    'installed', 'tier', 'tools'):
            self.assertIn(key, item)
        posted = self._post(client, '/api/skills/market').json()
        self.assertEqual(posted['count'], 1)
        self.assertIsNone(posted['error'])

    def test_installed_get_and_post(self):
        _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        client = self._client()
        self.assertTrue(self._post(client, '/api/skills/install',
                                  {'file': str(self.market / 'demo-skill.zip')}
                                  ).json()['ok'])
        for response in (client.get('/api/skills/installed'),
                         self._post(client, '/api/skills/installed')):
            data = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(data['ok'])
            self.assertEqual(data['count'], 1)
            self.assertEqual(Path(data['root']), self.extensions)
            item = data['items'][0]
            for key in ('id', 'name', 'version', 'tier', 'tools', 'enabled',
                        'known', 'blocked'):
                self.assertIn(key, item)

    def test_summary_get_and_post(self):
        _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        client = self._client()
        for response in (client.get('/api/skills/summary'),
                         self._post(client, '/api/skills/summary')):
            data = response.json()
            self.assertEqual(response.status_code, 200)
            for key in ('installed', 'marketCount', 'eligible', 'blocked',
                        'marketDir', 'error'):
                self.assertIn(key, data)
            self.assertEqual(data['marketCount'], 1)
            self.assertEqual(Path(data['marketDir']), self.market)


class InstallEndpointTests(_Case):
    def test_tier_one_installs_enabled(self):
        zip_path = _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        data = self._post(self._client(), '/api/skills/install',
                          {'file': str(zip_path)}).json()
        self.assertTrue(data['ok'], data['error'])
        self.assertEqual(data['id'], 'demo-skill')
        self.assertEqual(data['tier'], 'T1-ready')
        self.assertEqual(data['entry'], 'index.js')
        self.assertTrue(data['enabled'])
        self.assertEqual(data['note'], '市场安装并启用')
        self.assertIsNone(data['error'])

    def test_t4_installs_but_stays_disabled(self):
        zip_path = _skill_zip(self.market / 'deep.zip', 'deep-skill', body=_T4_BODY)
        data = self._post(self._client(), '/api/skills/install',
                          {'file': str(zip_path)}).json()
        self.assertTrue(data['ok'], data['error'])
        self.assertEqual(data['tier'], 'T4-host-source')
        self.assertFalse(data['enabled'])
        self.assertIn('未启用', data['note'])
        self.assertIn('T4-host-source', data['note'])

    def test_enable_false_is_explicitly_off(self):
        zip_path = _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        data = self._post(self._client(), '/api/skills/install',
                          {'file': str(zip_path), 'enable': False}).json()
        self.assertTrue(data['ok'], data['error'])
        self.assertFalse(data['enabled'])
        self.assertEqual(data['note'], '市场安装，按你的选择停用')

    def test_enable_must_be_bool(self):
        zip_path = _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        response = self._post(self._client(), '/api/skills/install',
                             {'file': str(zip_path), 'enable': 'yes'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('enable', json.dumps(response.json(), ensure_ascii=False))

    def test_missing_source_is_400(self):
        response = self._post(self._client(), '/api/skills/install', {})
        self.assertEqual(response.status_code, 400)

    def test_market_file_name_resolves(self):
        _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        data = self._post(self._client(), '/api/skills/install',
                          {'file': 'demo-skill.zip'}).json()
        self.assertTrue(data['ok'], data['error'])
        self.assertEqual(data['id'], 'demo-skill')

    def test_exists_is_200_with_error_code(self):
        zip_path = _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        client = self._client()
        body = {'file': str(zip_path)}
        self.assertTrue(self._post(client, '/api/skills/install', body).json()['ok'])
        again = self._post(client, '/api/skills/install', body)
        self.assertEqual(again.status_code, 200)
        data = again.json()
        self.assertFalse(data['ok'])
        self.assertEqual(data['error']['code'], 'exists')

    def test_no_manifest_is_200_with_code(self):
        zip_path = self.market / 'nomanifest.zip'
        _write_zip(zip_path, [('demo-skill/index.js', b'1', 0o644 << 16)])
        data = self._post(self._client(), '/api/skills/install',
                          {'file': str(zip_path)}).json()
        self.assertFalse(data['ok'])
        self.assertEqual(data['error']['code'], 'no-manifest')

    def test_uninstall(self):
        zip_path = _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        client = self._client()
        self.assertTrue(self._post(client, '/api/skills/install',
                                   {'file': str(zip_path)}).json()['ok'])
        data = self._post(client, '/api/skills/uninstall', {'id': 'demo-skill'}).json()
        self.assertTrue(data['ok'], data['error'])
        self.assertTrue(data['removed'])
        self.assertFalse((self.extensions / 'demo-skill').exists())
        missing = self._post(client, '/api/skills/uninstall', {'id': 'ghost'})
        self.assertEqual(missing.status_code, 200)
        self.assertFalse(missing.json()['ok'])
        self.assertEqual(missing.json()['error']['code'], 'missing')
        self.assertEqual(self._post(client, '/api/skills/uninstall', {}).status_code, 400)

    def test_rescan_forces_rebuild(self):
        _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        client = self._client()
        self.assertTrue(self._post(client, '/api/skills/install',
                                   {'file': str(self.market / 'demo-skill.zip')}
                                   ).json()['ok'])
        first = self._post(client, '/api/skills/rescan')
        self.assertEqual(first.status_code, 200)
        data = first.json()
        self.assertTrue(data['ok'], data['error'])
        self.assertGreaterEqual(data['skills'], 1)
        self.assertIsInstance(data['elapsedMs'], int)
        self.assertTrue(data['rebuilt'])
        self.assertIsNone(data['error'])


class ThreadOffloadTests(_Case):
    def test_market_endpoint_runs_in_worker_thread(self):
        _skill_zip(self.market / 'demo-skill.zip', 'demo-skill')
        client = self._client()
        real = asyncio.to_thread
        seen = []
        main = threading.get_ident()

        async def spy(func, *args, **kwargs):
            seen.append((getattr(func, '__name__', str(func)), threading.get_ident()))
            return await real(func, *args, **kwargs)

        with mock.patch.object(asyncio, 'to_thread', spy):
            data = self._post(client, '/api/skills/market').json()
        self.assertTrue(data['ok'])
        names = [name for name, _ in seen]
        self.assertIn('list_market', names)
        workers = [ident for name, ident in seen if name == 'list_market']
        self.assertTrue(workers and all(ident != main for ident in workers), seen)


class BridgeGateTests(_Case):
    def test_every_extension_carries_bridge_gate(self):
        """真清单：每条都有 bridgeGate，且与能力桥口径一致（known 且未启用 → registry）。"""
        # Exported market assets are optional. Discover a real isolated manifest
        # instead of depending on the developer's sibling market directory.
        manifest = self.root / 'manifest'
        manifest.mkdir()
        fixture = manifest / 'fixture.js'
        fixture.write_text('export default {};\n', encoding='utf-8')
        (manifest / 'all-plugins-v4.json').write_text(json.dumps([
            {'id': 'fixture-disabled', 'file': str(fixture), 'ok': True,
             'extension': {'tools': ['fixture-tool']}}
        ]), encoding='utf-8')
        extension_registry.set_enabled('fixture-disabled', False, self.data, source='market')
        manifest_env = mock.patch.dict(os.environ, {'QQAGENT_MANIFEST_DIR': str(manifest)})
        manifest_env.start()
        self.addCleanup(manifest_env.stop)
        extension_registry.invalidate()
        data = self._post(self._client(), '/api/extensions').json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['items'])
        self.assertIn('fixture-disabled', {item['id'] for item in data['items']})
        seen = set()
        for item in data['items']:
            gate = item['bridgeGate']
            self.assertEqual(set(gate), {'known', 'enabled', 'blockedBy'})
            self.assertEqual(gate['known'], extension_registry.is_known(item['id'], self.data))
            self.assertEqual(gate['enabled'],
                             extension_registry.is_enabled(item['id'], self.data))
            expected = ('registry' if gate['known'] and not gate['enabled']
                        else 'tier' if item['tier'] in ('T4-host-source', 'T6-disabled')
                        else '')
            self.assertEqual(gate['blockedBy'], expected, item['id'])
            seen.add(gate['blockedBy'])
        self.assertIn('registry', seen)

    def test_blocked_by_mapping(self):
        """known+disabled → registry；未登记且层级干净 → ''；未登记但层级被拦 → tier。"""
        extension_registry.set_enabled('demo-gate', False, self.data, source='test')
        crafted = [
            {'id': 'demo-gate', 'layer': 'catalog', 'source': 'market',
             'tier': 'T1-ready', 'tools': [], 'hooks': [], 'providers': []},
            {'id': 'demo-ghost', 'layer': 'catalog', 'source': 'market',
             'tier': 'T1-ready', 'tools': [], 'hooks': [], 'providers': []},
            {'id': 'demo-tier', 'layer': 'catalog', 'source': 'market',
             'tier': 'T4-host-source', 'tools': [], 'hooks': [], 'providers': []},
        ]
        with mock.patch.object(extension_registry, 'list_extensions',
                               lambda *args, **kwargs: crafted), \
                mock.patch.object(extension_registry, 'summary',
                                  lambda *args, **kwargs: {'layers': {}, 'items': 3}):
            data = self._post(self._client(), '/api/extensions').json()
        gates = {item['id']: item['bridgeGate'] for item in data['items']}
        self.assertEqual(gates['demo-gate'], {'known': True, 'enabled': False,
                                              'blockedBy': 'registry'})
        self.assertEqual(gates['demo-ghost']['known'], False)
        self.assertEqual(gates['demo-ghost']['blockedBy'], '')
        self.assertEqual(gates['demo-tier']['blockedBy'], 'tier')


if __name__ == '__main__':
    unittest.main()
