"""W9a：只读探测本机 DSH（外部后端面板的「自动识别」）。

本模块只做一件事：扫一遍本机常见位置，把找到的 DSH 入口归一成可以直接喂给
src/agent_backends.py 的 validate_profile() 的候选条目。硬约束：

- **永不抛异常**：任何一层失败（环境变量怪、PATH 爆、子进程炸）都归一成
  items 为空 + reason，顶层 ok 恒 True；
- **默认不启动进程**：probe=False 时只用 shutil.which / os.path.exists 看，
  面板首屏要快；probe=True 才真跑 <command> --version（每个命令 <= 15s，最多 3 个）；
- 返回值是 json 安全的纯 dict/list/str/int/bool，不夹带任何对象。

公开 API：
  dsh_home(env=None)   -> $DSH_HOME 优先，否则 ~/.dsh（绝对路径）
  dsh_profiles(root)   -> <root>/profiles 下的子目录名，headless 第一，其余字母序
  detect(*, probe=False, env=None, which=None, exists=None)
                       -> {'ok': True, 'items': [...], 'scanned': [...], 'elapsed_ms': int}

item 的 11 个键（前六个里的 command/args/workdir 直接进 profile）：
  id       'dsh-<profile>'，只含 [A-Za-z0-9_-]，全表唯一
  kind     'dsh'
  label    给人看的短名
  command  解析后的绝对路径
  args     ['--profile', '<profile>', '{prompt}']（没有 profile 时只有 ['{prompt}'])
  workdir  ''（继承当前进程 CWD）
  source   来源标签：which / appdata-npm / localappdata-asar
  profile  DSH profile 名（可能为空）
  version  probe=True 时的 --version 输出首行，否则 ''
  ok       条目本身是否可用
  reason   不可用的原因（或 probe 失败原因），正常为空
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Any, Optional

__all__ = ['PROMPT_PLACEHOLDER', 'PROBE_TIMEOUT', 'MAX_PROBE_CANDIDATES', 'ITEM_KEYS',
           'dsh_home', 'dsh_profiles', 'detect']

PROMPT_PLACEHOLDER = '{prompt}'
PROBE_TIMEOUT = 15.0
MAX_PROBE_CANDIDATES = 3
MAX_VERSION_LENGTH = 64
ITEM_KEYS = ('id', 'kind', 'label', 'command', 'args', 'workdir', 'source',
             'profile', 'version', 'ok', 'reason')

# profiles 目录下这些不是 profile（npm 依赖目录、隐藏目录、缓存）。
_SKIPPED_PROFILE_DIRS = frozenset({'node_modules', '__pycache__'})
# app.asar 里既可能有 dsh/ 目录，也可能有 dsh.cmd，两头都试。
_ENTRY_NAMES = ('dsh.cmd', 'dsh.exe', 'dsh')
_SHIM_SUFFIXES = ('.cmd', '.exe', '.bat')
_ID_SAFE = re.compile(r'[^A-Za-z0-9_-]+')


class _DetectError(Exception):
    """内部哨兵：只在探测流程内部用，对外一律收敛成 reason。"""


def _environ(env) -> Any:
    return os.environ if env is None else env


def _env_text(environ, name: str) -> str:
    try:
        raw = environ.get(name) if hasattr(environ, 'get') else ''
    except Exception:      # noqa: BLE001 - 注入的 env 千奇百怪，一律当空
        return ''
    return str(raw or '').strip()


def dsh_home(env=None) -> str:
    """DSH 的家目录：$DSH_HOME 优先，否则 ~/.dsh；取不到就返回 ''。"""
    raw = _env_text(_environ(env), 'DSH_HOME')
    try:
        if raw:
            return os.path.abspath(os.path.expanduser(raw))
        return os.path.abspath(os.path.join(os.path.expanduser('~'), '.dsh'))
    except (OSError, TypeError, ValueError):      # pragma: no cover - 极端路径
        return ''


def dsh_profiles(root) -> list:
    """<root>/profiles 下的 profile 目录名；headless 排第一，其余字母序。"""
    try:
        if not root or not str(root).strip():
            return []
        base = os.path.join(str(root), 'profiles')
        if not os.path.isdir(base):
            return []
        names = [name for name in os.listdir(base)
                 if name not in _SKIPPED_PROFILE_DIRS and not name.startswith('.')
                 and os.path.isdir(os.path.join(base, name))]
    except (OSError, TypeError, ValueError):
        return []
    ordered = sorted(set(names))
    if 'headless' in ordered:
        ordered.remove('headless')
        ordered.insert(0, 'headless')
    return ordered


def _resolve_entry(path: str, exists) -> str:
    """把候选路径归一成可执行文件：目录就在里面找入口，文件原样返回绝对路径。"""
    try:
        if not path or not exists(path):
            return ''
        if os.name == 'nt' and not os.path.splitext(path)[1]:
            # PATH 命中无扩展名的 npm bash 垫片时，CreateProcess 起不来
            # （WinError 193）：同目录有 .cmd/.exe/.bat 就用它。
            for ext in _SHIM_SUFFIXES:
                if exists(path + ext) and not os.path.isdir(path + ext):
                    return os.path.abspath(path + ext)
        if os.path.isdir(path):
            for name in _ENTRY_NAMES:
                child = os.path.join(path, name)
                if exists(child) and not os.path.isdir(child):
                    return os.path.abspath(child)
            return ''
        if os.path.isdir is not None and os.path.isdir(path):
            return ''
        return os.path.abspath(path)
    except (OSError, TypeError, ValueError):
        return ''


def _candidate_paths(env, which) -> list:
    """[(原始路径, 来源标签)]：PATH 命中 → npm 垫片 → asar 内置。"""
    entries = []
    try:
        found = shutil.which('dsh') if which is None else which('dsh')
    except Exception:      # noqa: BLE001 - which 自己炸了不能带崩探测
        found = None
    if found:
        entries.append((str(found), 'which'))
    environ = _environ(env)
    appdata = _env_text(environ, 'APPDATA')
    localappdata = _env_text(environ, 'LOCALAPPDATA')
    if appdata:
        for name in ('dsh.cmd', 'dsh'):
            entries.append((os.path.join(appdata, 'npm', name), 'appdata-npm'))
    if localappdata:
        asar = os.path.join(localappdata, 'Programs', 'DeepSeek Harness',
                            'resources', 'app.asar', 'dsh')
        entries.append((asar, 'localappdata-asar'))
        entries.append((asar + '.cmd', 'localappdata-asar'))
    return entries


def _item_id(profile: str, used: set) -> str:
    """dsh-<profile> 归一成 [A-Za-z0-9_-]{1,64}，并在全表里保持唯一。"""
    raw = _ID_SAFE.sub('-', 'dsh-%s' % (profile or 'default')).strip('-') or 'dsh-default'
    raw = raw[:64]
    candidate = raw
    index = 2
    while candidate in used:
        suffix = '-%d' % index
        candidate = raw[:64 - len(suffix)] + suffix
        index += 1
    used.add(candidate)
    return candidate


def _items_for(command: str, source: str, profiles, used: set) -> list:
    items = []
    for profile in (list(profiles) if profiles else ['']):
        args = (['--profile', profile, PROMPT_PLACEHOLDER] if profile
                else [PROMPT_PLACEHOLDER])
        items.append({'id': _item_id(profile, used), 'kind': 'dsh',
                      'label': 'DSH %s' % (profile or 'default'),
                      'command': command, 'args': args, 'workdir': '',
                      'source': source, 'profile': profile,
                      'version': '', 'ok': True, 'reason': ''})
    return items


def _probe_version(command: str) -> tuple:
    """真跑一次 <command> --version；返回 (version, reason)，永不抛。"""
    kwargs: dict = {'capture_output': True, 'text': True, 'encoding': 'utf-8',
                    'errors': 'replace', 'stdin': subprocess.DEVNULL,
                    'timeout': PROBE_TIMEOUT}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        done = subprocess.run([command, '--version'], **kwargs)
    except subprocess.TimeoutExpired:
        return '', 'version 调用超过 %gs' % PROBE_TIMEOUT
    except (OSError, ValueError) as exc:
        return '', 'version 调用失败：%s: %s' % (type(exc).__name__, exc)
    text = (done.stdout or '').strip() or (done.stderr or '').strip()
    line = text.splitlines()[0].strip() if text else ''
    version = line[:MAX_VERSION_LENGTH]
    if done.returncode != 0:
        return version, 'version 退出码 %s' % done.returncode
    return version, ''


def _detect(*, probe: bool, env, which, exists) -> dict:
    check = os.path.exists if exists is None else exists
    scanned: list = []
    commands: list = []

    def _scan(path: str) -> None:
        try:
            shown = os.path.abspath(path)
        except (OSError, TypeError, ValueError):
            shown = str(path or '')
        if shown and shown not in scanned:
            scanned.append(shown)

    for raw, source in _candidate_paths(env, which):
        _scan(raw)
        resolved = _resolve_entry(raw, check)
        if resolved and resolved.lower() not in [item[0].lower() for item in commands]:
            commands.append((resolved, source))

    home = dsh_home(env)
    profiles: list = []
    if home:
        _scan(home)
        _scan(os.path.join(home, 'profiles'))
        profiles = dsh_profiles(home)

    items: list = []
    used: set = set()
    for command, source in commands:
        items.extend(_items_for(command, source, profiles, used))

    if probe:
        probed: dict = {}
        for command, _source in commands[:MAX_PROBE_CANDIDATES]:
            probed[command] = _probe_version(command)
        for item in items:
            found = probed.get(item['command'])
            if found is None:
                continue
            version, problem = found
            item['version'] = version
            if problem and not item['reason']:
                item['reason'] = problem

    reason = ''
    if not items:
        parts = ['已扫描：%s' % ('、'.join(scanned) or '(无)'),
                 '没有找到可用的 DSH 入口（PATH / npm / app.asar 均未命中）']
        if not profiles:
            parts.append('profile 目录 %s 也没有读到 profile'
                         % (os.path.join(home, 'profiles') if home else '(未知)'))
        reason = '；'.join(parts)
    return {'ok': True, 'items': items, 'scanned': scanned, 'reason': reason}


def detect(*, probe: bool = False, env=None, which=None, exists=None) -> dict:
    """只读探测本机 DSH；永不抛，ok 恒 True。

    probe=False（默认）只做 exists/which 检查，不起任何进程；
    probe=True 才对最多 3 个命令真跑一次 --version 拿版本号。
    """
    started = time.monotonic()
    try:
        payload = _detect(probe=bool(probe), env=env, which=which, exists=exists)
    except Exception as exc:      # noqa: BLE001 - 契约：永不抛
        payload = {'ok': True, 'items': [], 'scanned': [],
                   'reason': '探测异常：%s: %s' % (type(exc).__name__, exc)}
    payload['elapsed_ms'] = int(round((time.monotonic() - started) * 1000))
    return payload
