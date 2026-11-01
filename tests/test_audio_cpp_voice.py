"""Real protocol shape and cache lifecycle; no external speech engine required."""
import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src.voice_service import VoiceService


class AudioCppVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(':memory:')
        self.settings = {'qq_only.voice.enabled': True, 'qq_only.voice.provider': 'audio_cpp'}
        self.service = VoiceService(self.temp.name, SimpleNamespace(get=self.settings.get), self.db)
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\x00\x00' * 1600)
        self.audio = buffer.getvalue()
        self.calls = []

        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=self.audio)

        self.service.http_transport = httpx.MockTransport(handler)

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def test_payload_and_concurrent_repeats_generate_once(self):
        results = await asyncio.gather(*(self.service.synthesize('你好') for _ in range(3)))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(str(self.calls[0].url), 'http://127.0.0.1:18100/v1/audio/speech')
        self.assertEqual(json.loads(self.calls[0].content), {
            'model': 'breeze-tts-2', 'input': '你好', 'voice': 'demo_3_woman', 'response_format': 'wav'})
        self.assertEqual([result['cached'] for result in results], [False, True, True])

    async def test_model_voice_and_text_changes_invalidate_cache(self):
        await self.service.synthesize('你好')
        self.settings['qq_only.voice.audio_cpp.voice'] = 'demo_2_man'
        await self.service.synthesize('你好')
        self.settings['qq_only.voice.audio_cpp.model'] = 'index-tts2-5'
        await self.service.synthesize('你好')
        await self.service.synthesize('晚上好')
        self.assertEqual(len(self.calls), 4)

    async def test_cache_expiry_eviction_and_disabled_option(self):
        with patch('src.voice_service.time.monotonic', return_value=1000):
            await self.service.synthesize('你好')
        with patch('src.voice_service.time.monotonic', return_value=1301):
            self.assertFalse((await self.service.synthesize('你好'))['cached'])
        for index in range(65):
            await self.service.synthesize(f'短句{index}')
        self.assertEqual(len(self.service._cache), 64)
        await self.service.synthesize('短句0')
        self.assertFalse((await self.service.synthesize('短句1'))['cached'])
        self.settings['qq_only.voice.audio_cpp.cache'] = False
        await self.service.synthesize('短句1')
        await self.service.synthesize('短句1')
        self.assertEqual(len(self.calls), 71)

    async def test_missing_or_tampered_file_is_not_reused(self):
        first = await self.service.synthesize('你好')
        self.service.path(first['audio_id']).unlink()
        self.assertFalse((await self.service.synthesize('你好'))['cached'])
        self.service.path(first['audio_id']).write_bytes(b'changed')
        self.assertFalse((await self.service.synthesize('你好'))['cached'])
        # Restoring identical content must also repair the corrupt managed file.
        self.assertEqual(self.service.path(first['audio_id']).read_bytes(), self.audio)

    async def test_invalid_endpoint_voice_and_provider_do_not_connect(self):
        for endpoint in ['https://example.com/v1/audio/speech', 'http://127.0.0.1:18100/tts',
                         'http://user:pass@127.0.0.1:18100/v1/audio/speech']:
            self.settings['qq_only.voice.audio_cpp.endpoint'] = endpoint
            with self.assertRaises(ValueError):
                await self.service.synthesize('你好')
        self.settings.pop('qq_only.voice.audio_cpp.endpoint')
        self.settings['qq_only.voice.audio_cpp.voice'] = 'D:/private.wav'
        with self.assertRaises(ValueError):
            await self.service.synthesize('你好')
        self.settings['qq_only.voice.provider'] = 'unrecognized'
        with self.assertRaises(ValueError):
            await self.service.synthesize('你好')
        self.assertEqual(self.calls, [])

    async def test_failures_are_not_cached_or_retried(self):
        def fail(request):
            self.calls.append(request)
            raise httpx.ReadTimeout('not ready')
        self.service.http_transport = httpx.MockTransport(fail)
        with self.assertRaisesRegex(RuntimeError, 'audio.cpp 合成失败或超时'):
            await self.service.synthesize('你好')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.service._cache)

    async def test_disable_during_queue_wait_blocks_cache_and_generation(self):
        await self.service.synthesize('你好')
        async with self.service._synthesis_lock:
            pending = asyncio.create_task(self.service.synthesize('你好'))
            await asyncio.sleep(0)
            self.settings['qq_only.voice.enabled'] = False
        with self.assertRaisesRegex(ValueError, '尚未启用'):
            await pending
        self.assertEqual(len(self.calls), 1)

    async def test_settings_changed_while_queued_require_new_request(self):
        async with self.service._synthesis_lock:
            pending = asyncio.create_task(self.service.synthesize('你好'))
            await asyncio.sleep(0)
            self.settings['qq_only.voice.audio_cpp.voice'] = 'demo_1_man'
        with self.assertRaisesRegex(ValueError, '设置已变化'):
            await pending
        self.assertEqual(self.calls, [])

    async def test_gpt_sovits_keeps_legacy_request_and_sampling_behavior(self):
        self.settings.pop('qq_only.voice.provider')
        self.settings['qq_only.voice.gpt_sovits.ref_audio_path'] = 'reference.wav'
        for _ in range(2):
            result = await self.service.synthesize('你好')
            self.assertNotIn('cached', result)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(str(self.calls[0].url), 'http://127.0.0.1:9880/tts')
        self.assertEqual(json.loads(self.calls[0].content)['ref_audio_path'], 'reference.wav')


if __name__ == '__main__':
    unittest.main()
