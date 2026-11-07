"""机器人控制台 ↔ 管理器（WhaleQQGateway）的连接。

管理器启动时会在 `<数据目录>/core/connection.json` 写下 base_url 与令牌，
退出时该文件可能残留，所以读取时要顺便确认进程还活着（pid）。

这里只做**只读**：列出管理器里的账号；真正的中继接入在后续版本。
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time

__all__ = ['connection_path', 'connection_paths', 'read_connection', 'fetch_accounts',
           'manager_exe', 'start_headless_bot', 'probe', 'describe_failure']

import requests


def _env_path(name: str) -> pathlib.Path | None:
    value = os.getenv(name)
    return pathlib.Path(value) if value else None


def connection_paths() -> list[pathlib.Path]:
    r"""管理器可能写连接文件的位置，按优先级排列。

    除了标准的 %LOCALAPPDATA%\WhaleQQGateway，还覆盖：
    - 漫游/公共数据目录（有些安装器用 APPDATA/ProgramData）；
    - 绿色版或自定义安装目录（连接文件就在 exe 旁边或 data 子目录）。
    """
    roots: list[pathlib.Path] = []
    for env in ('LOCALAPPDATA', 'APPDATA', 'ProgramData'):
        base = _env_path(env)
        if base:
            roots += [base / 'WhaleQQGateway', base / 'WhaleQQGateway' / 'data']
    for candidate in _manager_candidates():
        roots += [candidate.parent, candidate.parent / 'data']

    paths: list[pathlib.Path] = []
    seen: set[str] = set()
    for root in roots:
        candidate = root / 'core' / 'connection.json'
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            paths.append(candidate)
    # 旧版/便携版有时直接把 connection.json 放在数据根目录
    local = _env_path('LOCALAPPDATA')
    if local:
        legacy = local / 'WhaleQQGateway' / 'connection.json'
        if str(legacy).lower() not in seen:
            paths.append(legacy)
    if not paths:
        paths.append(pathlib.Path.home() / 'WhaleQQGateway' / 'core' / 'connection.json')
    return paths


def connection_path() -> pathlib.Path:
    """第一个存在的连接文件；都不存在时返回标准位置（兼容旧调用）。"""
    paths = connection_paths()
    for candidate in paths:
        if candidate.is_file():
            return candidate
    return paths[0]


def _pid_state(pid: int) -> str:
    """返回 alive / denied / dead。

    denied 表示进程确实在，但当前控制台权限不够、打不开它（例如管理器
    以管理员身份运行）。这种情况不能当成"已退出"，否则控制台永远连不上。
    """
    if not pid:
        return 'dead'
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, int(pid))     # QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return 'alive'
        return 'denied' if ctypes.get_last_error() == 5 else 'dead'  # ERROR_ACCESS_DENIED
    except Exception:
        return 'dead'


def _read_connection_file(target: pathlib.Path) -> dict:
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not data.get('base_url'):
        return {}
    if data.get('pid') and _pid_state(int(data.get('pid') or 0)) == 'dead':
        return {}
    return data


def read_connection(path: pathlib.Path | None = None) -> dict:
    """读管理器连接信息；文件不存在、损坏或进程已退出都返回 {}。

    不指定路径时按 connection_paths() 依次尝试：旧位置残留的 connection.json
    可能指向已经退出的进程，不能因为它存在就放弃后面仍然有效的位置。
    """
    targets = [pathlib.Path(path)] if path else connection_paths()
    for target in targets:
        data = _read_connection_file(target)
        if data:
            return data
    return {}


_SESSION = None
_SESSION_LOCK = threading.Lock()


def _session():
    """进程内复用的 requests.Session。

    每次 get/post 都新建连接要重做一次 TCP 握手；管理器的账号列表在界面上是
    定时刷新的，握手开销会直接变成界面卡顿。trust_env=False 让 requests 不要
    读 HTTP(S)_PROXY：管理器只在本机监听，走公司代理只会连到一个不存在的端口。
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            session = requests.Session()
            session.trust_env = False
            _SESSION = session
        return _SESSION


def reset_session() -> None:
    """丢掉复用的连接（改过地址/代理设置后调用，下次请求会重连）。"""
    global _SESSION
    with _SESSION_LOCK:
        old, _SESSION = _SESSION, None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass


def probe(base_url: str, token: str = '', timeout: float = 2.0) -> dict:
    """对管理器做一次轻量探活，返回 {'ok','status','message','elapsed_ms'}。

    和 fetch_accounts 的区别：这里要的是"为什么连不上"，不是账号列表。
    status 取值：ok / unauthorized / http_error / unreachable / no_config。
    """
    base = str(base_url or '').rstrip('/')
    started = time.monotonic()

    def done(ok, status, message, **extra):
        result = {'ok': ok, 'status': status, 'message': message,
                  'elapsed_ms': int((time.monotonic() - started) * 1000)}
        result.update(extra)
        return result

    if not base:
        return done(False, 'no_config', '没有管理器地址')
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    try:
        response = _session().get(base + '/v1/accounts', timeout=timeout, headers=headers)
    except Exception as error:
        return done(False, 'unreachable', f'{type(error).__name__}: {error}')
    if response.status_code in (401, 403):
        return done(False, 'unauthorized', '令牌无效或已过期',
                    http_status=response.status_code)
    if response.status_code >= 400:
        return done(False, 'http_error', f'管理器返回 {response.status_code}',
                    http_status=response.status_code)
    return done(True, 'ok', '管理器在线', http_status=response.status_code)


def describe_failure(result: dict) -> str:
    """把 probe() 的结果翻成一句给用户看的话（不含堆栈）。"""
    status = str((result or {}).get('status') or '')
    message = str((result or {}).get('message') or '')
    if status == 'ok':
        return '管理器在线'
    if status == 'no_config':
        return '没有找到管理器连接信息：先启动 WhaleQQGateway，或用「打开管理器」指定位置'
    if status == 'unauthorized':
        return '管理器在线但令牌无效：管理器重启后会重写连接文件，刷新一次即可'
    if status == 'http_error':
        return f'管理器在线但接口异常（{message}）：检查管理器版本是否与控制台匹配'
    if status == 'unreachable':
        return f'连不上管理器（{message}）：确认它还在运行，或端口被占用'
    return message or '未知状态'


def fetch_accounts(connection: dict, timeout: float = 3.0) -> dict:
    """用管理令牌拉账号列表；失败返回 {'ok': False, 'message': ...}。"""
    base = str((connection or {}).get('base_url') or '').rstrip('/')
    token = str((connection or {}).get('token') or '')
    if not base or not token:
        return {'ok': False, 'message': '没有管理器连接信息'}
    try:
        response = _session().get(base + '/v1/accounts', timeout=timeout,
                                  headers={'Authorization': 'Bearer ' + token})
        data = response.json()
    except Exception as error:
        return {'ok': False, 'message': f'{type(error).__name__}: {error}'}
    if response.status_code >= 400:
        return {'ok': False, 'message': str((data or {}).get('detail') or response.status_code)}
    items = (data or {}).get('items') if isinstance(data, dict) else None
    return {'ok': True, 'items': items if isinstance(items, list) else [],
            'active': (data or {}).get('active', '')}


def _manager_candidates() -> list[pathlib.Path]:
    """管理器可能被装到的位置（用户目录、Program Files、绿色版）。"""
    roots: list[pathlib.Path] = []
    local = os.getenv('LOCALAPPDATA')
    if local:
        roots += [pathlib.Path(local) / 'Programs' / 'WhaleQQGateway',
                  pathlib.Path(local) / 'WhaleQQGateway']
    for env in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
        base = os.getenv(env)
        if base:
            roots.append(pathlib.Path(base) / 'WhaleQQGateway')
    seen: set[str] = set()
    candidates: list[pathlib.Path] = []
    for root in roots:
        candidate = root / 'WhaleQQGateway.exe'
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    return candidates


def _running_manager_exe() -> str:
    """正在运行的管理器 exe 路径（覆盖绿色版/自定义安装目录）。

    固定候选目录找不到时，用 Get-Process 问一下系统；只在 Windows 上尝试，
    失败就安静地返回空串，不影响后面的固定目录探测。
    """
    if os.name != 'nt':
        return ''
    try:
        import subprocess
        completed = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
             '(Get-Process -Name WhaleQQGateway -ErrorAction SilentlyContinue | '
             'Select-Object -First 1 -ExpandProperty Path)'],
            capture_output=True, text=True, timeout=4,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for line in (completed.stdout or '').splitlines():
            candidate = line.strip().strip('"')
            if candidate and pathlib.Path(candidate).is_file():
                return candidate
    except Exception:
        pass
    return ''


def manager_exe() -> str:
    """管理器安装位置（用于「打开管理器」按钮）；找不到返回空串。"""
    running = _running_manager_exe()
    if running:
        return running
    for candidate in _manager_candidates():
        if candidate.is_file():
            return str(candidate)
    return ''

def start_headless_bot(connection: dict, account_id: str, source: str,
                       timeout: float = 120.0) -> dict:
    """让管理器为该账号拉起"无窗口机器人后端"，返回端口。

    这是"一个控制台控制多个 QQ"的关键：后端没有窗口，控制台切换账号时
    只把请求地址切到对应端口，不必开一堆窗口。
    """
    base = str((connection or {}).get('base_url') or '').rstrip('/')
    token = str((connection or {}).get('token') or '')
    if not base or not token:
        return {'ok': False, 'message': '没有管理器连接信息'}
    try:
        response = _session().post(base + f'/v1/accounts/{account_id}/bot',
                                   json={'source': source, 'headless': True},
                                   headers={'Authorization': 'Bearer ' + token},
                                   timeout=timeout)
        data = response.json() if response.content else {}
    except Exception as error:
        return {'ok': False, 'message': f'{type(error).__name__}: {error}'}
    if response.status_code >= 400:
        return {'ok': False, 'message': str((data or {}).get('detail') or (data or {}).get('message')
                                            or response.status_code)}
    port = int((data or {}).get('port') or 0)
    return {'ok': bool(port), 'port': port,
            'message': str((data or {}).get('message') or ''), 'raw': data}
