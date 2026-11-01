"""机器人控制台 ↔ 管理器（WhaleQQGateway）的连接。

管理器启动时会在 `<数据目录>/core/connection.json` 写下 base_url 与令牌，
退出时该文件可能残留，所以读取时要顺便确认进程还活着（pid）。

这里只做**只读**：列出管理器里的账号；真正的中继接入在后续版本。
"""
from __future__ import annotations

import json
import os
import pathlib

__all__ = ['connection_path', 'read_connection', 'fetch_accounts', 'manager_exe',
           'start_headless_bot']

import requests


def connection_path() -> pathlib.Path:
    base = pathlib.Path(os.getenv('LOCALAPPDATA') or pathlib.Path.home())
    return base / 'WhaleQQGateway' / 'core' / 'connection.json'


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


def read_connection(path: pathlib.Path | None = None) -> dict:
    """读管理器连接信息；文件不存在、损坏或进程已退出都返回 {}。"""
    target = pathlib.Path(path) if path else connection_path()
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not data.get('base_url'):
        return {}
    if data.get('pid') and _pid_state(int(data.get('pid') or 0)) == 'dead':
        return {}
    return data


def fetch_accounts(connection: dict, timeout: float = 3.0) -> dict:
    """用管理令牌拉账号列表；失败返回 {'ok': False, 'message': ...}。"""
    base = str((connection or {}).get('base_url') or '').rstrip('/')
    token = str((connection or {}).get('token') or '')
    if not base or not token:
        return {'ok': False, 'message': '没有管理器连接信息'}
    try:
        response = requests.get(base + '/v1/accounts', timeout=timeout,
                                headers={'Authorization': 'Bearer ' + token},
                                proxies={'http': None, 'https': None, 'all': None})
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


def manager_exe() -> str:
    """管理器安装位置（用于「打开管理器」按钮）；找不到返回空串。"""
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
        response = requests.post(base + f'/v1/accounts/{account_id}/bot',
                                 json={'source': source, 'headless': True},
                                 headers={'Authorization': 'Bearer ' + token},
                                 timeout=timeout,
                                 proxies={'http': None, 'https': None, 'all': None})
        data = response.json() if response.content else {}
    except Exception as error:
        return {'ok': False, 'message': f'{type(error).__name__}: {error}'}
    if response.status_code >= 400:
        return {'ok': False, 'message': str((data or {}).get('detail') or (data or {}).get('message')
                                            or response.status_code)}
    port = int((data or {}).get('port') or 0)
    return {'ok': bool(port), 'port': port,
            'message': str((data or {}).get('message') or ''), 'raw': data}
