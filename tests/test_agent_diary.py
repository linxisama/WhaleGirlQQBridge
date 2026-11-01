"""AI 日记：当天聊天取材、提示构造、模型清洗、每日 txt 与接口。"""
import asyncio
import datetime
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import agent_diary
from src.agent_scheduler import AgentScheduler
from src.memory_api import register_memory_api
from src.unified_memory import UnifiedMemory


LONG_TEXT = ('今天和主人聊了很久，他问我晚饭吃什么，我说随便，结果他还是认真列了几样。'
             '后来群里有人分享了一张猫的照片，大家都在笑。我把这些小事记下来，'
             '提醒自己下次别只顾着回答，也要记得问问对方今天过得怎么样。')


class FakeStyle:
    def full_prompt(self):
        return '鲸鱼娘：温柔、记性好，偶尔有点小脾气。'


class FakeAI:
    model = 'test-model'
    api_url = 'http://127.0.0.1:1/v1/chat/completions'
    api_key_set = True


class FakeEngine:
    def __init__(self, text=LONG_TEXT, last_error=''):
        self.text = text
        self.last_error = last_error
        self.seen = None

    async def chat(self, messages, **kwargs):
        self.seen = (messages, kwargs)
        return self.text


class FakeBot:
    mode = 'onebot'
    style = FakeStyle()
    ai = FakeAI()

    def __init__(self, folder):
        self.root = Path(folder)
        self.config = type('Config', (), {'config_path': Path(folder) / 'config.yaml'})()
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.execute('CREATE TABLE inbox (key TEXT PRIMARY KEY, mode TEXT, account TEXT, '
                        'room TEXT, payload TEXT, state TEXT, reply TEXT DEFAULT \'\', '
                        'error TEXT DEFAULT \'\', created REAL)')
        self.diary_path = Path(folder) / '日记.md'
        self.diary_dir = Path(folder) / '日记'
        self.unified_memory = UnifiedMemory(self.db, self.diary_path, self.diary_dir)
        self.notes = []
        self.engine = FakeEngine()

    def account(self):
        return '10001'

    def owner_ids(self):
        return ['20002']

    def note(self, text):
        self.notes.append(text)

    def _make_ai(self):
        return self.engine


def add_inbox(bot, room, payload, state='sent', mode='onebot', account='10001', when=None):
    bot.db.execute('INSERT INTO inbox(key,mode,account,room,payload,state,created) '
                   'VALUES(?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, mode, account, room,
                    json.dumps(payload, ensure_ascii=False), state,
                    float(when if when is not None else time.time())))
    bot.db.commit()


def day_ts(day, hour=12):
    date = datetime.date.fromisoformat(day)
    return datetime.datetime.combine(date, datetime.time(hour)).timestamp()


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = FakeBot(self.tmp.name)
        self.day = '2026-10-29'

    def tearDown(self):
        self.bot.db.close()
        self.tmp.cleanup()

    def test_private_only_owner_and_notice_is_skipped(self):
        add_inbox(self.bot, 'private:20002', {'text': '主人说早点睡', 'sender_id': '20002',
                                             'sender_name': '主人'}, when=day_ts(self.day))
        add_inbox(self.bot, 'private:20002', {'notice': 'system', 'text': '通知'}, when=day_ts(self.day))
        add_inbox(self.bot, 'private:99999', {'text': '陌生人私聊', 'sender_id': '99999'}, when=day_ts(self.day))
        add_inbox(self.bot, 'group:1', {'text': '群里聊猫', 'sender_id': '30003',
                                        'sender_name': '群友'}, when=day_ts(self.day))
        items = agent_diary.gather_messages(self.bot, self.day)
        self.assertEqual([item['text'] for item in items], ['主人说早点睡', '群里聊猫'])
        self.assertTrue(items[0]['private'])
        self.assertFalse(items[1]['private'])

    def test_other_day_and_other_account_are_excluded(self):
        add_inbox(self.bot, 'group:1', {'text': '昨天的', 'sender_id': '1'},
                  when=day_ts('2026-10-28'))
        add_inbox(self.bot, 'group:1', {'text': '别的号', 'sender_id': '1'},
                  account='99999', when=day_ts(self.day))
        self.assertEqual(agent_diary.gather_messages(self.bot, self.day), [])

    def test_build_prompt_keeps_evidence_and_limits(self):
        text = agent_diary.build_prompt(
            self.day, persona='温柔', messages=[{'text': '聊猫', 'name': '群友', 'private': False}],
            memories=['主人喜欢猫'], previous='昨天的日记')
        self.assertIn('2026-10-29', text)
        self.assertIn('群聊 群友：聊猫', text)
        self.assertIn('主人喜欢猫', text)
        self.assertIn('昨天的日记', text)
        empty = agent_diary.build_prompt(self.day)
        self.assertIn('今天没有记录到聊天', empty)
        with self.assertRaises(ValueError):
            agent_diary.normalize_day('2026/10/29')

    def test_compose_cleans_code_fence_and_date_heading(self):
        engine = FakeEngine('\x60\x60\x60text\n# 2026-10-29\n' + LONG_TEXT + '\n\x60\x60\x60')
        text = asyncio.run(agent_diary.compose(self.bot, engine, self.day))
        self.assertFalse(text.startswith('#'))
        self.assertNotIn('\x60\x60\x60', text)
        self.assertGreaterEqual(len(text), agent_diary.MIN_CHARS)
        self.assertEqual(engine.seen[1]['temperature'], 0.7)
        self.assertEqual(engine.seen[1]['max_tokens'], 900)

    def test_compose_rejects_too_short_text(self):
        engine = FakeEngine('太短了')
        with self.assertRaises(ValueError):
            asyncio.run(agent_diary.compose(self.bot, engine, self.day))

    def test_compose_reports_engine_failure_instead_of_too_short(self):
        engine = FakeEngine('', last_error='HTTP 403：没有可用订阅')
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(agent_diary.compose(self.bot, engine, self.day))
        self.assertIn('没有返回日记内容', str(caught.exception))
        self.assertIn('HTTP 403', str(caught.exception))

    def test_save_writes_daily_txt_and_tail_prefers_it(self):
        self.bot.unified_memory.write_diary('2026-10-28', '昨天的事')
        info = agent_diary.save(self.bot, self.day, LONG_TEXT)
        self.assertTrue(Path(info['path']).is_file())
        self.assertEqual(Path(info['path']).read_text(encoding='utf-8').strip(), LONG_TEXT)
        self.assertTrue(agent_diary.has_day(self.bot, self.day))
        self.assertEqual([item['day'] for item in agent_diary.list_days(self.bot)],
                         [self.day, '2026-10-28'])
        tail = self.bot.unified_memory.diary_tail(500)
        self.assertIn(LONG_TEXT[-100:], tail)
        self.assertIn('昨天的事', tail)


class SchedulerDiaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = FakeBot(self.tmp.name)
        self.scheduler = AgentScheduler(self.bot, Path(self.tmp.name) / 'schedules.json')

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.bot.db.close()
        self.tmp.cleanup()

    async def test_daily_diary_uses_model_and_writes_txt(self):
        task = self.scheduler.create('写日记', 'daily_diary', 1440)
        result = await self.scheduler.run_now(task['id'])
        self.assertIn('已完成', result['last_result'])
        path = agent_diary.diary_file(self.bot, agent_diary.today())
        self.assertTrue(path.is_file())
        self.assertIn(LONG_TEXT[-50:], path.read_text(encoding='utf-8'))
        again = await self.scheduler.run_now(task['id'])
        self.assertIn('已经写过', again['last_result'])

    async def test_no_model_reports_instead_of_fake_template(self):
        self.bot._make_ai = lambda: (_ for _ in ()).throw(RuntimeError('no model'))
        task = self.scheduler.create('写日记', 'daily_diary', 1440)
        result = await self.scheduler.run_now(task['id'])
        self.assertIn('没有可用的模型', result['last_result'])
        self.assertFalse(agent_diary.has_day(self.bot, agent_diary.today()))

    async def test_start_seeds_daily_diary_only_when_model_ready(self):
        self.scheduler._ensure_daily_diary()
        tasks = [item for item in self.scheduler.list() if item['action'] == 'daily_diary']
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['every_minutes'], 1440)
        self.scheduler._ensure_daily_diary()
        self.assertEqual(len([item for item in self.scheduler.list()
                              if item['action'] == 'daily_diary']), 1)


class DiaryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = FakeBot(self.tmp.name)
        app = FastAPI()
        app.state.bot = self.bot
        register_memory_api(app)
        self.client = TestClient(app)

    def tearDown(self):
        self.bot.db.close()
        self.tmp.cleanup()

    def test_diary_route_lists_daily_files(self):
        agent_diary.save(self.bot, '2026-10-29', LONG_TEXT)
        body = self.client.get('/api/memory/diary').json()
        self.assertTrue(body['success'])
        self.assertEqual(body['files'][0]['day'], '2026-10-29')
        self.assertTrue(body['dir'].endswith('日记'))

    def test_auto_requires_confirmation_and_skips_existing(self):
        response = self.client.post('/api/memory/diary/auto', json={'day': '2026-10-29'})
        self.assertEqual(response.status_code, 400, response.text)
        agent_diary.save(self.bot, '2026-10-29', LONG_TEXT)
        body = self.client.post('/api/memory/diary/auto',
                                json={'day': '2026-10-29', 'confirmed': True}).json()
        self.assertTrue(body['success'])
        self.assertTrue(body['skipped'])

    def test_auto_writes_new_diary(self):
        response = self.client.post('/api/memory/diary/auto',
                                    json={'day': '2026-10-29', 'confirmed': True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertFalse(body['skipped'])
        self.assertGreaterEqual(body['chars'], agent_diary.MIN_CHARS)
        self.assertTrue(Path(body['path']).is_file())
        self.assertIn(LONG_TEXT[-50:], Path(body['path']).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
