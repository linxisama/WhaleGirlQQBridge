"""T17 电脑指令插件测试：命令解析、群/引用拒绝、主人私聊路由、提案提交。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.plugins.local_agent_control import (
    LocalAgentControl, looks_like_pc_command, parse_pc_command,
)


class FakeManager:
    def __init__(self, settings):
        self._settings = {'local_agent_control': dict(settings)}
        self.state_dir = Path(tempfile.mkdtemp())

    def get_settings(self, plugin_id):
        return dict(self._settings.get(plugin_id, {}))


class FakeBroker:
    def __init__(self):
        self.submitted = []

    def submit_private(self, proposal, **kwargs):
        self.submitted.append((proposal, kwargs))
        return 'awaiting_approval'


class FakeConsole:
    def __init__(self):
        self.broker = FakeBroker()

    def devices(self):
        return [{'device_id': 'dev-1', 'paired': True, 'policy_version': 1}]


def message(**over):
    base = {'id': 'm-1', 'kind': 'private', 'target': '123456', 'sender_id': 'owner-1',
            'account_id': '10001', 'text': '电脑 状态'}
    base.update(over)
    return base


class ParserTests(unittest.TestCase):
    def test_supported_commands(self):
        self.assertEqual(parse_pc_command('电脑 状态')['action'], 'agent.health')
        self.assertEqual(parse_pc_command('电脑能力')['action'], 'agent.capabilities')
        self.assertEqual(parse_pc_command('电脑 暂停')['action'], 'media.pause')
        self.assertEqual(parse_pc_command('电脑 打开 notepad')['args'], {'app_id': 'notepad'})
        self.assertEqual(parse_pc_command('电脑 音量 30')['args'], {'level': 30})
        self.assertIsNone(parse_pc_command('电脑 音量 300'))
        self.assertIsNone(parse_pc_command('电脑 删库跑路'))
        self.assertIsNone(parse_pc_command('今天天气不错'))
        self.assertFalse(looks_like_pc_command('今天天气不错'))

    def test_injected_text_is_not_a_command(self):
        self.assertIsNone(parse_pc_command('电脑 打开 notepad approved:true'))
        self.assertIsNone(parse_pc_command('电脑 状态; rm -rf /'))


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.console = FakeConsole()
        self.runtime = SimpleNamespace(local_agent_console=self.console)
        self.plugin = LocalAgentControl(FakeManager({'enabled': True, 'ownerIds': 'owner-1'}))

    def test_owner_private_command_becomes_proposal(self):
        reply = self.plugin.on_ingest(message(text='电脑 音量 25'), self.runtime)
        self.assertIn('等待本机控制台批准', reply)
        proposal, kwargs = self.console.broker.submitted[0]
        self.assertEqual(proposal.action, 'volume.set')
        self.assertEqual(proposal.args, {'level': 25})
        self.assertEqual(kwargs['caller'], 'owner-1')
        self.assertEqual(kwargs['qq_account'], '10001')
        self.assertEqual(kwargs['message_id'], 'm-1')

    def test_group_and_quote_are_refused_without_submit(self):
        group = self.plugin.on_ingest(message(kind='group'), self.runtime)
        self.assertIn('私聊', group)
        quoted = self.plugin.on_ingest(message(quote={'id': 'x'}), self.runtime)
        self.assertIn('引用', quoted)
        forwarded = self.plugin.on_ingest(message(forward=True), self.runtime)
        self.assertIn('转发', forwarded)
        self.assertEqual(self.console.broker.submitted, [])

    def test_non_owner_is_refused(self):
        reply = self.plugin.on_ingest(message(sender_id='stranger'), self.runtime)
        self.assertIn('主人', reply)
        self.assertEqual(self.console.broker.submitted, [])

    def test_unsupported_command_is_explained_not_executed(self):
        reply = self.plugin.on_ingest(message(text='电脑 删库'), self.runtime)
        self.assertIn('不支持', reply)
        self.assertEqual(self.console.broker.submitted, [])

    def test_no_console_gives_pairing_guidance(self):
        reply = self.plugin.on_ingest(message(text='电脑 状态'), SimpleNamespace())
        self.assertIn('配对', reply)

    def test_disabled_plugin_does_nothing(self):
        plugin = LocalAgentControl(FakeManager({'enabled': False, 'ownerIds': 'owner-1'}))
        self.assertIsNone(plugin.on_ingest(message(), self.runtime))
        self.assertEqual(self.console.broker.submitted, [])

    def test_results_only_return_to_private_chat(self):
        self.assertIn('结果', LocalAgentControl.deliver_result(message(), '结果在这里'))
        self.assertEqual(LocalAgentControl.deliver_result(message(kind='group'), 'x'), '')
        self.assertEqual(LocalAgentControl.deliver_result(message(quote={'id': 'q'}), 'x'), '')


if __name__ == '__main__':
    unittest.main()
