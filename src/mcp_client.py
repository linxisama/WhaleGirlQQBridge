"""stdio MCP 客户端：把外部 MCP server 当子进程拉起、握手、列工具、调工具、收干净。

标准库手写 JSON-RPC 2.0（venv 里没有 mcp SDK）：一行一条消息，UTF-8 双向。
所有公开方法都不抛未捕获异常，一律返回 {'ok': ...} 结构；失败时带 code。

两个必防的坑：
- Windows 的 .cmd/.bat 垫片会留孤儿（cmd.exe → node）。收尾必须连树杀
  （taskkill /T /F），只 kill 顶层进程会把 node 留在后台。
- 对端可能写出非 JSON 的行（含 log 噪声）；解析失败记 bad-json 后继续读下一行。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

PROTOCOL_VERSION = '2025-06-18'
CLIENT_NAME = 'whale-agent'
FALLBACK_CLIENT_VERSION = '1.0.0'

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_PS_SUFFIXES = ('.ps1',)
_STDERR_KEEP = 8000
_STDERR_TAIL_CHARS = 200
_MAX_BAD_LINES = 20
_STOP_WAIT_SECONDS = 1.5
_KILL_WAIT_SECONDS = 5.0

ERROR_CODES = ('spawn-failed', 'handshake-timeout', 'handshake-failed',
               'not-running', 'timeout', 'server-error', 'bad-json', 'io',
               'internal')


def client_version() -> str:
    """clientInfo.version：读仓库根 version.txt，读不到退回 '1.0.0'。"""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'version.txt'), 'r', encoding='utf-8') as handle:
            text = handle.read().strip()
        if text:
            return text.splitlines()[0].strip()
    except (OSError, UnicodeDecodeError):
        pass
    return FALLBACK_CLIENT_VERSION


def resolve_command(command: str):
    """把用户给的命令解析成可执行文件的绝对路径；解析不到返回 None。

    带路径分隔符的按文件找（Windows 上补 .cmd/.exe/.bat/.ps1 后缀），
    裸命令名走 PATH 查找。
    """
    text = str(command or '')
    if not text.strip():
        return None
    looks_path = os.path.isabs(text) or '/' in text or '\\' in text
    if looks_path:
        candidates = [text]
        if os.name == 'nt' and not os.path.splitext(text)[1]:
            candidates += [text + ext for ext in ('.cmd', '.exe', '.bat', '.ps1')]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return None
    found = shutil.which(text)
    return os.path.abspath(found) if found else None


def build_argv(resolved: str, args=()) -> list:
    """拼 argv。.ps1 走 powershell；.cmd/.bat 直接当 argv[0]（实测可用，且参数不丢引号）。"""
    tail = [str(item) for item in (args or ())]
    if os.name == 'nt' and str(resolved).lower().endswith(_PS_SUFFIXES):
        shell = shutil.which('pwsh') or shutil.which('powershell') or 'powershell.exe'
        return [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-File', resolved] + tail
    return [resolved] + tail


def _kill_tree(proc) -> None:
    """终止整个子进程树（不做优雅等待，调用方自己 bounded wait）。"""
    pid = int(getattr(proc, 'pid', 0) or 0)
    if pid <= 0:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=False,
                           creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except (OSError, ValueError):
            pass
    try:
        proc.kill()
    except (OSError, ValueError):
        pass


def _wait_gone(proc, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        try:
            if proc.poll() is not None:
                return True
        except OSError:
            return True
        time.sleep(0.02)
    try:
        return proc.poll() is not None
    except OSError:
        return True


class McpServerError(Exception):
    """结构化失败：.code 是稳定枚举，.detail 带上对端 stderr 尾巴（栈常常只在那儿）。"""

    def __init__(self, code: str, message: str, detail: str = ''):
        super().__init__(str(message))
        self.code = str(code)
        self.message = str(message)
        self.detail = str(detail or '')

    def as_dict(self, **extra) -> dict:
        payload = {'ok': False, 'code': self.code, 'message': self.message,
                   'detail': self.detail}
        payload.update(extra)
        return payload


class McpClient:
    """一条 stdio 管道上的 MCP 客户端：start / list_tools / call_tool / stop。"""

    def __init__(self, command: str, args=None, env=None, cwd: str = None,
                 timeout: float = 20.0, name: str = ''):
        self.command = str(command or '')
        self.args = [str(item) for item in (args or [])]
        self.env = dict(env or {})
        self.cwd = str(cwd) if cwd else None
        try:
            self.timeout = float(timeout) if float(timeout) > 0 else 20.0
        except (TypeError, ValueError):
            self.timeout = 20.0
        self.name = str(name or '')
        self.protocol_version = PROTOCOL_VERSION
        self.server_info: dict = {}
        self.stats = {'requests': 0, 'notifications': 0, 'bad_json': 0,
                      'unknown_ids': 0, 'timeouts': 0}
        self.bad_lines: list = []
        self._proc = None
        self._running = False
        self._eof = False
        self._next_id = 0
        self._pending: dict = {}
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stderr_buf = bytearray()
        self._reader = None
        self._stderr_reader = None

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> dict:
        """拉起子进程并完成 initialize / notifications/initialized 握手。"""
        with self._lock:
            if self._running and self._alive(self._proc):
                return {'ok': True, 'already_started': True, 'pid': self.pid,
                        'protocolVersion': self.protocol_version,
                        'serverInfo': dict(self.server_info), 'name': self.name}
        resolved = resolve_command(self.command)
        if resolved is None:
            return McpServerError('spawn-failed',
                                  f'命令不可执行：{self.command!r}',
                                  '命令既不是可执行文件，也不在 PATH 上').as_dict(
                                      name=self.name, pid=0)
        argv = build_argv(resolved, self.args)
        env = os.environ.copy()
        env.update(self.env)
        kwargs: dict = {
            'stdin': subprocess.PIPE,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'bufsize': 0,
            'cwd': self.cwd,
            'env': env,
        }
        if os.name == 'nt':
            kwargs['creationflags'] = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        else:
            kwargs['start_new_session'] = True
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except FileNotFoundError:
            return McpServerError('spawn-failed', f'可执行文件不存在：{argv[0]!r}',
                                  '').as_dict(name=self.name, pid=0, argv=argv)
        except (OSError, ValueError) as exc:
            return McpServerError('spawn-failed', '启动子进程失败',
                                  f'{type(exc).__name__}: {exc}').as_dict(
                                      name=self.name, pid=0, argv=argv)

        with self._lock:
            self._proc = proc
            self._running = True
            self._eof = False
            self._pending = {}
            self._stderr_buf = bytearray()
            self.bad_lines = []
        self._stderr_reader = threading.Thread(
            target=self._pump_stderr, args=(proc,), daemon=True,
            name=f'mcp-stderr-{self.name or "client"}')
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc,), daemon=True,
            name=f'mcp-stdout-{self.name or "client"}')
        self._stderr_reader.start()
        self._reader.start()

        params = {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {},
            'clientInfo': {'name': CLIENT_NAME, 'version': client_version()},
        }
        try:
            result = self._request('initialize', params, timeout=self.timeout,
                                   timeout_code='handshake-timeout',
                                   error_code='handshake-failed',
                                   eof_code='handshake-failed')
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except McpServerError as exc:
            detail = exc.detail or self.stderr_tail()
            # 握手没成就不留活进程：当场连树收掉，避免调用方忘了 stop 造成残留
            self._teardown(proc)
            return McpServerError(exc.code, exc.message, detail).as_dict(
                name=self.name, pid=int(getattr(proc, 'pid', 0) or 0),
                argv=argv, stopped=True)

        payload = result if isinstance(result, dict) else {}
        returned = payload.get('protocolVersion')
        self.protocol_version = str(returned or PROTOCOL_VERSION)
        info = payload.get('serverInfo')
        self.server_info = dict(info) if isinstance(info, dict) else {}
        return {'ok': True, 'protocolVersion': self.protocol_version,
                'serverInfo': self.server_info, 'pid': self.pid,
                'name': self.name, 'command': resolved}

    @property
    def pid(self) -> int:
        return int(getattr(self._proc, 'pid', 0) or 0)

    @property
    def running(self) -> bool:
        return bool(self._running and self._alive(self._proc))

    def stop(self) -> dict:
        """关 stdin → 等退出 → 超时连树强杀。幂等，重复调用不报错。"""
        with self._lock:
            proc = self._proc
            if proc is None:
                self._running = False
                return {'ok': True, 'stopped': False, 'killed': False,
                        'already_stopped': True, 'name': self.name}
            alive = self._alive(proc)
            pid = int(getattr(proc, 'pid', 0) or 0)
        if not alive:
            self._running = False
            self._close_pipes(proc)
            return {'ok': True, 'stopped': False, 'killed': False,
                    'already_stopped': True, 'pid': pid, 'name': self.name,
                    'exit_code': self._poll(proc)}
        self._close_stdin(proc)
        gone = _wait_gone(proc, min(self.timeout, _STOP_WAIT_SECONDS))
        killed = False
        if not gone:
            _kill_tree(proc)
            killed = True
        else:
            _kill_tree(proc)          # 温柔退出也补一刀：垫片可能留孤儿
        _wait_gone(proc, _KILL_WAIT_SECONDS)
        self._join_readers()
        self._close_pipes(proc)
        self._running = False
        self._wake_pending('eof')
        return {'ok': True, 'stopped': True, 'killed': killed, 'pid': pid,
                'name': self.name, 'exit_code': self._poll(proc)}

    def close(self) -> dict:
        return self.stop()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _teardown(self, proc) -> None:
        self._close_stdin(proc)
        _kill_tree(proc)
        _wait_gone(proc, _KILL_WAIT_SECONDS)
        self._join_readers()
        self._close_pipes(proc)
        self._running = False
        self._wake_pending('eof')

    # ── MCP 方法 ────────────────────────────────────────────────────────────

    def list_tools(self) -> dict:
        """tools/list：只保留 name / description / inputSchema 三个字段。"""
        try:
            result = self._request('tools/list', {}, label='tools/list')
        except McpServerError as exc:
            return exc.as_dict(name=self.name)
        payload = result if isinstance(result, dict) else {}
        tools = []
        for item in (payload.get('tools') or []):
            if not isinstance(item, dict):
                continue
            schema = item.get('inputSchema')
            tools.append({'name': str(item.get('name') or ''),
                          'description': str(item.get('description') or ''),
                          'inputSchema': schema if isinstance(schema, dict) else {}})
        return {'ok': True, 'tools': tools, 'count': len(tools)}

    def call_tool(self, name, arguments=None) -> dict:
        """tools/call：isError 原样透传；超时/对端错误按工具名记。"""
        tool = str(name or '')
        started = time.monotonic()
        if arguments is not None and not isinstance(arguments, dict):
            return McpServerError('internal', f'tools/call {tool!r} 的 arguments 必须是 dict',
                                  f'收到 {type(arguments).__name__}').as_dict(
                                      name=tool, duration_ms=0.0)
        params = {'name': tool}
        if arguments is not None:
            params['arguments'] = arguments
        try:
            result = self._request('tools/call', params, label=f'tools/call:{tool}')
        except McpServerError as exc:
            return exc.as_dict(name=tool, duration_ms=self._elapsed_ms(started))
        if not isinstance(result, dict):
            return McpServerError('bad-json', f'tools/call {tool!r} 回包不是 JSON 对象',
                                  repr(result)[:_STDERR_TAIL_CHARS]).as_dict(
                                      name=tool, duration_ms=self._elapsed_ms(started))
        content = result.get('content')
        return {'ok': True, 'content': content if isinstance(content, list) else [],
                'isError': bool(result.get('isError')), 'raw': result,
                'duration_ms': self._elapsed_ms(started), 'name': tool}

    # ── JSON-RPC 底层 ───────────────────────────────────────────────────────

    def _request(self, method: str, params=None, *, timeout=None, label=None,
                 timeout_code: str = 'timeout', error_code: str = 'server-error',
                 eof_code: str = 'io'):
        tag = str(label or method)
        limit = self.timeout if timeout is None else float(timeout)
        with self._lock:
            if not self._running or self._proc is None or not self._alive(self._proc):
                raise McpServerError('not-running', 'MCP 连接未建立或已关闭',
                                     self.stderr_tail())
            self._next_id += 1
            request_id = self._next_id
            key = self._id_key(request_id)
            event = threading.Event()
            bucket: dict = {}
            self._pending[key] = {'event': event, 'bucket': bucket, 'label': tag}
        message = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
        if params is not None:
            message['params'] = params
        try:
            self._send(message)
        except McpServerError:
            with self._lock:
                self._pending.pop(key, None)
            raise
        self.stats['requests'] += 1
        if not event.wait(limit):
            with self._lock:
                self._pending.pop(key, None)
            self.stats['timeouts'] += 1
            raise McpServerError(timeout_code,
                                 f'{tag} 等待回包超时（{limit:g}s）',
                                 self.stderr_tail())
        if 'eof' in bucket:
            self._settle_after_eof()
            raise McpServerError(eof_code, f'{tag} 期间对端关闭了 stdout（子进程已退出）',
                                 self.stderr_tail(wait=True))
        if 'error' in bucket:
            error = bucket.get('error')
            if isinstance(error, dict):
                text = str(error.get('message') or error)
                detail = json.dumps(error, ensure_ascii=False)
            else:
                text = str(error)
                detail = text
            raise McpServerError(error_code, f'{tag} 对端返回错误：{text}',
                                 f'{detail[:400]} | stderr={self.stderr_tail()}')
        return bucket.get('result')

    def _send(self, message: dict) -> None:
        proc = self._proc
        try:
            payload = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8')
        except (TypeError, ValueError) as exc:
            raise McpServerError('internal', '请求参数无法序列化成 JSON',
                                 f'{type(exc).__name__}: {exc}') from None
        with self._write_lock:
            stream = getattr(proc, 'stdin', None)
            if proc is None or stream is None:
                raise McpServerError('not-running', 'stdin 不可用，MCP 连接未建立',
                                     self.stderr_tail())
            try:
                stream.write(payload)
                stream.flush()
            except (OSError, ValueError) as exc:
                raise McpServerError('io', '写入子进程 stdin 失败',
                                     f'{type(exc).__name__}: {exc} | '
                                     f'stderr={self.stderr_tail()}') from None

    # ── 读线程 ──────────────────────────────────────────────────────────────

    def _read_loop(self, proc) -> None:
        stream = getattr(proc, 'stdout', None)
        while stream is not None:
            try:
                raw = stream.readline()
            except (OSError, ValueError):
                break
            if not raw:
                break
            line = raw.decode('utf-8', 'replace').strip()
            if line:
                self._dispatch(line)
        self._eof = True
        self._wake_pending('eof')

    def _pump_stderr(self, proc) -> None:
        stream = getattr(proc, 'stderr', None)
        while stream is not None:
            try:
                chunk = stream.read(512)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._lock:
                self._stderr_buf.extend(chunk)
                overflow = len(self._stderr_buf) - _STDERR_KEEP
                if overflow > 0:
                    del self._stderr_buf[:overflow]

    def _dispatch(self, line: str) -> None:
        """一行一条 JSON-RPC：按 id 派回包，其余（notification / log / 坏行）跳过。"""
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            self.stats['bad_json'] += 1
            if len(self.bad_lines) < _MAX_BAD_LINES:
                self.bad_lines.append(line[:_STDERR_TAIL_CHARS])
            return
        if not isinstance(message, dict):
            self.stats['bad_json'] += 1
            if len(self.bad_lines) < _MAX_BAD_LINES:
                self.bad_lines.append(line[:_STDERR_TAIL_CHARS])
            return
        has_id = message.get('id') is not None
        if has_id and ('result' in message or 'error' in message):
            key = self._id_key(message.get('id'))
            with self._lock:
                entry = self._pending.pop(key, None)
            if entry is None:
                self.stats['unknown_ids'] += 1
                return
            if 'error' in message:
                entry['bucket']['error'] = message.get('error')
            else:
                entry['bucket']['result'] = message.get('result')
            entry['event'].set()
            return
        self.stats['notifications'] += 1

    def _wake_pending(self, reason: str) -> None:
        with self._lock:
            entries = list(self._pending.values())
            self._pending = {}
        for entry in entries:
            entry['bucket'][reason] = True
            entry['event'].set()

    def _settle_after_eof(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=2.0)
            except (OSError, ValueError, subprocess.SubprocessError):
                self._wait_gone(proc, 1.0)
        thread = self._stderr_reader
        if thread is not None and thread.is_alive():
            thread.join(1.0)

    # ── 小工具 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _id_key(value) -> str:
        return value if isinstance(value, str) else str(value)

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.monotonic() - started) * 1000, 3)

    @staticmethod
    def _poll(proc):
        try:
            return proc.poll()
        except OSError:
            return None

    @staticmethod
    def _alive(proc=None) -> bool:
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except OSError:
            return False

    def _join_readers(self) -> None:
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive():
                thread.join(1.0)

    @staticmethod
    def _close_pipes(proc) -> None:
        for name in ('stdin', 'stdout', 'stderr'):
            stream = getattr(proc, name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _close_stdin(proc) -> None:
        stream = getattr(proc, 'stdin', None)
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    def stderr_tail(self, wait: bool = False) -> str:
        """对端 stderr 的收尾 200 字符：子进程的栈常常只出现在这里。"""
        if wait:
            self._settle_after_eof()
        with self._lock:
            blob = bytes(self._stderr_buf)
        text = blob.decode('utf-8', 'replace').strip()
        return text[-_STDERR_TAIL_CHARS:]
