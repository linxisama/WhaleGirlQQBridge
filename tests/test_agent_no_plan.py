"""方案/逐步确认下线后仍要守住的三件事：撤销、危险动作一次性确认、旧数据容忍。"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src import console_agent as agent_module
from src import local_control
from src.agent_scheduler import AgentScheduler
from src.qq_app import QQRuntime, create_app
from tests.test_console_agent import FakeEngine
from tests.test_persona_files import build_root

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

MACHINE_TYPES = {'service', 'open_path', 'open_url', 'type_text', 'hotkey', 'screenshot'}


class SurfaceTests(unittest.TestCase):
    """方案展示与逐步确认的入口必须消失，底层能力必须留下。"""

    def test_plan_display_symbols_are_gone(self):
        for gone in ('parse_plan', 'describe_plan'):
            self.assertFalse(hasattr(agent_module, gone), gone)
        for gone in ('validate_plan', 'describe_plan', 'fingerprint_plan'):
            self.assertFalse(hasattr(agent_module.ConsoleAgent, gone), gone)

    def test_execution_primitives_are_kept(self):
        self.assertTrue(callable(agent_module.parse_actions))
        for kept in ('validate_actions', 'fingerprints', 'fingerprint', 'confirm', 'undo',
                     '_apply_one', '_undo', '_undo_one'):
            self.assertTrue(callable(getattr(agent_module.ConsoleAgent, kept, None)), kept)

    def test_machine_control_needs_confirm_but_notes_do_not(self):
        self.assertEqual(set(agent_module.DANGEROUS), MACHINE_TYPES)
        for free in ('write_note', 'read_note'):
            self.assertNotIn(free, agent_module.DANGEROUS)


class NoPlanGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from src.gui.assistant import AssistantTab
        from src.gui import assistant as assistant_module
        self.module = assistant_module
        self.tab = AssistantTab('http://127.0.0.1:1', Path.cwd())

    def tearDown(self):
        self.tab.shutdown(); self.tab.close(); self.tab.deleteLater(); self.app.processEvents()

    def test_no_plan_panel_and_no_auto_apply(self):
        for gone in ('preview_title', 'preview', 'empty_preview', 'summary', 'apply_button',
                     'persona_prompt', 'activate', 'draft', '_auto_applied', 'apply'):
            self.assertFalse(hasattr(self.tab, gone), gone)
        self.assertFalse(hasattr(self.module, 'AUTO_EXECUTE'))
        self.assertTrue(hasattr(self.tab, 'confirm'))
        self.assertFalse(self.tab.confirm_button.isEnabled())

    def test_confirm_button_opens_only_while_waiting(self):
        self.tab.present({'id': 'id1', 'state': 'applying', 'message': '正在执行…'})
        self.assertFalse(self.tab.confirm_button.isEnabled())
        self.tab.present({'id': 'id1', 'state': 'confirm', 'message': '这一步要操作你的电脑，需要你点一次确认：\n打开网页'})
        self.assertTrue(self.tab.confirm_button.isEnabled())
        self.tab.present({'id': 'id1', 'state': 'applied', 'message': '已请求打开网址'})
        self.assertFalse(self.tab.confirm_button.isEnabled())


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root/'test.sqlite')
        self.bot.ai.api_url = 'https://model.example.test/v1/chat/completions'
        self.bot.ai.api_key = 'private-fixture-key'
        self.bot.ai.model = 'running-model'
        self.app = create_app(self.bot); self.app.state.bot = self.bot
        self.app.state.agent_scheduler = AgentScheduler(self.bot, self.root/'schedules.json')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1')
        FakeEngine.output = ''; FakeEngine.gate = None; FakeEngine.captured = []; FakeEngine.closed = 0
        self.engine = patch('src.console_agent.AIEngine', FakeEngine); self.engine.start()

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
            if job['state'] not in ('planning', 'applying', 'undoing'):
                return job
            await asyncio.sleep(.01)
        self.fail('任务没有结束')

    async def submit(self, action):
        FakeEngine.output = json.dumps({'action': action}, ensure_ascii=False)
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '请按要求处理'})
        self.assertEqual(response.status_code, 200, response.text)
        return await self.wait(response.json()['id'])

    async def service(self):
        await self.submit({'type': 'settings', 'values': {'reply_timeout': 20}})
        return self.app.state.console_agent

    async def test_dangerous_action_runs_only_after_one_confirm(self):
        with patch.object(local_control, 'open_url') as opened:
            job = await self.submit({'type': 'open_url', 'url': 'https://example.test/'})
            self.assertEqual(job['state'], 'confirm', job)
            self.assertIn('点一次确认', job['message'])
            self.assertEqual(opened.call_count, 0)
            response = await self.client.post(f'/api/assistant/jobs/{job["id"]}/confirm', json={})
            self.assertEqual(response.status_code, 200, response.text)
            done = await self.wait(job['id'])
            self.assertEqual(done['state'], 'applied', done)
            self.assertEqual(opened.call_count, 1)
            again = await self.client.post(f'/api/assistant/jobs/{job["id"]}/confirm', json={})
            self.assertEqual(again.status_code, 200, again.text)
            self.assertEqual(opened.call_count, 1)

    async def test_undo_reverts_the_applied_change(self):
        before = self.bot.config.get('ai.reply_timeout', 20)
        job = await self.submit({'type': 'settings', 'values': {'reply_timeout': 33}})
        self.assertEqual(job['state'], 'applied', job)
        self.assertTrue(job['can_undo'], job)
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), 33)
        response = await self.client.post(f'/api/assistant/jobs/{job["id"]}/undo')
        self.assertEqual(response.status_code, 200, response.text)
        undone = await self.wait(job['id'])
        self.assertEqual(undone['state'], 'undone', undone)
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), before)
        self.assertFalse(undone['can_undo'])

    async def test_undo_is_refused_after_a_later_change(self):
        job = await self.submit({'type': 'settings', 'values': {'reply_timeout': 33}})
        self.assertEqual(job['state'], 'applied', job)
        self.bot.config.set('ai.reply_timeout', 27)
        response = await self.client.post(f'/api/assistant/jobs/{job["id"]}/undo')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('又被改过', response.text)
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), 27)

    async def test_history_has_no_planned_event(self):
        job = await self.submit({'type': 'settings', 'values': {'reply_timeout': 31}})
        self.assertEqual(job['state'], 'applied', job)
        response = await self.client.get('/api/assistant/history')
        self.assertEqual(response.status_code, 200, response.text)
        history = response.json()['history']
        events = [item.get('event') for item in history]
        self.assertNotIn('planned', events)
        self.assertIn('applied', events)

    async def test_legacy_ready_job_without_actions_or_snapshot_still_runs(self):
        service = await self.service()
        service.jobs['legacy-1'] = {'id': 'legacy-1', 'state': 'ready', 'message': '旧的方案',
                                    '_created': time.monotonic(), 'prompt': '旧的要求',
                                    'action': {'type': 'settings', 'values': {'reply_timeout': 41}}}
        polled = await self.client.get('/api/assistant/jobs/legacy-1')
        self.assertEqual(polled.status_code, 200, polled.text)
        response = await self.client.post('/api/assistant/jobs/legacy-1/confirm', json={})
        self.assertEqual(response.status_code, 200, response.text)
        job = await self.wait('legacy-1')
        self.assertEqual(job['state'], 'applied', job)
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), 41)

    async def test_legacy_empty_actions_fall_back_and_broken_actions_do_not_write(self):
        service = await self.service()
        service.jobs['legacy-2'] = {'id': 'legacy-2', 'state': 'ready', 'message': '旧的方案',
                                    '_created': time.monotonic(), 'actions': [],
                                    'action': {'type': 'settings', 'values': {'reply_timeout': 42}}}
        response = await self.client.post('/api/assistant/jobs/legacy-2/confirm', json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.wait('legacy-2'))['state'], 'applied')
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), 42)
        service.jobs['legacy-3'] = {'id': 'legacy-3', 'state': 'ready', 'message': '坏数据',
                                    '_created': time.monotonic(), 'actions': 'oops',
                                    'action': {'type': 'settings', 'values': {'reply_timeout': 43}}}
        response = await self.client.post('/api/assistant/jobs/legacy-3/confirm', json={})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), 42)
        self.assertIsNone(service.fingerprints('oops'))
        self.assertEqual(service.fingerprints([]), [])


if __name__ == '__main__':
    unittest.main()
