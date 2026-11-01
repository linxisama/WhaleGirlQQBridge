"""T03 资产与后处理的单元测试（不依赖 FFmpeg、网络或真实语音引擎）。"""
import io
import sqlite3
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from src.audio_contracts import ContractError, ErrorCode
from src.audio_assets import AudioAssetStore
from src.audio_postprocess import Transcoder, convert_wav, read_wav


def make_wav(seconds=0.2, rate=24000, channels=1, freq=440.0) -> bytes:
    frames = int(rate * seconds)
    samples = []
    for i in range(frames):
        value = int(8000 * (i * freq / rate % 1.0 * 2 - 1))
        samples.extend([value] * channels)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack('<%dh' % len(samples), *samples))
    return buffer.getvalue()


class PostprocessTests(unittest.TestCase):
    def test_read_wav_reports_real_parameters(self):
        info = read_wav(make_wav(0.5, rate=16000, channels=2))
        self.assertEqual(info['sample_rate'], 16000)
        self.assertEqual(info['channels'], 2)
        self.assertAlmostEqual(info['duration'], 0.5, places=2)

    def test_broken_wav_is_rejected_with_code(self):
        with self.assertRaises(ContractError) as ctx:
            read_wav(b'RIFF-not-really-a-wave')
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    def test_empty_and_oversize_are_rejected(self):
        with self.assertRaises(ContractError):
            read_wav(b'')
        with self.assertRaises(ContractError):
            read_wav(b'\0' * (8 * 1024 * 1024 + 1))

    def test_native_conversion_downmixes_and_resamples(self):
        source = make_wav(0.4, rate=48000, channels=2)
        converted = convert_wav(source, sample_rate=24000, channels=1)
        info = read_wav(converted)
        self.assertEqual(info['sample_rate'], 24000)
        self.assertEqual(info['channels'], 1)
        self.assertAlmostEqual(info['duration'], 0.4, delta=0.01)

    def test_transcoder_builds_argument_array_not_shell_string(self):
        commands = []

        def runner(command, timeout):
            commands.append((command, timeout))
            source = command[command.index('-i') + 1]
            Path(command[-1]).write_bytes(Path(source).read_bytes())

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'in.wav'
            dst = Path(directory) / 'out.wav'
            src.write_bytes(make_wav())
            Transcoder(ffmpeg='ffmpeg.exe', timeout=12, runner=runner).transcode(
                src, dst, sample_rate=24000, channels=1)
            self.assertTrue(dst.is_file())
        command, timeout = commands[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], 'ffmpeg.exe')
        self.assertIn('-ar', command)
        self.assertEqual(command[command.index('-ar') + 1], '24000')
        self.assertEqual(timeout, 12)
        self.assertNotIn(';', ' '.join(command))

    def test_transcode_timeout_is_classified(self):
        import subprocess

        def runner(command, timeout):
            raise subprocess.TimeoutExpired(command, timeout)

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'in.wav'
            src.write_bytes(make_wav())
            with self.assertRaises(ContractError) as ctx:
                Transcoder(ffmpeg='ffmpeg.exe', timeout=1, runner=runner).transcode(
                    src, Path(directory) / 'out.wav')
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSCODE_FAILED)

    def test_missing_ffmpeg_falls_back_for_wav_but_not_other_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'in.wav'
            src.write_bytes(make_wav(0.2, rate=48000, channels=2))
            dst = Path(directory) / 'out.wav'
            Transcoder(ffmpeg='definitely-not-ffmpeg-xyz').transcode(
                src, dst, sample_rate=24000, channels=1)
            self.assertEqual(read_wav(dst.read_bytes())['channels'], 1)
            with self.assertRaises(ContractError) as ctx:
                Transcoder(ffmpeg='definitely-not-ffmpeg-xyz').transcode(
                    src, Path(directory) / 'out.mp3', media_type='mp3')
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSCODE_FAILED)

    def test_transcode_never_overwrites_master(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'in.wav'
            src.write_bytes(make_wav())
            with self.assertRaises(ContractError):
                Transcoder().transcode(src, src)

    def test_probe_wav_and_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'a.wav'
            path.write_bytes(make_wav(0.3))
            probed = Transcoder().probe(path)
            self.assertEqual(probed['channels'], 1)
            self.assertAlmostEqual(probed['duration'], 0.3, places=1)
            broken = Path(directory) / 'b.wav'
            broken.write_bytes(b'not a wave')
            with self.assertRaises(ContractError):
                Transcoder().probe(broken)


class AssetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(':memory:')
        self.store = AudioAssetStore(self.root, self.db)
        self.audio = make_wav(0.3)
        self.hash = __import__('hashlib').sha256(self.audio).hexdigest()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_store_and_read_for_owner(self):
        result = self.store.store(self.audio, owner_domain='private:1')
        self.assertEqual(result['audio_id'], self.hash)
        self.assertFalse(result['deduped'])
        path = self.store.path(self.hash, owner_domain='private:1')
        self.assertTrue(path.is_file())
        self.assertEqual(self.store.meta(self.hash, owner_domain='private:1')['channels'], 1)

    def test_same_content_deduped_but_owner_isolated(self):
        self.store.store(self.audio, owner_domain='private:1')
        again = self.store.store(self.audio, owner_domain='private:1')
        self.assertTrue(again['deduped'])
        self.store.store(self.audio, owner_domain='group:99')
        with self.assertRaises(ContractError) as ctx:
            self.store.path(self.hash, owner_domain='private:2')
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_rejects_bad_input(self):
        with self.assertRaises(ContractError):
            self.store.store(b'fake-wav', owner_domain='private:1')
        with self.assertRaises(ContractError):
            self.store.store(b'', owner_domain='private:1')
        with self.assertRaises(ContractError):
            self.store.store(self.audio, owner_domain='')
        with self.assertRaises(ContractError):
            self.store.store(self.audio, owner_domain='private:1', master_of='..\\evil')

    def test_path_rejects_traversal_and_tampering(self):
        self.store.store(self.audio, owner_domain='private:1')
        with self.assertRaises(ContractError):
            self.store.path('../../etc/passwd', owner_domain='private:1')
        path = self.store.path(self.hash, owner_domain='private:1')
        path.write_bytes(make_wav(0.2))
        with self.assertRaises(ContractError) as ctx:
            self.store.path(self.hash, owner_domain='private:1')
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)

    def test_derived_keeps_master_and_links_parent(self):
        master_audio = make_wav(0.3, rate=48000, channels=2)
        master = self.store.store(master_audio, owner_domain='private:1', retained=True)
        derived = self.store.derived(master['audio_id'], owner_domain='private:1',
                                     spec={'sample_rate': 24000, 'channels': 1})
        self.assertNotEqual(derived['audio_id'], master['audio_id'])
        self.assertEqual(derived['master_of'], master['audio_id'])
        self.assertTrue(self.store.path(master['audio_id'], owner_domain='private:1').is_file())
        self.assertTrue(self.store.path(derived['audio_id'], owner_domain='private:1').is_file())

    def test_cleanup_removes_only_expired_unreferenced(self):
        expired = self.store.store(self.audio, owner_domain='private:1',
                                   retain_seconds=5, now=1000.0)
        self.assertEqual(self.store.cleanup(now=1002.0), 0)
        self.assertEqual(self.store.cleanup(now=1006.0), 1)
        self.assertFalse((self.store.directory / (expired['audio_id'] + '.wav')).exists())

    def test_cleanup_keeps_job_referenced_asset(self):
        result = self.store.store(self.audio, owner_domain='private:1',
                                  source_job_id='job-1', retain_seconds=5, now=1000.0)
        self.db.execute(
            'INSERT INTO audio_jobs (job_id,client_request_id,subject_domain,principal,mode,'
            'param_hash,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
            ('job-1', 'req-1', 'private:1', 'p', 'tts', 'h', 'ready', 1.0, 1.0))
        self.db.commit()
        self.assertEqual(self.store.cleanup(now=2000.0), 0)
        self.assertTrue(self.store.path(result['audio_id'], owner_domain='private:1').is_file())

    def test_cleanup_keeps_delivery_referenced_asset(self):
        result = self.store.store(self.audio, owner_domain='private:1',
                                  retain_seconds=5, now=1000.0)
        self.db.execute(
            'INSERT INTO audio_deliveries (delivery_id,job_id,audio_asset_ids,account_id,kind,'
            'target,segment_index,request_id,state) VALUES (?,?,?,?,?,?,?,?,?)',
            ('d1', 'j1', '["%s"]' % result['audio_id'], '1', 'private', '2', 0, 'r1', 'pending'))
        self.db.commit()
        self.assertEqual(self.store.cleanup(now=2000.0), 0)

    def test_retained_asset_is_never_cleaned(self):
        result = self.store.store(self.audio, owner_domain='private:1',
                                  retained=True, retain_seconds=1, now=1000.0)
        self.assertEqual(self.store.cleanup(now=99999.0), 0)
        self.assertTrue(self.store.path(result['audio_id'], owner_domain='private:1').is_file())

    def test_cleanup_expires_one_owner_without_deleting_shared_content(self):
        expired = self.store.store(self.audio, owner_domain='private:1',
                                   retain_seconds=5, now=1000.0)
        self.store.store(self.audio, owner_domain='private:2', retained=True, now=1000.0)
        self.assertEqual(self.store.cleanup(now=2000.0), 0)
        refs = self.db.execute(
            'SELECT owner_domain FROM audio_asset_refs WHERE audio_id=?',
            (expired['audio_id'],)).fetchall()
        self.assertEqual([row[0] for row in refs], ['private:2'])
        self.assertTrue(self.store.path(expired['audio_id'],
                                        owner_domain='private:2').is_file())


if __name__ == '__main__':
    unittest.main()
