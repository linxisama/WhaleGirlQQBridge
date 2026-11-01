"""设置页选 OneBot 保存：令牌写进去、再保存一次不会丢、模式不被改写。"""
import tempfile
import unittest

import httpx

from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root


class OneBotSettingsSaveTests(unittest.IsolatedAsyncioTestCase):
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

    def payload(self, **overrides):
        body = {
            'mode': 'onebot',
            'api_url': 'https://api.example-provider.test/v1/chat/completions',
            'api_key': 'sk-test',
            'model': 'test-model',
            'onebot_token': 'relay-token-123',
            'groups': [],
            'private_users': [],
        }
        body.update(overrides)
        return body

    async def test_save_keeps_onebot_mode_and_token(self):
        response = await self.client.post('/api/settings', json=self.payload())
        self.assertEqual(response.status_code, 200, response.text)
        data = (await self.client.get('/api/settings')).json()
        self.assertEqual(data['mode'], 'onebot')
        self.assertTrue(data['onebot_token_set'])
        self.assertEqual(self.bot.secret('qq_only.onebot_token'), 'relay-token-123')

    async def test_blank_token_keeps_saved_value(self):
        await self.client.post('/api/settings', json=self.payload())
        # 设置页把令牌框留空 = 保留已保存值（界面上就是这么写的）。
        response = await self.client.post('/api/settings', json=self.payload(onebot_token=''))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bot.secret('qq_only.onebot_token'), 'relay-token-123')

    async def test_onebot_mode_cannot_start_without_token(self):
        await self.client.post('/api/settings', json=self.payload(onebot_token=''))
        response = await self.client.post('/api/start')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('连接令牌', response.text)


if __name__ == '__main__':
    unittest.main()
