"""W2 第一层：配置口径、工作区工具注册、危险动作闸门（不起模型、不起 node）。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import agent_tools, tool_loop


async def call_tool(registry, name, arguments=None, timeout=5.0):
    """按 tool_loop 的调用方式取一次工具结果（同步 handler 丢线程，和 run_calls 一致）。"""
    spec = registry.get(name)
    if spec is None:
        return None
    return await asyncio.to_thread(spec.handler, spec.raw_name, arguments or {})


class SettingsTests(unittest.TestCase):
    def test_defaults_when_no_config(self):
        got = agent_tools.settings(None)
        self.assertFalse(got.enabled)
        self.assertEqual(got.max_rounds, 3)
        self.assertEqual(got.timeout, 15.0)

    def test_missing_keys_fall_back_to_defaults(self):
        got = agent_tools.settings({'ai.tool_loop': True})
        self.assertFalse(got.enabled)
        self.assertEqual((got.max_rounds, got.timeout), (3, 15.0))

    def test_max_rounds_is_clamped_into_1_to_5(self):
        for raw, want in ((0, 1), (9, 5), (-3, 1), ('4', 4), ('abc', 3), (None, 3)):
            with self.subTest(raw=raw):
                self.assertEqual(
                    agent_tools.settings({'ai.agent_tools': True,
                                          'ai.agent_tools_max_rounds': raw}).max_rounds, want)

    def test_timeout_is_clamped_into_1_to_60(self):
        for raw, want in ((0.2, 1.0), (120, 60.0), ('3.5', 3.5), ('abc', 15.0), (None, 15.0)):
            with self.subTest(raw=raw):
                self.assertEqual(
                    agent_tools.settings({'ai.agent_tools': True,
                                          'ai.agent_tools_timeout': raw}).timeout, want)

    def test_switch_accepts_the_usual_truthy_spellings(self):
        for raw in (True, 1, 'true', 'TRUE', 'on', 'yes', '1', '开', '开启'):
            with self.subTest(raw=raw):
                self.assertTrue(agent_tools.settings({'ai.agent_tools': raw}).enabled)
        for raw in (False, 0, '', 'false', 'off', 'no', None, '随便'):
            with self.subTest(raw=raw):
                self.assertFalse(agent_tools.settings({'ai.agent_tools': raw}).enabled)

    def test_switch_does_not_imply_tool_loop_and_vice_versa(self):
        # ai.tool_loop 打开不会顺手打开控制台工具环。
        self.assertFalse(agent_tools.settings({'ai.tool_loop': True}).enabled)
        # 控制台工具环打开也不需要 ai.tool_loop。
        got = agent_tools.settings({'ai.agent_tools': True, 'ai.tool_loop': False})
        self.assertTrue(got.enabled)

    def test_loop_settings_mirror_the_three_keys(self):
        got = agent_tools.settings({'ai.agent_tools': True, 'ai.agent_tools_max_rounds': 4,
                                   'ai.agent_tools_timeout': 7.5})
        loop = got.loop_settings()
        self.assertIsInstance(loop, tool_loop.LoopSettings)
        self.assertTrue(loop.enabled)
        self.assertFalse(loop.owner_only)          # 控制台本身即主人，不做 owner 判定
        self.assertEqual(loop.max_rounds, 4)
        self.assertEqual(loop.tool_timeout, 7.5)
        self.assertEqual(loop.round_timeout, agent_tools.CHAT_TIMEOUT)
        self.assertTrue(loop.hint)


class WorkspaceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.data_dir = self.base / 'data'
        self.registry = tool_loop.ToolRegistry()
        self.count = agent_tools.register_workspace(self.registry, self.data_dir)

    def tearDown(self):
        self.temp.cleanup()

    def test_注册键沿用_source__name_规则(self):
        self.assertEqual(self.count, 4)
        self.assertEqual(self.registry.names(),
                         ['workspace__append', 'workspace__list', 'workspace__read', 'workspace__write'])
        self.assertEqual({item['source'] for item in self.registry.describe()}, {'workspace'})

    async def test_write_read_append_roundtrip_uses_real_files(self):
        written = await call_tool(self.registry, 'workspace__write',
                                  {'relpath': 'notes/todo.txt', 'text': '第一行\n'})
        self.assertTrue(written['ok'], written)
        target = self.data_dir / 'agent_workspace' / agent_tools.AGENT_ID / 'notes' / 'todo.txt'
        self.assertTrue(target.is_file())
        read = await call_tool(self.registry, 'workspace__read', {'relpath': 'notes/todo.txt'})
        self.assertEqual(read['text'], '第一行\n')
        appended = await call_tool(self.registry, 'workspace__append',
                                   {'relpath': 'notes/todo.txt', 'text': '第二行\n'})
        self.assertTrue(appended['ok'], appended)
        again = await call_tool(self.registry, 'workspace__read', {'relpath': 'notes/todo.txt'})
        self.assertEqual(again['text'], '第一行\n第二行\n')

    async def test_list_shows_files_and_missing_relpath_is_visible(self):
        await call_tool(self.registry, 'workspace__write', {'relpath': 'a.txt', 'text': 'x'})
        listed = await call_tool(self.registry, 'workspace__list', {})
        self.assertTrue(listed['ok'], listed)
        self.assertIn('a.txt', [item['name'] for item in listed['entries']])
        missing = await call_tool(self.registry, 'workspace__read', {})
        self.assertFalse(missing['ok'])
        self.assertEqual(missing['code'], 'invalid')
        self.assertIn('relpath', missing['message'])

    async def test_text_must_be_a_string(self):
        bad = await call_tool(self.registry, 'workspace__write', {'relpath': 'a.txt', 'text': 5})
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['code'], 'invalid')

    async def test_every_write_lands_in_the_workspace_audit(self):
        await call_tool(self.registry, 'workspace__write', {'relpath': 'a.txt', 'text': 'x'})
        await call_tool(self.registry, 'workspace__append', {'relpath': 'a.txt', 'text': 'y'})
        audit = self.data_dir / 'qq-only' / 'agent_workspace_audit.jsonl'
        self.assertTrue(audit.is_file(), audit)
        rows = [json.loads(line) for line in audit.read_text(encoding='utf-8').splitlines() if line.strip()]
        ops = [row.get('op') for row in rows]
        self.assertIn('write', ops)
        self.assertIn('append', ops)
        self.assertTrue(all(agent_tools.AGENT_ID in json.dumps(row, ensure_ascii=False) for row in rows))

    async def test_unknown_workspace_tool_is_not_in_the_table(self):
        self.assertIsNone(self.registry.get('workspace__shell'))
        self.assertIsNone(await call_tool(self.registry, 'workspace__shell', {}))

    def test_registering_the_same_source_twice_keeps_tool_loop_suffix_rule(self):
        again = tool_loop.ToolRegistry()
        agent_tools.register_workspace(again, self.data_dir)
        agent_tools.register_workspace(again, self.data_dir)
        names = again.names()
        self.assertIn('workspace__read', names)
        self.assertIn('workspace__read_2', names)


class DangerGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_source_is_always_allowed(self):
        gate = agent_tools.DangerGate()
        for name in ('workspace__read', 'workspace__write', 'workspace__append', 'workspace__list'):
            self.assertIsNone(await gate.run_on_before_tool(name, {'relpath': 'a.txt'}))
        self.assertEqual(gate.blocked, [])

    async def test_machine_tool_is_blocked_with_a_readable_reason(self):
        gate = agent_tools.DangerGate()
        verdict = await gate.run_on_before_tool('qqagent_bridge__pc_hotkey', {'keys': 'win+r'})
        self.assertIsInstance(verdict, dict)
        self.assertTrue(verdict['block'])
        self.assertIn('危险动作确认', verdict['reason'])
        self.assertIn('qqagent_bridge__pc_hotkey', verdict['reason'])
        self.assertEqual(gate.blocked, ['qqagent_bridge__pc_hotkey'])

    async def test_keyboard_and_service_tools_are_recognised(self):
        gate = agent_tools.DangerGate()
        for name in ('pc__type_text', 'pc__service_restart', 'bridge__key_press',
                     'pc__screenshot', 'x__delete_file', 'x__shell_exec'):
            with self.subTest(name=name):
                self.assertTrue(await gate.run_on_before_tool(name, {}), name)

    async def test_harmless_extension_tool_passes_through(self):
        gate = agent_tools.DangerGate()
        for name in ('forward-reader__expand', 'qqagent_bridge__expand', 'wiki-query__wiki_query'):
            with self.subTest(name=name):
                self.assertIsNone(await gate.run_on_before_tool(name, {'text': 'hi'}))
        self.assertEqual(gate.blocked, [])

    def test_machine_hit_reports_the_word_that_matched(self):
        gate = agent_tools.DangerGate()
        self.assertEqual(gate.machine_hit('pc__hotkey'), 'hotkey')
        self.assertEqual(gate.machine_hit('pc__open_path'), 'open_path')
        self.assertEqual(gate.machine_hit('pc__read_clipboard'), '')


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_is_a_duck_typed_home_for_tool_loop(self):
        registry = tool_loop.ToolRegistry()
        runtime = agent_tools.ConsoleRuntime(self.base, {}, registry, data_dir=self.base / 'data')
        self.assertIsNone(runtime.data_root)       # 审计落 <root>/data/qq-only/
        self.assertEqual(runtime.config, {})
        self.assertIs(runtime.tool_registry, registry)
        self.assertEqual(tool_loop.audit_path(runtime),
                         self.base / 'data' / 'qq-only' / 'tool-loop-audit.jsonl')
        runtime.note('扩展工具注册失败：x')
        self.assertEqual(runtime.notes, ['扩展工具注册失败：x'])

    async def test_console_tools_counts_sources_and_skips_disabled(self):
        registry = tool_loop.ToolRegistry()
        agent_tools.register_workspace(registry, self.base / 'data')
        registry.register('forward-reader', 'expand', lambda name, args: {'ok': True})
        registry.register('dead', 'gone', None)     # 没有 handler → 不可用，不该被计数
        runtime = agent_tools.ConsoleRuntime(self.base, {}, registry, data_dir=self.base / 'data')
        tools = agent_tools.ConsoleTools(runtime)
        self.assertEqual(tools.sources(), {'workspace': 4, 'forward-reader': 1})

    def test_open_tools_off_builds_nothing(self):
        bot = SimpleNamespace(root=self.base, config={'ai.agent_tools': False}, plugins=None)
        self.assertIsNone(agent_tools.open_tools(bot))
        self.assertFalse((self.base / 'data').exists())   # 开关关着连目录都不建

    def test_open_tools_on_registers_the_workspace_source(self):
        bot = SimpleNamespace(root=self.base, config={'ai.agent_tools': True}, plugins=None)
        tools = agent_tools.open_tools(bot)
        self.assertIsNotNone(tools)
        self.assertEqual(tools.sources().get('workspace'), 4)
        self.assertTrue((self.base / 'data').is_dir())

    async def test_disabled_console_tools_never_calls_the_engine(self):
        class Engine:
            calls = 0

            async def chat(self, *args, **kwargs):
                type(self).calls += 1
                return {'text': 'x'}

        registry = tool_loop.ToolRegistry()
        agent_tools.register_workspace(registry, self.base / 'data')
        runtime = agent_tools.ConsoleRuntime(self.base, {}, registry, data_dir=self.base / 'data')
        tools = agent_tools.ConsoleTools(runtime, agent_tools.ConsoleToolSettings(enabled=False))
        self.assertIsNone(await tools.ask(Engine(), [{'role': 'user', 'content': 'hi'}]))
        self.assertEqual(Engine.calls, 0)


class WorkspaceDataRootTests(unittest.IsolatedAsyncioTestCase):
    """工作区数据根与 log_api.data_dir_of（控制台记事本口径）必须同一个解析结果。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _bot(self, data_root):
        return SimpleNamespace(root=self.base / 'host', data_root=data_root,
                               config={'ai.agent_tools': True}, plugins=None)

    def _want(self, data_dir, name='notes/todo.txt'):
        return Path(data_dir) / 'agent_workspace' / agent_tools.AGENT_ID / name

    async def test_bot_data_root_wins_for_the_workspace_tools(self):
        account = self.base / 'account-root'
        runtime = agent_tools.build_runtime(self._bot(account))
        self.assertEqual(Path(runtime.data_dir), account)
        written = await call_tool(runtime.tool_registry, 'workspace__write',
                                 {'relpath': 'notes/todo.txt', 'text': 'x\n'})
        self.assertTrue(written['ok'], written)
        target = self._want(account)
        self.assertTrue(target.is_file(), target)
        read = await call_tool(runtime.tool_registry, 'workspace__read', {'relpath': 'notes/todo.txt'})
        self.assertEqual(read['text'], 'x\n')
        # 老口径的 <root>/data 下不得再冒出 agent_workspace 副本
        self.assertFalse((self.base / 'host' / 'data' / 'agent_workspace').exists())

    async def test_missing_data_root_still_lands_in_root_data(self):
        runtime = agent_tools.build_runtime(self._bot(None))
        fallback = self.base / 'host' / 'data'
        self.assertEqual(Path(runtime.data_dir), fallback)
        written = await call_tool(runtime.tool_registry, 'workspace__write',
                                 {'relpath': 'notes/todo.txt', 'text': 'y\n'})
        self.assertTrue(written['ok'], written)
        target = self._want(fallback)
        self.assertTrue(target.is_file(), target)
        read = await call_tool(runtime.tool_registry, 'workspace__read', {'relpath': 'notes/todo.txt'})
        self.assertEqual(read['text'], 'y\n')

    async def test_the_two_roots_are_really_different_paths(self):
        account = self.base / 'account-root'
        with_data_root = agent_tools.workspace_data_dir(self._bot(account))
        without = agent_tools.workspace_data_dir(self._bot(None))
        self.assertEqual(Path(with_data_root), account)
        self.assertEqual(Path(without), self.base / 'host' / 'data')
        self.assertNotEqual(Path(with_data_root), Path(without))   # 防恒真断言
        runtime = agent_tools.build_runtime(self._bot(account))
        # ConsoleRuntime.data_dir 与 register_workspace 用的是同一个解析结果
        self.assertEqual(Path(runtime.data_dir), Path(agent_tools.workspace_data_dir(self._bot(account))))


if __name__ == '__main__':
    unittest.main()
