"""T08 遗留：语音按字数上限切段时不能静默截断。

原来的实现是 `'，'.join(segments)[:limit]` —— 直接按字符切，可能把一句话切一半念出去，
而且回复文字里没有任何说明。这里盯两件事：切点落在句末、且被截短必须告知用户。
"""
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.audio_reply_policy import fit_segments
from src.plugins.voice_reply import VoiceReply


class FitSegmentsTests(unittest.TestCase):
    def test_whole_sentences_are_kept_when_they_fit(self):
        text, truncated = fit_segments(['第一句', '第二句'], 20)
        self.assertEqual(text, '第一句，第二句')
        self.assertFalse(truncated)

    def test_result_never_exceeds_the_limit(self):
        segments = ['这是一句比较长的话用来占位置', '第二句也不短啊真的不短', '第三句同样有内容']
        for limit in (1, 5, 12, 25, 40):
            text, _ = fit_segments(segments, limit)
            self.assertLessEqual(len(text), limit, f'limit={limit} 时超了：{text!r}')

    def test_overflowing_segment_is_cut_at_a_sentence_end(self):
        text, truncated = fit_segments(['前半句。后半句还在继续写下去'], 6)
        self.assertTrue(truncated)
        self.assertTrue(text.endswith('。'), text)
        self.assertNotIn('后半句', text)

    def test_segment_without_any_sentence_end_is_dropped_whole(self):
        text, truncated = fit_segments(['没有任何标点的一长串内容继续继续'], 5)
        self.assertTrue(truncated)
        self.assertEqual(text, '')

    def test_zero_limit_reports_truncation_without_text(self):
        self.assertEqual(fit_segments(['有话'], 0), ('', True))
        self.assertEqual(fit_segments([], 0), ('', False))

    def test_empty_segments_are_ignored(self):
        self.assertEqual(fit_segments(['', '  ', '好'], 10), ('好', False))


class _Manager:
    def __init__(self, settings):
        self._settings = {'voice_reply': dict(settings)}
        self.state_dir = Path('.')

    def get_settings(self, plugin_id):
        return dict(self._settings.get(plugin_id, {}))


class _Gateway:
    def __init__(self):
        self.calls = []

    async def enqueue_voice_reply(self, **kwargs):
        self.calls.append(kwargs)
        return {'queued': True}


class VoiceTruncationNoticeTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **settings):
        values = {'enabled': True, 'mode': 'explicit', 'groupRequiresName': True,
                  'fallbackToText': True, 'maxChars': 20}
        values.update(settings)
        gateway = _Gateway()
        self.gateway = gateway
        plugin = VoiceReply(_Manager(values))
        self.runtime = SimpleNamespace(audio_gateway=gateway)
        return plugin

    def message(self):
        return {'id': 'm-1', 'kind': 'private', 'target': '123456',
                'sender_id': 'owner-1', 'text': '用语音读一下'}

    async def test_truncated_speech_is_announced_in_the_text_reply(self):
        plugin = self.plugin()
        msg = self.message()
        plugin.on_pre_ai(msg, [], self.runtime)
        long_reply = '第一句话有点长\n第二句话也挺长的\n第三句同样不短'
        reply = await plugin.on_post_reply(msg, long_reply, self.runtime)
        self.assertEqual(len(self.gateway.calls), 1)
        spoken = self.gateway.calls[0]['text']
        self.assertLessEqual(len(spoken), 20)
        self.assertIn('语音按单段上限', reply)
        self.assertIn('完整内容见上', reply)
        self.assertIn(long_reply, reply)

    async def test_short_reply_gets_no_truncation_notice(self):
        plugin = self.plugin(maxChars=120)
        msg = self.message()
        plugin.on_pre_ai(msg, [], self.runtime)
        reply = await plugin.on_post_reply(msg, '你好呀', self.runtime)
        self.assertEqual(reply, '你好呀')
        self.assertNotIn('语音按单段上限', reply)

    async def test_spoken_text_ends_at_a_sentence_boundary(self):
        plugin = self.plugin(maxChars=12)
        msg = self.message()
        plugin.on_pre_ai(msg, [], self.runtime)
        await plugin.on_post_reply(msg, '第一句话。第二句话。第三句话。', self.runtime)
        spoken = self.gateway.calls[0]['text']
        self.assertTrue(spoken.endswith('。'), spoken)


if __name__ == '__main__':
    unittest.main()
