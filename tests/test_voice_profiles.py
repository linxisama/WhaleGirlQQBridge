"""音色库测试：多参考音频/标签表导入、复制到数据目录、删除与校验。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.audio_contracts import ContractError, ErrorCode
from src.voice_profiles import VoiceProfileStore
from tests.audio_fixtures import make_wav


class VoiceProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'samples'
        self.source.mkdir()
        self.db = sqlite3.connect(':memory:')
        self.store = VoiceProfileStore(self.root, self.db)
        self.alice = self.source / 'Alice.wav'
        self.bob = self.source / 'Bob.flac'
        self.alice.write_bytes(make_wav(0.2, rate=16000))
        self.bob.write_bytes(b'fLaC' + b'\0' * 200)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_import_multiple_audio_creates_profiles_and_copies_files(self):
        imported = self.store.import_audio([str(self.alice), str(self.bob)])
        self.assertEqual(len(imported), 2)
        ids = {item['profile_id'] for item in imported}
        self.assertEqual(ids, {'alice', 'bob'})
        mapping = imported[0]['provider_map']
        self.assertEqual(set(mapping), {'audio_cpp', 'gpt_sovits_api_v2', 'qwen_webui'})
        ref = Path(mapping['gpt_sovits_api_v2']['ref_audio_path'])
        self.assertTrue(ref.is_file())
        self.assertTrue(str(ref).startswith(str(self.root / 'data' / 'plugin_uploads')))
        self.assertEqual(mapping['qwen_webui']['mode'], 'voice_clone')
        # 删除原文件后仍可用（复制到运行数据目录）。
        self.alice.unlink()
        self.assertTrue(ref.is_file())
        self.assertIsNotNone(self.store.get('alice'))

    def test_import_table_with_relative_reference(self):
        ref = self.source / 'ref.wav'
        ref.write_bytes(make_wav(0.2))
        table = self.source / 'voices.csv'
        table.write_text(
            'id,name,engine,voice,ref_audio,language,licensed\n'
            'whale,鲸鱼娘,audio_cpp+gpt_sovits_api_v2,demo_3_woman,ref.wav,zh,true\n',
            encoding='utf-8')
        imported = self.store.import_table(str(table))
        self.assertEqual(len(imported), 1)
        profile = imported[0]
        self.assertEqual(profile['profile_id'], 'whale')
        self.assertEqual(profile['language'], 'zh')
        self.assertTrue(profile['licensed'])
        self.assertEqual(profile['provider_map']['audio_cpp']['voice'], 'demo_3_woman')
        self.assertTrue(Path(profile['provider_map']['gpt_sovits_api_v2']['ref_audio_path']).is_file())

    def test_import_json_table(self):
        table = self.source / 'voices.json'
        table.write_text(json.dumps({'voices': [
            {'name': '测试音色', 'engine': 'qwen_webui', 'speaker': '女声',
             'model': 'qwen-tts'}]}), encoding='utf-8')
        imported = self.store.import_table(str(table))
        self.assertEqual(imported[0]['profile_id'], 'voice-' + __import__('hashlib').sha256(
            '测试音色'.encode()).hexdigest()[:12])
        mapping = imported[0]['provider_map']['qwen_webui']
        self.assertEqual(mapping['mode'], 'custom_voice')
        self.assertEqual(mapping['speaker'], '女声')

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ContractError):
            self.store.import_audio([])
        bad = self.source / 'x.txt'
        bad.write_text('x')
        with self.assertRaises(ContractError) as ctx:
            self.store.import_audio([str(bad)])
        self.assertEqual(ctx.exception.code, ErrorCode.ASSET_INVALID)
        big = self.source / 'big.wav'
        big.write_bytes(b'\0' * (8 * 1024 * 1024 + 1))
        with self.assertRaises(ContractError):
            self.store.import_audio([str(big)])
        table = self.source / 'bad.csv'
        table.write_text('id,name,ref_audio\nx,坏,missing.wav\n', encoding='utf-8')
        with self.assertRaises(ContractError):
            self.store.import_table(str(table))
        with self.assertRaises(ContractError):
            self.store.import_table(str(self.source / 'missing.csv'))

    def test_delete_removes_profile_and_managed_files(self):
        imported = self.store.import_audio([str(self.alice)])[0]
        directory = self.root / 'data' / 'plugin_uploads' / 'voice_profiles' / imported['profile_id']
        self.assertTrue(directory.is_dir())
        self.assertTrue(self.store.delete(imported['profile_id']))
        self.assertIsNone(self.store.get(imported['profile_id']))
        self.assertFalse(directory.exists())
        self.assertFalse(self.store.delete(imported['profile_id']))

    def test_same_profile_id_updates_in_place(self):
        self.store.import_audio([str(self.alice)])
        self.alice.write_bytes(make_wav(0.3, rate=16000))
        imported = self.store.import_audio([str(self.alice)])
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(imported[0]['profile_id'], 'alice')


if __name__ == '__main__':
    unittest.main()
