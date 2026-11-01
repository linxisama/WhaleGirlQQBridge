"""T08 自动语音插件测试：默认 explicit、群聊不自动、失败回文字。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.audio_contracts import ContractError, ErrorCode
from src.plugins.voice_reply import VoiceReply


class FakeManager:
    def __init__(self, settings):
        self._settings = {'voice_reply': dict(settings)}
        self.state_dir = Path(tempfile.mkdtemp())

    def get_settings(self, plugin_id):
        return dict(self._settings.get(plugin_id, {}))


class FakeGateway:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def enqueue_voice_reply(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return {'queued': True, 'job_id': 'job-1'}


def message(**over):
    base = {'id': 'm-1', 'kind': 'private', 'target': '123456', 'sender_id': 'owner-1',
            'text': '用语音读一下'}
    base.update(over)
    return base


class VoiceReplyTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **settings):
        values = {'enabled': True, 'mode': 'explicit', 'groupRequiresName': True,
                  'fallbackToText': True, 'maxChars': 120}
        values.update(settings)
        gateway = FakeGateway()
        self.gateway = gateway
        plugin = VoiceReply(FakeManager(values))
        self.runtime = SimpleNamespace(audio_gateway=gateway)
        return plugin

    async def test_explicit_requires_request(self):
        plugin = self.plugin()
        msg = message(text='随便聊聊')
        plugin.on_pre_ai(msg, [], self.runtime)
        reply = await plugin.on_post_reply(msg, '你好呀', self.runtime)
        self.assertEqual(reply, '你好呀')
        self.assertEqual(self.gateway.calls, [])

    async def test_requested_voice_is_enqueued_with_trusted_target(self):
        plugin = self.plugin()
        msg = message()
        plugin.on_pre_ai(msg, [], self.runtime)
        reply = await plugin.on_post_reply(msg, '你好呀', self.runtime)
        self.assertEqual(reply, '你好呀')
        self.assertEqual(len(self.gateway.calls), 1)
        call = self.gateway.calls[0]
        self.assertEqual(call['subject_domain'], 'private:123456')
        self.assertEqual(call['principal'], 'owner-1')
        self.assertEqual(call['text'], '你好呀')
        self.assertTrue(msg['_voice_enqueued'])

    async def test_group_without_name_is_not_read(self):
        plugin = self.plugin()
        msg = message(kind='group', text='大家好')
        plugin.on_pre_ai(msg, [], self.runtime)
        reply = await plugin.on_post_reply(msg, '大家好呀', self.runtime)
        self.assertEqual(reply, '大家好呀')
        self.assertEqual(self.gateway.calls, [])

    async def test_group_allowed_when_requirement_disabled(self):
        plugin = self.plugin(groupRequiresName=False, mode='both')
        msg = message(kind='group')
        plugin.on_pre_ai(msg, [], self.runtime)
        reply = await plugin.on_post_reply(msg, '你好呀', self.runtime)
        self.assertEqual(reply, '你好呀')
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_disabled_plugin_does_nothing(self):
        plugin = self.plugin(enabled=False)
        msg = message()
        plugin.on_pre_ai(msg, [], self.runtime)
        reply = await plugin.on_post_reply(msg, '你好呀', self.runtime)
        self.assertEqual(reply, '你好呀')
        self.assertEqual(self.gateway.calls, [])

    async def test_failure_adds_explanation_but_keeps_text(self):
        plugin = self.plugin()
        msg = message()
        self.gateway.fail = ContractError(ErrorCode.PROVIDER_UNAVAILABLE, '引擎不可用')
        plugin.on_pre_ai(msg, [], self.runtime)
        reply = await plugin.on_post_reply(msg, '你好呀', self.runtime)
        self.assertIn('你好呀', reply)
        self.assertIn('语音没有生成', reply)

    async def test_missing_target_never_uses_model_output(self):
        plugin = self.plugin()
        msg = message(target='')
        plugin.on_pre_ai(msg, [], self.runtime)
        reply = await plugin.on_post_reply(msg, '你好呀', self.runtime)
        self.assertEqual(reply, '你好呀')
        self.assertEqual(self.gateway.calls, [])

    async def test_no_gateway_keeps_text_only(self):
        plugin = self.plugin()
        msg = message()
        runtime = SimpleNamespace()
        plugin.on_pre_ai(msg, [], runtime)
        reply = await plugin.on_post_reply(msg, '你好呀', runtime)
        self.assertEqual(reply, '你好呀')


if __name__ == '__main__':
    unittest.main()
