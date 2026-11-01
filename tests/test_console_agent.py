"""助手走真实 HTTP 路由与存储；仅模型和 QQ 网络用替身。"""
import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.console_agent import ConsoleAgent, clean_persona, parse_actions
from src.agent_scheduler import AgentScheduler
from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root

PERSONA = {'display_name': '鲸鱼妈妈', 'description': '温柔自然', 'system_prompt': '你是鲸鱼妈妈，温柔可靠，简短自然地接话。',
           'examples': [{'user': '累了', 'assistant': '过来坐一会儿。'}], 'keywords': ['鲸妈妈'],
           'temperature': .7, 'max_tokens': 500}


class FakeEngine:
    output = ''
    gate = None
    captured = []
    closed = 0

    def __init__(self, *args, **kwargs):
        self.args = args; self.kwargs = kwargs

    async def chat(self, messages, **kwargs):
        type(self).captured.append((self.args, self.kwargs, messages, kwargs))
        if type(self).gate:
            await type(self).gate.wait()
        return type(self).output

    async def close(self):
        type(self).closed += 1


class AgentTests(unittest.IsolatedAsyncioTestCase):
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
            if job['state'] not in ('planning', 'applying'):
                return job
            await asyncio.sleep(.01)
        self.fail('任务没有结束')

    async def plan(self, action, **kwargs):
        FakeEngine.output = json.dumps({'action': action}, ensure_ascii=False)
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '请按要求处理', **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        return await self.wait(response.json()['id'])

    async def confirm(self, job):
        # 确认是一次性的：同一任务重复确认只取同一个结果，不会重复执行。
        response = await self.client.post(f'/api/assistant/jobs/{job["id"]}/confirm', json={})
        self.assertEqual(response.status_code, 200, response.text)
        return await self.wait(job['id'])

    async def test_create_persona_activate_restart_and_prompt(self):
        before = self.bot.style.active_name
        job = await self.plan({'type': 'create_persona', 'persona': PERSONA}, mode='persona')
        self.assertEqual(job['state'], 'applied', job)
        name = job['result']['persona_name']
        # 保存不再顺带启用：启用是单独一步 activate_persona。
        self.assertEqual(self.bot.style.active_name, before)
        activated = await self.plan({'type': 'activate_persona', 'name': name})
        self.assertEqual(activated['state'], 'applied', activated)
        self.assertEqual(activated['result']['activated'], True)
        self.assertEqual(self.bot.style.active_name, name)
        self.assertIn(PERSONA['system_prompt'], self.bot.style.full_prompt())
        self.assertIn('过来坐一会儿', self.bot.style.full_prompt())
        again = await self.confirm(job)
        self.assertEqual(again['state'], 'applied', again)
        self.assertEqual(again['result']['persona_name'], name)
        self.assertEqual(len(list((self.root/'styles/custom').glob('agent_*.yaml'))), 1)
        restarted = QQRuntime(root=self.root, db_path=self.root/'restart.sqlite')
        try:
            self.assertEqual(restarted.style.active_name, name)
            self.assertIn(PERSONA['system_prompt'], restarted.style.full_prompt())
        finally:
            await restarted.close()

    async def test_save_without_activate_preserves_existing_and_duplicate_names(self):
        before = self.bot.style.active_name
        names = []
        for _ in range(2):
            job = await self.plan({'type': 'create_persona', 'persona': PERSONA})
            self.assertEqual(job['state'], 'applied', job)
            names.append(job['result']['persona_name'])
        self.assertNotEqual(*names)
        self.assertEqual(self.bot.style.active_name, before)

    async def test_agent_creates_schedule_and_toggles_skill(self):
        job = await self.plan({'type': 'schedule', 'name': '连接检查',
                               'action': 'connection_check', 'every_minutes': 30, 'enabled': True})
        # 不需要确认的动作在提交时就已经执行完，没有"待应用"中间态。
        self.assertEqual(job['state'], 'applied', job)
        tasks = self.app.state.agent_scheduler.list()
        self.assertEqual(tasks[0]['name'], '连接检查')
        undone = await self.client.post(f'/api/assistant/jobs/{job["id"]}/undo')
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(self.app.state.agent_scheduler.list(), [])

        service = self.app.state.console_agent
        service.learning.import_skill({'format': 'whale-agent-skill', 'version': 1,
                                       'id': 'tone', 'name': '语气', 'instructions': '简短。'})
        skill_job = await self.plan({'type': 'skill', 'id': 'tone', 'enabled': False})
        self.assertEqual(skill_job['state'], 'applied', skill_job)
        self.assertFalse(service.learning.overview()['skills'][0]['enabled'])

    async def test_settings_are_patch_only_and_persist_without_restart(self):
        old = copy.deepcopy(self.bot.config.config)
        with patch.object(self.bot, 'start', AsyncMock()) as start, patch.object(self.bot, 'stop', AsyncMock()) as stop:
            # 执行发生在提交过程中，所以"没有启停服务"要在提交期间观察。
            job = await self.plan({'type': 'settings', 'values': {'reply_timeout': 45, 'reply_max_tokens': 300}})
        self.assertEqual(job['state'], 'applied', job)
        self.assertFalse(start.called or stop.called)
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), 45)
        current = copy.deepcopy(self.bot.config.config)
        for key in ('reply_timeout', 'reply_max_tokens'):
            if key in old['ai']: current['ai'][key] = old['ai'][key]
            else: current['ai'].pop(key)
        self.assertEqual(current, old)

    async def test_settings_save_failure_rolls_memory_back(self):
        old = copy.deepcopy(self.bot.config.config)
        with patch('src.qq_app._persist_config', side_effect=OSError('disk full')):
            job = await self.plan({'type': 'settings', 'values': {'reply_timeout': 45}})
        self.assertEqual(job['state'], 'failed_apply', job)
        self.assertEqual(self.bot.config.config, old)

    async def test_stale_settings_rejected_but_unrelated_changes_allowed(self):
        original = ConsoleAgent._execute

        async def racing(service, job, actions):
            # 没有"预览之后再确认"的等待窗口了，于是直接在执行瞬间制造一次并发改动。
            self.bot.config.set('ai.reply_timeout', 40)
            return await original(service, job, actions)

        with patch.object(ConsoleAgent, '_execute', racing):
            job = await self.plan({'type': 'settings', 'values': {'reply_timeout': 45}})
        self.assertEqual(job['state'], 'failed_apply', job)
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), 40)

        async def unrelated(service, job, actions):
            self.bot.config.set('ai.reply_max_tokens', 200)
            return await original(service, job, actions)

        with patch.object(ConsoleAgent, '_execute', unrelated):
            job = await self.plan({'type': 'settings', 'values': {'reply_timeout': 45}})
        self.assertEqual(job['state'], 'applied', job)
        self.assertEqual(self.bot.config.get('ai.reply_max_tokens'), 200)

    async def test_plugin_toggle_preserves_its_other_settings(self):
        previous = self.bot.plugins.get_settings('image_understanding')
        job = await self.plan({'type': 'plugin', 'id': 'image_understanding', 'enabled': False})
        self.assertEqual(job['state'], 'applied', job)
        self.assertEqual(self.bot.plugins.get_settings('image_understanding'), dict(previous, enabled=False))

    async def test_activate_and_navigate_existing_targets(self):
        job = await self.plan({'type': 'create_persona', 'persona': PERSONA})
        name = job['result']['persona_name']
        job = await self.plan({'type': 'activate_persona', 'name': name})
        self.assertEqual(job['result']['activated'], True)
        job = await self.plan({'type': 'navigate', 'page': '外观'})
        self.assertEqual(job['result']['page'], '外观')

    async def test_plain_text_is_conversation_and_answer_can_mix_with_action(self):
        FakeEngine.output = '你好，我在。'
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '你好'})
        job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'done')
        self.assertIn('你好', job['message'])
        FakeEngine.output = json.dumps({'actions': [
            {'type': 'settings', 'values': {'reply_timeout': 25}},
            {'type': 'answer', 'text': '调好了，之后回复会多等一会儿。'}]}, ensure_ascii=False)
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '把等待调长一点并告诉我'})
        job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'applied', job)
        self.assertEqual(len(job['actions']), 2)
        self.assertIn('调好了', job['actions'][1]['text'])
        self.assertEqual(self.bot.config.get('ai.reply_timeout'), 25)
        self.assertIn('调好了', job['message'])

    async def test_local_action_waits_for_one_confirm_then_executes(self):
        FakeEngine.output = json.dumps({'actions': [
            {'type': 'open_url', 'url': 'https://example.test/'},
            {'type': 'answer', 'text': '准备打开。'}]}, ensure_ascii=False)
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '打开网页'})
        job = await self.wait(response.json()['id'])
        # 本机操控动作在用户点确认之前一步都不执行：没有结果，也没有任何副作用记录。
        self.assertEqual(job['state'], 'confirm', job)
        self.assertEqual(job['actions'][0]['type'], 'open_url')
        self.assertNotIn('result', job)
        self.assertIn('点一次确认', job['message'])
        service = self.app.state.console_agent
        with patch.object(service, '_apply_one',
                          AsyncMock(return_value=('已请求打开网址：https://example.test/', {}))) as apply_one:
            confirmed = await self.confirm(job)
        self.assertEqual(confirmed['state'], 'applied', confirmed)
        self.assertEqual(apply_one.await_count, 2)

    async def test_expired_and_cancelled_confirm_cannot_execute(self):
        opener = {'type': 'open_url', 'url': 'https://example.test/'}
        job = await self.plan(opener)
        self.assertEqual(job['state'], 'confirm', job)
        self.app.state.console_agent.jobs[job['id']]['_created'] -= 601
        response = await self.client.post(f'/api/assistant/jobs/{job["id"]}/confirm', json={})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('10 分钟', response.text)
        job = await self.plan(opener)
        self.assertEqual(job['state'], 'confirm', job)
        response = await self.client.post(f'/api/assistant/jobs/{job["id"]}/cancel')
        self.assertEqual(response.json()['state'], 'cancelled')
        self.assertEqual((await self.client.post(f'/api/assistant/jobs/{job["id"]}/confirm', json={})).status_code, 400)

    async def test_slow_model_is_cancellable_and_leaves_console_responsive(self):
        FakeEngine.gate = asyncio.Event()
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '帮我生成人设'})
        identifier = response.json()['id']
        await asyncio.sleep(.02)
        self.assertEqual((await self.client.get('/api/status')).status_code, 200)
        busy = await self.client.post('/api/assistant/jobs', json={'prompt': '另一项'})
        self.assertEqual(busy.status_code, 400)
        cancelled = await self.client.post(f'/api/assistant/jobs/{identifier}/cancel')
        self.assertEqual(cancelled.json()['state'], 'cancelled')
        await asyncio.sleep(.02)
        self.assertEqual(FakeEngine.closed, 1)

    async def test_shutdown_releases_pending_model(self):
        FakeEngine.gate = asyncio.Event()
        await self.client.post('/api/assistant/jobs', json={'prompt': '帮我生成人设'})
        await asyncio.sleep(.02)
        await self.app.state.console_agent.close()
        self.assertFalse(self.app.state.console_agent.tasks)
        self.assertEqual(FakeEngine.closed, 1)

    async def test_request_id_prevents_duplicate_model_generation(self):
        FakeEngine.gate = asyncio.Event()
        body = {'prompt': '生成', 'request_id': 'a'*32}
        first = await self.client.post('/api/assistant/jobs', json=body)
        second = await self.client.post('/api/assistant/jobs', json=body)
        await asyncio.sleep(.02)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(len(FakeEngine.captured), 1)

    async def test_confirm_duplicate_cannot_repeat_service_start(self):
        FakeEngine.output = ''
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '开启机器人'})
        job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'confirm', job)
        with patch.object(self.bot, 'start', AsyncMock()) as start:
            await self.confirm(job); await self.confirm(job)
            self.assertEqual(start.await_count, 1)
        self.assertEqual(FakeEngine.captured, [])

    async def test_status_does_not_call_model_or_include_messages(self):
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '查看状态'})
        job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'done')
        self.assertEqual(FakeEngine.captured, [])
        self.assertNotIn('messages', self.app.state.console_agent.context())

    async def test_engine_uses_running_connection_not_disk_and_minimal_context(self):
        self.bot.config.set('ai.model', 'disk-other-model')
        job = await self.plan({'type': 'answer', 'text': '请先说明要调整什么。'})
        self.assertEqual(job['state'], 'done')
        args, opts, messages, _ = FakeEngine.captured[0]
        self.assertEqual(opts['model'], 'running-model')
        context = messages[0]['content']
        self.assertNotIn('private-fixture-key', context)
        self.assertNotIn('api_url', context)
        self.assertNotIn('messages', context)
        self.assertEqual(opts['max_retries'], 1)

    async def test_followup_gets_previous_user_and_assistant_messages(self):
        FakeEngine.output = json.dumps({'actions': [{'type': 'answer', 'text': '我可以改成安全版本，你确认吗？'}]},
                                       ensure_ascii=False)
        first = await self.client.post('/api/assistant/jobs', json={'prompt': '生成一个角色'})
        first_job = await self.wait(first.json()['id'])
        self.assertEqual(first_job['state'], 'done')
        second = await self.client.post('/api/assistant/jobs', json={
            'prompt': '也行', 'session_id': first_job['session_id']})
        second_job = await self.wait(second.json()['id'])
        self.assertEqual(second_job['state'], 'done')
        messages = FakeEngine.captured[-1][2]
        self.assertEqual(messages[0]['role'], 'user')
        self.assertEqual(messages[0]['content'], '生成一个角色')
        self.assertEqual(messages[-2]['role'], 'assistant')
        self.assertIn('安全版本', messages[-2]['content'])
        self.assertEqual(messages[-1]['role'], 'user')
        self.assertIn('"request": "也行"', messages[-1]['content'])

    async def test_manual_draft_edits_are_in_followup(self):
        first = await self.plan({'type': 'create_persona', 'persona': PERSONA})
        edited = dict(PERSONA, system_prompt='手动编辑过的正文')
        await self.plan({'type': 'create_persona', 'persona': PERSONA}, previous_id=first['id'], draft=edited)
        payload = json.loads(FakeEngine.captured[-1][2][-1]['content'])
        self.assertEqual(payload['previous_draft']['system_prompt'], '手动编辑过的正文')

    async def test_unconfigured_can_use_local_commands(self):
        self.bot.ai.api_key = ''
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '停止机器人'})
        # 启停机器人属于本机操控，先落到待确认，不会直接执行。
        self.assertEqual((await self.wait(response.json()['id']))['state'], 'confirm')
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '生成人设'})
        self.assertEqual((await self.wait(response.json()['id']))['state'], 'failed')

    async def test_unsafe_or_invalid_model_plans_do_not_mutate(self):
        old = copy.deepcopy(self.bot.config.config)
        for action in ({'type': 'exec', 'command': 'whoami'}, {'type': 'service', 'running': 'false'},
                       {'type': 'settings', 'values': {'api_key': 'replace'}},
                       {'type': 'settings', 'values': {'reply_timeout': float('nan')}},
                       {'type': 'plugin', 'id': 'pc_control', 'enabled': True},
                       {'type': 'create_persona', 'persona': dict(PERSONA, image='C:/private.png')},
                       {'type': 'activate_persona', 'name': '../../default'},
                       {'type': 'service', 'running': True, 'command': 'delete'},
                       {'type': 'navigate', 'page': 'https://attacker.test'}):
            with self.subTest(action=action):
                job = await self.plan(action)
                self.assertEqual(job['state'], 'failed', job)
        self.assertEqual(self.bot.config.config, old)

    async def test_persona_mode_rejects_control_action_and_banned_words(self):
        job = await self.plan({'type': 'service', 'running': True}, mode='persona')
        self.assertEqual(job['state'], 'failed')
        self.bot.style.set_banned_words(['温柔'])
        job = await self.plan({'type': 'create_persona', 'persona': PERSONA})
        self.assertEqual(job['state'], 'failed')
        self.assertFalse(list((self.root/'styles/custom').glob('agent_*.yaml')))

    async def test_cross_origin_and_forged_apply_are_rejected(self):
        response = await self.client.post('/api/assistant/jobs', json={'prompt': '停止机器人'}, headers={'Origin': 'https://elsewhere.test'})
        self.assertEqual(response.status_code, 403)
        job = await self.plan({'type': 'service', 'running': False})
        self.assertEqual(job['state'], 'confirm', job)
        # 前端不能夹带自己的动作：确认接口只接受 confirm 字段。
        for path in ('apply', 'confirm'):
            with self.subTest(path=path):
                response = await self.client.post(f'/api/assistant/jobs/{job["id"]}/{path}',
                                                  json={'action': {'type': 'service', 'running': True}})
                self.assertEqual(response.status_code, 400, response.text)

    async def test_empty_malformed_and_large_requests_fail_cleanly(self):
        for data in ([], {'prompt': ''}, {'prompt': 'x'*4001}, {'prompt': 'hi', 'previous_id': []},
                     {'prompt': 'hi', 'request_id': ['bad']}, {'prompt': 'hi', 'request_id': []}):
            response = await self.client.post('/api/assistant/jobs', json=data)
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.client.post('/api/assistant/jobs', content=b'x'*64001)
        self.assertEqual(response.status_code, 400)

    async def test_real_engine_openai_and_anthropic_response_parsing(self):
        from src.ai_engine import AIEngine
        raw = json.dumps({'action': {'type': 'create_persona', 'persona': PERSONA}}, ensure_ascii=False)
        for style in ('openai', 'anthropic'):
            self.bot.ai.api_style = style
            payloads = []
            async def respond(request):
                payloads.append(json.loads(request.content))
                if style == 'anthropic':
                    return httpx.Response(200, json={'content': [{'type': 'text', 'text': raw}],
                        'stop_reason': 'end_turn', 'usage': {'input_tokens': 100, 'output_tokens': 120}})
                return httpx.Response(200, json={'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 120}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with patch('src.console_agent.AIEngine', AIEngine), patch.object(AIEngine, '_get_client', AsyncMock(return_value=client)):
                    response = await self.client.post('/api/assistant/jobs', json={'prompt': '生成人设', 'mode': 'persona'})
                    job = await self.wait(response.json()['id'])
            self.assertEqual(job['state'], 'applied', job)
            self.assertEqual(payloads[0]['model'], 'running-model')
            self.assertEqual(job['action']['persona']['system_prompt'], PERSONA['system_prompt'])


    async def test_recall_memory_includes_recent_session_turns(self):
        service = ConsoleAgent(self.bot)
        session = service.sessions.create('海边')
        service.sessions.add_turn(session['id'], '我昨天去了海边', 'chat', '海风很大')
        lines = service.recall_memory('海边')
        self.assertIn('[会话]', lines)
        self.assertIn('海边', lines)


class ParsingTests(unittest.TestCase):
    def test_fenced_json_and_invalid_json(self):
        actions = parse_actions('```json\n{"action":{"type":"service","running":false}}\n```')
        self.assertEqual(actions, [{'type': 'service', 'running': False}])
        # 多动作形状走同一个解析入口，一步到三步。
        self.assertEqual(len(parse_actions('{"actions":[{"type":"answer","text":"a"},{"type":"answer","text":"b"}]}')), 2)
        for raw in ('not json', '[]', '{"action":[]}', '{}', '{"action":{},"exec":1}',
                    '{"actions":[]}',
                    '{"actions":[{"type":"answer"},{"type":"answer"},{"type":"answer"},{"type":"answer"}]}'):
            with self.assertRaises(ValueError): parse_actions(raw)

    def test_persona_rejects_paths_empty_prompt_bad_numbers(self):
        for fields in ({'system_prompt': ''}, {'temperature': float('inf')}, {'max_tokens': True},
                       {'original_name': 'default'}, {'examples': ['bad']}, {'keywords': 'name'}):
            with self.assertRaises(ValueError): clean_persona(dict(PERSONA, **fields))


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class AssistantGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from src.gui.assistant import AssistantTab
        self.tab = AssistantTab('http://127.0.0.1:1', Path.cwd())

    def tearDown(self):
        self.tab.shutdown(); self.tab.close(); self.tab.deleteLater(); self.app.processEvents()

    def awaiting(self):
        self.tab.present({'id': 'id1', 'state': 'confirm',
                          'message': '这一步要操作你的电脑，需要你点一次确认：\n打开网页',
                          'actions': [{'type': 'open_url', 'url': 'https://example.test/'}]})

    def test_confirm_is_one_click_and_only_while_waiting(self):
        self.awaiting()
        self.assertTrue(self.tab.confirm_button.isEnabled())
        with patch.object(self.tab, '_request') as request:
            self.tab.confirm()
        self.assertEqual(request.call_args.args[1], 'assistant/jobs/id1/confirm')
        self.tab.present({'id': 'id1', 'state': 'applied', 'message': '已请求打开网址'})
        self.assertFalse(self.tab.confirm_button.isEnabled())

    def test_chat_only_no_plan_panel_and_send_has_no_draft(self):
        for gone in ('preview_title', 'preview', 'summary', 'apply_button', 'persona_prompt', 'activate'):
            self.assertFalse(hasattr(self.tab, gone), gone)
        self.awaiting()
        # 待确认提示写在对话里，而不是打开右侧面板。
        self.assertIn('允许这次操作', self.tab.history.toPlainText())
        self.tab.prompt.setPlainText('活泼一点')
        with patch.object(self.tab, '_request') as request:
            self.tab.send()
        payload = request.call_args.args[2]
        self.assertEqual(payload['prompt'], '活泼一点')
        self.assertNotIn('draft', payload)
        self.assertNotIn('previous_id', payload)

    def test_result_once_and_plain_text_no_html_interpretation(self):
        seen = []; self.tab.navigate.connect(seen.append)
        result = {'id': 'x', 'state': 'applied', 'message': '<b>并未加粗</b>', 'result': {'page': '外观'}}
        self.tab.present(result); self.tab.present(result)
        self.assertEqual(seen, ['外观'])
        self.assertIn('<b>并未加粗</b>', self.tab.history.toPlainText())

    def test_pending_request_does_not_block_qt_and_shutdown_aborts(self):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtTest import QTest
        ticks = []; timer = QTimer(); timer.timeout.connect(lambda: ticks.append(1)); timer.start(10)
        self.tab.prompt.setPlainText('查看状态'); self.tab.send()
        # 固定等 80ms 在满载机器上只能抢到 2 次 tick（实测会偶发失败）。
        # 改成"等到 tick 超过 2 次或 2 秒超时"：断言仍是 >2，只是不再依赖机器负载。
        import time as _time
        deadline = _time.monotonic() + 2
        while len(ticks) <= 2 and _time.monotonic() < deadline:
            QTest.qWait(10)
        self.tab.shutdown(); timer.stop()
        self.assertGreater(len(ticks), 2)
        self.assertIsNone(self.tab.pending)
        self.assertFalse(self.tab.timer.isActive())

    def test_narrow_layout_keeps_chat_and_confirm_button(self):
        self.awaiting(); self.tab.resize(780, 450); self.tab.show(); self.app.processEvents()
        self.assertFalse(self.tab.send_button.visibleRegion().isEmpty())
        self.assertTrue(self.tab.confirm_button.isEnabled())
