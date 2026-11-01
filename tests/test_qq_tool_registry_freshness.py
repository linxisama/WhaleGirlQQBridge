"""QQ 消息链路的工具表随 PluginManager.reload() 更新（m06）。

改前实测的两个后果（探针 m06_stale_*.py 全 exit=0）：
  ① reload() 之后旧表同一 spec 仍 available=True，调用返回旧版本值（实例早已不在 _plugins）；
  ② 插件文件删掉再 reload()，旧表照样能跑；表建好之后新装的 mod 工具则完全看不见。
根因：registry_for 见到 runtime.tool_registry 是 ToolRegistry 就原样返回（src/tool_loop.py:384-386），
而 register_plugin_tools 只往表里追加、从不注销（src/tool_loop.py:354-379）——那张表等于进程寿命。

修法一句话：插件层已有只读修订号 PluginManager.mod_revision（src/plugins/manager.py:892），
qq_app.QQRuntime.tool_registry_fresh() 建表时记下当时的号（复用 src/agent_tools.py:347
mod_revision_of，不另写判据），之后每次取表前比一次，号变了就删掉 runtime.tool_registry
让 tool_loop.registry_for 自然重建。预热扫描（QQRuntime._tool_warm_scan）与单条消息的工具环
（QQRuntime._tool_registry_refresh_maybe）共用这一个方法，不各写一遍。

本文件的风俗：
  - 一律真 PluginManager + 真插件文件（裸 custom_plugins/<id>.py 与目录 mod 两种），不造假插件；
  - 工具在表里叫 <插件id>__<工具名>（src/tool_loop.py:246 的来源前缀）；
  - 不跑 start()/stop()：这次修的就是"哪个入口负责取表"，直接调那两个入口同样能钉死行为，
    比拉起整个运行循环脆得多。
"""
import asyncio
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src import tool_loop
from src.qq_app import QQRuntime
from src.plugins.manager import PluginManager

BARE_ID = 'inv_fresh_probe'
BARE_TOOL = BARE_ID + '__ver'
MOD_ID = 'inv_fresh_mod'
MOD_TOOL = MOD_ID + '__peek'
SKILL_SRC = 'skill:demo'
SKILL_TOOL = 'skill_demo__demo'          # skill_tools.register_skills 就是这么落表的
SKILL_TOOL_SPEC = {'name': 'demo', 'description': '假 skill 工具，只为验名字',
                   'parameters': {'type': 'object', 'properties': {}}}


def skill_handler(name, arguments=None):
    """假 skill 工具的 handler：回一句固定的，用来证明重建后绑的还是活 handler。"""
    return {'echo': 'x'}


SUFFIXED = re.compile(r'_[2-9]\d*$')
_ABSENT = object()          # "这个属性根本不存在"，与 None 区分开

# 裸 .py 插件：工具返回**源码里的**版本号。改文件 + reload 之后，旧表能给出的仍然是旧号，
# 这就是"过期"最直接的证据。
BARE_SRC = '''from . import PluginBase, PluginMeta


class InvFreshProbe(PluginBase):
    META = PluginMeta(id="__PID__", name="工具表保鲜探针", category="utility", description="测试用")
    VERSION = "__VER__"

    def list_tools(self):
        return [{"name": "ver", "description": "读版本号",
                 "parameters": {"type": "object", "properties": {}}}]

    def call_tool(self, name, arguments=None):
        return {"version": self.VERSION}


'''

# 目录 mod（真目录约定）：handler 每跑一次就在自己的 state 里 +1，跑没跑有返回值之外的证据。
MOD_SRC = '''from . import PluginBase, PluginMeta


class InvFreshMod(PluginBase):
    META = PluginMeta(id="__PID__", name="工具表保鲜 mod", category="utility", description="测试用")

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


def build_root(tmp, tool_loop_on=True):
    """QQRuntime 只要 ai/qq/style/qq_only 四段（src/qq_app.py:339），最小可跑即可。"""
    root = Path(tmp)
    (root / 'styles' / 'custom').mkdir(parents=True, exist_ok=True)
    (root / 'styles' / 'default.yaml').write_text(
        chr(10).join(['name: default', 'display_name: 默认',
                      'system_prompt: 你是默认助手。']) + chr(10), encoding='utf-8')
    (root / 'data' / 'qq-only').mkdir(parents=True, exist_ok=True)
    ai = ['ai:', '  api_url: https://example.test/v1', '  api_key: fake',
          '  model: test-model', '  reply_timeout: 8']
    if tool_loop_on:
        ai.append('  tool_loop: true')
    lines = ai + ['qq:', '  processing_status: false',
                  'qq_only:', '  mode: onebot', '  listen_private: true',
                  '  onebot_token: token123']
    (root / 'config.qq.yaml').write_text(chr(10).join(lines) + chr(10), encoding='utf-8')
    return root


def call_tool(registry, name, arguments=None):
    """按工具环的真实调用约定执行一个 spec：handler(raw_name, arguments)。"""
    spec = registry.get(name)
    assert spec is not None, f'工具 {name} 不在表里或不可用：{registry.names()}'
    return spec.handler(spec.raw_name, arguments or {})


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / 'config.qq.yaml',
                             db_path=self.root / 't.sqlite3')
        self.addCleanup(self._close, self.bot)
        self.manager = self.bot.plugins
        self.assertTrue(isinstance(self.manager, PluginManager))
        self.custom_dir = self.manager.custom_plugins_dir

    @staticmethod
    def _close(bot):
        try:
            bot.db.close()
        except Exception:
            pass

    # ── 落盘 ───────────────────────────────────────────────────────────────
    def write_bare(self, version, plugin_id=BARE_ID):
        path = self.custom_dir / f'{plugin_id}.py'
        path.write_text(BARE_SRC.replace('__PID__', plugin_id).replace('__VER__', version),
                        encoding='utf-8')
        self.manager.register_custom(plugin_id)
        return path

    def write_mod(self, mod_id=MOD_ID):
        folder = self.custom_dir / mod_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'mod.py').write_text(MOD_SRC.replace('__PID__', mod_id), encoding='utf-8')
        (folder / 'plugin.json').write_text(json.dumps({
            'id': mod_id, 'api_version': 1, 'entry': 'mod.py',
            'permissions': ['register_tool'], 'enabled_by_default': True}),
            encoding='utf-8')
        return folder

    def mod_calls(self, mod_id=MOD_ID):
        # mod 的 state 落在插件层的数据目录下（data_root 可能是 None，manager.data_dir 才是准的）。
        path = Path(self.manager.data_dir) / 'plugin_state' / mod_id / 'calls.json'
        if not path.is_file():
            return 0
        return int(json.loads(path.read_text(encoding='utf-8')).get('peek', 0))

    def names(self, registry=None):
        return list((registry or self.bot.tool_registry).names())


class FreshnessTests(Base):
    """A 组：真裸 .py 插件——改源码 + reload 之后，新入口必须给出新版本；不经过它就还是旧版本。"""

    def test_改源码_reload_后新入口换新表且绑新实例(self):
        self.write_bare('v1')
        self.manager.reload()
        old = self.bot.tool_registry_fresh()
        self.assertIn(BARE_TOOL, self.names(old))
        self.assertEqual(call_tool(old, BARE_TOOL), {'version': 'v1'})
        self.assertEqual(self.bot._tool_registry_mod_rev, self.manager.mod_revision)
        old_instance = self.manager._plugins[BARE_ID]
        built = self.bot._tool_registry_builds
        self.assertEqual(built, 1)

        self.write_bare('v2')
        self.manager.reload()
        self.assertGreater(self.manager.mod_revision, self.bot._tool_registry_mod_rev)
        self.assertIsNot(self.manager._plugins[BARE_ID], old_instance)   # 实例确实换了

        # 对照：不经过新入口，旧表原封不动地继续返回旧版本——这就是改前的每一条消息。
        same = tool_loop.registry_for(self.bot)
        self.assertIs(same, old)
        self.assertEqual(call_tool(same, BARE_TOOL), {'version': 'v1'})

        fresh = self.bot.tool_registry_fresh()
        self.assertIsNot(fresh, old)
        self.assertEqual(call_tool(fresh, BARE_TOOL), {'version': 'v2'})
        self.assertEqual(self.bot._tool_registry_mod_rev, self.manager.mod_revision)
        self.assertEqual(self.bot._tool_registry_builds, built + 1)

        # 重建之后同一个号不再动表。
        again = self.bot.tool_registry_fresh()
        self.assertIs(again, fresh)
        self.assertEqual(self.bot._tool_registry_builds, built + 1)

    def test_号没变就不重建(self):
        self.write_bare('v1')
        self.manager.reload()
        table = self.bot.tool_registry_fresh()
        for _ in range(5):
            self.assertIs(self.bot.tool_registry_fresh(), table)
            self.assertIs(self.bot._tool_registry_refresh_maybe(), table)
        self.assertEqual(self.bot._tool_registry_builds, 1)


class RemovalTests(Base):
    """B 组：摘除——文件删掉 + reload，新入口之后该工具连名字都不该在表里。"""

    def test_删掉插件目录_reload_后工具从表里消失(self):
        folder = self.write_mod()
        self.manager.reload()
        old = self.bot.tool_registry_fresh()
        self.assertIn(MOD_TOOL, self.names(old))
        result = call_tool(old, MOD_TOOL, {'what': 'a'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['value'], 'peeked:a')
        self.assertEqual(self.mod_calls(), 1)

        shutil.rmtree(folder)
        self.manager.reload()
        self.assertEqual(self.manager._mod_plugins, {})

        # 旧表照样能跑（不靠 available 骗人——它真的执行了已经卸载的代码）。
        self.assertIs(tool_loop.registry_for(self.bot), old)
        self.assertTrue(old.get(MOD_TOOL) is not None)
        self.assertEqual(self.mod_calls(), 1)          # 还没再调，先确认基线

        fresh = self.bot.tool_registry_fresh()
        self.assertIsNot(fresh, old)
        self.assertNotIn(MOD_TOOL, self.names(fresh))
        self.assertIsNone(fresh.get(MOD_TOOL))
        self.assertNotIn(MOD_ID, [spec.source for spec in fresh._tools.values()])


class NewInstallTests(Base):
    """C 组：新装——表建好之后再装一个 mod，reload 后新入口必须看得见、且能真调。"""

    def test_表建好之后新装的_mod_进得来而且能调(self):
        self.write_bare('v1')
        self.manager.reload()
        first = self.bot.tool_registry_fresh()
        self.assertNotIn(MOD_TOOL, self.names(first))

        self.write_mod()
        self.manager.reload()
        fresh = self.bot.tool_registry_fresh()
        self.assertIsNot(fresh, first)
        self.assertIn(MOD_TOOL, self.names(fresh))
        result = call_tool(fresh, MOD_TOOL, {'what': 'b'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['value'], 'peeked:b')
        self.assertEqual(self.mod_calls(), 1)


class NoAccumulationTests(Base):
    """D 组：连续 5 次号变化之后，表里不许攒出 _2/_3 之类重名后缀。"""

    def test_连续五次变化不攒重名后缀(self):
        self.write_bare('v1')
        self.write_mod()
        self.manager.reload()
        table = self.bot.tool_registry_fresh()
        self.assertIn(BARE_TOOL, self.names(table))
        self.assertIn(MOD_TOOL, self.names(table))
        baseline = set(self.names(table))

        for index in range(5):
            self.write_bare('v%d' % (index + 2))
            self.manager.reload()
            table = self.bot.tool_registry_fresh()
            self.assertEqual(set(self.names(table)), baseline)
            self.assertEqual([name for name in self.names(table) if SUFFIXED.search(name)], [])
            self.assertEqual(call_tool(table, BARE_TOOL), {'version': 'v%d' % (index + 2)})

        self.assertEqual(self.bot._tool_registry_builds, 6)      # 初次 + 5 次变化
        self.assertEqual(len([n for n in table._tools if SUFFIXED.search(n)]), 0)

    def test_技能工具反复重建重注册也不攒后缀(self):
        """skill_tools.register_skills 落表走的就是 tool_loop.register_tools（src/tool_loop.py:404-406）。

        这条路径与插件工具共用同一个 ToolRegistry._add 撞名逻辑（src/tool_loop.py:245-255）：
        表若被复用，重扫一次就多出 skill_demo__demo_2。修好之后每次注册都落在新表上，
        所以连续重扫也只该有干净的那一份。
        """
        self.write_bare('v1')
        self.manager.reload()
        for index in range(3):
            self.write_bare('v%d' % (index + 2))
            self.manager.reload()
            table = self.bot.tool_registry_fresh()
            count = tool_loop.register_tools(self.bot, SKILL_SRC, [dict(SKILL_TOOL_SPEC)],
                                             handler=skill_handler)
            self.assertEqual(count, 1)
            names = self.names(table)
            self.assertEqual([n for n in names if n == SKILL_TOOL], [SKILL_TOOL], names)
            self.assertEqual([n for n in names if SUFFIXED.search(n)], [], names)
            self.assertEqual(call_tool(table, SKILL_TOOL, {'q': 'x'}), {'echo': 'x'})


class AvailableSemanticsTests(Base):
    """E 组：把**既有**的 available 语义钉死，别让这次改动顺手改了它。

    停用/启用是活读 settings：号不变 → 不重建 → 旧 spec 仍然 available=True（看得见），
    点了才被 mod 工具宿主拒掉（code=disabled）。reload 之后才会因为重建而消失。
    """

    def test_停用后旧表仍可见但调用被拒_disabled(self):
        self.write_mod()
        self.manager.reload()
        table = self.bot.tool_registry_fresh()
        self.assertIn(MOD_TOOL, self.names(table))

        self.manager.update_settings(MOD_ID, {'enabled': False})
        self.assertFalse(self.manager._plugins[MOD_ID].enabled)
        self.assertIs(self.bot.tool_registry_fresh(), table)     # 号没变：一张表都不重建
        self.assertEqual(self.bot._tool_registry_builds, 1)

        spec = table.get(MOD_TOOL)
        self.assertIsNotNone(spec)
        self.assertTrue(spec.available)                          # 可见
        result = spec.handler(spec.raw_name, {})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'disabled')             # 点了被拒
        self.assertEqual(result['error']['code'], 'disabled')

        self.manager.reload()                                    # 重载才算集合变了
        fresh = self.bot.tool_registry_fresh()
        self.assertIsNot(fresh, table)
        self.assertNotIn(MOD_TOOL, self.names(fresh))             # 新表里它真的没了


class RebuildFailureTests(Base):
    """F 组：重建失败的取舍——保留旧表、记下"这个号试过了"、不每条消息重试。"""

    def test_重建失败保留旧表且同一个号只试一次(self):
        self.write_bare('v1')
        self.manager.reload()
        old = self.bot.tool_registry_fresh()

        self.write_bare('v2')
        self.manager.reload()
        revision = self.manager.mod_revision
        real = tool_loop.registry_for
        with mock.patch('src.tool_loop.registry_for', side_effect=RuntimeError('建表失败（测试桩）')) as spy:
            with self.assertRaises(RuntimeError):
                self.bot.tool_registry_fresh()
            self.assertIs(self.bot.tool_registry, old)            # 旧表被放回来了
            self.assertEqual(self.bot._tool_registry_mod_rev, revision)   # 这个号算试过

            # 消息链路：不抛，直接拿旧表继续服务，而且同一个号没有第二次建表。
            self.assertIs(self.bot._tool_registry_refresh_maybe(), old)
            self.assertIs(self.bot.tool_registry, old)
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(self.bot._tool_registry_builds, 2)   # 初次 + 这一次（失败也算试过）

        self.assertIs(self.bot.tool_registry_fresh(), old)         # 号没变：直接复用旧表
        self.assertIs(real, tool_loop.registry_for)               # 补丁已撤

        self.write_bare('v3')
        self.manager.reload()                                     # 新变化 → 重新有机会
        fresh = self.bot.tool_registry_fresh()
        self.assertIsNot(fresh, old)
        self.assertEqual(call_tool(fresh, BARE_TOOL), {'version': 'v3'})

    def test_没有表时消息链路绝不抢先建表(self):
        self.assertIsNone(getattr(self.bot, 'tool_registry', None))
        for _ in range(3):
            self.assertIsNone(self.bot._tool_registry_refresh_maybe())
        self.assertIsNone(getattr(self.bot, 'tool_registry', None))
        self.assertEqual(self.bot._tool_registry_builds, 0)


class WarmPassthroughTests(Base):
    """G 组：两个入口共用同一份实现——预热扫描与消息链路都落到 tool_registry_fresh。"""

    def test_预热扫描与消息链路都走_tool_registry_fresh(self):
        self.write_bare('v1')
        self.manager.reload()
        calls = []
        real = self.bot.tool_registry_fresh

        def spy():
            calls.append(1)
            return real()

        self.bot.tool_registry_fresh = spy
        fake_skills = mock.Mock(return_value={'registered': 0, 'skipped': 0, 'failed': 0,
                                             'counts': {}})
        with mock.patch('src.skill_tools.register_skills', fake_skills):
            asyncio.run(self.bot._tool_warm_scan('test'))
        self.assertEqual(len(calls), 1)
        self.assertIn(BARE_TOOL, self.names())

        self.write_bare('v2')
        self.manager.reload()
        self.assertIsNotNone(self.bot._tool_registry_refresh_maybe())
        self.assertEqual(len(calls), 2)
        self.assertEqual(call_tool(self.bot.tool_registry, BARE_TOOL), {'version': 'v2'})
        self.assertEqual(self.bot._tool_warm_state.get('total'), len(self.names()))


class CrippledBotTests(Base):
    """H 组：三种残缺插件层都不抛，且不会无谓失效（计数器证明"号没变就不重建"）。"""

    def shapes(self):
        """三种残缺插件层。用最小 Shell 而不是 Mock：Mock 的 hasattr 恒真，测不出"属性缺失"。"""
        root = self.bot.data_root
        config = self.bot.config

        class Shell(QQRuntime):
            """真的 QQRuntime 方法 + 残缺的插件层；不跑 __init__（不建库、不装插件）。"""

            _tool_registry_builds = 0

            def __init__(self, plugins=_ABSENT):
                self.root = self.data_root = root
                self.config = config
                if plugins is not _ABSENT:
                    self.plugins = plugins

        return {
            'no-attribute': Shell(),                                  # 连 plugins 属性都没有
            'none': Shell(plugins=None),
            'new-manager': Shell(plugins=PluginManager.__new__(PluginManager)),
        }

    def test_残缺插件层不抛且不无谓失效(self):
        for label, bot in self.shapes().items():
            with self.subTest(label):
                builds = getattr(bot, '_tool_registry_builds', 0)
                table = QQRuntime.tool_registry_fresh(bot)
                self.assertIsNotNone(table)
                for _ in range(3):
                    self.assertIs(QQRuntime.tool_registry_fresh(bot), table)
                    QQRuntime._tool_registry_refresh_maybe(bot)
                self.assertEqual(bot._tool_registry_builds, builds + 1)

    def test_残缺插件层的修订号恒为零(self):
        for label, bot in self.shapes().items():
            with self.subTest(label):
                self.assertEqual(QQRuntime._tool_registry_revision(bot), 0)


class PresetTableTests(Base):
    """I 组：表不是 tool_registry_fresh 自己建的（测试预置、别处先 registry_for 了）时，
    号没变就不许把它当"过期"删掉。

    回归现场：QQRuntime.__init__ 把建表记号留成 None，而 _tool_registry_revision() 对
    "没有插件层"返回 0；None != 0 让 tool_registry_fresh() 认定表过期，于是 del
    runtime.tool_registry 重建出一张空表——测试预置的 echo 工具就此消失，消息链路
    tool_loop.should_use() 变 False、这一轮不带工具问模型，回复为空（tests/test_tool_loop_e2e.py:190
    的 'uncertain' != 'sent'）。没记过号 == 0 号：号没变，就该原样交出别人建的那张表。
    """

    TOOL = 'preset__echo'

    def preset(self):
        registry = tool_loop.ToolRegistry()
        registry.register_tools('preset', [{'name': 'echo', 'description': '回声',
                                            'parameters': {'type': 'object', 'properties': {}}}],
                                handler=lambda name, arguments: {'echo': (arguments or {}).get('text', '')})
        self.bot.tool_registry = registry
        return registry

    def test_预置的表在号没变时原样返回(self):
        preset = self.preset()
        self.assertEqual(self.bot._tool_registry_revision(), 0)    # 无插件层：号就是 0，且没变过
        table = self.bot.tool_registry_fresh()
        self.assertIs(table, preset)                               # 同一张表，不是重建的空表
        self.assertIn(self.TOOL, table.names())                    # 工具还在
        self.assertIs(self.bot.tool_registry, preset)
        self.assertEqual(self.bot._tool_registry_builds, 0)        # 一次都没重建
        self.assertIs(self.bot._tool_registry_refresh_maybe(), preset)   # 消息链路同样不动它


if __name__ == '__main__':
    unittest.main()
