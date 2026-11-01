"""T11 SVC 测试：目标模型边界、结果时长核对、混音母版。"""
import unittest
from types import SimpleNamespace

from src.audio_contracts import AudioJobRequest, AudioMode, ContractError, ErrorCode
from src.singing.local_svc import LocalSvcEngine
from src.singing.mix import MixOptions
from tests.audio_fixtures import make_wav, wav_duration

HASH = 'a' * 64


def job(**over):
    base = {'client_request_id': 'req-1', 'mode': AudioMode.VOICE_CONVERT,
            'source_audio': HASH, 'target_voice': 'rvc-whale-1'}
    base.update(over)
    return AudioJobRequest(**base)


class SvcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.vocals = make_wav(1.0, rate=24000, channels=1)
        self.engine = LocalSvcEngine(SimpleNamespace(get=lambda key, default=None: default),
                                     runner=lambda job, vocals, inst, model, options: vocals,
                                     target_model='rvc-whale-1')

    async def test_requires_source_and_target(self):
        with self.assertRaises(ContractError) as ctx:
            self.engine.requires(job(source_audio=''))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)
        with self.assertRaises(ContractError):
            self.engine.requires(job(target_voice=''))

    async def test_source_must_be_imported_asset_id(self):
        with self.assertRaises(ContractError) as ctx:
            self.engine.requires(job(source_audio='D:/music/vocal.wav'))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    async def test_tts_weights_and_paths_are_refused(self):
        engine = LocalSvcEngine(None, runner=lambda *a: b'', target_model='gpt_sovits_api_v2')
        with self.assertRaises(ContractError) as ctx:
            engine.requires(job())
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)
        engine = LocalSvcEngine(None, runner=lambda *a: b'', target_model='D:/models/x.pth')
        with self.assertRaises(ContractError) as ctx2:
            engine.requires(job())
        self.assertEqual(ctx2.exception.code, ErrorCode.INVALID_INPUT)
        engine = LocalSvcEngine(None, runner=lambda *a: b'',
                                target_model={'id': 'voice-1', 'kind': 'tts'})
        with self.assertRaises(ContractError) as ctx3:
            engine.requires(job())
        self.assertEqual(ctx3.exception.code, ErrorCode.POLICY_DENIED)

    async def test_convert_returns_master_with_same_duration(self):
        result = await self.engine.convert_with_meta(job(), self.vocals)
        self.assertAlmostEqual(result['duration_seconds'], 1.0, delta=0.05)
        self.assertFalse(result['verified'])
        self.assertEqual(result['model'], 'rvc-whale-1')

    async def test_duration_drift_is_rejected(self):
        short = make_wav(0.2, rate=24000)
        engine = LocalSvcEngine(None, runner=lambda *a: short,
                                target_model='rvc-whale-1')
        with self.assertRaises(ContractError) as ctx:
            await engine.convert(job(), self.vocals)
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSCODE_FAILED)

    async def test_convert_with_instrumental_mixes_master(self):
        instrumental = make_wav(1.0, rate=24000, channels=1, amplitude=3000)
        result = await self.engine.convert_with_meta(job(), self.vocals, instrumental)
        self.assertEqual(result['mix']['channels'], 2)
        self.assertAlmostEqual(result['mix']['duration_seconds'], 1.0, delta=0.05)
        self.assertAlmostEqual(wav_duration(result['audio']), 1.0, delta=0.05)

    async def test_mix_directly_returns_wav(self):
        instrumental = make_wav(1.0, rate=24000, channels=1)
        master = self.engine.mix(self.vocals, instrumental, MixOptions())
        self.assertEqual(master[:4], b'RIFF')

    async def test_invalid_source_audio_is_asset_error(self):
        with self.assertRaises(ContractError) as ctx:
            await self.engine.convert(job(), b'not-a-wave')
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)


if __name__ == '__main__':
    unittest.main()
