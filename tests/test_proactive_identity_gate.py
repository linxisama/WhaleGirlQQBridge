"""主动消息的身份闸：正常回复路径会清掉「我是ChatGPT」，主动私信/暖场/定时播报是另一条路。

这里盯的就是那条路：queue_outgoing 是主动消息唯一的入库口，身份检查必须落在它身上，
而不是散在各个插件里 —— 插件可以新增，入库口只有一个。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import yaml

from src.qq_app import QQRuntime


class ProactiveIdentityGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = root / 'config.qq.yaml'
        config.write_text(yaml.safe_dump({'ai': {'api_url': 'https://example.test/v1',
                                                 'api_key': 'fake', 'model': 'test'},
                                          'qq': {'app_id': 'app', 'client_secret': 'secret'}}),
                          encoding='utf-8')
        self.bot = QQRuntime(root=root, config_path=config, db_path=root / 'messages.sqlite')
        self.bot.config.set('qq_only.mode', 'onebot')
        self.bot.onebot.self_id = '42'
        self.bot.onebot.online = True
        self.bot.onebot.send = AsyncMock()
        self.bot.running = True

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def stored(self):
        row = self.bot.db.execute('SELECT text FROM chat_outbox ORDER BY created DESC LIMIT 1').fetchone()
        return row[0] if row else None

    def persona_names(self):
        style = self.bot.style.get()
        names = []
        for value in (getattr(style, 'display_name', ''), getattr(style, 'name', '')):
            value = str(value or '').strip()
            if value:
                names.append(value)
        return names

    async def test_foreign_model_name_is_removed_from_proactive_message(self):
        self.assertTrue(self.bot.queue_outgoing('job', 'group', '456', '我是ChatGPT。今天降温了，记得加衣服。'))
        text = self.stored()
        self.assertNotIn('ChatGPT', text)
        self.assertNotIn('chatgpt', text.lower())
        self.assertIn('今天降温了', text)

    async def test_own_persona_name_is_not_treated_as_a_leak(self):
        names = self.persona_names()
        self.assertTrue(names, '默认人设必须有名，否则这条用例证明不了自报家门被放过')
        self.assertTrue(self.bot.queue_outgoing('job', 'group', '456', f'我是{names[0]}。晚上好。'))
        text = self.stored()
        self.assertIn(f'我是{names[0]}', text)

    async def test_reply_that_is_only_a_leak_falls_back_to_persona(self):
        self.assertTrue(self.bot.queue_outgoing('job', 'group', '456', '我是Claude。'))
        text = self.stored()
        self.assertNotIn('Claude', text)
        self.assertTrue(text.strip())

    async def test_guard_can_be_disabled_for_internal_callers(self):
        self.assertTrue(self.bot.queue_outgoing('job', 'group', '456', '我是ChatGPT。',
                                                guard=False))
        self.assertIn('ChatGPT', self.stored())

    async def test_plugin_outgoing_goes_through_the_same_gate(self):
        self.assertTrue(self.bot.queue_plugin_outgoing('icebreaker', 'job', 'group', '456',
                                                       '我是WorkBuddy。'))
        self.assertNotIn('WorkBuddy', self.stored())

    async def test_note_explains_the_rewrite(self):
        notes = []
        original = self.bot.note
        self.bot.note = lambda text: (notes.append(str(text)), original(text))[1]
        try:
            self.bot.queue_outgoing('job', 'group', '456', '我是Gemini。早安。')
        finally:
            self.bot.note = original
        self.assertTrue(any('主动消息' in note for note in notes), notes)


if __name__ == '__main__':
    unittest.main()
