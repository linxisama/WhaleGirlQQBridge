"""后端集成路由的回归测试。

覆盖 GUI 会直接调用的新接口：更新进度轮询、Windows Sandbox 状态/通道/启动/禁用、
插件包扫描与安装，以及启动时 DSH 自动接入改用 dsh_auto_connect_all。
"""
import asyncio
import json
import tempfile
import threading
import unittest
from unittest import mock

import httpx

from src import app_updater
from src import plugin_packs
from src import qq_app
from src import windows_sandbox
from src.qq_app import APP_VERSION, QQRuntime, create_app
from tests.test_persona_files import build_root


class _FakeObject:
    """模拟 windows_sandbox 里带 to_dict() 的 dataclass 返回值。"""

    def __init__(self, **data):
        self._data = dict(data)
        for key, value in data.items():
            setattr(self, key, value)

    def to_dict(self):
        return dict(self._data)


class _BackendRouteTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.bot.close()
        self.temp.cleanup()

    @property
    def sandbox_root(self):
        return self.root / 'data' / 'sandbox'


class UpdateProgressRouteTests(_BackendRouteTestCase):
    async def test_progress_is_idle_empty_snapshot_before_any_task(self):
        response = await self.client.get('/api/update/progress')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        progress = body['progress']
        self.assertEqual(progress['state'], 'idle')
        self.assertEqual(progress['done'], 0)
        self.assertEqual(progress['total'], 0)
        self.assertIsNone(progress['percent'])
        self.assertEqual(progress['message'], '')

    async def test_apply_exposes_live_progress_and_finishes(self):
        release = {
            'ok': True, 'repo': 'example/WhaleGirlQQBridge', 'current': APP_VERSION,
            'latest': '99.0.0', 'tag': 'v99.0.0', 'hasUpdate': True,
            'releaseUrl': '', 'publishedAt': '', 'notes': '',
            'asset': {'name': 'WhaleGirlQQBridge-Setup-99.0.0.exe', 'size': 100,
                      'url': 'https://github.com/example/WhaleGirlQQBridge/releases/'
                             'download/v99/setup.exe', 'digest': ''},
        }
        exe = self.root / 'WhaleGirlQQBridge.exe'
        exe.write_bytes(b'MZ')
        setup = self.root / 'setup.exe'
        setup.write_bytes(b'x' * 100)
        started = threading.Event()
        release_gate = threading.Event()

        def fake_download(info, dest_dir=None, timeout=600.0, progress=None):
            self.assertIsNotNone(progress)
            progress(50, 100)
            started.set()
            release_gate.wait(10)
            return {'path': str(setup), 'bytes': 100, 'sha256': 'a' * 64,
                    'name': 'setup.exe', 'downloadedAt': '',
                    'verified': True, 'verifyStatus': 'verified'}

        with mock.patch.object(app_updater, 'check_update', return_value=release), \
                mock.patch.object(app_updater, 'app_executable', return_value=str(exe)), \
                mock.patch.object(app_updater, 'download_update',
                                  side_effect=fake_download), \
                mock.patch.object(app_updater, 'build_update_script',
                                  return_value=self.root / 'update.cmd'), \
                mock.patch.object(app_updater, 'launch_update_script'):
            task = asyncio.create_task(self.client.post('/api/update/apply', json={
                'repo': 'example/WhaleGirlQQBridge', 'confirmed': True}))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5),
                                'download_update 没有被调用')
                middle = (await self.client.get('/api/update/progress')).json()
                self.assertEqual(middle['progress']['state'], 'downloading')
                self.assertGreater(middle['progress']['percent'], 0)
                self.assertEqual(middle['progress']['done'], 50)
            finally:
                release_gate.set()
            response = await task

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertTrue(body['hasUpdate'])
        self.assertEqual(body['progress']['state'], 'done')
        self.assertGreater(body['progress']['percent'], 0)
        self.assertTrue(body['progress']['verified'])
        after = (await self.client.get('/api/update/progress')).json()
        self.assertEqual(after['progress']['state'], 'done')
        self.assertGreater(after['progress']['percent'], 0)

    async def test_apply_rejects_without_confirmation_before_touching_updater(self):
        with mock.patch.object(app_updater, 'check_update') as check:
            response = await self.client.post('/api/update/apply', json={
                'repo': 'example/WhaleGirlQQBridge'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未经确认', response.json()['detail'])
        check.assert_not_called()


class SandboxRouteTests(_BackendRouteTestCase):
    async def test_status_returns_detection_payload(self):
        detection = _FakeObject(ok=True, status='ready', available=True,
                                detail='Windows Sandbox 可用')
        with mock.patch.object(windows_sandbox, 'detect', return_value=detection) as detect:
            response = await self.client.get('/api/sandbox/status')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['status'], 'ready')
        self.assertTrue(body['available'])
        detect.assert_called_once_with()

    async def test_enable_requires_confirmation(self):
        response = await self.client.post('/api/sandbox/enable', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未经确认', response.json()['detail'])

    async def test_enable_returns_success_and_status(self):
        with mock.patch.object(windows_sandbox, 'ensure_ready',
                               return_value={'ok': True, 'status': 'ready',
                                             'detail': '已启用'}) as ready:
            response = await self.client.post('/api/sandbox/enable',
                                              json={'confirmed': True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['status'], 'ready')
        ready.assert_called_once_with(apply=True)

    async def test_enable_unsupported_is_not_a_500(self):
        with mock.patch.object(windows_sandbox, 'ensure_ready',
                               return_value={'ok': False, 'status': 'unsupported',
                                             'detail': '当前系统不支持'}):
            response = await self.client.post('/api/sandbox/enable',
                                              json={'confirmed': True, 'apply': False})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['status'], 'unsupported')

    async def test_channel_status_uses_default_name_and_root(self):
        status = _FakeObject(name='default', exists=True, wsb_path='')
        with mock.patch.object(windows_sandbox, 'read_channel_status',
                               return_value=status) as read:
            response = await self.client.get('/api/sandbox/channel')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['channel']['name'], 'default')
        read.assert_called_once_with(name='default', root=self.sandbox_root)

    async def test_channel_create_defaults_host_folder_under_bot_root(self):
        config = _FakeObject(host_folder='', networking=False, clipboard=False,
                             read_only=True, memory_mb=2048)
        channel = _FakeObject(name='demo', wsb_path=str(self.sandbox_root / 'demo' / 'demo.wsb'))
        status = _FakeObject(name='demo', exists=True, wsb_path=channel.wsb_path)
        captured = {}

        def fake_default(host_folder, **overrides):
            captured['host_folder'] = host_folder
            captured['overrides'] = overrides
            return config

        with mock.patch.object(windows_sandbox, 'default_config',
                               side_effect=fake_default), \
                mock.patch.object(windows_sandbox, 'create_isolation_channel',
                                  return_value=channel) as create, \
                mock.patch.object(windows_sandbox, 'read_channel_status',
                                  return_value=status) as read:
            response = await self.client.post('/api/sandbox/channel', json={
                'name': 'demo', 'launch': True, 'confirmed': True,
                'networking': True, 'clipboard': False, 'read_only': True,
                'memory_mb': 2048})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['channel']['name'], 'demo')
        self.assertEqual(body['status']['name'], 'demo')
        expected = str(self.sandbox_root / 'demo' / 'workspace')
        self.assertEqual(captured['host_folder'], expected)
        self.assertEqual(captured['overrides']['networking'], True)
        self.assertEqual(captured['overrides']['read_only'], True)
        self.assertEqual(captured['overrides']['memory_mb'], 2048)
        create.assert_called_once_with(config, name='demo', root=self.sandbox_root,
                                       launch=True)
        read.assert_called_once_with(name='demo', root=self.sandbox_root)

    async def test_channel_create_launch_needs_confirmation(self):
        response = await self.client.post('/api/sandbox/channel', json={
            'name': 'demo', 'launch': True})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未经确认', response.json()['detail'])

    async def test_launch_requires_confirmation(self):
        response = await self.client.post('/api/sandbox/launch', json={'name': 'default'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未经确认', response.json()['detail'])

    async def test_launch_returns_404_when_channel_has_no_wsb(self):
        status = _FakeObject(name='default', exists=False, wsb_path='')
        with mock.patch.object(windows_sandbox, 'read_channel_status', return_value=status):
            response = await self.client.post('/api/sandbox/launch', json={
                'name': 'default', 'confirmed': True})
        self.assertEqual(response.status_code, 404)
        self.assertIn('隔离通道', response.json()['detail'])

    async def test_launch_uses_existing_wsb_and_returns_result(self):
        wsb = self.sandbox_root / 'default' / 'default.wsb'
        wsb.parent.mkdir(parents=True, exist_ok=True)
        wsb.write_text('<Configuration></Configuration>', encoding='utf-8')
        status = _FakeObject(name='default', exists=True, wsb_path=str(wsb))
        result = _FakeObject(ok=True, status='started', command='WindowsSandbox.exe',
                             detail='', pid=1234)
        with mock.patch.object(windows_sandbox, 'read_channel_status', return_value=status), \
                mock.patch.object(windows_sandbox, 'launch_sandbox',
                                  return_value=result) as launch:
            response = await self.client.post('/api/sandbox/launch', json={
                'name': 'default', 'confirmed': True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['launch']['status'], 'started')
        launch.assert_called_once_with(str(wsb))

    async def test_disable_requires_confirmation(self):
        response = await self.client.post('/api/sandbox/disable', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未经确认', response.json()['detail'])

    async def test_disable_returns_unsupported_without_500(self):
        result = _FakeObject(ok=False, status='unsupported', detail='当前系统不支持')
        with mock.patch.object(windows_sandbox, 'disable_sandbox',
                               return_value=result) as disable:
            response = await self.client.post('/api/sandbox/disable',
                                              json={'confirmed': True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['status'], 'unsupported')
        disable.assert_called_once_with(apply=False)


class PluginPacksRouteTests(_BackendRouteTestCase):
    def write_pack(self, pack_id='demo-pack', body='VALUE = 1\n'):
        folder = self.root / 'plugin_packages' / pack_id
        folder.mkdir(parents=True, exist_ok=True)
        manifest = {
            'id': pack_id,
            'name': '演示包',
            'version': '1.0.0',
            'api_version': 1,
            'entry': 'demo.py',
            'host': 'WhaleGirlQQ',
            'min_host_version': '5.0.0',
            'permissions': [],
            'description': '测试用包',
        }
        (folder / 'plugin.json').write_text(json.dumps(manifest, ensure_ascii=False),
                                            encoding='utf-8')
        (folder / 'demo.py').write_text(body, encoding='utf-8')
        return folder

    async def test_scan_lists_directory_pack(self):
        self.write_pack()
        response = await self.client.get('/api/plugins/packs')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['directory'], str(plugin_packs.default_packs_dir(self.root)))
        pack = next(item for item in body['packs'] if item['id'] == 'demo-pack')
        self.assertTrue(pack['ok'])
        self.assertEqual(pack['version'], '1.0.0')

    async def test_scan_uses_packaged_root_when_bot_root_has_none(self):
        with mock.patch.object(plugin_packs, 'scan',
                               return_value={'packs': [], 'issues': []}) as scan:
            response = await self.client.get('/api/plugins/packs')
        self.assertEqual(response.status_code, 200, response.text)
        scan.assert_called_once_with(root=plugin_packs.repo_root())

    async def test_install_requires_confirmation(self):
        self.write_pack()
        response = await self.client.post('/api/plugins/packs/install',
                                          json={'id': 'demo-pack'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未经确认', response.json()['detail'])

    async def test_install_copies_pack_and_syncs(self):
        self.write_pack()
        sync_result = {'added': ['demo-pack'], 'removed': [], 'reloaded': [],
                       'unchanged': [], 'rejected': [], 'failed': [], 'actions': 1}
        with mock.patch.object(self.bot.plugins, 'sync_mods',
                               return_value=sync_result) as sync:
            response = await self.client.post('/api/plugins/packs/install', json={
                'id': 'demo-pack', 'confirmed': True})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertTrue(body['success'])
            self.assertEqual(body['id'], 'demo-pack')
            self.assertTrue(body['installed'])
            self.assertTrue(body['changed'])
            self.assertEqual(body['sync'], sync_result)
            target = self.bot.plugins.custom_plugins_dir / 'demo-pack'
            self.assertEqual(body['target'], str(target))
            self.assertTrue((target / 'plugin.json').is_file())
            self.assertTrue((target / 'demo.py').is_file())
            sync.assert_called_once_with()

            again = await self.client.post('/api/plugins/packs/install', json={
                'id': 'demo-pack', 'confirmed': True})
            self.assertEqual(again.status_code, 409)
            self.assertIn('插件包已经安装', again.json()['detail'])

            (target / 'demo.py').write_text('VALUE = 2\nEXTRA = 3\n', encoding='utf-8')
            overwritten = await self.client.post('/api/plugins/packs/install', json={
                'id': 'demo-pack', 'confirmed': True, 'overwrite': True})
            self.assertEqual(overwritten.status_code, 200, overwritten.text)
            self.assertTrue(overwritten.json()['changed'])
            self.assertEqual(sync.call_count, 2)
            self.assertEqual((target / 'demo.py').read_text(encoding='utf-8'), 'VALUE = 1\n')

    async def test_install_rejects_unsafe_id(self):
        response = await self.client.post('/api/plugins/packs/install', json={
            'id': '../evil', 'confirmed': True})
        self.assertEqual(response.status_code, 400)
        self.assertIn('id', response.json()['detail'])

    async def test_scan_marks_installed_directory(self):
        self.write_pack()
        target = self.bot.plugins.custom_plugins_dir / 'demo-pack'
        target.mkdir(parents=True)
        (target / 'plugin.json').write_text('{}', encoding='utf-8')
        response = await self.client.get('/api/plugins/packs')
        self.assertEqual(response.status_code, 200, response.text)
        pack = next(item for item in response.json()['packs']
                    if item['id'] == 'demo-pack')
        self.assertTrue(pack['installed'])

    async def test_install_with_enable_enables_loaded_plugin(self):
        self.write_pack()
        self.bot.plugins._plugins['demo_plugin'] = _FakeObject(enabled=True)
        sync_result = {'added': ['demo-pack'], 'removed': [], 'reloaded': [],
                       'unchanged': [], 'rejected': [], 'failed': [], 'actions': 1}
        with mock.patch.object(self.bot.plugins, 'sync_mods',
                               return_value=sync_result), \
                mock.patch.object(self.bot.plugins, 'mod_plugin_ids',
                                  return_value=['demo_plugin']), \
                mock.patch.object(self.bot.plugins, 'update_settings') as update:
            response = await self.client.post('/api/plugins/packs/install', json={
                'id': 'demo-pack', 'confirmed': True, 'enable': True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['enabled'], ['demo_plugin'])
        self.assertEqual(body['enableErrors'], [])
        update.assert_called_once_with('demo_plugin', {'enabled': True})

    async def test_custom_list_includes_directory_pack(self):
        folder = self.bot.plugins.custom_plugins_dir / 'demo-pack'
        folder.mkdir(parents=True)
        (folder / 'plugin.json').write_text('{}', encoding='utf-8')
        with mock.patch.object(self.bot.plugins, 'custom_plugin_ids',
                               return_value=[]), \
                mock.patch.object(self.bot.plugins, 'mod_plugin_ids',
                                  return_value=['demo_plugin']):
            response = await self.client.get('/api/plugins/custom')
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()['plugins']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'demo_plugin')
        self.assertEqual(items[0]['kind'], 'pack')
        self.assertEqual(items[0]['mod'], 'demo-pack')

    async def test_delete_directory_pack_removes_folder(self):
        folder = self.bot.plugins.custom_plugins_dir / 'demo-pack'
        folder.mkdir(parents=True)
        (folder / 'plugin.json').write_text('{}', encoding='utf-8')
        with mock.patch.object(self.bot.plugins, 'mod_id_for_plugin',
                               return_value='demo-pack'), \
                mock.patch.object(self.bot.plugins, 'custom_mod_path',
                                  return_value=folder), \
                mock.patch.object(self.bot.plugins, 'sync_mods',
                                  return_value={'removed': ['demo-pack']}):
            response = await self.client.delete('/api/plugins/demo_plugin')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['kind'], 'pack')
        self.assertEqual(body['mod'], 'demo-pack')
        self.assertFalse(folder.exists())


class AutoConnectDshTests(_BackendRouteTestCase):
    async def test_calls_auto_connect_all_and_logs_info(self):
        result = {'ok': True, 'attempted': 2, 'linked': 1, 'skipped': 1,
                  'failed': 0, 'message': 'desktop 已接入'}
        with mock.patch('src.agent_backend_api.dsh_auto_connect_all',
                        return_value=result) as auto, \
                mock.patch('src.qq_app._log_event') as log:
            qq_app._auto_connect_dsh(self.app)
        auto.assert_called_once_with(self.app)
        log.assert_called_once()
        args = log.call_args.args
        self.assertEqual(args[1], 'info')
        self.assertEqual(args[2], 'backend')
        self.assertIn('尝试 2', args[3])
        self.assertIn('成功 1', args[3])

    async def test_failure_is_logged_as_warning(self):
        result = {'ok': False, 'attempted': 1, 'linked': 0, 'skipped': 0,
                  'failed': 1, 'message': '启动失败'}
        with mock.patch('src.agent_backend_api.dsh_auto_connect_all',
                        return_value=result), \
                mock.patch('src.qq_app._log_event') as log:
            qq_app._auto_connect_dsh(self.app)
        args = log.call_args.args
        self.assertEqual(args[1], 'warning')
        self.assertIn('失败 1', args[3])


if __name__ == '__main__':
    unittest.main()
