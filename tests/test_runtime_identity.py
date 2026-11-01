"""Prevent the installed UI from silently using a stale development backend."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

import httpx
import yaml
from fastapi.testclient import TestClient
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import qq_gui
from src.ai_engine import AIEngine
from src.qq_app import QQRuntime,create_app,Settings
from src.runtime_identity import APP_ID,APP_VERSION,backend_identity_error


class IdentityTests(unittest.TestCase):
    def identity(self,root):
        return {'app':APP_ID,'version':APP_VERSION,'data_root':str(root)}

    def test_version_and_data_root_both_must_match(self):
        root=Path(tempfile.gettempdir())/'expected'
        identity=self.identity(root)
        self.assertEqual(backend_identity_error(identity,root),'')
        self.assertTrue(backend_identity_error(dict(identity,version='1.3.0'),root))
        self.assertTrue(backend_identity_error(dict(identity,data_root=str(root/'other')),root))
        self.assertTrue(backend_identity_error({},root))

    def test_wrong_backend_blocks_get_post_and_delete(self):
        response=SimpleNamespace(status_code=404,json=lambda:{'detail':'not found'})
        with patch.object(qq_gui.requests,'get',return_value=response) as get,patch.object(qq_gui.requests,'post') as post,patch.object(qq_gui.requests,'delete') as delete:
            self.assertFalse(qq_gui.api_get('plugins')['success'])
            self.assertFalse(qq_gui.api_post('settings',{'model':'x'})['success'])
            self.assertFalse(qq_gui.api_delete('personas/x')['success'])
        post.assert_not_called()
        delete.assert_not_called()
        self.assertTrue(all(call.args[0].endswith('/api/runtime') for call in get.call_args_list))

    def test_compatible_backend_allows_requested_operation(self):
        def get(url,**kwargs):
            return SimpleNamespace(status_code=200,json=lambda:self.identity(qq_gui.ROOT) if url.endswith('/runtime') else ['plugins'])
        with patch.object(qq_gui.requests,'get',side_effect=get),patch.object(qq_gui.requests,'post',return_value=SimpleNamespace(json=lambda:{'success':True})) as post:
            self.assertEqual(qq_gui.api_get('plugins'),['plugins'])
            self.assertTrue(qq_gui.api_post('settings',{'model':'x'})['success'])
        post.assert_called_once()

    def test_occupied_old_service_does_not_spawn_duplicate_backend(self):
        window=SimpleNamespace(_backend_ready=lambda:False,console_tab=SimpleNamespace(set_notice=Mock()),_start_backend_inprocess=Mock())
        with patch.object(qq_gui,'FROZEN',True),patch.object(qq_gui.requests,'get',return_value=SimpleNamespace(status_code=200)),patch.object(qq_gui,'backend_error',return_value='旧后台'):
            qq_gui.MainWindow._start_backend(window)
        window._start_backend_inprocess.assert_not_called()
        window.console_tab.set_notice.assert_called_once_with('旧后台',ok=False)

    def test_settings_are_not_populated_from_error_response(self):
        window=SimpleNamespace(settings_tab=SimpleNamespace(populate=Mock()))
        with patch.object(qq_gui,'api_get',return_value={'success':False,'message':'旧后台'}):
            qq_gui.MainWindow._load_settings(window)
        window.settings_tab.populate.assert_not_called()

    def test_runtime_endpoint_returns_version_and_real_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            config=root/'config.qq.yaml'
            config.write_text(yaml.safe_dump({'ai':{'api_url':'https://example.test/v1','api_key':'fake','model':'m'}}),encoding='utf-8')
            bot=QQRuntime(root=root,config_path=config)
            with TestClient(create_app(bot)) as client:
                result=client.get('/api/runtime').json()
                self.assertEqual(result['version'],APP_VERSION)
                self.assertEqual(Path(result['data_root']),root.resolve())
                self.assertIn('plugin_file_upload',result['features'])
                self.assertEqual(client.get('/api/runtime',headers={'Origin':'https://other.example'}).status_code,403)


class PlaceholderTests(unittest.IsolatedAsyncioTestCase):
    async def test_example_host_never_makes_a_network_request(self):
        for url in ['https://api.example.com/v1','https://example.org','https://foo.example.net/v1']:
            engine=AIEngine(url,'fake',model='m')
            engine._get_client=AsyncMock()
            self.assertFalse(engine.configured)
            self.assertEqual(await engine.chat([{'role':'user','content':'test'}]),'')
            self.assertIn('示例',engine.last_error)
            engine._get_client.assert_not_awaited()
            await engine.close()

    async def test_rejecting_placeholder_does_not_stop_healthy_bot_or_replace_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            config=root/'config.qq.yaml'
            config.write_text(yaml.safe_dump({'ai':{'api_url':'https://example.test/v1','api_key':'fake','model':'m'},
                'qq':{'app_id':'123','client_secret':'fake'}}),encoding='utf-8')
            bot=QQRuntime(root=root,config_path=config)
            bot.running=True
            before=config.read_bytes()
            try:
                with self.assertRaisesRegex(ValueError,'示例'):
                    await bot.save(Settings(api_url='https://api.example.com/v1',model='bad',app_id='123'))
                self.assertTrue(bot.running)
                self.assertEqual(config.read_bytes(),before)
            finally:
                await bot.close()

    async def test_network_error_has_actionable_detail(self):
        engine=AIEngine('https://example.test/v1','fake',max_retries=1)
        engine._get_client=AsyncMock(return_value=SimpleNamespace(post=AsyncMock(side_effect=httpx.ConnectError(''))))
        self.assertEqual(await engine.chat([{'role':'user','content':'test'}]),'')
        self.assertIn('API地址',engine.last_error)
        self.assertIn('ConnectError',engine.last_error)
        await engine.close()


if __name__=='__main__':
    unittest.main()
