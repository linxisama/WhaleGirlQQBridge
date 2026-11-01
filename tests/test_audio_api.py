"""v2 音频 API 集成测试（真 FastAPI 路由 + 假引擎，不连网络/QQ）。"""
import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.audio_contracts import Capability
from src.voice_v2_api import install_v2_audio_routes
from tests.audio_fixtures import make_wav


class Config:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_atomic(self):
        return None


class FakeBridge:
    online = False


class FakeBot:
    def __init__(self, root, config, db):
        self.root = root
        self.config = config
        self.db = db
        self.lock = asyncio.Lock()
        self.mode = 'qqbridge'
        self.bridge = FakeBridge()

    def account(self):
        return '10001'

    def can_send_proactive(self, kind, target):
        return True


class FakeAdapter:
    name = 'audio_cpp'
    capability = Capability.TTS
    supports_cloud = False

    def __init__(self, audio):
        self.audio = audio
        self.calls = 0
        self.probe = None
        self.profiles = []

    def available(self):
        return True

    def ready(self):
        return True

    async def synthesize(self, request, profile):
        self.calls += 1
        self.profiles.append(profile)
        return self.audio


class AudioApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS voice_sends('
                        'request_id TEXT PRIMARY KEY,payload TEXT,state TEXT,result TEXT)')
        config = Config({'qq_only.voice.provider': 'audio_cpp',
                         'qq_only.voice.route_policy': 'local_only',
                         'qq_only.voice.cloud.daily_limit': 10.0})
        self.bot = FakeBot(self.root, config, self.db)
        self.app = FastAPI()
        self.app.state.bot = self.bot
        install_v2_audio_routes(self.app)
        self.client = TestClient(self.app)
        # 首次访问创建网关，然后替换成假引擎（不连真实服务）。
        self.client.get('/api/v2/audio/capabilities')
        self.adapter = FakeAdapter(make_wav(0.4))
        self.app.state.audio_gateway._adapters['audio_cpp'] = self.adapter
        self.app.state.audio_gateway.set_sender(None)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.temp.cleanup()

    def wait_ready(self, job_id, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(f'/api/v2/audio/jobs/{job_id}')
            if response.status_code == 200 and response.json()['state'] == 'ready':
                return response.json()
            time.sleep(0.02)
        self.fail(self.client.get(f'/api/v2/audio/jobs/{job_id}').text)

    def create(self, **over):
        body = {'client_request_id': 'req-1', 'mode': 'tts', 'text': '你好'}
        body.update(over)
        return self.client.post('/api/v2/audio/jobs', json=body)

    def test_capabilities_and_job_lifecycle(self):
        caps = self.client.get('/api/v2/audio/capabilities')
        self.assertEqual(caps.status_code, 200)
        self.assertIn('engines', caps.json())
        response = self.create()
        self.assertEqual(response.status_code, 200)
        job_id = response.json()['job_id']
        row = self.wait_ready(job_id)
        self.assertEqual(row['mode'], 'tts')
        audio = row['audio']
        self.assertAlmostEqual(audio['duration_seconds'], 0.4, delta=0.05)
        asset = self.client.get('/api/v2/audio/assets/' + audio['audio_id'])
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.json()['channels'], 1)
        listing = self.client.get('/api/v2/audio/jobs').json()['jobs']
        self.assertTrue(any(item['job_id'] == job_id for item in listing))

    def test_duplicate_request_returns_same_job(self):
        first = self.create().json()
        self.wait_ready(first['job_id'])
        second = self.create().json()
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['job_id'], first['job_id'])
        self.assertEqual(self.adapter.calls, 1)

    def test_invalid_text_and_unsupported_mode(self):
        response = self.create(text='')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail']['code'], 'INVALID_INPUT')
        response = self.client.post('/api/v2/audio/jobs', json={
            'client_request_id': 'req-song', 'mode': 'song_generate', 'lyrics': '啦'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'CAPABILITY_UNSUPPORTED')

    def test_cancel_queued_job(self):
        self.app.state.audio_gateway.scheduler._ensure_workers = lambda: None
        created = self.create(client_request_id='req-cancel').json()
        response = self.client.post(f"/api/v2/audio/jobs/{created['job_id']}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['state'], 'cancelled')
        self.assertEqual(self.adapter.calls, 0)

    def test_unknown_job_and_asset_are_404_or_400(self):
        self.assertEqual(self.client.get('/api/v2/audio/jobs/nope').status_code, 404)
        missing_asset = self.client.get('/api/v2/audio/assets/' + 'b' * 64)
        self.assertIn(missing_asset.status_code, (400, 404, 503))

    def test_delivery_plan_roundtrip(self):
        job = self.create().json()
        row = self.wait_ready(job['job_id'])
        body = {'delivery_id': 'd1', 'audio_asset_ids': [row['audio']['audio_id']],
                'segments': [], 'kind': 'private', 'target': '123456',
                'account_id': '10001', 'request_id': 'req-deliver'}
        plan = self.client.post('/api/v2/audio/deliveries', json=body)
        self.assertEqual(plan.status_code, 200)
        self.assertFalse(plan.json()['send_available'])
        status = self.client.get('/api/v2/audio/deliveries/d1')
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['state'], 'pending')
        send = self.client.post('/api/v2/audio/deliveries/d1/send', json={'segments': []})
        self.assertEqual(send.status_code, 200)
        self.assertEqual(send.json()['state'], 'local_only')

    def test_voice_import_list_and_delete(self):
        source = self.root / 'Alice.wav'
        source.write_bytes(make_wav(0.2, rate=16000))
        imported = self.client.post('/api/v2/audio/voices/import',
                                    json={'paths': [str(source)]})
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()['count'], 1)
        voices = self.client.get('/api/v2/audio/voices').json()['voices']
        self.assertEqual(len(voices), 1)
        profile_id = voices[0]['profile_id']
        self.assertIn('provider_map', voices[0])
        deleted = self.client.delete(f'/api/v2/audio/voices/{profile_id}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get('/api/v2/audio/voices').json()['voices'], [])
        self.assertEqual(
            self.client.delete(f'/api/v2/audio/voices/{profile_id}').status_code, 404)

    def test_voice_import_requires_exactly_one_source(self):
        response = self.client.post('/api/v2/audio/voices/import', json={})
        self.assertEqual(response.status_code, 400)
        source = self.root / 'a.wav'
        source.write_bytes(make_wav(0.2))
        table = self.root / 'a.csv'
        table.write_text('id,name\nx,y\n', encoding='utf-8')
        response = self.client.post('/api/v2/audio/voices/import', json={
            'paths': [str(source)], 'table_path': str(table)})
        self.assertEqual(response.status_code, 400)

    def test_voice_can_be_set_as_default(self):
        source = self.root / 'Carol.wav'
        source.write_bytes(make_wav(0.2))
        imported = self.client.post('/api/v2/audio/voices/import',
                                    json={'paths': [str(source)]}).json()
        profile_id = imported['imported'][0]['profile_id']
        response = self.client.post(f'/api/v2/audio/voices/{profile_id}/default')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.bot.config.values['qq_only.voice.default_profile'], profile_id)
        self.assertEqual(
            self.client.post('/api/v2/audio/voices/nope/default').status_code, 404)

    def test_job_accepts_imported_voice_profile(self):
        source = self.root / 'Bob.wav'
        source.write_bytes(make_wav(0.2))
        imported = self.client.post('/api/v2/audio/voices/import',
                                    json={'paths': [str(source)]}).json()
        profile_id = imported['imported'][0]['profile_id']
        response = self.create(client_request_id='req-voice',
                               voice_profile_id=profile_id)
        self.assertEqual(response.status_code, 200)
        self.wait_ready(response.json()['job_id'])
        self.assertEqual(self.adapter.profiles[-1]['profile_id'], profile_id)


if __name__ == '__main__':
    unittest.main()
