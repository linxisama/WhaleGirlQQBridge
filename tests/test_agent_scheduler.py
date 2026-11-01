"""Agent 定时任务：连接检查、日报、间隔与启停。"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from src.agent_scheduler import AgentScheduler


class FakeAI:
    model = 'test-model'


class FakeStyle:
    active_name = 'whale'


class FakeBot:
    root = Path(tempfile.gettempdir())
    ai = FakeAI()
    style = FakeStyle()

    def __init__(self):
        self.notes = []
        self.config = type('Config', (), {'config_path': Path(tempfile.gettempdir()) / 'x.yaml'})()
        self.db = __import__('sqlite3').connect(':memory:')
        self.db.execute('CREATE TABLE inbox(payload TEXT,state TEXT,created REAL)')
        self.db.execute("INSERT INTO inbox VALUES(?,?,?)",
                        ('{"_usage":{"total_tokens":120}}', 'sent', time.time()))
        self.db.commit()

    def status(self):
        return {'running': True, 'reply_paused': False, 'mode': 'onebot',
                'onebot': {'connected': True, 'online': True}, 'official': {}}

    def note(self, text):
        self.notes.append(text)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'schedules.json'
        self.bot = FakeBot()
        self.scheduler = AgentScheduler(self.bot, self.path)

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.bot.db.close()
        self.tmp.cleanup()

    async def test_create_requires_valid_interval(self):
        task = self.scheduler.create('连接检查', 'connection_check', 30)
        self.assertEqual(task['name'], '连接检查')
        with self.assertRaises(ValueError):
            self.scheduler.create('x', 'nope', 30)
        with self.assertRaises(ValueError):
            self.scheduler.create('x', 'connection_check', 1)

    async def test_run_connection_and_digest(self):
        task = self.scheduler.create('连接检查', 'connection_check', 30)
        result = await self.scheduler.run_now(task['id'])
        self.assertIn('连接正常', result['last_result'])
        self.assertTrue(self.bot.notes)
        daily = self.scheduler.create('日报', 'daily_digest', 60)
        result = await self.scheduler.run_now(daily['id'])
        self.assertIn('近 24 小时', result['last_result'])
        self.assertIn('120', result['last_result'])

    async def test_due_only_runs_enabled_after_interval(self):
        task = self.scheduler.create('连接检查', 'connection_check', 5)
        await self.scheduler.run_now(task['id'])
        self.assertEqual(await self.scheduler.run_due(now=time.time()), [])
        ran = await self.scheduler.run_due(now=time.time() + 301)
        self.assertEqual(len(ran), 1)
        self.scheduler.set_enabled(task['id'], False)
        self.assertEqual(await self.scheduler.run_due(now=time.time() + 999999), [])

    async def test_state_survives_reload(self):
        self.scheduler.create('日报', 'daily_digest', 60)
        again = AgentScheduler(self.bot, self.path)
        self.assertEqual(again.list()[0]['action'], 'daily_digest')
        self.assertTrue(again.delete(again.list()[0]['id']))
        self.assertEqual(again.list(), [])


if __name__ == '__main__':
    unittest.main()
