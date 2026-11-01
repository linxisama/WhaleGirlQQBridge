"""学习 Agent 的路由：多步计划、反馈、技能包导入、长期偏好。"""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

import httpx

from src.qq_app import QQRuntime, create_app
from tests.test_console_agent import FakeEngine
from tests.test_persona_files import build_root


class LearningAgentRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.bot.ai.api_url = 'https://model.example.test/v1/chat/completions'
        self.bot.ai.api_key = 'private-fixture-key'
        self.bot.ai.model = 'running-model'
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')
        FakeEngine.output = ''
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
        for _ in range(200):
            job = (await self.client.get('/api/assistant/jobs/' + identifier)).json()
            if job['state'] not in ('planning', 'applying'):
                return job
            await asyncio.sleep(.01)
        self.fail('任务没有结束')

    async def plan(self, actions):
        FakeEngine.output = json.dumps({'actions': actions}, ensure_ascii=False)
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '把回复调快并打开表情插件'})
        self.assertEqual(response.status_code, 200, response.text)
        return await self.wait(response.json()['id'])

    async def test_multi_step_plan_applies_in_order(self):
        job = await self.plan([
            {'type': 'settings', 'values': {'reply_timeout': 15}},
            {'type': 'plugin', 'id': 'memes', 'enabled': True},
            {'type': 'navigate', 'page': '插件'},
        ])
        # 提交即执行：非本机操控动作不再停在待确认态。
        self.assertEqual(job['state'], 'applied')
        self.assertEqual(len(job['actions']), 3)
        applied = await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
        self.assertEqual(applied.status_code, 200, applied.text)
        done = await self.wait(job['id'])
        self.assertEqual(done['state'], 'applied')
        self.assertEqual(len(done['result']['steps']), 3)
        self.assertEqual(done['result']['page'], '插件')
        self.assertAlmostEqual(float(self.bot.config.get('ai.reply_timeout')), 15.0)
        self.assertTrue(self.bot.plugins.get_settings('memes')['enabled'])

    async def test_two_heavy_steps_are_rejected(self):
        job = await self.plan([{'type': 'service', 'running': True},
                               {'type': 'activate_persona', 'name': 'default'}])
        self.assertEqual(job['state'], 'failed')
        self.assertIn('重操作', job['message'])

    async def test_extra_settings_apply_and_undo(self):
        old_timeout = float(self.bot.config.get('ai.reply_timeout', 20) or 20)
        job = await self.plan([{'type': 'settings', 'values': {
            'context_budget_tokens': 3000, 'history_limit': 30, 'adaptive_max_tokens': False}}])
        self.assertEqual(job['state'], 'applied')
        await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
        done = await self.wait(job['id'])
        self.assertEqual(done['state'], 'applied')
        self.assertTrue(done['can_undo'])
        self.assertEqual(int(self.bot.config.get('ai.context_budget_tokens')), 3000)
        self.assertEqual(int(self.bot.config.get('ai.history_max_messages')), 30)
        self.assertFalse(bool(self.bot.config.get('ai.adaptive_max_tokens')))

        response = await self.client.post(f"/api/assistant/jobs/{job['id']}/undo")
        self.assertEqual(response.status_code, 200, response.text)
        undone = await self.wait(job['id'])
        self.assertEqual(undone['state'], 'undone')
        self.assertEqual(int(self.bot.config.get('ai.context_budget_tokens')), 2200)
        self.assertEqual(int(self.bot.config.get('ai.history_max_messages')), 24)
        self.assertTrue(bool(self.bot.config.get('ai.adaptive_max_tokens')))
        self.assertAlmostEqual(float(self.bot.config.get('ai.reply_timeout')), old_timeout)

    async def test_undo_counts_as_negative_feedback(self):
        job = await self.plan([{'type': 'settings', 'values': {'reply_timeout': 16}}])
        await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
        await self.wait(job['id'])
        await self.client.post(f"/api/assistant/jobs/{job['id']}/undo")
        await self.wait(job['id'])
        overview = (await self.client.get('/api/assistant/learning')).json()
        lesson = overview['lessons'][0]
        self.assertGreaterEqual(lesson['fail'], 1)
        self.assertIn('撤销', lesson['note'])

    async def test_plugin_toggle_undo(self):
        was = bool(self.bot.plugins.get_settings('memes')['enabled'])
        job = await self.plan([{'type': 'plugin', 'id': 'memes', 'enabled': not was}])
        await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
        await self.wait(job['id'])
        self.assertEqual(bool(self.bot.plugins.get_settings('memes')['enabled']), not was)
        response = await self.client.post(f"/api/assistant/jobs/{job['id']}/undo")
        self.assertEqual(response.status_code, 200, response.text)
        await self.wait(job['id'])
        self.assertEqual(bool(self.bot.plugins.get_settings('memes')['enabled']), was)

    async def test_undo_refuses_after_later_change(self):
        job = await self.plan([{'type': 'settings', 'values': {'reply_timeout': 15}}])
        await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
        await self.wait(job['id'])
        self.bot.config.set('ai.reply_timeout', 25)
        response = await self.client.post(f"/api/assistant/jobs/{job['id']}/undo")
        self.assertEqual(response.status_code, 400)
        self.assertIn('又被改过', response.json()['detail'])

    async def test_high_confidence_lesson_is_reused_without_model(self):
        prompt = {'prompt': '把回复调快并打开表情插件'}
        for _ in range(2):
            FakeEngine.output = json.dumps({'actions': [
                {'type': 'settings', 'values': {'reply_timeout': 17}}]}, ensure_ascii=False)
            response = await self.client.post('/api/assistant/jobs', json=prompt)
            job = await self.wait(response.json()['id'])
            await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
            await self.wait(job['id'])
        before = len(FakeEngine.captured)
        FakeEngine.output = ''
        response = await self.client.post('/api/assistant/jobs', json=prompt)
        job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'applied')
        self.assertTrue(job.get('reused'), job)
        self.assertEqual(len(FakeEngine.captured), before)  # 没有多花一次模型调用
        self.assertIn('复用上次', job['message'])

    async def test_recall_memory_action_reads_local_memory(self):
        self.bot.db.execute("INSERT INTO self_memories VALUES('m1',?,'用户喜欢鲸鱼',1,1)",
                            (self.bot.style.active_name,))
        self.bot.db.commit()
        FakeEngine.output = json.dumps({'actions': [
            {'type': 'recall_memory', 'query': '鲸鱼', 'limit': 5}]}, ensure_ascii=False)
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '机器人记得我什么'})
        job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'done')
        self.assertIn('用户喜欢鲸鱼', job['message'])
        self.assertIn('没有改动', job['message'])

    async def test_recall_memory_shortcut_skips_model(self):
        before = len(FakeEngine.captured)
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '机器人记得什么'})
        job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'done')
        self.assertTrue('机器人记得这些' in job['message'] or '还没有相关记忆' in job['message'])
        self.assertEqual(len(FakeEngine.captured), before)

    async def test_sessions_create_list_rename_and_delete(self):
        first = (await self.client.get('/api/assistant/sessions')).json()['sessions']
        self.assertTrue(first)
        created = (await self.client.post('/api/assistant/sessions', json={'title': '第二段'})).json()['session']
        self.assertEqual(created['title'], '第二段')
        listing = (await self.client.get('/api/assistant/sessions')).json()['sessions']
        self.assertEqual(listing[0]['id'], created['id'])
        renamed = (await self.client.patch(f"/api/assistant/sessions/{created['id']}",
                                           json={'title': '改名了'})).json()['session']
        self.assertEqual(renamed['title'], '改名了')
        deleted = (await self.client.delete(f"/api/assistant/sessions/{created['id']}")).json()
        self.assertTrue(deleted['removed'])
        self.assertNotIn(created['id'], [item['id'] for item in deleted['sessions']])

    async def test_submit_keeps_turns_in_selected_session(self):
        created = (await self.client.post('/api/assistant/sessions', json={'title': '任务卡片'})).json()['session']
        response = await self.client.post('/api/assistant/jobs', json={
            'prompt': '打开表情插件', 'session_id': created['id']})
        job = await self.wait(response.json()['id'])
        sessions = (await self.client.get('/api/assistant/sessions')).json()['sessions']
        item = next(s for s in sessions if s['id'] == created['id'])
        self.assertEqual(item['title'], '任务卡片')
        self.assertGreaterEqual(item['turns'], 1)

    async def test_history_endpoint_lists_applied_plan(self):
        job = await self.plan([{'type': 'settings', 'values': {'reply_timeout': 16}}])
        await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
        await self.wait(job['id'])
        response = await self.client.get('/api/assistant/history')
        self.assertEqual(response.status_code, 200, response.text)
        events = [item['event'] for item in response.json()['history']]
        self.assertIn('applied', events)
        # 「展示方案」已下线：历史里只剩执行结果，不再有 planned 事件。
        self.assertNotIn('planned', events)

    async def test_feedback_trains_lesson(self):
        job = await self.plan([{'type': 'settings', 'values': {'reply_timeout': 18}}])
        await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
        await self.wait(job['id'])
        response = await self.client.post(f"/api/assistant/jobs/{job['id']}/feedback",
                                          json={'score': 1, 'note': '就是这样'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['stats']['lessons'], 1)
        overview = (await self.client.get('/api/assistant/learning')).json()
        self.assertEqual(overview['lessons'][0]['ok'], 2)  # 应用一次 + 反馈一次
        self.assertIn('把回复调快', overview['lessons'][0]['intent'])

        response = await self.client.post(f"/api/assistant/jobs/{job['id']}/feedback",
                                          json={'score': -1})
        self.assertEqual(response.status_code, 200)
        overview = (await self.client.get('/api/assistant/learning')).json()
        self.assertEqual(overview['lessons'][0]['fail'], 1)

    async def test_learning_maintain_endpoint(self):
        response = await self.client.post('/api/assistant/learning/maintain')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['success'])
        self.assertIn('removed', response.json())

    async def test_skill_preview_permissions_and_toggle(self):
        package = {'format': 'whale-agent-skill', 'version': 1, 'id': 'perm_skill',
                   'name': '权限技能', 'instructions': '测试权限。', 'triggers': ['权限'],
                   'permissions': ['read_memory']}
        preview = await self.client.post('/api/assistant/skills/preview', json={'package': package})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()['skill']['permissions'], ['read_memory'])
        imported = await self.client.post('/api/assistant/skills/import', json={'package': package})
        self.assertEqual(imported.status_code, 200, imported.text)
        toggled = await self.client.post('/api/assistant/skills/perm_skill/toggle',
                                         json={'enabled': False})
        self.assertEqual(toggled.status_code, 200, toggled.text)
        skill = toggled.json()['skills'][0]
        self.assertFalse(skill['enabled'])
        bad = await self.client.post('/api/assistant/skills/preview',
                                     json={'package': dict(package, permissions=['root'])})
        self.assertEqual(bad.status_code, 400)

    async def test_skill_pack_import_and_validation(self):
        package = {'format': 'whale-agent-skill', 'version': 1, 'id': 'timeout_help',
                   'name': '超时排查', 'instructions': '先问清是首条慢还是全部都慢。',
                   'triggers': ['超时', '回复慢']}
        response = await self.client.post('/api/assistant/skills/import', json={'package': package})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['skills'][0]['id'], 'timeout_help')
        bad = await self.client.post('/api/assistant/skills/import', json={'package': {'format': 'x'}})
        self.assertEqual(bad.status_code, 400)
        overview = (await self.client.get('/api/assistant/learning')).json()
        self.assertEqual(overview['stats']['skills'], 1)

    async def test_remember_action_stores_preference(self):
        job = await self.plan([{'type': 'remember', 'key': '称呼', 'value': '老鲸'}])
        self.assertEqual(job['state'], 'applied')
        await self.client.post(f"/api/assistant/jobs/{job['id']}/apply", json={})
        done = await self.wait(job['id'])
        self.assertEqual(done['state'], 'applied')
        overview = (await self.client.get('/api/assistant/learning')).json()
        self.assertEqual(overview['preferences'][0]['key'], '称呼')
        self.assertEqual(overview['preferences'][0]['value'], '老鲸')

    async def test_forget_lesson_and_preference(self):
        response = await self.client.post('/api/assistant/preferences',
                                          json={'key': '语气', 'value': '简短'})
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.post('/api/assistant/learning/forget', json={'preference': '语气'})
        self.assertTrue(response.json()['removed'])
        self.assertEqual((await self.client.get('/api/assistant/learning')).json()['preferences'], [])


if __name__ == '__main__':
    unittest.main()
