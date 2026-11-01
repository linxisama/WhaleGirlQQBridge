"""Reserved voice API stays off by default and sends only explicit record requests."""
import asyncio
import base64
import io
import json
import sqlite3
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import websockets
import yaml
from fastapi.testclient import TestClient

from src.qq_app import QQRuntime, create_app
from src.qqbridge_transport import QqBridge
from src.voice_service import VoiceService, validate_wav, VoiceDeliveryUncertain


def wav_bytes(seconds=.05):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b'\x00\x00' * int(seconds * 16000))
    return output.getvalue()


class Config:
    def __init__(self):
        self.values = {'qq_only.voice.enabled': True,
                       'qq_only.voice.gpt_sovits.ref_audio_path': 'reference.wav'}

    def get(self, key, default=None):
        return self.values.get(key, default)


class VoiceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(':memory:')
        self.config = Config()
        self.service = VoiceService(self.temp.name, self.config, self.db)
        self.bot = SimpleNamespace(mode='qqbridge', bridge=SimpleNamespace(online=True, send_voice=AsyncMock(return_value={'message_id': 'v1'})),
                                   lock=asyncio.Lock(), account=lambda: '999', can_send_proactive=lambda kind, target: True)
        self.asset = self.service.store(wav_bytes())

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    def send_args(self, **overrides):
        return dict(audio_id=self.asset['audio_id'], kind='group', target='123', account_id='999',
                    request_id='voice-job-1', **overrides)

    async def test_default_off_makes_no_network_or_send(self):
        self.config.values.clear()
        with self.assertRaisesRegex(ValueError, '尚未启用'):
            await self.service.synthesize('你好')
        with self.assertRaisesRegex(ValueError, '尚未启用'):
            await self.service.send(self.bot, **self.send_args())
        self.bot.bridge.send_voice.assert_not_awaited()

    async def test_gpt_sovits_payload_and_wav_response(self):
        calls = []
        def handler(request):
            calls.append(json.loads(request.content))
            self.assertEqual(str(request.url), 'http://127.0.0.1:9880/tts')
            return httpx.Response(200, content=wav_bytes(), headers={'Content-Type': 'audio/wav'})
        self.service.http_transport = httpx.MockTransport(handler)
        result = await self.service.synthesize('你好')
        self.assertEqual(result['audio_id'], self.asset['audio_id'])
        self.assertEqual(calls[0]['text'], '你好')
        self.assertEqual(calls[0]['ref_audio_path'], 'reference.wav')
        self.assertEqual(calls[0]['media_type'], 'wav')
        self.assertIs(calls[0]['streaming_mode'], False)
        self.bot.bridge.send_voice.assert_not_awaited()

    async def test_remote_tts_service_is_rejected_before_request(self):
        self.config.values['qq_only.voice.gpt_sovits.endpoint'] = 'https://example.test/tts'
        with self.assertRaisesRegex(ValueError, '本机'):
            await self.service.synthesize('你好')

    async def test_tts_error_and_json_body_are_not_saved_as_audio(self):
        self.service.http_transport = httpx.MockTransport(lambda request: httpx.Response(500))
        with self.assertRaisesRegex(RuntimeError, '失败或超时'):
            await self.service.synthesize('你好')
        self.service.http_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'error': 'bad'}))
        with self.assertRaises(ValueError):
            await self.service.synthesize('你好')

    async def test_same_request_is_sent_once_even_concurrently(self):
        results = await asyncio.gather(*(self.service.send(self.bot, **self.send_args()) for _ in range(2)))
        self.assertEqual(sum(not r['duplicate'] for r in results), 1)
        self.bot.bridge.send_voice.assert_awaited_once()
        self.assertEqual(results[0]['state'], 'sent')

    async def test_changed_payload_cannot_reuse_request_id(self):
        await self.service.send(self.bot, **self.send_args())
        args = self.send_args()
        args['target'] = '456'
        with self.assertRaisesRegex(ValueError, '其他语音或目标'):
            await self.service.send(self.bot, **args)
        self.bot.bridge.send_voice.assert_awaited_once()

    async def test_uncertain_send_is_never_automatically_retried(self):
        self.bot.bridge.send_voice.side_effect = ConnectionError('lost acknowledgement')
        with self.assertRaises(VoiceDeliveryUncertain):
            await self.service.send(self.bot, **self.send_args())
        result = await self.service.send(self.bot, **self.send_args())
        self.assertEqual(result['state'], 'uncertain')
        self.assertTrue(result['duplicate'])
        self.bot.bridge.send_voice.assert_awaited_once()

    async def test_account_and_existing_reply_rules_are_enforced(self):
        self.bot.account = lambda: '888'
        with self.assertRaisesRegex(ValueError, '账号'):
            await self.service.send(self.bot, **self.send_args())
        self.bot.account = lambda: '999'
        self.bot.can_send_proactive = lambda kind, target: False
        with self.assertRaises(ValueError):
            await self.service.send(self.bot, **self.send_args())
        self.bot.bridge.send_voice.assert_not_awaited()

    async def test_audio_path_cannot_escape_managed_directory(self):
        with self.assertRaises(ValueError):
            self.service.path('../../secret')

    async def test_changed_managed_audio_is_not_sent_under_old_id(self):
        self.service.path(self.asset['audio_id']).write_bytes(wav_bytes(.1))
        with self.assertRaisesRegex(ValueError, '已变化'):
            await self.service.send(self.bot, **self.send_args())
        self.bot.bridge.send_voice.assert_not_awaited()

    async def test_missing_reference_and_long_wav_are_rejected(self):
        self.config.values['qq_only.voice.gpt_sovits.ref_audio_path'] = ''
        with self.assertRaisesRegex(ValueError, '参考音频'):
            await self.service.synthesize('你好')
        with self.assertRaisesRegex(ValueError, '时长'):
            self.service.store(wav_bytes(121))

    async def test_interrupted_sending_becomes_uncertain_after_restart(self):
        self.db.execute('INSERT INTO voice_sends VALUES(?,?,?,?)', ('old', '[]', 'sending', '{}'))
        self.db.commit()
        VoiceService(self.temp.name, self.config, self.db)
        self.assertEqual(self.db.execute("SELECT state FROM voice_sends WHERE request_id='old'").fetchone()[0], 'uncertain')


class VoiceApiTests(unittest.TestCase):
    def setUp(self):
        probe = create_app()
        paths = {route.path for route in probe.routes}
        if ('/api/voice/status' not in paths and '/api/voice/audio' not in paths
                and '/api/voice/send' in paths):
            self.skipTest('当前统一接入版只装配本机文件 /api/voice/send；此类保留旧 VoiceService 状态、导入和幂等发送 API 断言，未恢复旧接口。当前文件发送另有独立回归。')
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = {'ai': {'api_url': 'https://example.test/v1', 'api_key': 'fake', 'model': 'fake'},
                  'qq_only': {'mode': 'qqbridge'}}
        (root / 'config.qq.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
        self.bot = QQRuntime(root=root)
        self.bot.bridge.self_id = '999'
        self.bot.bridge.online = True
        self.bot.running = True
        self.bot.bridge.send_voice = AsyncMock(return_value={'message_id': 'voice-1'})
        self.app = create_app(self.bot)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_status_and_disabled_import(self):
        self.assertFalse(self.client.get('/api/voice/status').json()['enabled'])
        result = self.client.post('/api/voice/audio', json={'audio_base64': base64.b64encode(wav_bytes()).decode()})
        self.assertEqual(result.status_code, 400)
        self.bot.bridge.send_voice.assert_not_awaited()

    def test_import_then_explicit_send_and_duplicate(self):
        self.bot.config.set('qq_only.voice.enabled', True)
        asset = self.client.post('/api/voice/audio', json={'audio_base64': base64.b64encode(wav_bytes()).decode()}).json()
        self.assertTrue(asset['success'])
        self.assertNotIn('path', asset)
        body = {'audio_id': asset['audio_id'], 'kind': 'private', 'target': '123', 'account_id': '999', 'request_id': 'test-1'}
        first = self.client.post('/api/voice/send', json=body).json()
        second = self.client.post('/api/voice/send', json=body).json()
        self.assertTrue(first['success'])
        self.assertTrue(second['duplicate'])
        self.bot.bridge.send_voice.assert_awaited_once()

    def test_api_requires_explicit_identity_and_blocks_foreign_origin(self):
        self.assertEqual(self.client.post('/api/voice/send', json={}).status_code, 422)
        self.assertEqual(self.client.get('/api/voice/status', headers={'Origin': 'https://other.example'}).status_code, 403)


class RecordTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_websocket_record_segment(self):
        messages = []
        async def handler(ws):
            async for raw in ws:
                item = json.loads(raw)
                if item['action'] == 'get_login_info':
                    data = {'user_id': '999'}
                elif item['action'] == 'get_status':
                    data = {'online': True, 'good': True}
                else:
                    messages.append(item)
                    data = {'message_id': 'record-id'}
                await ws.send(json.dumps({'status': 'ok', 'retcode': 0, 'data': data, 'echo': item['echo']}))
        server = await websockets.serve(handler, '127.0.0.1', 0)
        bridge = QqBridge(ws_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}')
        try:
            await bridge.connect()
            for kind in ('group', 'private'):
                result = await bridge.send_voice(kind, '123', wav_bytes())
                self.assertEqual(result['message_id'], 'record-id')
            self.assertEqual([m['action'] for m in messages], ['send_group_msg', 'send_private_msg'])
            for item in messages:
                segments = item['params']['message']
                self.assertEqual(len(segments), 1)
                self.assertEqual(segments[0]['type'], 'record')
                self.assertEqual(base64.b64decode(segments[0]['data']['file'].removeprefix('base64://')), wav_bytes())
        finally:
            await bridge.close()
            server.close()
            await server.wait_closed()
