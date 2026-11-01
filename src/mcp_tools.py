"""外部 MCP server 的工具并进现有工具面：登记表 → 工具清单 → 可调用函数。

三条边界决定了这个模块的形状：

1. import 期绝不起子进程：登记表 <data_dir>/qq-only/mcp_servers.json 只是一堆 dict，
   只有真去 tools/list、tools/call 时才 McpClient.start()；同一 server 复用同一个 client
   （懒启动 + 进程内缓存），进程退出前 stop_all() 收干净。
2. 外部世界的失败不许冒泡：起不来 / 握手超时 / 回垃圾 JSON 一律折算成
   {'ok': False, 'code': ...}；枚举期失败的那个 server 只是从清单里消失，既不影响别的
   server，也不拖慢本地 skill 工具。
3. 工具名必须落在既有 id 校验 ^[a-zA-Z0-9_-]{1,64}$ 里：<serverId>__<toolName> 清洗、
   截断、去重后再交出去，别让严格端点 400。

不新增顶层配置段：开关就是登记表里每条 server 的 enabled（缺字段按 false，安全默认）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import mcp_client

QQ_ONLY = 'qq-only'
MCP_INDEX = 'mcp_servers.json'
CALL_LOG = 'mcp_tool_calls.jsonl'
LOG_MAX_BYTES = 8 * 1024 * 1024
CLIENT_LABEL = 'mcp-client'

DEFAULT_TIMEOUT = 20.0
DEFAULT_ENUMERATE_TIMEOUT = 5.0

#: 与 src/tool_loop.py 的 _NAME_BAD 同一口径：宿主工具表只认这些字符。
MAX_ID = 64
ID_BAD = re.compile(r'[^a-zA-Z0-9_-]')
SERVER_ID_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$')

#: 起不来（进程/握手）→ 该 server unavailable；起来了但列不出来 → error。
_UNAVAILABLE_CODES = ('spawn-failed', 'handshake-timeout', 'handshake-failed', 'not-running')
#: 只有断链类失败值得重连一次；超时不重试——重试等于把等待翻倍。
_RETRY_CODES = ('not-running', 'io')

#: write_hint 只是提示：MCP 没有稳定的写语义标注，mcp_client.list_tools 也只回三个字段，
#: 按工具名里的动词切词猜，猜错不影响调用。
WRITE_TOKENS = frozenset((
    'write', 'create', 'add', 'set', 'update', 'edit', 'save', 'send', 'post', 'put',
    'patch', 'delete', 'remove', 'clear', 'move', 'rename', 'install', 'run', 'exec',
    'upload', 'push', 'merge', 'apply', 'import', 'commit',
))


def _qq_only(data_dir) -> Path:
    """data_dir 可能是运行数据根（<root>/data），也可能已经是 <root>/data/qq-only。"""
    root = Path(data_dir or '.')
    return root if root.name == QQ_ONLY else root / QQ_ONLY


def _sanitise(value) -> str:
    return ID_BAD.sub('_', str(value or '')).strip('_') or 'server'


def _tool_id(server_id, tool_name, used: set) -> str:
    """<serverId>__<toolName> 清洗成合法 id；太长截断，冲突挂 _2 后缀，保证唯一。"""
    base = ('%s__%s' % (_sanitise(server_id), _sanitise(tool_name)))[:MAX_ID]
    candidate = base
    index = 2
    while candidate in used:
        suffix = '_%d' % index
        candidate = base[:MAX_ID - len(suffix)] + suffix
        index += 1
    used.add(candidate)
    return candidate


def write_hint_of(name) -> bool:
    """按工具名切词猜「这会改东西吗」；猜错只是审计里多一个布尔。"""
    tokens = {token.lower() for token in re.split(r'[^0-9a-zA-Z]+', str(name or '')) if token}
    return bool(tokens & WRITE_TOKENS)


def _error_text(payload: dict) -> str:
    """把 mcp_client 的结构化失败压成一行原因（带 code，方便事后 grep）。"""
    payload = payload if isinstance(payload, dict) else {}
    code = str(payload.get('code') or 'error')
    message = str(payload.get('message') or payload.get('detail') or '').strip()
    return ('%s: %s' % (code, message))[:300] if message else code


class McpToolProvider:
    """登记表 → 工具清单 → 调用。一个 provider 管一批 server，各自懒启动。"""

    def __init__(self, data_dir, *, timeout=DEFAULT_TIMEOUT,
                 enumerate_timeout=DEFAULT_ENUMERATE_TIMEOUT) -> None:
        self.data_dir = Path(data_dir or '.')
        self.qq_only = _qq_only(self.data_dir)
        self.index_path = self.qq_only / MCP_INDEX
        self.log_path = self.qq_only / CALL_LOG
        self.timeout = _positive(timeout, DEFAULT_TIMEOUT)
        self.enumerate_timeout = _positive(enumerate_timeout, DEFAULT_ENUMERATE_TIMEOUT)
        self._lock = threading.RLock()
        self._servers: List[dict] = []
        self._states: Dict[str, dict] = {}
        self._clients: Dict[str, object] = {}
        self._tools: Dict[str, dict] = {}
        self._errors: List[str] = []
        #: 本代（load 以来）枚举过的 server：坏 server 只探一次，不许每条调用都等它 5s。
        self._attempted = set()
        self._loaded = False
        self._stamp = None

    # ── 登记表（只读 JSON，绝不起进程） ─────────────────────────────────────

    def load(self) -> dict:
        """读 mcp_servers.json；返回 {servers, enabled, errors}。可重复调用（改完 enabled
        再 load 一次就能看到变化），顺手关掉已停用/已删除 server 的子进程。"""
        with self._lock:
            servers, errors = self._read_index()
            keep = [one['id'] for one in servers if one['enabled']]
            for ident in [key for key in list(self._clients) if key not in keep]:
                self._drop_client(ident)
            self._servers = servers
            self._errors = errors
            wanted = {one['id']: one for one in servers}
            # 登记表变了就重新枚举：enabled 从 false 改回 true 必须立刻看得到工具。
            self._states = {ident: self._new_state(ident) for ident in wanted}
            self._attempted = set()
            self._tools = {}
            self._loaded = True
            self._stamp = self._index_stamp()
            return {'servers': len(servers),
                    'enabled': sum(1 for one in servers if one['enabled']),
                    'errors': list(errors)}

    def _index_stamp(self):
        """登记表的 (mtime_ns, size)；不存在就是 None。一次 stat，比读 JSON 便宜得多。"""
        try:
            info = self.index_path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _read_index(self) -> Tuple[List[dict], List[str]]:
        """读 + 校验登记表。文件不存在是正常态（没装过外部 server），不算错误。"""
        path = self.index_path
        try:
            if not path.is_file():
                return [], []
            loaded = json.loads(path.read_text(encoding='utf-8-sig') or '{}')
        except (OSError, ValueError) as exc:
            return [], ['登记表读不出来：%s: %s' % (type(exc).__name__, exc)]
        if not isinstance(loaded, dict) or not isinstance(loaded.get('servers'), list):
            return [], ['登记表结构不对（要 version=1 + servers 列表）：%s' % path]
        servers: List[dict] = []
        errors: List[str] = []
        for raw in loaded['servers']:
            if not isinstance(raw, dict):
                continue
            ident = str(raw.get('id') or '')
            if not SERVER_ID_RE.match(ident):
                errors.append('server id 不合法，跳过：%r' % (ident,))
                continue
            command = str(raw.get('command') or '')
            if not command.strip():
                errors.append('server %s 没有 command，跳过' % ident)
                continue
            raw_args = raw.get('args')
            args = [str(one) for one in raw_args] if isinstance(raw_args, list) else []
            raw_env = raw.get('env')
            env = ({str(key): str(value) for key, value in raw_env.items()}
                   if isinstance(raw_env, dict) else {})
            cwd = str(raw.get('cwd') or '').strip() or None
            name = str(raw.get('name') or '').strip() or ident
            servers.append({
                'id': ident, 'name': name, 'command': command, 'args': args,
                'env': env, 'cwd': cwd,
                # 缺 enabled 一律当停用：装进来不等于放行。
                'enabled': raw.get('enabled') is True,
            })
        return servers, errors

    # ── 枚举 ───────────────────────────────────────────────────────────────

    def tools(self) -> list:
        """enabled 且枚举成功的 server 的工具表，形如
        {id, name, description, parameters, server, write_hint}。任何失败只让该 server 缺席。"""
        if not self._loaded:
            self.load()
        elif self._index_stamp() != self._stamp:
            # 登记表是运行时可写的（导入一个包就会多一条）：缓存按 mtime+size 失效，
            # 否则 enabled 改回 true / 新装 server 得重启进程才看得见。
            self.load()
        with self._lock:
            enabled = [one for one in self._servers if one['enabled']]
        for server in enabled:
            self._ensure_tools(server)
        with self._lock:
            return [dict(entry) for entry in self._tools.values()]

    def _ensure_tools(self, server: dict) -> None:
        ident = str(server['id'])
        with self._lock:
            if ident in self._attempted:
                return
            self._attempted.add(ident)
        client, error = self._client_for(server)
        if client is None:
            self._mark_failure(ident, 'unavailable', error)
            return
        with self._lock:
            client.timeout = self.enumerate_timeout
        try:
            listed = client.list_tools()
        except Exception as exc:
            listed = {'ok': False, 'code': 'internal',
                      'message': '%s: %s' % (type(exc).__name__, exc)}
        finally:
            with self._lock:
                client.timeout = self.timeout
        if not listed.get('ok'):
            code = str(listed.get('code') or '')
            state_name = 'unavailable' if code in _UNAVAILABLE_CODES else 'error'
            self._mark_failure(ident, state_name, _error_text(listed))
            return
        rows = listed.get('tools') or []
        with self._lock:
            used = {key for key, entry in self._tools.items() if entry['server'] != ident}
            collected: Dict[str, dict] = {}
            for item in rows:
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name') or '').strip()
                if not name:
                    continue
                tool_id = _tool_id(ident, name, used)
                collected[tool_id] = {
                    'id': tool_id, 'name': name,
                    'description': str(item.get('description') or '') or name,
                    'parameters': item.get('inputSchema') if isinstance(
                        item.get('inputSchema'), dict) else {'type': 'object', 'properties': {}},
                    'server': ident, 'write_hint': write_hint_of(name),
                }
            for key in [k for k, v in self._tools.items() if v['server'] == ident]:
                self._tools.pop(key, None)
            self._tools.update(collected)
            self._states[ident] = {'id': ident, 'state': 'running', 'error': '',
                                   'tools': len(collected)}

    def _mark_failure(self, ident: str, state_name: str, error: str) -> None:
        with self._lock:
            self._states[ident] = {'id': ident, 'state': state_name, 'error': str(error)[:300],
                                   'tools': 0}
            for key in [k for k, v in self._tools.items() if v['server'] == ident]:
                self._tools.pop(key, None)

    def _new_state(self, ident: str) -> dict:
        return {'id': ident, 'state': 'idle', 'error': '', 'tools': 0}

    # ── 懒启动 / 缓存 ──────────────────────────────────────────────────────

    def _client_for(self, server: dict) -> Tuple[object, str]:
        """取这个 server 的 client；没有就起一个（复用缓存，同一 server 只有一条管道）。"""
        ident = str(server['id'])
        with self._lock:
            existing = self._clients.get(ident)
        if existing is not None and getattr(existing, 'running', False):
            return existing, ''
        if existing is not None:
            self._drop_client(ident)
        client = mcp_client.McpClient(str(server['command']), args=server['args'],
                                      env=server['env'], cwd=server['cwd'],
                                      timeout=self.timeout, name=ident)
        # 拉起+握手也算 enumerate 预算：卡住的握手必须被 5s 掐掉。
        client.timeout = self.enumerate_timeout
        try:
            started = client.start()
        except Exception as exc:
            started = {'ok': False, 'code': 'internal',
                       'message': '%s: %s' % (type(exc).__name__, exc)}
        finally:
            client.timeout = self.timeout
        if not started.get('ok'):
            try:
                client.stop()
            except Exception:
                pass
            return None, _error_text(started)
        with self._lock:
            self._clients[ident] = client
        return client, ''

    def _drop_client(self, ident: str) -> None:
        with self._lock:
            client = self._clients.pop(ident, None)
        if client is None:
            return
        try:
            client.stop()
        except Exception:
            pass

    # ── 调用 ───────────────────────────────────────────────────────────────

    def owns(self, tool_id) -> bool:
        """只读登记表（不起任何进程）：这个 id 的前缀是不是某个 enabled 的 server。"""
        ident = str(tool_id or '')
        if '__' not in ident:
            return False
        head = ident.split('__')[0]
        if not self._loaded:
            self.load()
        with self._lock:
            return any(one['enabled'] and _sanitise(one['id']) == head
                       for one in self._servers)

    def call(self, tool_id, arguments=None) -> dict:
        """调一个外部工具；永不抛异常，永远带 ok/code/duration_ms/server/name 回包。"""
        started = time.monotonic()
        ident = str(tool_id or '')
        result = {'ok': False, 'code': 'unknown-tool', 'content': [], 'isError': False,
                  'duration_ms': 0, 'server': '', 'name': ''}
        entry = None
        try:
            entry = self._entry(ident)
            if entry is None:
                result['message'] = '没有这个外部 MCP 工具：%s' % ident
            else:
                result['server'] = str(entry.get('server') or '')
                result['name'] = str(entry.get('name') or '')
                result.update(self._call_entry(entry, arguments))
        except Exception as exc:
            result.update({'ok': False, 'code': 'internal', 'content': [], 'isError': False,
                           'message': ('%s: %s' % (type(exc).__name__, exc))[:300]})
        result['duration_ms'] = int((time.monotonic() - started) * 1000)
        if result.get('ok'):
            result['code'] = ''
        result['write'] = (bool(entry.get('write_hint')) if entry
                           else entry_write_hint(ident))
        result['audited'] = self._audit(ident, result)
        return result

    def _entry(self, tool_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._tools.get(tool_id)
        if entry is not None:
            return dict(entry)
        self.tools()
        with self._lock:
            entry = self._tools.get(tool_id)
        return dict(entry) if entry is not None else None

    def _call_entry(self, entry: dict, arguments) -> dict:
        """真正调一次；断链类失败重连一次，超时不重试（重试只会把等待翻倍）。"""
        if arguments is not None and not isinstance(arguments, dict):
            return {'ok': False, 'code': 'internal',
                    'message': 'arguments 必须是 dict，收到 %s' % type(arguments).__name__}
        ident = str(entry.get('server') or '')
        with self._lock:
            server = next((one for one in self._servers if one['id'] == ident), None)
        if server is None or not server['enabled']:
            return {'ok': False, 'code': 'unavailable',
                    'message': 'server %s 不在登记表里或已停用' % ident}
        outcome: dict = {}
        for attempt in (1, 2):
            client, error = self._client_for(server)
            if client is None:
                return {'ok': False, 'code': 'unavailable', 'message': error}
            try:
                outcome = client.call_tool(str(entry.get('name') or ''),
                                          arguments if isinstance(arguments, dict) else None)
            except Exception as exc:
                outcome = {'ok': False, 'code': 'internal',
                           'message': '%s: %s' % (type(exc).__name__, exc)}
            if outcome.get('ok'):
                return {'ok': True, 'code': '', 'content': outcome.get('content') or [],
                        'isError': bool(outcome.get('isError'))}
            if attempt == 1 and str(outcome.get('code') or '') in _RETRY_CODES:
                self._drop_client(ident)
                continue
            break
        return {'ok': False, 'code': str(outcome.get('code') or 'error'),
                'message': _error_text(outcome)}

    # ── 审计 / 状态 / 收尾 ─────────────────────────────────────────────────

    def _audit(self, tool_id: str, result: dict) -> bool:
        """每次调用写一行 mcp_tool_calls.jsonl（字段与工具桥一致）；写失败只判 audited:false。"""
        record = {
            'ts': round(time.time(), 3), 'tool': str(tool_id),
            'write': bool(result.get('write')), 'confirm': False,
            # 应用级失败（isError）不该在账本里记成成功
            'ok': bool(result.get('ok')) and not bool(result.get('isError')),
            'code': str(result.get('code') or ''),
            'duration_ms': int(result.get('duration_ms') or 0), 'client': CLIENT_LABEL,
        }
        path = self.log_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.is_file() and path.stat().st_size > LOG_MAX_BYTES:
                    path.replace(path.with_name(path.stem + '.previous.jsonl'))
            except OSError:
                pass
            with path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + chr(10))
            return True
        except (OSError, ValueError):
            return False

    def status(self) -> dict:
        """每个 server 一行：{id, enabled, state, tools, error}，state 只取
        idle / running / unavailable / error。"""
        if not self._loaded:
            self.load()
        with self._lock:
            rows = []
            for server in self._servers:
                state = dict(self._states.get(server['id']) or self._new_state(server['id']))
                rows.append({'id': server['id'], 'enabled': bool(server['enabled']),
                             'state': state['state'], 'tools': int(state['tools']),
                             'error': state['error'],
                             'running': bool(getattr(self._clients.get(server['id']),
                                                    'running', False))})
            return {'servers': rows, 'tools': len(self._tools),
                    'enabled': sum(1 for one in self._servers if one['enabled']),
                    'errors': list(self._errors)}

    def stop_all(self) -> dict:
        """收掉所有子进程；幂等。收完清空枚举缓存，下次 tools() 会重新拉起。"""
        with self._lock:
            clients = dict(self._clients)
            self._clients = {}
            idents = [one['id'] for one in self._servers]
        stopped, errors = [], []
        for ident, client in clients.items():
            try:
                client.stop()
                stopped.append(ident)
            except Exception as exc:
                errors.append('%s: %s: %s' % (ident, type(exc).__name__, exc))
        with self._lock:
            self._tools = {}
            self._attempted = set()
            for ident in idents:
                state = self._states.get(ident)
                if state is not None:
                    self._states[ident] = {'id': ident, 'state': 'idle', 'error': '',
                                           'tools': 0}
        return {'ok': not errors, 'stopped': len(stopped), 'servers': stopped,
                'errors': errors}


def _positive(value, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number if number > 0 else float(fallback)


def entry_write_hint(tool_id) -> bool:
    """审计用的 write 列：名字里的动词已经在 tools() 里算过，这里只按 id 尾巴复算。"""
    tail = str(tool_id or '').split('__')[-1]
    return write_hint_of(tail)


_PROVIDERS: Dict[str, McpToolProvider] = {}
_PROVIDERS_LOCK = threading.RLock()


def provider_for(data_dir, **kwargs) -> McpToolProvider:
    """按 qq-only 目录缓存 provider：工具环每条调用都会问一次，不能每次新建。"""
    key = str(_qq_only(data_dir).resolve())
    with _PROVIDERS_LOCK:
        existing = _PROVIDERS.get(key)
        if existing is None:
            existing = McpToolProvider(data_dir, **kwargs)
            _PROVIDERS[key] = existing
        return existing


def tool_list(data_dir, **kwargs) -> list:
    """给工具面用的便捷入口：外部失败一律折成空表，绝不打扰调用方。"""
    try:
        return provider_for(data_dir, **kwargs).tools()
    except Exception:
        return []


def stop_all(data_dir=None) -> dict:
    """收干净：传 data_dir 只收那一个 provider，不传收本进程建过的全部。"""
    with _PROVIDERS_LOCK:
        if data_dir is not None:
            key = str(_qq_only(data_dir).resolve())
            providers = [p for k, p in _PROVIDERS.items() if k == key]
            for provider in providers:
                _PROVIDERS.pop(key, None)
        else:
            providers = list(_PROVIDERS.values())
            _PROVIDERS.clear()
    results = []
    for provider in providers:
        try:
            results.append(provider.stop_all())
        except Exception as exc:
            results.append({'ok': False, 'stopped': 0, 'servers': [],
                            'errors': ['%s: %s' % (type(exc).__name__, exc)]})
    return {'ok': all(one.get('ok') for one in results), 'providers': len(results),
            'stopped': sum(int(one.get('stopped') or 0) for one in results),
            'errors': [error for one in results for error in one.get('errors') or []]}
