"""控制台主体 Agent 的工具调用接线（工作包 W2）。

三类工具收进同一张 tool_loop.ToolRegistry：工作区工具（src/agent_workspace.py 的
读/写/追加/列目录）、扩展工具（src/plugins/qqagent_bridge.py 的 list_tools()/call_tool()）、
skill 工具（src/skill_tools.py）。控制台自己只写一层薄驱动——
engine.chat → tool_loop.parse_tool_calls → tool_loop.run_calls → 结果回灌；
执行、超时、审计、未知工具、被拦截等语义全部沿用 tool_loop 的既有实现。

开关 ai.agent_tools 默认关闭：关着时 open_tools() 返回 None，ConsoleAgent 完全走原来那一次
chat，本模块连工具表都不建。ai.tool_loop 是 QQ 主链路的开关，与本模块互不隐含。
"""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import agent_workspace, tool_loop
# 确认口径只有一份实现（src/plugins/__init__.py）：闸门与 mod 工具宿主都调它，
# 避免"闸门放行、mod 层再拒一次"。
from .plugins import CONFIRM_KEY, confirm_flag, has_confirm

DEFAULT_MAX_ROUNDS = 3
DEFAULT_TIMEOUT = 15.0
MAX_ROUNDS = (1, 5)
TIMEOUT_RANGE = (1.0, 60.0)
# 一次 chat 的上限，以及控制台外层 asyncio.wait_for(90s) 之内留给工具环的总预算。
CHAT_TIMEOUT = 75.0
WALL_BUDGET = 84.0
WORKSPACE_SOURCE = 'workspace'
CONSOLE_SOURCE = 'console'
AGENT_ID = 'console'
SKILL_LIMIT = 20
# 插件（mod）工具进工具表后的真实形态是 <插件id>__<工具名>：tool_loop.register_plugin_tools
# 拿插件实例的 dict 键（= 插件 id）当 source（src/tool_loop.py:354-379），ToolRegistry._add
# 再拼上来源前缀（src/tool_loop.py:246）。哪些插件 id 是 mod，唯一判据是
# PluginManager._mod_plugins（插件 id -> mod id，src/plugins/manager.py:380）。
# MOD_NAMESPACE 只留给"插件 id 字面就叫 mod"的兼容分支，不再是主判据。
MOD_NAMESPACE = 'mod'
# CONFIRM_KEY 由 src/plugins/__init__.py 导入（上面那条 import），不在这里另写一份：
# 两处各写一个字面量就是 B 类口径漂移的起点。
# 缺 confirm 时回给模型的原因/提示：写工具不许静默放行，也不许模型自己拍板。
MOD_CONFIRM_REASON = ('工具 %s 是插件（mod）暴露的工具，执行会改动机器状态，必须先经用户确认：'
                      '把这次调用要做什么、会改哪里说给用户听，用户明确同意后再带 '
                      + CONFIRM_KEY + '=true 重新调用')
MOD_CONFIRM_HINT = ('插件工具（名字形如 <插件id>__<工具名>）一律按写操作对待：调用前先向用户说明影响，'
                    '拿到明确同意才带 ' + CONFIRM_KEY + '=true 调用；用户没点头就不要自己加确认')

WORKSPACE_TOOLS = (
    {'name': 'read',
     'description': '读取工作区里一个文本文件的内容。路径用工作区内的相对路径。',
     'parameters': {'type': 'object', 'properties': {
         'relpath': {'type': 'string', 'description': '工作区内的相对路径，例如 notes/todo.txt'}},
         'required': ['relpath']}},
    {'name': 'write',
     'description': '把文本整篇写入工作区的一个文件（覆盖已有内容，工作区内不需要额外授权）。',
     'parameters': {'type': 'object', 'properties': {
         'relpath': {'type': 'string', 'description': '工作区内的相对路径'},
         'text': {'type': 'string', 'description': '要写入的完整文本'}},
         'required': ['relpath', 'text']}},
    {'name': 'append',
     'description': '在工作区文件末尾追加一行或一段文本；文件不存在时会新建。',
     'parameters': {'type': 'object', 'properties': {
         'relpath': {'type': 'string', 'description': '工作区内的相对路径'},
         'text': {'type': 'string', 'description': '要追加的文本'}},
         'required': ['relpath', 'text']}},
    {'name': 'list',
     'description': '列出工作区某个目录下的文件与子目录。',
     'parameters': {'type': 'object', 'properties': {
         'relpath': {'type': 'string', 'description': '目录的相对路径；留空表示工作区根目录'}},
         'required': []}},
)

# 工具名里出现这些词就说明它会改本机状态（启停服务、敲键盘、开窗口…）。
# 这类调用一律拦下：控制台的危险动作必须先变成 actions 里的对应动作，由用户点一次确认。
MACHINE_PARTS = ('service', 'open_path', 'open_url', 'type_text', 'hotkey', 'screenshot',
                 'keyboard', 'keypress', 'key_press', 'keystroke', 'click', 'mouse',
                 'launch', 'shutdown', 'reboot', 'kill', 'terminate', 'exec', 'shell',
                 'powershell', 'registry', 'install', 'uninstall', 'delete', 'unlink')


def _flag(value):
    """配置开关的真值判定；与 confirm 复用同一张真值表（plugins.confirm_flag）。"""
    return confirm_flag(value)


def _cfg_get(config, key, default):
    getter = getattr(config, 'get', None)
    if not callable(getter):
        return default
    try:
        value = getter(key, default)
    except Exception:
        return default
    return default if value is None else value


def _number(value, low, high, default, cast):
    try:
        number = cast(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(low, min(high, number))


@dataclass
class ConsoleToolSettings:
    """控制台工具环的三个口径：开关、轮数、单次工具超时。"""

    enabled: bool = False
    max_rounds: int = DEFAULT_MAX_ROUNDS
    timeout: float = DEFAULT_TIMEOUT

    def loop_settings(self):
        """交给 tool_loop.run_calls 的设置；只读本包的三个键，与 ai.tool_loop 无关。"""
        return tool_loop.LoopSettings(enabled=True, owner_only=False,
                                      max_rounds=self.max_rounds, tool_timeout=self.timeout,
                                      round_timeout=CHAT_TIMEOUT, hint=tool_loop.TOOL_HINT)


def settings(config=None):
    """读三个配置键：缺项就用本模块默认值，不做任何写入。"""
    return ConsoleToolSettings(
        enabled=_flag(_cfg_get(config, 'ai.agent_tools', False)),
        max_rounds=_number(_cfg_get(config, 'ai.agent_tools_max_rounds', DEFAULT_MAX_ROUNDS),
                           MAX_ROUNDS[0], MAX_ROUNDS[1], DEFAULT_MAX_ROUNDS, int),
        timeout=_number(_cfg_get(config, 'ai.agent_tools_timeout', DEFAULT_TIMEOUT),
                        TIMEOUT_RANGE[0], TIMEOUT_RANGE[1], DEFAULT_TIMEOUT, float))


def _mod_id_set(value):
    """mod 名单规整成 set：空串丢掉，否则 '__peek' 会被 startswith('__') 认成 mod 工具。"""
    return {str(item) for item in (value or ()) if str(item)}


class DangerGate:
    """把 tool_loop.run_calls 的 before 钩子接到控制台的危险动作闸门上。

    工作区工具（source 为 workspace）直接放行——用户已经把记事本权限给了 agent。哪些名字算
    工作区工具，看的是 register_workspace 的**注册结果**（workspace_tools），不是名字前缀：
    名字前缀分不开"真工作区工具"与"某个 id 恰好叫 workspace 的 mod 的同名工具"。插件（mod）
    工具（名字是 <插件id>__<工具名>，id 来自 PluginManager._mod_plugins）走确认闸门：参数里
    没有 confirm 真值就拦下，把"要先问用户"的原因和系统提示一起回给模型，由模型下一轮向用户
    确认、用户同意后再带 confirm=true 重发——这与 tools/whale_tools_mcp.py 的 confirm-required
    是同一条语义，落表未执行时 run_calls 会记 'blocked' 审计。其余来源只要工具名带
    MACHINE_PARTS 里的词就拦下（改本机状态的活一律走 actions 协议、由用户点一次确认）；
    mod 工具即使带了 confirm，撞上这些词也一样拦。

    mod 名单是**活的**：构造时仍可直接给 mod_ids（静态集合，语义一字不变），也可以给
    mod_ids_provider（无参可调用对象，每次判定前现取最新名单）。build_runtime 走后者：
    目录 mod 增量热重载（PluginManager.sync_mods）挂上来的新 mod 立刻进闸门，而不是停留在
    build_runtime 那一刻的快照。优先级见 mod_ids 属性：手工赋值 > provider > 构造时的集合。
    """

    def __init__(self, machine_parts=MACHINE_PARTS, mod_ids=(), workspace_tools=(),
                 mod_ids_provider=None):
        self.machine_parts = tuple(machine_parts)
        # mod 工具的真实判据：PluginManager._mod_plugins 的键（插件 id），由 build_runtime 灌进来。
        # 这里只存"已知集合"（构造时的值，或后来手工赋的值）；活名单见 mod_ids 属性。
        self._mod_ids = _mod_id_set(mod_ids)
        self._mod_ids_manual = False
        # 无参可调用对象（通常是 lambda: mod_ids_of(bot.plugins)）：判定前现取最新名单。
        # 传了不可调用的东西就当没有 provider——闸门不许因为调用方写错而抛。
        self.mod_ids_provider = mod_ids_provider if callable(mod_ids_provider) else None
        # 工作区工具的真名单：register_workspace 实际注册出的工具名（同样由 build_runtime 灌）。
        # 只给名字前缀是不够的——mod id 恰好叫 workspace 时，workspace__read 与那个 mod 的
        # 工具前缀一模一样，只有注册来源分得开。
        self.workspace_tools = {str(item) for item in (workspace_tools or ()) if str(item)}
        self.blocked = []
        self.confirmed = []

    @property
    def mod_ids(self):
        """当前生效的 mod id 集合。优先级：手工赋值 > provider > 构造时的集合。

        手工赋值优先，是为了保住打桩与应急能力：测试里 gate.mod_ids = set() 模拟闸门失效，
        现场也可能要临时拉闸/加闸——一旦有人给这个属性赋过值，provider 就再也不覆盖它。
        其余情况（没赋过值）每次读都现取 provider；provider 抛任何异常、返回 None（那是
        "没说"，不是"空名单"）或返回取不出集合的东西，都退回当前已知集合：闸门不许因为
        插件层抖动而抛，也不许因此把已知的 mod 工具放过去。没传 provider 时直接返回已知
        集合本身——不重算、不改写，与加 provider 之前逐字节等价。
        """
        if self._mod_ids_manual or self.mod_ids_provider is None:
            return self._mod_ids
        try:
            fresh = self.mod_ids_provider()
        except Exception:
            return self._mod_ids
        if fresh is None:
            return self._mod_ids
        try:
            return _mod_id_set(fresh)
        except Exception:
            return self._mod_ids

    @mod_ids.setter
    def mod_ids(self, value):
        self._mod_ids = _mod_id_set(value)
        self._mod_ids_manual = True

    def is_mod_tool(self, tool_name):
        """工具名以 `<mod id>__` 开头就判为 mod 工具。

        不能只切第一个 __ 取首段：mod id 允许含 __（src/plugins/manager.py 只校验
        "非空 + 等于目录名"），id 为 inv__nested 的 mod 暴露的读工具叫
        inv__nested__peek，首段只有 inv —— 按首段比会漏判，读工具就绕过确认闸门。
        所以对 mod_ids 全量做整名前缀匹配；MOD_NAMESPACE 那条保留给"插件 id 字面
        就叫 mod"的兼容分支。modular__x / workspace__read / x__<id>__peek / 无 __ /
        空串都不匹配。
        """
        name = str(tool_name or '')
        if not name:
            return False
        if name.startswith(MOD_NAMESPACE + '__'):
            return True
        return any(name.startswith(mod_id + '__') for mod_id in self.mod_ids)

    def is_workspace_tool(self, tool_name):
        """这个工具名是不是 workspace 注册出来的真工作区工具。

        名单里有它就直通。名单为空（手工 new 出来的 gate，没有注册表可查）时退回名字前缀，
        但 mod 判据优先：那时 mod id 恰好叫 workspace 的话，前缀判据分不出真假，只能从紧
        按 mod 规则拦——宁可让真工作区工具多要一次确认，也不许 mod 工具静默放行。
        """
        name = str(tool_name or '')
        if not name:
            return False
        if self.workspace_tools:
            return name in self.workspace_tools
        return name.split('__')[0] == WORKSPACE_SOURCE and not self.is_mod_tool(name)

    def machine_hit(self, tool_name):
        segment = str(tool_name or '').split('__')[-1].strip().lower()
        return next((part for part in self.machine_parts if part in segment), '')

    async def run_on_before_tool(self, tool_name, arguments, runtime=None):
        name = str(tool_name or '')
        args = arguments if isinstance(arguments, dict) else {}
        # 判定顺序：先算 mod 判据，再问"是不是注册出来的真工作区工具"——谁能直通只由
        # is_workspace_tool() 这一份判据决定（名单命中就直通；名单为空才退回名字前缀，且 mod
        # 判据优先，是从紧取舍）。这里没有任何按名字前缀的早退：名字像 workspace、却没进过
        # workspace_tools 的工具（例如 id 恰好叫 workspace 的非 mod 桥插件注册出来的
        # workspace__delete_everything）照常往下走，危险词那层 machine_hit 对它一样生效。
        mod_tool = self.is_mod_tool(name)
        if self.is_workspace_tool(name):
            return None
        if mod_tool and not has_confirm(args):
            self.blocked.append(name)
            return {'block': True, 'reason': MOD_CONFIRM_REASON % name,
                    'extra_system': MOD_CONFIRM_HINT}
        hit = self.machine_hit(name)
        if not hit:
            if mod_tool:
                self.confirmed.append(name)
            return None
        self.blocked.append(name)
        return {'block': True,
                'reason': '工具 %s 会改本机状态（%s），控制台要先走一次危险动作确认；'
                          '请改用 actions 里的对应动作，由用户点头后再执行' % (name, hit)}


class ConsoleRuntime:
    """tool_loop / skill_tools 只按鸭子类型取属性，这里给它们一个最小的落脚点。"""

    def __init__(self, root, config, registry, data_dir=None, gate=None):
        self.root = Path(root)
        self.data_root = None      # 让 tool_loop 的审计落在 <root>/data/qq-only/
        self.data_dir = Path(data_dir) if data_dir else self.root / 'data'
        self.config = config
        self.tool_registry = registry
        self.plugins = gate if gate is not None else DangerGate()
        self.notes = []
        self.counts = {}

    def note(self, message):
        self.notes.append(str(message))


def _workspace_handler(data_dir, agent_id, op):
    def handler(name, arguments):
        args = arguments if isinstance(arguments, dict) else {}
        relpath = str(args.get('relpath') or args.get('path') or '').strip()
        if op == 'list':
            return agent_workspace.list_dir(data_dir, agent_id, relpath)
        if not relpath:
            return {'ok': False, 'code': 'invalid', 'message': '缺少 relpath（工作区内的相对路径）'}
        if op == 'read':
            return agent_workspace.read_text(data_dir, agent_id, relpath)
        text = args.get('text')
        if not isinstance(text, str):
            return {'ok': False, 'code': 'invalid', 'message': 'text 必须是字符串'}
        if op == 'write':
            return agent_workspace.write_text(data_dir, agent_id, relpath, text,
                                             principal=CONSOLE_SOURCE)
        return agent_workspace.append_text(data_dir, agent_id, relpath, text,
                                          principal=CONSOLE_SOURCE)
    return handler


def register_workspace(registry, data_dir, agent_id=AGENT_ID):
    """把四个工作区工具注册进工具表，返回注册条数。"""
    count = 0
    for spec in WORKSPACE_TOOLS:
        handler = _workspace_handler(data_dir, agent_id, spec['name'])
        count += 1 if registry.register(WORKSPACE_SOURCE, spec['name'], handler,
                                        description=spec['description'],
                                        parameters=spec['parameters']) else 0
    return count


def _bot_root(bot):
    root = getattr(bot, 'root', None)
    if root:
        return Path(root)
    config = getattr(bot, 'config', None)
    path = getattr(config, 'config_path', None)
    return Path(path).parent if path else Path('.')


def workspace_data_dir(bot):
    """工作区数据根：与 log_api.data_dir_of 同一个口径（bot.data_root 优先），
    取不到才退回 <root>/data。函数内 import，避免 agent_tools 与 log_api 互相拖累。"""
    try:
        from .log_api import data_dir_of
        resolved = data_dir_of(bot)
    except Exception:                      # log_api 出问题不该拖垮工具表，退回老口径
        resolved = ''
    return Path(resolved) if resolved else _bot_root(bot) / 'data'


def mod_ids_of(plugins):
    """mod 工具的真实判据：PluginManager._mod_plugins 的键（插件 id）。

    bot.plugins 可能是 None，也可能是 __new__ 造出来、根本没有 _mod_plugins 的 manager，
    所以 getattr 链带默认值且整体不抛——判据取不到就只是没有 mod 工具被认出来。
    """
    try:
        table = getattr(plugins, '_mod_plugins', None)
        return {str(key) for key in table} if isinstance(table, dict) else set()
    except Exception:
        return set()


def mod_revision_of(plugins):
    """插件层 mod 集合的只读修订号；取不到就是 0（没有插件层 == 没有 mod 会变）。

    这个数只用来回答一个问题：'按 _plugins 建的工具表还配得上现在的插件集合吗'。
    PluginManager.mod_revision 在挂载/摘除/重挂/reload 后 +1（src/plugins/manager.py 的
    mod_revision 属性），所以两边一致就等于"集合没变"。

    bot.plugins 可能没有、是 None、或者是 __new__ 造出来没有 _mod_revision 的 manager，
    所以整条链都带默认值且不抛：读不到就当作 0，绝不因为读版本失败去重建工具表。
    """
    try:
        return max(0, int(getattr(plugins, 'mod_revision', 0) or 0))
    except Exception:                      # 插件层坏了不该拖垮控制台，退回"没有变化"
        return 0


def workspace_tool_names(registry):
    """工作区工具的真名单：按注册来源（WORKSPACE_SOURCE）从注册表里取，交给闸门做区分。"""
    names = set()
    try:
        for name in registry.names():
            spec = registry.get(name)
            if str(getattr(spec, 'source', '')) == WORKSPACE_SOURCE:
                names.add(str(name))
    except Exception:                      # 注册表形状变了也不该拖垮工具表
        return set()
    return names


def build_runtime(bot, tool_settings=None):
    """建一次工具表：workspace + 扩展工具（桥）+ skill 工具；任何一路失败都只记不抛。"""
    tool_settings = tool_settings or settings(getattr(bot, 'config', None))
    root = _bot_root(bot)
    data_dir = workspace_data_dir(bot)
    registry = tool_loop.ToolRegistry()
    # mod 名单按需取最新：目录 mod 增量热重载挂上来的新 mod 立刻进闸门，而不是停留在
    # 这一次 build_runtime 的快照（写工具还有 mod 层的 confirm-required 兜住，读工具
    # 只有这道闸门，快照就等于给热挂上的 mod 留了一个后门）。
    gate = DangerGate(mod_ids_provider=lambda: mod_ids_of(getattr(bot, 'plugins', None)))
    runtime = ConsoleRuntime(root, getattr(bot, 'config', None), registry,
                             data_dir=data_dir, gate=gate)
    runtime.counts['workspace'] = register_workspace(registry, data_dir, AGENT_ID)
    # 注册完工作区工具才知道真名单：先建 gate 再注册，所以名单在这里回填。
    gate.workspace_tools = workspace_tool_names(registry)
    try:
        runtime.counts['plugins'] = tool_loop.register_plugin_tools(
            registry, getattr(bot, 'plugins', None))
    except Exception as error:
        runtime.counts['plugins'] = 0
        runtime.note('扩展工具注册失败：%s: %s' % (type(error).__name__, error))
    try:
        from . import skill_tools
        outcome = skill_tools.register_skills(runtime, limit=SKILL_LIMIT)
        runtime.counts['skills'] = int((outcome or {}).get('counts', {}).get('registeredTools') or 0)
    except Exception as error:
        runtime.counts['skills'] = 0
        runtime.note('skill 工具注册失败：%s: %s' % (type(error).__name__, error))
    return runtime


def _prompt_with_hint(system_prompt, hint, extra=''):
    parts = [str(system_prompt or '').strip(), str(hint or '').strip(), str(extra or '').strip()]
    return chr(10).join(part for part in parts if part)


def _assistant_message(reply, calls):
    """复用 tool_loop 自己拼的 OpenAI 回灌形状，避免两份不一致。"""
    builder = getattr(tool_loop, '_assistant_message', None)
    if callable(builder):
        return builder(reply, calls)
    payload = [{'id': call.id, 'type': 'function',
                'function': {'name': call.name,
                             'arguments': call.raw_arguments or json.dumps(call.arguments,
                                                                          ensure_ascii=False)}}
               for call in calls]
    return {'role': 'assistant', 'content': str((reply or {}).get('text') or ''),
            'tool_calls': payload}


class ConsoleTools:
    """控制台这一侧的工具环入口：一次提问最多 max_rounds 轮工具，最后仍回 JSON actions。"""

    def __init__(self, runtime, tool_settings=None, mod_revision=0):
        self.runtime = runtime
        self.settings = tool_settings or settings(getattr(runtime, 'config', None))
        # 建表时的插件集合修订号：表里的 spec 绑的就是这一版插件实例。
        self.mod_revision = int(mod_revision or 0)

    def is_stale(self, revision=None):
        """这张表还配得上 revision 那一版插件集合吗？取不到当前版本（None）就说不清，不算过期。

        过期只意味着"该重建"，不意味着这张表立刻不能用了：重建失败时旧表继续服务，
        见 src/console_agent.py 的 _tool_turn。
        """
        if revision is None:
            return False
        try:
            return int(revision) != self.mod_revision
        except (TypeError, ValueError):
            return False

    def mark_attempted(self, revision=None):
        """承认已经按 revision 这一版试过重建（多半是失败了），下一轮别再试同一个数。

        这样一次坏挂载只花一次重建，而不是每轮都花一次；等到插件集合再变（修订号再涨），
        比较自然又会不等，于是再试一次——重试的机会来自新的变化，而不是靠每轮撞。
        """
        if revision is None:
            return
        try:
            self.mod_revision = int(revision)
        except (TypeError, ValueError):
            pass

    def sources(self):
        """注册进表里的来源与工具数，报告/自检用。"""
        table = {}
        for item in self.runtime.tool_registry.describe():
            if not item.get('enabled'):
                continue
            table[item['source']] = table.get(item['source'], 0) + 1
        return table

    def _audit_turn(self, started, rounds, calls, text, reason):
        try:
            tool_loop.audit_write(self.runtime, {
                'ts': round(time.time(), 3), 'phase': 'turn', 'tool': 'console',
                'arguments': {'agent': AGENT_ID, 'source': CONSOLE_SOURCE},
                'result': str(text or '')[:600], 'source': CONSOLE_SOURCE,
                'rounds': rounds, 'calls': calls, 'reason': reason,
                'elapsed_ms': round((time.monotonic() - started) * 1000, 1)})
        except Exception:
            pass

    async def ask(self, engine, messages, system_prompt='', temperature=.4, max_tokens=3000,
                  timeout=CHAT_TIMEOUT, retry_on_timeout=False):
        """带工具的模型调用；返回最终文本，返回 None 表示调用方该回落到原来那次纯 chat。"""
        if not self.settings.enabled:
            return None
        registry = self.runtime.tool_registry
        tools = registry.specs()
        conversation = [dict(item) for item in (messages or []) if isinstance(item, dict)]
        if not tools or not conversation:
            return None
        loop_settings = self.settings.loop_settings()
        base_prompt = str(system_prompt or '')
        started = time.monotonic()
        deadline = started + WALL_BUDGET
        rounds = calls = 0
        last_text = extra_system = ''
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 2.0:
                self._audit_turn(started, rounds, calls, last_text, 'budget')
                return last_text or tool_loop.FALLBACK_FAILED
            final_round = rounds >= self.settings.max_rounds
            round_timeout = max(5.0, min(float(timeout or CHAT_TIMEOUT), remaining - 1.0))
            try:
                reply = await asyncio.wait_for(engine.chat(
                    conversation,
                    system_prompt=_prompt_with_hint(base_prompt, loop_settings.hint, extra_system),
                    temperature=temperature, max_tokens=max_tokens, timeout=round_timeout,
                    retry_on_timeout=retry_on_timeout,
                    tools=None if final_round else tools, with_message=True),
                    round_timeout + 3.0)
            except asyncio.TimeoutError:
                self._audit_turn(started, rounds, calls, last_text, 'model-timeout')
                if last_text:
                    return last_text
                raise
            except TypeError:
                # 引擎不支持 tools/with_message（老引擎或被替换的简化实现）→ 交回纯 chat
                return None
            if isinstance(reply, str):
                self._audit_turn(started, rounds, calls, reply, 'plain')
                return reply or last_text
            data = reply if isinstance(reply, dict) else {}
            text = str(data.get('text') or '').strip()
            if text:
                last_text = text
            batch = tool_loop.parse_tool_calls(data) if not final_round else []
            if not batch:
                self._audit_turn(started, rounds, calls, text, 'answer')
                return text or last_text or tool_loop.FALLBACK_EMPTY
            rounds += 1
            calls += len(batch)
            conversation.append(_assistant_message(data, batch))
            outcome = await tool_loop.run_calls(
                registry, self.runtime.plugins, self.runtime, batch, loop_settings,
                context={'source': CONSOLE_SOURCE, 'target': CONSOLE_SOURCE, 'sender': ''})
            for call_id, name, result_text in (outcome or {}).get('messages') or []:
                conversation.append({'role': 'tool', 'tool_call_id': call_id,
                                     'name': name, 'content': result_text})
            extra = str((outcome or {}).get('extra_system') or '')
            if extra:
                extra_system = (extra_system + chr(10) + extra) if extra_system else extra


def open_tools(bot, tool_settings=None):
    """开关打开时建一次工具环；关闭或建表失败都返回 None（控制台行为保持不变）。"""
    tool_settings = tool_settings or settings(getattr(bot, 'config', None))
    if not tool_settings.enabled:
        return None
    # 先取修订号再建表：这中间要是又挂上/摘掉了 mod，这张表就比戳旧，下一轮会自己重建。
    # 反过来（先建后取）会把新版本记到旧表上，那次变化就永远补不回来了。
    revision = mod_revision_of(getattr(bot, 'plugins', None))
    runtime = build_runtime(bot, tool_settings)
    tools = ConsoleTools(runtime, tool_settings, mod_revision=revision)
    if not tools.runtime.tool_registry.specs():
        return None
    return tools
