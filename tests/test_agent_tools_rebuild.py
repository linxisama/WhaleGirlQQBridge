"""控制台工具表随插件集合（mod 集合）重建。

被修的两个后果（都在 m06 任务里）：
  ① 热挂上的 mod 的工具永远进不了控制台 agent 的工具表；
  ② 摘掉的 mod 的 spec 还躺在已经建好的 registry 里，模型再点它就执行已卸载 mod 的旧代码。

修法一句话：插件层给一个只读修订号（PluginManager.mod_revision），建表时把当时的
修订号记进 ConsoleTools.mod_revision，src/console_agent.py 的 _tool_turn 每轮只比较
这两个数，不等才重建——所以既不会漏掉变化，也没有"每轮建表"的开销。

本文件的风俗：
  - 一律真 PluginManager + 真目录 mod（custom_plugins/<id>/plugin.json），不造假插件；
  - 工具在表里叫 <插件id>__<工具名>（见 src/tool_loop.py:246 的来源前缀）；
  - _tool_turn 用真 ConsoleAgent 的真方法，只跳掉 __init__ 里那堆存储/记忆构造
    （记忆库、会话库对"工具表重建"这件事没有影响，造出来只让用例变慢变脆）。
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import agent_tools
from src.console_agent import ConsoleAgent
from src.plugins.manager import PluginManager

MOD_ID = 'inv_rebuild_mod'
_MISSING = object()
PEEK = MOD_ID + '__peek'
WORKSPACE_NAMES = ['workspace__append', 'workspace__list', 'workspace__read', 'workspace__write']

# 一个真 mod：post_load 里声明一个读工具，handler 每次执行都在自己的 state 里 +1，
# 这样"handler 到底跑没跑"有独立于返回值之外的证据（摘除后用得上）。
MOD_SRC = '''from . import PluginBase, PluginMeta


class InvRebuildMod(PluginBase):
    META = PluginMeta(id="__PID__", name="重建测试 mod", category="utility", description="测试用")

    def post_load(self):
        self.register_tool("peek", self.peek, description="看一眼",
                           parameters={"type": "object",
                                       "properties": {"what": {"type": "string"}}})

    def peek(self, arguments, runtime):
        data = self.load_state("calls.json", {})
        data["peek"] = int(data.get("peek", 0)) + 1
        self.save_state("calls.json", data)
        return "peeked:" + str((arguments or {}).get("what", ""))


'''


def write_mod(root, mod_id=MOD_ID):
    """按真实目录约定落盘：<root>/custom_plugins/<id>/{plugin.json,mod.py}。"""
    folder = Path(root) / 'custom_plugins' / mod_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'mod.py').write_text(MOD_SRC.replace('__PID__', mod_id), encoding='utf-8')
    (folder / 'plugin.json').write_text(json.dumps({
        'id': mod_id, 'api_version': 1, 'entry': 'mod.py',
        'permissions': ['register_tool'], 'enabled_by_default': True}), encoding='utf-8')
    return folder


def mod_calls(root, mod_id=MOD_ID):
    """这个 mod 的 handler 被执行了几次（摘除后仍能读到，正好用来证明旧代码还在跑）。"""
    path = Path(root) / 'plugin_state' / mod_id / 'calls.json'
    if not path.is_file():
        return 0
    return int(json.loads(path.read_text(encoding='utf-8')).get('peek', 0))


class Counter:
    """把真 open_tools 包一层：数调用次数；fail=True 时改成抛异常，模拟建表失败。"""

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self._real = agent_tools.open_tools

    def __call__(self, bot, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError('建表失败（测试桩）')
        return self._real(bot)


class PlainEngine:
    """只要 engine.chat 这个形状：回一条没有 tool_calls 的纯文本，环就一轮收尾。"""

    def __init__(self):
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        return {'text': '好', 'tool_calls': []}


def fake_agent(bot):
    """真 ConsoleAgent、真 _tool_turn，只跳过 __init__ 里的存储/记忆构造（见模块头）。"""
    agent = ConsoleAgent.__new__(ConsoleAgent)
    agent.bot = bot
    agent._tools = None
    agent._tools_ready = False
    return agent


MESSAGES = [{'role': 'user', 'content': '在吗'}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.addCleanup(self.tmp.cleanup)

    def bot(self, plugins=_MISSING, enabled=True):
        return SimpleNamespace(
            root=self.root, data_root=self.root,
            config={'ai.agent_tools': enabled},
            plugins=self.manager if plugins is _MISSING else plugins)

    def turn(self, agent, engine, prompt='系统'):
        return asyncio.run(agent._tool_turn(engine, list(MESSAGES), prompt))

    def names(self, tools):
        return list(tools.runtime.tool_registry.names())


class RevisionTests(Base):
    """A 组：修订号本身——哪些操作让它 +1，哪些不该让它动。"""

    def test_挂上摘掉重挂与_reload_都让修订号变大(self):
        self.assertEqual(self.manager.mod_revision, 0)
        folder = write_mod(self.root)
        # 只是把目录写出来，还没 sync：插件集合没变，修订号不该动。
        self.assertEqual(self.manager.mod_revision, 0)

        report = self.manager.sync_mods()
        self.assertEqual(report['added'], [MOD_ID])
        mounted = self.manager.mod_revision
        self.assertGreater(mounted, 0)

        # 集合没变的一轮：unchanged，修订号不许涨（否则工具表会白重建一次）。
        report = self.manager.sync_mods()
        self.assertEqual(report['unchanged'], [MOD_ID])
        self.assertEqual(self.manager.mod_revision, mounted)

        self.manager.reload()
        reloaded = self.manager.mod_revision
        self.assertGreater(reloaded, mounted)

        shutil.rmtree(folder)
        report = self.manager.sync_mods()
        self.assertEqual(report['removed'], [MOD_ID])
        self.assertGreater(self.manager.mod_revision, reloaded)

    def test_没挂过东西的_manager_从零开始且只读(self):
        self.assertEqual(self.manager.mod_revision, 0)
        with self.assertRaises(AttributeError):
            self.manager.mod_revision = 7

    def test_new_造出来的_manager_取值安全(self):
        bare = PluginManager.__new__(PluginManager)      # 没有 __init__，没有 _mod_revision
        self.assertEqual(bare.mod_revision, 0)
        self.assertEqual(agent_tools.mod_revision_of(bare), 0)


class RebuildOnChangeTests(Base):
    """B 组：过期判定——没变就复用（open_tools 只被调一次），变了恰好重建一次。"""

    def test_没变就复用_挂了_mod_只重建一次(self):
        bot = self.bot()
        agent = fake_agent(bot)
        engine = PlainEngine()
        counter = Counter()

        with patch('src.agent_tools.open_tools', counter):
            self.turn(agent, engine)
            first = agent._tools
            built = list(self.names(first))
            self.turn(agent, engine)                     # 第二轮：集合没变
            self.turn(agent, engine)                     # 第三轮：还是没变
        self.assertEqual(counter.calls, 1)               # 只建了一次表
        self.assertIs(agent._tools, first)               # 而且一直是同一张表
        self.assertNotIn(PEEK, built)
        self.assertEqual(sorted(WORKSPACE_NAMES), sorted(built))

        write_mod(self.root)
        self.manager.sync_mods()
        with patch('src.agent_tools.open_tools', counter):
            self.turn(agent, engine)                     # 这一轮才发现集合变了
        self.assertEqual(counter.calls, 2)               # 恰好重建一次
        self.assertIsNot(agent._tools, first)
        self.assertIn(PEEK, self.names(agent._tools))    # 热挂的 mod 工具进了新表
        self.assertEqual(agent._tools.mod_revision, self.manager.mod_revision)

        with patch('src.agent_tools.open_tools', counter):
            self.turn(agent, engine)                     # 重建后又不该再动
        self.assertEqual(counter.calls, 2)

    def test_摘掉_mod_后重建_旧工具名从表里消失(self):
        write_mod(self.root)
        self.manager.sync_mods()
        bot = self.bot()
        agent = fake_agent(bot)
        engine = PlainEngine()

        tools = agent_tools.open_tools(bot)
        self.assertIn(PEEK, self.names(tools))

        counter = Counter()
        with patch('src.agent_tools.open_tools', counter):
            self.turn(agent, engine)                     # 第一次：建表
        self.assertEqual(counter.calls, 1)
        old_tools = agent._tools
        self.assertIn(PEEK, self.names(old_tools))
        old_spec = old_tools.runtime.tool_registry.get(PEEK)
        self.assertIsNotNone(old_spec)

        shutil.rmtree(self.root / 'custom_plugins' / MOD_ID)
        self.manager.sync_mods()
        self.assertEqual(self.manager._mod_plugins, {})
        with patch('src.agent_tools.open_tools', counter):
            self.turn(agent, engine)                     # 第二次：摘除后重建
        self.assertEqual(counter.calls, 2)
        self.assertIsNot(agent._tools, old_tools)
        self.assertNotIn(PEEK, self.names(agent._tools))          # 表里查不到旧工具名
        self.assertNotIn(MOD_ID, agent._tools.sources())
        self.assertEqual(sorted(WORKSPACE_NAMES), sorted(self.names(agent._tools)))

        # 旧 registry 里那个 spec 还连着旧实例，直接调它**真的会执行**已卸载的代码——
        # 这就是改动前每轮都会走到的那条路（旧表一直被复用，spec 从没被清过）。
        before = mod_calls(self.root)
        result = asyncio.run(asyncio.to_thread(old_spec.handler, old_spec.raw_name, {'what': 'x'}))
        # mod 工具宿主会把返回值包一层（ok/tool/...），这里只认"旧 handler 真的跑了"。
        self.assertIn('peeked:x', json.dumps(result, ensure_ascii=False))
        self.assertEqual(mod_calls(self.root), before + 1)

        # 现在走不到的原因：_tool_turn 在修订号变了的那一轮把 self._tools 换成了新表，
        # ConsoleTools.ask 每次按 self.runtime.tool_registry.specs() 现算（src/agent_tools.py:487），
        # 新表里没有这个 spec，模型点名也只会得到"未知工具"。旧表只还被测试自己的
        # old_tools 引用着，控制台这条路上已经没有任何东西指向它。
        self.assertIsNot(agent._tools.runtime.tool_registry, old_tools.runtime.tool_registry)
        self.assertIsNone(agent._tools.runtime.tool_registry.get(PEEK))

    def test_重挂同一版本也重建_因为_handler_换了新实例(self):
        write_mod(self.root)
        self.manager.sync_mods()
        agent = fake_agent(self.bot())
        engine = PlainEngine()
        counter = Counter()
        with patch('src.agent_tools.open_tools', counter):
            self.turn(agent, engine)
        first = agent._tools
        self.manager.reload()                            # 集合一样，实例全换了
        with patch('src.agent_tools.open_tools', counter):
            self.turn(agent, engine)
        self.assertEqual(counter.calls, 2)
        self.assertIsNot(agent._tools, first)


class RebuildFailureTests(Base):
    """C 组：重建失败的降级——留住旧表、不报错、不每轮重试。"""

    def test_重建失败留住旧表且对话不报错(self):
        agent = fake_agent(self.bot())
        engine = PlainEngine()
        counter = Counter()
        with patch('src.agent_tools.open_tools', counter):
            self.turn(agent, engine)
        old_tools = agent._tools
        self.assertIsNotNone(old_tools)

        write_mod(self.root)
        self.manager.sync_mods()                         # 修订号涨了，下一轮必然想重建
        broken = Counter(fail=True)
        with patch('src.agent_tools.open_tools', broken):
            text = self.turn(agent, engine)
            self.assertEqual(text, '好')                 # 对话没被打断
            self.assertIs(agent._tools, old_tools)       # 旧表继续服务
            self.assertEqual(broken.calls, 1)
            self.turn(agent, engine)                     # 同一个修订号不重试
            self.assertEqual(broken.calls, 1)
            self.assertIs(agent._tools, old_tools)

        # 集合再变一次，才重新试一次（重试的机会来自新变化，不是靠每轮撞）。
        write_mod(self.root, mod_id='inv_rebuild_mod2')
        self.manager.sync_mods()
        with patch('src.agent_tools.open_tools', broken):
            self.turn(agent, engine)
        self.assertEqual(broken.calls, 2)
        self.assertIs(agent._tools, old_tools)

    def test_第一次建表就失败_与改动前一样永久回落纯_chat(self):
        agent = fake_agent(self.bot())
        engine = PlainEngine()
        broken = Counter(fail=True)
        with patch('src.agent_tools.open_tools', broken):
            self.assertIsNone(self.turn(agent, engine))
            self.assertIsNone(agent._tools)
            self.assertTrue(agent._tools_ready)

            write_mod(self.root)
            self.manager.sync_mods()
            self.assertIsNone(self.turn(agent, engine))   # 仍然回落
            self.assertEqual(broken.calls, 1)             # 但没有再试一次
        self.assertEqual(engine.calls, [])


class SwitchOffTests(Base):
    """D 组：开关关着时行为与改动前逐字节相同——不建表、不比较修订号、不报错。"""

    def test_开关关着不建表也不报错(self):
        bot = self.bot(enabled=False)
        self.assertIsNone(agent_tools.open_tools(bot))
        agent = fake_agent(bot)
        engine = PlainEngine()
        counter = Counter()
        with patch('src.agent_tools.open_tools', counter):
            self.assertIsNone(self.turn(agent, engine))
            self.assertIsNone(self.turn(agent, engine))
        self.assertEqual(counter.calls, 0)               # 一次都没建
        self.assertIsNone(agent._tools)
        self.assertFalse(agent._tools_ready)
        self.assertEqual(engine.calls, [])               # 也没问模型

    def test_开关关着时插件层修订号是坏的也不报错(self):
        class Hostile:
            @property
            def mod_revision(self):
                raise RuntimeError('插件层坏了')

        bot = self.bot(plugins=Hostile())
        self.assertEqual(agent_tools.mod_revision_of(bot.plugins), 0)
        agent = fake_agent(bot)
        with patch('src.agent_tools.settings',
                   return_value=agent_tools.ConsoleToolSettings(enabled=False)):
            self.assertIsNone(self.turn(agent, PlainEngine(), None))


class CrippledBotTests(Base):
    """E 组：三种残缺 bot（无 plugins / plugins=None / __new__ 的 manager）都不抛。"""

    def shapes(self):
        bare = PluginManager.__new__(PluginManager)
        return {
            'no-attribute': SimpleNamespace(root=self.root, data_root=self.root,
                                           config={'ai.agent_tools': True}),
            'none': self.bot(plugins=None),
            'new-manager': self.bot(plugins=bare),
        }

    def test_三种残缺_bot_建表与过期判定都不抛(self):
        for label, bot in self.shapes().items():
            with self.subTest(label):
                self.assertEqual(agent_tools.mod_revision_of(getattr(bot, 'plugins', None)), 0)
                tools = agent_tools.open_tools(bot)
                self.assertIsNotNone(tools)
                self.assertEqual(tools.mod_revision, 0)
                self.assertFalse(tools.is_stale(tools.mod_revision))
                self.assertFalse(tools.is_stale(None))          # 取不到当前版本 → 不算过期
                self.assertTrue(tools.is_stale(1))              # 真变了才算

    def test_三种残缺_bot_走_tool_turn_只建一次表(self):
        for label, bot in self.shapes().items():
            with self.subTest(label):
                agent = fake_agent(bot)
                engine = PlainEngine()
                counter = Counter()
                with patch('src.agent_tools.open_tools', counter):
                    self.assertEqual(self.turn(agent, engine), '好')
                    self.assertEqual(self.turn(agent, engine), '好')
                self.assertEqual(counter.calls, 1)
                self.assertIsNotNone(agent._tools)


class MarkAttemptedTests(Base):
    """F 组：ConsoleTools 的过期判定与 mark_attempted 语义（重建降级的地基）。"""

    def test_mark_attempted_让同一个修订号不再算过期(self):
        tools = agent_tools.open_tools(self.bot())
        self.assertEqual(tools.mod_revision, 0)
        tools.mark_attempted(3)
        self.assertEqual(tools.mod_revision, 3)
        self.assertFalse(tools.is_stale(3))
        self.assertTrue(tools.is_stale(4))
        tools.mark_attempted(None)                            # None 不该把戳改坏
        self.assertEqual(tools.mod_revision, 3)

    def test_open_tools_把建表时的修订号带出来(self):
        write_mod(self.root)
        self.manager.sync_mods()
        tools = agent_tools.open_tools(self.bot())
        self.assertEqual(tools.mod_revision, self.manager.mod_revision)
        self.assertGreater(tools.mod_revision, 0)
        self.assertIn(PEEK, list(tools.runtime.tool_registry.names()))


if __name__ == '__main__':
    unittest.main()
