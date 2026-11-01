"""W2 第二层：多轮工具环的真实语义（真文件、真审计）＋ 危险动作仍走 confirm。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src import agent_tools, agent_workspace, tool_loop
from src.agent_scheduler import AgentScheduler
from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root


def answer(text):
    """模型不再调工具、直接给答案（控制台这里就是一段 JSON actions 文本）。"""
    return {'text': text, 'message': {'role': 'assistant', 'content': text},
            'finish_reason': 'stop'}


def tool_reply(*calls, text=''):
    """模型要求调工具：OpenAI 的 message.tool_calls 形状。"""
    items = []
    for index, (name, arguments) in enumerate(calls):
        raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        items.append({'id': 'call_%d' % (index + 1), 'type': 'function',
                      'function': {'name': name, 'arguments': raw}})
    return {'text': text, 'message': {'role': 'assistant', 'content': text, 'tool_calls': items},
            'finish_reason': 'tool_calls'}


class ScriptedEngine:
    """按脚本回放模型回复，并记下每次 chat 真正收到的参数。"""

    replies = []
    calls = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def chat(self, messages, **kwargs):
        type(self).calls.append({'messages': [dict(item) for item in messages], **kwargs})
        if not type(self).replies:
            return answer('（脚本已经用完）')
        item = type(self).replies.pop(0)
        reply = item(messages, kwargs) if callable(item) else item
        # 和真 AIEngine 一样：只有要了 with_message 才给整包，否则只给文本。
        if isinstance(reply, dict) and not kwargs.get('with_message'):
            return reply.get('text', '')
        return reply

    async def close(self):
        pass


class LoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.data_dir = self.base / 'data'
        self.registry = tool_loop.ToolRegistry()
        agent_tools.register_workspace(self.registry, self.data_dir)
        self.gate = agent_tools.DangerGate()
        self.runtime = agent_tools.ConsoleRuntime(self.base, {}, self.registry,
                                                  data_dir=self.data_dir, gate=self.gate)
        ScriptedEngine.replies = []
        ScriptedEngine.calls = []
        self.messages = [{'role': 'user', 'content': '把今天的巡检记到记事本里'}]

    def tearDown(self):
        self.temp.cleanup()

    def tools(self, **overrides):
        options = {'enabled': True, 'max_rounds': 3, 'timeout': 5.0}
        options.update(overrides)
        return agent_tools.ConsoleTools(self.runtime, agent_tools.ConsoleToolSettings(**options))

    def audit_rows(self):
        path = self.data_dir / 'qq-only' / 'tool-loop-audit.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def notebook(self):
        return self.data_dir / 'agent_workspace' / agent_tools.AGENT_ID / 'notebook.txt'

    async def test_read_append_read_round_trip_with_real_files(self):
        agent_workspace.write_text(self.data_dir, agent_tools.AGENT_ID, 'notebook.txt',
                                   '今天值班：鲸鱼娘\n')
        ScriptedEngine.replies = [
            tool_reply(('workspace__read', {'relpath': 'notebook.txt'})),
            tool_reply(('workspace__append',
                        {'relpath': 'notebook.txt', 'text': '22:10 巡检完成\n'})),
            tool_reply(('workspace__read', {'relpath': 'notebook.txt'})),
            answer('{"actions": [{"type": "answer", "text": "已追加一行：22:10 巡检完成"}]}'),
        ]
        text = await self.tools().ask(ScriptedEngine(), self.messages)
        self.assertIn('已追加一行', text)
        self.assertEqual(self.notebook().read_text(encoding='utf-8'),
                         '今天值班：鲸鱼娘\n22:10 巡检完成\n')
        # 工具结果真的回灌进了下一轮上下文：第 2 轮最后一条就是第 1 轮的读取结果。
        self.assertEqual(ScriptedEngine.calls[1]['messages'][-1]['role'], 'tool')
        self.assertIn('今天值班', ScriptedEngine.calls[1]['messages'][-1]['content'])
        self.assertTrue(ScriptedEngine.calls[0]['with_message'])
        self.assertTrue(ScriptedEngine.calls[0]['tools'])
        self.assertEqual([row['tool'] for row in self.audit_rows() if row['phase'] == 'tool'],
                         ['workspace__read', 'workspace__append', 'workspace__read'])

    async def test_审计行字段齐全且来源可区分(self):
        ScriptedEngine.replies = [
            tool_reply(('workspace__write', {'relpath': 'a.txt', 'text': 'x'})),
            answer('写好了'),
        ]
        await self.tools().ask(ScriptedEngine(), self.messages)
        rows = self.audit_rows()
        tool_row = next(row for row in rows if row['phase'] == 'tool')
        for field in ('ts', 'phase', 'tool', 'arguments', 'result', 'source', 'target',
                      'sender', 'elapsed_ms'):
            self.assertIn(field, tool_row, tool_row)
        self.assertEqual(tool_row['tool'], 'workspace__write')
        self.assertEqual(tool_row['arguments'], {'relpath': 'a.txt', 'text': 'x'})
        self.assertEqual(tool_row['source'], 'workspace')   # 工具行记工具自己的来源
        self.assertEqual(tool_row['target'], 'console')
        turn = rows[-1]
        self.assertEqual(turn['phase'], 'turn')
        self.assertEqual(turn['source'], 'console')         # 回合行记控制台
        self.assertEqual(turn['tool'], 'console')
        self.assertEqual(turn['reason'], 'answer')
        self.assertEqual((turn['rounds'], turn['calls']), (1, 1))
        self.assertIn('写好了', turn['result'])

    async def test_工具超时进上下文也进审计(self):
        slow_started = []

        async def slow(name, arguments):
            slow_started.append(name)
            await asyncio.sleep(2.0)
            return {'ok': True}

        self.registry.register('slow', 'wait', slow, description='慢工具')
        ScriptedEngine.replies = [
            tool_reply(('slow__wait', {})),
            answer('慢工具没等到结果，我先说明一下。'),
        ]
        text = await self.tools(timeout=1.0).ask(ScriptedEngine(), self.messages)
        self.assertIn('慢工具没等到结果', text)
        feed = ScriptedEngine.calls[1]['messages'][-1]
        self.assertEqual(feed['role'], 'tool')
        self.assertIn('工具超时（1.0s）', feed['content'])
        row = next(row for row in self.audit_rows() if row['tool'] == 'slow__wait')
        self.assertEqual(row['phase'], 'error')
        self.assertIn('工具超时', row['error'])
        self.assertEqual(slow_started, ['wait'])

    async def test_工具抛错进上下文也进审计(self):
        def broken(name, arguments):
            raise ValueError('磁盘满了')

        self.registry.register('disk', 'full', broken, description='必炸工具')
        ScriptedEngine.replies = [
            tool_reply(('disk__full', {})),
            answer('写不进去，磁盘满了。'),
        ]
        text = await self.tools().ask(ScriptedEngine(), self.messages)
        self.assertIn('磁盘满了', text)
        feed = ScriptedEngine.calls[1]['messages'][-1]['content']
        self.assertIn('工具执行失败：ValueError: 磁盘满了', feed)
        row = next(row for row in self.audit_rows() if row['tool'] == 'disk__full')
        self.assertEqual(row['phase'], 'error')
        self.assertEqual(row['error'], 'ValueError: 磁盘满了')

    async def test_未知工具给出可用清单(self):
        ScriptedEngine.replies = [
            tool_reply(('workspace__rename', {'relpath': 'a.txt'})),
            answer('没有重命名这个工具。'),
        ]
        await self.tools().ask(ScriptedEngine(), self.messages)
        feed = ScriptedEngine.calls[1]['messages'][-1]['content']
        self.assertIn('没有这个工具：workspace__rename', feed)
        self.assertIn('workspace__read', feed)          # 可用工具清单
        row = next(row for row in self.audit_rows() if row['phase'] == 'tool')
        self.assertEqual(row['error'], 'unknown tool')
        self.assertEqual(row['source'], 'console')

    async def test_参数不是合法_JSON_也不静默(self):
        ScriptedEngine.replies = [
            tool_reply(('workspace__read', '{oops')),
            answer('参数没给对。'),
        ]
        await self.tools().ask(ScriptedEngine(), self.messages)
        feed = ScriptedEngine.calls[1]['messages'][-1]['content']
        self.assertIn('参数解析失败', feed)
        row = next(row for row in self.audit_rows() if row['phase'] == 'tool')
        self.assertIn('arguments 不是合法 JSON 对象', row['error'])

    async def test_轮数上限后不再给工具收尾(self):
        ScriptedEngine.replies = [
            tool_reply(('workspace__list', {})),
            tool_reply(('workspace__list', {})),
            answer('好，收尾。'),
        ]
        await self.tools(max_rounds=1).ask(ScriptedEngine(), self.messages)
        self.assertEqual(len(ScriptedEngine.calls), 2)
        self.assertTrue(ScriptedEngine.calls[0]['tools'])
        self.assertIsNone(ScriptedEngine.calls[1]['tools'])   # 到顶那一轮不带工具
        self.assertEqual([row['phase'] for row in self.audit_rows()].count('tool'), 1)

    async def test_机器动作工具被拦下且没有执行(self):
        runs = []

        async def hotkey(name, arguments):
            runs.append(arguments)
            return {'ok': True}

        self.registry.register('pc', 'hotkey', hotkey, description='按下热键')
        ScriptedEngine.replies = [
            tool_reply(('pc__hotkey', {'keys': 'win+r'})),
            answer('这个得你点一次确认。'),
        ]
        text = await self.tools().ask(ScriptedEngine(), self.messages)
        self.assertIn('点一次确认', text)
        self.assertEqual(runs, [])                       # 工具一次都没真的执行
        self.assertEqual(self.gate.blocked, ['pc__hotkey'])
        feed = ScriptedEngine.calls[1]['messages'][-1]['content']
        self.assertIn('这次工具调用被安全策略拦下了', feed)
        self.assertIn('危险动作确认', feed)
        row = next(row for row in self.audit_rows() if row['phase'] == 'blocked')
        self.assertEqual(row['tool'], 'pc__hotkey')
        self.assertIn('会改本机状态', row['error'])

    async def test_工作区写文件不算危险动作(self):
        ScriptedEngine.replies = [
            tool_reply(('workspace__write', {'relpath': 'ok.txt', 'text': 'hi'})),
            answer('写好了'),
        ]
        await self.tools().ask(ScriptedEngine(), self.messages)
        self.assertEqual(self.gate.blocked, [])
        self.assertEqual((self.data_dir / 'agent_workspace' / agent_tools.AGENT_ID
                          / 'ok.txt').read_text(encoding='utf-8'), 'hi')

    async def test_引擎不吃_tools_时交回纯_chat(self):
        class OldEngine:
            async def chat(self, messages, **kwargs):
                if 'tools' in kwargs:
                    raise TypeError("chat() got an unexpected keyword argument 'tools'")
                return answer('老引擎也能答')

        self.assertIsNone(await self.tools().ask(OldEngine(), self.messages))

    async def test_引擎直接回字符串也算数(self):
        class PlainEngine:
            async def chat(self, messages, **kwargs):
                return '就一句话'

        self.assertEqual(await self.tools().ask(PlainEngine(), self.messages), '就一句话')
        self.assertEqual(self.audit_rows()[-1]['reason'], 'plain')

    async def test_时间预算用完就说明原因(self):
        ScriptedEngine.replies = [tool_reply(('workspace__list', {}))]
        with patch.object(agent_tools, 'WALL_BUDGET', 1.0):
            text = await self.tools().ask(ScriptedEngine(), self.messages)
        self.assertEqual(text, tool_loop.FALLBACK_FAILED)
        self.assertEqual(ScriptedEngine.calls, [])       # 预算不够，连模型都不叫
        self.assertEqual(self.audit_rows()[-1]['reason'], 'budget')

    async def test_开关关着不建表不调模型(self):
        tools = self.tools(enabled=False)
        self.assertIsNone(await tools.ask(ScriptedEngine(), self.messages))
        self.assertEqual(ScriptedEngine.calls, [])
        self.assertFalse((self.data_dir / 'qq-only').exists())


class ConsoleConfirmTests(unittest.IsolatedAsyncioTestCase):
    """控制台这一层：工具环开着也好，危险动作仍旧停在 confirm，工具绕不过去。"""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build_root(self.temp.name)
        (self.root / 'config.yaml').write_text(
            'ai:\n  agent_tools: true\n  agent_tools_max_rounds: 2\n'
            '  agent_tools_timeout: 5\n', encoding='utf-8')
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.bot.ai.api_url = 'https://model.example.test/v1/chat/completions'
        self.bot.ai.api_key = 'private-fixture-key'
        self.bot.ai.model = 'running-model'
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.app.state.agent_scheduler = AgentScheduler(self.bot, self.root / 'schedules.json')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')
        ScriptedEngine.replies = []
        ScriptedEngine.calls = []

    async def asyncTearDown(self):
        service = getattr(self.app.state, 'console_agent', None)
        if service:
            await service.close()
        await self.client.aclose()
        await self.bot.close()
        self.temp.cleanup()

    async def wait(self, identifier):
        for _ in range(200):
            response = await self.client.get('/api/assistant/jobs/' + identifier)
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job['state'] not in ('planning', 'applying'):
                return job
            await asyncio.sleep(.01)
        self.fail('任务没有结束')

    def fake_tools(self, hotkey_runs):
        registry = tool_loop.ToolRegistry()
        agent_tools.register_workspace(registry, self.root / 'data')

        async def hotkey(name, arguments):
            hotkey_runs.append(arguments)
            return {'ok': True}

        registry.register('pc', 'hotkey', hotkey, description='按下热键')
        gate = agent_tools.DangerGate()
        runtime = agent_tools.ConsoleRuntime(self.root, self.bot.config, registry,
                                             data_dir=self.root / 'data', gate=gate)
        self.gate = gate
        return agent_tools.ConsoleTools(
            runtime, agent_tools.ConsoleToolSettings(enabled=True, max_rounds=2, timeout=5.0))

    async def test_工具被拦后仍要用确认按钮才执行危险动作(self):
        runs = []
        tools = self.fake_tools(runs)
        ScriptedEngine.replies = [
            tool_reply(('pc__hotkey', {'keys': 'win+r'})),
            answer(json.dumps({'actions': [{'type': 'service', 'running': False},
                                           {'type': 'answer', 'text': '停服务前先点一次确认。'}]},
                              ensure_ascii=False)),
        ]
        with patch('src.console_agent.AIEngine', ScriptedEngine), \
                patch('src.agent_tools.open_tools', lambda bot, **kwargs: tools):
            response = await self.client.post('/api/assistant/jobs', json={'prompt': '把服务停掉'})
            self.assertEqual(response.status_code, 200, response.text)
            job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'confirm', job)      # 一次工具调用没能跳过确认
        self.assertEqual(runs, [])
        self.assertEqual(self.gate.blocked, ['pc__hotkey'])
        self.assertTrue(ScriptedEngine.calls[0]['tools'])
        audit = [json.loads(line) for line in
                 (self.root / 'data' / 'qq-only' / 'tool-loop-audit.jsonl')
                 .read_text(encoding='utf-8').splitlines() if line.strip()]
        self.assertEqual([row['phase'] for row in audit if row['phase'] == 'blocked'], ['blocked'])

    async def test_开关关着时控制台不传_tools_给模型(self):
        ScriptedEngine.replies = [
            answer(json.dumps({'actions': [{'type': 'answer', 'text': '好呀'}]}, ensure_ascii=False))]
        with patch('src.console_agent.AIEngine', ScriptedEngine), \
                patch('src.agent_tools.settings',
                      return_value=agent_tools.ConsoleToolSettings(enabled=False)):
            response = await self.client.post('/api/assistant/jobs', json={'prompt': '你好'})
            self.assertEqual(response.status_code, 200, response.text)
            job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'done', job)   # 纯 answer 直接结束，不进 confirm
        self.assertNotIn('tools', ScriptedEngine.calls[0])
        self.assertNotIn('with_message', ScriptedEngine.calls[0])

    async def test_开关开着但建表失败时回落纯_chat(self):
        ScriptedEngine.replies = [
            answer(json.dumps({'actions': [{'type': 'answer', 'text': '好呀'}]}, ensure_ascii=False))]
        with patch('src.console_agent.AIEngine', ScriptedEngine), \
                patch('src.agent_tools.open_tools', lambda bot, **kwargs: None):
            response = await self.client.post('/api/assistant/jobs', json={'prompt': '你好'})
            self.assertEqual(response.status_code, 200, response.text)
            job = await self.wait(response.json()['id'])
        self.assertEqual(job['state'], 'done', job)
        self.assertNotIn('tools', ScriptedEngine.calls[0])


if __name__ == '__main__':
    unittest.main()
