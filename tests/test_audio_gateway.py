"""音频网关粘合层测试：作业→资产→查询→取消→预算/交付（不碰真实引擎与 QQ）。"""
import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src.audio_contracts import (
    AudioJobRequest, Capability, ContractError, DeliveryPlanRequest, ErrorCode, JobState,
)
from src.audio_gateway import AudioGateway, TtsCache
from tests.audio_fixtures import make_wav


class Config:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


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


def gateway_config(**over):
    values = {'qq_only.voice.provider': 'audio_cpp',
              'qq_only.voice.route_policy': 'local_only',
              'qq_only.voice.cloud.daily_limit': 10.0,
              'qq_only.voice.cloud.monthly_limit': 100.0,
              'qq_only.voice.cloud.per_request_limit': 1.0}
    values.update(over)
    return Config(values)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.audio = make_wav(0.5)
        self.adapter = FakeAdapter(self.audio)
        self.gateway = AudioGateway(self.root, gateway_config(), self.db)
        self.gateway._adapters['audio_cpp'] = self.adapter

    async def asyncTearDown(self):
        await self.gateway.close()
        self.db.close()
        self.temp.cleanup()

    async def wait_state(self, job_id, state, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.gateway.jobs.get(job_id)
            if row and row['state'] == state:
                return row
            await asyncio.sleep(0.01)
        self.fail(f'{job_id} 未到达 {state}: {self.gateway.jobs.get(job_id)}')

    async def create(self, **over):
        values = {'text': '你好', 'subject_domain': 'console:local',
                  'principal': 'local-admin', 'client_request_id': 'req-1'}
        values.update(over)
        return await self.gateway.create_tts(**values)

    async def test_create_job_runs_and_stores_asset(self):
        result = await self.create()
        self.assertTrue(result['queued'])
        await self.wait_state(result['job_id'], JobState.READY.value)
        self.assertEqual(self.adapter.calls, 1)
        meta = self.gateway.get_job(result['job_id'], subject_domain='console:local')
        self.assertEqual(meta['state'], 'ready')
        self.assertIsNotNone(meta['audio'])
        self.assertAlmostEqual(meta['audio']['duration_seconds'], 0.5, delta=0.05)

    async def test_duplicate_request_is_idempotent(self):
        first = await self.create()
        await self.wait_state(first['job_id'], JobState.READY.value)
        second = await self.create()
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['job_id'], first['job_id'])
        self.assertEqual(self.adapter.calls, 1)

    async def test_same_text_different_request_hits_cache(self):
        first = await self.create(client_request_id='req-a')
        await self.wait_state(first['job_id'], JobState.READY.value)
        second = await self.create(client_request_id='req-b')
        await self.wait_state(second['job_id'], JobState.READY.value)
        self.assertEqual(self.adapter.calls, 1)
        self.assertGreaterEqual(self.gateway.cache.stats()['hits'], 1)
        self.assertEqual(self.gateway.cache.stats()['entries'], 1)

    async def test_imported_voice_profile_reaches_adapter_and_bypasses_cache(self):
        source = self.root / 'voices'
        source.mkdir()
        sample = source / 'Alice.wav'
        sample.write_bytes(make_wav(0.2, rate=16000))
        imported = self.gateway.import_voices(paths=[str(sample)])
        self.assertEqual(len(imported), 1)
        profile_id = imported[0]['profile_id']
        first = await self.create(client_request_id='req-profile')
        await self.wait_state(first['job_id'], JobState.READY.value)
        second = await self.create(client_request_id='req-profile-2',
                                   voice_profile_id=profile_id)
        await self.wait_state(second['job_id'], JobState.READY.value)
        self.assertEqual(self.adapter.calls, 2)
        self.assertIn('gpt_sovits_api_v2',
                      self.adapter.profiles[-1]['provider_map'])
        self.assertEqual(self.gateway.capabilities()['voices'], 1)

    async def test_configured_default_profile_applies_to_default_requests(self):
        source = self.root / 'Default.wav'
        source.write_bytes(make_wav(0.2))
        imported = self.gateway.import_voices(paths=[str(source)])[0]
        self.gateway.config.values['qq_only.voice.default_profile'] = imported['profile_id']
        job = await self.create(client_request_id='req-default-profile')
        await self.wait_state(job['job_id'], JobState.READY.value)
        self.assertEqual(self.adapter.profiles[-1]['profile_id'], imported['profile_id'])

    async def test_unknown_voice_profile_is_rejected_before_queue(self):
        with self.assertRaises(ContractError) as ctx:
            await self.create(voice_profile_id='not-imported')
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.adapter.calls, 0)

    async def test_job_isolation_between_subjects(self):
        result = await self.create(subject_domain='private:1')
        await self.wait_state(result['job_id'], JobState.READY.value)
        self.assertIsNotNone(self.gateway.get_job(result['job_id'],
                                                  subject_domain='private:1'))
        with self.assertRaises(ContractError) as ctx:
            self.gateway.get_job(result['job_id'], subject_domain='private:2')
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)
        with self.assertRaises(ContractError):
            self.gateway.asset_meta(result['job_id'], subject_domain='console:local')

    async def test_cancel_queued_job_does_not_run(self):
        self.gateway.scheduler._ensure_workers = lambda: None
        result = await self.create(client_request_id='req-cancel')
        cancelled = await self.gateway.cancel(result['job_id'], subject_domain='console:local')
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(self.adapter.calls, 0)

    async def test_capabilities_reports_engines_without_secrets(self):
        payload = self.gateway.capabilities()
        self.assertIn('audio_cpp', payload['engines'])
        self.assertTrue(payload['engines']['audio_cpp']['available'])
        self.assertIn('budget', payload)
        self.assertNotIn('api_key', str(payload).lower())

    async def test_delivery_plan_and_send_with_injected_sender(self):
        stored = self.gateway.assets.store(self.audio, owner_domain='console:local')
        body = DeliveryPlanRequest(delivery_id='d1', audio_asset_ids=[stored['audio_id']],
                                   segments=[], kind='private', target='123456',
                                   account_id='10001', request_id='req-deliver')
        plan = self.gateway.submit_delivery(body, subject_domain='console:local')
        self.assertEqual(plan['plan']['segments'][0]['state'], 'pending')
        self.assertFalse(plan['send_available'])
        local = await self.gateway.send_delivery('d1', subject_domain='console:local')
        self.assertEqual(local['state'], 'local_only')

        sent_calls = []

        async def sender(**kwargs):
            sent_calls.append(kwargs)
            return {'message_id': 'm-1'}

        self.gateway.set_sender(sender)
        sent = await self.gateway.send_delivery('d1', subject_domain='console:local')
        self.assertEqual(sent['state'], 'sent')
        self.assertEqual(len(sent_calls), 1)
        status = self.gateway.delivery_status('d1', subject_domain='console:local')
        self.assertEqual(status['state'], 'sent')
        self.assertEqual(status['confirmed'], [0])

    async def test_reply_delivery_without_sender_stays_local(self):
        message = {'kind': 'private', 'target': '123456', 'account_id': '10001'}
        result = await self.gateway.enqueue_voice_reply(
            text='读出来', message=message, subject_domain='private:123456',
            principal='owner-1')
        await self.wait_state(result['job_id'], JobState.READY.value)
        delivered = await self.gateway.deliver_job(result['job_id'])
        self.assertEqual(delivered['state'], 'local_only')

    async def test_reply_delivery_uses_sender_and_is_idempotent(self):
        calls = []

        async def sender(**kwargs):
            calls.append(kwargs)
            return {'message_id': 'm-2'}

        self.gateway.set_sender(sender)
        message = {'kind': 'private', 'target': '123456', 'account_id': '10001'}
        result = await self.gateway.enqueue_voice_reply(
            text='读出来', message=message, subject_domain='private:123456',
            principal='owner-1')
        await self.wait_state(result['job_id'], JobState.READY.value)
        again = await self.gateway.deliver_job(result['job_id'])
        self.assertEqual(again['state'], 'sent')
        self.assertEqual(len(calls), 1)

    async def test_cancel_other_subject_is_denied(self):
        self.gateway.scheduler._ensure_workers = lambda: None
        result = await self.create(client_request_id='req-x', subject_domain='private:1')
        with self.assertRaises(ContractError) as ctx:
            await self.gateway.cancel(result['job_id'], subject_domain='private:2')
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)


class TtsCacheTests(unittest.TestCase):
    def test_ttl_and_byte_budget(self):
        now = [100.0]
        cache = TtsCache(ttl=5, max_bytes=10, max_entries=2, now=lambda: now[0])
        cache.put('a', b'12345')
        self.assertEqual(cache.get('a'), b'12345')
        now[0] = 106.0
        self.assertIsNone(cache.get('a'))
        cache.put('b', b'1234567890')
        cache.put('c', b'x')
        self.assertLessEqual(cache.stats()['bytes'], 10)
        self.assertLessEqual(cache.stats()['entries'], 2)

    def test_clear_and_stats(self):
        cache = TtsCache()
        cache.put('a', b'1')
        self.assertEqual(cache.get('a'), b'1')
        self.assertEqual(cache.stats()['hits'], 1)
        cache.clear()
        self.assertEqual(cache.stats()['entries'], 0)


if __name__ == '__main__':
    unittest.main()
