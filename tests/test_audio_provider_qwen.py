"""T06 Qwen WebUI 适配器测试：三接口、Base64 数组、错误分类。"""
import base64
import json
import unittest
from types import SimpleNamespace

import httpx

from src.audio_contracts import AudioJobRequest, AudioMode, ContractError, ErrorCode
from src.audio_providers.qwen_webui import QwenWebUIProvider
from tests.audio_fixtures import make_wav, wav_duration


def job(text='你好'):
    return AudioJobRequest(client_request_id='req-1', mode=AudioMode.TTS, text=text)


def config(**values):
    settings = {'qq_only.voice.qwen.endpoint': 'http://127.0.0.1:7860',
                'qq_only.voice.qwen.model': 'Qwen3-TTS-12Hz-1.7B',
                'qq_only.voice.qwen.mode': 'custom_voice',
                'qq_only.voice.qwen.speaker': '中文女',
                'qq_only.voice.qwen.instruct': '平静地说话'}
    settings.update(values)
    return SimpleNamespace(get=settings.get)


def responder(*encoded):
    payload = {'audio_files_base64': list(encoded)}
    return lambda request: httpx.Response(200, content=json.dumps(payload).encode(),
                                          headers={'content-type': 'application/json'})


class QwenProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_voice_payload_and_decode(self):
        audio = make_wav(0.2)
        calls = []

        def handler(request):
            calls.append(request)
            return responder(base64.b64encode(audio).decode())(request)

        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(handler))
        result = await provider.synthesize(job(), {})
        self.assertEqual(result, audio)
        self.assertEqual(str(calls[0].url), 'http://127.0.0.1:7860/qwenapi/v1/custom-voice')
        payload = json.loads(calls[0].content)
        self.assertEqual(payload['model_name'], 'Qwen3-TTS-12Hz-1.7B')
        self.assertEqual(payload['speaker'], '中文女')
        self.assertFalse(payload['segment_gen'])

    async def test_multiple_segments_are_joined(self):
        first = make_wav(0.1, rate=24000)
        second = make_wav(0.1, rate=24000)
        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(
            responder(base64.b64encode(first).decode(), base64.b64encode(second).decode())))
        result = await provider.synthesize(job(), {})
        self.assertAlmostEqual(wav_duration(result), 0.2, delta=0.02)

    async def test_segment_parameter_mismatch_is_rejected(self):
        first = make_wav(0.1, rate=24000)
        second = make_wav(0.1, rate=16000)
        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(
            responder(base64.b64encode(first).decode(), base64.b64encode(second).decode())))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    async def test_empty_array_is_an_error(self):
        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(responder()))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    async def test_non_audio_base64_is_rejected(self):
        encoded = base64.b64encode(b'definitely not audio').decode()
        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(responder(encoded)))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    async def test_missing_model_raises_model_not_ready_without_request(self):
        calls = []
        provider = QwenWebUIProvider(
            config(**{'qq_only.voice.qwen.model': ''}),
            http_transport=httpx.MockTransport(lambda request: calls.append(request)))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.MODEL_NOT_READY)
        self.assertEqual(calls, [])

    async def test_404_maps_to_model_not_ready(self):
        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(
            lambda request: httpx.Response(404, content=b'model not found')))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.MODEL_NOT_READY)

    async def test_voice_design_mode_from_profile(self):
        audio = make_wav(0.1)
        calls = []

        def handler(request):
            calls.append(request)
            return responder(base64.b64encode(audio).decode())(request)

        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(handler))
        profile = {'provider_map': {'qwen_webui': {'mode': 'voice_design'}},
                   'language': 'zh'}
        await provider.synthesize(job(), profile)
        self.assertIn('/voice-design', str(calls[0].url))
        payload = json.loads(calls[0].content)
        self.assertEqual(payload['instruct'], '平静地说话')
        self.assertNotIn('speaker', payload)

    async def test_voice_clone_uses_reference_bytes(self):
        audio = make_wav(0.1)
        reference = make_wav(0.1, rate=16000)
        calls = []

        def handler(request):
            calls.append(request)
            return responder(base64.b64encode(audio).decode())(request)

        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(handler))
        await provider.synthesize(job(), {'provider_map': {'qwen_webui': {'mode': 'voice_clone'}},
                                          'ref_audio_bytes': reference})
        payload = json.loads(calls[0].content)
        self.assertEqual(base64.b64decode(payload['ref_audio_base64']), reference)

    async def test_profile_overrides_model_and_speaker(self):
        audio = make_wav(0.1)
        calls = []

        def handler(request):
            calls.append(request)
            return responder(base64.b64encode(audio).decode())(request)

        provider = QwenWebUIProvider(config(), http_transport=httpx.MockTransport(handler))
        await provider.synthesize(job(), {'provider_map': {'qwen_webui': {
            'mode': 'custom_voice', 'model': 'imported-model', 'speaker': '导入音色',
            'instruct': '温柔'}}})
        payload = json.loads(calls[0].content)
        self.assertEqual(payload['model_name'], 'imported-model')
        self.assertEqual(payload['speaker'], '导入音色')
        self.assertEqual(payload['instruct'], '温柔')

    async def test_remote_endpoint_is_refused(self):
        provider = QwenWebUIProvider(config(**{
            'qq_only.voice.qwen.endpoint': 'http://192.168.1.9:7860'}), http_transport=None)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)


if __name__ == '__main__':
    unittest.main()
