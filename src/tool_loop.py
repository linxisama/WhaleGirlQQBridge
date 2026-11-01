'''工具调用环（tool loop）：让模型在回复前真正调用工具。

主链路（qq_app._work -> plugins.generate_reply -> ai_engine.chat）以前是纯文本一问一答：
插件虽然声明了 on_before_tool / on_after_tool，但 src 里没有任何地方执行工具，于是 182 个
带工具的扩展和 1149 个 skill 工具一条也用不上。

本模块做三件事：
1. ToolRegistry：一张工具表，桥暴露的扩展工具、静态清单、后续的 skill 工具都通过
   register_tools(source, tools) 进同一张表；
2. OpenAI 兼容的 tools / tool_calls 往返（deepseek、moonshot、openai 字段一致）：请求体带
   tools，回复里解析 tool_calls，把结果以 role=tool 回灌，再取最终文本；不支持 tools 的
   后端（400/422 提到 tool/function）自动降级成纯文本回复，本会话不再重试；
3. 每轮工具调用前后派发 PluginManager.run_on_before_tool（manager.py:721）与
   run_on_after_tool（manager.py:748），支持被插件拒绝（block）或改写参数、改写结果。

安全边界（默认值见 LoopSettings）：默认关闭；默认只有主人能触发；最多 3 轮；单个工具 15 秒
超时；工具抛错/超时只把错误文本回灌给模型，绝不断掉回复；每次调用写一条 jsonl 审计。
'''
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# 默认值集中在这里：config_manager.DEFAULT_CONFIG 不在本工作包的写入范围，所以新配置项
# 全部靠"读不到就用这里的默认值"，配置文件缺项不影响启动。
DEFAULT_MAX_ROUNDS = 3
DEFAULT_TOOL_TIMEOUT = 15.0
DEFAULT_ROUND_TIMEOUT = 20.0
DEFAULT_TURN_BUDGET_FACTOR = 2.0
MAX_CALLS_PER_ROUND = 4
MAX_RESULT_CHARS = 4000
AUDIT_MAX_BYTES = 2 * 1024 * 1024
AUDIT_FILE = 'tool-loop-audit.jsonl'
MANIFEST_FILE = 'tool_loop_tools.json'

# 工具名要满足 OpenAI 的约束（字母数字下划线横线，最长 64），其余字符一律换掉。
_NAME_BAD = re.compile('[^0-9A-Za-z_-]+')
# 审计里必须脱敏的字段名。
_SENSITIVE = re.compile('(?i)(api[-_]?key|secret|token|password|passwd|authorization|cookie|credential)')

TOOL_HINT = '需要外部信息或动作时可以调用提供的工具；用不到就直接回答，不要为了调用而调用。'

FALLBACK_FAILED = '这个我暂时查不到（工具没调通），先说我知道的部分。'
FALLBACK_EMPTY = '（这次没取到内容，再说一次试试？）'


# ── 配置 ─────────────────────────────────────────────────────────────────────

def _cfg_get(config, key, default=None):
    '''读 ConfigManager 或普通 dict 里的配置项（支持点号路径）。'''
    if config is None:
        return default
    getter = getattr(config, 'get', None)
    if callable(getter):
        try:
            value = getter(key, None)
        except TypeError:
            value = None
        if value is not None:
            return value
    if isinstance(config, dict):
        node = config
        for part in key.split('.'):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        if node is not None:
            return node
    return default


def _flag(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on', '是', '开')
    return bool(value)


def _number(value, default, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(low, min(high, number))


@dataclass
class LoopSettings:
    '''工具环的运行时开关；所有值都有默认值，配置缺项不影响启动。'''

    enabled: bool = False
    owner_only: bool = True
    max_rounds: int = DEFAULT_MAX_ROUNDS
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT
    round_timeout: float = DEFAULT_ROUND_TIMEOUT
    turn_budget: float = 0.0
    budget_factor: float = DEFAULT_TURN_BUDGET_FACTOR
    hint: str = TOOL_HINT
    manifest_path: str = ''

    @property
    def total_budget(self):
        '''整轮工具环的总等待预算（秒）。'''
        if self.turn_budget > 0:
            return self.turn_budget
        return max(self.round_timeout, self.round_timeout * self.budget_factor)


def _first(config, keys, default):
    for key in keys:
        value = _cfg_get(config, key, None)
        if value is not None:
            return value
    return default


def loop_settings(config):
    '''从配置读开关，默认关闭。

    真正的开关名是 ``ai.tool_loop``：QQRuntime 只保留 ai/qq/style/qq_only 四段配置
    （src/qq_app.py:339 的白名单），顶格写的 ``toolLoop`` 会在加载时被丢掉，所以真实落盘
    位置必须在 ai 段里。``toolLoop`` / ``tool_loop`` 保留为兼容别名，给直接传 dict 的场景
    （测试、插件自带配置）用。''',
    return LoopSettings(
        enabled=_flag(_first(config, ('ai.tool_loop', 'toolLoop', 'tool_loop'), False)),
        owner_only=_flag(_first(config, ('ai.tool_loop_owner_only', 'toolLoopOwnerOnly'), True), True),
        max_rounds=int(_number(_first(config, ('ai.tool_loop_max_rounds', 'toolLoopMaxRounds'), 3), 3, 1, 5)),
        tool_timeout=_number(_first(config, ('ai.tool_loop_tool_timeout', 'toolLoopToolTimeout'),
                                    DEFAULT_TOOL_TIMEOUT), DEFAULT_TOOL_TIMEOUT, 1.0, 60.0),
        round_timeout=_number(_cfg_get(config, 'ai.reply_timeout', DEFAULT_ROUND_TIMEOUT),
                              DEFAULT_ROUND_TIMEOUT, 1.0, 120.0),
        turn_budget=_number(_first(config, ('ai.tool_loop_turn_budget', 'toolLoopTurnBudget'), 0.0),
                            0.0, 0.0, 180.0),
        manifest_path=str(_first(config, ('ai.tool_loop_manifest', 'toolLoopManifest'), '') or ''),
    )


# ── 工具表 ───────────────────────────────────────────────────────────────────

@dataclass
class ToolSpec:
    '''一个可调用工具。name 是给模型看的名字，raw_name 是来源里的原名。'''

    name: str
    raw_name: str
    source: str = 'builtin'
    description: str = ''
    parameters: dict = field(default_factory=lambda: {'type': 'object', 'properties': {}})
    handler: object = None
    enabled: bool = True

    def to_openai(self):
        return {'type': 'function',
                'function': {'name': self.name,
                             'description': self.description or self.name,
                             'parameters': self.parameters}}

    @property
    def available(self):
        return bool(self.enabled and self.handler is not None)


def _clean_name(value):
    name = _NAME_BAD.sub('_', str(value or '').strip()).strip('_')
    return name[:64]


def _normalise_parameters(parameters):
    if not isinstance(parameters, dict) or not parameters:
        return {'type': 'object', 'properties': {}}
    if parameters.get('type') == 'object':
        return parameters
    if 'properties' in parameters:
        return dict(parameters, type='object')
    return {'type': 'object', 'properties': dict(parameters)}


def _spec_from_raw(source, raw, handler):
    '''把各种形状的工具描述统一成 ToolSpec（OpenAI function / 扁平 dict / 对象）。'''
    if isinstance(raw, str):
        raw = {'name': raw}
    if isinstance(raw, dict):
        function = raw.get('function') if isinstance(raw.get('function'), dict) else raw
        name = function.get('name') or raw.get('name')
        description = function.get('description') or raw.get('description') or ''
        parameters = (function.get('parameters') or function.get('input_schema')
                      or raw.get('parameters') or raw.get('input_schema'))
    else:
        name = getattr(raw, 'name', '')
        description = getattr(raw, 'description', '') or ''
        parameters = getattr(raw, 'parameters', None) or getattr(raw, 'input_schema', None)
    raw_name = str(name or '').strip()
    if not raw_name:
        return None
    clean = _clean_name(raw_name)
    if not clean:
        return None
    return ToolSpec(name=clean, raw_name=raw_name, source=str(source or 'builtin'),
                    description=str(description or ''), parameters=_normalise_parameters(parameters),
                    handler=handler)


class ToolRegistry:
    '''一张工具表，多个来源共用（桥暴露的扩展工具、静态清单、skill 工具）。'''

    def __init__(self):
        self._tools = {}
        self.declared = {}
        self.hidden = set()

    # ── 注册 ────────────────────────────────────────────────────────────
    def register_tools(self, source, tools, handler=None):
        '''公共入口：把一批工具注册进同一张表，返回注册成功的条数。

        后续工作包（skill 工具）只要调这一个入口，不必知道表的内部结构。
        handler 的调用约定统一为 handler(name, arguments) -> 任意可 JSON 化结果。
        '''
        count = 0
        for raw in tools or []:
            spec = _spec_from_raw(source, raw, handler)
            if spec is None:
                continue
            self._add(spec)
            count += 1
        return count

    def register(self, source, name, handler, description='', parameters=None):
        '''注册单个工具，返回落表后的 ToolSpec（或 None）。'''
        spec = _spec_from_raw(source, {'name': name, 'description': description,
                                       'parameters': parameters}, handler)
        if spec is None:
            return None
        self._add(spec)
        return self._tools.get(spec.name)

    def _add(self, spec):
        base = spec.name if spec.source in ('', 'builtin') else _clean_name(spec.source + '__' + spec.raw_name)
        base = base or _clean_name(spec.raw_name)
        name = base
        index = 2
        while name in self._tools:
            name = _clean_name(base + '_' + str(index))
            index += 1
        spec.name = name
        spec.enabled = base not in self.hidden
        self._tools[name] = spec

    def unregister_source(self, source):
        for name in [key for key, spec in self._tools.items() if spec.source == source]:
            self._tools.pop(name, None)

    # ── 查询 ────────────────────────────────────────────────────────────
    def get(self, name):
        spec = self._tools.get(str(name or ''))
        return spec if spec is not None and spec.available else None

    def specs(self):
        '''给模型的 tools 列表（只含真的能执行的工具）。'''
        return [spec.to_openai() for spec in self._tools.values() if spec.available]

    def names(self):
        return sorted(spec.name for spec in self._tools.values() if spec.available)

    def describe(self):
        return [{'name': spec.name, 'raw_name': spec.raw_name, 'source': spec.source,
                 'enabled': spec.available} for spec in self._tools.values()]

    def __len__(self):
        return len(self.specs())

    # ── 静态清单 ────────────────────────────────────────────────────────
    def apply_manifest(self, entries):
        '''应用静态清单：启用/禁用已注册工具，或覆盖描述与参数。

        清单只描述"哪些工具允许下发给模型"，不含可执行代码——处理函数必须来自代码侧
        （桥暴露的扩展工具、后续工作包注册的 skill 工具），清单文件坏了也不会变成任意
        代码执行。清单里声明但尚无实现的工具记在 declared 里，不下发给模型。
        '''
        applied = 0
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get('name') or '').strip()
            source = str(entry.get('source') or '').strip()
            if not name:
                continue
            spec = self._tools.get(_clean_name(name))
            if spec is None and source:
                spec = self._tools.get(_clean_name(source + '__' + name))
            if spec is None:
                self.declared[name] = dict(entry)
                if entry.get('enabled') is not None and not _flag(entry.get('enabled'), True):
                    self.hidden.add(_clean_name(name))
                continue
            if entry.get('enabled') is not None:
                spec.enabled = _flag(entry.get('enabled'), True)
            if entry.get('description'):
                spec.description = str(entry['description'])
            if isinstance(entry.get('parameters'), dict) and entry['parameters']:
                spec.parameters = _normalise_parameters(entry['parameters'])
            applied += 1
        return applied


# ── 静态清单文件 ─────────────────────────────────────────────────────────────

def _runtime_root(runtime):
    for attr in ('data_root', 'root'):
        value = getattr(runtime, attr, None)
        if value:
            return Path(value)
    return Path('.')


def manifest_path(runtime, settings=None):
    settings = settings or LoopSettings()
    if settings.manifest_path:
        return Path(settings.manifest_path)
    return _runtime_root(runtime) / 'data' / 'qq-only' / MANIFEST_FILE


def load_manifest(path):
    '''读清单文件；文件缺失或内容损坏都只是"没有清单"，不抛错。'''
    try:
        raw = Path(path).read_text(encoding='utf-8')
    except (OSError, ValueError):
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get('tools') or []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


# ── 来源收集 ─────────────────────────────────────────────────────────────────

def _plugin_handler(caller):
    def handler(name, arguments):
        return caller(name, arguments)
    return handler


def register_plugin_tools(registry, plugins):
    '''把插件暴露的工具收进表里（桥的 list_tools() / call_tool(name, args)）。

    桥由另一个工作包实现，这里只按接口调用：插件没有这两个方法就直接跳过；桥还没落地时
    工具表为空，工具环自己会降级回纯文本，不影响现有回复。
    '''
    registered = 0
    table = getattr(plugins, '_plugins', None)
    if not isinstance(table, dict):
        return 0
    for source, plugin in table.items():
        lister = getattr(plugin, 'list_tools', None)
        caller = getattr(plugin, 'call_tool', None)
        if not callable(lister) or not callable(caller):
            continue
        try:
            tools = lister()
        except Exception:
            continue
        if not tools:
            continue
        try:
            registered += registry.register_tools(str(source), tools, handler=_plugin_handler(caller))
        except Exception:
            continue
    return registered


def registry_for(runtime):
    '''拿到（并按需建立）这个 runtime 的工具表；测试可以预置 runtime.tool_registry。'''
    registry = getattr(runtime, 'tool_registry', None)
    if isinstance(registry, ToolRegistry):
        return registry
    registry = ToolRegistry()
    settings = loop_settings(getattr(runtime, 'config', None))
    try:
        registry.apply_manifest(load_manifest(manifest_path(runtime, settings)))
    except Exception:
        pass
    try:
        register_plugin_tools(registry, getattr(runtime, 'plugins', None))
    except Exception:
        pass
    try:
        runtime.tool_registry = registry
    except Exception:
        pass
    return registry


def register_tools(runtime, source, tools, handler=None):
    '''模块级公共入口：往这个 runtime 的工具表里注册一批工具（给后续工作包用）。'''
    return registry_for(runtime).register_tools(source, tools, handler=handler)

# ── tool_calls 解析 ──────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    '''模型要求的一次工具调用。'''

    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    raw_arguments: str = ''
    arguments_error: str = ''


def _parse_arguments(raw):
    if raw is None or raw == '':
        return {}, ''
    if isinstance(raw, dict):
        try:
            return dict(raw), json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            return {}, ''
    text = str(raw)
    if not text.strip():
        return {}, text
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}, text
    if isinstance(value, dict):
        return value, text
    return {'value': value}, text


def _call_from(name, raw_arguments, call_id='', index=0):
    args, raw = _parse_arguments(raw_arguments)
    error = ''
    if raw and not args and raw.strip() not in ('{}', '[]', 'null'):
        error = 'arguments 不是合法 JSON 对象'
    return ToolCall(id=str(call_id or '').strip() or 'call_' + str(index),
                    name=str(name or '').strip(), arguments=args,
                    raw_arguments=raw, arguments_error=error)


def _blocks_of(reply):
    if not isinstance(reply, dict):
        return []
    blocks = reply.get('content_blocks')
    return blocks if isinstance(blocks, list) else []


def parse_tool_calls(reply):
    '''从模型回复里抽出工具调用。

    兼容真实存在的 4 种形状：OpenAI / DeepSeek / Moonshot 的 message.tool_calls（arguments
    是 JSON 字符串或已是 dict）、legacy function_call、Anthropic content 里的 tool_use 块、
    少数网关直出的扁平 {name, arguments}。arguments 解析失败不丢调用，挂 arguments_error。
    '''
    if reply is None:
        return []
    message = reply.get('message') if isinstance(reply, dict) else None
    if not isinstance(message, dict):
        message = reply if isinstance(reply, dict) else {}
    calls = []
    raw_calls = message.get('tool_calls')
    if isinstance(raw_calls, list):
        for index, item in enumerate(raw_calls):
            if not isinstance(item, dict):
                continue
            function = item.get('function') if isinstance(item.get('function'), dict) else item
            name = function.get('name') or item.get('name')
            if not name:
                continue
            calls.append(_call_from(name, function.get('arguments', item.get('arguments')),
                                    item.get('id') or item.get('tool_use_id') or '', index))
    if calls:
        return calls
    legacy = message.get('function_call')
    if isinstance(legacy, dict) and legacy.get('name'):
        calls.append(_call_from(legacy.get('name'), legacy.get('arguments'), legacy.get('id') or '', 0))
        return calls
    single = message.get('tool_use')
    if isinstance(single, dict) and single.get('name'):
        calls.append(_call_from(single.get('name'), single.get('input', single.get('arguments')),
                                single.get('id') or '', 0))
        return calls
    if message.get('name') and not message.get('role'):
        calls.append(_call_from(message.get('name'), message.get('arguments', message.get('input')), '', 0))
        return calls
    for index, block in enumerate(_blocks_of(reply)):
        if not isinstance(block, dict):
            continue
        if block.get('type') not in ('tool_use', 'tool_call'):
            continue
        if not block.get('name'):
            continue
        calls.append(_call_from(block.get('name'), block.get('input', block.get('arguments')),
                                block.get('id') or '', index))
    return calls


# ── 审计 ─────────────────────────────────────────────────────────────────────

def _summarise(value, limit=600):
    '''把任意工具结果压成一段单行摘要（审计用）。'''
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit] + '...'


def _redact(value, depth=0):
    '''递归把敏感字段换成 ***，并把长字符串截断。'''
    if depth > 4:
        return '...'
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result[key] = '***' if _SENSITIVE.search(str(key)) else _redact(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:20]]
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:400] + '...'
    return value


def audit_path(runtime):
    return _runtime_root(runtime) / 'data' / 'qq-only' / AUDIT_FILE


def audit_write(runtime, record):
    '''写一条 jsonl 审计；写失败只是丢一条审计，绝不影响回复。'''
    path = audit_path(runtime)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > AUDIT_MAX_BYTES:
                path.replace(path.with_name(path.stem + '.previous.jsonl'))
        except OSError:
            pass
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + chr(10))
    except (OSError, ValueError, TypeError):
        return ''
    return str(path)


def _audit(runtime, phase, tool, arguments=None, result=None, error='', extra=None):
    record = {'ts': round(time.time(), 3), 'phase': phase, 'tool': tool,
              'arguments': _redact(arguments or {})}
    if result is not None:
        record['result'] = _summarise(result)
    if error:
        record['error'] = error
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key not in record:
                record[key] = value
    return audit_write(runtime, record)


# ── 工具执行 ─────────────────────────────────────────────────────────────────

async def _invoke(handler, name, arguments, timeout):
    '''执行一个工具：异步处理函数直接 await，同步的丢线程池；整体加超时。'''
    try:
        if inspect.iscoroutinefunction(handler):
            value = await asyncio.wait_for(handler(name, arguments), timeout=timeout)
        else:
            value = await asyncio.wait_for(asyncio.to_thread(handler, name, arguments), timeout=timeout)
        return True, value, ''
    except asyncio.TimeoutError:
        return False, None, '工具超时（' + str(timeout) + 's）'
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return False, None, type(error).__name__ + ': ' + str(error)


async def _before_tool(plugins, tool_name, arguments, runtime):
    '''派发 manager.run_on_before_tool：插件可以拒绝（block）或改写参数。'''
    outcome = {'allowed': True, 'arguments': arguments, 'reason': '', 'extra_system': ''}
    runner = getattr(plugins, 'run_on_before_tool', None)
    if not callable(runner):
        return outcome
    try:
        result = await runner(tool_name, arguments, runtime)
    except asyncio.CancelledError:
        raise
    except Exception:
        return outcome
    if not isinstance(result, dict):
        return outcome
    if result.get('block'):
        outcome['allowed'] = False
        outcome['reason'] = str(result.get('reason') or '插件拒绝了这次工具调用')
    if isinstance(result.get('arguments'), dict):
        outcome['arguments'] = result['arguments']
    if result.get('extra_system'):
        outcome['extra_system'] = str(result['extra_system'])
    return outcome


async def _after_tool(plugins, tool_name, arguments, value, runtime):
    '''派发 manager.run_on_after_tool：插件可以替换工具结果。'''
    runner = getattr(plugins, 'run_on_after_tool', None)
    if not callable(runner):
        return value
    try:
        result = await runner(tool_name, arguments, value, runtime)
    except asyncio.CancelledError:
        raise
    except Exception:
        return value
    return value if result is None else result


def _result_text(value):
    if value is None:
        return 'ok'
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    else:
        text = str(value)
    text = text or 'ok'
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + '...（已截断）'


async def run_calls(registry, plugins, runtime, calls, settings=None, context=None):
    '''执行一轮工具调用，返回 {'messages': [(call_id, name, text)], 'extra_system': str}。'''
    settings = settings or LoopSettings()
    context = context or {}
    messages = []
    extra_system = ''
    for call in list(calls)[:MAX_CALLS_PER_ROUND]:
        audit_extra = {'source': context.get('source', ''), 'target': context.get('target', ''),
                       'sender': context.get('sender', '')}
        spec = registry.get(call.name)
        if spec is None:
            text = '没有这个工具：' + call.name + '。可用工具：' + '、'.join(registry.names()[:15])
            _audit(runtime, 'tool', call.name, call.arguments, result=text, error='unknown tool', extra=audit_extra)
            messages.append((call.id, call.name, text))
            continue
        if call.arguments_error:
            text = '参数解析失败：' + call.arguments_error + '。请用合法 JSON 重发。'
            _audit(runtime, 'tool', call.name, call.arguments, result=text,
                   error=call.arguments_error, extra=audit_extra)
            messages.append((call.id, call.name, text))
            continue
        arguments = dict(call.arguments or {})
        gate = await _before_tool(plugins, call.name, arguments, runtime)
        if gate.get('extra_system'):
            extra_system = extra_system + chr(10) + gate['extra_system'] if extra_system else gate['extra_system']
        if not gate['allowed']:
            text = '这次工具调用被安全策略拦下了：' + (gate['reason'] or '未说明原因')
            _audit(runtime, 'blocked', call.name, arguments, result=text, error=gate['reason'], extra=audit_extra)
            messages.append((call.id, call.name, text))
            continue
        arguments = gate['arguments']
        started = time.monotonic()
        ok, value, error = await _invoke(spec.handler, spec.raw_name, arguments, settings.tool_timeout)
        elapsed = round((time.monotonic() - started) * 1000, 1)
        if ok:
            value = await _after_tool(plugins, call.name, arguments, value, runtime)
            text = _result_text(value)
            _audit(runtime, 'tool', call.name, arguments, result=text,
                   extra=dict(audit_extra, elapsed_ms=elapsed, source=spec.source))
            messages.append((call.id, call.name, text))
        else:
            text = '工具执行失败：' + error
            _audit(runtime, 'error', call.name, arguments, result='', error=error,
                   extra=dict(audit_extra, elapsed_ms=elapsed, source=spec.source))
            messages.append((call.id, call.name, text))
    return {'messages': messages, 'extra_system': extra_system}


# ── 主循环 ───────────────────────────────────────────────────────────────────

def _is_owner(runtime, turn):
    getter = getattr(runtime, 'owner_ids', None)
    if not callable(getter):
        return False
    try:
        owners = getter() or set()
    except Exception:
        return False
    sender = str((turn or {}).get('sender_id') or (turn or {}).get('userId') or '')
    if not sender:
        return False
    try:
        return sender in {str(item) for item in owners}
    except TypeError:
        return False


def should_use(runtime, turn, settings=None):
    '''这条消息该不该走工具环；任何一项不满足就回落到原有纯文本回复。'''
    try:
        settings = settings or loop_settings(getattr(runtime, 'config', None))
        if not settings.enabled:
            return False
        if getattr(runtime, 'tool_loop_unsupported', False):
            return False
        turn = turn or {}
        if turn.get('_direct_reply') is not None or turn.get('_robot_admin'):
            return False
        engine = getattr(runtime, 'ai', None)
        if engine is None or not bool(getattr(engine, 'configured', False)):
            return False
        # tools/tool_calls 是 OpenAI 形状；anthropic 风格要换一套回灌格式，本轮不接。
        if str(getattr(engine, 'api_style', 'openai') or 'openai') not in ('openai', 'auto'):
            return False
        registry = getattr(runtime, 'tool_registry', None)
        if not isinstance(registry, ToolRegistry):
            registry = registry_for(runtime)
        if not registry.specs():
            return False
        if settings.owner_only and not _is_owner(runtime, turn):
            return False
        return True
    except Exception:
        return False


def _system_prompt_with_hint(system_prompt, hint, extra=''):
    parts = [str(system_prompt or '').strip(), str(hint or '').strip(), str(extra or '').strip()]
    return chr(10).join(part for part in parts if part)


def _assistant_message(reply, calls):
    text = str((reply or {}).get('text') or '')
    payload = []
    for call in calls:
        raw = call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False)
        payload.append({'id': call.id, 'type': 'function',
                        'function': {'name': call.name, 'arguments': raw}})
    return {'role': 'assistant', 'content': text, 'tool_calls': payload}


def _tools_rejected(engine):
    '''后端明确不接受 tools（400/422 且提到 tool/function）时返回 True。'''
    text = str(getattr(engine, 'last_error', '') or '').lower()
    if not text:
        return False
    if not any(code in text for code in ('400', '422', 'invalid')):
        return False
    return 'tool' in text or 'function' in text


def _degrade(runtime, reason):
    try:
        note = getattr(runtime, 'note', None)
        if callable(note):
            note('工具环降级：' + reason)
    except Exception:
        pass
    return FALLBACK_FAILED


async def respond_with_tools(engine, runtime, conversation, system_prompt='', settings=None,
                             temperature=None, max_tokens=None, timeout=None,
                             retry_on_timeout=None, turn=None):
    '''带工具的回复主循环。

    返回最终文本；返回 None 表示"这一轮不该由工具环负责"（引擎不接受 tools 等），调用方
    应该回落到原来的 generate_reply 路径，因此开关关闭或异常时线上行为完全不变。
    '''
    settings = settings or loop_settings(getattr(runtime, 'config', None))
    registry = getattr(runtime, 'tool_registry', None)
    if not isinstance(registry, ToolRegistry):
        registry = registry_for(runtime)
    tools = registry.specs()
    if not tools:
        return None
    plugins = getattr(runtime, 'plugins', None)
    context = {'target': str((turn or {}).get('target') or ''),
               'sender': str((turn or {}).get('sender_id') or '')}
    messages = [dict(item) for item in (conversation or []) if isinstance(item, dict)]
    if not messages:
        return None
    body_transform = None
    transform = getattr(plugins, 'transform_request', None)
    if callable(transform):
        try:
            params = inspect.signature(engine.chat).parameters.values()
        except (TypeError, ValueError):
            params = ()
        if any(param.name == 'body_transform' or param.kind == param.VAR_KEYWORD
               for param in params):
            async def body_transform(payload):
                return await transform(payload, {
                    'messages': list(messages),
                    'tools': list(tools),
                    'senderId': context['sender'],
                    'model': str(getattr(engine, 'model', '') or ''),
                })
    budget = settings.total_budget
    if timeout:
        budget = min(budget, max(settings.round_timeout, float(timeout) * 2))
    deadline = time.monotonic() + max(1.0, budget)
    rounds_used = 0
    allow_tools = True
    extra_system = ''
    base_prompt = str(system_prompt or '')
    last_text = ''
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            return last_text or _degrade(runtime, '工具环总预算用尽')
        final_round = rounds_used >= settings.max_rounds or not allow_tools
        round_timeout = max(1.0, min(settings.round_timeout, remaining))
        try:
            reply = await asyncio.wait_for(
                engine.chat(messages,
                            system_prompt=_system_prompt_with_hint(base_prompt, settings.hint, extra_system),
                            temperature=temperature, max_tokens=max_tokens, timeout=round_timeout,
                            retry_on_timeout=retry_on_timeout,
                            tools=None if final_round else tools, with_message=True,
                            body_transform=body_transform),
                round_timeout + 3.0)
        except asyncio.TimeoutError:
            engine.last_error = '工具环等待模型超时'
            return last_text or _degrade(runtime, '等待模型超时')
        except TypeError:
            # 引擎还不支持 tools / with_message（旧引擎或被替换的引擎）→ 纯文本
            return None
        if isinstance(reply, str):
            return reply or last_text or FALLBACK_EMPTY
        data = reply if isinstance(reply, dict) else {}
        text = str(data.get('text') or '').strip()
        if text:
            last_text = text
        calls = parse_tool_calls(data) if not final_round else []
        if not calls:
            if not text and not final_round and allow_tools and _tools_rejected(engine):
                allow_tools = False
                try:
                    runtime.tool_loop_unsupported = True
                except Exception:
                    pass
                continue
            return text or last_text or FALLBACK_EMPTY
        rounds_used += 1
        messages.append(_assistant_message(data, calls))
        outcome = await run_calls(registry, plugins, runtime, calls, settings, context=context)
        for call_id, name, result_text in outcome['messages']:
            messages.append({'role': 'tool', 'tool_call_id': call_id, 'name': name,
                             'content': result_text})
        if outcome.get('extra_system'):
            extra_system = (extra_system + chr(10) + outcome['extra_system']) if extra_system else outcome['extra_system']
