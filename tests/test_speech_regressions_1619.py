"""语音附件、协议契约与无转写时的完整回复链路。"""
import asyncio
import unittest
import contextlib
import io
from unittest.mock import AsyncMock

from src.qq_transports import official_event, onebot_message
from src.plugins.speech_to_text_qq import parse_budget
from tests import test_speech_to_text_qq as contract
from tests import test_core_pipeline_1618 as pipeline


class SpeechContractRegressions(unittest.IsolatedAsyncioTestCase):
    make = contract.TranscribeTests.make
    async def test_message_id_is_sufficient_and_strings_remain_exact(self):
        from tests.test_speech_to_text_qq import FakeOneBot, FakeRuntime
        for mid in ('native-id-123', '92233720368547758071', -123):
            with self.subTest(mid=mid):
                peer = FakeOneBot(result={'text': '带伞'})
                self.assertEqual(await self.make().transcribe(FakeRuntime(onebot=peer),
                                                          {'message_id': mid}), '带伞')
                self.assertEqual(peer.calls, [('fetch_ptt_text', {'message_id': mid})])

    async def test_failed_response_never_becomes_transcript(self):
        from tests.test_speech_to_text_qq import FakeOneBot, FakeRuntime
        for response in ({'status': 'failed', 'message': '内部错误堆栈'},
                         {'retcode': 1400, 'text': '内部错误详情'},
                         {'retcode': '1400', 'data': {'text': '错误'}}):
            self.assertIsNone(await self.make().transcribe(
                FakeRuntime(onebot=FakeOneBot(result=response)), {'file': 'f'}))

    async def test_invalid_budget_cannot_abort_transcription(self):
        for raw in ('nan', 'inf', float('nan'), float('inf'), 0.1):
            self.assertEqual(parse_budget(raw, 8000), 8000)

    async def test_debug_does_not_print_record_url_or_signature(self):
        peer = contract.FakeOneBot(result={'text': '你好'})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await self.make(debugLog=True).transcribe(contract.FakeRuntime(onebot=peer),
                {'file': 'https://example.test/private?secret=123'})
        self.assertNotIn('secret', output.getvalue())
        self.assertNotIn('private', output.getvalue())

    async def test_repeated_message_id_is_transcribed_once_and_marked_partial(self):
        peer = contract.FakeOneBot(result={'text': '你好'})
        message = {'id': 'x', 'text': '[语音][语音]',
                   'media': [{'type': 'record', 'file': 'a'}, {'type': 'record', 'file': 'b'}]}
        await self.make(enabled=True).on_prepare(message, contract.FakeRuntime(onebot=peer))
        self.assertEqual(len(peer.calls), 1)
        self.assertIn('部分', message['speech_status'])
        self.assertEqual(message['text'].count('你好'), 1)


class SpeechPipelineRegressions(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.MixedPluginPipelineTests.asyncSetUp
    asyncTearDown = pipeline.MixedPluginPipelineTests.asyncTearDown
    run_message = pipeline.MixedPluginPipelineTests.run_message
    def voice(self, mode, kind, mid):
        if mode == 'official':
            return official_event('GROUP_AT_MESSAGE_CREATE' if kind == 'group' else 'C2C_MESSAGE_CREATE', {
                'id': mid, 'group_openid': 'g1', 'author': {'user_openid': '30003', 'member_openid': '30003'},
                'content': '<@!BOT> ' if kind == 'group' else '',
                'attachments': [{'content_type': 'audio/silk', 'url': '//example.test/voice.silk'}]})
        return onebot_message({'post_type': 'message', 'message_type': kind, 'message_id': mid,
            'group_id': 20002, 'user_id': 30003,
            'message': ('[CQ:at,qq=10001]' if kind == 'group' else '') + '[CQ:record,file=voice.silk]'}, '10001')

    async def test_official_voice_attachment_reaches_worker_without_fabrication(self):
        self.bot.running = True; self.worker = asyncio.create_task(self.bot._work())
        for kind in ('group', 'private'):
            message = self.voice('official', kind, 'official-' + kind)
            self.assertEqual(message['media'][0]['type'], 'record')
            _, text, _ = await self.run_message(message)
            self.assertIn('语音', text)
            self.assertNotIn('完整回答', text)
        self.bot.ai.chat.assert_not_awaited()

    async def test_disabled_or_failed_bridge_voice_never_generates_guess(self):
        self.bot.config.set('qq_only.mode', 'onebot'); self.bot.running = True
        self.worker = asyncio.create_task(self.bot._work())
        for index, enabled in enumerate((False, True)):
            self.bot.plugins._settings['speech_to_text_qq']['enabled'] = enabled
            self.bot.onebot.call = AsyncMock(side_effect=RuntimeError('secret traceback'))
            for kind in ('group', 'private'):
                _, text, _ = await self.run_message(self.voice('onebot', kind, str(index) + kind))
                self.assertIn('语音', text)
                self.assertNotIn('secret', text)
                self.assertNotIn('完整回答', text)
        self.bot.ai.chat.assert_not_awaited()

    async def test_string_message_id_transcript_reaches_model(self):
        self.bot.config.set('qq_only.mode', 'onebot'); self.bot.running = True
        self.bot.plugins._settings['speech_to_text_qq']['enabled'] = True
        self.bot.onebot.call = AsyncMock(return_value={'text': '明天记得带伞'})
        self.worker = asyncio.create_task(self.bot._work())
        _, text, _ = await self.run_message(self.voice('onebot', 'private', 'opaque-id'))
        self.assertIn('完整回答', text)
        self.assertIn('明天记得带伞', str(self.conversations[-1]))
        self.assertEqual(self.bot.onebot.call.call_args.args, ('fetch_ptt_text', {'message_id': 'opaque-id'}))

    async def test_official_native_transcript_is_not_discarded_when_plugin_disabled(self):
        self.bot.running = True; self.worker = asyncio.create_task(self.bot._work())
        self.bot.onebot.call = AsyncMock()
        message = official_event('C2C_MESSAGE_CREATE', {'id': 'native-asr',
            'author': {'user_openid': '30003'}, 'content': '',
            'attachments': [{'content_type': 'audio/silk', 'url': '//example.test/a.silk',
                             'asr_refer_text': '明天记得带伞'}]})
        _, text, _ = await self.run_message(message)
        self.assertIn('完整回答', text)
        self.assertIn('明天记得带伞', str(self.conversations[-1]))
        self.bot.onebot.call.assert_not_awaited()

    async def test_unread_speech_with_text_still_answers_written_question(self):
        self.bot.config.set('qq_only.mode', 'onebot'); self.bot.running = True
        self.worker = asyncio.create_task(self.bot._work())
        message = self.voice('onebot', 'private', 'mixed-voice')
        message['text'] += '\n明天的天气呢？'
        _, text, _ = await self.run_message(message)
        self.assertIn('完整回答', text)
        prompt = self.bot.ai.chat.call_args.kwargs['system_prompt']
        self.assertIn('不能猜测语音内容', prompt)


class OfficialSpeechAttachmentTests(unittest.TestCase):
    def test_audio_dedup_and_no_image_misclassification(self):
        data = {'id': 'x', 'author': {'user_openid': 'u'}, 'content': [
            {'type': 'voice', 'data': {'url': 'https://example.test/voice.silk'}}],
            'attachments': [{'content_type': 'audio/silk', 'url': '//example.test/voice.silk'},
                            {'content_type': 'application/pdf', 'url': '//example.test/p.pdf'}]}
        message = official_event('C2C_MESSAGE_CREATE', data)
        self.assertEqual(message['text'], '[语音]')
        self.assertEqual([m['type'] for m in message['media']], ['record'])

    def test_duplicate_voice_attachment_can_supply_missing_transcript(self):
        data = {'id': 'x', 'author': {'user_openid': 'u'}, 'content': [
            {'type': 'voice', 'data': {'url': 'https://example.test/a'}}],
            'attachments': [{'content_type': 'audio/silk', 'url': '//example.test/a',
                             'asr_refer_text': '官方转写'}]}
        message = official_event('C2C_MESSAGE_CREATE', data)
        self.assertEqual(len(message['media']), 1)
        self.assertEqual(message['speech_status'], '已转写')
        self.assertEqual(message['text'].count('官方转写'), 1)

    def test_partial_and_invalid_native_transcripts_are_honest(self):
        data = {'id': 'x', 'author': {'user_openid': 'u'}, 'content': '', 'attachments': [
            {'content_type': 'audio/silk', 'url': '//example.test/a', 'asr_refer_text': '已识别'},
            {'content_type': 'audio/silk', 'url': '//example.test/b', 'asr_refer_text': {'text': '不能串化'}}]}
        message = official_event('C2C_MESSAGE_CREATE', data)
        self.assertIn('部分', message['speech_status'])
        self.assertNotIn('不能串化', message['text'])
