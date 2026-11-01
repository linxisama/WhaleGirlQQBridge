"""把鲸鱼娘 MCP（记忆桥 + 控制桥 + 工具桥）注册到本地 DSH 的 profile（可逆、先备份）。

DSH 用 cordis 的 patch 列表扩展能力；三个桥都是 stdio MCP server，
注册后 DSH 模型会看到 mcp__whale_memory__*、mcp__whale_bot__* 与 mcp__whale_tools__*
三组工具。

这个模块只改一个 YAML 文件，规则：
- 改动前先备份；写入走「临时文件 + 原子替换」；替换失败回滚成备份内容；
- 幂等：内容没变化时不写文件、不产生多余备份；
- remove 可完全撤销三条登记，也可只撤指定的那一条；
- 登记一律写进 `- insert:` 块：DSH 的 patch 列表里，裸 `- id: X` 只能改「已存在」
  的行，找不到就打印 `patch: entry "X" not found` 并整行丢弃（新增必须用 insert）；
  旧版本写下的裸行在下次登记时搬进 insert 块。
"""
from __future__ import annotations

import copy
import os
import shutil
import time
from pathlib import Path

ENTRY_ID = 'mcp-whale-memory'
SERVER_NAME = 'whale_memory'
CONTROL_ENTRY_ID = 'mcp-whale-bot'
CONTROL_SERVER_NAME = 'whale_bot'
TOOLS_ENTRY_ID = 'mcp-whale-tools'
TOOLS_SERVER_NAME = 'whale_tools'
MCP_PACKAGE = '@deepseek-ai/dsh-mcp-client'
ENTRY_IDS = (ENTRY_ID, CONTROL_ENTRY_ID, TOOLS_ENTRY_ID)
# DSH patch 列表的新增语法：- insert: 后面跟一串待插入的行。
INSERT_KEY = 'insert'
BACKUP_PREFIX = '.bak-whale-memory-'
# stdio MCP 必须说 UTF-8。中文 Windows 的默认代码页是 cp936，DSH 按 UTF-8 解析
# initialize 失败后只会报 server unavailable（stderr 是空的，很难查），所以这里在
# 客户端侧也把 python 的 UTF-8 模式钉死，服务端自己再 reconfigure 一遍。
UTF8_ENV = {'PYTHONUTF8': '1'}
# DSH 的 MCP client 默认只重连 10 次（指数退避，约 5 分钟）就放弃并把工具摘掉；
# 控制台可能晚于 DSH 启动，或者中途重启，10 次不够用。把重连预算放大到约 8 小时，
# DSH 那边就会一直等控制台上线，而不是让用户重开 DSH。
RECONNECT = {
    'enabled': True,
    'initialDelayMs': 1000,
    'maxDelayMs': 60000,
    'maxAttempts': 500,
}


def default_dsh_home() -> Path:
    return Path.home() / '.dsh'


def profile_path(dsh_home, profile='desktop') -> Path:
    return Path(dsh_home) / 'profiles' / str(profile) / 'cordis.patch.yml'


# 两个控制台（安装版自动更新、开发机双开）可能同时改同一份 profile：yaml 的
# 「读-改-写」必须整段互斥，否则后写的那个会按它读到的旧内容把先写的整块覆盖掉。
# 原子替换只保证「不会读到写了一半的文件」，挡不住这种丢失更新。
LOCK_SUFFIX = '.whale.lock'
LOCK_TIMEOUT = 20.0


def lock_path(dsh_home, profile='desktop') -> Path:
    target = profile_path(dsh_home, profile)
    return target.with_name(target.name + LOCK_SUFFIX)


def _lock_try(handle) -> bool:
    """非阻塞抢锁：抢到 True，别人正占着 False（Windows 用 msvcrt，其余用 flock）。"""
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _lock_drop(handle) -> None:
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass


class profile_lock:
    """同一个 profile 的读-改-写互斥：跨进程（文件锁），等太久就报错而不是干等。"""

    def __init__(self, target, timeout=None):
        self.path = Path(str(target))
        self.timeout = LOCK_TIMEOUT if timeout is None else float(timeout)
        self.handle = None

    def __enter__(self):
        lock_file = self.path.with_name(self.path.name + LOCK_SUFFIX)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(lock_file, 'a+b')
        except OSError:
            # 连锁文件都建不出来（只读目录）：不换一种失败方式，让真正的写去报错。
            self.handle = None
            return self
        deadline = time.monotonic() + self.timeout
        while not _lock_try(self.handle):
            if time.monotonic() >= deadline:
                self._release()
                raise OSError('等 DSH 配置锁超时（%.0f 秒）：%s'
                              % (self.timeout, self.path))
            time.sleep(0.02)
        return self

    def __exit__(self, *_exc_info):
        self._release()
        return False

    def _release(self):
        if self.handle is not None:
            _lock_drop(self.handle)
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None


def _write_locked(func):
    """给写入口加锁：读 yaml → 改 → 落盘整段在一个跨进程锁里（每个 profile 一把）。"""
    import functools
    import inspect

    signature = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        with profile_lock(profile_path(bound.arguments.get('dsh_home'),
                                       bound.arguments.get('profile', 'desktop'))):
            return func(*args, **kwargs)

    return wrapper


def _entry_args(value) -> list:
    # 登记里的 args：字符串保持旧行为，list/tuple 原样逐项转字符串。
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def build_entry(python_executable, script_path, bot_home, args=None) -> dict:
    return {
        'id': ENTRY_ID,
        'name': MCP_PACKAGE,
        'config': {
            'serverName': SERVER_NAME,
            'transport': 'stdio',
            'command': str(python_executable),
            'args': _entry_args(script_path if args is None else args),
            'env': dict({'WHALE_QQ_HOME': str(bot_home)}, **UTF8_ENV),
            'toolCallTimeoutMs': 20000,
            'reconnect': dict(RECONNECT),
        },
    }


def build_control_entry(python_executable, script_path, bot_url, args=None) -> dict:
    return {
        'id': CONTROL_ENTRY_ID,
        'name': MCP_PACKAGE,
        'config': {
            'serverName': CONTROL_SERVER_NAME,
            'transport': 'stdio',
            'command': str(python_executable),
            'args': _entry_args(script_path if args is None else args),
            'env': dict({'WHALE_BOT_URL': str(bot_url)}, **UTF8_ENV),
            'toolCallTimeoutMs': 60000,
            'reconnect': dict(RECONNECT),
        },
    }


def build_tools_entry(python_executable, script_path, bot_home, args=None) -> dict:
    # 工具桥内部还要起 node 子进程跑扩展宿主，单次 tool call 比另两个桥慢得多，
    # 20s 会稳定超时，所以这里给 30s。
    return {
        'id': TOOLS_ENTRY_ID,
        'name': MCP_PACKAGE,
        'config': {
            'serverName': TOOLS_SERVER_NAME,
            'transport': 'stdio',
            'command': str(python_executable),
            'args': _entry_args(script_path if args is None else args),
            'env': dict({'WHALE_QQ_HOME': str(bot_home)}, **UTF8_ENV),
            'toolCallTimeoutMs': 30000,
            'reconnect': dict(RECONNECT),
        },
    }


def _load(path: Path) -> list:
    if not path.is_file():
        return []
    import yaml
    data = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f'{path} 不是 patch 列表，未改动')
    return data


def _save(path: Path, data: list):
    """写回 patch 列表：先备份、临时文件原子替换、失败回滚。返回备份路径（没备份则 None）。"""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.is_file() else None
    backup = None
    if original is not None:
        backup = path.with_name(path.name + BACKUP_PREFIX + time.strftime('%Y%m%d-%H%M%S'))
        shutil.copy2(path, backup)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    # 沿用原文件的换行风格，否则整份 DSH 配置会被 diff 成全文改动
    newline = '\r\n' if original and b'\r\n' in original else '\n'
    temporary = path.with_name(path.name + '.writing')
    try:
        temporary.write_text(text, encoding='utf-8', newline=newline)
        os.replace(temporary, path)
    except OSError:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        if original is not None:
            path.write_bytes(original)
        raise
    return backup


def _persist(path: Path, data: list, before: list) -> dict:
    """内容没变就不落盘（幂等），变了就登记备份路径。"""
    if data == before and path.is_file():
        return {'changed': False, 'backup': None}
    backup = _save(path, data)
    return {'changed': True, 'backup': str(backup) if backup else None}


def _entry_rows(data: list):
    """展开 patch 列表里的所有行：(所在的 insert 块, 下标, entry)；裸行的块是 None。

    insert 块内的行和顶层裸行都可能承载登记，判定/替换/卸载都得两种都看。
    """
    for position, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        nested = item.get(INSERT_KEY)
        if isinstance(nested, list):
            for index, row in enumerate(nested):
                yield item, index, row
        elif 'id' in item:
            yield None, position, item


def _pick(rows, entry_id: str):
    """从展开后的行里按 id 取第一条登记。"""
    return next((row for _, _, row in rows
                 if isinstance(row, dict) and row.get('id') == entry_id), None)


def _find_entry(data: list, entry_id: str):
    """按 id 找登记行：优先 insert 块内的，退而求其次才是裸行。返回 (块, 下标, entry)。"""
    bare = (None, None, None)
    for block, index, row in _entry_rows(data):
        if isinstance(row, dict) and row.get('id') == entry_id:
            if block is not None:
                return block, index, row
            bare = (None, index, row)
    return bare


def _insert_block(data: list) -> dict:
    """承载鲸鱼登记的 insert 块：已有就复用，没有就新建一个追加到末尾。

    只复用「已经装着鲸鱼登记」的块：DSH 自己的 insert 块（例如 dsh-base 的
    code-runtime）不塞我们的行，卸载时也不会被顺手删空。
    """
    for item in data:
        nested = item.get(INSERT_KEY) if isinstance(item, dict) else None
        if isinstance(nested, list) and any(
                isinstance(row, dict) and row.get('id') in ENTRY_IDS for row in nested):
            return item
    block = {INSERT_KEY: []}
    data.append(block)
    return block


def _drop_bare(data: list, entry_id: str) -> int:
    """删掉顶层裸 `- id: <entry_id>` 行（旧版本写的登记），返回删了几行。"""
    kept = [item for item in data
            if not (isinstance(item, dict) and INSERT_KEY not in item
                    and item.get('id') == entry_id)]
    dropped = len(data) - len(kept)
    data[:] = kept
    return dropped


def _reconnect_ready(entry) -> bool:
    """这条登记是否带上了够长的重连预算（旧版本没写 reconnect 就不算）。"""
    config = (entry or {}).get('config') or {}
    reconnect = config.get('reconnect') if isinstance(config, dict) else None
    if not isinstance(reconnect, dict) or not reconnect.get('enabled'):
        return False
    try:
        return (int(reconnect.get('maxAttempts') or 0) >= RECONNECT['maxAttempts']
                and int(reconnect.get('maxDelayMs') or 0) >= RECONNECT['maxDelayMs'])
    except (TypeError, ValueError):
        return False


def status(dsh_home, profile='desktop') -> dict:
    path = profile_path(dsh_home, profile)
    try:
        data = _load(path)
    except (ValueError, OSError) as exc:
        return {'path': str(path), 'installed': False, 'control_installed': False,
                'tools_installed': False, 'error': str(exc)}
    rows = list(_entry_rows(data))
    entry = _pick(rows, ENTRY_ID)
    control = _pick(rows, CONTROL_ENTRY_ID)
    tools_entry = _pick(rows, TOOLS_ENTRY_ID)
    control_config = (control or {}).get('config') or {}
    control_env = control_config.get('env') or {}
    control_env = control_env if isinstance(control_env, dict) else {}
    memory_config = (entry or {}).get('config') or {}
    memory_env = memory_config.get('env') or {}
    memory_env = memory_env if isinstance(memory_env, dict) else {}
    tools_config = (tools_entry or {}).get('config') or {}
    tools_env = tools_config.get('env') or {}
    tools_env = tools_env if isinstance(tools_env, dict) else {}
    return {'path': str(path), 'installed': entry is not None,
            'serverName': (entry or {}).get('config', {}).get('serverName'),
            'control_installed': control is not None,
            'controlServerName': (control or {}).get('config', {}).get('serverName'),
            'memory_command': memory_config.get('command', ''),
            'memory_args': list(memory_config.get('args') or []),
            'control_command': control_config.get('command', ''),
            'control_script': (control_config.get('args') or [''])[0],
            'control_args': list(control_config.get('args') or []),
            'control_utf8_env': str(control_env.get('PYTHONUTF8') or '') == '1',
            'memory_qq_home': str(memory_env.get('WHALE_QQ_HOME') or ''),
            'tools_installed': tools_entry is not None,
            'toolsServerName': tools_config.get('serverName'),
            'tools_command': tools_config.get('command', ''),
            'tools_script': (tools_config.get('args') or [''])[0],
            'tools_args': list(tools_config.get('args') or []),
            'tools_qq_home': str(tools_env.get('WHALE_QQ_HOME') or ''),
            'tools_utf8_env': str(tools_env.get('PYTHONUTF8') or '') == '1',
            'tools_timeout_ms': tools_config.get('toolCallTimeoutMs'),
            # 三条都带着足够长的重连预算，DSH 才会在控制台晚启动/重启后继续等。
            'reconnect_ok': all(_reconnect_ready(item) for item in (entry, control, tools_entry)),
            'entry_count': len(rows),
            # 形状自检：登记必须在 insert 块里，裸行会被 DSH 静默丢弃。
            'insert_blocks': sum(1 for item in data
                                 if isinstance(item, dict)
                                 and isinstance(item.get(INSERT_KEY), list)),
            'bare_entry_ids': [str(row.get('id')) for block, _, row in rows
                               if block is None and isinstance(row, dict)
                               and row.get('id') in ENTRY_IDS]}


def install(dsh_home, python_executable, script_path, bot_home, profile='desktop') -> dict:
    path = profile_path(dsh_home, profile)
    data = _load(path)
    before = copy.deepcopy(data)
    entry = build_entry(python_executable, script_path, bot_home)
    replaced = _install_entry(data, entry, ENTRY_ID)
    result = {'path': str(path), 'replaced': replaced, 'installed': True,
              'serverName': SERVER_NAME, 'entry': entry}
    result.update(_persist(path, data, before))
    return result


def _install_entry(data: list, entry: dict, entry_id: str) -> bool:
    """登记一条（幂等）：已存在就原地替换；旧版本的裸行搬进 insert 块。

    返回 True 表示这条原本就在（替换或迁移），False 表示这次新增。
    """
    block, index, row = _find_entry(data, entry_id)
    known = row is not None
    if block is not None:
        block[INSERT_KEY][index] = entry
    else:
        _insert_block(data)[INSERT_KEY].append(entry)
    if known:
        _drop_bare(data, entry_id)
    return known


def install_control(dsh_home, python_executable, script_path, bot_url, profile='desktop') -> dict:
    path = profile_path(dsh_home, profile)
    data = _load(path)
    before = copy.deepcopy(data)
    entry = build_control_entry(python_executable, script_path, bot_url)
    replaced = _install_entry(data, entry, CONTROL_ENTRY_ID)
    result = {'path': str(path), 'replaced': replaced, 'installed': True,
              'serverName': CONTROL_SERVER_NAME, 'entry': entry}
    result.update(_persist(path, data, before))
    return result


def install_tools(dsh_home, python_executable, script_path, bot_home, profile='desktop') -> dict:
    """登记第三条（工具桥 whale_tools），追加在列表末尾。"""
    path = profile_path(dsh_home, profile)
    data = _load(path)
    before = copy.deepcopy(data)
    entry = build_tools_entry(python_executable, script_path, bot_home)
    replaced = _install_entry(data, entry, TOOLS_ENTRY_ID)
    result = {'path': str(path), 'tools_installed': True, 'tools_replaced': replaced,
              'toolsServerName': TOOLS_SERVER_NAME, 'toolsEntry': entry}
    result.update(_persist(path, data, before))
    return result


def install_all(dsh_home, python_executable, memory_script, bot_home,
                control_script, bot_url, profile='desktop', tools_script=None,
                memory_args=None, control_args=None, tools_args=None) -> dict:
    """一次写好三条登记（记忆桥 + 控制桥 + 工具桥），共用一次备份；重复调用幂等。

    tools_script 不给时只写前两条，保持旧调用点的行为不变。
    """
    path = profile_path(dsh_home, profile)
    data = _load(path)
    before = copy.deepcopy(data)
    memory = build_entry(python_executable, memory_script, bot_home,
                         args=memory_args)
    control = build_control_entry(python_executable, control_script, bot_url,
                                  args=control_args)
    memory_replaced = _install_entry(data, memory, ENTRY_ID)
    control_replaced = _install_entry(data, control, CONTROL_ENTRY_ID)
    result = {'path': str(path), 'installed': True, 'serverName': SERVER_NAME,
              'replaced': memory_replaced, 'entry': memory,
              'control_installed': True, 'controlServerName': CONTROL_SERVER_NAME,
              'control_replaced': control_replaced, 'controlEntry': control}
    if tools_script is not None:
        tools = build_tools_entry(python_executable, tools_script, bot_home,
                                  args=tools_args)
        tools_replaced = _install_entry(data, tools, TOOLS_ENTRY_ID)
        result.update({'tools_installed': True, 'toolsServerName': TOOLS_SERVER_NAME,
                       'tools_replaced': tools_replaced, 'toolsEntry': tools})
    result.update(_persist(path, data, before))
    return result


def remove(dsh_home, profile='desktop', only=None) -> dict:
    """撤销登记：only 为空撤三条；only 给 entry id（或它们的列表）只撤指定的那些。"""
    targets = _target_entry_ids(only)
    path = profile_path(dsh_home, profile)
    data = _load(path)
    before = copy.deepcopy(data)
    dropped = _drop_entries(data, targets)
    result = {'path': str(path), 'removed': bool(dropped), 'targets': sorted(targets)}
    if data != before:
        result.update(_persist(path, data, before))
    else:
        result.update({'changed': False, 'backup': None})
    return result


def _drop_entries(data: list, targets: set) -> int:
    """删掉 targets 里各 id 的登记（裸行与 insert 块内的都算），返回删了几条。

    被删空的 insert 块一并去掉，块里其它行（别人的登记）原样保留。
    """
    dropped = 0
    kept_items = []
    for item in data:
        nested = item.get(INSERT_KEY) if isinstance(item, dict) else None
        if isinstance(nested, list):
            kept_rows = [row for row in nested
                         if not (isinstance(row, dict) and row.get('id') in targets)]
            dropped += len(nested) - len(kept_rows)
            if kept_rows:
                kept_items.append(dict(item, **{INSERT_KEY: kept_rows}))
            continue
        if isinstance(item, dict) and item.get('id') in targets:
            dropped += 1
            continue
        kept_items.append(item)
    data[:] = kept_items
    return dropped


def _target_entry_ids(only) -> set:
    if only is None:
        return set(ENTRY_IDS)
    wanted = {only} if isinstance(only, str) else set(only)
    unknown = sorted(item for item in wanted if item not in ENTRY_IDS)
    if unknown:
        raise ValueError(f'未知的登记 id: {", ".join(map(str, unknown))}')
    return wanted


# 所有写入口都套上跨进程锁（含 setup_dsh_memory.py 走的那几个单条写入口）：
# 单条写也一样是「读-改-写」，不互斥就会互相覆盖。
install = _write_locked(install)
install_control = _write_locked(install_control)
install_tools = _write_locked(install_tools)
install_all = _write_locked(install_all)
remove = _write_locked(remove)
