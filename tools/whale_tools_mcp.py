#!/usr/bin/env python3
"""鲸鱼娘工具面 MCP 服务：把本机扩展工具与技能工具按标准 MCP 协议交给外部 agent。

对外只有三个工具（固定）：
- tools_list   列出可用工具（id = <extensionId>__<toolId>，与仓内 skillId__tool 口径一致）；
- tools_call   调用一个工具，路由到 src/skill_tools.py:1061 build_tool_handler；
- tools_status node / 宿主脚本 / 源码树解析结果、扩展与工具计数、data_dir。

只读工具直接放行；写工具必须在 tools_call 里显式带 confirm:true，且每次调用（无论成败）
都落一行审计 <data_dir>/qq-only/mcp_tool_calls.jsonl（8MB 轮转 .previous.jsonl）。

只走 stdio，手写 JSON-RPC 2.0：venv 里没有 mcp SDK，不引任何第三方依赖。
形状照 tools/whale_bot_mcp.py：force_utf8_stdio / respond / structured_result / handle / main。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import extension_host, qqagent_host, skill_tools

PROTOCOL_VERSION = '2025-06-18'
SERVER_NAME = 'whale_tools'
SERVER_VERSION = '1.0.0'

AUDIT_NAME = 'mcp_tool_calls.jsonl'
AUDIT_DIR_PARTS = ('qq-only',)
AUDIT_MAX_BYTES = 8 * 1024 * 1024
CONFIRM_HINT = '该工具会改动机器，需外部客户端确认后带 confirm:true'
DATA_DIR_ENV = 'WHALE_TOOLS_DATA_DIR'
# 登记桥 src/dsh_memory_bridge.py:84 给本 server 传的是 WHALE_QQ_HOME=<bot_home>
# （与 tools/whale_memory_mcp.py:40 同一个约定）；漏认它会静默写到默认 data/。
HOME_ENV = 'WHALE_QQ_HOME'
DEFAULT_TOOL_TIMEOUT = 60.0
LIST_LIMIT = 500
SKILL_ROOT_PARTS = ('qq-only', 'skill_extensions')

# ── 写操作判定（写死常量，报告逐条列） ───────────────────────────────────────
# 三处来源：
# 1. src/console_agent.py:42 DANGEROUS 逐字；
# 2. src/agent_workspace.py:65 WRITE_OPS 对应的 workspace 写；
# 3. tools/pc_control_mcp.py:36-80 里会动本机的 pc_* 工具（其余 pc_* 判只读）。
DANGEROUS = frozenset({'service', 'open_path', 'open_url', 'type_text', 'hotkey', 'screenshot'})
WORKSPACE_WRITE_OPS = frozenset({'write', 'append'})
PC_CONTROL_PREFIX = 'pc_'
PC_CONTROL_READONLY = frozenset({'pc_status', 'pc_active_window', 'pc_clipboard_read'})
WRITE_TOOLS = frozenset({
    'service', 'open_path', 'open_url', 'type_text', 'hotkey', 'screenshot',
    'pc_screenshot', 'pc_open_path', 'pc_open_url', 'pc_type_text', 'pc_hotkey',
    'pc_move', 'pc_click', 'pc_clipboard_write',
    'workspace__write', 'workspace__append',
})


def force_utf8_stdio() -> None:
    """MCP stdio 必须说 UTF-8。

    Windows 下 python 的管道默认走 ANSI 代码页（中文系统是 cp936），对端按 UTF-8
    解析 initialize / tools/list 就会失败——DSH 侧表现为 server unavailable。
    这里显式把两个方向都锁成 UTF-8，不依赖父进程是否设了 PYTHONUTF8。
    """
    for stream, errors in ((sys.stdin, 'replace'), (sys.stdout, 'strict')):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding='utf-8', errors=errors, newline='\n')
        except (ValueError, OSError):
            pass


def is_write(identifier) -> bool:
    """这个工具是不是写操作；<extId>__<toolId> 与裸 toolId 两种写法都认。"""
    text = str(identifier or '').strip()
    if not text:
        return False
    if text in WRITE_TOOLS:
        return True
    parts = [part for part in text.split('__') if part]
    head = parts[0] if parts else text
    tail = parts[-1] if parts else text
    if head.startswith(PC_CONTROL_PREFIX) or tail.startswith(PC_CONTROL_PREFIX):
        return tail not in PC_CONTROL_READONLY
    return tail in DANGEROUS or tail in WORKSPACE_WRITE_OPS


META_TOOLS = [
    {'name': 'tools_list',
     'description': ('列出本机可用的扩展工具与技能工具（id 形如 <extensionId>__<toolId>）。'
                     'write:true 的是会改动机器的工具，调用时要带 confirm:true。'),
     'inputSchema': {'type': 'object', 'properties': {
         'filter': {'type': 'string', 'description': '在 id / extension / description 里做子串匹配'},
         'write': {'type': 'boolean', 'description': 'true 只看写工具，false 只看只读工具'},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': LIST_LIMIT, 'default': 200},
         'refresh': {'type': 'boolean', 'default': False, 'description': 'true 时重新枚举工具面'}},
         'additionalProperties': False}},
    {'name': 'tools_call',
     'description': ('调用一个工具。只读工具直接执行；write:true 的工具必须带 confirm:true，'
                     '否则返回 confirm-required 且不执行。每次调用都会落审计。'),
     'inputSchema': {'type': 'object', 'properties': {
         'tool': {'type': 'string', 'description': 'tools_list 给出的 id'},
         'arguments': {'type': 'object', 'description': '工具自己的参数对象'},
         'confirm': {'type': 'boolean', 'description': '写工具必须为 true 才真的执行'}},
         'required': ['tool'], 'additionalProperties': False}},
    {'name': 'tools_status',
     'description': 'node / 宿主脚本 / 源码树解析结果、扩展与工具计数、审计落点与写工具清单。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
]


def respond(identifier, result=None, error=None) -> dict:
    """只构造 JSON-RPC 响应；写 stdout 交给 emit（测试要拿返回值）。"""
    message = {'jsonrpc': '2.0', 'id': identifier}
    if error is not None:
        message['error'] = error
    else:
        message['result'] = result if result is not None else {}
    return message


def emit(message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False)
    try:
        sys.stdout.write(payload + '\n')
    except UnicodeEncodeError:
        # 极端情况下 stdout 仍是 ANSI 代码页：退回纯 ASCII 也要让对端解析成功。
        sys.stdout.write(json.dumps(message, ensure_ascii=True) + '\n')
    sys.stdout.flush()


def structured_result(payload: dict, is_error: bool = False) -> dict:
    """结构化载荷按 MCP 结果返回：正文是 JSON，ok/error 由正文表达。"""
    return {'content': [{'type': 'text',
                         'text': json.dumps(payload, ensure_ascii=False, default=str)}],
            'isError': bool(is_error)}


# ── 工具面枚举 ───────────────────────────────────────────────────────────────
def _tool_entry(full: str, extension: str, description: str, kind: str, target: str) -> dict:
    return {'id': full, 'extension': extension, 'description': description,
            'write': is_write(full), 'enabled': True, 'kind': kind, 'target': target}


def _extension_tools(data_dir, errors: list) -> list:
    """宿主根 <data>/qq-only/qqagent_host/plugins/<id> 里已装扩展声明的工具。"""
    try:
        rows = qqagent_host.list_installed(qqagent_host.default_host_root(data_dir))
    except Exception as exc:
        errors.append('extension: %s: %s' % (type(exc).__name__, exc))
        return []
    entries = []
    for row in rows:
        ext_id = str(row.get('id') or '')
        folder = str(row.get('dir') or '')
        if not ext_id or not folder:
            continue
        prefix = '%s__' % ext_id
        for raw in row.get('tools') or []:
            tool_id = str(raw or '').strip()
            if not tool_id:
                continue
            full = tool_id if tool_id.startswith(prefix) else prefix + tool_id
            entries.append(_tool_entry(full, ext_id, '', 'extension', folder))
    return entries


def _skill_tools(data_dir, errors: list) -> list:
    """src/skill_tools.py:805 eligible_skills 的可注册技能工具（id 已是 skillId__tool）。"""
    try:
        plan = skill_tools.eligible_skills()
    except Exception as exc:
        errors.append('skill: %s: %s' % (type(exc).__name__, exc))
        return []
    installed_root = Path(data_dir).joinpath(*SKILL_ROOT_PARTS)
    entries = []
    for item in plan.get('eligible') or []:
        ident = str(item.get('id') or '')
        if not ident:
            continue
        folder = installed_root / ident
        for tool in item.get('tools') or []:
            full = str(tool.get('full') or '')
            if not full:
                continue
            entry = _tool_entry(full, str(tool.get('prefix') or ident),
                                str(tool.get('description') or ''), 'skill', str(folder))
            entry['enabled'] = folder.is_dir()
            entries.append(entry)
    return entries


def collect_tools(data_dir) -> dict:
    """扩展面 + 技能面合成一张表；两份来源各自失败只记 errors，不拖垮整份清单。"""
    errors: list = []
    tools, seen = [], set()
    for entry in _extension_tools(data_dir, errors) + _skill_tools(data_dir, errors):
        if entry['id'] in seen:
            continue
        seen.add(entry['id'])
        tools.append(entry)
    counts = {'tools': len(tools),
              'extensionTools': sum(1 for item in tools if item['kind'] == 'extension'),
              'skillTools': sum(1 for item in tools if item['kind'] == 'skill'),
              'writeTools': sum(1 for item in tools if item['write']),
              'extensions': len({item['extension'] for item in tools})}
    return {'tools': tools, 'errors': errors, 'counts': counts}


def catalog(state, refresh: bool = False) -> dict:
    if refresh or not isinstance(state.get('catalog'), dict):
        state['catalog'] = collect_tools(state['data_dir'])
    return state['catalog']


# ── 运行时路径（node / host-one.mjs / 0.4 源码树） ───────────────────────────
def runtime_paths(state) -> dict:
    """取值方式与 src/skill_tools.py:1161 register_skills 一致，不另造枚举。"""
    if isinstance(state.get('paths'), dict):
        return state['paths']
    override = state.get('overrides') or {}
    try:
        found = skill_tools.find_runtime_paths(override.get('host_script'),
                                               override.get('source_dir'),
                                               override.get('node'))
    except Exception:
        found = {}
    state['paths'] = {key: str((found or {}).get(key) or override.get(key) or '')
                      for key in ('node', 'host_script', 'source_dir')}
    return state['paths']


def _host_root(data_dir) -> str:
    try:
        return str(qqagent_host.default_host_root(data_dir))
    except Exception:
        return ''


def status_payload(state) -> dict:
    paths = runtime_paths(state)
    plan = catalog(state)

    def present(value, check) -> bool:
        return bool(value) and check(Path(value))

    return {'ok': True, 'server': SERVER_NAME, 'version': SERVER_VERSION,
            'protocolVersion': PROTOCOL_VERSION,
            'node': paths['node'], 'nodeFound': bool(paths['node']),
            'hostScript': paths['host_script'],
            'sourceDir': paths['source_dir'],
            'hostScriptFound': present(paths['host_script'], Path.is_file),
            'sourceDirFound': present(paths['source_dir'], Path.is_dir),
            'hostRoot': _host_root(state['data_dir']),
            'dataDir': str(state['data_dir']),
            'counts': plan['counts'],
            'audit': str(audit_path(state['data_dir'])),
            'writeTools': sorted(WRITE_TOOLS),
            'metaTools': [tool['name'] for tool in META_TOOLS],
            'errors': plan['errors']}


# ── 审计 ─────────────────────────────────────────────────────────────────────
def audit_path(data_dir) -> Path:
    return Path(data_dir).joinpath(*AUDIT_DIR_PARTS) / AUDIT_NAME


def record_call(data_dir, *, tool, write, confirm, ok, code, duration_ms, client) -> bool:
    """每次 tools_call 追加一行；写失败只是 audited=false，绝不让调用失败。"""
    path = audit_path(data_dir)
    entry = {'ts': round(time.time(), 3), 'tool': str(tool or ''), 'write': bool(write),
             'confirm': bool(confirm), 'ok': bool(ok), 'code': str(code or ''),
             'duration_ms': int(duration_ms), 'client': str(client or '')}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > AUDIT_MAX_BYTES:
                path.replace(path.with_name(path.stem + '.previous.jsonl'))
        except OSError:
            pass
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except (OSError, ValueError, TypeError):
        return False
    return True


# ── 调用路由 ─────────────────────────────────────────────────────────────────
def handler_for(state, entry: dict):
    """一个扩展/技能目录共用一个 handler：build_tool_handler 已把 extension 绑死。"""
    handlers = state.setdefault('handlers', {})
    key = (entry['extension'], entry['target'])
    if key in handlers:
        return handlers[key]
    mapping: dict = {}
    prefix = '%s__' % entry['extension']
    for other in catalog(state)['tools']:
        if other['extension'] != entry['extension']:
            continue
        mapping[other['id']] = other['id']
        mapping.setdefault(other['id'][len(prefix):], other['id'])
    paths = runtime_paths(state)
    handler = skill_tools.build_tool_handler(
        node=paths['node'], host_script=paths['host_script'], source_dir=paths['source_dir'],
        extension=entry['target'], extension_id=entry['extension'],
        mapping=mapping, timeout=DEFAULT_TOOL_TIMEOUT, data_dir=state['data_dir'])
    handlers[key] = handler
    return handler


async def exec_tool(state, entry: dict, arguments: dict) -> dict:
    """技能与扩展同一条 build_tool_handler 路径；异常一律收敛成 error.code。"""
    if not entry.get('enabled'):
        return {'ok': False, 'tool': entry['id'],
                'error': {'code': 'not-installed', 'message': '这个技能还没装到 %s' % entry['target']}}
    paths = runtime_paths(state)
    if not paths['node']:
        return {'ok': False, 'tool': entry['id'],
                'error': {'code': 'node-missing', 'message': '找不到 node 可执行文件，扩展工具跑不起来'}}
    try:
        return await handler_for(state, entry)(entry['id'], arguments)
    except Exception as exc:
        return {'ok': False, 'tool': entry['id'],
                'error': {'code': 'internal-error',
                          'message': '%s: %s' % (type(exc).__name__, exc)}}


def _finish(state, *, tool, write, confirm, started, outcome, is_error) -> dict:
    duration = int((time.time() - started) * 1000)
    error = outcome.get('error')
    code = str(error.get('code') or '') if isinstance(error, dict) else ''
    audited = record_call(state['data_dir'], tool=tool, write=write, confirm=confirm,
                          ok=bool(outcome.get('ok')), code=code, duration_ms=duration,
                          client=state.get('client') or '')
    payload = dict(outcome)
    payload.setdefault('tool', tool)
    payload['duration_ms'] = duration
    payload['audited'] = audited
    payload['client'] = state.get('client') or ''
    return structured_result(payload, is_error=is_error)


# ── 三个对外工具 ─────────────────────────────────────────────────────────────
async def tools_list(state, arguments: dict) -> dict:
    plan = catalog(state, arguments.get('refresh') is True)
    needle = str(arguments.get('filter') or '').strip().lower()
    want = arguments.get('write')
    limit = arguments.get('limit')
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        limit = 200
    limit = min(int(limit), LIST_LIMIT)
    items = []
    for entry in plan['tools']:
        if isinstance(want, bool) and bool(entry['write']) is not want:
            continue
        if needle:
            haystack = ' '.join((entry['id'], entry['extension'], entry['description'])).lower()
            if needle not in haystack:
                continue
        items.append({key: entry[key]
                      for key in ('id', 'extension', 'description', 'write', 'enabled')})
    return structured_result({'ok': True, 'total': len(plan['tools']), 'matched': len(items),
                              'shown': len(items[:limit]), 'counts': plan['counts'],
                              'tools': items[:limit], 'errors': plan['errors']})


async def tools_status(state, arguments: dict) -> dict:
    return structured_result(status_payload(state))


async def tools_call(state, arguments: dict) -> dict:
    tool = str(arguments.get('tool') or '').strip()
    payload = arguments.get('arguments') if isinstance(arguments.get('arguments'), dict) else {}
    confirm = arguments.get('confirm') is True
    started = time.time()
    entry = next((item for item in catalog(state)['tools'] if item['id'] == tool), None)
    if entry is None:
        return _finish(state, tool=tool, write=False, confirm=confirm, started=started,
                       is_error=True,
                       outcome={'ok': False, 'error': {'code': 'unknown-tool',
                                                       'message': '没有这个工具：%s' % (tool or '(空)')}})
    if entry['write'] and not confirm:
        return _finish(state, tool=tool, write=True, confirm=False, started=started,
                       is_error=True,
                       outcome={'ok': False, 'write': True, 'confirm': False,
                                'error': {'code': 'confirm-required', 'message': CONFIRM_HINT}})
    result = await exec_tool(state, entry, payload)
    ok = bool(result.get('ok'))
    outcome = {'ok': ok, 'tool': tool, 'write': bool(entry['write']), 'confirm': confirm,
               'result': result.get('result')}
    if not ok:
        error = result.get('error')
        outcome['error'] = (error if isinstance(error, dict)
                            else {'code': 'tool-error', 'message': str(error or '')[:300]})
    return _finish(state, tool=tool, write=bool(entry['write']), confirm=confirm,
                   started=started, outcome=outcome, is_error=not ok)


META_HANDLERS = {'tools_list': tools_list, 'tools_call': tools_call, 'tools_status': tools_status}


def _allowed_args(name: str) -> set:
    schema = next((tool['inputSchema'] for tool in META_TOOLS if tool['name'] == name), {})
    return set((schema.get('properties') or {}))


# ── JSON-RPC 主循环 ──────────────────────────────────────────────────────────
async def handle(message: dict, state: dict):
    method = str(message.get('method') or '')
    identifier = message.get('id')
    params = message.get('params') if isinstance(message.get('params'), dict) else {}
    if method == 'initialize':
        client = params.get('clientInfo')
        if isinstance(client, dict) and client.get('name'):
            state['client'] = str(client['name'])[:80]
        return respond(identifier, {
            'protocolVersion': params.get('protocolVersion') or PROTOCOL_VERSION,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': ('本机鲸鱼娘工具面。先 tools_list 看有什么；'
                             'write:true 的工具必须在 tools_call 里带 confirm:true 才会执行。'),
        })
    if not method or method.startswith('notifications/'):
        return None
    if method == 'ping':
        return respond(identifier, {})
    if method == 'tools/list':
        return respond(identifier, {'tools': META_TOOLS})
    if method == 'tools/call':
        name = str(params.get('name') or '')
        arguments = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
        if name not in META_HANDLERS:
            return respond(identifier, structured_result(
                {'ok': False, 'tool': name,
                 'error': {'code': 'unknown-tool', 'message': '未知工具：%s' % (name or '(空)')}},
                is_error=True))
        extra = set(arguments) - _allowed_args(name)
        if extra:
            return respond(identifier, structured_result(
                {'ok': False, 'tool': name,
                 'error': {'code': 'invalid-argument',
                           'message': '不支持的参数：' + '、'.join(sorted(extra))}}, is_error=True))
        return respond(identifier, await META_HANDLERS[name](state, arguments))
    if identifier is None:
        return None
    return respond(identifier, error={'code': -32601,
                                      'message': 'Unsupported method: %s' % method})


def resolve_data_dir(explicit=None) -> Path:
    """显式参数 > WHALE_TOOLS_DATA_DIR > WHALE_QQ_HOME/data > 仓库 data/。

    WHALE_QQ_HOME 是机器人根（里面有 config.qq.yaml 和 data/），与
    tools/whale_memory_mcp.py:48 的 ``home()/'data'`` 同一口径。
    """
    chosen = str(explicit if explicit is not None else '').strip()
    if chosen:
        return Path(chosen)
    dedicated = str(os.environ.get(DATA_DIR_ENV) or '').strip()
    if dedicated:
        return Path(dedicated)
    home = str(os.environ.get(HOME_ENV) or '').strip()
    if home:
        return Path(home) / 'data'
    return Path(extension_host.default_data_dir())


def build_state(*, data_dir=None, node=None, host_script=None, source_dir=None) -> dict:
    return {'data_dir': resolve_data_dir(data_dir), 'client': '', 'paths': None, 'catalog': None,
            'handlers': {},
            'overrides': {'node': node or '', 'host_script': host_script or '',
                          'source_dir': source_dir or ''}}


async def _serve(state: dict) -> int:
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        reply = await handle(message, state)
        if reply is not None:
            emit(reply)
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='鲸鱼娘工具面 MCP 服务（stdio，JSON-RPC 2.0）')
    parser.add_argument('--list', action='store_true',
                        help='打印诊断 JSON（路径 / 计数 / 写工具清单）后退出')
    parser.add_argument('--data-dir', default=None, help='数据目录（默认仓库 data/）')
    parser.add_argument('--node', default=None, help='node 可执行文件（默认自动定位）')
    parser.add_argument('--host-script', default=None, help='host-one.mjs 路径')
    parser.add_argument('--source-dir', default=None, help='QQ Agent 0.4 源码树')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    force_utf8_stdio()
    state = build_state(data_dir=args.data_dir, node=args.node,
                        host_script=args.host_script, source_dir=args.source_dir)
    if args.list:
        emit(status_payload(state))
        return 0
    try:
        return asyncio.run(_serve(state))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
