"""T12 云原创歌曲测试：二选一参数、预算、未知结果、人声验收。"""
import json
import unittest
from types import SimpleNamespace

import httpx

from src.audio_billing import AudioBilling
from src.audio_contracts import AudioJobRequest, AudioMode, ContractError, ErrorCode
from src.singing.cloud_music import CloudMusicProvider, accept_as_singing
from tests.audio_fixtures import make_wav


def job(**over):
    base = {'client_request_id': 'song-1', 'mode': AudioMode.SONG_GENERATE, 'lyrics': '啦'}
    base.update(over)
    return AudioJobRequest(**base)


def config(**values):
    settings = {'qq_only.voice.cloud.allow_outbound': True}
    settings.update(values)
    return SimpleNamespace(get=settings.get)


class CloudMusicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.billing = AudioBilling(daily_limit=10.0, monthly_limit=100.0,
                                    per_request_limit=1.0, price_per_second=0.01,
                                    price_per_1k_chars=0.01)
        self.audio = make_wav(1.0)
        self.calls = []

    def provider(self, handler, **values):
        return CloudMusicProvider(config(**values), credentials=SimpleNamespace(api_key='xi'),
                                  billing=self.billing, http_transport=httpx.MockTransport(handler))

    async def test_prompt_and_plan_are_mutually_exclusive(self):
        provider = self.provider(lambda request: httpx.Response(500))
        with self.assertRaises(ContractError) as ctx:
            await provider.generate(job(), mode='prompt', prompt='欢快',
                                    composition_plan={'sections': []})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)
        with self.assertRaises(ContractError):
            await provider.generate(job(), mode='composition_plan',
                                    composition_plan={'sections': []}, prompt='欢快')
        with self.assertRaises(ContractError):
            await provider.generate(job(), mode='prompt', prompt='')
        self.assertEqual(self.calls, [])

    async def test_not_configured_means_no_request(self):
        provider = self.provider(lambda request: self.calls.append(request),
                                 **{'qq_only.voice.cloud.allow_outbound': False})
        with self.assertRaises(ContractError) as ctx:
            await provider.generate(job(), mode='prompt', prompt='欢快')
        self.assertEqual(ctx.exception.code, ErrorCode.CAPABILITY_UNSUPPORTED)
        self.assertEqual(self.calls, [])

    async def test_binary_wav_result_is_unverified_by_default(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=self.audio,
                                  headers={'content-type': 'audio/wav'})

        result = await self.provider(handler).generate(job(), mode='prompt', prompt='欢快')
        self.assertEqual(result['audio'], self.audio)
        self.assertIsNone(result['vocal_verified'])
        self.assertFalse(result['singing_verified'])
        self.assertIn('人声', result['warning'])
        self.assertEqual(self.billing.state_for('song-1'), 'settled')

    async def test_instrumental_only_fails_vocal_verification(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=self.audio,
                                  headers={'content-type': 'audio/wav'})

        provider = self.provider(handler)
        with self.assertRaises(ContractError) as ctx:
            await provider.generate(job(), mode='prompt', prompt='欢快',
                                    verify_vocals=lambda audio: False)
        self.assertEqual(ctx.exception.code, ErrorCode.CAPABILITY_UNSUPPORTED)
        self.assertEqual(self.billing.state_for('song-1'), 'settled')

    async def test_composition_plan_payload_has_no_prompt(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=self.audio,
                                  headers={'content-type': 'audio/wav'})

        plan = {'sections': [{'type': 'verse', 'lyrics': '啦'}]}
        await self.provider(handler).generate(job(), mode='composition_plan',
                                              composition_plan=plan)
        body = json.loads(self.calls[0].content)
        self.assertEqual(body['composition_plan'], plan)
        self.assertNotIn('prompt', body)
        self.assertEqual(self.calls[0].headers['xi-api-key'], 'xi')

    async def test_url_result_is_downloaded(self):
        def handler(request):
            self.calls.append(request)
            if request.method == 'POST':
                return httpx.Response(200, content=json.dumps(
                    {'audio_url': 'https://api.elevenlabs.io/files/a.mp3'}).encode())
            return httpx.Response(200, content=b'ID3fake-mp3',
                                  headers={'content-type': 'audio/mpeg'})

        result = await self.provider(handler).generate(job(), mode='prompt', prompt='欢快')
        self.assertEqual(result['media_type'], 'audio/mpeg')
        self.assertEqual(len(self.calls), 2)

    async def test_foreign_download_host_is_refused(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=json.dumps(
                {'audio_url': 'https://evil.example/a.mp3'}).encode())

        with self.assertRaises(ContractError) as ctx:
            await self.provider(handler).generate(job(), mode='prompt', prompt='欢快')
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)
        self.assertEqual(len(self.calls), 1)

    async def test_timeout_is_unknown_and_not_retried(self):
        def handler(request):
            self.calls.append(request)
            raise httpx.ReadTimeout('long job')

        provider = self.provider(handler)
        with self.assertRaises(ContractError) as ctx:
            await provider.generate(job(), mode='prompt', prompt='欢快', target_seconds=60)
        self.assertEqual(ctx.exception.code, ErrorCode.GENERATION_UNKNOWN)
        before = len(self.calls)
        with self.assertRaises(ContractError):
            await provider.generate(job(), mode='prompt', prompt='欢快', target_seconds=60)
        self.assertEqual(len(self.calls), before)

    async def test_401_releases_and_500_keeps_reservation(self):
        provider = self.provider(lambda request: httpx.Response(401, content=b'no'))
        with self.assertRaises(ContractError) as ctx:
            await provider.generate(job(), mode='prompt', prompt='欢快')
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)
        self.assertEqual(self.billing.state_for('song-1'), 'released')

        billing = AudioBilling(daily_limit=10.0, monthly_limit=100.0,
                               per_request_limit=1.0, price_per_second=0.01)
        provider = CloudMusicProvider(config(), credentials=SimpleNamespace(api_key='xi'),
                                      billing=billing, http_transport=httpx.MockTransport(
                                          lambda request: httpx.Response(503, content=b'busy')))
        with self.assertRaises(ContractError):
            await provider.generate(job(), mode='prompt', prompt='欢快')
        self.assertEqual(billing.state_for('song-1'), 'unknown')

    async def test_duration_out_of_range_is_rejected_before_request(self):
        provider = self.provider(lambda request: self.calls.append(request))
        with self.assertRaises(ContractError) as ctx:
            await provider.generate(job(), mode='prompt', prompt='欢快', target_seconds=2)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.calls, [])

    async def test_accept_as_singing_requires_heard_lyrics(self):
        result = {'audio': self.audio}
        with self.assertRaises(ContractError):
            accept_as_singing(result, heard_lyrics=False)
        accepted = accept_as_singing(result, heard_lyrics=True, verified_by='tester')
        self.assertTrue(accepted['singing_verified'])
        self.assertEqual(accepted['verified_by'], 'tester')


if __name__ == '__main__':
    unittest.main()
