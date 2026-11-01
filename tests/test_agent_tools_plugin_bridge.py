"""控制台工具环接插件（mod）工具：真实 mod 链路 + 确认闸门。

真实形状：目录 mod（custom_plugins/<id>/plugin.json）装载后，插件实例以**插件 id** 进
PluginManager._plugins；tool_loop.register_plugin_tools 拿这个键当 source
（src/tool_loop.py:354-379），ToolRegistry._add 再拼来源前缀（src/tool_loop.py:246），
所以工具在表里叫 <插件id>__<工具名>，不是 mod__<工具名>。闸门判 mod 工具的唯一依据是
PluginManager._mod_plugins 的键，因此本文件的风俗：一律用真实插件 id 命名工具，不再造一个
名叫 mod 的假插件去凑字符串。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import agent_tools, tool_loop
from src.plugins.manager import PluginManager

MOD_ID = 'inv_tool_mod'
_MISSING = object()
PEEK = MOD_ID + '__peek'
POKE = MOD_ID + '__poke'
WORKSPACE_NAMES = ['workspace__append', 'workspace__list', 'workspace__read', 'workspace__write']

# 真 mod 的入口：post_load 里声明两个工具（读 / 写），handler 每次执行都在自己的
# state 里 +1，测试用它当"handler 到底跑没跑"的唯一证据。
MOD_SRC = '''from . import PluginBase, PluginMeta


class InvToolMod(PluginBase):
    META = PluginMeta(id="__PID__", name="工具 mod", category="utility", description="测试用")

    def post_load(self):
        self.register_tool("peek", self.peek, description="看一眼",
                           parameters={"type": "object",
                                       "properties": {"what": {"type": "string"}}})
        self.register_tool("poke", self.poke, description="写一下", write=True)

    def _bump(self, key):
        data = self.load_state("calls.json", {})
        data[key] = int(data.get(key, 0)) + 1
        self.save_state("calls.json", data)

    def peek(self, arguments, runtime):
        self._bump("peek")
        return "peeked:" + str((arguments or {}).get("what", ""))

    def poke(self, arguments, runtime):
        self._bump("poke")
        return "poked"
'''


def write_mod(root, mod_id=MOD_ID, source=MOD_SRC, permissions=('register_tool',)):
    """按真实 mod 的目录约定落盘：<root>/custom_plugins/<id>/{plugin.json,mod.py}。"""
    folder = Path(root) / 'custom_plugins' / mod_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'mod.py').write_text(source.replace('__PID__', mod_id), encoding='utf-8')
    (folder / 'plugin.json').write_text(json.dumps({
        'id': mod_id, 'api_version': 1, 'entry': 'mod.py',
        'permissions': list(permissions), 'enabled_by_default': True}), encoding='utf-8')
    return folder


class ModEnv:
    """一次真实装载：真 PluginManager + 真目录 mod；同类的测试共享这一份（只读共享）。"""

    def __init__(self, mod_id=MOD_ID, source=MOD_SRC):
        # mod_id 可换：A 组要用 id 含 __ 的真 mod（见 NestedModIdTests）；
        # source 可换：C 组要一个工具名带危险词的真 mod。
        self.mod_id = mod_id
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_mod(self.root, mod_id, source=source)
        self.manager = PluginManager(self.root)
        self.problems = self.manager.take_load_problems()

    def close(self):
        self._tmp.cleanup()

    def bot(self, plugins=_MISSING, enabled=True):
        return SimpleNamespace(root=self.root, data_root=self.root,
                               config={'ai.agent_tools': enabled},
                               plugins=self.manager if plugins is _MISSING else plugins)

    def reset_state(self):
        path = self.root / 'plugin_state' / self.mod_id / 'calls.json'
        if path.is_file():
            path.unlink()

    def ran(self, key):
        path = self.root / 'plugin_state' / self.mod_id / 'calls.json'
        if not path.is_file():
            return 0
        return int(json.loads(path.read_text(encoding='utf-8')).get(key, 0))

    def audit_rows(self, runtime, **match):
        path = Path(tool_loop.audit_path(runtime))
        if not path.is_file():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip()]
        return [row for row in rows
                if all(str(row.get(key)) == str(value) for key, value in match.items())]


class RealModChainTests(unittest.TestCase):
    """A 组：真实 mod 走真链路——工具名、闸门判据都来自真实装载结果。"""

    @classmethod
    def setUpClass(cls):
        cls.env = ModEnv()

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def setUp(self):
        self.env.reset_state()

    def test_real_mod_mounts_without_load_problems(self):
        self.assertEqual(self.env.problems, [])
        self.assertIn(MOD_ID, self.env.manager._plugins)
        # 闸门判据的来源：插件 id -> mod id
        self.assertEqual(dict(self.env.manager._mod_plugins), {MOD_ID: MOD_ID})

    def test_real_mod_tool_name_is_plugin_id_prefixed(self):
        runtime = agent_tools.build_runtime(self.env.bot())
        names = sorted(runtime.tool_registry.names())
        self.assertEqual(names, sorted(WORKSPACE_NAMES + [PEEK, POKE]))
        self.assertEqual(runtime.counts['plugins'], 2)
        # 上一版的假前提在这里被钉住：真 mod 工具并不以 mod__ 开头
        for name in (PEEK, POKE):
            self.assertFalse(name.startswith('mod__'), name)
        spec = runtime.tool_registry.get(PEEK)
        self.assertEqual((spec.source, spec.raw_name), (MOD_ID, 'peek'))

    def test_gate_takes_its_mod_ids_from_the_manager(self):
        runtime = agent_tools.build_runtime(self.env.bot())
        gate = runtime.plugins
        self.assertIsInstance(gate, agent_tools.DangerGate)
        self.assertEqual(gate.mod_ids, {MOD_ID})
        self.assertTrue(gate.is_mod_tool(PEEK))
        self.assertTrue(gate.is_mod_tool(POKE))

    def test_gate_without_mod_ids_claims_nothing(self):
        # 上一版就是这样：判据只有字面 'mod'，对真 mod 永远不触发。
        gate = agent_tools.DangerGate()
        self.assertEqual(gate.mod_ids, set())
        self.assertFalse(gate.is_mod_tool(PEEK))
        self.assertFalse(gate.is_mod_tool(POKE))

    def test_bot_without_plugins_does_not_raise_and_reports_no_mod_ids(self):
        bare = SimpleNamespace(root=self.env.root, data_root=self.env.root,
                               config={'ai.agent_tools': True})
        runtime = agent_tools.build_runtime(bare)
        self.assertEqual(runtime.plugins.mod_ids, set())
        self.assertEqual(sorted(runtime.tool_registry.names()), sorted(WORKSPACE_NAMES))
        self.assertEqual(runtime.notes, [])

    def test_bot_with_plugins_none_and_manager_without_mod_plugins_does_not_raise(self):
        for plugins in (None, PluginManager.__new__(PluginManager)):
            with self.subTest(plugins=plugins):
                runtime = agent_tools.build_runtime(SimpleNamespace(
                    root=self.env.root, data_root=self.env.root,
                    config={'ai.agent_tools': True}, plugins=plugins))
                self.assertEqual(runtime.plugins.mod_ids, set())
                self.assertEqual(runtime.counts['plugins'], 0)

    def test_open_tools_off_builds_nothing(self):
        with patch.object(agent_tools, 'build_runtime',
                          side_effect=AssertionError('开关关着不该建工具表')):
            self.assertIsNone(agent_tools.open_tools(self.env.bot(enabled=False)))
        self.assertFalse((self.env.root / 'host').exists())


class ModGateTests(unittest.IsolatedAsyncioTestCase):
    """B 组：闸门语义（判据显式给 mod_ids，不再靠工具名碰巧叫 mod）。"""

    def gate(self, **kwargs):
        return agent_tools.DangerGate(mod_ids={MOD_ID}, **kwargs)

    async def test_mod_tool_without_confirm_is_blocked(self):
        gate = self.gate()
        verdict = await gate.run_on_before_tool(PEEK, {'what': 'x'})
        self.assertTrue(verdict['block'])
        self.assertIn(PEEK, verdict['reason'])
        self.assertIn('confirm', verdict['reason'])
        self.assertIn('confirm', verdict['extra_system'])
        self.assertEqual(gate.blocked, [PEEK])
        self.assertEqual(gate.confirmed, [])

    async def test_mod_tool_with_confirm_passes(self):
        gate = self.gate()
        for arguments in ({'confirm': True}, {'confirm': 'true'}, {'confirm': 1}):
            with self.subTest(arguments=arguments):
                self.assertIsNone(await gate.run_on_before_tool(PEEK, arguments))
        self.assertEqual(gate.blocked, [])
        self.assertEqual(gate.confirmed, [PEEK] * 3)

    async def test_falsy_confirm_is_still_blocked(self):
        gate = self.gate()
        for arguments in ({'confirm': False}, {'confirm': 'false'}, {'confirm': ''},
                          {'confirm': 0}, {}, None):
            with self.subTest(arguments=arguments):
                self.assertTrue(await gate.run_on_before_tool(POKE, arguments))

    async def test_confirm_does_not_unlock_machine_tools(self):
        gate = self.gate()
        verdict = await gate.run_on_before_tool(MOD_ID + '__delete_everything', {'confirm': True})
        self.assertTrue(verdict['block'])
        self.assertIn('危险动作确认', verdict['reason'])
        self.assertEqual(gate.confirmed, [])

    def test_is_mod_tool_matches_only_the_real_prefix(self):
        gate = self.gate()
        for name in (PEEK, POKE):
            self.assertTrue(gate.is_mod_tool(name), name)
        for name in ('modular__x', 'workspace__read', 'no_separator', '', 'mod__set_mode',
                     MOD_ID, MOD_ID + 'x__peek', 'x__' + MOD_ID + '__peek'):
            self.assertEqual(gate.is_mod_tool(name), name == 'mod__set_mode', name)

    def test_is_mod_tool_matches_the_whole_mod_id_prefix(self):
        """A：判据是整名前缀。id 含 __ 时按首段比会漏判（'inv' 根本不在 mod_ids 里）。"""
        gate = agent_tools.DangerGate(mod_ids={'inv__nested'})
        self.assertTrue(gate.is_mod_tool('inv__nested__peek'))
        for name in ('inv__nested', 'inv__nestedx__peek', 'inv__', 'x__inv__nested__peek',
                     'modular__x', 'workspace__read', '', 'no_separator'):
            self.assertFalse(gate.is_mod_tool(name), name)
        # 同一张表里既有短 id 又有长 id 时互不串味
        both = agent_tools.DangerGate(mod_ids={'inv', 'inv__nested'})
        self.assertTrue(both.is_mod_tool('inv__nested__peek'))
        self.assertTrue(both.is_mod_tool('inv__peek'))
        self.assertFalse(both.is_mod_tool('invx__peek'))
        # 空 id 会被丢掉，否则 '__x' 这类名字会被 startswith('__') 误判成 mod 工具
        self.assertEqual(agent_tools.DangerGate(mod_ids={'', 'inv'}).mod_ids, {'inv'})
        self.assertFalse(agent_tools.DangerGate(mod_ids={''}).is_mod_tool('__peek'))

    async def test_workspace_tools_still_bypass_the_gate(self):
        gate = self.gate()
        for name in WORKSPACE_NAMES:
            self.assertIsNone(await gate.run_on_before_tool(name, {'relpath': 'a.txt'}))
        self.assertEqual((gate.blocked, gate.confirmed), ([], []))

    def test_mod_ids_tolerates_none(self):
        for value in (None, (), [], {'a'}):
            with self.subTest(value=value):
                gate = agent_tools.DangerGate(mod_ids=value)
                self.assertIsInstance(gate.mod_ids, set)
        self.assertEqual(agent_tools.DangerGate(mod_ids=None).mod_ids, set())


class ScriptedEngine:
    """本机没有 API key：按剧本回 OpenAI 形状的 tool_calls，并记下模型侧收到的消息。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def chat(self, conversation, **kwargs):
        self.seen.append({'messages': [dict(item) for item in conversation],
                          'tools': kwargs.get('tools')})
        return self.replies.pop(0) if self.replies else {'text': '好了'}


def tool_reply(name, arguments, call_id='call_1'):
    return {'text': '', 'tool_calls': [{
        'id': call_id, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}


def fed_tool_messages(seen):
    return [item for item in seen if item.get('role') == 'tool']


class RealModTurnTests(unittest.IsolatedAsyncioTestCase):
    """C 组：真实 mod 的两回合——没确认不执行，确认了才执行。"""

    @classmethod
    def setUpClass(cls):
        cls.env = ModEnv()

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def setUp(self):
        self.env.reset_state()

    async def test_round_without_confirm_never_runs_the_handler(self):
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply(PEEK, {'what': 'x'}), {'text': '需要你点头'}])
        answer = await tools.ask(engine, [{'role': 'user', 'content': '看一眼'}])
        self.assertEqual(answer, '需要你点头')
        self.assertEqual(self.env.ran('peek'), 0)                     # handler 没被执行
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertEqual(fed['name'], PEEK)
        self.assertIn('被安全策略拦下', fed['content'])
        self.assertIn('confirm', fed['content'])
        self.assertEqual(len(self.env.audit_rows(tools.runtime, phase='blocked', tool=PEEK)), 1)
        self.assertEqual(tools.runtime.plugins.blocked, [PEEK])

    async def test_round_with_confirm_runs_the_handler_and_returns_its_value(self):
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply(PEEK, {'what': 'y', 'confirm': True}),
                                 {'text': '看过了'}])
        answer = await tools.ask(engine, [{'role': 'user', 'content': '用户同意了，看一眼'}])
        self.assertEqual(answer, '看过了')
        self.assertEqual(self.env.ran('peek'), 1)
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('peeked:y', fed['content'])
        self.assertIn('"ok": true', fed['content'])
        self.assertEqual(len(self.env.audit_rows(tools.runtime, phase='tool', tool=PEEK)), 1)

    async def test_mod_layer_still_refuses_a_write_tool_when_the_gate_is_stubbed(self):
        # 纵深防御：把闸门的 mod 判定打桩成 False，模拟"闸门失效"，mod 层必须自己拒。
        runtime = agent_tools.build_runtime(self.env.bot())
        gate = runtime.plugins
        self.assertTrue(gate.is_mod_tool(POKE))
        gate.mod_ids = set()
        self.assertFalse(gate.is_mod_tool(POKE))
        tools = agent_tools.ConsoleTools(runtime, agent_tools.ConsoleToolSettings(
            enabled=True, max_rounds=2, timeout=5.0))
        engine = ScriptedEngine([tool_reply(POKE, {}), {'text': '插件自己拒了'}])
        await tools.ask(engine, [{'role': 'user', 'content': '戳一下'}])
        self.assertEqual(self.env.ran('poke'), 0)
        self.assertEqual(gate.blocked, [])                            # 闸门这次确实没拦
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('confirm-required', fed['content'])

    async def test_the_model_sees_both_tool_families(self):
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([{'text': '直接回答'}])
        await tools.ask(engine, [{'role': 'user', 'content': '你好'}])
        sent = sorted(item['function']['name'] for item in engine.seen[0]['tools'])
        self.assertEqual(sent, sorted(WORKSPACE_NAMES + [PEEK, POKE]))
        self.assertEqual(tools.sources(), {MOD_ID: 2, 'workspace': 4})
        self.assertNotIn(PEEK, tool_loop.TOOL_HINT)


NESTED_ID = 'inv__nested'      # 目录名与 META.id 都含 __（manager 只校验"非空 + 等于目录名"）
NESTED_PEEK = NESTED_ID + '__peek'
NESTED_POKE = NESTED_ID + '__poke'


class NestedModIdTests(unittest.IsolatedAsyncioTestCase):
    """A 回归：mod id 含 __ 时闸门也要认出它的工具——旧的首段判据在这里漏判。"""

    @classmethod
    def setUpClass(cls):
        cls.env = ModEnv(NESTED_ID)

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def setUp(self):
        self.env.reset_state()

    def test_nested_id_mod_mounts_and_the_gate_sees_it(self):
        self.assertEqual(self.env.problems, [])
        self.assertEqual(dict(self.env.manager._mod_plugins), {NESTED_ID: NESTED_ID})
        runtime = agent_tools.build_runtime(self.env.bot())
        self.assertEqual(sorted(runtime.tool_registry.names()),
                         sorted(WORKSPACE_NAMES + [NESTED_PEEK, NESTED_POKE]))
        gate = runtime.plugins
        self.assertEqual(gate.mod_ids, {NESTED_ID})
        self.assertTrue(gate.is_mod_tool(NESTED_PEEK))
        self.assertTrue(gate.is_mod_tool(NESTED_POKE))
        # 漏判点本身：旧判据只切第一个 __ 取首段 'inv' 去比 mod_ids，
        # 首段不在表里 → 判 False，读工具就这样绕过确认闸门。新判据按整名前缀比。
        head = NESTED_PEEK.split('__', 1)[0]
        self.assertEqual(head, 'inv')
        self.assertNotIn(head, gate.mod_ids)
        self.assertTrue(gate.is_mod_tool(NESTED_PEEK))

    async def test_read_tool_without_confirm_is_blocked_and_audited(self):
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply(NESTED_PEEK, {'what': 'x'}), {'text': '要你点头'}])
        answer = await tools.ask(engine, [{'role': 'user', 'content': '看一眼'}])
        self.assertEqual(answer, '要你点头')
        self.assertEqual(self.env.ran('peek'), 0)                      # handler 一次都没跑
        self.assertEqual(tools.runtime.plugins.blocked, [NESTED_PEEK])
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('被安全策略拦下', fed['content'])
        self.assertIn('confirm', fed['content'])
        self.assertEqual(
            len(self.env.audit_rows(tools.runtime, phase='blocked', tool=NESTED_PEEK)), 1)

    async def test_read_tool_with_confirm_runs_once(self):
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply(NESTED_PEEK, {'what': 'y', 'confirm': True}),
                                 {'text': '看过了'}])
        await tools.ask(engine, [{'role': 'user', 'content': '用户同意了'}])
        self.assertEqual(self.env.ran('peek'), 1)
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('peeked:y', fed['content'])
        self.assertEqual(tools.runtime.plugins.confirmed, [NESTED_PEEK])


class ConfirmWordingTests(unittest.IsolatedAsyncioTestCase):
    """B 回归：闸门与 mod 层共用一份确认判定——一致放行，不许"闸门放行、mod 层再拒一次"。"""

    @classmethod
    def setUpClass(cls):
        cls.env = ModEnv()

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def setUp(self):
        self.env.reset_state()

    async def turn(self, name, arguments):
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply(name, arguments), {'text': '好了'}])
        await tools.ask(engine, [{'role': 'user', 'content': '用户同意'}])
        return tools, fed_tool_messages(engine.seen[1]['messages'])[0]

    async def test_string_confirm_lets_the_read_tool_run_once(self):
        tools, fed = await self.turn(PEEK, {'what': 'z', 'confirm': 'true'})
        self.assertEqual(self.env.ran('peek'), 1)
        self.assertIn('peeked:z', fed['content'])
        self.assertNotIn('confirm-required', fed['content'])           # 不再被拒第二次
        self.assertEqual(tools.runtime.plugins.blocked, [])
        self.assertEqual(tools.runtime.plugins.confirmed, [PEEK])

    async def test_string_confirm_lets_the_write_tool_run_once(self):
        tools, fed = await self.turn(POKE, {'confirm': 'true'})
        self.assertEqual(self.env.ran('poke'), 1)
        self.assertIn('"ok": true', fed['content'])
        self.assertNotIn('confirm-required', fed['content'])
        self.assertEqual(tools.runtime.plugins.confirmed, [POKE])

    async def test_boolean_confirm_path_is_unchanged(self):
        for arguments in ({'confirm': True}, {'confirm': 1}):
            with self.subTest(arguments=arguments):
                self.env.reset_state()
                _, fed = await self.turn(POKE, arguments)
                self.assertEqual(self.env.ran('poke'), 1)
                self.assertNotIn('confirm-required', fed['content'])

    async def test_every_truthy_form_runs_the_write_tool_once(self):
        for value in ('yes', 'on', '开', '开启', ' TRUE ', 1):
            with self.subTest(value=value):
                self.env.reset_state()
                _, fed = await self.turn(POKE, {'confirm': value})
                self.assertEqual(self.env.ran('poke'), 1)
                self.assertNotIn('confirm-required', fed['content'])

    async def test_falsy_confirm_is_blocked_by_the_gate(self):
        for arguments in ({}, {'confirm': False}, {'confirm': '0'}, {'confirm': 0},
                          {'confirm': ''}, {'confirm': None}):
            with self.subTest(arguments=arguments):
                self.env.reset_state()
                tools, fed = await self.turn(POKE, arguments)
                self.assertEqual(self.env.ran('poke'), 0)
                self.assertIn('被安全策略拦下', fed['content'])
                self.assertEqual(tools.runtime.plugins.blocked, [POKE])


class FakeBridge:
    """最小桥（list_tools/call_tool）：只测工具表登记的容错，与闸门判据无关。"""

    def __init__(self, tools=None, list_error=None, with_caller=True):
        self.tools = list(tools or [])
        self.list_error = list_error
        if not with_caller:
            self.call_tool = None

    def list_tools(self):
        if self.list_error:
            raise self.list_error
        return list(self.tools)

    def call_tool(self, name, arguments=None):
        return {'ok': True, 'tool': name}


class CountingBridge:
    """非 mod 的普通桥（list_tools/call_tool）：每执行一次记一笔，当"handler 跑没跑"的证据。"""

    def __init__(self, names):
        self.names = list(names)
        self.calls = []

    def list_tools(self):
        return [{'name': name} for name in self.names]

    def call_tool(self, name, arguments=None):
        self.calls.append(str(name))
        return {'ok': True, 'tool': name}


def plugins_of(**bridges):
    """plugins_of(demo_plugin=bridge) -> 一个只有 _plugins 的假 manager。"""
    return SimpleNamespace(_plugins=dict(bridges))


class TableTests(unittest.TestCase):
    """D 组：登记容错（坏清单/坏参数不拖垮整张表）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def bot(self, plugins):
        return SimpleNamespace(root=self.root, data_root=self.root,
                               config={'ai.agent_tools': True}, plugins=plugins)

    def test_no_plugins_keeps_the_table_exactly_as_before(self):
        runtime = agent_tools.build_runtime(self.bot(None))
        self.assertEqual(sorted(runtime.tool_registry.names()), sorted(WORKSPACE_NAMES))
        self.assertEqual(runtime.counts, {'workspace': 4, 'plugins': 0, 'skills': 0})
        self.assertEqual(runtime.notes, [])

    def test_broken_list_tools_leaves_workspace_tools_alone(self):
        runtime = agent_tools.build_runtime(self.bot(plugins_of(
            broken_plugin=FakeBridge(list_error=RuntimeError('桥炸了')))))
        self.assertEqual(sorted(runtime.tool_registry.names()), sorted(WORKSPACE_NAMES))
        self.assertEqual(runtime.counts['plugins'], 0)

    def test_bridge_without_call_tool_is_skipped(self):
        runtime = agent_tools.build_runtime(self.bot(plugins_of(
            demo_plugin=FakeBridge([{'name': 'peek'}], with_caller=False))))
        self.assertEqual(runtime.counts['plugins'], 0)

    def test_bad_parameters_do_not_crash_the_table(self):
        runtime = agent_tools.build_runtime(self.bot(plugins_of(demo_plugin=FakeBridge([
            {'name': 'bare_name'},
            {'name': 'list_params', 'parameters': ['oops']},
            {'name': 'nested', 'parameters': {'properties': {'a': {'type': 'string'}}}},
            {'name': '', 'description': '没有名字的坏条目'}]))))
        names = sorted(runtime.tool_registry.names())
        self.assertEqual(names, sorted(WORKSPACE_NAMES + [
            'demo_plugin__bare_name', 'demo_plugin__list_params', 'demo_plugin__nested']))
        for name in names:
            self.assertEqual(runtime.tool_registry.get(name).parameters['type'], 'object', name)


WORKSPACE_ID = 'workspace'          # mod id 恰好叫 workspace：旧闸门在"名字像 workspace"处早退
WSPACE_PEEK = WORKSPACE_ID + '__peek'
WSPACE_POKE = WORKSPACE_ID + '__poke'
DANGEROUS_TOOL = WORKSPACE_ID + '__delete_everything'

# 工具名带 MACHINE_PARTS 里的词（delete）的真 mod：用来钉住"带 confirm 也过不了危险词那层"。
DANGER_SRC = '''from . import PluginBase, PluginMeta


class DangerMod(PluginBase):
    META = PluginMeta(id="__PID__", name="危险词 mod", category="utility", description="测试用")

    def post_load(self):
        self.register_tool("delete_everything", self.delete_everything, description="全删")

    def delete_everything(self, arguments, runtime):
        data = self.load_state("calls.json", {})
        data["danger"] = int(data.get("danger", 0)) + 1
        self.save_state("calls.json", data)
        return "deleted"
'''


class WorkspaceNamedModTests(unittest.IsolatedAsyncioTestCase):
    """C 回归：mod id 恰好叫 workspace 时，它的读工具不许因为"名字像工作区工具"被放行。

    旧顺序在闸门开头就 return None（name.split('__')[0] == 'workspace'），于是
    workspace__peek 已被认成 mod 工具也照样不带 confirm 执行，连危险词那层都跳过。
    """

    @classmethod
    def setUpClass(cls):
        cls.env = ModEnv(WORKSPACE_ID)

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def setUp(self):
        self.env.reset_state()

    def test_the_mod_tools_share_the_workspace_prefix_but_not_its_identity(self):
        self.assertEqual(self.env.problems, [])
        self.assertEqual(dict(self.env.manager._mod_plugins), {WORKSPACE_ID: WORKSPACE_ID})
        runtime = agent_tools.build_runtime(self.env.bot())
        self.assertEqual(sorted(runtime.tool_registry.names()),
                         sorted(WORKSPACE_NAMES + [WSPACE_PEEK, WSPACE_POKE]))
        gate = runtime.plugins
        self.assertEqual(gate.mod_ids, {WORKSPACE_ID})
        # 同一份名字前缀，两种身份：唯一判据是注册来源（workspace_tools）
        self.assertTrue(gate.is_mod_tool(WSPACE_PEEK))
        self.assertEqual(gate.workspace_tools, set(WORKSPACE_NAMES))
        self.assertFalse(gate.is_workspace_tool(WSPACE_PEEK))
        for name in WORKSPACE_NAMES:
            self.assertTrue(gate.is_workspace_tool(name), name)

    async def test_read_tool_without_confirm_never_runs_the_handler(self):
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply(WSPACE_PEEK, {'what': 'x'}), {'text': '要你点头'}])
        answer = await tools.ask(engine, [{'role': 'user', 'content': '看一眼'}])
        self.assertEqual(answer, '要你点头')
        self.assertEqual(self.env.ran('peek'), 0)                     # 旧顺序这里是 1
        self.assertEqual(tools.runtime.plugins.blocked, [WSPACE_PEEK])
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('被安全策略拦下', fed['content'])
        self.assertIn('confirm', fed['content'])
        self.assertEqual(
            len(self.env.audit_rows(tools.runtime, phase='blocked', tool=WSPACE_PEEK)), 1)

    async def test_read_tool_with_confirm_runs_once(self):
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply(WSPACE_PEEK, {'what': 'y', 'confirm': True}),
                                 {'text': '看过了'}])
        await tools.ask(engine, [{'role': 'user', 'content': '用户同意了'}])
        self.assertEqual(self.env.ran('peek'), 1)
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('peeked:y', fed['content'])
        self.assertEqual(tools.runtime.plugins.confirmed, [WSPACE_PEEK])

    async def test_real_workspace_tools_still_bypass_the_gate(self):
        # 同一个装载里，真工作区工具（注册来源 = workspace）不被"同名 mod"连累：
        # 不要求 confirm，照旧直接执行。
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply('workspace__write',
                                            {'relpath': 'a.txt', 'text': 'hi'}),
                                 {'text': '写好了'}])
        await tools.ask(engine, [{'role': 'user', 'content': '写一句话'}])
        gate = tools.runtime.plugins
        self.assertEqual(gate.blocked, [])
        self.assertNotIn('workspace__write', gate.confirmed)          # 没走 mod 确认分支
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('"ok": true', fed['content'])
        self.assertEqual(
            self.env.audit_rows(tools.runtime, phase='blocked', tool='workspace__write'), [])


class GateOrderTests(unittest.IsolatedAsyncioTestCase):
    """B 回归：判定顺序（mod 判据在前）+ 工作区工具白名单 + 危险词那层不许被绕过。"""

    def gate(self, **kwargs):
        return agent_tools.DangerGate(**kwargs)

    async def test_workspace_named_mod_read_tool_is_blocked_without_confirm(self):
        gate = self.gate(mod_ids={WORKSPACE_ID}, workspace_tools=WORKSPACE_NAMES)
        verdict = await gate.run_on_before_tool(WSPACE_PEEK, {'what': 'x'})
        self.assertTrue(verdict['block'])
        self.assertIn(WSPACE_PEEK, verdict['reason'])
        self.assertEqual(gate.blocked, [WSPACE_PEEK])

    async def test_registered_workspace_tools_pass_even_with_a_workspace_named_mod(self):
        gate = self.gate(mod_ids={WORKSPACE_ID}, workspace_tools=WORKSPACE_NAMES)
        for name in WORKSPACE_NAMES:
            self.assertIsNone(await gate.run_on_before_tool(name, {'relpath': 'a.txt'}), name)
        self.assertEqual((gate.blocked, gate.confirmed), ([], []))

    async def test_without_the_registered_whitelist_the_mod_rule_wins(self):
        # 实测口径（不是猜）：手工造的 gate 没有注册表可查，名字前缀分不出真假，
        # 于是 workspace__read 按 mod 规则要求确认——从紧，不静默放过。
        gate = self.gate(mod_ids={WORKSPACE_ID})
        self.assertEqual(gate.workspace_tools, set())
        verdict = await gate.run_on_before_tool('workspace__read', {'relpath': 'a.txt'})
        self.assertTrue(verdict['block'])
        self.assertEqual(gate.blocked, ['workspace__read'])

    async def test_every_whitelisted_workspace_tool_still_passes_without_confirm(self):
        # 参数化覆盖四工具：纯白名单 / 白名单 + 同名 mod 两种 gate 都不要求 confirm，
        # 也不许被记进 blocked/confirmed（真工作区工具零回归）。
        for mod_ids in (set(), {WORKSPACE_ID}):
            gate = self.gate(mod_ids=mod_ids, workspace_tools=WORKSPACE_NAMES)
            for name in WORKSPACE_NAMES:
                with self.subTest(name=name, mod_ids=sorted(mod_ids)):
                    self.assertIsNone(
                        await gate.run_on_before_tool(name, {'relpath': 'a.txt'}), name)
            self.assertEqual((gate.blocked, gate.confirmed), ([], []))

    async def test_empty_whitelist_with_a_workspace_named_mod_still_needs_confirm(self):
        # 从紧取舍（钉住）：手搓 gate 没有注册表可查，名字前缀分不出真假 → 四工具一律按
        # mod 规则要确认；带了 confirm 才放行。宁可多要一次确认，也不许静默放行。
        gate = self.gate(mod_ids={WORKSPACE_ID})
        self.assertEqual(gate.workspace_tools, set())
        for name in WORKSPACE_NAMES:
            with self.subTest(name=name):
                verdict = await gate.run_on_before_tool(name, {'relpath': 'a.txt'})
                self.assertTrue(verdict['block'])
                self.assertIn(name, verdict['reason'])
                self.assertIn('confirm', verdict['reason'])
                self.assertIsNone(await gate.run_on_before_tool(name, {'confirm': True}))
        self.assertEqual(gate.blocked, list(WORKSPACE_NAMES))
        self.assertEqual(gate.confirmed, list(WORKSPACE_NAMES))

    async def test_machine_words_still_block_a_mod_tool_carrying_confirm(self):
        cases = (('workspace__delete_everything', {WORKSPACE_ID}, WORKSPACE_NAMES),
                 ('workspace__delete_everything', {WORKSPACE_ID}, ()),
                 (MOD_ID + '__delete_everything', {MOD_ID}, ()))
        for name, mod_ids, whitelist in cases:
            with self.subTest(name=name, whitelist=bool(whitelist)):
                gate = self.gate(mod_ids=mod_ids, workspace_tools=whitelist)
                verdict = await gate.run_on_before_tool(name, {'confirm': True})
                self.assertTrue(verdict['block'])
                self.assertIn('delete', verdict['reason'])
                self.assertEqual(gate.confirmed, [])

    async def test_workspace_named_mod_write_tool_needs_confirm_too(self):
        gate = self.gate(mod_ids={WORKSPACE_ID}, workspace_tools=WORKSPACE_NAMES)
        self.assertTrue((await gate.run_on_before_tool(WSPACE_POKE, {}))['block'])
        self.assertIsNone(await gate.run_on_before_tool(WSPACE_POKE, {'confirm': True}))
        self.assertEqual(gate.confirmed, [WSPACE_POKE])


WSPACE_BRIDGE_ID = 'workspace'     # 非 mod 的普通桥插件，id 恰好与来源名 workspace 同名
BRIDGE_DANGER = WSPACE_BRIDGE_ID + '__delete_everything'
BRIDGE_HARMLESS = WSPACE_BRIDGE_ID + '__peek2'


class BridgeSourcedWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    """C 回归：非 mod 的桥插件 id 恰好叫 workspace（source='workspace'），它注册的工具名
    同样形如 workspace__<tool>，却不在 register_workspace 的注册结果里。

    白名单是 build_runtime 在注册完工作区工具后取的快照（src/agent_tools.py:326），桥插件
    晚一步进表，所以它的工具不在快照里。旧闸门在 is_workspace_tool 之前有一条
    "name.split('__')[0] == workspace 就 return None" 的早退：这类工具因此整段放行，连
    危险词那层 machine_hit 也一并跳过——名字带 delete 的工具 handler 照跑（实测 1 次）。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = CountingBridge(['delete_everything', 'peek2'])
        self.bot = SimpleNamespace(root=self.root, data_root=self.root,
                                   config={'ai.agent_tools': True},
                                   plugins=plugins_of(**{WSPACE_BRIDGE_ID: self.bridge}))
        self.runtime = agent_tools.build_runtime(self.bot)
        self.gate = self.runtime.plugins

    def tearDown(self):
        self.temp.cleanup()

    def audit_rows(self):
        path = Path(tool_loop.audit_path(self.runtime))
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip()]

    async def run_tool(self, name, arguments=None):
        calls = tool_loop.parse_tool_calls(tool_reply(name, arguments or {}))
        outcome = await tool_loop.run_calls(self.runtime.tool_registry, self.gate,
                                            self.runtime, calls)
        return outcome['messages'][0][2]

    def test_bridge_tools_are_workspace_sourced_but_not_whitelisted(self):
        self.assertEqual(self.runtime.tool_registry.names(),
                         sorted(WORKSPACE_NAMES + [BRIDGE_DANGER, BRIDGE_HARMLESS]))
        self.assertEqual(self.gate.mod_ids, set())                      # 桥插件不是 mod
        self.assertEqual(self.gate.workspace_tools, set(WORKSPACE_NAMES))
        for name in (BRIDGE_DANGER, BRIDGE_HARMLESS):
            self.assertEqual(self.runtime.tool_registry.get(name).source, 'workspace', name)
            self.assertFalse(self.gate.is_workspace_tool(name), name)   # 名字像，身份不是

    async def test_dangerous_bridge_tool_verdict_comes_from_the_machine_layer(self):
        verdict = await self.gate.run_on_before_tool(BRIDGE_DANGER, {})
        self.assertTrue(verdict['block'])
        self.assertIn(BRIDGE_DANGER, verdict['reason'])
        self.assertIn('会改本机状态', verdict['reason'])
        self.assertIn('delete', verdict['reason'])
        self.assertEqual(self.gate.blocked, [BRIDGE_DANGER])
        self.assertEqual(self.gate.confirmed, [])

    async def test_dangerous_bridge_tool_never_reaches_its_handler(self):
        text = await self.run_tool(BRIDGE_DANGER, {})
        self.assertEqual(self.bridge.calls, [])                         # 旧早退下这里是 1
        self.assertIn('被安全策略拦下', text)
        self.assertIn('会改本机状态', text)
        self.assertEqual(self.gate.blocked, [BRIDGE_DANGER])
        self.assertEqual([row['tool'] for row in self.audit_rows()
                          if row.get('phase') == 'blocked'], [BRIDGE_DANGER])

    async def test_harmless_bridge_tool_still_runs_once(self):
        text = await self.run_tool(BRIDGE_HARMLESS, {})
        self.assertEqual(self.bridge.calls, ['peek2'])                  # 没有危险词照旧执行
        self.assertEqual(self.gate.blocked, [])
        self.assertEqual(self.gate.confirmed, [])                       # 不是 mod，别误记确认
        self.assertIn('"ok": true', text)
        self.assertEqual([row for row in self.audit_rows()
                          if row.get('phase') == 'blocked'], [])


class WorkspaceNamedDangerToolTests(unittest.IsolatedAsyncioTestCase):
    """C 回归：id 叫 workspace 的 mod 暴露带危险词的工具时，confirm 也放不过去。"""

    @classmethod
    def setUpClass(cls):
        cls.env = ModEnv(WORKSPACE_ID, source=DANGER_SRC)

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def setUp(self):
        self.env.reset_state()

    async def test_dangerous_mod_tool_with_confirm_is_blocked_and_audited(self):
        runtime = agent_tools.build_runtime(self.env.bot())
        self.assertIn(DANGEROUS_TOOL, runtime.tool_registry.names())
        tools = agent_tools.open_tools(self.env.bot())
        engine = ScriptedEngine([tool_reply(DANGEROUS_TOOL, {'confirm': True}),
                                 {'text': '不许删'}])
        await tools.ask(engine, [{'role': 'user', 'content': '删掉'}])
        self.assertEqual(self.env.ran('danger'), 0)                   # 危险词那层拦住了
        self.assertEqual(tools.runtime.plugins.blocked, [DANGEROUS_TOOL])
        self.assertEqual(tools.runtime.plugins.confirmed, [])
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('会改本机状态', fed['content'])
        self.assertIn('delete', fed['content'])
        self.assertEqual(
            len(self.env.audit_rows(tools.runtime, phase='blocked', tool=DANGEROUS_TOOL)), 1)


class CollidingModIdControlTests(unittest.IsolatedAsyncioTestCase):
    """C 回归：id 为 mod / tools 的兼容分支调顺序后仍照旧拦（不许把老分支弄坏）。"""

    @classmethod
    def setUpClass(cls):
        cls.envs = {mod_id: ModEnv(mod_id) for mod_id in ('mod', 'tools')}

    @classmethod
    def tearDownClass(cls):
        for env in cls.envs.values():
            env.close()

    async def test_prefixed_control_mods_are_still_blocked(self):
        for mod_id, env in sorted(self.envs.items()):
            with self.subTest(mod_id=mod_id):
                env.reset_state()
                name = mod_id + '__peek'
                self.assertEqual(env.problems, [])
                tools = agent_tools.open_tools(env.bot())
                self.assertTrue(tools.runtime.plugins.is_mod_tool(name))
                engine = ScriptedEngine([tool_reply(name, {'what': 'x'}), {'text': '要你点头'}])
                await tools.ask(engine, [{'role': 'user', 'content': '看一眼'}])
                self.assertEqual(env.ran('peek'), 0)
                self.assertEqual(tools.runtime.plugins.blocked, [name])
                fed = fed_tool_messages(engine.seen[1]['messages'])[0]
                self.assertIn('被安全策略拦下', fed['content'])
                self.assertEqual(
                    len(env.audit_rows(tools.runtime, phase='blocked', tool=name)), 1)

    async def test_prefixed_control_mods_run_once_with_confirm(self):
        for mod_id, env in sorted(self.envs.items()):
            with self.subTest(mod_id=mod_id):
                env.reset_state()
                name = mod_id + '__peek'
                tools = agent_tools.open_tools(env.bot())
                engine = ScriptedEngine([tool_reply(name, {'what': 'y', 'confirm': True}),
                                         {'text': '看过了'}])
                await tools.ask(engine, [{'role': 'user', 'content': '用户同意'}])
                self.assertEqual(env.ran('peek'), 1)
                self.assertEqual(tools.runtime.plugins.confirmed, [name])


# ── E 组：mod 名单跟着插件层热重载走（provider 活取，不再是 build_runtime 快照）─────

class LiveModIdsProviderTests(unittest.IsolatedAsyncioTestCase):
    """E 组：mod_ids_provider 每次判定前现取名单。

    钉住的真实语义（不是猜的，见 DangerGate.mod_ids 的 docstring）：
    手工赋值 > provider > 构造时的集合；provider 抛异常或返回 None 都退回已知集合。
    手工赋值优先，是因为它服务于打桩（gate.mod_ids = set() 模拟闸门失效）与应急拉闸/加闸。
    """

    def test_a_live_provider_makes_the_same_gate_instance_see_a_new_mod(self):
        live = set()
        gate = agent_tools.DangerGate(mod_ids_provider=lambda: set(live))
        self.assertEqual(gate.mod_ids, set())
        self.assertFalse(gate.is_mod_tool('inv_mod__peek'))
        live.add('inv_mod')                       # 插件层把新 mod 挂上来的那一刻
        self.assertEqual(gate.mod_ids, {'inv_mod'})
        self.assertTrue(gate.is_mod_tool('inv_mod__peek'))   # 同一个 gate 实例：立刻是活的
        live.discard('inv_mod')                   # 摘掉也一样跟着退，不留快照残留
        self.assertFalse(gate.is_mod_tool('inv_mod__peek'))

    async def test_the_before_hook_consults_the_provider_on_every_verdict(self):
        live = set()
        gate = agent_tools.DangerGate(mod_ids_provider=lambda: set(live))
        self.assertIsNone(await gate.run_on_before_tool('inv_mod__peek', {'what': 'x'}))
        live.add('inv_mod')
        verdict = await gate.run_on_before_tool('inv_mod__peek', {'what': 'x'})
        self.assertTrue(verdict['block'])
        self.assertIn('inv_mod__peek', verdict['reason'])
        self.assertEqual((gate.blocked, gate.confirmed), (['inv_mod__peek'], []))

    async def test_a_broken_provider_is_swallowed_and_the_known_set_still_guards(self):
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError('插件层抖了')

        gate = agent_tools.DangerGate(mod_ids={MOD_ID}, mod_ids_provider=boom)
        self.assertEqual(gate.mod_ids, {MOD_ID})              # 退回已知集合，不向外抛
        self.assertTrue(gate.is_mod_tool(PEEK))
        self.assertFalse(gate.is_mod_tool('other_mod__peek'))
        verdict = await gate.run_on_before_tool(PEEK, {'what': 'x'})   # 判定照常拦，不抛
        self.assertTrue(verdict['block'])
        self.assertEqual(gate.blocked, [PEEK])
        self.assertGreaterEqual(len(calls), 1)                # 确实问过 provider

    def test_a_provider_saying_none_is_silence_not_an_empty_list(self):
        # None 是"没说"，不是"没有 mod"：退回已知集合（从紧），空名单必须显式给 set()。
        gate = agent_tools.DangerGate(mod_ids={MOD_ID}, mod_ids_provider=lambda: None)
        self.assertEqual(gate.mod_ids, {MOD_ID})
        self.assertTrue(gate.is_mod_tool(PEEK))

    def test_manual_assignment_wins_over_the_provider(self):
        gate = agent_tools.DangerGate(mod_ids={MOD_ID}, mod_ids_provider=lambda: {MOD_ID})
        self.assertTrue(gate.is_mod_tool(POKE))
        gate.mod_ids = set()                    # 打桩/应急拉闸：provider 不许抢回去
        self.assertEqual(gate.mod_ids, set())
        self.assertFalse(gate.is_mod_tool(POKE))

    async def test_manual_assignment_disables_the_gate_even_with_a_live_provider(self):
        gate = agent_tools.DangerGate(mod_ids_provider=lambda: {MOD_ID})
        self.assertIsNotNone(await gate.run_on_before_tool(PEEK, {}))    # provider 说它是 mod
        gate.mod_ids = set()
        self.assertIsNone(await gate.run_on_before_tool(PEEK, {}))       # 赋值后按空集合走
        self.assertEqual(gate.blocked, [PEEK])

    def test_manual_assignment_can_also_add_ids_the_provider_does_not_report(self):
        gate = agent_tools.DangerGate(mod_ids_provider=lambda: set())
        self.assertFalse(gate.is_mod_tool('emergency_mod__peek'))
        gate.mod_ids = {'emergency_mod'}         # 应急加闸：provider 说没有也照拦
        self.assertTrue(gate.is_mod_tool('emergency_mod__peek'))

    def test_without_a_provider_nothing_is_recomputed_or_rewritten(self):
        gate = agent_tools.DangerGate(mod_ids={'a'})
        self.assertIsNone(gate.mod_ids_provider)
        first = gate.mod_ids
        self.assertIs(gate.mod_ids, first)       # 同一个 set 对象：不重算、不改写
        self.assertEqual(first, {'a'})
        self.assertTrue(gate.is_mod_tool('a__x'))


HOT_MOD_ID = 'hot_added_mod'
HOT_PEEK = HOT_MOD_ID + '__peek'
HOT_POKE = HOT_MOD_ID + '__poke'


class HotReloadedModTests(unittest.IsolatedAsyncioTestCase):
    """E 端到端：真 PluginManager + 热加入的目录 mod。

    快照时代这里是漏的：工具表建好之后才落盘的 mod，它的**读**工具不在构造时的 mod_ids
    里，不带 confirm 也会直通（写工具还有 mod 层 call_tool 的 confirm-required 兜住）。
    插件的 re-scan 优先用 sync_mods()，它没落地时退回 reload()。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = PluginManager(self.root)               # 空插件目录：此刻还没有 mod
        self.bot = SimpleNamespace(root=self.root, data_root=self.root,
                                   config={'ai.agent_tools': True}, plugins=self.manager)
        self.runtime = agent_tools.build_runtime(self.bot)    # 建表时刻：一个 mod 都没有
        self.gate = self.runtime.plugins

    def tearDown(self):
        self.temp.cleanup()

    def hot_add(self):
        """落盘一个 mod，然后触发插件层重扫（sync_mods 没落地时退回 reload）。"""
        write_mod(self.root, HOT_MOD_ID)
        sync = getattr(self.manager, 'sync_mods', None)
        if callable(sync):
            return sync()
        self.manager.reload()
        return {}

    def ran(self, key):
        path = self.root / 'plugin_state' / HOT_MOD_ID / 'calls.json'
        if not path.is_file():
            return 0
        return int(json.loads(path.read_text(encoding='utf-8')).get(key, 0))

    def audit_rows(self):
        path = Path(tool_loop.audit_path(self.runtime))
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def console(self):
        return agent_tools.ConsoleTools(self.runtime, agent_tools.ConsoleToolSettings(
            enabled=True, max_rounds=2, timeout=5.0))

    def test_the_gate_built_before_the_hot_add_is_alive_after_it(self):
        self.assertEqual(self.manager.take_load_problems(), [])
        self.assertEqual(self.gate.mod_ids, set())
        self.assertFalse(self.gate.is_mod_tool(HOT_PEEK))     # 建表时刻确实没有它
        self.hot_add()
        self.assertEqual(dict(self.manager._mod_plugins), {HOT_MOD_ID: HOT_MOD_ID})
        self.assertEqual(self.manager.take_load_problems(), [])
        self.assertEqual(self.gate.mod_ids, {HOT_MOD_ID})
        self.assertTrue(self.gate.is_mod_tool(HOT_PEEK))      # 同一个 gate：不留在快照上
        self.assertTrue(self.gate.is_mod_tool(HOT_POKE))

    async def test_hot_added_mod_read_tool_without_confirm_never_runs_its_handler(self):
        self.assertFalse(self.gate.is_mod_tool(HOT_PEEK))
        self.hot_add()
        # 增量同步要做的也正是这一步：把新 mod 的工具挂进同一张表
        self.assertEqual(tool_loop.register_plugin_tools(
            self.runtime.tool_registry, self.manager), 2)
        self.assertEqual(sorted(self.runtime.tool_registry.names()),
                         sorted(WORKSPACE_NAMES + [HOT_PEEK, HOT_POKE]))
        engine = ScriptedEngine([tool_reply(HOT_PEEK, {'what': 'x'}), {'text': '要你点头'}])
        answer = await self.console().ask(engine, [{'role': 'user', 'content': '看一眼'}])
        self.assertEqual(answer, '要你点头')
        self.assertEqual(self.ran('peek'), 0)                 # handler 一次都没跑
        self.assertEqual(self.gate.blocked, [HOT_PEEK])
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('被安全策略拦下', fed['content'])
        self.assertIn('confirm', fed['content'])
        self.assertEqual([row['tool'] for row in self.audit_rows()
                          if row.get('phase') == 'blocked'], [HOT_PEEK])

    async def test_hot_added_mod_read_tool_with_confirm_runs_once(self):
        self.hot_add()
        tool_loop.register_plugin_tools(self.runtime.tool_registry, self.manager)
        engine = ScriptedEngine([tool_reply(HOT_PEEK, {'what': 'y', 'confirm': True}),
                                 {'text': '看过了'}])
        await self.console().ask(engine, [{'role': 'user', 'content': '用户同意了'}])
        self.assertEqual(self.ran('peek'), 1)
        fed = fed_tool_messages(engine.seen[1]['messages'])[0]
        self.assertIn('peeked:y', fed['content'])
        self.assertEqual(self.gate.confirmed, [HOT_PEEK])


if __name__ == '__main__':
    unittest.main()

