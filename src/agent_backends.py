"""W5a：本地外部 agent 的通用后端层（profile + 连接生命周期 + 结构化错误 + 工作区）。

本模块把“一个本地 CLI agent”当成后端来描述和调用，只做核心与测试：
- profile 语法与校验（command / args / workdir / env / timeout / protocol）；
- 连接生命周期（probe / start / cancel / stop / status）与状态机
  idle | running | stopped | failed（心跳/取消/急停下一次动作前生效、重启不重放，
  语义照 src/local_agent/runtime.py 的风格：取消只是“请求”，状态以真实退出为准）；
- 结构化错误：任何对外方法都返回 dict，**永不抛裸异常**；失败形如
  {'ok': False, 'code': '...', 'message': '...'}，code 一律小写下划线；
- 工作区：直接复用 src/agent_workspace.py，键就是 backendId
  （<data_dir>/agent_workspace/<backendId>/，data_dir 由构造参数传入，绝不硬编码）；
- 不留残进程：stop() 终止整个子进程树（Windows taskkill /T /F），timeout / cancel /
  异常路径同样走树杀；模块级 live_backends() 列出仍在跑的后端，进程退出自动清除。

明确不做（留给 W5b）：桥接端点、GUI 面板、local_agent_bridge 重写、DSH 协议对话、
MCP、cordis.patch.yml。profile 里的 protocol 只是元数据，本模块不解释它。

错误码清单（全部为小写下划线，出现处见各方法）：
  bad_profile        构造参数或 profile 校验失败（backendId / data_dir / prompt / timeout）
  bad_profile_file   profile 文件读不出来或结构不合法（load_profiles 专用）
  duplicate_id       同一个 backendId 已有后端在跑（一个 id 同时只能有一个活进程）
  command_not_found  命令解析不到，或可执行文件不存在
  already_running    同一个后端同时只允许一个 run
  not_running        没有在跑的任务（cancel 时）
  timeout            超过预算未结束，进程树已被终止
  nonzero_exit       退出码非 0（stderr 尾部进 message，完整输出在 stderr 字段）
  cancelled          被 cancel() 请求取消后结束
  stopped            被 stop() 主动终止后结束
  io                 子进程/文件系统层面失败（含启动失败、写盘失败）
  internal           兜底：任何未预料的异常都归一成这个码，不外抛

profile 校验规则（写死在这里，W5b 只能在此基础上加，不能放宽）：
  1. 只允许这六个键：command / args / workdir / env / timeout / protocol；
     出现任何其它键（含拼错的 work_dir）= bad_profile。
  2. command：必填、非空字符串、无控制字符、长度 <= 512；不做 shell 解释
     （永远 shell=False），解析顺序 = 绝对/含分隔符路径直接判存在，否则 shutil.which。
  3. args：必须是 list（tuple 也不行），每项必须是 str、无控制字符、长度 <= 4096、
     项数 <= 64。参数**不拼 shell**；仅当解析结果是 .cmd/.bat 垫片时（Windows 上
     CreateProcess 会经 cmd.exe 解析命令行）额外拒绝 shell 元字符 | & < > ^ % "。
  4. workdir：可选字符串；非空时按当前进程 CWD 解析为绝对路径，且必须是已存在的
     目录，否则 = bad_profile；默认 '' 表示继承当前进程 CWD。
  5. env：必须是 dict，键匹配 ^[A-Za-z_][A-Za-z0-9_]{0,63}$，值必须是 str、无控制字符、
     长度 <= 1024，项数 <= 32。**拒绝**会改写“命令解析 / 动态库加载”注入面的键
     （PATH、PATHEXT、COMSPEC、SYSTEMROOT、WINDIR、PSModulePath、PYTHONPATH /
     PYTHONHOME / PYTHONSTARTUP / PYTHONUSERBASE / PYTHONEXECUTABLE、NODE_OPTIONS /
     NODE_PATH、LD_PRELOAD / LD_LIBRARY_PATH / LD_AUDIT、DYLD_INSERT_LIBRARIES /
     DYLD_LIBRARY_PATH、BASH_ENV、ENV、IFS、SHELLOPTS、CDPATH、PROMPT_COMMAND）：
     这些键能把 command 劫持到另一个可执行文件或往子进程塞代码，不是“环境变量”。
  6. timeout：可选数字（bool 不算数字），必须有限且 > 0，否则 = bad_profile；
     随后夹到 [1, 600] 秒（< 1 抬到 1，> 600 压到 600），缺省 120。
  7. protocol：可选标识符 ^[A-Za-z][A-Za-z0-9_.-]{0,31}$，缺省 'none'；只是元数据。

输出与进程约定：
- stdout / stderr 分开读，各自上限 MAX_OUTPUT_BYTES = 64KB，超限保留**尾部**
  （丢最早的内容）并置 truncated=True，同时给出 total 字节数与 dropped 字节数；
- 子进程以 CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW 启动（不弹窗、可成组终止），
  stdin 关闭或写入 prompt 后立即关闭；POSIX 用 start_new_session=True。
"""
from __future__ import annotations

import abc
import functools
import itertools
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Optional

from . import agent_workspace
from .agent_detect import PROMPT_PLACEHOLDER

__all__ = [
    'BACKEND_KINDS', 'STATES', 'ERROR_CODES', 'PROFILE_KEYS', 'DENIED_ENV_KEYS',
    'MAX_OUTPUT_BYTES', 'MIN_TIMEOUT', 'MAX_TIMEOUT', 'DEFAULT_TIMEOUT',
    'PROBE_TIMEOUT', 'SCHEMA_VERSION', 'DSH_PROFILE', 'BACKEND_ID_PATTERN',
    'AgentBackend', 'CliBackend', 'DshBackend',
    'validate_profile', 'make_backend', 'live_backends',
    'load_profiles', 'save_profiles',
]

# ── 常量 ────────────────────────────────────────────────────────────────────

BACKEND_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
BACKEND_KINDS = ('cli', 'dsh')
STATES = ('idle', 'running', 'stopped', 'failed')
ERROR_CODES = ('bad_profile', 'bad_profile_file', 'duplicate_id', 'command_not_found',
               'already_running', 'not_running', 'timeout', 'nonzero_exit',
               'cancelled', 'stopped', 'io', 'internal')

PROFILE_KEYS = frozenset({'command', 'args', 'workdir', 'env', 'timeout', 'protocol'})
DEFAULT_PROTOCOL = 'none'
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 600.0
DEFAULT_TIMEOUT = 120.0
PROBE_TIMEOUT = 15.0
MAX_OUTPUT_BYTES = 64 * 1024
MAX_COMMAND_LENGTH = 512
MAX_ARG_LENGTH = 4096
MAX_ARGS = 64
MAX_ENV_ENTRIES = 32
MAX_ENV_VALUE = 1024
SCHEMA_VERSION = 1
MAX_PROFILE_FILE_BYTES = 1024 * 1024

DSH_PROFILE = {
    'command': 'dsh',
    'args': [],
    'workdir': '',
    'env': {},
    'timeout': DEFAULT_TIMEOUT,
    'protocol': 'none',
}

DENIED_ENV_KEYS = frozenset({
    'PATH', 'PATHEXT', 'COMSPEC', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'PSMODULEPATH',
    'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONUSERBASE', 'PYTHONEXECUTABLE',
    'NODE_OPTIONS', 'NODE_PATH',
    'LD_PRELOAD', 'LD_LIBRARY_PATH', 'LD_AUDIT', 'DYLD_INSERT_LIBRARIES',
    'DYLD_LIBRARY_PATH', 'BASH_ENV', 'ENV', 'IFS', 'SHELLOPTS', 'CDPATH',
    'PROMPT_COMMAND',
})

_CTRL = re.compile(r'[\x00-\x1f\x7f]')
_ENV_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
_PROTOCOL = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,31}$')
_SHELL_META = re.compile(r'[|&<>^%"]')
_SHIM_SUFFIXES = ('.cmd', '.bat')
_PS_SUFFIXES = ('.ps1',)

_POLL_SECONDS = 0.02
_KILL_WAIT_SECONDS = 10.0
_READ_JOIN_SECONDS = 5.0

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

_TMP_COUNTER = itertools.count(1)


# ── 结构化返回 ──────────────────────────────────────────────────────────────

def _fail(code: str, message: str, **fields: Any) -> dict:
    """失败载体：{'ok': False, 'code', 'message', ...附加字段}。"""
    payload: dict = {'ok': False, 'code': str(code), 'message': str(message)}
    payload.update(fields)
    return payload


def _guarded(method):
    """任何未预料异常都归一成 internal，对外永不抛裸异常（W5a 契约 1）。"""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        init_error = getattr(self, '_init_error', None)
        if init_error is not None:
            return dict(init_error)
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:      # noqa: BLE001 —— 契约要求不抛裸异常
            return _fail('internal', f'{type(exc).__name__}: {exc}')

    return wrapper


def _tail(text: str, limit: int = 400) -> str:
    raw = str(text or '')
    return raw if len(raw) <= limit else '…' + raw[-limit:]


def _normalize_timeout(value: Any, default: float) -> tuple:
    """(夹到 1..600 的秒数, 错误消息或 None)。bool 不算数字。"""
    if value is None:
        return float(default), None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, 'timeout 必须是数字（秒）'
    number = float(value)
    if not math.isfinite(number):
        return None, 'timeout 必须是有限数字'
    if number <= 0:
        return None, 'timeout 必须是正数'
    return min(max(number, MIN_TIMEOUT), MAX_TIMEOUT), None


# ── profile 校验 ────────────────────────────────────────────────────────────

def validate_profile(kind: str, profile: Any = None) -> dict:
    """校验并归一化 profile；成功 {'ok': True, 'kind', 'profile'}，失败 bad_profile。

    归一化后一定含全部六个键（command/args/workdir/env/timeout/protocol），
    timeout 已夹到 [1, 600]，workdir 已转绝对路径或 ''。规则见模块 docstring。
    """
    if kind not in BACKEND_KINDS:
        return _fail('bad_profile', "kind 只允许 'cli' 或 'dsh'")
    if profile is None:
        profile = {}
    if not isinstance(profile, dict):
        return _fail('bad_profile', 'profile 必须是 dict')
    unknown = sorted(str(key) for key in profile if key not in PROFILE_KEYS)
    if unknown:
        return _fail('bad_profile', 'profile 出现不认识的键：' + ','.join(unknown))

    command = profile.get('command')
    if not isinstance(command, str):
        return _fail('bad_profile', 'command 必填且必须是字符串')
    command = command.strip()
    if not command:
        return _fail('bad_profile', 'command 不能为空')
    if _CTRL.search(command):
        return _fail('bad_profile', 'command 含控制字符')
    if len(command) > MAX_COMMAND_LENGTH:
        return _fail('bad_profile', f'command 过长（>{MAX_COMMAND_LENGTH}）')

    raw_args = profile.get('args', [])
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list):
        return _fail('bad_profile', 'args 必须是 list[str]')
    if len(raw_args) > MAX_ARGS:
        return _fail('bad_profile', f'args 项数过多（>{MAX_ARGS}）')
    args = []
    for item in raw_args:
        if not isinstance(item, str):
            return _fail('bad_profile', 'args 只允许字符串元素')
        if _CTRL.search(item):
            return _fail('bad_profile', 'args 含控制字符')
        if len(item) > MAX_ARG_LENGTH:
            return _fail('bad_profile', f'args 单项过长（>{MAX_ARG_LENGTH}）')
        args.append(item)

    workdir = profile.get('workdir', '')
    if workdir is None:
        workdir = ''
    if not isinstance(workdir, str):
        return _fail('bad_profile', 'workdir 必须是字符串')
    if _CTRL.search(workdir):
        return _fail('bad_profile', 'workdir 含控制字符')
    workdir = workdir.strip()
    if workdir:
        resolved = os.path.abspath(workdir)
        if not os.path.isdir(resolved):
            return _fail('bad_profile', f'workdir 不存在或不是目录：{resolved}')
        workdir = resolved

    raw_env = profile.get('env', {})
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict):
        return _fail('bad_profile', 'env 必须是 dict')
    if len(raw_env) > MAX_ENV_ENTRIES:
        return _fail('bad_profile', f'env 项数过多（>{MAX_ENV_ENTRIES}）')
    env: dict = {}
    for key, value in raw_env.items():
        if not isinstance(key, str) or not _ENV_KEY.match(key):
            return _fail('bad_profile',
                         f'env 键名非法：{key!r}（只允许 [A-Za-z_][A-Za-z0-9_]{{0,63}}）')
        if not isinstance(value, str):
            return _fail('bad_profile', f'env[{key}] 必须是字符串')
        if _CTRL.search(value):
            return _fail('bad_profile', f'env[{key}] 含控制字符')
        if len(value) > MAX_ENV_VALUE:
            return _fail('bad_profile', f'env[{key}] 过长（>{MAX_ENV_VALUE}）')
        if key.upper() in DENIED_ENV_KEYS:
            return _fail('bad_profile',
                         f'env 不允许注入 {key}：会改写命令解析/动态库加载路径')
        env[str(key)] = str(value)

    timeout, problem = _normalize_timeout(profile.get('timeout'), DEFAULT_TIMEOUT)
    if problem is not None:
        return _fail('bad_profile', problem)

    protocol = profile.get('protocol', DEFAULT_PROTOCOL)
    if not isinstance(protocol, str) or not _PROTOCOL.match(protocol):
        return _fail('bad_profile',
                     'protocol 必须是标识符（[A-Za-z][A-Za-z0-9_.-]{0,31}）')

    normalized = {'command': command, 'args': args, 'workdir': workdir, 'env': env,
                  'timeout': float(timeout), 'protocol': protocol}
    return {'ok': True, 'kind': kind, 'profile': normalized}


# ── 子进程工具 ──────────────────────────────────────────────────────────────

class _Sink:
    """按上限保留**尾部**的字节缓冲（超限丢最早内容并置 truncated）。"""

    __slots__ = ('limit', 'total', 'truncated', '_buf')

    def __init__(self, limit: int = MAX_OUTPUT_BYTES):
        self.limit = int(limit)
        self.total = 0
        self.truncated = False
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        self._buf += chunk
        overflow = len(self._buf) - self.limit
        if overflow > 0:
            del self._buf[:overflow]
            self.truncated = True

    @property
    def dropped(self) -> int:
        return max(0, self.total - len(self._buf))

    def text(self) -> str:
        return bytes(self._buf).decode('utf-8', 'replace')


def _pump(stream, sink: _Sink) -> None:
    """读线程：一直读到底（不中断），只把尾部留在缓冲里。"""
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            sink.feed(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _feed(proc, payload: Optional[bytes]) -> None:
    """写线程：把 prompt 交给 stdin 后关闭；子进程先退出时静默失败。"""
    try:
        if payload:
            proc.stdin.write(payload)
            proc.stdin.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass


def _kill_tree(proc) -> None:
    """终止整个子进程树（不做“优雅等待”，调用方自己 bounded wait）。"""
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
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    try:
        proc.kill()
    except (OSError, ValueError):
        pass


def _wait_gone(proc, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        try:
            if proc.poll() is not None:
                return True
        except OSError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)


def _shim_args_problem(resolved: str, args) -> Optional[str]:
    """Windows 上 .cmd/.bat 垫片经 cmd.exe 解析命令行，元字符能改写命令。"""
    if os.name != 'nt' or not str(resolved).lower().endswith(_SHIM_SUFFIXES):
        return None
    for item in args:
        if _SHELL_META.search(str(item)):
            return ('目标 %s 是 cmd 垫片，参数不允许 shell 元字符（| & < > ^ %% "）：%r'
                    % (os.path.basename(str(resolved)), item))
    return None


# ── 抽象后端 ────────────────────────────────────────────────────────────────

class AgentBackend(abc.ABC):
    """一个本地外部 agent 后端的生命周期外壳（字段 + 六个对外方法）。

    状态机（照 local_agent/runtime.py 的风格）：
      idle    可启动（启动前 / 上一次 run 成功结束后）
      running 有一个 run 正在跑（同一后端同时只允许一个）
      stopped 被 stop() 或 cancel() 主动终止（可以再 start，绝不重放上一次的 prompt）
      failed  启动失败、超时或非 0 退出
    所有对外方法返回 dict 且不抛裸异常；失败带小写下划线 code。
    """

    kind = 'cli'
    PROBE_ARGS = ('--version',)

    def __init__(self, backend_id, profile: Any = None, *, data_dir=None,
                 defaults: Optional[dict] = None):
        self.id = backend_id if isinstance(backend_id, str) else ''
        self.kind = type(self).kind
        self.data_dir = ''
        self.profile: dict = {}
        self.raw_profile: dict = {}
        self.state = 'idle'
        self._init_error: Optional[dict] = None
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._cancel = threading.Event()
        self._stop_requested = False
        self._proc = None
        self._pid: Optional[int] = None
        self._run_started: Optional[float] = None
        self._runs = 0
        self._last: Optional[dict] = None

        if not isinstance(backend_id, str) or not BACKEND_ID_PATTERN.fullmatch(backend_id):
            self._init_error = _fail(
                'bad_profile', f'backendId 只允许 [A-Za-z0-9_-]{{1,64}}：{backend_id!r}')
            return
        if not isinstance(data_dir, (str, os.PathLike)) or not str(data_dir).strip():
            self._init_error = _fail('bad_profile', 'data_dir 必须是非空路径（不允许硬编码）')
            return
        self.data_dir = os.path.abspath(str(data_dir))

        merged: dict = dict(defaults or {})
        if profile is not None:
            if not isinstance(profile, dict):
                self._init_error = _fail('bad_profile', 'profile 必须是 dict')
                return
            merged.update(profile)
        self.raw_profile = dict(merged)
        result = validate_profile(self.kind, merged)
        if not result.get('ok'):
            self._init_error = result
            return
        self.profile = result['profile']

    # ── 抽象点（CliBackend 实现）──────────────────────────────────────────
    @abc.abstractmethod
    def _spawn(self, argv, *, stdin_text):
        """启动子进程（已配置管道、cwd、env、不弹窗）。"""

    @abc.abstractmethod
    def _resolve_command(self) -> Optional[str]:
        """把 profile.command 解析成可执行文件绝对路径；解析不到返回 None。"""

    @abc.abstractmethod
    def _build_argv(self, resolved: str, extra_args=()) -> list:
        """resolved + profile.args（+ extra_args）的最终 argv。"""

    # ── 只读信息 ──────────────────────────────────────────────────────────
    @property
    def timeout(self) -> float:
        return float(self.profile.get('timeout') or DEFAULT_TIMEOUT)

    def __repr__(self) -> str:      # pragma: no cover —— 便于调试输出
        return f'<{type(self).__name__} id={self.id!r} state={self.state}>'

    def _workspace_path(self):
        try:
            return agent_workspace.root(self.data_dir, self.id)
        except Exception:           # noqa: BLE001 —— 只读信息，不能因此崩
            return None

    @_guarded
    def status(self) -> dict:
        """真实状态：running 以子进程是否真的活着为准；不创建任何目录。"""
        proc = self._proc
        alive = False
        if proc is not None:
            try:
                alive = proc.poll() is None
            except OSError:
                alive = False
        workspace_path = self._workspace_path()
        return {'ok': True, 'id': self.id, 'kind': self.kind,
                'state': 'running' if alive else self.state,
                'running': alive, 'pid': self._pid if alive else None,
                'command': self.profile.get('command', ''),
                'timeout': self.timeout, 'runs': self._runs,
                'started_at': self._run_started if alive else None,
                'duration': (round(time.time() - self._run_started, 3)
                             if alive and self._run_started else 0.0),
                'workspace': str(workspace_path) if workspace_path else '',
                'last': dict(self._last) if self._last else None}

    @_guarded
    def workspace(self) -> dict:
        """确保 <data_dir>/agent_workspace/<backendId>/ 存在并返回它（越界写仍由
        agent_workspace 拦）。只在这里建目录，构造与 import 期都不建。"""
        try:
            path = agent_workspace.root(self.data_dir, self.id)
        except agent_workspace.WorkspaceError as exc:
            return _fail('bad_profile', f'{exc.code}: {exc.message}')
        created = False
        try:
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                created = True
        except OSError as exc:
            return _fail('io', f'创建工作区目录失败：{type(exc).__name__}: {exc}')
        return {'ok': True, 'backend': self.id, 'path': str(path),
                'exists': path.is_dir(), 'created': created}

    # ── 探测 ──────────────────────────────────────────────────────────────
    @_guarded
    def probe(self) -> dict:
        """干跑：命令解析得到 + 真能起进程 + 版本可取。

        - 解析不到 → {'ok': False, 'code': 'command_not_found'}；
        - 起不来（OSError）→ io；
        - 起来了但版本调用超时/非 0 → 仍然 ok=True（runnable=True），
          用 version_ok / timed_out / returncode 如实反映。
        """
        resolved = self._resolve_command()
        if resolved is None:
            return _fail('command_not_found',
                         f"命令不可执行：{self.profile.get('command')!r}（PATH 与 workdir 均未命中）")
        try:
            extra = list(self.PROBE_ARGS)
            argv = self._probe_argv(resolved, extra)
            # 形状校验仍看完整 argv：profile.args 里带 shell 元字符的 .cmd/.bat
            # 装上就跑不起来，干跑必须提前挡掉。
            full_argv = self._build_argv(resolved, extra)
        except Exception as exc:    # noqa: BLE001
            return _fail('bad_profile', f'构造 argv 失败：{type(exc).__name__}: {exc}')
        problem = _shim_args_problem(full_argv[0] if full_argv else resolved, full_argv[1:])
        if problem:
            return _fail('bad_profile', problem)
        run = self._collect(argv, timeout=min(self.timeout, PROBE_TIMEOUT),
                            stdin_text=None, track=False)
        if not run.get('ok') and run.get('code') in ('command_not_found', 'io'):
            return run
        return {'ok': True, 'id': self.id, 'kind': self.kind,
                'command': self.profile.get('command', ''), 'resolved': resolved,
                'runnable': run.get('pid') is not None,
                'version_ok': bool(run.get('ok')),
                'returncode': run.get('exit_code'),
                'timed_out': bool(run.get('timed_out')),
                'stdout': run.get('stdout', ''), 'stderr': run.get('stderr', ''),
                'stdout_truncated': run.get('stdout_truncated', False),
                'duration': run.get('duration', 0.0), 'argv': run.get('argv', [])}

    # ── 运行 ──────────────────────────────────────────────────────────────
    @_guarded
    def start(self, prompt: Any = '', *, timeout: Any = None) -> dict:
        """跑一次（阻塞到结束/取消/超时）。同一后端并发第二次 → already_running。"""
        if prompt is not None and not isinstance(prompt, str):
            return _fail('bad_profile', 'prompt 必须是字符串（或 None 表示不写 stdin）')
        if timeout is not None:
            effective, problem = _normalize_timeout(timeout, self.timeout)
            if problem is not None:
                return _fail('bad_profile', problem)
        else:
            effective = self.timeout
        if not self._run_lock.acquire(blocking=False):
            return _fail('already_running',
                         f'后端 {self.id} 已有一次 run 在跑（同一后端同时只允许一个）')
        try:
            if _live_entry(self.id) is not None and _live_entry(self.id) is not self:
                return _fail('duplicate_id',
                             f'backendId {self.id} 已有另一个后端在运行')
            resolved = self._resolve_command()
            if resolved is None:
                with self._lock:
                    self.state = 'failed'
                return _fail('command_not_found',
                             f"命令不可执行：{self.profile.get('command')!r}")
            uses_placeholder = self._uses_prompt_placeholder()
            if uses_placeholder and prompt is not None and len(str(prompt)) > MAX_ARG_LENGTH:
                # argv 传 prompt 时不可能截断：超长直接拒绝（bad_profile）。
                return _fail('bad_profile',
                             f'prompt 有 {len(str(prompt))} 个字符，超过参数上限 '
                             f'{MAX_ARG_LENGTH}（profile 用了 {PROMPT_PLACEHOLDER} 占位）')
            try:
                argv = self._build_argv(resolved, (), prompt=prompt)
            except Exception as exc:    # noqa: BLE001
                return _fail('bad_profile', f'构造 argv 失败：{type(exc).__name__}: {exc}')
            problem = _shim_args_problem(argv[0] if argv else resolved, argv[1:])
            if problem:
                return _fail('bad_profile', problem)
            self._stop_requested = False
            self._cancel.clear()
            # 占位已把 prompt 放进 argv：这次 run 不能再写 stdin（否则重复投递）。
            stdin_text = None if (prompt is None or uses_placeholder) else str(prompt)
            result = self._collect(argv, timeout=effective,
                                   stdin_text=stdin_text, track=True)
            result = dict(result)
            result.update({'id': self.id, 'kind': self.kind, 'command': resolved,
                           'timeout': effective, 'state': self.state,
                           'prompt_in_argv': bool(uses_placeholder)})
            return result
        finally:
            self._run_lock.release()

    # ── 取消 / 停止 ────────────────────────────────────────────────────────
    @_guarded
    def cancel(self) -> dict:
        """只请求取消：terminate 直接子进程，并对进程树做一次收尾（垫片会留孤儿）。

        不重置状态机、不清 profile：状态由 start() 那一侧的等待循环按真实退出落定。
        """
        with self._lock:
            proc = self._proc
            if proc is None:
                return _fail('not_running', f'后端 {self.id} 当前没有正在运行的任务')
            try:
                alive = proc.poll() is None
            except OSError:
                alive = False
            if not alive:
                return _fail('not_running', f'后端 {self.id} 的任务已自行结束')
            self._cancel.set()
            pid = int(getattr(proc, 'pid', 0) or 0)
        try:
            proc.terminate()
        except (OSError, ValueError):
            pass
        _kill_tree(proc)                    # 树收尾：cmd.exe 垫片不能留 node 孤儿
        _wait_gone(proc, _KILL_WAIT_SECONDS)
        return {'ok': True, 'id': self.id, 'cancelled': True, 'pid': pid}

    @_guarded
    def stop(self) -> dict:
        """终止整个子进程树并落定 stopped；幂等、可重复调用。"""
        with self._lock:
            proc = self._proc
            if proc is None:
                return {'ok': True, 'id': self.id, 'stopped': False,
                        'already_stopped': True, 'state': self.state}
            try:
                alive = proc.poll() is None
            except OSError:
                alive = False
            pid = int(getattr(proc, 'pid', 0) or 0)
            if not alive:
                self.state = 'stopped'
                return {'ok': True, 'id': self.id, 'stopped': False,
                        'already_stopped': True, 'pid': pid, 'state': self.state}
            self._stop_requested = True
            self._cancel.set()
            _kill_tree(proc)
            _wait_gone(proc, _KILL_WAIT_SECONDS)
            exit_code = None
            try:
                exit_code = proc.poll()
            except OSError:
                exit_code = None
            self.state = 'stopped'
        _unregister_live(self)
        return {'ok': True, 'id': self.id, 'stopped': True, 'already_stopped': False,
                'pid': pid, 'exit_code': exit_code, 'state': 'stopped'}

    # ── 单次采集（probe 与 start 共用同一条执行路径）───────────────────────
    def _collect(self, argv, *, timeout: float, stdin_text, track: bool) -> dict:
        started = time.monotonic()
        try:
            proc = self._spawn(argv, stdin_text=stdin_text)
        except FileNotFoundError:
            return _fail('command_not_found', f'可执行文件不存在：{argv[0]!r}',
                         argv=list(argv))
        except OSError as exc:
            return _fail('io', f'启动子进程失败：{type(exc).__name__}: {exc}',
                         argv=list(argv))

        out, err = _Sink(MAX_OUTPUT_BYTES), _Sink(MAX_OUTPUT_BYTES)
        readers = [threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
                   threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True)]
        for thread in readers:
            thread.start()
        writer = None
        if stdin_text is not None:
            payload = str(stdin_text).encode('utf-8')
            writer = threading.Thread(target=_feed, args=(proc, payload), daemon=True)
            writer.start()

        if track:
            with self._lock:
                self._proc = proc
                self._pid = int(getattr(proc, 'pid', 0) or 0)
                self._run_started = time.time()
                self._runs += 1
                self.state = 'running'
                _register_live(self)

        timed_out = False
        deadline = started + float(timeout)
        try:
            while True:
                try:
                    if proc.poll() is not None:
                        break
                except OSError:
                    break
                if track and self._cancel.is_set():
                    _kill_tree(proc)
                    _wait_gone(proc, _KILL_WAIT_SECONDS)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _kill_tree(proc)
                    _wait_gone(proc, _KILL_WAIT_SECONDS)
                    break
                time.sleep(_POLL_SECONDS)
            exit_code = proc.poll()
            if exit_code is None:
                _kill_tree(proc)
                _wait_gone(proc, _KILL_WAIT_SECONDS)
                exit_code = proc.poll()
        finally:
            for thread in readers:
                thread.join(timeout=_READ_JOIN_SECONDS)
            if writer is not None:
                writer.join(timeout=_READ_JOIN_SECONDS)

        duration = round(time.monotonic() - started, 3)
        base = {'pid': int(getattr(proc, 'pid', 0) or 0), 'argv': list(argv),
                'exit_code': exit_code, 'duration': duration,
                'stdout': out.text(), 'stderr': err.text(),
                'stdout_truncated': out.truncated, 'stderr_truncated': err.truncated,
                'stdout_bytes': out.total, 'stderr_bytes': err.total,
                'stdout_dropped': out.dropped, 'stderr_dropped': err.dropped,
                'timed_out': timed_out}
        if timed_out:
            result = _fail('timeout', f'超过 {timeout:g}s 未结束，进程树已终止', **base)
        elif track and self._stop_requested:
            result = _fail('stopped', '被 stop() 主动终止，状态以真实退出为准', **base)
        elif track and self._cancel.is_set():
            result = _fail('cancelled', '被 cancel() 请求取消后结束', **base)
        elif exit_code is None:
            result = _fail('io', '拿不到退出码（子进程状态不可读）', **base)
        elif int(exit_code) != 0:
            result = _fail('nonzero_exit',
                           f'退出码 {exit_code}：' + _tail(err.text() or out.text()),
                           **base)
        else:
            result = {'ok': True, **base}

        if track:
            self._finish_run(result, started)
        return result

    def _finish_run(self, result: dict, started: float) -> None:
        """落定状态、写 last（供 status() 读）并把自己从 live 里摘掉。"""
        with self._lock:
            # stop() 与 cancel() 都是“我们主动收掉”，落定 stopped 而不是 failed
            stop_requested = (bool(self._stop_requested) or self._cancel.is_set()
                              or self.state == 'stopped')
            if stop_requested:
                state = 'stopped'
            elif result.get('ok'):
                state = 'idle'
            else:
                state = 'failed'
            self.state = state
            self._last = {'ok': bool(result.get('ok')),
                          'code': result.get('code'),
                          'message': result.get('message'),
                          'exit_code': result.get('exit_code'),
                          'pid': result.get('pid'),
                          'duration': result.get('duration'),
                          'timed_out': bool(result.get('timed_out')),
                          'stdout_truncated': bool(result.get('stdout_truncated')),
                          'stderr_truncated': bool(result.get('stderr_truncated')),
                          'finished_at': time.time()}
            self._proc = None
            self._pid = None
            self._run_started = None
            self._stop_requested = False
            self._cancel.clear()
        _unregister_live(self)


# ── 活进程注册表 ────────────────────────────────────────────────────────────

_LIVE: dict = {}
_LIVE_LOCK = threading.RLock()


def _register_live(backend: 'AgentBackend') -> None:
    with _LIVE_LOCK:
        _LIVE[backend.id] = backend


def _unregister_live(backend: 'AgentBackend') -> None:
    with _LIVE_LOCK:
        if _LIVE.get(backend.id) is backend:
            _LIVE.pop(backend.id, None)


def _live_entry(backend_id: str) -> Optional['AgentBackend']:
    with _LIVE_LOCK:
        backend = _LIVE.get(backend_id)
        if backend is None:
            return None
        proc = backend._proc
        if proc is None:
            _LIVE.pop(backend_id, None)
            return None
        try:
            alive = proc.poll() is None
        except OSError:
            alive = False
        if not alive:
            _LIVE.pop(backend_id, None)
            return None
        return backend


def live_backends() -> list:
    """仍在跑的后端列表（进程退出后自动清除，不需要调用方收拾）。"""
    with _LIVE_LOCK:
        rows = []
        for backend_id in list(_LIVE):
            backend = _live_entry(backend_id)
            if backend is None:
                continue
            rows.append({'id': backend.id, 'kind': backend.kind,
                         'pid': backend._pid,
                         'started_at': backend._run_started,
                         'duration': (round(time.time() - backend._run_started, 3)
                                      if backend._run_started else 0.0)})
        return rows


# ── CLI 后端 ────────────────────────────────────────────────────────────────

class CliBackend(AgentBackend):
    """任意 CLI：profile 描述命令/参数/工作目录/环境/超时/协议名，subprocess 执行。"""

    kind = 'cli'

    def _resolve_command(self) -> Optional[str]:
        command = str(self.profile.get('command') or '')
        if not command:
            return None
        looks_path = (os.path.isabs(command) or '/' in command or '\\' in command)
        if looks_path:
            candidates = [command]
            if os.name == 'nt' and not os.path.splitext(command)[1]:
                candidates += [command + ext for ext in ('.cmd', '.exe', '.bat', '.ps1')]
            for candidate in candidates:
                if os.path.isfile(candidate):
                    return os.path.abspath(candidate)
            return None
        found = shutil.which(command)
        return os.path.abspath(found) if found else None

    def _probe_argv(self, resolved: str, extra_args=()) -> list:
        """探测/取版本专用 argv：命令本身 + 探测参数，不带 profile.args 里的任务参数。"""
        return self._build_argv(resolved, extra_args, base_args=())

    def _uses_prompt_placeholder(self) -> bool:
        """profile.args 里是否有 {prompt}：有就走 argv 传 prompt、不写 stdin。"""
        return any(PROMPT_PLACEHOLDER in str(item)
                   for item in (self.profile.get('args') or []))

    def _build_argv(self, resolved: str, extra_args=(), prompt: Any = None,
                    base_args=None) -> list:
        """resolved +（profile.args 或 base_args）+ extra_args 的最终 argv。

        base_args=None 用 profile.args（正常起进程）；传空序列则丢掉 profile.args——
        探测/取版本只跑命令本身，不能让 profile.args 里的任务参数混进 argv
        （例如 dsh --profile headless "{prompt}" --version 会被 dsh 当成未知选项）。
        """
        base = list(self.profile.get('args') or []) if base_args is None \
            else [str(item) for item in base_args]
        args = base + [str(item) for item in extra_args]
        if prompt is not None:
            text = str(prompt)
            args = [item.replace(PROMPT_PLACEHOLDER, text) for item in args]
        if os.name == 'nt' and str(resolved).lower().endswith(_PS_SUFFIXES):
            shell = shutil.which('pwsh') or shutil.which('powershell') or 'powershell.exe'
            return [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                    '-File', resolved] + args
        return [resolved] + args

    def _spawn(self, argv, *, stdin_text):
        env = os.environ.copy()
        env.update(self.profile.get('env') or {})
        kwargs: dict = {
            'stdin': subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'bufsize': 0,
            'cwd': self.profile.get('workdir') or None,
            'env': env,
        }
        if os.name == 'nt':
            kwargs['creationflags'] = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        else:
            kwargs['start_new_session'] = True
        return subprocess.Popen(argv, **kwargs)


# ── DSH 后端 ────────────────────────────────────────────────────────────────

class DshBackend(CliBackend):
    """DSH 的 profile 预置（command 默认 'dsh'，任何字段都可用 profile 覆盖）。

    本类只负责“把 dsh 当 CLI 调起来”：干跑验证、真跑一次 --version、找不到时
    返回 command_not_found。不实现 DSH 协议对话、不连 MCP、不读 cordis.patch.yml。
    """

    kind = 'dsh'
    PROBE_ARGS = ('--version',)

    def __init__(self, backend_id, profile: Any = None, *, data_dir=None):
        super().__init__(backend_id, profile, data_dir=data_dir, defaults=DSH_PROFILE)

    @_guarded
    def version(self) -> dict:
        """真跑一次 dsh --version（快速返回、无副作用），返回完整的结构化结果。"""
        resolved = self._resolve_command()
        if resolved is None:
            return _fail('command_not_found',
                         f"命令不可执行：{self.profile.get('command')!r}")
        argv = self._probe_argv(resolved, ['--version'])
        result = dict(self._collect(argv, timeout=min(self.timeout, PROBE_TIMEOUT),
                                    stdin_text=None, track=False))
        result.update({'id': self.id, 'kind': self.kind, 'resolved': resolved})
        return result


# ── 工厂与 profile 存取 ─────────────────────────────────────────────────────

def make_backend(backend_id, kind: str = 'cli', profile: Any = None, *,
                 data_dir=None) -> dict:
    """按 kind 造后端；同一 backendId 已有后端在跑时返回 duplicate_id。

    成功返回 {'ok': True, 'backend': AgentBackend, 'id', 'kind', 'profile'}。
    """
    if kind not in BACKEND_KINDS:
        return _fail('bad_profile', "kind 只允许 'cli' 或 'dsh'")
    if isinstance(backend_id, str) and _live_entry(backend_id) is not None:
        return _fail('duplicate_id',
                     f'backendId {backend_id} 已有一个后端在运行（一个 id 同时只允许一个活进程）')
    factory = DshBackend if kind == 'dsh' else CliBackend
    backend = factory(backend_id, profile, data_dir=data_dir)
    if backend._init_error is not None:
        return dict(backend._init_error)
    return {'ok': True, 'backend': backend, 'id': backend.id, 'kind': backend.kind,
            'profile': dict(backend.profile)}


def load_profiles(path) -> dict:
    """读 profile 文件（JSON，schema={'version':1,'backends':[{id,kind,profile}]}）。

    文件不存在 → {'ok': True, 'profiles': {}, 'exists': False}；
    坏文件（非 UTF-8 / 非 JSON / 结构不对 / id 重复 / profile 校验失败）
    → {'ok': False, 'code': 'bad_profile_file', 'message', 'profiles': {}}。
    """
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        return _bad_file('path 必须是非空路径')
    target = os.path.abspath(str(path))
    if not os.path.exists(target):
        return {'ok': True, 'profiles': {}, 'path': target, 'exists': False, 'count': 0}
    try:
        with open(target, 'rb') as handle:
            blob = handle.read(MAX_PROFILE_FILE_BYTES + 1)
    except OSError as exc:
        return _bad_file(f'读取失败：{type(exc).__name__}: {exc}')
    if len(blob) > MAX_PROFILE_FILE_BYTES:
        return _bad_file(f'文件过大（>{MAX_PROFILE_FILE_BYTES} 字节）')
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError:
        return _bad_file('不是合法 UTF-8 文本')
    try:
        document = json.loads(text) if text.strip() else None
    except ValueError as exc:
        return _bad_file(f'不是合法 JSON：{exc}')

    if isinstance(document, dict):
        version = document.get('version', SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) \
                or version != SCHEMA_VERSION:
            return _bad_file(f'不支持的 schema 版本：{version!r}')
        if 'backends' not in document:
            return _bad_file("顶层缺少 'backends' 数组")
        entries = document.get('backends')
    elif isinstance(document, list):
        entries = document
    else:
        return _bad_file('顶层必须是对象或数组')
    if not isinstance(entries, list):
        return _bad_file("'backends' 必须是数组")

    profiles: dict = {}
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            return _bad_file(f'第 {index} 条不是对象')
        backend_id = entry.get('id')
        if not isinstance(backend_id, str) or not BACKEND_ID_PATTERN.fullmatch(backend_id):
            return _bad_file(f'第 {index} 条 id 非法：{backend_id!r}')
        if backend_id in profiles:
            return _bad_file(f'第 {index} 条 id 重复：{backend_id}')
        kind = entry.get('kind', 'cli')
        result = validate_profile(kind, entry.get('profile', {}))
        if not result.get('ok'):
            return _bad_file(f"第 {index} 条 profile 校验失败：{result.get('message')}")
        profiles[backend_id] = {'kind': kind, 'profile': result['profile']}
    return {'ok': True, 'profiles': profiles, 'path': target, 'exists': True,
            'count': len(profiles)}


def save_profiles(path, profiles) -> dict:
    """原子写 profile 文件（同目录 tmp + fsync + os.replace，不留半截文件）。

    profiles: {'<id>': {'kind','profile'}} 或 {'<id>': <profile dict（可含 kind）>}。
    """
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        return _fail('bad_profile', 'path 必须是非空路径')
    if not isinstance(profiles, dict):
        return _fail('bad_profile', 'profiles 必须是 id → profile 的字典')
    normalized: dict = {}
    for backend_id, value in profiles.items():
        if not isinstance(backend_id, str) or not BACKEND_ID_PATTERN.fullmatch(backend_id):
            return _fail('bad_profile', f'backendId 非法：{backend_id!r}')
        if isinstance(value, dict) and 'profile' in value:
            kind = value.get('kind', 'cli')
            payload = value.get('profile', {})
        elif isinstance(value, dict):
            payload = dict(value)
            kind = payload.pop('kind', 'cli')
        else:
            return _fail('bad_profile', f'id {backend_id} 的 profile 必须是 dict')
        result = validate_profile(kind, payload)
        if not result.get('ok'):
            return _fail('bad_profile',
                         f"id {backend_id}: {result.get('message')}")
        normalized[backend_id] = {'kind': kind, 'profile': result['profile']}

    document = {'version': SCHEMA_VERSION,
                'backends': [{'id': key, **value}
                             for key, value in normalized.items()]}
    target = os.path.abspath(str(path))
    tmp = ''
    try:
        os.makedirs(os.path.dirname(target) or '.', exist_ok=True)
        tmp = f'{target}.tmp-{os.getpid()}-{next(_TMP_COUNTER)}'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        tmp = ''
    except OSError as exc:
        return _fail('io', f'写入 profile 文件失败：{type(exc).__name__}: {exc}')
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    try:
        size = os.path.getsize(target)
    except OSError:
        size = 0
    return {'ok': True, 'path': target, 'count': len(normalized), 'bytes': size}


def _bad_file(message: str) -> dict:
    return _fail('bad_profile_file', message, profiles={})
