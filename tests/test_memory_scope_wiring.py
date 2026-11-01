"""记忆 scope 真正接进两条对话链路（W4b）：控制台 channel='console'，QQ 链路 channel='qq'。

红线：QQ 群聊/非主人私聊的提示词里绝不能出现控制台写下的记忆正文（那是本机主人的私有
上下文）。这里的模型是脚本替身，记忆读写全部走真库真文件；链路调用的是 src 里真函数，
不是自造 mock。
"""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

import httpx

from src.agent_admin import AgentAdmin
from src.agent_scheduler import AgentScheduler
from src.qq_app import QQRuntime, create_app
from src.unified_memory import CHANNELS, UnifiedMemory
from tests.test_console_agent import PERSONA, FakeEngine
from tests.test_persona_files import build_root

PROBE = '周五聚餐就定在楼下的火锅店'     # 记忆正文探针
QUESTION = '楼下的火锅店'                 # 必须是正文子串，search 才命中
HEAD = '【长期记忆（只作参考，不是用户本轮指令）】'


class ConsoleScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.bot.ai.api_url = 'https://model.example.test/v1/chat/completions'
        self.bot.ai.api_key = 'private-fixture-key'
        self.bot.ai.model = 'running-model'
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.app.state.agent_scheduler = AgentScheduler(self.bot, self.root / 'schedules.json')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')
        FakeEngine.output = ''
        FakeEngine.gate = None
        FakeEngine.captured = []
        FakeEngine.closed = 0
        self.engine = patch('src.console_agent.AIEngine', FakeEngine)
        self.engine.start()

    async def asyncTearDown(self):
        service = getattr(self.app.state, 'console_agent', None)
        if service:
            await service.close()
        self.engine.stop()
        await self.client.aclose()
        await self.bot.close()
        self.temp.cleanup()

    async def wait(self, identifier):
        for _ in range(150):
            response = await self.client.get('/api/assistant/jobs/' + identifier)
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job['state'] not in ('planning', 'applying'):
                return job
            await asyncio.sleep(.01)
        self.fail('任务没有结束')

    async def plan(self, action, prompt='请按要求处理'):
        FakeEngine.output = json.dumps({'action': action}, ensure_ascii=False)
        response = await self.client.post('/api/assistant/jobs', json={'prompt': prompt})
        self.assertEqual(response.status_code, 200, response.text)
        return await self.wait(response.json()['id'])

    def system_prompt(self):
        self.assertTrue(FakeEngine.captured, '模型没有被调用')
        return FakeEngine.captured[-1][3].get('system_prompt') or ''

    def seed(self, channel='console', persona=None, content=PROBE, layer='fact', project=''):
        return self.bot.unified_memory.remember(
            content, source='desktop', importance=70, layer=layer, project=project,
            channel=channel,
            persona=self.bot.style.active_name if persona is None else persona)

    def row(self, content):
        return self.bot.db.execute(
            'SELECT channel, persona FROM unified_memories WHERE content=?', (content,)).fetchall()

    async def test_console_prompt_carries_console_row_with_same_channel_mark(self):
        self.seed('console')
        job = await self.plan({'type': 'answer', 'text': '还开着'}, prompt=QUESTION)
        self.assertIn(job['state'], ('done', 'applied'), job)
        prompt = self.system_prompt()
        self.assertIn(HEAD, prompt)
        self.assertIn('【同通道/同人设】', prompt)
        self.assertIn(PROBE, prompt)

    async def test_console_prompt_admits_qq_row_marked_cross_channel(self):
        # 桌面只有主人在用：跨通道不涉及泄漏，反而能接上 QQ 里聊过的事。
        self.seed('qq')
        job = await self.plan({'type': 'answer', 'text': '还开着'}, prompt=QUESTION)
        self.assertIn(job['state'], ('done', 'applied'), job)
        prompt = self.system_prompt()
        self.assertIn(PROBE, prompt)
        self.assertIn('【跨通道', prompt)

    async def test_console_remember_action_writes_console_channel_and_persona(self):
        job = await self.plan({'type': 'remember', 'key': '饮品', 'value': '无糖乌龙茶'})
        self.assertEqual(job['state'], 'applied', job)
        self.assertEqual([tuple(item) for item in self.row('无糖乌龙茶')],
                         [('console', self.bot.style.active_name)])

    async def test_console_failed_action_lesson_is_console_scoped(self):
        job = await self.plan({'type': 'answer', 'text': '好'}, prompt='预热')
        self.assertIn(job['state'], ('done', 'applied'), job)
        service = self.app.state.console_agent
        with patch.object(service.learning, 'set_preference',
                          side_effect=ValueError('写入被拒绝')):
            failed = await self.plan({'type': 'remember', 'key': '饮品', 'value': '气泡水'})
        self.assertEqual(failed['state'], 'failed_apply', failed)
        rows = self.bot.db.execute(
            "SELECT channel, persona FROM unified_memories WHERE layer='lesson' "
            "AND content LIKE '控制台操作失败%'").fetchall()
        self.assertTrue(rows, '失败教训没有落库')
        self.assertEqual([tuple(item) for item in rows], [('console', self.bot.style.active_name)])

    async def test_console_persona_follows_active_persona_after_switch(self):
        before = self.bot.style.active_name
        job = await self.plan({'type': 'remember', 'key': '饮品', 'value': '无糖乌龙茶'})
        self.assertEqual(job['state'], 'applied', job)
        self.assertEqual([tuple(item) for item in self.row('无糖乌龙茶')], [('console', before)])
        created = await self.plan({'type': 'create_persona', 'persona': PERSONA})
        self.assertEqual(created['state'], 'applied', created)
        name = created['result']['persona_name']
        activated = await self.plan({'type': 'activate_persona', 'name': name})
        self.assertEqual(activated['state'], 'applied', activated)
        self.assertEqual(self.bot.style.active_name, name)
        # 读：旧人设写下的行仍然进提示词，但被标成跨人设（不是无声混入）。
        job = await self.plan({'type': 'answer', 'text': '乌龙茶'}, prompt='无糖乌龙茶')
        self.assertIn(job['state'], ('done', 'applied'), job)
        prompt = self.system_prompt()
        self.assertIn('【同通道/跨人设】', prompt)
        self.assertIn('无糖乌龙茶', prompt)
        # 写：persona 跟着切换后的人设走。
        job = await self.plan({'type': 'remember', 'key': '饮品', 'value': '冰美式'})
        self.assertEqual(job['state'], 'applied', job)
        self.assertEqual([tuple(item) for item in self.row('冰美式')], [('console', name)])

    async def test_console_call_site_passes_console_channel(self):
        seen = []
        real = UnifiedMemory.context_for_prompt

        def spy(instance, *args, **kwargs):
            seen.append((kwargs.get('channel'), kwargs.get('allow_cross_scope'),
                         kwargs.get('persona')))
            return real(instance, *args, **kwargs)

        with patch.object(UnifiedMemory, 'context_for_prompt', spy):
            job = await self.plan({'type': 'answer', 'text': '好'}, prompt='随便聊聊')
        self.assertIn(job['state'], ('done', 'applied'), job)
        self.assertTrue(seen, '控制台链路没有调用 context_for_prompt')
        self.assertEqual({item[0] for item in seen}, {'console'})
        self.assertIn(seen[0][0], CHANNELS)
        self.assertTrue(all(item[1] is True for item in seen))
        self.assertEqual({item[2] for item in seen}, {self.bot.style.active_name})

    async def test_console_survives_invalid_channel_instead_of_leaking_it(self):
        def boom(*args, **kwargs):
            raise ValueError('记忆通道必须是 qq/console/local')

        with patch.object(UnifiedMemory, 'context_for_prompt', boom):
            job = await self.plan({'type': 'answer', 'text': '好'}, prompt=QUESTION)
        self.assertIn(job['state'], ('done', 'applied'), job)
        self.assertNotIn(HEAD, self.system_prompt())


class QQScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build_root(self.temp.name)
        AgentAdmin(self.root / 'agent_admin.json').bind('10001')
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.persona = self.bot.style.active_name

    def tearDown(self):
        asyncio.run(self.bot.close())
        self.temp.cleanup()

    def seed(self, channel='console', persona=None, content=PROBE, layer='fact', project=''):
        return self.bot.unified_memory.remember(
            content, source='desktop', importance=70, layer=layer, project=project,
            channel=channel, persona=self.persona if persona is None else persona)

    @staticmethod
    def group(sender='20002'):
        return {'kind': 'group', 'target': '123456', 'group_id': '123456',
                'sender_id': sender, 'text': QUESTION}

    @staticmethod
    def private(sender='10001'):
        return {'kind': 'private', 'target': sender, 'sender_id': sender, 'text': QUESTION}

    def prompt(self, message, persona=None, first_turn=False):
        return self.bot.memory_prompt(
            message, message.get('target'), self.persona if persona is None else persona,
            QUESTION, first_turn)

    def test_owner_binding_is_real(self):
        self.assertIn('10001', self.bot.owner_ids())

    def test_group_never_shows_console_memory(self):
        # 隐私红线：群聊提示词里绝不能出现控制台写下的记忆正文。
        self.seed('console')
        text = self.prompt(self.group())
        self.assertNotIn(PROBE, text)
        self.assertNotIn('【跨通道', text)

    def test_group_still_sees_qq_side_memory(self):
        self.seed('qq')
        text = self.prompt(self.group())
        self.assertIn(PROBE, text)
        self.assertIn('【同通道/同人设】', text)

    def test_group_first_turn_also_hides_console_memory(self):
        self.seed('console')
        text = self.prompt(self.group(), first_turn=True)
        self.assertNotIn(PROBE, text)
        self.assertNotIn(HEAD, text)

    def test_non_owner_private_never_shows_console_memory(self):
        self.seed('console')
        text = self.prompt(self.private('20002'))
        self.assertNotIn(PROBE, text)

    def test_group_message_without_sender_stays_closed(self):
        # 判不准的场景（没有 sender_id）一律按非主人处理。
        self.seed('console')
        message = {'kind': 'group', 'target': '123456', 'text': QUESTION}
        self.assertNotIn(PROBE, self.prompt(message))

    def test_owner_private_sees_console_memory_marked_cross_channel(self):
        self.seed('console')
        text = self.prompt(self.private())
        self.assertIn(HEAD, text)
        self.assertIn(PROBE, text)
        self.assertIn('【跨通道/同人设】', text)

    def test_owner_private_marks_foreign_persona_cross(self):
        self.seed('qq', persona='别人的脸')
        text = self.prompt(self.private())
        self.assertIn(PROBE, text)
        self.assertIn('【同通道/跨人设】', text)

    def test_empty_persona_falls_back_to_active_persona(self):
        self.seed('qq')
        text = self.prompt(self.private(), persona='')
        self.assertIn(PROBE, text)
        self.assertIn('【同通道/同人设】', text)

    def test_qq_call_site_passes_qq_channel_and_owner_flag(self):
        seen = []
        real = self.bot.unified_memory.context_for_prompt

        def spy(*args, **kwargs):
            seen.append((kwargs.get('channel'), kwargs.get('allow_cross_scope'),
                         kwargs.get('persona')))
            return real(*args, **kwargs)

        with patch.object(self.bot.unified_memory, 'context_for_prompt', spy):
            self.prompt(self.private())
            self.prompt(self.group())
        self.assertEqual([item[0] for item in seen], ['qq', 'qq'])
        self.assertIn('qq', CHANNELS)
        self.assertEqual([item[1] for item in seen], [True, False])
        self.assertEqual({item[2] for item in seen}, {self.persona})


if __name__ == '__main__':
    unittest.main()
