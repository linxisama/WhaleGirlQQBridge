"""T07 云 TTS 测试：出网闸、URL/Base64/PCM 分流、预算与未知结果。"""
import base64
import json
import unittest
from types import SimpleNamespace

import httpx

from src.audio_billing import AudioBilling
from src.audio_contracts import AudioJobRequest, AudioMode, ContractError, ErrorCode
from src.audio_providers.cloud_tts import CloudTtsProvider
from tests.audio_fixtures import make_wav


def job(text='你好', request_id='req-1'):
    return AudioJobRequest(client_request_id=request_id, mode=AudioMode.TTS, text=text)


def config(**values):
    settings = {'qq_only.voice.cloud.allow_outbound': True,
                'qq_only.voice.cloud.model': 'qwen3-tts-flash',
                'qq_only.voice.cloud.voice': 'Cherry'}
    settings.update(values)
    return SimpleNamespace(get=settings.get)


class CloudTtsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.audio = make_wav(0.2)
        self.billing = AudioBilling(daily_limit=10.0, monthly_limit=100.0,
                                    per_request_limit=1.0, price_per_1k_chars=0.01)
        self.calls = []

    def provider(self, handler, **values):
        self.transport = httpx.MockTransport(handler)
        return CloudTtsProvider(config(**values), credentials=SimpleNamespace(api_key='secret'),
                                billing=self.billing, http_transport=self.transport)

    async def test_not_allowed_outbound_means_zero_requests(self):
        provider = self.provider(lambda request: self.calls.append(request),
                                 **{'qq_only.voice.cloud.allow_outbound': False})
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.CAPABILITY_UNSUPPORTED)
        self.assertEqual(self.calls, [])

    async def test_url_response_is_downloaded_and_billed_once(self):
        def handler(request):
            self.calls.append(request)
            if request.method == 'POST':
                return httpx.Response(200, content=json.dumps({
                    'output': {'audio': {'url': 'https://dashscope.aliyuncs.com/tmp/a.wav'}}}
                ).encode())
            return httpx.Response(200, content=self.audio)

        provider = self.provider(handler)
        result = await provider.synthesize(job(), {})
        self.assertEqual(result, self.audio)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0].headers['authorization'], 'Bearer secret')
        payload = json.loads(self.calls[0].content)
        self.assertEqual(payload['input'], {'text': '你好', 'voice': 'Cherry'})
        self.assertEqual(self.billing.state_for('req-1'), 'settled')

    async def test_base64_and_pcm_responses(self):
        def base64_handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=json.dumps({
                'output': {'audio': {'data': base64.b64encode(self.audio).decode()}}}).encode())

        provider = self.provider(base64_handler)
        self.assertEqual(await provider.synthesize(job(request_id='a'), {}), self.audio)

        pcm = b'\x00\x01' * 4800
        def pcm_handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=json.dumps({
                'output': {'audio': {'data': base64.b64encode(pcm).decode(),
                                     'format': 'pcm'}}}).encode())

        provider = self.provider(pcm_handler)
        result = await provider.synthesize(job(request_id='b'), {})
        self.assertEqual(result[:4], b'RIFF')

    async def test_401_is_unauthorized_and_releases_budget(self):
        provider = self.provider(lambda request: httpx.Response(401, content=b'no key'))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)
        self.assertAlmostEqual(self.billing.snapshot().reserved_today, 0.0)

    async def test_429_is_budget_error(self):
        provider = self.provider(lambda request: httpx.Response(429, content=b'slow down'))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.BUDGET_EXCEEDED)
        self.assertAlmostEqual(self.billing.snapshot().reserved_today, 0.0)

    async def test_5xx_keeps_reservation_for_possible_charge(self):
        provider = self.provider(lambda request: httpx.Response(503, content=b'busy'))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        self.assertEqual(self.billing.state_for('req-1'), 'unknown')
        self.assertGreater(self.billing.snapshot().unknown_today, 0)

    async def test_timeout_is_unknown_and_is_not_retried(self):
        def handler(request):
            self.calls.append(request)
            raise httpx.ReadTimeout('timed out')

        provider = self.provider(handler)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.GENERATION_UNKNOWN)
        before = len(self.calls)
        with self.assertRaises(ContractError) as ctx2:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx2.exception.code, ErrorCode.GENERATION_UNKNOWN)
        self.assertEqual(len(self.calls), before)

    async def test_redirect_download_is_refused(self):
        def handler(request):
            self.calls.append(request)
            if request.method == 'POST':
                return httpx.Response(200, content=json.dumps({
                    'output': {'audio': {'url': 'https://dashscope.aliyuncs.com/tmp/a.wav'}}}).encode())
            return httpx.Response(302, headers={'location': 'https://evil.example/x.wav'})

        provider = self.provider(handler)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    async def test_expired_url_is_not_regenerated(self):
        def handler(request):
            self.calls.append(request)
            if request.method == 'POST':
                return httpx.Response(200, content=json.dumps({
                    'output': {'audio': {'url': 'https://dashscope.aliyuncs.com/tmp/a.wav'}}}).encode())
            return httpx.Response(403, content=b'expired')

        provider = self.provider(handler)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)
        self.assertEqual(sum(1 for call in self.calls if call.method == 'POST'), 1)

    async def test_download_host_outside_allowlist_is_refused(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=json.dumps({
                'output': {'audio': {'url': 'https://evil.example/a.wav'}}}).encode())

        provider = self.provider(handler)
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)
        self.assertEqual(len(self.calls), 1)

    async def test_budget_exhausted_means_zero_requests(self):
        self.billing = AudioBilling(daily_limit=0.0, monthly_limit=0.0,
                                    per_request_limit=1.0, price_per_1k_chars=0.01)
        provider = self.provider(lambda request: self.calls.append(request))
        with self.assertRaises(ContractError) as ctx:
            await provider.synthesize(job(), {})
        self.assertEqual(ctx.exception.code, ErrorCode.BUDGET_EXCEEDED)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
