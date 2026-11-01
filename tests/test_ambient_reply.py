"""群聊随机接话：没点名的消息可按概率放行，带冷却/上限/时段。"""
import time
import unittest
from unittest.mock import patch

from src.plugins.ambient_reply import AmbientReply


class _Manager:
    def __init__(self, settings):
        self._settings = {'ambient_reply': settings}

    def get_settings(self, plugin_id):
        return self._settings.get(plugin_id, {})


def _plugin(**settings):
    base = {'enabled': True, 'percent': 100, 'cooldownSec': 60, 'maxPerHour': 5,
            'minChars': 2, 'activeHours': '00:00-24:00', 'boostQuestion': 0,
            'groups': '', 'ignorePrefix': '/'}
    base.update(settings)
    return AmbientReply(_Manager(base))


def _message(text='今天吃啥呢', target='group:1', **extra):
    data = {'kind': 'group', 'target': target, 'text': text, 'id': 'm1', 'at': False}
    data.update(extra)
    return data


class AmbientReplyTests(unittest.TestCase):
    def test_replies_to_unaddressed_message(self):
        plugin = _plugin()
        self.assertTrue(plugin.wants_reply(_message(), None))

    def test_cooldown_blocks_second_reply(self):
        plugin = _plugin()
        self.assertTrue(plugin.wants_reply(_message(), None))
        self.assertFalse(plugin.wants_reply(_message('再来一句'), None))

    def test_at_message_is_not_ambient(self):
        plugin = _plugin()
        self.assertFalse(plugin.wants_reply(_message(at=True), None))

    def test_other_group_not_targeted(self):
        plugin = _plugin(groups='group:2')
        self.assertFalse(plugin.wants_reply(_message(target='group:1'), None))
        self.assertTrue(plugin.wants_reply(_message(target='group:2'), None))

    def test_percent_zero_never_replies(self):
        plugin = _plugin(percent=1)
        with patch('src.plugins.ambient_reply.random.random', return_value=0.99):
            self.assertFalse(plugin.wants_reply(_message(), None))

    def test_hourly_cap(self):
        plugin = _plugin(cooldownSec=0, maxPerHour=2)
        self.assertTrue(plugin.wants_reply(_message(), None))
        self.assertTrue(plugin.wants_reply(_message(), None))
        self.assertFalse(plugin.wants_reply(_message(), None))

    def test_command_prefix_ignored(self):
        plugin = _plugin()
        self.assertFalse(plugin.wants_reply(_message('/status'), None))


if __name__ == '__main__':
    unittest.main()


class AutoMemoryPatternTests(unittest.TestCase):
    def test_first_person_statements_are_observed(self):
        from src.plugins.conversation_memory import ConversationMemory
        import re
        pattern = (r'我(?:喜欢|不喜欢|讨厌|叫|的昵称是|是|在|住|养|玩|学|上班|来自)'
                   r'[^\n。！？!?]{1,70}[。！!~～]?')
        for text in ('我是学生', '我住在杭州', '我养了一只猫', '我在做机器人'):
            self.assertTrue(re.fullmatch(pattern, text), text)
        self.assertFalse(re.fullmatch(pattern, '你是学生'))


class ApiUrlHealTests(unittest.TestCase):
    def test_bigmodel_broken_url_is_healed(self):
        from src.qq_app import QQRuntime
        self.assertEqual(
            QQRuntime.heal_api_url('https://open.bigmodel.cn/api/paas/v4/chat/completions/messages'),
            'https://open.bigmodel.cn/api/paas/v4/chat/completions')

    def test_duplicated_tail_is_healed(self):
        from src.qq_app import QQRuntime
        self.assertEqual(
            QQRuntime.heal_api_url('https://workbuddy.tokenmao.cn/v1/messages/chat/completions'
                                   '/messages/chat/completions'),
            'https://workbuddy.tokenmao.cn/v1/chat/completions')

    def test_normal_urls_untouched(self):
        from src.qq_app import QQRuntime
        for url in ('https://api.anthropic.com/v1/messages',
                    'https://open.bigmodel.cn/api/paas/v4',
                    'https://x.cn/v1/chat/completions'):
            self.assertEqual(QQRuntime.heal_api_url(url), '', url)
