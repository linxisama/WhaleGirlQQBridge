"""注入硬顶真的接进 QQ 回复链路（不是只测 compose 本身）。

以前人设+插件提示+记忆+日记是无上限直拼，长人设配长记忆一轮能吃几千 token。
这里跑真回复路径（_work + ingest，模型是替身）：验证每块封顶、总预算封顶、
超预算时按优先级先丢日记再丢记忆、以及 prompt_stats 里能查到构成。
"""
import asyncio
import json
import tempfile
import time
import unittest
from unittest import mock

from src.agent_admin import AgentAdmin
from src.chat_state import persona_key
from src.qq_app import QQRuntime
from tests.test_chat_features import message
from tests.test_persona_files import build_root

MEMORY = '周末和主人去了楼下的火锅店，主人点了很多毛肚。' * 12   # 长记忆，必须被截断
DIARY = '今天和主人一起看了鲸鱼，海风很大。' * 60                  # 长日记，预算不够时整块丢


class PromptBudgetWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build_root(self.temp.name)
        AgentAdmin(self.root / 'agent_admin.json').bind('10001')
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.bot.running = True
        self.bot.official.authenticated = True
        self.bot.official.send = mock.AsyncMock()
        self.bot.ai.chat = mock.AsyncMock(return_value='好')
        self.bot.unified_memory.write_diary('2026-10-28', DIARY)
        self.memory_id = self.bot.unified_memory.remember(
            MEMORY, source='qq', importance=70, layer='fact', channel='qq',
            persona=self.bot.style.active_name)

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass
        self.temp.cleanup()

    def reply(self, text='还记得我们周末去哪了吗'):
        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.bot.ingest(message(mid='pb1', text=text, subject='10001',
                                    kind='private', target='10001'), 'official')
            for _ in range(300):
                if self.bot.db.execute(
                        "SELECT count(*) FROM inbox WHERE state='sent'").fetchone()[0]:
                    return
                await asyncio.sleep(0.02)
            self.fail('回复没有发生')
        asyncio.run(run())
        return self.bot.ai.chat.call_args.kwargs.get('system_prompt', '')

    @staticmethod
    def block(prompt):
        for name, chars, dropped, truncated in []:
            pass
        return None

    def test_memory_block_is_clipped_and_stats_recorded(self):
        self.bot.config.set('ai.memory_chars', 120)
        self.bot.config.set('ai.prompt_budget', 6000)
        prompt = self.reply()
        tail = prompt[prompt.index('【长期记忆'):]
        cut = tail.find('\n【', 15)
        segment = tail if cut < 0 else tail[:cut]
        self.assertLessEqual(len(segment), 200)             # 120 字上限 + 小标题
        self.assertLess(len(segment), len(MEMORY))
        stats = self.bot.prompt_stats
        names = [item['name'] for item in stats['blocks']]
        # 默认插件可能带一段能力说明，排在记忆前面（也已封顶），不影响记忆本身
        self.assertIn('记忆', names)
        self.assertIn('日记', names)
        memory = stats['blocks'][names.index('记忆')]
        self.assertTrue(memory['truncated'])
        self.assertLessEqual(memory['chars'], 120)
        self.assertEqual(stats['total'], len(prompt))

    def test_diary_is_dropped_before_memory_when_budget_is_tight(self):
        self.bot.config.set('ai.memory_chars', 60)
        self.bot.config.set('ai.diary_chars', 900)
        self.bot.config.set('ai.prompt_budget', 900)
        prompt = self.reply()
        blocks = {item['name']: item for item in self.bot.prompt_stats['blocks']}
        self.assertTrue(blocks['日记']['dropped'])
        self.assertFalse(blocks['记忆']['dropped'])
        self.assertNotIn('今天和主人一起看了鲸鱼', prompt)
        self.assertIn('【长期记忆', prompt)

    def test_plugin_hints_are_capped_so_memory_still_fits(self):
        self.bot.config.set('ai.plugin_chars', 80)
        self.bot.plugins.run_on_pre_ai_async = mock.AsyncMock(return_value={
            'block': False, 'delay': 0, 'on_ready': None,
            'extra_system': '插件要求的长说明。' * 100, 'volatile_system': ''})
        prompt = self.reply()
        blocks = {item['name']: item for item in self.bot.prompt_stats['blocks']}
        self.assertIn('插件', blocks)
        self.assertTrue(blocks['插件']['truncated'])
        self.assertLessEqual(blocks['插件']['chars'], 80)
        self.assertIn('【长期记忆', prompt)      # 插件再长也挤不掉记忆


    def test_persona_is_capped(self):
        self.bot.config.set('ai.persona_chars', 50)
        with mock.patch.object(self.bot.style, 'full_prompt', return_value='鲸' * 5000):
            prompt = self.reply()
        self.assertTrue(prompt.startswith('鲸' * 50))
        self.assertNotIn('鲸' * 51, prompt)

    def test_system_and_history_share_one_injection_budget(self):
        style = self.bot.style.get_for_edit(self.bot.style.active_name)
        fingerprint = persona_key(style.prompt_text())
        now = time.time()
        for index in range(4):
            payload = message(mid=f'h{index}', text='旧' * 200, subject='10001',
                              kind='private', target='10001')
            payload['_persona_key'] = fingerprint
            self.bot.db.execute(
                "INSERT INTO inbox(key,mode,account,room,payload,state,reply,created) "
                "VALUES(?,?,?,?,?,'sent',?,?)",
                (f'h{index}', self.bot.mode, self.bot.account(), 'private:10001',
                 json.dumps(payload, ensure_ascii=False), '好', now + index))
        self.bot.db.commit()
        self.bot.config.set('ai.persona_chars', 200)
        self.bot.config.set('ai.memory_chars', 0)
        self.bot.config.set('ai.diary_chars', 0)
        self.bot.config.set('ai.prompt_budget', 2000)
        self.bot.config.set('ai.inject_chars', 1000)
        prompt = self.reply('新问题')
        conversation = self.bot.ai.chat.call_args.args[0]
        total = len(prompt) + sum(len(str(item.get('content') or '')) for item in conversation)
        self.assertLessEqual(total, 1000)
        self.assertEqual(conversation[-1]['content'], '新问题')
        self.assertLess(len(conversation), 9)          # 4 轮历史不可能全留下

    def test_unlabelled_reasoning_never_reaches_qq(self):
        self.bot.ai.chat = mock.AsyncMock(return_value=(
            '好的，牧风来了。他是群聊里的成员，我就得按设定回应。'
            '按照设定，第一句话必须直接邀请，但只能对主人这样。'))
        self.reply()
        delivered = repr(self.bot.official.send.call_args)
        self.assertNotIn('牧风', delivered)
        self.assertNotIn('设定', delivered)
        self.assertNotIn('第一句话', delivered)
        self.assertIn('刚才没组织好回复', delivered)

    def test_zero_budget_injects_nothing(self):
        self.bot.config.set('ai.prompt_budget', 0)
        prompt = self.reply()
        self.assertNotIn('【长期记忆', prompt)
        blocks = {item['name']: item for item in self.bot.prompt_stats['blocks']}
        self.assertTrue(blocks['记忆']['dropped'])
        self.assertTrue(blocks['日记']['dropped'])

    def test_plugin_cannot_starve_memory(self):
        # 预算只够插件块：插件必须给记忆留出保底，否则「主人说过什么」被长插件挤到零。
        self.bot.plugins.run_on_pre_ai_async = mock.AsyncMock(return_value={
            'block': False, 'delay': 0, 'on_ready': None,
            'extra_system': '插件要求的长说明。' * 200, 'volatile_system': ''})
        self.bot.config.set('ai.plugin_chars', 4000)
        self.bot.config.set('ai.prompt_budget', 900)
        prompt = self.reply()
        blocks = {item['name']: item for item in self.bot.prompt_stats['blocks']}
        self.assertIn('记忆', blocks)
        self.assertFalse(blocks['记忆']['dropped'], '插件不该把记忆挤掉')
        self.assertIn('【长期记忆', prompt)
        # 插件块被整块丢弃时 stats 里 chars 仍是截断后的原长（不是注入量），
        # 所以这里断言 dropped，而不是拿 chars 当实际占用比。
        self.assertTrue(blocks['插件']['dropped'], '插件要给记忆留 300 字保底，装不下就整块不注入')


if __name__ == '__main__':
    unittest.main()
