"""功能补全接口测试：OneBot 动作白名单 + 语音预留。"""
import pathlib
import tempfile
import unittest

from fastapi.testclient import TestClient

from src.account_book import AccountBook


class _StubOneBot:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def call(self, action, params=None):
        self.calls.append((action, params))
        if self.fail:
            raise RuntimeError('上游拒绝')
        return {'message_id': 42}


class _StubBot:
    def __init__(self, mode='qqbridge'):
        self.onebot = _StubOneBot()
        self.bridge = _StubOneBot()

        class _Config:
            @staticmethod
            def get(key, default=None):
                return mode if key == 'qq_only.mode' else default

        self.config = _Config()


class FeatureApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        (root / 'config.qq.yaml').write_text('qq_only:\n  mode: qqbridge\n', encoding='utf-8')
        (root / 'data' / 'qq-only').mkdir(parents=True)

        from src.qq_app import create_app

        self.app = create_app()
        self.app.state.account_book = AccountBook(root)
        self.bot = _StubBot()
        self.app.state.bot = self.bot
        self.client = TestClient(self.app)

    def test_allowed_action_calls_transport(self):
        response = self.client.post('/api/onebot/action',
                                    json={'action': 'delete_msg', 'params': {'message_id': 7}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['success'])
        self.assertEqual(self.bot.bridge.calls[0], ('delete_msg', {'message_id': 7}))

    def test_action_not_in_whitelist_is_403(self):
        response = self.client.post('/api/onebot/action',
                                    json={'action': 'set_restart', 'params': {}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'ACTION_NOT_ALLOWED')

    def test_action_without_name_is_422(self):
        self.assertEqual(self.client.post('/api/onebot/action', json={}).status_code, 422)

    def test_official_mode_rejects_onebot_action(self):
        self.app.state.bot = _StubBot(mode='official')
        response = self.client.post('/api/onebot/action',
                                    json={'action': 'get_status', 'params': {}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'MODE_NOT_ONEBOT')

    def test_upstream_error_maps_to_502(self):
        self.app.state.bot = _StubBot()
        self.app.state.bot.bridge.fail = True
        response = self.client.post('/api/onebot/action',
                                    json={'action': 'get_status', 'params': {}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['code'], 'ONEBOT_ACTION_FAILED')

    def test_voice_send_uses_record_segment(self):
        self.bot = _StubBot(mode='onebot')
        self.app.state.bot = self.bot
        voice = pathlib.Path(self._tmp.name) / 'v.wav'
        voice.write_bytes(b'RIFF' + b'0' * 100)
        response = self.client.post('/api/voice/send', json={
            'target_type': 'group', 'target_id': '123', 'path': str(voice)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['success'])
        action, params = self.bot.onebot.calls[-1]
        self.assertEqual(action, 'send_group_msg')
        self.assertEqual(params['message'][0]['type'], 'record')
        self.assertTrue(params['message'][0]['data']['file'].startswith('base64://'))

    def test_voice_send_validates_input(self):
        response = self.client.post('/api/voice/send', json={'target_type': 'bad'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'INVALID_REQUEST')

    def test_voice_send_rejects_official_mode(self):
        self.app.state.bot = _StubBot(mode='official')
        voice = pathlib.Path(self._tmp.name) / 'v.wav'
        voice.write_bytes(b'RIFF' + b'0' * 100)
        response = self.client.post('/api/voice/send', json={
            'target_type': 'private', 'target_id': '1', 'path': str(voice)})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'MODE_NOT_ONEBOT')


if __name__ == '__main__':
    unittest.main()
