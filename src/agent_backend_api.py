"""W5b：外部 agent 后端的 HTTP 端点（前缀 /api/agent/backends）。

这一层只做“接线”，能力全部来自 W5a 的 agent_backends / agent_workspace：
profile 落盘（save_profiles 原子写）、探测、后台启动、停止、状态、工作区浏览。

安全边界（与宿主 qq_app.py:3092 的 local_admin 中间件同等或更严）：
- 客户端必须是回环地址（127.0.0.1 / ::1 / testclient / testserver）；
- 带 forwarded / x-forwarded-for / x-real-ip 的请求一律拒绝：代理转发不能代表本机；
- Origin 若存在必须与 Host 同源，Host 必须是本机名。
中间件已经拦过一遍，这里再显式拦一次：单独装配本 router（测试或嵌入式）时也不裸奔。

返回约定：JSON 体 {'ok', 'code', 'message', ...}；失败时错误码原样透出，
HTTP 状态码按 _STATUS 映射（鉴权 403、未知 id 404、已运行/重复 id 409、其余 400/500）。
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import agent_detect, agent_workspace, dsh_memory_bridge
from .agent_backends import (BACKEND_ID_PATTERN, BACKEND_KINDS, live_backends,
                             load_profiles, make_backend, save_profiles,
                             validate_profile)

PROFILE_FILENAME = 'agent_backends.json'
PREFIX = '/api/agent/backends'

_LOOPBACK_HOSTS = frozenset({'127.0.0.1', '::1', 'testclient', 'testserver'})
_LOCAL_HOSTNAMES = frozenset({'127.0.0.1', 'localhost', '::1', 'testserver'})
_FORWARD_HEADERS = ('forwarded', 'x-forwarded-for', 'x-real-ip')

_STATUS = {
    'bad_profile': 400, 'bad_profile_file': 400, 'duplicate_id': 409,
    'already_running': 409, 'not_running': 409, 'nonzero_exit': 400,
    'timeout': 504, 'cancelled': 409, 'stopped': 409,
    'command_not_found': 400, 'not_found': 404, 'io': 500, 'internal': 500,
    'no_data_dir': 503, 'auth_denied': 403,
}

# 模块级实例表：端点造出来的后端都登记在这里，进程退出 / 面板关闭时统一 stop。
_REGISTRY: dict = {}
_REGISTRY_LOCK = threading.RLock()
# 正在跑 start() 的后台线程（start 是阻塞的，端点不能等它）。
_INFLIGHT: dict = {}
_LAST_RUN: dict = {}
_SHUT_DOWN = False


class _Denied(Exception):
    """鉴权失败：由 exception_handler 落成 403 + auth_denied。"""


# ── 鉴权 ────────────────────────────────────────────────────────────────────

def _require_local(request: Request) -> None:
    client = getattr(request, 'client', None)
    host = str(getattr(client, 'host', '') or '')
    if host not in _LOOPBACK_HOSTS:
        raise _Denied('外部后端管理只允许本机访问（来源 %s）' % (host or 'unknown'))
    headers = request.headers
    for name in _FORWARD_HEADERS:
        if name in headers:
            raise _Denied('拒绝经由代理转发的请求（检测到 %s 头）' % name)
    origin = headers.get('origin')
    if origin and urlsplit(origin).netloc != (headers.get('host') or ''):
        raise _Denied('Origin 与 Host 不同源，拒绝')
    hostname = str(getattr(request.url, 'hostname', '') or '')
    if hostname not in _LOCAL_HOSTNAMES:
        raise _Denied('Host 必须是本机地址（收到 %s）' % (hostname or 'unknown'))


# ── 通用小工具 ──────────────────────────────────────────────────────────────

def _fail(code: str, message: str, **fields: Any) -> dict:
    payload: dict = {'ok': False, 'code': str(code), 'message': str(message)}
    payload.update(fields)
    return payload


def _respond(result: Any, *, ok_status: int = 200) -> JSONResponse:
    payload = dict(result) if isinstance(result, dict) else _fail('internal', '结果不是字典')
    if payload.get('ok'):
        return JSONResponse(payload, status_code=ok_status)
    code = str(payload.get('code') or 'internal')
    payload['code'] = code
    payload.setdefault('message', '')
    return JSONResponse(payload, status_code=_STATUS.get(code, 400))


def profile_path(data_dir) -> str:
    """profile 文件位置：<data_dir>/agent_backends.json（不写 config.yaml）。"""
    if data_dir is None or not str(data_dir).strip():
        return ''
    return os.path.join(os.path.abspath(str(data_dir)), PROFILE_FILENAME)


def _data_dir(app) -> tuple:
    """(数据目录, 是否就绪)。没有 bot / bot.root 时没地方放 profile 文件。"""
    bot = getattr(app.state, 'bot', None)
    root = getattr(bot, 'root', None) if bot is not None else None
    if root is None or not str(root).strip():
        return '', False
    try:
        return os.path.abspath(str(root)), True
    except (OSError, ValueError):
        return '', False


def _profiles(data_dir: str) -> tuple:
    """(profiles, 错误载体或 None)。读不到就空表。"""
    if not data_dir:
        return {}, None
    loaded = load_profiles(profile_path(data_dir))
    if not loaded.get('ok'):
        return {}, loaded
    return dict(loaded.get('profiles') or {}), None


def _live_ids() -> dict:
    return {str(row.get('id')): row for row in live_backends() if row.get('id')}


def _thread_alive(backend_id: str) -> bool:
    with _REGISTRY_LOCK:
        thread = _INFLIGHT.get(backend_id)
        if thread is None:
            return False
        if thread.is_alive():
            return True
        _INFLIGHT.pop(backend_id, None)
        return False


def _cached(backend_id: str, data_dir: str):
    with _REGISTRY_LOCK:
        backend = _REGISTRY.get(backend_id)
    if backend is not None and getattr(backend, 'data_dir', '') == data_dir:
        return backend
    return None


def _forget(backend_id: str, *, stop: bool = False) -> None:
    with _REGISTRY_LOCK:
        backend = _REGISTRY.pop(backend_id, None)
    if backend is not None and stop:
        try:
            backend.stop()
        except Exception:       # noqa: BLE001 — 收尾不能把端点带崩
            pass


def _instance(app, backend_id) -> tuple:
    """按 id 取（或造）后端实例；拿不到时返回 (None, 失败载体)。"""
    data_dir, ready = _data_dir(app)
    if not ready:
        return None, _fail('no_data_dir',
                           '运行数据目录不可用：后端实例必须带 data_dir 才能 probe/version')
    if not isinstance(backend_id, str) or not BACKEND_ID_PATTERN.fullmatch(backend_id):
        return None, _fail('bad_profile',
                           'backendId 只允许 [A-Za-z0-9_-]{1,64}：%r' % (backend_id,))
    cached = _cached(backend_id, data_dir)
    if cached is not None:
        return cached, None
    profiles, problem = _profiles(data_dir)
    if problem is not None:
        return None, problem
    entry = profiles.get(backend_id)
    if not isinstance(entry, dict):
        return None, _fail('not_found', '没有 id 为 %s 的后端 profile' % backend_id)
    made = make_backend(backend_id, str(entry.get('kind') or 'cli'),
                        entry.get('profile') or {}, data_dir=data_dir)
    if not made.get('ok'):
        return None, made
    backend = made['backend']
    with _REGISTRY_LOCK:
        _REGISTRY[backend_id] = backend
    return backend, None


def _workspace_path(data_dir: str, backend_id: str) -> str:
    if not data_dir:
        return ''
    try:
        return str(agent_workspace.root(data_dir, backend_id))
    except Exception:           # noqa: BLE001 — 只读展示信息，不能因此崩
        return ''


def _row(data_dir: str, ready: bool, backend_id: str, entry: dict) -> dict:
    """列表里的一行：profile + 真实状态 + 工作区路径 + data_dir 就绪标记。"""
    profile = dict(entry.get('profile') or {})
    kind = str(entry.get('kind') or 'cli')
    row = {'id': backend_id, 'kind': kind, 'profile': profile,
           'command': str(profile.get('command') or ''),
           'args': list(profile.get('args') or []),
           'workdir': str(profile.get('workdir') or ''),
           'timeout': profile.get('timeout'),
           'workspace': _workspace_path(data_dir, backend_id),
           'data_dir_ready': bool(ready),
           'state': 'idle', 'pid': None, 'running': False, 'last': None}
    backend = _cached(backend_id, data_dir) if ready else None
    if backend is not None:
        status = backend.status()
        if status.get('ok'):
            for key in ('state', 'pid', 'running', 'last', 'runs', 'started_at'):
                row[key] = status.get(key)
    live = _live_ids().get(backend_id)
    if live is not None:
        row['running'] = True
        row['state'] = 'running'
        row['pid'] = live.get('pid')
    if _thread_alive(backend_id) and row.get('state') in ('idle', 'failed', 'stopped'):
        row['state'] = 'starting'
    return row


# ── 启动：阻塞的 start 放进后台线程 ─────────────────────────────────────────

def _run_background(backend, prompt, timeout) -> None:
    try:
        result = backend.start(prompt, timeout=timeout)
        with _REGISTRY_LOCK:
            _LAST_RUN[backend.id] = dict(result)
    except Exception as exc:    # noqa: BLE001 — 线程里的异常不能静默消失
        with _REGISTRY_LOCK:
            _LAST_RUN[backend.id] = _fail('internal', '%s: %s' % (type(exc).__name__, exc))
    finally:
        with _REGISTRY_LOCK:
            if _INFLIGHT.get(backend.id) is threading.current_thread():
                _INFLIGHT.pop(backend.id, None)


def start_background(backend, *, prompt='', timeout=None) -> dict:
    """起后台线程跑 backend.start()，立刻返回；已在跑 → already_running。"""
    if prompt is not None and not isinstance(prompt, str):
        return _fail('bad_profile', 'prompt 必须是字符串（null 表示不写 stdin）')
    backend_id = backend.id
    with _REGISTRY_LOCK:
        if _thread_alive(backend_id):
            return _fail('already_running', '后端 %s 已有一次 run 在跑' % backend_id)
        if backend_id in _live_ids():
            return _fail('already_running', '后端 %s 的进程还活着' % backend_id)
        thread = threading.Thread(target=_run_background, args=(backend, prompt, timeout),
                                  name='agent-backend-%s' % backend_id, daemon=True)
        _INFLIGHT[backend_id] = thread
        thread.start()
    return {'ok': True, 'id': backend_id, 'kind': backend.kind, 'state': 'starting',
            'started': True, 'blocking': False,
            'poll': '%s/%s/status' % (PREFIX, backend_id)}


def shutdown_all() -> dict:
    """停掉所有登记过的后端进程树（进程退出 / 控制台关闭时调用）。"""
    global _SHUT_DOWN
    with _REGISTRY_LOCK:
        _SHUT_DOWN = True
        backends = list(_REGISTRY.items())
    rows = []
    for backend_id, backend in backends:
        if backend is None:
            continue
        try:
            result = backend.stop()
        except Exception as exc:    # noqa: BLE001
            result = _fail('internal', '%s: %s' % (type(exc).__name__, exc))
        rows.append({'id': backend_id, 'ok': bool(result.get('ok')),
                     'stopped': bool(result.get('stopped')), 'pid': result.get('pid')})
    with _REGISTRY_LOCK:
        threads = list(_INFLIGHT.values())
    for thread in threads:
        if thread.is_alive():
            thread.join(timeout=5.0)
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        _INFLIGHT.clear()
    return {'ok': True, 'count': len(rows), 'stopped': rows}


def _reset_for_tests() -> None:
    """测试用：清空登记表（不 stop，测试自己负责停）。"""
    global _SHUT_DOWN
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        _INFLIGHT.clear()
        _LAST_RUN.clear()
        _SHUT_DOWN = False


atexit.register(lambda: None if _SHUT_DOWN else shutdown_all())


# ── 请求体 ──────────────────────────────────────────────────────────────────

class ProfileBody(BaseModel):
    id: str = ''
    kind: str = 'cli'
    profile: Any = None
    create_only: bool = False


class StartBody(BaseModel):
    prompt: Optional[str] = None
    timeout: Optional[float] = None


class DshBody(BaseModel):
    profile: str = ''
    # 源码仓库根：安装版没有仓库源码，靠这个字段指过去（填过一次就记进数据目录）。
    repo_root: str = ''


# ── DSH 联动（等价于 tools/setup_dsh_memory.py，可撤销）─────────────────────
#
# 三个 MCP 桥（记忆 / 控制 / 工具）写进 DSH 的 profile patch 文件，DSH 的模型才看得见
# mcp__whale_memory__* 之类工具。写文件交给 dsh_memory_bridge（备份、原子替换、幂等、
# insert 块语法都在那边），端点只做三件事：定参数、把写后的真实情况读回来、给出一条
# 和这次写入完全等价的命令行。

DSH_DEFAULT_PROFILE = 'desktop'
DSH_PROFILE_PATTERN = re.compile(r'[A-Za-z0-9_.\-]{1,64}\Z')
DSH_SCRIPT_FILES = {'memory': 'whale_memory_mcp.py',
                    'bot': 'whale_bot_mcp.py',
                    'tools': 'whale_tools_mcp.py'}
DSH_LINK_HINT = ('写入完成。DSH 只在「会话创建」时拉起一次 MCP client，所以要新开一个 '
                 'DSH 会话（或重启 DSH）才看得到 mcp__whale_memory__* / '
                 'mcp__whale_bot__* / mcp__whale_tools__* 三组工具。控制台以后每次启动'
                 '都会自动检查并在需要时重写这份登记；接好后 DSH 里新建的任何工作区/会话'
                 '都会直接带上这三组工具。登记里带了约 8 小时的重连预算，控制台晚启动或'
                 '中途重启也会自己连回来，但已经开着的旧会话仍需重开才加载。')
DSH_UNLINK_HINT = ('撤销完成。同样要新开一个 DSH 会话（或重启 DSH）才生效：已经在跑的'
                   '会话里那三组工具会保留到该会话结束。控制台以后不再自动接入；在面板里'
                   '再点一次「让 DSH 接上控制台」会重新打开自动接入。')


DSH_REPO_ENV = 'WHALE_QQ_REPO'
DSH_REPO_CONFIG = 'dsh_link_config.json'
DSH_VENV_DIRS = ('.venv-build', '.venv-qq', '.venv')
DSH_SOURCE_HINT = (
    '找不到源码仓库：三个 MCP 桥要 import src.*，安装版（冻结产物）里没有这些 Python '
    '模块，所以必须指定仓库位置。请在「源码仓库根」里填含 tools\\ 与 src\\ 的那一层'
    '（例如 D:\\qq机器人5.0），或设环境变量 %s，再点接入。' % DSH_REPO_ENV)


def repo_root() -> Path:
    """仓库根（src/ 的上一级）：源码态就是仓库；冻结态没有源码，改用 dsh_repo_root()。"""
    return Path(__file__).resolve().parents[1]


def frozen_mode() -> bool:
    """是不是 PyInstaller 冻结产物（冻结后既没有 src 的 .py，也没有 python.exe）。"""
    return bool(getattr(sys, 'frozen', False))


def dsh_repo_candidates(override=None) -> list:
    """候选仓库根，按优先级：显式传入 > 环境变量 WHALE_QQ_REPO > 源码态自己的仓库。"""
    found = []
    for value in (override, os.environ.get(DSH_REPO_ENV)):
        text = str(value or '').strip().strip('"')
        if text:
            found.append(Path(os.path.expanduser(text)))
    if not frozen_mode():
        found.append(repo_root())
    return found


def dsh_repo_root(override=None):
    """第一个「tools 下三个 MCP 脚本都在」的候选根；一个都没有返回 None。"""
    for base in dsh_repo_candidates(override):
        tools = Path(base) / 'tools'
        if all((tools / name).is_file() for name in DSH_SCRIPT_FILES.values()):
            return Path(base)
    return None


def dsh_repo_source(override=None) -> str:
    """这次用的仓库根是哪来的：override / env / repo（源码态自己）/ unset。"""
    if str(override or '').strip():
        return 'override'
    if str(os.environ.get(DSH_REPO_ENV) or '').strip():
        return 'env'
    return 'unset' if frozen_mode() else 'repo'


def dsh_scripts(root=None) -> dict:
    base = Path(root) if root is not None else (dsh_repo_root() or repo_root())
    return {key: str(base / 'tools' / name) for key, name in DSH_SCRIPT_FILES.items()}


def dsh_bundled_exe() -> str:
    """安装版自带的 MCP 宿主：控制台 exe 本身（只在 PyInstaller 冻结产物里成立）。"""
    if not frozen_mode() or not str(getattr(sys, '_MEIPASS', '') or '').strip():
        return ''
    exe = str(sys.executable or '').strip()
    if not exe or not os.path.isfile(exe):
        return ''
    if Path(exe).stem.lower().startswith('python'):
        return ''
    return exe


def dsh_bundled_args() -> dict:
    """内置宿主接收的三个 stdio MCP 子命令；一个 exe 分饰三路。"""
    return {'memory': ['--mcp', 'memory'],
            'bot': ['--mcp', 'bot'],
            'tools': ['--mcp', 'tools']}


def dsh_home(override=None) -> str:
    """DSH 根目录：显式参数 > 环境变量 DSH_HOME > ~/.dsh（与命令行工具的默认一致）。"""
    text = str(override or os.environ.get('DSH_HOME') or '').strip()
    if text:
        return os.path.abspath(text)
    return str(dsh_memory_bridge.default_dsh_home())


def dsh_python(root=None) -> str:
    """写进 MCP 条目的解释器：真能 import 仓库 src.* 的那个 python。

    源码态就是跑本控制台的 python（命令行工具的默认也是它）。冻结态的 sys.executable
    是 WhaleGirlQQBridge.exe —— 那不是解释器，绝不能写进 MCP 登记，所以先找仓库自带的
    venv，再退回 PATH 上的 python。
    """
    if not frozen_mode():
        candidate = str(sys.executable or '').strip()
        if candidate and os.path.isfile(candidate):
            return candidate
    base = Path(root) if root is not None else (dsh_repo_root() or repo_root())
    for name in DSH_VENV_DIRS:
        for parts in (('Scripts', 'python.exe'), ('bin', 'python3')):
            candidate = base.joinpath(name, *parts)
            if candidate.is_file():
                return str(candidate)
    for command in ('python', 'python3'):
        found = str(shutil.which(command) or '')
        # 微软商店的 python.exe 桩子会在 PATH 上，跑起来只会弹商店窗口。
        if found and 'WindowsApps' not in found:
            return found
    return '' if frozen_mode() else str(sys.executable or '')


_DSH_PYTHON_CHECKS = {}


def dsh_python_check(python, root) -> dict:
    """这个解释器能不能 import 仓库的 src.*：冻结态写登记前先验一遍（结果按路径缓存）。"""
    key = (str(python or ''), str(root or ''))
    if key in _DSH_PYTHON_CHECKS:
        return _DSH_PYTHON_CHECKS[key]
    result = {'ok': False, 'error': '没有解释器可检查'}
    if python and root:
        code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                'import src.chat_state, src.unified_memory; print("ok")')
        try:
            done = subprocess.run([str(python), '-c', code, str(root)],
                                  capture_output=True, text=True, encoding='utf-8',
                                  errors='replace', timeout=120)
            ok = done.returncode == 0 and 'ok' in str(done.stdout or '')
            lines = [line for line in str(done.stderr or '').splitlines() if line.strip()]
            result = {'ok': ok, 'error': '' if ok else (lines[-1].strip() if lines else '')}
        except (OSError, subprocess.SubprocessError) as exc:
            result = {'ok': False, 'error': str(exc)}
    _DSH_PYTHON_CHECKS[key] = result
    return result


_DSH_BUNDLED_CHECKS = {}


def dsh_bundled_check(exe) -> dict:
    """内置宿主能不能 import 三个桥：link 前跑 exe --mcp check（按路径缓存）。"""
    key = str(exe or '')
    if key in _DSH_BUNDLED_CHECKS:
        return _DSH_BUNDLED_CHECKS[key]
    result = {'ok': False, 'error': '没有内置 MCP 宿主'}
    if exe and os.path.isfile(exe):
        try:
            done = subprocess.run([str(exe), '--mcp', 'check'],
                                  capture_output=True, text=True, encoding='utf-8',
                                  errors='replace', timeout=120,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            ok = done.returncode == 0 and 'ok' in str(done.stdout or '')
            lines = [line for line in str(done.stderr or '').splitlines() if line.strip()]
            result = {'ok': ok, 'error': '' if ok else (lines[-1].strip() if lines else '')}
        except (OSError, subprocess.SubprocessError) as exc:
            result = {'ok': False, 'error': str(exc)}
    _DSH_BUNDLED_CHECKS[key] = result
    return result


def dsh_profiles(home) -> list:
    """<home>/profiles 下真正存在的 profile 目录名（要带 cordis 配置；node_modules 不算）。"""
    names = []
    try:
        with os.scandir(os.path.join(str(home), 'profiles')) as scan:
            entries = list(scan)
    except OSError:
        return []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if any(os.path.isfile(os.path.join(entry.path, name))
               for name in ('cordis.yml', 'cordis.patch.yml')):
            names.append(entry.name)
    return sorted(names)


def dsh_token(value) -> str:
    """命令行里的一项：含空格/引号就用双引号包起来（Windows 复制粘贴即可跑）。"""
    text = str(value)
    if text and not any(ch in text for ch in ' \t"'):
        return text
    return '"%s"' % text.replace('"', '')


def dsh_command(profile, action='link', home=None, bot_home=None, python=None,
                scripts=None) -> str:
    """给用户复制的等价命令：profile / 解释器 / DSH 根 / 数据根与端点这次用的完全一样。"""
    home = home or dsh_home()
    python = python or dsh_python()
    scripts = scripts or dsh_scripts()
    setup_script = Path(str(scripts['memory'])).parent / 'setup_dsh_memory.py'
    parts = [dsh_token(python), dsh_token(str(setup_script))]
    if action == 'unlink':
        parts.append('--remove')
    elif action == 'status':
        parts.append('--status')
    parts += ['--profile', dsh_token(profile), '--python', dsh_token(python),
              '--dsh-home', dsh_token(home)]
    if bot_home:
        parts += ['--bot-home', dsh_token(bot_home)]
    if action != 'status':
        # 脚本路径它也按自己的位置推，但显式写出来才不依赖命令行的工作目录。
        parts += ['--script', dsh_token(scripts['memory']),
                  '--bot-script', dsh_token(scripts['bot']),
                  '--tools-script', dsh_token(scripts['tools'])]
    return ' '.join(parts)


def dsh_profile_name(value) -> tuple:
    """(规范化的 profile 名, 问题字典)。空值按 desktop；非法名直接拒（防目录穿越）。"""
    name = str(value or '').strip() or DSH_DEFAULT_PROFILE
    if name in ('.', '..') or not DSH_PROFILE_PATTERN.match(name):
        return '', _fail('bad_profile',
                         'profile 名只允许字母/数字/下划线/点/短横线（1-64 个字符）：%r' % name)
    return name, None


def _dsh_entries(state) -> list:
    """三条登记各自的实际状态（回读 yaml 得来，不是「我们以为写了什么」）。"""
    return [
        {'id': dsh_memory_bridge.ENTRY_ID,
         'serverName': str(state.get('serverName') or dsh_memory_bridge.SERVER_NAME),
         'installed': bool(state.get('installed'))},
        {'id': dsh_memory_bridge.CONTROL_ENTRY_ID,
         'serverName': str(state.get('controlServerName')
                           or dsh_memory_bridge.CONTROL_SERVER_NAME),
         'installed': bool(state.get('control_installed'))},
        {'id': dsh_memory_bridge.TOOLS_ENTRY_ID,
         'serverName': str(state.get('toolsServerName')
                           or dsh_memory_bridge.TOOLS_SERVER_NAME),
         'installed': bool(state.get('tools_installed'))},
    ]


def dsh_config_path(app) -> str:
    """记「用户填的源码仓库根」的文件：落在运行数据目录，安装版才有地方存。"""
    data_dir, ready = _data_dir(app)
    if not ready or not data_dir:
        return ''
    return os.path.join(str(data_dir), DSH_REPO_CONFIG)


def dsh_config_data(app) -> dict:
    """读联动配置（仓库根 + 是否允许自动接入），坏文件当空配置。"""
    path = dsh_config_path(app)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dsh_write_config(app, data) -> bool:
    path = dsh_config_path(app)
    if not path or not isinstance(data, dict):
        return False
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        return False
    return True


def dsh_saved_repo(app) -> str:
    """上次填过且验证通过的仓库根（读不到就当没填）。"""
    return str(dsh_config_data(app).get('repo_root') or '').strip()


def dsh_save_repo(app, value) -> bool:
    data = dsh_config_data(app)
    data['repo_root'] = str(value or '').strip()
    return dsh_write_config(app, data)


def dsh_auto_enabled(app) -> bool:
    """控制台启动时是否自动把 DSH 接上（默认开；用户点过「撤销接入」会写 False）。"""
    return bool(dsh_config_data(app).get('auto_connect', True))


def dsh_set_auto(app, value) -> bool:
    data = dsh_config_data(app)
    data['auto_connect'] = bool(value)
    return dsh_write_config(app, data)


def dsh_link_ok(app):
    """上次 link 的解释器自检结果：True/False，没记录过是 None。"""
    value = dsh_config_data(app).get('link_ok')
    return value if isinstance(value, bool) else None


def dsh_set_link_ok(app, value) -> bool:
    data = dsh_config_data(app)
    data['link_ok'] = bool(value)
    return dsh_write_config(app, data)


def dsh_link_state(app, action='status', profile='', override_home=None,
                   repo_override='') -> dict:
    """DSH 联动的共享实现：status 只读；link/unlink 写 profile 再把结果读回来。"""
    name, problem = dsh_profile_name(profile)
    if problem is not None:
        return problem
    typed = str(repo_override or '').strip()
    saved = dsh_saved_repo(app)
    bundled = dsh_bundled_exe()
    # 显式填的仓库根写错时仍然报错，不静默改用内置宿主：排障时要看到真正原因。
    repo = dsh_repo_root(typed) if typed else (dsh_repo_root(saved) if saved else dsh_repo_root())
    if typed and repo is None:
        return _fail('dsh_needs_source', DSH_SOURCE_HINT, profile=name,
                     repo_root=typed, repo_env=DSH_REPO_ENV,
                     repo_root_source=dsh_repo_source(typed))
    scripts = {}
    python = ''
    if repo is not None:
        scripts = dsh_scripts(repo)
        python = dsh_python(repo)
        if not python and bundled:
            # 旧配置指向的仓库已经没有 venv/python：安装版改用自带宿主，不让用户卡死。
            repo = None
            scripts = {}
    if repo is None and not bundled:
        return _fail('dsh_needs_source', DSH_SOURCE_HINT, profile=name,
                     repo_root=typed or saved, repo_env=DSH_REPO_ENV,
                     repo_root_source=dsh_repo_source(typed or saved))
    if typed and repo is not None and typed != saved:
        dsh_save_repo(app, typed)
    if repo is None:
        python = bundled
        mcp_mode = 'bundled'
        source = 'bundled'
        repo_payload = ''
        mcp_args = dsh_bundled_args()
    else:
        mcp_mode = 'python'
        repo_payload = str(repo)
        # 这次用的仓库根从哪来：用户刚填的 / 上次记住的 / 环境变量 / 源码态自己的仓库。
        if typed:
            source = 'override'
        elif saved and dsh_repo_root(saved) is not None:
            source = 'saved'
        else:
            source = dsh_repo_source('')
        mcp_args = {key: [scripts[key]] for key in DSH_SCRIPT_FILES}
    home = dsh_home(override_home)
    data_dir, _ready = _data_dir(app)
    # 已有登记里的 WHALE_QQ_HOME 不能随便换：安装版控制台的 root 是
    # %LOCALAPPDATA%\WhaleGirlQQ，而真实 QQ 数据在网关账号目录
    # （...\WhaleQQGateway\core\accounts\qq-<uin>\bot）。自动接入若用 root 覆盖，
    # DSH 的记忆/工具桥会突然读不到这个账号的消息和插件，所以优先沿用旧值。
    existing = dsh_memory_bridge.status(home, profile=name)
    existing_home = str(existing.get('memory_qq_home')
                        or existing.get('tools_qq_home') or '').strip()
    bot_home = str(os.environ.get('WHALE_QQ_HOME') or existing_home
                   or data_dir or '').strip()
    command = ('' if mcp_mode == 'bundled'
               else dsh_command(name, action, home=home, bot_home=bot_home,
                                python=python, scripts=scripts))
    existed = os.path.isdir(os.path.join(home, 'profiles', name))
    raw = None
    if action in ('link', 'unlink'):
        try:
            if action == 'link':
                # bot_url 写空串 = 运行期解析（与命令行工具的默认一致）。
                if mcp_mode == 'bundled':
                    raw = dsh_memory_bridge.install_all(
                        home, python, '--mcp', bot_home, '--mcp', '',
                        profile=name, tools_script='--mcp',
                        memory_args=['--mcp', 'memory'],
                        control_args=['--mcp', 'bot'],
                        tools_args=['--mcp', 'tools'])
                else:
                    raw = dsh_memory_bridge.install_all(
                        home, python, scripts['memory'], bot_home, scripts['bot'], '',
                        profile=name, tools_script=scripts['tools'])
            else:
                raw = dsh_memory_bridge.remove(home, profile=name)
        except ValueError as exc:      # patch 文件不是列表：bridge 已断言、未落盘
            return _fail('bad_dsh_profile',
                         'DSH 的 cordis.patch.yml 不是 patch 列表，未改动：%s' % exc,
                         profile=name)
        except OSError as exc:
            return _fail('io', '写 DSH 配置失败（%s）：%s' % (home, exc), profile=name)
        except Exception as exc:       # noqa: BLE001 - 端点不许把裸 traceback 抛出去
            return _fail('internal', 'DSH %s失败：%s'
                         % ('接入' if action == 'link' else '撤销', exc), profile=name)
    # 写登记前实跑一次自检，免得登记了一个起不来的桥。
    if action == 'link' and mcp_mode == 'bundled':
        check = dsh_bundled_check(python)
    elif action == 'link' and frozen_mode():
        check = dsh_python_check(python, repo)
    else:
        check = None
    state = dsh_memory_bridge.status(home, profile=name)
    if state.get('error'):
        return _fail('bad_dsh_profile', '读 DSH 配置失败（%s）：%s'
                     % (str(state.get('path') or home), state.get('error')), profile=name)
    # 这两个是「这台机器现在该用的」，不是 yaml 里已有的；dsh_commands_current 拿它
    # 跟登记里的命令比对，才能发现仓库搬家/换 venv 后条目看着齐其实起不来。
    state['python'] = python
    state['scripts'] = dict(scripts)
    state['mcp_mode'] = mcp_mode
    state['mcp_command'] = python
    state['mcp_args'] = dict(mcp_args)
    payload = {
        'ok': True, 'action': action, 'profile': name,
        'dsh_home': home,
        'path': str(state.get('path') or dsh_memory_bridge.profile_path(home, name)),
        'python': python, 'bot_home': bot_home, 'scripts': scripts,
        'repo_root': repo_payload, 'repo_root_source': source,
        'repo_env': DSH_REPO_ENV,
        'mcp_mode': mcp_mode, 'mcp_command': python, 'mcp_args': mcp_args,
        'profiles': dsh_profiles(home),
        # tools_installed 指「三条都到位」；单条状态见下面三个键。
        'tools_installed': bool(state.get('installed') and state.get('control_installed')
                                and state.get('tools_installed')),
        'memory_installed': bool(state.get('installed')),
        'control_installed': bool(state.get('control_installed')),
        'tools_bridge_installed': bool(state.get('tools_installed')),
        'reconnect_ok': bool(state.get('reconnect_ok')),
        'entry_count': int(state.get('entry_count') or 0),
        'insert_blocks': int(state.get('insert_blocks') or 0),
        'bare_entry_ids': list(state.get('bare_entry_ids') or []),
        'entries': _dsh_entries(state),
        # yaml 里已经写着的命令：可能指向旧仓库/旧解释器，靠它一眼看出「登记了但起不来」。
        'memory_command': str(state.get('memory_command') or ''),
        'memory_args': list(state.get('memory_args') or []),
        'control_command': str(state.get('control_command') or ''),
        'control_script': str(state.get('control_script') or ''),
        'control_args': list(state.get('control_args') or []),
        'tools_command': str(state.get('tools_command') or ''),
        'tools_script': str(state.get('tools_script') or ''),
        'tools_args': list(state.get('tools_args') or []),
        'command': command,
        'hint': DSH_UNLINK_HINT if action == 'unlink' else DSH_LINK_HINT,
    }
    if raw is not None:
        payload['changed'] = bool(raw.get('changed'))
        payload['backup'] = raw.get('backup')
    if action == 'link':
        dsh_set_auto(app, True)
        # 记下这次真的改过 yaml：体检据此判断 DSH 需不需要重启（没改动就不写文件）。
        dsh_note_change(app, name, bool((raw or {}).get('changed')))
        # 记下这次解释器自检结果：装进去但 python 起不来时，下次启动要重试，
        # 不能因为条目「看起来齐了」就永远跳过。
        dsh_set_link_ok(app, bool(check is None or check.get('ok')))
    elif action == 'unlink':
        dsh_set_auto(app, False)
        dsh_set_link_ok(app, False)
        payload['removed'] = bool(raw.get('removed'))
    # link/unlink 刚改过配置，回读一次再返回，别把旧值报给面板。
    payload['link_ok'] = dsh_link_ok(app)
    if action == 'link':
        payload['profile_existed'] = existed
        notes = []
        if not existed:
            notes.append('DSH 里原来没有 profile %s 目录，已按 DSH 的目录结构新建；确认这个 '
                         'profile 名和 DSH 启动时用的一致（dsh --profile %s）' % (name, name))
        if check is not None:
            payload['python_check'] = check
            if not check.get('ok'):
                if mcp_mode == 'bundled':
                    notes.append('内置 MCP 宿主自检失败（%s），DSH 那边会连不上；'
                                 '请重新安装控制台。'
                                 % (check.get('error') or '未知错误'))
                else:
                    notes.append('解释器 %s 起不来这三个桥（%s），DSH 那边会连不上；在仓库里建好 '
                                 'venv 或换一个 python 再点一次。'
                                 % (python, check.get('error') or '未知错误'))
        if notes:
            payload['warning'] = ' '.join(notes)
    return payload


def _dsh_path_same(left, right) -> bool:
    if not left or not right:
        return False
    return (os.path.normcase(os.path.abspath(str(left)))
            == os.path.normcase(os.path.abspath(str(right))))


def dsh_commands_current(state) -> bool:
    """登记里的解释器/脚本是否还指向这台机器上的同一份仓库。

    status 只比对条目 id 不知道命令指到哪；仓库搬过家或换过 venv 时，条目看着
    "已装好"但 DSH 永远起不来。缺字段（老版本/测试替身）按未知处理，不误重写。
    """
    mode = str(state.get('mcp_mode') or '')
    if mode == 'bundled':
        exe = str(state.get('mcp_command') or state.get('python') or '')
        if not exe:
            return False
        for key in ('memory_command', 'control_command', 'tools_command'):
            got = str(state.get(key) or '').strip()
            if not got or not _dsh_path_same(got, exe):
                return False
        for key, expected in (('memory_args', ['--mcp', 'memory']),
                              ('control_args', ['--mcp', 'bot']),
                              ('tools_args', ['--mcp', 'tools'])):
            got = state.get(key)
            if not isinstance(got, (list, tuple)):
                return False
            if [str(item) for item in got] != expected:
                return False
        return True
    scripts = state.get('scripts')
    scripts = scripts if isinstance(scripts, dict) else {}
    python = str(state.get('python') or '')
    pairs = (('control_command', python),
             ('control_script', scripts.get('bot')),
             ('tools_command', python),
             ('tools_script', scripts.get('tools')))
    for key, expected in pairs:
        got = str(state.get(key) or '').strip()
        # 缺字段按未知处理：老版本/测试替身没有对齐信息时，不能据此判定成 stale。
        if not got or not expected:
            continue
        if not _dsh_path_same(got, expected):
            return False
    return True


def dsh_auto_connect(app, profile='desktop') -> dict:
    """控制台启动时自动把 DSH 接上（只对安装版 + 已有 DSH profile 生效）。

    背景：DSH 的 MCP 登记是 profile 级的，接好之后新工作区/新会话都会自动继承；
    难点只在「用户要先手动点一次」。这里把这一步做成启动时自动完成。已经接好
    且带着足够重连预算时不动配置，避免每次启动都备份一次 yaml。
    """
    name, problem = dsh_profile_name(profile)
    if problem is not None:
        return problem
    home = dsh_home()
    if not os.path.isdir(os.path.join(home, 'profiles', name)):
        return {'ok': True, 'skipped': True, 'reason': 'no_dsh',
                'message': '这台机器上没有 DSH profile %s，跳过自动接入' % name}
    if not dsh_auto_enabled(app):
        return {'ok': True, 'skipped': True, 'reason': 'disabled',
                'message': '自动接入已关闭'}
    # 源码态不自动改真实 ~/.dsh：开发机上的 profile 可能不属于这个仓库；
    # 安装版（冻结态）才是用户真正需要零点击的场景。
    if not frozen_mode():
        return {'ok': True, 'skipped': True, 'reason': 'dev_mode',
                'message': '源码态不自动改 DSH 配置，请在面板里手动接入'}
    state = dsh_link_state(app, 'status', name)
    if not state.get('ok'):
        if state.get('code') == 'dsh_needs_source':
            return {'ok': True, 'skipped': True, 'reason': 'no_source',
                    'message': str(state.get('message') or '')}
        return state
    if (state.get('tools_installed') and state.get('reconnect_ok')
            and dsh_link_ok(app) is not False and dsh_commands_current(state)):
        return {'ok': True, 'skipped': True, 'reason': 'already',
                'profile': name}
    result = dsh_link_state(app, 'link', name)
    result['auto'] = True
    return result


# ── DSH 多 profile 自动接入 + 连接体检 ──────────────────────────────────────
#
# 一个 DSH 根下通常有多个 profile（desktop / web / …），每个各有一份
# cordis.patch.yml。自动接入要逐个过一遍：漏掉哪个，用户在 DSH 里新建的会话就少了
# 三组工具；而一个写坏的 profile 也不能把整轮接入拖停。

# 本进程的启动时刻：体检靠它判断「这份 yaml 是不是刚被这次控制台改过」。
DSH_STARTED_AT = time.time()
DSH_TOOL_NAMES = {'whale_memory': 'mcp__whale_memory__*',
                  'whale_bot': 'mcp__whale_bot__*',
                  'whale_tools': 'mcp__whale_tools__*'}
DSH_ENTRY_TOOLS = {dsh_memory_bridge.ENTRY_ID: DSH_TOOL_NAMES['whale_memory'],
                   dsh_memory_bridge.CONTROL_ENTRY_ID: DSH_TOOL_NAMES['whale_bot'],
                   dsh_memory_bridge.TOOLS_ENTRY_ID: DSH_TOOL_NAMES['whale_tools']}


def _dsh_iso(stamp) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp)) if stamp else ''


def dsh_config_changed_at(app, profile) -> float:
    """上次真正改过这个 profile 的 yaml 是什么时候（没记过就是 0）。"""
    stamps = dsh_config_data(app).get('config_changed_at')
    if not isinstance(stamps, dict):
        return 0.0
    try:
        return float(stamps.get(str(profile)) or 0)
    except (TypeError, ValueError):
        return 0.0


def dsh_note_change(app, profile, changed) -> bool:
    """记下这次真的改过某个 profile 的 yaml（内容没变就不落盘，保持幂等）。"""
    if not changed:
        return True
    data = dsh_config_data(app)
    stamps = data.get('config_changed_at')
    stamps = dict(stamps) if isinstance(stamps, dict) else {}
    stamps[str(profile)] = time.time()
    data['config_changed_at'] = stamps
    return dsh_write_config(app, data)


def dsh_restart_required(app, profile) -> tuple:
    """(要不要重启 DSH, 变动时间戳)：本次运行期间改过这份 yaml 就必须重启。

    判据只看「本进程有没有写过它」：写完那一刻 DSH 手里还是旧配置，而 DSH 不会
    热加载 profile，所以得重启 DSH 再新建会话。上次控制台留下的老时间戳不算，
    免得永远提示重启。
    """
    stamp = dsh_config_changed_at(app, profile)
    return bool(stamp and stamp >= DSH_STARTED_AT), stamp


def _dsh_target_profiles(home, profiles=None) -> list:
    """这一轮接哪些 profile：显式给了就用它（去重、校验名字），否则扫 DSH 目录。"""
    if profiles is None:
        names = dsh_profiles(home)
    else:
        raw = [profiles] if isinstance(profiles, str) else list(profiles or [])
        names = []
        for value in raw:
            name, problem = dsh_profile_name(value)
            if problem is None and name not in names:
                names.append(name)
    # profile 目录下的 node_modules 不是 profile；显式点名也不接。
    return [name for name in names if name != 'node_modules']


def _dsh_auto_one(app, name) -> dict:
    """跑单个 profile 的自动接入；异常一律收进这一条结果，不打断其它 profile。"""
    try:
        raw = dsh_auto_connect(app, name)
    except Exception as exc:       # noqa: BLE001 - 一个坏 profile 不能拖垮整轮接入
        return {'profile': name, 'ok': False, 'skipped': False, 'reason': 'error',
                'message': '自动接入 %s 抛异常：%s' % (name, exc)}
    if not isinstance(raw, dict):
        return {'profile': name, 'ok': False, 'skipped': False, 'reason': 'error',
                'message': '自动接入 %s 返回的不是字典：%r' % (name, raw)}
    ok = bool(raw.get('ok'))
    skipped = bool(raw.get('skipped'))
    reason = str(raw.get('reason') or raw.get('code')
                 or ('already' if skipped else ('linked' if ok else 'failed')))
    return {'profile': name, 'ok': ok, 'skipped': skipped, 'reason': reason,
            'message': str(raw.get('message') or ''),
            'changed': bool(raw.get('changed')), 'detail': raw}


def dsh_auto_connect_all(app, profiles=None) -> dict:
    """把 DSH 的所有 profile 逐个接上（控制台启动时调一次；单个失败不影响其它）。

    profiles 不给就扫 dsh_home()/profiles 下真实存在的 profile 目录。前置条件和
    dsh_auto_connect 一致：安装版 + 自动接入开着 + 至少有一个 profile 才动手，
    源码态不改真实 ~/.dsh。
    """
    home = dsh_home()
    names = _dsh_target_profiles(home, profiles)
    base = {'dsh_home': home, 'profiles': list(names),
            'attempted': 0, 'linked': 0, 'skipped': 0}
    if not names:
        return dict(base, ok=True, reason='no_dsh', results=[],
                    message='%s 下没有可接入的 DSH profile，跳过自动接入' % home)
    reason, message = '', ''
    if not dsh_auto_enabled(app):
        reason, message = 'disabled', '自动接入已关闭'
    elif not frozen_mode():
        reason, message = 'dev_mode', '源码态不自动改 DSH 配置，请在面板里手动接入'
    if reason:
        results = [{'profile': name, 'ok': True, 'skipped': True,
                    'reason': reason, 'message': message} for name in names]
        return dict(base, ok=True, reason=reason, message=message,
                    skipped=len(results), results=results)
    results = [_dsh_auto_one(app, name) for name in names]
    failed = [row for row in results if not row['ok']]
    payload = dict(base, ok=not failed, failed=len(failed), results=results,
                   attempted=sum(1 for row in results if not row['skipped']),
                   linked=sum(1 for row in results
                              if row['ok'] and not row['skipped']),
                   skipped=sum(1 for row in results if row['skipped']))
    if failed:
        payload['reason'] = str(failed[0].get('reason') or 'error')
        payload['message'] = '；'.join(
            '%s：%s' % (row['profile'], row.get('message') or row.get('reason'))
            for row in failed)
    return payload


def _dsh_report_hint(name, installed, reconnect_ok, current, restart_required,
                     missing, error='') -> str:
    """体检里的中文下一步：只给「最该先解决的那一个」，不要一串并列选项。"""
    if error:
        return ('profile %s 的 cordis.patch.yml 读不出来（%s），DSH 会当它没有这三条登记；'
                '先修好这个文件（控制台每次改动前都会留 .bak-whale-memory-* 备份）。'
                % (name, error))
    if missing:
        return ('profile %s 还缺 %d 条 MCP 登记（%s）；在面板点「让 DSH 接上控制台」，'
                '或把 dsh_command("%s") 给出的命令原样跑一遍。'
                % (name, len(missing), '、'.join(missing), name))
    if not reconnect_ok:
        return ('三条登记都在，但缺约 8 小时的重连预算：控制台晚于 DSH 启动或中途重启时，'
                'DSH 会放弃重连并把这些工具摘掉。重新接一次（控制台启动时会自动做）即可补上。')
    if not current:
        return ('三条登记里的解释器/脚本和这台机器上的仓库对不上（仓库搬过家或换过 venv），'
                'DSH 起来也会立刻断开；重新接一次会改写成当前路径。')
    if restart_required:
        return ('三组工具已经登记好：重启 DSH 后新建会话才能看到 mcp__whale_memory__* / '
                'mcp__whale_bot__* / mcp__whale_tools__*；已经开着的会话不会重载配置。')
    return ('profile 级 MCP 登记会被 DSH 的新会话直接继承：现在在 DSH 里新建会话就能看到 '
            'mcp__whale_memory__* / mcp__whale_bot__* / mcp__whale_tools__* 三组工具，'
            '不用再配置。控制台晚启动或中途重启时，靠约 8 小时的重连预算自己连回来。')


def dsh_connection_report(app, profile='') -> dict:
    """只读体检：DSH 的新会话现在能不能直接用上三组工具，还差什么。

    不执行任何外部 DSH 命令（不跑 dsh，也不起 MCP 子进程），只读 profile 的
    cordis.patch.yml 和控制台自己的配置，所以面板随便刷新都安全。
    返回的 ok 只表示「体检本身跑通了」，能不能用看 new_session_ready。
    """
    name, problem = dsh_profile_name(profile)
    if problem is not None:
        return problem
    home = dsh_home()
    raw = dsh_memory_bridge.status(home, profile=name)
    state = dsh_link_state(app, 'status', name)
    known = bool(state.get('ok'))
    error = str(raw.get('error') or '')
    entries = [dict(entry, tools=DSH_ENTRY_TOOLS.get(entry['id'], ''))
               for entry in _dsh_entries(raw)]
    installed = bool(raw.get('installed') and raw.get('control_installed')
                     and raw.get('tools_installed'))
    reconnect_ok = bool(raw.get('reconnect_ok'))
    current = bool(known and dsh_commands_current(state))
    restart_required, changed_at = dsh_restart_required(app, name)
    missing = [entry['id'] for entry in entries if not entry['installed']]
    issues = []
    if error:
        issues.append('bad_yaml')
    if missing:
        issues.append('missing_entries')
    if installed and not reconnect_ok:
        issues.append('no_reconnect_budget')
    if installed and not current:
        issues.append('stale_commands')
    if restart_required:
        issues.append('restart_required')
    # 「新会话能不能直接用」只看三条齐全 + 命令有效：重连预算影响的是控制台晚启动
    # 或重启后的自愈能力，不挡新建会话。
    ready = bool(installed and current)
    return {
        'ok': True, 'host': home, 'profile': name,
        'path': str(raw.get('path') or state.get('path')
                    or dsh_memory_bridge.profile_path(home, name)),
        'profile_exists': os.path.isdir(os.path.join(home, 'profiles', name)),
        'installed': installed, 'missing': missing, 'entries': entries,
        'tools': dict(DSH_TOOL_NAMES),
        'reconnect_ok': reconnect_ok,
        'commands_current': current, 'commands_known': known,
        'restart_required': restart_required,
        'config_changed_at': _dsh_iso(changed_at),
        'new_session_ready': ready, 'issues': issues,
        'hint': _dsh_report_hint(name, installed, reconnect_ok, current,
                                restart_required, missing, error),
        'checked_at': _dsh_iso(time.time()),
    }


# ── 路由 ────────────────────────────────────────────────────────────────────

def install_agent_backend_routes(app) -> None:
    """把 /api/agent/backends* 挂到宿主 app 上（由 qq_app.create_app 调用）。"""

    @app.exception_handler(_Denied)
    async def _on_denied(request: Request, exc: _Denied):
        return JSONResponse({'ok': False, 'code': 'auth_denied', 'message': str(exc)},
                            status_code=403)

    # 直接用 app.get/app.post 注册，不走 include_router：本机装的 FastAPI 版本会往
    # app.routes 里塞 _IncludedRouter 占位符（没有 .path），smoke 与既有测试都是
    # 遍历 app.routes 找 route.path 的，include_router 会把它们全打断。
    guard = {'dependencies': [Depends(_require_local)]}
    item_path = PREFIX + '/{backend_id}'

    @app.get(PREFIX, **guard)
    async def list_backends():
        data_dir, ready = _data_dir(app)
        profiles, problem = _profiles(data_dir)
        rows = [_row(data_dir, ready, backend_id, entry)
                for backend_id, entry in sorted(profiles.items())]
        payload = {'ok': True, 'data_dir': data_dir, 'data_dir_ready': ready,
                   'profile_file': profile_path(data_dir) if ready else '',
                   'backends': rows, 'count': len(rows),
                   'live': live_backends(), 'kinds': list(BACKEND_KINDS)}
        if problem is not None:
            # 坏文件不该让列表 500：空表 + 原样带出 code/message。
            payload.update({'ok': False,
                            'code': str(problem.get('code') or 'bad_profile_file'),
                            'message': str(problem.get('message') or '')})
        return _respond(payload)

    @app.get(PREFIX + '/detect', **guard)
    async def detect_backends(probe: int = 0):
        """只读探测本机 DSH：probe=0（默认）不启动任何进程，probe=1 才跑 --version。"""
        found = await asyncio.to_thread(agent_detect.detect, probe=bool(probe))
        return _respond({'ok': True,
                         'items': list(found.get('items') or []),
                         'scanned': list(found.get('scanned') or []),
                         'elapsed_ms': int(found.get('elapsed_ms') or 0),
                         'probe': bool(probe),
                         'reason': str(found.get('reason') or '')})

    # DSH 联动必须注册在 item_path 之前：GET /dsh/status 和 GET /{backend_id}/status
    # 是同一个形状，先注册的赢，否则 dsh 会被当成一个 backend_id 吞掉变成 404。
    @app.get(PREFIX + '/dsh/status', **guard)
    async def dsh_status(profile: str = '', repo_root: str = ''):
        """只读：DSH 那边现在登记了什么、有哪些 profile（不写 DSH 文件）。"""
        return _respond(await asyncio.to_thread(dsh_link_state, app, 'status', profile,
                                               None, repo_root))

    # 只读体检放在 status 后面：面板刷新它不会改 yaml、不会起子进程。
    @app.get(PREFIX + '/dsh/connection-report', **guard)
    async def dsh_connection_report_api(profile: str = ''):
        """只读：DSH 的新会话现在能不能直接用上三组工具，还差什么。"""
        return _respond(await asyncio.to_thread(dsh_connection_report, app, profile))

    @app.post(PREFIX + '/dsh/link', **guard)
    async def dsh_link(body: Optional[DshBody] = None):
        """把三条 MCP 桥写进 DSH profile（等价 tools/setup_dsh_memory.py，可撤销）。"""
        return _respond(await asyncio.to_thread(dsh_link_state, app, 'link',
                                                (body.profile if body else ''),
                                                None,
                                                (body.repo_root if body else '')))

    @app.post(PREFIX + '/dsh/unlink', **guard)
    async def dsh_unlink(body: Optional[DshBody] = None):
        """撤销接入：把三条登记从 DSH profile 删掉（先备份）。"""
        return _respond(await asyncio.to_thread(dsh_link_state, app, 'unlink',
                                                (body.profile if body else ''),
                                                None,
                                                (body.repo_root if body else '')))

    @app.post(PREFIX, **guard)
    async def upsert_backend(body: ProfileBody):
        data_dir, ready = _data_dir(app)
        if not ready:
            return _respond(_fail('no_data_dir', '运行数据目录不可用，无法保存 profile'))
        backend_id = str(body.id or '').strip()
        if not BACKEND_ID_PATTERN.fullmatch(backend_id):
            return _respond(_fail('bad_profile',
                                  'backendId 只允许 [A-Za-z0-9_-]{1,64}：%r' % backend_id))
        checked = validate_profile(str(body.kind or ''), body.profile)
        if not checked.get('ok'):
            return _respond(checked)          # 错误码原样透出（bad_profile）
        profiles, problem = _profiles(data_dir)
        if problem is not None:
            return _respond(problem)
        exists = backend_id in profiles
        if exists and body.create_only:
            return _respond(_fail('duplicate_id', 'backendId %s 已存在' % backend_id))
        if exists and (backend_id in _live_ids() or _thread_alive(backend_id)):
            return _respond(_fail('already_running',
                                  '后端 %s 正在运行，先停止再改 profile' % backend_id))
        profiles[backend_id] = {'kind': checked['kind'], 'profile': checked['profile']}
        saved = save_profiles(profile_path(data_dir), profiles)
        if not saved.get('ok'):
            return _respond(saved)
        _forget(backend_id)                   # 未运行才走到这里：重建实例用新 profile
        return _respond({'ok': True, 'id': backend_id, 'created': not exists,
                         'updated': exists, 'profile_file': saved.get('path'),
                         'profile': checked['profile'], 'kind': checked['kind'],
                         'backend': _row(data_dir, ready, backend_id, profiles[backend_id])})

    @app.post(PREFIX + '/shutdown', **guard)
    async def shutdown():
        return _respond(await asyncio.to_thread(shutdown_all))

    @app.delete(item_path, **guard)
    async def delete_backend(backend_id: str):
        data_dir, ready = _data_dir(app)
        if not ready:
            return _respond(_fail('no_data_dir', '运行数据目录不可用，无法删除 profile'))
        profiles, problem = _profiles(data_dir)
        if problem is not None:
            return _respond(problem)
        if backend_id not in profiles:
            return _respond(_fail('not_found', '没有 id 为 %s 的后端 profile' % backend_id))
        if backend_id in _live_ids() or _thread_alive(backend_id):
            return _respond(_fail('already_running',
                                  '后端 %s 正在运行，不能删除（先停止）' % backend_id))
        backend = _cached(backend_id, data_dir)
        if backend is not None and backend.status().get('running'):
            return _respond(_fail('already_running',
                                  '后端 %s 的进程还活着，不能删除（先停止）' % backend_id))
        profiles.pop(backend_id, None)
        saved = save_profiles(profile_path(data_dir), profiles)
        if not saved.get('ok'):
            return _respond(saved)
        _forget(backend_id, stop=True)
        return _respond({'ok': True, 'removed': backend_id,
                         'profile_file': saved.get('path'), 'count': len(profiles)})

    @app.post(item_path + '/probe', **guard)
    async def probe(backend_id: str):
        backend, problem = _instance(app, backend_id)
        if problem is not None:
            return _respond(problem)
        return _respond(await asyncio.to_thread(backend.probe))

    @app.post(item_path + '/start', **guard)
    async def start(backend_id: str, body: Optional[StartBody] = None):
        backend, problem = _instance(app, backend_id)
        if problem is not None:
            return _respond(problem)
        payload = body or StartBody()
        result = start_background(backend, prompt=payload.prompt, timeout=payload.timeout)
        return _respond(result, ok_status=202)

    @app.post(item_path + '/stop', **guard)
    async def stop(backend_id: str):
        backend, problem = _instance(app, backend_id)
        if problem is not None:
            return _respond(problem)
        result = await asyncio.to_thread(backend.stop)
        with _REGISTRY_LOCK:
            thread = _INFLIGHT.get(backend_id)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)          # 让 start 那侧落定 stopped 再回话
        return _respond(result)

    @app.get(item_path + '/status', **guard)
    async def status(backend_id: str):
        backend, problem = _instance(app, backend_id)
        if problem is not None:
            return _respond(problem)
        row = backend.status()
        if not row.get('ok'):
            return _respond(row)
        with _REGISTRY_LOCK:
            last = dict(_LAST_RUN[backend_id]) if backend_id in _LAST_RUN else None
        if _thread_alive(backend_id) and row.get('state') in ('idle', 'failed', 'stopped'):
            row['state'] = 'starting'
        row.update({'profile': dict(backend.profile),
                    'running_thread': _thread_alive(backend_id),
                    'last_run': last,
                    'profile_file': profile_path(getattr(backend, 'data_dir', '')),
                    'data_dir_ready': True})
        return _respond(row)

    @app.get(item_path + '/workspace', **guard)
    async def workspace(backend_id: str, path: str = ''):
        backend, problem = _instance(app, backend_id)
        if problem is not None:
            return _respond(problem)
        made = await asyncio.to_thread(backend.workspace)
        if not made.get('ok'):
            return _respond(made)
        listing = await asyncio.to_thread(agent_workspace.list_dir,
                                          backend.data_dir, backend.id, path)
        if not listing.get('ok'):
            error = dict(listing.get('error') or {})
            return _respond(_fail(str(error.get('code') or 'denied'),
                                  str(error.get('message') or '列目录失败'),
                                  details=error.get('details') or {}))
        entries = list(listing.get('entries') or [])
        return _respond({'ok': True, 'backend': backend.id, 'root': made.get('path'),
                         'path': listing.get('path'), 'relpath': path,
                         'entries': entries, 'count': len(entries)})

    return app
