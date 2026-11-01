"""定时任务接入「夜间固化」与「自画像重写」（W7c2）。

全程离线：SimpleNamespace 假 bot + :memory: sqlite + 临时 data_dir，
不联网、不 sleep、不碰真实 data/。
"""
import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import agent_dream
from src import agent_portrait as ap
from src.agent_scheduler import ACTIONS, AgentScheduler
from src.unified_memory import UnifiedMemory

# 清洗后仍 >80 字，且不以「我是谁：」开头（避免触发 clean_text 的标题剥离）。
LONG = '我最近常在深夜整理记忆。' + '把说过的话慢慢沉淀成自己的判断，不急着下结论。' * 4
NOTE_ID = '今天和小鲸一起看了海边的日落'


class FakeConfig:
    """模拟 ConfigManager.get() 的点号取值 + config_path。"""

    def __init__(self, data=None, path=None):
        self.data = data or {}
        self.config_path = Path(path or (Path(tempfile.gettempdir()) / 'config.qq.yaml'))

    def get(self, key, default=None):
        node = self.data
        for part in str(key).split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class FakeEngine:
    def __init__(self, reply='', error=None):
        self.reply = reply
        self.error = error
        self.calls = []
        self.closed = 0

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error is not None:
            raise self.error
        return self.reply

    async def close(self):
        self.closed += 1


class EngineWithoutClose:
    async def chat(self, messages, **kwargs):
        return LONG


def make_bot(data_dir, config=None, engine=None, maker=True):
    notes = []
    bot = SimpleNamespace(db=sqlite3.connect(':memory:'), config=config or FakeConfig(),
                          root=Path(data_dir).parent, data_root=Path(data_dir),
                          diary_path=None, notes=notes)
    bot.note = notes.append
    if maker:
        bot._make_ai = lambda: engine
    return bot


DREAM_ON = {'ai': {'dream_enabled': True, 'dream_rate': 1.0}}
DREAM_RATE0 = {'ai': {'dream_enabled': True, 'dream_rate': 0.0}}
PORTRAIT_ON = {'ai': {'portrait_enabled': True}}


class GrowthSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.data_dir = self.base / 'data'
        self.data_dir.mkdir()
        self._bots = []
        self._schedulers = []
        self._seq = 0

    def tearDown(self):
        for bot in self._bots:
            try:
                bot.db.close()
            except Exception:
                pass
        self._schedulers.clear()
        self._tmp.cleanup()

    def build(self, config=None, engine=None, maker=True):
        self._seq += 1
        bot = make_bot(self.data_dir, config=config or FakeConfig(), engine=engine, maker=maker)
        self._bots.append(bot)
        scheduler = AgentScheduler(bot, self.base / ('schedules%d.json' % self._seq))
        self._schedulers.append(scheduler)
        return bot, scheduler

    def seed(self, bot, texts=(NOTE_ID,)):
        memory = UnifiedMemory(bot.db)
        for text in texts:
            memory.remember(text, source='chat', layer='topic', importance=50)
        return memory

    def soul_rows(self, bot):
        return UnifiedMemory(bot.db).search(layer='soul', limit=30) or []

    def history_files(self):
        folder = ap.history_dir(self.data_dir)
        return sorted(folder.iterdir()) if folder.exists() else []

    async def run_action(self, scheduler, action):
        task = scheduler.create('', action, 60)
        return await scheduler.run_now(task['id'])

    # ---------- ACTIONS / 校验 ----------

    async def test_actions_include_growth_tasks(self):
        self.assertEqual(ACTIONS['dream_consolidate'], '夜间固化记忆')
        self.assertEqual(ACTIONS['self_portrait'], '重写自画像')
        self.assertEqual(len(ACTIONS), 10)

    async def test_memory_daily_writes_report_and_log(self):
        bot, scheduler = self.build()
        UnifiedMemory(bot.db).remember('主人喜欢在深夜整理当天所有聊天记录和记忆条目',
                                       source='chat', layer='fact', importance=50)
        task = await self.run_action(scheduler, 'memory_daily')
        self.assertIn('记忆每日维护', task['last_result'])
        folder = self.data_dir / '记忆'
        self.assertTrue((folder / '每日日志.md').is_file())
        self.assertTrue(any(folder.glob('体检报告-*.md')))

    async def test_memory_maintain_backs_up_before_cleanup(self):
        bot, scheduler = self.build()
        db_path = self.data_dir / 'messages.sqlite3'
        sqlite3.connect(db_path).close()
        bot.db_path = str(db_path)
        task = await self.run_action(scheduler, 'memory_maintain')
        self.assertIn('记忆维护', task['last_result'])
        self.assertIn('备份 memory-', task['last_result'])
        files = list((self.data_dir / '记忆' / '备份').glob('memory-*.db'))
        self.assertEqual(len(files), 1)

    async def test_create_accepts_new_actions(self):
        _bot, scheduler = self.build()
        dream = scheduler.create('', 'dream_consolidate', 60)
        portrait = scheduler.create('', 'self_portrait', 1440)
        self.assertEqual(dream['name'], '夜间固化记忆')
        self.assertEqual(portrait['name'], '重写自画像')
        self.assertEqual([item['action'] for item in scheduler.list()],
                         ['dream_consolidate', 'self_portrait'])

    async def test_validate_rejects_unknown_action(self):
        _bot, scheduler = self.build()
        with self.assertRaises(ValueError):
            scheduler.create('x', 'nightmare', 60)
        with self.assertRaises(ValueError):
            scheduler.create('x', 'dream_consolidate', 1)

    # ---------- 夜间固化 ----------

    async def test_dream_disabled_text(self):
        bot, scheduler = self.build(FakeConfig({'ai': {'dream_enabled': False}}))
        task = await self.run_action(scheduler, 'dream_consolidate')
        self.assertEqual(task['last_result'], '夜间固化未启用（设置页开关关着）')
        self.assertTrue(bot.notes)

    async def test_dream_no_items_text(self):
        _bot, scheduler = self.build(FakeConfig(DREAM_ON))
        task = await self.run_action(scheduler, 'dream_consolidate')
        self.assertEqual(task['last_result'], '近几天没有可固化的素材')

    async def test_dream_rate_skipped_text(self):
        bot, scheduler = self.build(FakeConfig(DREAM_RATE0))
        self.seed(bot)
        task = await self.run_action(scheduler, 'dream_consolidate')
        self.assertEqual(task['last_result'], '这次概率门没放行（今晚不固化）')
        self.assertEqual(self.soul_rows(bot), [])

    async def test_dream_ok_text_writes_soul_and_ledger(self):
        bot, scheduler = self.build(FakeConfig(DREAM_ON))
        self.seed(bot)
        task = await self.run_action(scheduler, 'dream_consolidate')
        self.assertEqual(task['last_result'], '夜间固化：写 1 条，聚类 1 簇')
        rows = self.soul_rows(bot)
        self.assertEqual(len(rows), 1)
        self.assertIn('dream', rows[0]['tags'])
        self.assertTrue(agent_dream.ledger_path(self.data_dir).exists())

    async def test_dream_error_reason_text(self):
        _bot, scheduler = self.build(FakeConfig(DREAM_ON))
        broken = {'ok': False, 'reason': 'error', 'detail': 'ValueError: 账本坏了'}
        with mock.patch.object(agent_dream, 'consolidate', return_value=broken):
            task = await self.run_action(scheduler, 'dream_consolidate')
        self.assertEqual(task['last_result'], '夜间固化失败：ValueError: 账本坏了')

    async def test_dream_exception_is_swallowed(self):
        _bot, scheduler = self.build(FakeConfig(DREAM_ON))
        with mock.patch.object(agent_dream, 'consolidate', side_effect=RuntimeError('boom')):
            task = await self.run_action(scheduler, 'dream_consolidate')
        self.assertEqual(task['last_result'], '夜间固化失败：RuntimeError: boom')

    # ---------- 自画像 ----------

    async def test_portrait_ok_writes_file_and_one_soul_memory(self):
        engine = FakeEngine(reply=LONG)
        bot, scheduler = self.build(FakeConfig(PORTRAIT_ON), engine=engine)
        task = await self.run_action(scheduler, 'self_portrait')
        path = self.data_dir / '我是谁.md'
        self.assertTrue(path.exists())
        self.assertIn('深夜整理记忆', ap.read_portrait(self.data_dir))
        soul = [row for row in self.soul_rows(bot)
                if 'portrait' in (row.get('tags') or [])]
        self.assertEqual(len(soul), 1)
        self.assertEqual(task['last_result'],
                         '自画像已重写：%d 字（首次生成）' % len(ap.read_portrait(self.data_dir)))
        self.assertEqual(self.history_files(), [])
        self.assertEqual(engine.closed, 1)
        messages, kwargs = engine.calls[0]
        self.assertIn('我是谁', messages[0]['content'])
        self.assertIn('我是谁', kwargs['system_prompt'])
        self.assertEqual(kwargs['temperature'], 0.6)
        self.assertEqual(kwargs['max_tokens'], 600)

    async def test_portrait_first_run_says_first_generation(self):
        engine = FakeEngine(reply=LONG)
        bot, scheduler = self.build(FakeConfig(PORTRAIT_ON), engine=engine)
        task = await self.run_action(scheduler, 'self_portrait')
        self.assertIn('（首次生成）', task['last_result'])
        self.assertNotIn('旧版已快照', task['last_result'])
        self.assertEqual(self.history_files(), [])

    async def test_portrait_second_run_says_snapshot(self):
        engine = FakeEngine(reply=LONG)
        bot, scheduler = self.build(FakeConfig(PORTRAIT_ON), engine=engine)
        task = await self.run_action(scheduler, 'self_portrait')
        first = task['last_result']
        again = await scheduler.run_now(task['id'])
        self.assertIn('（首次生成）', first)
        self.assertIn('（旧版已快照）', again['last_result'])
        self.assertEqual(len(self.history_files()), 1)
        self.assertEqual(engine.closed, 2)

    async def test_portrait_without_engine_text(self):
        bot, scheduler = self.build(FakeConfig(PORTRAIT_ON), engine=None)
        task = await self.run_action(scheduler, 'self_portrait')
        self.assertEqual(task['last_result'], '没有可用的模型配置，自画像未重写')
        self.assertFalse((self.data_dir / '我是谁.md').exists())

    async def test_portrait_missing_maker_text(self):
        _bot, scheduler = self.build(FakeConfig(PORTRAIT_ON), maker=False)
        task = await self.run_action(scheduler, 'self_portrait')
        self.assertEqual(task['last_result'], '没有可用的模型配置，自画像未重写')

    async def test_portrait_chat_error_is_swallowed_and_engine_closed(self):
        engine = FakeEngine(error=RuntimeError('net down'))
        _bot, scheduler = self.build(FakeConfig(PORTRAIT_ON), engine=engine)
        task = await self.run_action(scheduler, 'self_portrait')
        self.assertTrue(task['last_result'].startswith('自画像重写失败：'))
        self.assertIn('RuntimeError', task['last_result'])
        self.assertIn('net down', task['last_result'])
        self.assertEqual(engine.closed, 1)
        self.assertFalse((self.data_dir / '我是谁.md').exists())

    async def test_portrait_disabled_text(self):
        engine = FakeEngine(reply=LONG)
        _bot, scheduler = self.build(FakeConfig({'ai': {'portrait_enabled': False}}), engine=engine)
        task = await self.run_action(scheduler, 'self_portrait')
        self.assertEqual(task['last_result'], '自画像开关关着，未重写')
        self.assertEqual(engine.closed, 1)
        self.assertFalse((self.data_dir / '我是谁.md').exists())

    async def test_portrait_too_short_text(self):
        engine = FakeEngine(reply='太短')
        _bot, scheduler = self.build(FakeConfig(PORTRAIT_ON), engine=engine)
        task = await self.run_action(scheduler, 'self_portrait')
        self.assertEqual(task['last_result'], '模型回的正文太短（<80 字），未覆盖')
        self.assertFalse((self.data_dir / '我是谁.md').exists())

    async def test_portrait_engine_without_close_is_tolerated(self):
        _bot, scheduler = self.build(FakeConfig(PORTRAIT_ON), engine=EngineWithoutClose())
        task = await self.run_action(scheduler, 'self_portrait')
        self.assertTrue(task['last_result'].startswith('自画像已重写：'))

    # ---------- 公共收尾：last_run / last_result / save ----------

    async def test_both_branches_update_last_run_and_save(self):
        bot, scheduler = self.build(FakeConfig({'ai': {'dream_enabled': True,
                                                       'dream_rate': 1.0,
                                                       'portrait_enabled': True}}),
                                    engine=FakeEngine(reply=LONG))
        self.seed(bot)
        saves = []
        original = scheduler.save

        def counting():
            saves.append(time.time())
            original()

        scheduler.save = counting
        dream = await self.run_action(scheduler, 'dream_consolidate')
        dream_saves = len(saves)
        portrait = await self.run_action(scheduler, 'self_portrait')
        for task, prefix in ((dream, '夜间固化：'), (portrait, '自画像已重写：')):
            self.assertGreater(task['last_run'], 0)
            self.assertTrue(task['last_result'].startswith(prefix))
        self.assertGreaterEqual(dream_saves, 1)
        self.assertGreater(len(saves), dream_saves)
        self.assertEqual(len(bot.notes), 2)
        self.assertIn('夜间固化记忆', bot.notes[0])
        self.assertIn('重写自画像', bot.notes[1])
        self.assertFalse(scheduler._lock.locked())


if __name__ == '__main__':
    unittest.main()
