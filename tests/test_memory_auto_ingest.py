"""主人私聊的明确约定自动写进长期记忆（零 token、只写 fact 层）。"""
import asyncio
import tempfile
import unittest
from unittest import mock

from src.agent_admin import AgentAdmin
from src.qq_app import QQRuntime
from tests.test_chat_features import message
from tests.test_persona_files import build_root


class AutoMemoryIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build_root(self.temp.name)
        AgentAdmin(self.root / 'agent_admin.json').bind('10001')
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass
        self.temp.cleanup()

    def owner_message(self, text='以后请叫我小鱼', subject='10001', kind='private', target='10001', mid='auto1'):
        return message(mid=mid, text=text, subject=subject, kind=kind, target=target)

    def facts(self, query=''):
        return self.bot.unified_memory.search(query, limit=10)

    def test_owner_private_decision_is_remembered(self):
        result = self.bot.remember_owner_message(self.owner_message(), 'private:10001')
        self.assertEqual(result['added'], 1)
        rows = self.facts('小鱼')
        self.assertTrue(rows)
        self.assertIn('以后请叫我小鱼', rows[0]['content'])
        self.assertEqual(rows[0]['layer'], 'fact')
        self.assertIn('主人私聊', rows[0]['evidence'])

    def test_group_message_is_not_ingested(self):
        result = self.bot.remember_owner_message(
            self.owner_message(kind='group', target='group:1'), 'group:1')
        self.assertEqual(result['added'], 0)
        self.assertEqual(self.bot.db.execute(
            'SELECT count(*) FROM unified_memories').fetchone()[0], 0)

    def test_non_owner_private_message_is_not_ingested(self):
        result = self.bot.remember_owner_message(
            self.owner_message(subject='20002', target='20002'), 'private:20002')
        self.assertEqual(result['added'], 0)
        self.assertEqual(self.bot.db.execute(
            'SELECT count(*) FROM unified_memories').fetchone()[0], 0)

    def test_secret_is_not_ingested(self):
        result = self.bot.remember_owner_message(
            self.owner_message('记住我的密码：abc123'), 'private:10001')
        self.assertEqual(result['added'], 0)
        self.assertEqual(self.bot.db.execute(
            'SELECT count(*) FROM unified_memories').fetchone()[0], 0)

    def test_repeated_decision_is_deduplicated(self):
        first = self.bot.remember_owner_message(self.owner_message(), 'private:10001')
        second = self.bot.remember_owner_message(self.owner_message(), 'private:10001')
        self.assertEqual(first['added'], 1)
        self.assertEqual(second['added'], 0)
        self.assertEqual(self.bot.db.execute(
            'SELECT count(*) FROM unified_memories').fetchone()[0], 1)

    def test_switch_off_disables_auto_memory(self):
        self.bot.config.set('ai.auto_memory', False)
        result = self.bot.remember_owner_message(self.owner_message(), 'private:10001')
        self.assertEqual(result['added'], 0)
        self.assertEqual(self.bot.db.execute(
            'SELECT count(*) FROM unified_memories').fetchone()[0], 0)

    def test_reply_path_calls_auto_memory(self):
        self.bot.running = True
        self.bot.official.authenticated = True
        self.bot.official.send = mock.AsyncMock()
        self.bot.ai.chat = mock.AsyncMock(return_value='好')
        seen = []
        self.bot.remember_owner_message = mock.Mock(
            side_effect=lambda msg, room='': seen.append((msg, room)) or {'added': 0})

        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.bot.ingest(self.owner_message(mid='reply1'), 'official')
            for _ in range(300):
                if self.bot.db.execute(
                        "SELECT count(*) FROM inbox WHERE state='sent'").fetchone()[0]:
                    return
                await asyncio.sleep(0.02)
            self.fail('回复没有发生')

        asyncio.run(run())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], 'private:10001')


if __name__ == '__main__':
    unittest.main()
