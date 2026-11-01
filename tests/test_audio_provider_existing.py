"""T05 适配器测试：旧参数保持一致，错误可分类，不碰真实引擎。"""
import json
import unittest
from types import SimpleNamespace

import httpx

from src.audio_contracts import AudioJobRequest, AudioMode, ContractError, ErrorCode
from src.audio_providers.local_existing import AudioCppProvider, GptSovitsProvider
from tests.audio_fixtures import make_wav


def job(text='你好'):
    return AudioJobRequest(client_request_id='req-1', mode=AudioMode.TTS, text=text)


def config(**values):
    settings = {'qq_only.voice.audio_cpp.endpoint': 'http://127.0.0.1:18100/v1/audio/speech',
                'qq_only.voice.gpt_sovits.endpoint': 'http://127.0.0.1:9880/tts',
                'qq_only.voice.gpt_sovits.ref_audio_path': 'reference.wav'}
    settings.update(values)
    return SimpleNamespace(get=settings.get)


class AudioCppProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.audio = make_wav(0.2, rate=16000)
        self.calls = []

        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=self.audio)

        self.provider = AudioCppProvider(config(), http_transport=httpx.MockTransport(handler))

    async def test_payload_matches_legacy_contract(self):
        audio = await self.provider.synthesize(job(), {})
        self.assertEqual(audio, self.audio)
        self.assertEqual(str(self.calls[0].url),
                         'http://127.0.0.1:18100/v1/audio/speech')
        self.assertEqual(json.loads(self.calls[0].content),
                         {'model': 'breeze-tts-2', 'input': '你好',
                          'voice': 'demo_3_woman', 'response_format': 'wav'})

    async def test_profile_override_payload(self):
        captured = []
        provider = AudioCppProvider(config(), http_transport=httpx.MockTransport(
            lambda request: captured.append(request) or httpx.Response(200, content=self.audio)))
        await provider.synthesize(job(), {'provider_map': {
            'audio_cpp': {'model': 'index-tts2-5', 'voice': 'demo_2_man'}}})
        payload = json.loads(captured[0].content)
        self.assertEqual(payload['model'], 'index-tts2-5')
        self.assertEqual(payload['voice'], 'demo_2_man')

    async def test_shared_http_client_is_used(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=self.audio)))
        try:
            provider = AudioCppProvider(config(), http_client=client)
            self.assertEqual(await provider.synthesize(job(), {}), self.audio)
        finally:
            await client.aclose()

    async def test_timeout_is_classified(self):
        def handler(request):
            raise httpx.ReadTimeout('slow')

        provider = AudioCppProvider(config(), http_transport=httpx.MockTransport(handler))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.DEADLINE_EXCEEDED)

    async def test_bad_audio_and_oversize_are_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            await AudioCppProvider(
                config(), http_transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=b'not a wave'))).synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

        big = b'\0' * (8 * 1024 * 1024 + 1)
        with self.assertRaises(ContractError) as ctx:
            await AudioCppProvider(
                config(), http_transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=big))).synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    async def test_http_error_status_is_classified(self):
        with self.assertRaises(ContractError) as ctx:
            await AudioCppProvider(
                config(), http_transport=httpx.MockTransport(
                    lambda request: httpx.Response(503, content=b'busy'))).synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    async def test_non_loopback_endpoint_never_connects(self):
        provider = AudioCppProvider(config(**{
            'qq_only.voice.audio_cpp.endpoint': 'https://example.com/v1/audio/speech'}),
            http_transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)
        self.assertFalse(provider.available())

    async def test_invalid_voice_path_is_rejected(self):
        provider = AudioCppProvider(config(**{
            'qq_only.voice.audio_cpp.voice': 'D:/private.wav'}), http_transport=None)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_ready_uses_injected_probe(self):
        self.assertTrue(AudioCppProvider(config(), probe=lambda: True).ready())
        self.assertFalse(AudioCppProvider(config(), probe=lambda: False).ready())


class GptSovitsProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.audio = make_wav(0.2, rate=24000)
        self.calls = []

        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=self.audio)

        self.provider = GptSovitsProvider(config(), http_transport=httpx.MockTransport(handler))

    async def test_legacy_payload_and_no_cache(self):
        for _ in range(2):
            await self.provider.synthesize(job(), {})
        self.assertEqual(len(self.calls), 2)
        payload = json.loads(self.calls[0].content)
        self.assertEqual(payload['ref_audio_path'], 'reference.wav')
        self.assertEqual(payload['media_type'], 'wav')
        self.assertFalse(payload['streaming_mode'])

    async def test_profile_overrides_reference_and_language(self):
        captured = []
        provider = GptSovitsProvider(config(), http_transport=httpx.MockTransport(
            lambda request: captured.append(request) or httpx.Response(200, content=self.audio)))
        await provider.synthesize(job(), {'provider_map': {'gpt_sovits_api_v2': {
            'ref_audio_path': 'managed/ref.wav', 'prompt_text': '参考台词',
            'text_lang': 'ja', 'prompt_lang': 'ja'}}})
        payload = json.loads(captured[0].content)
        self.assertEqual(payload['ref_audio_path'], 'managed/ref.wav')
        self.assertEqual(payload['prompt_text'], '参考台词')
        self.assertEqual(payload['text_lang'], 'ja')

    async def test_missing_reference_is_asset_error(self):
        provider = GptSovitsProvider(config(**{
            'qq_only.voice.gpt_sovits.ref_audio_path': ''}), http_transport=None)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    async def test_optional_local_reference_check(self):
        provider = GptSovitsProvider(config(**{
            'qq_only.voice.gpt_sovits.ref_audio_path': 'Z:/missing/ref.wav',
            'qq_only.voice.gpt_sovits.check_ref_local': True}), http_transport=None)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    async def test_language_is_validated(self):
        provider = GptSovitsProvider(config(**{
            'qq_only.voice.gpt_sovits.text_lang': '../zh'}), http_transport=None)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)


if __name__ == '__main__':
    unittest.main()
