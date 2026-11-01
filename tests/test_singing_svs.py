"""T10 本地 SVS 测试：谱面/输入校验、渲染结果核对、拒绝参考音频冒充。"""
import json
import unittest
from types import SimpleNamespace

from src.audio_contracts import AudioJobRequest, AudioMode, ContractError, ErrorCode
from src.singing.inputs import inputs
from src.singing.local_svs import LocalSvsEngine
from tests.audio_fixtures import make_wav


def job(**over):
    base = {'client_request_id': 'req-1', 'mode': AudioMode.SING_SCORE,
            'lyrics': '啦啦啦', 'score': 'song.ds'}
    base.update(over)
    return AudioJobRequest(**base)


def config(**values):
    settings = {'qq_only.voice.svs.min_seconds': 15.0,
                'qq_only.voice.svs.max_seconds': 30.0}
    settings.update(values)
    return SimpleNamespace(get=settings.get)


class SvsTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_project_and_lyrics(self):
        engine = LocalSvsEngine(config(), runner=lambda job, sin: make_wav(16))
        with self.assertRaises(ContractError) as ctx:
            engine.requires(job(score=''))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)
        with self.assertRaises(ContractError):
            engine.requires(job(lyrics=''))
        with self.assertRaises(ContractError) as ctx2:
            engine.requires(job(score='song.xyz'))
        self.assertEqual(ctx2.exception.code, ErrorCode.INVALID_INPUT)

    async def test_missing_engine_is_capability_unsupported(self):
        engine = LocalSvsEngine(config(), runner=None)
        with self.assertRaises(ContractError) as ctx:
            engine.requires(job())
        self.assertEqual(ctx.exception.code, ErrorCode.CAPABILITY_UNSUPPORTED)

    async def test_inline_project_needs_note_lyric_mapping(self):
        bad = json.dumps({'notes': [{'pitch': 60, 'duration': 1}]})
        engine = LocalSvsEngine(config(), runner=lambda job, sin: make_wav(16))
        with self.assertRaises(ContractError) as ctx:
            engine.requires(job(score=bad))
        self.assertIn('歌词', str(ctx.exception))
        good = json.dumps({'notes': [{'pitch': 60, 'duration': 1, 'lyric': '啦'}]})
        engine.requires(job(score=good))

    async def test_render_returns_audio_and_metadata(self):
        audio = make_wav(16)
        engine = LocalSvsEngine(config(), runner=lambda job, sin: audio)
        result = await engine.render_with_meta(job(), inputs(lyrics='啦啦啦', score='song.ds'))
        self.assertEqual(result['audio'], audio)
        self.assertFalse(result['verified'])
        self.assertEqual(result['lyrics'], '啦啦啦')
        self.assertAlmostEqual(result['duration'], 16.0, places=1)

    async def test_render_outside_project_window_is_rejected(self):
        engine = LocalSvsEngine(config(), runner=lambda job, sin: make_wav(5))
        with self.assertRaises(ContractError) as ctx:
            await engine.render(job(), inputs(lyrics='啦', score='song.ds'))
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    async def test_reference_audio_identity_is_rejected(self):
        reference = make_wav(16)
        engine = LocalSvsEngine(config(), runner=lambda job, sin: reference,
                                reference_audio=reference)
        with self.assertRaises(ContractError) as ctx:
            await engine.render(job(), inputs(lyrics='啦', score='song.ds'))
        self.assertIn('参考音频', str(ctx.exception))

    async def test_invalid_wav_result_is_rejected(self):
        engine = LocalSvsEngine(config(), runner=lambda job, sin: b'not-a-wave')
        with self.assertRaises(ContractError) as ctx:
            await engine.render(job(), inputs(lyrics='啦', score='song.ds'))
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    async def test_async_runner_is_supported(self):
        async def runner(job_, sin):
            return make_wav(16)

        engine = LocalSvsEngine(config(), runner=runner)
        audio = await engine.render(job(), inputs(lyrics='啦', score='song.ds'))
        self.assertEqual(audio[:4], b'RIFF')


if __name__ == '__main__':
    unittest.main()
