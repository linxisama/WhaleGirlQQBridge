"""多媒体发送接口测试：消息段形状、大小限制、模式限制、错误映射。"""
import base64
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
        return {'message_id': 9}


class _StubBot:
    def __init__(self, mode='qqbridge'):
        self.bridge = _StubOneBot()
        self.onebot = _StubOneBot()

        class _Config:
            @staticmethod
            def get(key, default=None):
                return mode if key == 'qq_only.mode' else default

        self.config = _Config()


class MediaApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        (self.root / 'config.qq.yaml').write_text('qq_only:\n  mode: qqbridge\n', encoding='utf-8')
        (self.root / 'data' / 'qq-only').mkdir(parents=True)
        from src.qq_app import create_app

        self.app = create_app()
        self.app.state.account_book = AccountBook(self.root)
        self.bot = _StubBot()
        self.app.state.bot = self.bot
        self.client = TestClient(self.app)
        self.image = self.root / 'cat.png'
        self.image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 64)

    def test_send_image_by_path_uses_base64_segment(self):
        response = self.client.post('/api/media/send', json={
            'target_type': 'group', 'target_id': '555',
            'kind': 'image', 'path': str(self.image)})
        self.assertEqual(response.status_code, 200, response.text)
        action, params = self.bot.bridge.calls[0]
        self.assertEqual(action, 'send_group_msg')
        self.assertEqual(params['group_id'], 555)
        segment = params['message'][0]
        self.assertEqual(segment['type'], 'image')
        self.assertTrue(segment['data']['file'].startswith('base64://'))
        decoded = base64.b64decode(segment['data']['file'][len('base64://'):])
        self.assertEqual(decoded, self.image.read_bytes())

    def test_send_file_keeps_name(self):
        response = self.client.post('/api/media/send', json={
            'target_type': 'private', 'target_id': '10001',
            'kind': 'file', 'path': str(self.image), 'name': '报告.png'})
        self.assertEqual(response.status_code, 200, response.text)
        action, params = self.bot.bridge.calls[0]
        self.assertEqual(action, 'send_private_msg')
        self.assertEqual(params['user_id'], 10001)
        self.assertEqual(params['message'][0]['data']['name'], '报告.png')

    def test_url_is_passed_through(self):
        response = self.client.post('/api/media/send', json={
            'target_type': 'group', 'target_id': '555', 'kind': 'image',
            'url': 'https://example.com/x.png'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.bot.bridge.calls[0][1]['message'][0]['data']['file'],
                         'https://example.com/x.png')

    def test_missing_file_is_422(self):
        response = self.client.post('/api/media/send', json={
            'target_type': 'group', 'target_id': '555', 'kind': 'image',
            'path': str(self.root / 'nope.png')})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'FILE_NOT_FOUND')

    def test_invalid_kind_and_target(self):
        self.assertEqual(self.client.post('/api/media/send', json={
            'target_type': 'group', 'target_id': '1', 'kind': 'video',
            'url': 'https://x'}).status_code, 422)
        self.assertEqual(self.client.post('/api/media/send', json={
            'target_type': 'x', 'target_id': '1', 'kind': 'image',
            'url': 'https://x'}).status_code, 422)

    def test_official_mode_is_409(self):
        self.app.state.bot = _StubBot(mode='official')
        response = self.client.post('/api/media/send', json={
            'target_type': 'group', 'target_id': '1', 'kind': 'image',
            'url': 'https://x'})
        self.assertEqual(response.status_code, 409)

    def test_upstream_error_is_502(self):
        self.bot.bridge.fail = True
        response = self.client.post('/api/media/send', json={
            'target_type': 'group', 'target_id': '1', 'kind': 'image',
            'url': 'https://x'})
        self.assertEqual(response.status_code, 502)

    def test_onebot_mode_uses_onebot_transport(self):
        self.bot = _StubBot(mode='onebot')
        self.app.state.bot = self.bot
        response = self.client.post('/api/media/send', json={
            'target_type': 'group', 'target_id': '555', 'kind': 'image',
            'url': 'https://example.com/x.png'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bot.onebot.calls[0][0], 'send_group_msg')
        self.assertEqual(self.bot.bridge.calls, [])

    def test_onebot_action_uses_bridge_in_qqbridge_mode(self):
        response = self.client.post('/api/onebot/action', json={
            'action': 'send_group_msg',
            'params': {'group_id': 555, 'message': 'hi'}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bot.bridge.calls[0][0], 'send_group_msg')
        self.assertEqual(self.bot.onebot.calls, [])


if __name__ == '__main__':
    unittest.main()
