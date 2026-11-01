"""T11 混音测试：声道/采样率/漂移/增益/削波口径。"""
import unittest

from src.audio_contracts import ContractError, ErrorCode
from src.singing.mix import MixOptions, mix_pcm, silence_wav, derived_spec
from tests.audio_fixtures import make_wav, wav_duration


class MixTests(unittest.TestCase):
    def test_same_length_mono_is_upmixed_to_stereo_master(self):
        vocals = make_wav(1.0, rate=24000, channels=1)
        instrumental = make_wav(1.0, rate=24000, channels=1, amplitude=3000)
        master, stats = mix_pcm(vocals, instrumental, MixOptions())
        self.assertEqual(stats['channels'], 2)
        self.assertAlmostEqual(stats['duration_seconds'], 1.0, delta=0.02)
        self.assertEqual(master[:4], b'RIFF')

    def test_sample_rate_mismatch_is_rejected(self):
        vocals = make_wav(1.0, rate=24000)
        instrumental = make_wav(1.0, rate=48000)
        with self.assertRaises(ContractError) as ctx:
            mix_pcm(vocals, instrumental, MixOptions())
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSCODE_FAILED)

    def test_drift_beyond_tolerance_is_rejected(self):
        vocals = make_wav(2.0, rate=24000)
        instrumental = make_wav(0.5, rate=24000)
        with self.assertRaises(ContractError) as ctx:
            mix_pcm(vocals, instrumental, MixOptions())
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSCODE_FAILED)

    def test_short_drift_is_padded_and_accepted(self):
        vocals = make_wav(1.0, rate=24000)
        instrumental = make_wav(0.9, rate=24000)
        _master, stats = mix_pcm(vocals, instrumental, MixOptions())
        self.assertAlmostEqual(stats['duration_seconds'], 1.0, delta=0.05)
        self.assertLessEqual(stats['drift_seconds'], 0.25)

    def test_peak_is_limited_and_reported(self):
        vocals = make_wav(0.5, rate=24000, amplitude=32000)
        instrumental = make_wav(0.5, rate=24000, amplitude=32000)
        _master, stats = mix_pcm(vocals, instrumental,
                                 MixOptions(vocal_gain_db=0.0, instrumental_gain_db=0.0))
        self.assertGreater(stats['peak_before'], 32767)
        self.assertGreaterEqual(stats['clipped_samples'], 1)

    def test_unlimited_peak_is_rejected(self):
        vocals = make_wav(0.5, rate=24000, amplitude=32000)
        instrumental = make_wav(0.5, rate=24000, amplitude=32000)
        with self.assertRaises(ContractError) as ctx:
            mix_pcm(vocals, instrumental,
                    MixOptions(vocal_gain_db=0.0, instrumental_gain_db=0.0,
                               limit_peak=False))
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSCODE_FAILED)

    def test_excessive_clipping_ratio_is_rejected(self):
        vocals = make_wav(0.5, rate=24000, amplitude=32000)
        instrumental = make_wav(0.5, rate=24000, amplitude=32000)
        with self.assertRaises(ContractError) as ctx:
            mix_pcm(vocals, instrumental,
                    MixOptions(vocal_gain_db=0.0, instrumental_gain_db=0.0,
                               max_clipped_ratio=0.0))
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSCODE_FAILED)

    def test_derived_spec_is_qq_voice_baseline(self):
        spec = derived_spec(MixOptions())
        self.assertEqual(spec['sample_rate'], 24000)
        self.assertEqual(spec['channels'], 1)
        self.assertEqual(spec['media_type'], 'audio/wav')

    def test_silence_wav_duration(self):
        self.assertAlmostEqual(wav_duration(silence_wav(1.5)), 1.5, delta=0.01)


if __name__ == '__main__':
    unittest.main()
