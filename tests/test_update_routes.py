"""5.2.83 更新与外部插件导入路由的回归测试。

这里钉三件事：
1. 控制台发 POST /api/update（不是 GET）也能读到更新设置；
2. 仓库地址写进 config.qq.yaml，非法地址不落盘；
3. 链接导入必须先确认，且只接受外部 https。
"""
import tempfile
import unittest
from unittest import mock

import httpx

from src import app_updater
from src.qq_app import APP_VERSION, QQRuntime, create_app
from tests.test_persona_files import build_root


class UpdateRoutesTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_update_info_answers_get_and_post(self):
        for method in (self.client.get, self.client.post):
            response = await method('/api/update')
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertTrue(data['success'])
            self.assertEqual(data['version'], APP_VERSION)
            self.assertEqual(data['repo'], app_updater.DEFAULT_REPO)
            self.assertTrue(data['auto'])

    async def test_config_saves_normalized_repo_and_auto_flag(self):
        response = await self.client.post('/api/update/config', json={
            'repo': ' https://github.com/example/WhaleGirlQQBridge.git ',
            'auto': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['repo'], 'example/WhaleGirlQQBridge')
        self.assertEqual(self.bot.config.get('update.repo'), 'example/WhaleGirlQQBridge')
        self.assertFalse(self.bot.config.get('update.auto'))
        again = (await self.client.post('/api/update')).json()
        self.assertEqual(again['repo'], 'example/WhaleGirlQQBridge')
        self.assertFalse(again['auto'])

    async def test_empty_repo_uses_default_release_repo(self):
        with mock.patch.object(app_updater, 'check_update', return_value={
                'ok': True, 'hasUpdate': False}) as check:
            response = await self.client.post('/api/update/check', json={'repo': ''})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(check.call_args.args[0], app_updater.DEFAULT_REPO)

    async def test_invalid_repo_is_rejected_without_saving(self):
        response = await self.client.post('/api/update/config',
                                          json={'repo': 'not a repo', 'auto': True})
        self.assertEqual(response.status_code, 400)
        self.assertIn('owner/repo', response.json()['detail'])
        self.assertEqual(self.bot.config.get('update.repo', ''), '')

    async def test_check_rejects_unknown_repo(self):
        response = await self.client.post('/api/update/check', json={'repo': 'no-slash'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('owner/repo', response.json()['detail'])

    async def test_apply_needs_confirmation(self):
        response = await self.client.post('/api/update/apply', json={
            'repo': 'example/WhaleGirlQQBridge'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未经确认', response.json()['detail'])

    async def test_apply_refuses_to_overwrite_a_source_checkout(self):
        release = {'ok': True, 'repo': 'example/WhaleGirlQQBridge', 'current': APP_VERSION,
                   'latest': '99.0.0', 'tag': 'v99.0.0', 'hasUpdate': True,
                   'releaseUrl': '', 'publishedAt': '', 'notes': '',
                   'asset': {'name': 'WhaleGirlQQBridge-Setup-99.0.0.exe', 'size': 1,
                             'url': 'https://github.com/x/y/releases/download/v99/setup.exe',
                             'digest': ''}}
        with mock.patch.object(app_updater, 'check_update', return_value=release), \
                mock.patch.object(app_updater, 'app_executable', return_value=''):
            response = await self.client.post('/api/update/apply', json={
                'repo': 'example/WhaleGirlQQBridge', 'confirmed': True})
        self.assertEqual(response.status_code, 400)
        self.assertIn('源码运行', response.json()['detail'])


class ExtensionUrlImportRouteTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_import_url_requires_confirmation(self):
        response = await self.client.post('/api/extensions/install-url', json={
            'url': 'https://example.com/plugin.zip'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未经确认', response.json()['detail'])

    async def test_import_url_only_accepts_external_https(self):
        response = await self.client.post('/api/extensions/install-url', json={
            'url': 'http://127.0.0.1:8000/plugin.zip', 'confirmed': True})
        self.assertEqual(response.status_code, 400)
        self.assertIn('https', response.json()['detail'].lower())

    async def test_import_url_requires_url(self):
        response = await self.client.post('/api/extensions/install-url',
                                          json={'confirmed': True})
        self.assertEqual(response.status_code, 400)
        self.assertIn('缺少 url', response.json()['detail'])
