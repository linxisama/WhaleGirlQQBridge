"""QQ Agent 0.4 扩展宿主的落盘布局：解析 / 制备 / 装扩展 / 真加载探测。

为什么需要这一层：市场里 512 个扩展中有 338 个的 index.js 用 `../../src/config.js` 这类
相对导入。相对导入的落点由**安装位置**决定，不是由扩展本身决定——只要扩展装在
`<host_root>/plugins/<id>/`、host_root 里带着 0.4 的 src/ 与 node_modules/，
`../../src/config.js` 就解析到 `<host_root>/src/config.js`，导入成立。
本模块负责把这样一个 host_root 造出来、把扩展装进去、并真跑一次 node 加载来给出证据。

纯标准库；执行原语复用 extension_host（run_capability_sync），本模块不自己拼 argv。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import extension_host

ROOT = Path(__file__).resolve().parents[1]

# host_root 的判定条件：0.4 的配置与插件加载器都在，才算一个能解析 `../../src/*` 的根。
HOST_MARKERS = ('src/config.js', 'src/plugin-loader.js')
# 制备 host_root 时要复制的最小集合（约 4MB：src 1.0MB + node_modules 2.9MB）。
HOST_COPY_ENTRIES = ('src', 'node_modules', 'package.json')
# host-one.mjs 是单扩展隔离加载脚本，host.mjs 是整树清单脚本；两者都放在 host_root 根。
HOST_SCRIPTS = ('host-one.mjs', 'host.mjs')
# 根内的运行时指纹：记下这份根是从哪份载荷复制的，载荷一改就刷新。
# 只取 size 与 sha256——mtime 会被打包/解包/检出打乱，不能当内容证据。
STAMP_NAME = '.runtime-stamp.json'
STAMP_VERSION = 1
STAMP_HASH_FILES = ('host-one.mjs', 'host.mjs', 'package.json')
# 兼容层目录：随 512 扩展一起分发的脚本来源。环境变量可覆盖。
COMPAT_DIR_ENV = 'QQAGENT_COMPAT_DIR'
# 第一项是开发机的绝对路径，发布包里必然不命中——这是**预期**，不是故障：用户机器上的
# 运行时来自随包载荷 RUNTIME_PAYLOAD_DIR（见下方），这里只是开发机上顺手的候选。
COMPAT_DIR_CANDIDATES = (Path(r'D:\聊天机器人\插件市场\QQAgent兼容层'), ROOT / 'QQAgent兼容层')
DEV_HOST_CANDIDATES = (ROOT / '.tmp' / 'qq-agent-run', ROOT / 'qq-agent-run')

# 随产品分发的运行时载荷（仓库内自带 src/ + node_modules/ + 两个脚本）。
# 用户机器上通常只有「兼容层目录 + 仓库」：兼容层只有 host-one.mjs / host.mjs，
# 缺 src/ 就解析不了 ../../src/*，所以这份载荷才是发布环境的第一优先 source。
RUNTIME_PAYLOAD_DIR = ROOT / 'runtime' / 'qqagent-host'
RUNTIME_SOURCE_ENV = 'QQAGENT_HOST_RUNTIME'
# 随包 Node：干净机器上常常根本没装 Node，扩展装得上却加载不了。
# 安装版把 runtime/node/node.exe（含 LICENSE）一起带走，源码树里则是显式放进去的副本。
NODE_ENV = 'QQAGENT_NODE'
BUNDLED_NODE_DIR = ROOT / 'runtime' / 'node'
NODE_EXE_NAMES = ('node.exe', 'node')
# 兼容层目录将来若带运行时，放在这个子目录里（现在没有，只是候选位置）。
COMPAT_RUNTIME_SUBDIR = 'host-runtime'

# 只读基线：路径存在也不许写入（宿主脚本会往 source 下建 plugins-host-<pid>/）。
PROTECTED_ROOTS = (ROOT / '.tmp' / 'qq-agent-run',)

PLUGIN_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
HOST_IMPORT_RE = re.compile(r'''(?:from\s*|import\s*\(\s*)['"]((?:\.\./)+src/[^'"]+)['"]''')
HOOK_KEY_RE = re.compile(r'''hooks\s*=\s*\{([^}]*)\}''')
PROVIDER_KEY_RE = re.compile(r'''providers\s*=\s*\{([^}]*)\}''')
IDENT_KEY_RE = re.compile(r'''['"]?([A-Za-z_$][\w$.]*)['"]?\s*:''')

# 解包闸门：与 plugin_installer.MAX_ARCHIVE_* 同口径（24MB 总量）。
MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
MAX_ARCHIVE_FILES = 4000
MAX_MANIFEST_BYTES = 256 * 1024
#: 宿主根里记「安装时真加载过什么」的文件名（静态读 plugin.json 猜不准工具名）。
PROBE_CACHE_NAME = 'probes.json'
SCAN_FILE_BYTES = 1024 * 1024

PROBE_MARKER = extension_host.MARKER


# ── host_root 的判定与解析 ───────────────────────────────────────────────────
def _as_path(value) -> Optional[Path]:
    """统一成绝对路径：node 子进程的 cwd 是 host_root，相对路径会被拼两遍。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except OSError:
        return Path(text).expanduser()


def host_root_problems(root) -> List[str]:
    """返回缺失的判定文件（空列表 = 是一个可用 host_root）。"""
    path = _as_path(root)
    if path is None:
        return ['host_root 为空']
    if not path.is_dir():
        return ['host_root 不是目录：%s' % path]
    return [name for name in HOST_MARKERS if not (path / name).is_file()]


def is_host_root(root) -> bool:
    return not host_root_problems(root)


def default_host_root(data_dir=None) -> Path:
    """控制台自己持有的落点：``<data>/qq-only/qqagent_host``。

    调用方传进来的都是 ``PluginManager.data_dir``，它本身已经是 ``<data>/qq-only``；
    不剥掉这层就会落到 ``<data>/qq-only/qq-only/qqagent_host``：市场扩展装在一层、
    catalog 按同一层读，能力桥永远发现不到刚装好的扩展。
    """
    base = _as_path(data_dir) or (ROOT / 'data')
    if base.name == 'qq-only':
        base = base.parent
    return base / 'qq-only' / 'qqagent_host'


# 宿主自己的数据目录名：host-one.mjs 把 QQ_AGENT_DATA_DIR 缺省成 ``<source>/data-host-one``，
# 而 src/config.js 的 CONFIG_FILE 就是 ``<DATA_DIR>/config.json``。
HOST_DATA_DIR_NAME = 'data-host-one'


def host_config_path(host_root) -> Optional[Path]:
    """宿主开关文件：``<host_root>/data-host-one/config.json``（不存在也返回路径）。"""
    root = _as_path(host_root)
    if root is None:
        return None
    return root / HOST_DATA_DIR_NAME / 'config.json'


def read_host_config(host_root) -> Dict[str, Any]:
    path = host_config_path(host_root)
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


#: 各扩展声明「主人/管理员号码」用的键名：作者叫法不统一，宿主只把配置喂给扩展，
#: 号码写在扩展自己的 namespace 里才算数（api.config() = config.skills[id]）。
OWNER_SETTING_KEYS = ('ids', 'ownerIds', 'ownerId', 'owners', 'owner', 'masters', 'admins')


def _write_host_skill(host_root, extension_id, patch, only_missing=False) -> Dict[str, Any]:
    """合并若干配置进宿主 config.json 的 ``skills.<id>``（宿主唯一的扩展配置入口）。

    读改写必须原子（tmp + os.replace）：宿主进程可能正读到一半。
    only_missing=True 只补空值，别人已经写好的一律不动。
    返回 {ok, path, id, written, error}，不抛异常。
    """
    ident = safe_extension_id(extension_id)
    path = host_config_path(host_root)
    outcome = {'ok': False, 'path': str(path or ''), 'id': ident, 'written': {},
               'error': ''}
    if path is None:
        outcome['error'] = '缺少 host_root，不写宿主配置'
        return outcome
    if not ident:
        outcome['error'] = '扩展 id 非法：%s' % extension_id
        return outcome
    config = read_host_config(host_root)
    skills = config.get('skills')
    if not isinstance(skills, dict):
        skills = {}
    entry = skills.get(ident)
    entry = dict(entry) if isinstance(entry, dict) else {}
    changed: Dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if only_missing and str(entry.get(key) or '').strip():
            continue
        if entry.get(key) == value:
            continue
        entry[key] = value
        changed[key] = value
    if not changed:
        outcome['ok'] = True
        return outcome
    skills[ident] = entry
    config['skills'] = skills
    tmp = path.with_name(path.name + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n',
                       encoding='utf-8')
        os.replace(tmp, path)
    except OSError as exc:
        outcome['error'] = str(exc)
        return outcome
    outcome['ok'] = True
    outcome['written'] = changed
    return outcome


def set_extension_enabled(host_root, extension_id, enabled=True) -> Dict[str, Any]:
    """把启用态写进宿主自己的 config.json —— 宿主只认这里的 ``skills.<id>.enabled``。

    宿主侧「写 false」和「根本没写」是两种状态：没写时才回退清单里的 enabledByDefault，
    而市场扩展几乎都不声明它，于是「装好了但没写」= 宿主拒绝加载（extension-disabled）。
    """
    result = _write_host_skill(host_root, extension_id, {'enabled': bool(enabled)})
    result['enabled'] = bool(enabled)
    return result


def owner_setting_keys(manifest) -> List[str]:
    """这份清单里代表「主人号码」的配置键（settings 默认值与 configSchema 都算）。"""
    keys: List[str] = []
    for source in ((manifest or {}).get('settings'), (manifest or {}).get('configSchema')):
        if not isinstance(source, dict):
            continue
        for key in source:
            if key in OWNER_SETTING_KEYS and key not in keys:
                keys.append(key)
    return keys


def seed_owner_settings(host_root, extension_id, owner_ids, manifest=None) -> Dict[str, Any]:
    """把「主人 QQ」写进这份扩展自己的配置项，别让主人识别停在「还没填主人 QQ 号」。

    宿主把配置按扩展分命名空间喂进去（api.config() = config.skills[id]），所以号码必须
    落在宿主配置里；不同作者叫 ids / ownerIds / owners，按清单声明的键对齐。
    只补空值：已经配过的号码优先于账号绑定里的。
    """
    values: List[str] = []
    for item in (owner_ids if isinstance(owner_ids, (list, tuple, set)) else [owner_ids]):
        text = str(item or '').strip()
        if text and text not in values:
            values.append(text)
    keys = owner_setting_keys(manifest)
    outcome: Dict[str, Any] = {'ok': False, 'id': safe_extension_id(extension_id),
                               'keys': keys, 'ids': values, 'written': {}, 'error': ''}
    if not values or not keys:
        outcome['ok'] = True
        return outcome
    patch = {key: ','.join(values) for key in keys}
    written = _write_host_skill(host_root, extension_id, patch, only_missing=True)
    outcome['ok'] = bool(written.get('ok'))
    outcome['written'] = dict(written.get('written') or {})
    outcome['error'] = str(written.get('error') or '')
    return outcome


def compat_dir() -> Optional[Path]:
    override = _as_path(os.environ.get(COMPAT_DIR_ENV))
    for candidate in ([override] if override else []) + list(COMPAT_DIR_CANDIDATES):
        if candidate.is_dir():
            return candidate
    return None


def protected_roots() -> List[Path]:
    """不许写入的只读基线（判定按真实路径，避开符号链接绕过）。"""
    result: List[Path] = []
    for path in PROTECTED_ROOTS:
        try:
            result.append(path.resolve())
        except OSError:
            continue
    return result


def is_protected(root) -> bool:
    path = _as_path(root)
    if path is None:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for guard in protected_roots():
        if resolved == guard or guard in resolved.parents:
            return True
    return False


def find_host_script(root=None) -> Optional[Path]:
    """先在 host_root 里找，再退回兼容层目录。"""
    path = _as_path(root)
    if path is not None:
        for name in HOST_SCRIPTS[:1]:
            candidate = path / name
            if candidate.is_file():
                return candidate
    folder = compat_dir()
    if folder is not None:
        for name in HOST_SCRIPTS[:1]:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    return None


def resolve_host_root(explicit=None, data_dir=None) -> Optional[Path]:
    """按优先级给出一个可用 host_root：显式 > 控制台自持 > 开发态 .tmp/qq-agent-run。

    任何一个候选不满足 HOST_MARKERS 就跳过它继续往下走——"能解析 ../../src/*" 是
    唯一有效标准，路径存在但缺 src/ 的目录不能算宿主根。
    """
    candidates: List[Any] = [explicit, default_host_root(data_dir)]
    candidates.extend(DEV_HOST_CANDIDATES)
    for candidate in candidates:
        path = _as_path(candidate)
        if path is not None and is_host_root(path):
            return path
    return None



# ── source 候选与宿主根状态 ──────────────────────────────────────────────────
def plugins_dir(host_root) -> Path:
    """扩展的安装落点：<host_root>/plugins。不存在就建，调用方不必自己 mkdir。"""
    path = _as_path(host_root)
    if path is None:
        raise ValueError('host_root 为空')
    folder = path / 'plugins'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def runtime_source_candidates(extra=None) -> List[Path]:
    """能当宿主根用的 source，按优先级排：显式 > 环境变量 > 随包载荷 > 兼容层 > 开发态。

    每个候选都要过 HOST_MARKERS：路径存在但缺 src/ 的目录解析不了 `../../src/*`，
    兼容层目录正是这种（只有两个脚本），它只能提供脚本，不能当宿主根。
    """
    ordered: List[Any] = []
    if isinstance(extra, (list, tuple, set)):
        ordered.extend(list(extra))
    elif extra is not None:
        ordered.append(extra)
    override = os.environ.get(RUNTIME_SOURCE_ENV)
    if override:
        ordered.append(override)
    ordered.append(RUNTIME_PAYLOAD_DIR)
    folder = compat_dir()
    if folder is not None:
        ordered.append(folder / COMPAT_RUNTIME_SUBDIR)
    ordered.append(ROOT / '.tmp' / 'qq-agent-run')
    result: List[Path] = []
    for candidate in ordered:
        path = _as_path(candidate)
        if path is None or path in result or not is_host_root(path):
            continue
        result.append(path)
    return result


def host_root_ready(root) -> bool:
    """可用宿主根 = 判定文件 + package.json + node_modules 全在（复制用的那套判据）。"""
    path = _as_path(root)
    return path is not None and not _source_problems(path)


def host_root_stats(root) -> Dict[str, int]:
    """数一下根里有几个文件、共多少字节（只用于展示，不参与判定）。"""
    path = _as_path(root)
    stats = {'files': 0, 'bytes': 0}
    if path is None or not path.is_dir():
        return stats
    for item in path.rglob('*'):
        try:
            if not item.is_file():
                continue
            stats['bytes'] += item.stat().st_size
            stats['files'] += 1
        except OSError:
            continue
    return stats


# ── 运行时指纹：这份根还是不是当前载荷复制的 ─────────────────────────────────
def _sha256_file(path) -> str:
    """整文件 sha256；读不了返回空串（指纹缺一项就当变了，只多刷一次）。"""
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(256 * 1024), b''):
                digest.update(chunk)
    except OSError:
        return ''
    return digest.hexdigest()


def payload_stamp(source) -> Dict[str, Any]:
    """源载荷指纹：复制集合逐文件 size，外加两个脚本与 package.json 的 sha256。

    为什么按 size 不看 mtime：解包/检出的 mtime 不可信；为什么还要 sha256：
    脚本改了但字节数不变时 size 认不出来，而 host-one.mjs 正是所有扩展的加载口。
    """
    origin = _as_path(source)
    stamp: Dict[str, Any] = {'version': STAMP_VERSION, 'files': {}, 'hashes': {},
                             'count': 0, 'bytes': 0}
    if origin is None or not origin.is_dir():
        return stamp
    files: Dict[str, int] = {}
    for entry in HOST_COPY_ENTRIES:
        item = origin / entry
        if item.is_dir():
            for leaf in sorted(item.rglob('*')):
                try:
                    if not leaf.is_file():
                        continue
                    files[leaf.relative_to(origin).as_posix()] = int(leaf.stat().st_size)
                except OSError:
                    continue
        elif item.is_file():
            try:
                files[entry] = int(item.stat().st_size)
            except OSError:
                pass
    hashes: Dict[str, str] = {}
    for name in STAMP_HASH_FILES:
        candidate = origin / name
        if candidate.is_file():
            hashes[name] = _sha256_file(candidate)
    stamp['files'] = files
    stamp['hashes'] = hashes
    stamp['count'] = len(files)
    stamp['bytes'] = sum(files.values())
    return stamp


def stamp_matches(previous, current) -> bool:
    """两份指纹是否同源：只比 version/files/hashes（count/bytes 由 files 推出来）。"""
    left = previous if isinstance(previous, dict) else {}
    right = current if isinstance(current, dict) else {}
    if not left or not right:
        return False
    if int(left.get('version') or 0) != int(right.get('version') or 0):
        return False
    if dict(left.get('files') or {}) != dict(right.get('files') or {}):
        return False
    return dict(left.get('hashes') or {}) == dict(right.get('hashes') or {})


def stamp_diff(previous, current) -> List[str]:
    """变了的字段名（相对路径或脚本名）；无旧指纹只报一个标记，不倾家荡产列 273 行。

    同一文件既进 files 又进 hashes（脚本同时被记录大小与哈希），只报一次。
    """
    left = previous if isinstance(previous, dict) else {}
    right = current if isinstance(current, dict) else {}
    if not left:
        return ['(无旧指纹)'] if right else []
    changes: List[str] = []
    if int(left.get('version') or 0) != int(right.get('version') or 0):
        changes.append('version')
    old_files = dict(left.get('files') or {})
    new_files = dict(right.get('files') or {})
    changed = {str(name) for name in set(old_files) | set(new_files)
               if old_files.get(name) != new_files.get(name)}
    old_hash = dict(left.get('hashes') or {})
    new_hash = dict(right.get('hashes') or {})
    changed |= {str(name) for name in set(old_hash) | set(new_hash)
                if old_hash.get(name) != new_hash.get(name)}
    changes.extend(sorted(changed))
    return changes


def runtime_stamp(host_root) -> Dict[str, Any]:
    """读根内指纹；没有、坏 JSON、不是 dict 一律当"无指纹"（于是走 stale 刷新，不抛）。"""
    path = _as_path(host_root)
    if path is None:
        return {}
    try:
        raw = json.loads((path / STAMP_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_stamp(host_root, stamp) -> bool:
    """写指纹：先写临时文件再替换，半截 JSON 不会被下次读到；写不了只是下次再刷一次。"""
    path = _as_path(host_root)
    if path is None or not isinstance(stamp, dict) or not stamp:
        return False
    target = path / STAMP_NAME
    try:
        temp = target.with_name(STAMP_NAME + '.tmp')
        temp.write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(str(temp), str(target))
    except OSError:
        return False
    return True

# ── 制备 host_root ───────────────────────────────────────────────────────────
def _source_problems(source: Path) -> List[str]:
    problems = host_root_problems(source)
    if not (source / 'package.json').is_file():
        problems.append('package.json')
    if not (source / 'node_modules').is_dir():
        problems.append('node_modules')
    return problems


def _same_file(left: Path, right: Path) -> bool:
    """内容级同源：先比大小，再比 sha256。

    只比大小会漏掉"字节数一样但内容改了"的载荷（host-one.mjs 的参数位修复正是这类），
    所以刷新走内容比较：真正变了的才重写，其余一个字节都不动。
    """
    try:
        if not left.is_file() or not right.is_file():
            return False
        if left.stat().st_size != right.stat().st_size:
            return False
    except OSError:
        return False
    return _sha256_file(left) == _sha256_file(right)


def _copy_tree(source: Path, target: Path, counters: Dict[str, Any]) -> None:
    """按文件复制；目标已存在且内容同源的计入 skipped（幂等 / 可重入）。

    这里只覆盖复制集合里的文件，从不删除目标目录里的其它东西（plugins/ 因此安全）。
    """
    for item in sorted(source.rglob('*')):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file():
            continue
        if _same_file(destination, item):
            counters['skipped'] += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        counters['copied'] += 1
        counters.setdefault('copied_files', []).append(relative.as_posix())


def _copy_scripts(target: Path, counters: Dict[str, Any], source=None) -> List[str]:
    """取 host-one.mjs / host.mjs：先看被复制的 source（随包载荷自带），再退回兼容层。"""
    folders: List[Path] = []
    origin = _as_path(source)
    if origin is not None:
        folders.append(origin)
    folder = compat_dir()
    if folder is not None and folder not in folders:
        folders.append(folder)
    copied: List[str] = []
    for name in HOST_SCRIPTS:
        destination = target / name
        for holder in folders:
            candidate = holder / name
            if not candidate.is_file():
                continue
            if _same_file(destination, candidate):
                counters['skipped'] += 1
                break
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, destination)
            counters['copied'] += 1
            counters.setdefault('copied_files', []).append(name)
            copied.append(name)
            break
    return copied


_SMOKE_CODE = (
    "import { pathToFileURL } from 'node:url';"
    "const mod = await import(pathToFileURL(process.argv[1]).href);"
    "console.log('%s' + JSON.stringify({ ok: true, exports: Object.keys(mod).length }));"
) % PROBE_MARKER


def _looks_like_path(value) -> bool:
    """显式值是「路径」还是「命令名」：决定找不到时是报错还是回退自动发现。"""
    text = str(value or '').strip().strip('"')
    if not text:
        return False
    return ('/' in text or '\\' in text or ':' in text
            or text.lower().endswith(('.exe', '.cmd', '.bat')))


def _is_stale_bundled_node(value) -> bool:
    """旧安装目录里的随包 Node：重装/换目录后路径失效，应让自动发现接手。"""
    try:
        path = Path(str(value or '').strip().strip('"'))
    except (OSError, ValueError):
        return False
    names = {name.lower() for name in NODE_EXE_NAMES}
    if path.name.lower() not in names:
        return False
    return (path.parent.name.lower() == 'node'
            and path.parent.parent.name.lower() == 'runtime')


def _node_executable(value) -> Optional[str]:
    """一个候选值 -> 可执行文件路径；接受 node.exe、含空格的完整路径和裸命令名。"""
    raw = str(value or '').strip().strip('"')
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        if path.is_file():
            return str(path)
    except OSError:
        pass
    found = shutil.which(raw)
    return str(found) if found and 'windowsapps' not in str(found).lower() else None


def _common_node_paths() -> Tuple[Path, ...]:
    """常见安装位置；PATH 里只有 WindowsApps 商店桩时靠它兜底。"""
    env = os.environ
    folders: List[Path] = []

    def add(value) -> None:
        if not value:
            return
        try:
            folders.append(Path(value))
        except (TypeError, ValueError):
            return

    if os.name == 'nt':
        for key in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
            base = env.get(key)
            if base:
                add(Path(base) / 'nodejs')
        local = env.get('LOCALAPPDATA')
        if local:
            local_path = Path(local)
            add(local_path / 'Programs' / 'nodejs')
            add(local_path / 'Volta' / 'bin')
            add(local_path / 'Microsoft' / 'WinGet' / 'Links')
            packages = local_path / 'Microsoft' / 'WinGet' / 'Packages'
            try:
                for child in packages.glob('OpenJS.NodeJS*'):
                    add(child)
            except OSError:
                pass
        profile = env.get('USERPROFILE')
        if profile:
            profile_path = Path(profile)
            add(profile_path / 'scoop' / 'apps' / 'nodejs' / 'current')
            add(profile_path / 'scoop' / 'apps' / 'nodejs-lts' / 'current')
        add(env.get('NVM_SYMLINK'))
        add(Path('C:/ProgramData/chocolatey/bin'))
    else:
        for value in ('/usr/local/bin', '/usr/bin', '/opt/homebrew/bin', '/opt/local/bin'):
            add(value)
    return tuple(folders)


def _node_candidates(node=None) -> Iterable[Tuple[str, str]]:
    """按优先级给出 (路径, 来源)；来源用于状态面板，不猜。"""
    seen = set()

    def accept(value, source: str) -> Optional[Tuple[str, str]]:
        path = _node_executable(value)
        if not path:
            return None
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return None
        seen.add(key)
        return path, source

    if node:
        # 显式路径写错时不回退，要让状态里看见这条错路径；但历史配置里存的
        # 是裸命令名 'node'，干净机器上 PATH 里没有它时必须继续找随包 Node。
        path = _node_executable(node)
        if path:
            yield (path, 'argument')
            return
        if _looks_like_path(node) and not _is_stale_bundled_node(node):
            yield (str(node).strip().strip('"'), 'argument')
            return
    env_node = os.environ.get(NODE_ENV)
    if env_node:
        item = accept(env_node, 'env')
        if item:
            yield item
    for name in NODE_EXE_NAMES:
        item = accept(BUNDLED_NODE_DIR / name, 'bundled')
        if item:
            yield item
    for name in ('node', 'node.exe'):
        found = shutil.which(name)
        if not found or 'windowsapps' in str(found).lower():
            continue
        item = accept(found, 'path')
        if item:
            yield item
    for folder in _common_node_paths():
        for name in NODE_EXE_NAMES:
            item = accept(folder / name, 'common')
            if item:
                yield item


def node_info(node=None) -> Dict[str, Any]:
    """Node 现状：path/source/error 一次给全，HTTP 与安装链共用这一份。"""
    stale = None
    if node:
        text = str(node).strip().strip('"')
        path = _node_executable(text)
        if path:
            return {'ok': True, 'path': path, 'source': 'argument', 'error': None}
        if _looks_like_path(text):
            # 迁过来的配置可能指着上一台机器的路径：不回退到随包 Node，
            # 否则状态显示正常、设置里却留着一条坏路径。但旧安装目录里的
            # runtime\node\node.exe 是升级/重装后必然失效的路径，交给自动发现。
            if not _is_stale_bundled_node(text):
                return {
                    'ok': False,
                    'path': '',
                    'source': 'argument',
                    'error': {
                        'code': 'node-invalid',
                        'message': '指定的 Node 不存在或不可执行：%s；清空这项或改指向 node.exe' % (text or node),
                    },
                }
            stale = text
    for path, source in _node_candidates(node):
        info = {'ok': True, 'path': path, 'source': source, 'error': None}
        if stale:
            info['stale'] = stale
        return info
    return {
        'ok': False,
        'path': '',
        'source': '',
        'error': {
            'code': 'node-missing',
            'message': ('找不到 Node 20+：找过随包 runtime/node、PATH、常见安装目录；'
                        '装一个 Node 20+，或设环境变量 %s 指向 node.exe' % NODE_ENV),
        },
    }


def _node_binary(node=None) -> Optional[str]:
    return node_info(node).get('path') or None


def _node_smoke(target: Path, node=None, timeout: float = 30.0) -> Dict[str, Any]:
    """真跑一次 node，确认 host_root 的 src/config.js 能被 import。"""
    info = node_info(node)
    binary = info.get('path') or ''
    result: Dict[str, Any] = {'ok': False, 'node': binary, 'nodeSource': info.get('source') or '',
                              'detail': '', 'duration_ms': 0}
    if not binary:
        result['error'] = info.get('error') or {'code': 'node-missing',
                                               'message': '找不到 node 可执行文件'}
        return result
    target_file = target / 'src' / 'config.js'
    if not target_file.is_file():
        result['error'] = {'code': 'config-missing', 'message': 'src/config.js 不存在'}
        return result
    env = dict(os.environ, QQ_AGENT_SOURCE=str(target),
               QQ_AGENT_DATA_DIR=str(target / 'data-host-smoke'))
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [binary, '--input-type=module', '-e', _SMOKE_CODE, str(target_file)],
            cwd=str(target), env=env, capture_output=True, timeout=max(1.0, float(timeout)))
    except (OSError, subprocess.SubprocessError) as exc:
        result['error'] = {'code': 'node-failed',
                           'message': ('%s: %s' % (type(exc).__name__, exc))[:300]}
        result['duration_ms'] = int((time.perf_counter() - started) * 1000)
        return result
    result['duration_ms'] = int((time.perf_counter() - started) * 1000)
    stdout = completed.stdout.decode('utf-8', 'replace')
    stderr = completed.stderr.decode('utf-8', 'replace')
    result['returncode'] = completed.returncode
    if PROBE_MARKER in stdout:
        try:
            payload = json.loads(stdout.split(PROBE_MARKER, 1)[1].splitlines()[0])
        except ValueError:
            payload = {}
        result['ok'] = bool(payload.get('ok'))
        result['exports'] = payload.get('exports')
        return result
    message = (stderr.strip() or stdout.strip() or 'no-json')[-300:]
    result['error'] = {'code': 'smoke-failed', 'message': message}
    return result


def node_binary(node=None) -> Optional[str]:
    """给状态接口用的公开取法：别再让 HTTP 层自己 which 一遍。"""
    return _node_binary(node)


def node_smoke(target, node=None, timeout: float = 30.0) -> Dict[str, Any]:
    """对外的一次真冒烟（状态接口 ?smoke=1 用）。"""
    return _node_smoke(target, node=node, timeout=timeout)


def materialize_host_root(data_dir=None, extra_source=None, force=False, node=None,
                          smoke=True) -> Dict[str, Any]:
    """把可用的 0.4 运行时复制成 <data>/qq-only/qqagent_host，返回可直接装扩展的根。

    幂等 + 自愈：根可用、非 force、且根内指纹与当前载荷同源，就只冒一次烟、不搬 4MB。
    指纹不同（载荷被修过）或根本没有指纹（历史根 / 指纹坏了）就**刷新载荷**：
    只重写 HOST_COPY_ENTRIES 与两个脚本，`<root>/plugins/` 下已装扩展一个都不动。
    刷新后写新指纹，因此下一次调用回到 reused=True。
    source 按 runtime_source_candidates 的顺序挑第一个完整的，返回里说清用了哪一个。
    """
    base = _as_path(data_dir) or (ROOT / 'data')
    goal = default_host_root(base)
    started = time.perf_counter()
    candidates = runtime_source_candidates(extra_source)
    source: Optional[Path] = None
    for candidate in candidates:
        if not _source_problems(candidate):
            source = candidate
            break
    if source is None and candidates:
        source = candidates[0]
    source_stamp = payload_stamp(source) if source is not None else {}
    previous_stamp = runtime_stamp(goal)
    outcome: Dict[str, Any] = {'ok': False, 'reused': False, 'refreshed': False,
                               'root': str(goal), 'source': str(source or ''),
                               'candidates': [str(item) for item in candidates],
                               'copied': 0, 'skipped': 0, 'refreshedFiles': [],
                               'files': 0, 'bytes': 0, 'stamp': source_stamp,
                               'previousStamp': previous_stamp,
                               'stampChanged': False, 'stampChanges': [],
                               'smoke': {}, 'error': None, 'duration_ms': 0}
    if host_root_ready(goal) and not force:
        # 有源指纹且与根内指纹不同 = 载荷被改过（例如 host-one.mjs 的参数位修复）：
        # 必须刷新，否则老用户永远走安装那天的脚本。没有源就照旧复用（换不了就只能用旧的）。
        stale = bool(source_stamp) and not stamp_matches(previous_stamp, source_stamp)
        if not stale:
            check = _node_smoke(goal, node=node) if smoke else {}
            unverified = str((check.get('error') or {}).get('code') or '') == 'node-missing'
            if check.get('ok') or unverified or not smoke:
                outcome.update(host_root_stats(goal), ok=True, reused=True, smoke=check,
                               duration_ms=int((time.perf_counter() - started) * 1000))
                return outcome
    refresh = host_root_ready(goal) and not force
    result = ensure_host_root(target=goal, source=source, node=node, smoke=smoke)
    outcome.update(host_root_stats(goal), ok=bool(result.get('ok')),
                   copied=result.get('copied') or 0, skipped=result.get('skipped') or 0,
                   smoke=result.get('smoke') or {}, error=result.get('error'),
                   duration_ms=int((time.perf_counter() - started) * 1000))
    if refresh:
        outcome['refreshed'] = bool(result.get('ok'))
        outcome['refreshedFiles'] = list(result.get('copiedFiles') or [])
    if result.get('ok'):
        # 指纹只在这份载荷真落地之后才写：写早了等于把半个根认成新的。
        fresh = payload_stamp(_as_path(result.get('source') or source))
        if fresh.get('files'):
            _write_stamp(goal, fresh)
        outcome['stamp'] = fresh or source_stamp
        outcome['stampChanged'] = not stamp_matches(previous_stamp, outcome['stamp'])
        outcome['stampChanges'] = stamp_diff(previous_stamp, outcome['stamp'])
    return outcome


def ensure_host_root(target=None, source=None, node=None, smoke=True) -> Dict[str, Any]:
    """把最小宿主集合落到 target；幂等、可重入，复制后真跑一次 import 冒烟。

    返回 {ok, root, copied, skipped, copiedFiles, smoke, error}。source 缺料时直接给出
    source-incomplete 与缺的文件清单，半个 host_root 不会被当成可用。
    只重写 HOST_COPY_ENTRIES 与两个脚本里内容不同源的那些，`<root>/plugins/` 下
    已装扩展一个都不删、一个都不覆盖。
    """
    goal = _as_path(target) or default_host_root()
    origin = _as_path(source)
    if origin is None:
        origin = resolve_host_root(data_dir=goal.parent.parent.parent)
    if origin is None:
        # 用户机上没有 .tmp/qq-agent-run：随包载荷是唯一能当源的目录，这里必须回退到它，
        # 否则手工调 ensure_host_root(target) 会给出 source-missing（materialize 那条路本来就会挑它）。
        for candidate in runtime_source_candidates():
            if candidate is not None and not _source_problems(candidate):
                origin = candidate
                break
    outcome: Dict[str, Any] = {'ok': False, 'root': str(goal), 'source': str(origin or ''),
                               'copied': 0, 'skipped': 0, 'copiedFiles': [],
                               'smoke': {}, 'error': None}
    if origin is None:
        outcome['error'] = {'code': 'source-missing',
                            'message': '找不到可复制的 0.4 宿主根（src/config.js + src/plugin-loader.js）'}
        return outcome
    if is_protected(goal) and origin != goal:
        outcome['error'] = {'code': 'target-protected',
                            'message': '目标落在只读基线内：%s' % goal}
        return outcome
    problems = _source_problems(origin)
    if problems:
        outcome['error'] = {'code': 'source-incomplete',
                            'message': '源宿主缺：' + '、'.join(problems), 'missing': problems}
        return outcome
    counters: Dict[str, Any] = {'copied': 0, 'skipped': 0, 'copied_files': []}
    try:
        goal.mkdir(parents=True, exist_ok=True)
        for entry in HOST_COPY_ENTRIES:
            item = origin / entry
            if item.is_dir():
                _copy_tree(item, goal / entry, counters)
            elif item.is_file():
                destination = goal / entry
                if _same_file(destination, item):
                    counters['skipped'] += 1
                else:
                    shutil.copy2(item, destination)
                    counters['copied'] += 1
                    counters.setdefault('copied_files', []).append(entry)
        _copy_scripts(goal, counters, source=origin)
        (goal / 'plugins').mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        outcome['copied'], outcome['skipped'] = counters['copied'], counters['skipped']
        outcome['copiedFiles'] = list(counters.get('copied_files') or [])
        outcome['error'] = {'code': 'copy-failed',
                            'message': ('%s: %s' % (type(exc).__name__, exc))[:300]}
        return outcome
    outcome['copied'], outcome['skipped'] = counters['copied'], counters['skipped']
    outcome['copiedFiles'] = list(counters.get('copied_files') or [])
    remaining = host_root_problems(goal)
    if remaining:
        outcome['error'] = {'code': 'copy-incomplete', 'message': '复制后仍缺：' + '、'.join(remaining),
                            'missing': remaining}
        return outcome
    if smoke:
        outcome['smoke'] = _node_smoke(goal, node=node)
        if not outcome['smoke'].get('ok'):
            outcome['error'] = outcome['smoke'].get('error') or {'code': 'smoke-failed',
                                                                'message': '冒烟未通过'}
            return outcome
    outcome['ok'] = True
    return outcome


# ── 装 / 卸 / 列 / 探 ────────────────────────────────────────────────────────
def _archive_names(archive: zipfile.ZipFile) -> List[str]:
    return [info.filename for info in archive.infolist() if not info.filename.endswith('/')]


def _reject_path(name: str) -> str:
    """返回拒绝理由；空字符串表示安全。"""
    posix = PurePosixPath(name.replace('\\', '/'))
    if '..' in posix.parts:
        return '压缩包内含可疑路径（..）：%s' % name
    if posix.is_absolute() or (len(name) > 1 and name[1] == ':'):
        return '压缩包内含绝对路径：%s' % name
    return ''


def _strip_root(names: Iterable[str]) -> Tuple[str, List[str]]:
    """单层根目录剥离：<id>/index.js → index.js。返回 (前缀, 规范化后的名字)。"""
    items = list(names)
    if not items:
        return '', []
    heads = {PurePosixPath(name.replace('\\', '/')).parts[0] for name in items}
    if len(heads) != 1:
        return '', items
    head = heads.pop()
    if not all(len(PurePosixPath(name.replace('\\', '/')).parts) > 1 for name in items):
        return '', items
    prefix = head + '/'
    return prefix, [name.replace('\\', '/')[len(prefix):] for name in items]


def _manifest_of(archive: zipfile.ZipFile, names: List[str],
                 prefix: str = '') -> Dict[str, Any]:
    """读包内 plugin.json（只认剥掉外层目录后的根清单）。

    names 是 zip 里的原始条目名（archive.read 只认原名），prefix 是要剥掉的那层。
    """
    for name in names:
        relative = name.replace('\\', '/')[len(prefix):] if prefix else name.replace('\\', '/')
        if PurePosixPath(relative).name.lower() != 'plugin.json':
            continue
        if PurePosixPath(relative).parent.as_posix() not in ('.', ''):
            continue
        try:
            raw = archive.read(name)
        except KeyError:
            return {}
        if len(raw) > MAX_MANIFEST_BYTES:
            return {}
        try:
            meta = json.loads(raw.decode('utf-8-sig', 'replace'))
        except ValueError:
            return {}
        return meta if isinstance(meta, dict) else {}
    return {}


def read_archive_manifest(zip_path) -> Dict[str, Any]:
    """只读地看一眼 zip：返回 {id, name, version, files, bytes, error}。

    跑批要先知道"这个包该装成哪个目录"，但不能为了知道就把 512 个包全解开。
    """
    archive_path = _as_path(zip_path)
    outcome: Dict[str, Any] = {'id': '', 'name': '', 'version': '', 'files': 0, 'bytes': 0,
                               'error': None}
    if archive_path is None or not archive_path.is_file():
        outcome['error'] = {'code': 'zip-missing', 'message': '压缩包不存在：%s' % zip_path}
        return outcome
    try:
        archive = zipfile.ZipFile(str(archive_path))
    except (zipfile.BadZipFile, OSError) as exc:
        outcome['error'] = {'code': 'zip-invalid', 'message': str(exc)[:200]}
        return outcome
    with archive:
        infos = archive.infolist()
        names = _archive_names(archive)
        prefix, _ = _strip_root(names)
        meta = _manifest_of(archive, names, prefix)
    outcome['files'] = len(names)
    outcome['bytes'] = sum(info.file_size for info in infos)
    outcome['id'] = safe_extension_id(meta.get('id')) or safe_extension_id(archive_path.stem)
    outcome['name'] = str(meta.get('name') or '')
    outcome['version'] = str(meta.get('version') or '')
    return outcome


def safe_extension_id(value) -> str:
    text = str(value or '').strip()
    return text if PLUGIN_ID_RE.match(text) else ''


def _manifest_default_enabled(manifest) -> bool:
    """清单里的 enabledByDefault；缺失/坏值按启用（与宿主 plugin-loader 同口径）。"""
    if not isinstance(manifest, dict):
        return True
    flag = manifest.get('enabledByDefault')
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str):
        return flag.strip().lower() not in ('false', '0', 'no', 'off')
    if isinstance(flag, (int, float)):
        return flag != 0
    return True


def _host_skill_enabled(host_root, ident, manifest) -> bool:
    """扩展在宿主 config.json 里真实的开关；没写过才回退清单默认值。"""
    config = read_host_config(host_root)
    skills = config.get('skills')
    entry = skills.get(ident) if isinstance(skills, dict) else None
    if isinstance(entry, dict):
        flag = entry.get('enabled')
        if isinstance(flag, bool):
            return flag
        if isinstance(flag, str):
            return flag.strip().lower() not in ('false', '0', 'no', 'off')
        if isinstance(flag, (int, float)):
            return flag != 0
    return _manifest_default_enabled(manifest)


def _disabled_flags(manifest, tools=None, hooks=None, providers=None,
                   loaded: bool = False, host_enabled: Optional[bool] = None) -> Dict[str, Any]:
    """装成功 != 能用（W11-3）：给出 {installedButDisabled, disabledReason}。

    两种「装完静默不生效」都要明说，否则用户装完看不到任何提示、工具也调不到：
      · 宿主开关（或清单 enabledByDefault）为假 —— 只报一句未启用；
      · 加载成功但一个工具都没注册（入口 API 形状不兼容等），同样不可调。
    host_enabled 传入宿主 config.json 的真实开关；探测时必须以它为准，否则
    清单写 enabledByDefault=false、用户后来手动开启的扩展会被误报成未启用。
    这里只报告，不改用户的 enabled 配置、也不自动翻转它。
    """
    if host_enabled is None:
        enabled = _manifest_default_enabled(manifest)
    else:
        enabled = bool(host_enabled)
    if not enabled:
        return {'installedButDisabled': True,
                'disabledReason': '扩展清单声明 enabledByDefault=false，宿主默认不启用：'
                                  '装好了但工具调不到，需要在扩展配置里显式开启'}
    if loaded and not tools and not hooks and not providers:
        return {'installedButDisabled': True,
                'disabledReason': '扩展已加载却什么都没注册（无工具、无 hook、无 provider）：'
                                  '入口需导出 setup(api)/register(api) 并调用 api.registerTool(...)'}
    return {'installedButDisabled': False, 'disabledReason': ''}


def install_extension(zip_path, host_root, extension_id=None, force=False) -> Dict[str, Any]:
    """把 zip 安全解包到 <host_root>/plugins/<id>/。

    闸门与 plugin_installer._pick_from_archive 同口径：条目数、解压总量、路径穿越、
    软链一律拒绝。已存在时默认不覆盖（code='exists'），force=True 才替换。
    """
    archive_path = _as_path(zip_path)
    root = _as_path(host_root)
    outcome: Dict[str, Any] = {'ok': False, 'id': '', 'root': '', 'files': 0, 'bytes': 0,
                               'error': None, 'installedButDisabled': False, 'disabledReason': ''}
    if archive_path is None or not archive_path.is_file():
        outcome['error'] = {'code': 'zip-missing', 'message': '压缩包不存在：%s' % zip_path}
        return outcome
    if root is None or not is_host_root(root):
        outcome['error'] = {'code': 'host-root-invalid',
                            'message': 'host_root 缺少 %s' % '、'.join(HOST_MARKERS)}
        return outcome
    if is_protected(root):
        outcome['error'] = {'code': 'host-root-protected',
                            'message': '不许写入只读基线：%s' % root}
        return outcome
    try:
        archive = zipfile.ZipFile(str(archive_path))
    except (zipfile.BadZipFile, OSError) as exc:
        outcome['error'] = {'code': 'zip-invalid', 'message': str(exc)[:200]}
        return outcome
    with archive:
        infos = archive.infolist()
        names = _archive_names(archive)
        if len(infos) > MAX_ARCHIVE_FILES:
            outcome['error'] = {'code': 'zip-too-many-files',
                                'message': '压缩包内文件过多（%d 个，上限 %d）'
                                           % (len(names), MAX_ARCHIVE_FILES)}
            return outcome
        total = sum(info.file_size for info in infos)
        if total > MAX_ARCHIVE_BYTES:
            outcome['error'] = {'code': 'zip-too-large',
                                'message': '压缩包解压后过大（%dMB，上限 %dMB）'
                                           % (total // (1024 * 1024), MAX_ARCHIVE_BYTES // (1024 * 1024))}
            return outcome
        if not names:
            outcome['error'] = {'code': 'zip-empty', 'message': '压缩包是空的'}
            return outcome
        for info in infos:
            reason = _reject_path(info.filename)
            if reason:
                outcome['error'] = {'code': 'zip-unsafe-path', 'message': reason}
                return outcome
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                outcome['error'] = {'code': 'zip-symlink',
                                    'message': '压缩包含软链：%s' % info.filename}
                return outcome
        prefix, normalized = _strip_root(names)
        meta = _manifest_of(archive, names, prefix)
        ident = safe_extension_id(extension_id) or safe_extension_id(meta.get('id')) \
            or safe_extension_id(archive_path.stem)
        if not ident:
            outcome['error'] = {'code': 'bad-id',
                                'message': '无法确定合法的扩展 id（plugin.json 的 id 与 zip 名都不合法）'}
            return outcome
        destination = root / 'plugins' / ident
        outcome['id'] = ident
        outcome['root'] = str(destination)
        if destination.exists() and not force:
            outcome['error'] = {'code': 'exists', 'message': '扩展已存在：%s' % destination}
            return outcome
        staging = root / 'plugins' / ('.install-%s' % uuid.uuid4().hex)
        try:
            staging.mkdir(parents=True, exist_ok=True)
            for info in infos:
                source_name = info.filename.replace('\\', '/')
                if source_name.endswith('/'):
                    continue
                relative = source_name[len(prefix):] if prefix and source_name.startswith(prefix) \
                    else source_name
                if not relative:
                    continue
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as handle, target.open('wb') as sink:
                    shutil.copyfileobj(handle, sink)
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(str(staging), str(destination))
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            outcome['error'] = {'code': 'unpack-failed',
                                'message': ('%s: %s' % (type(exc).__name__, exc))[:300]}
            return outcome
    outcome.update({'ok': True, 'files': len(names), 'bytes': total, 'manifest': bool(meta)})
    outcome.update(_disabled_flags(meta))
    return outcome


def uninstall_extension(extension_id, host_root) -> Dict[str, Any]:
    ident = safe_extension_id(extension_id)
    root = _as_path(host_root)
    outcome: Dict[str, Any] = {'ok': False, 'id': ident, 'root': '', 'error': None}
    if not ident:
        outcome['error'] = {'code': 'bad-id', 'message': '扩展 id 不合法：%r' % (extension_id,)}
        return outcome
    if root is None or not is_host_root(root):
        outcome['error'] = {'code': 'host-root-invalid', 'message': 'host_root 不可用'}
        return outcome
    if is_protected(root):
        outcome['error'] = {'code': 'host-root-protected', 'message': '不许写入只读基线：%s' % root}
        return outcome
    destination = root / 'plugins' / ident
    outcome['root'] = str(destination)
    if not destination.is_dir():
        outcome['error'] = {'code': 'not-installed', 'message': '没装：%s' % destination}
        return outcome
    try:
        shutil.rmtree(destination)
    except OSError as exc:
        outcome['error'] = {'code': 'remove-failed',
                            'message': ('%s: %s' % (type(exc).__name__, exc))[:200]}
        return outcome
    outcome['ok'] = True
    return outcome


def _read_text(path: Path, limit: int = SCAN_FILE_BYTES) -> str:
    try:
        with path.open('rb') as handle:
            return handle.read(limit).decode('utf-8', 'replace')
    except OSError:
        return ''


def _scan_extension(folder: Path) -> Dict[str, Any]:
    """静态读一个扩展目录：宿主源码导入、hook 名、provider 名、是否声明 inputSchema。"""
    imports: List[str] = []
    hooks: List[str] = []
    providers: List[str] = []
    schema = False
    for path in sorted(folder.rglob('*.js')):
        if not path.is_file():
            continue
        text = _read_text(path)
        if not text:
            continue
        for match in HOST_IMPORT_RE.finditer(text):
            specifier = match.group(1)
            if specifier not in imports:
                imports.append(specifier)
        if 'inputSchema' in text or 'parameters:' in text:
            schema = True
        for pattern, bucket in ((HOOK_KEY_RE, hooks), (PROVIDER_KEY_RE, providers)):
            block = pattern.search(text)
            if not block:
                continue
            for key in IDENT_KEY_RE.finditer(block.group(1)):
                name = key.group(1)
                if name not in bucket:
                    bucket.append(name)
    return {'hostImports': imports, 'hooks': hooks, 'providers': providers, 'inputSchema': schema}


def probe_cache_path(host_root) -> Optional[Path]:
    """宿主根里那份"安装时真加载过"的记录（静态读 plugin.json 猜不准工具名）。"""
    root = _as_path(host_root)
    return (root / PROBE_CACHE_NAME) if root else None


def read_probe_cache(host_root) -> Dict[str, Any]:
    """按扩展 id 索引的实测结果；缺失或坏了就当没有，不抛异常。"""
    path = probe_cache_path(host_root)
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(_read_text(path, MAX_MANIFEST_BYTES) or '{}')
    except ValueError:
        return {}
    extensions = payload.get('extensions') if isinstance(payload, dict) else None
    return extensions if isinstance(extensions, dict) else {}


def remember_probe(host_root, extension_id, probe) -> bool:
    """把一次真加载的结果记进宿主根；写不进去只是少一份证据，不该让安装失败。"""
    ident = str(extension_id or '')
    path = probe_cache_path(host_root)
    if not ident or path is None:
        return False
    payload = read_probe_cache(host_root)
    payload[ident] = {'ok': bool((probe or {}).get('ok')),
                      'tools': [str(item.get('id') or '') for item
                                in ((probe or {}).get('tools') or [])
                                if isinstance(item, dict) and item.get('id')],
                      'hooks': [str(item) for item in ((probe or {}).get('hooks') or [])],
                      'providers': [str(item) for item
                                    in ((probe or {}).get('providers') or [])],
                      'error': str(((probe or {}).get('error') or {}).get('code') or ''),
                      'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    temp = path.with_name(path.name + '.tmp')
    try:
        temp.write_text(json.dumps({'version': 1, 'extensions': payload},
                                   ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False
    return True


def read_plugin_manifest(folder) -> Dict[str, Any]:
    """读扩展目录里的 plugin.json；缺失或不是合法 JSON 对象时给空 dict。"""
    path = _as_path(folder)
    if path is None:
        return {}
    manifest = path / 'plugin.json'
    if not manifest.is_file():
        return {}
    try:
        payload = json.loads(_read_text(manifest, MAX_MANIFEST_BYTES) or '{}')
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def list_installed(host_root) -> List[Dict[str, Any]]:
    """读 plugins/*/plugin.json，给出 id / version / 声明项 / 宿主导入。"""
    root = _as_path(host_root)
    if root is None or not (root / 'plugins').is_dir():
        return []
    result: List[Dict[str, Any]] = []
    probes = read_probe_cache(root)
    for folder in sorted((root / 'plugins').iterdir()):
        if not folder.is_dir() or folder.name.startswith('.'):
            continue
        manifest = folder / 'plugin.json'
        meta = read_plugin_manifest(folder)
        scan = _scan_extension(folder)
        declared = meta.get('tools') if isinstance(meta.get('tools'), list) else []
        capabilities = meta.get('capabilities') if isinstance(meta.get('capabilities'), list) else []
        tools = [str(item) for item in declared] or [
            str(item)[len('tool.'):] for item in capabilities
            if isinstance(item, str) and item.startswith('tool.')]
        ident = str(meta.get('id') or folder.name)
        probed = probes.get(ident) if isinstance(probes.get(ident), dict) else {}
        result.append({
            'id': ident,
            'dir': str(folder),
            'version': str(meta.get('version') or ''),
            'name': str(meta.get('name') or ''),
            # 静态读只能看到声明；真加载过的工具名以宿主那次实测为准。
            'tools': list(probed.get('tools') or tools),
            'hooks': list(probed.get('hooks') or scan['hooks']) or list(meta.get('hooks') or []),
            'providers': list(probed.get('providers') or scan['providers']) or [
                str(item) for item in capabilities
                if isinstance(item, str) and not item.startswith('tool.')],
            'hostImports': list(scan['hostImports']),
            'hasManifest': manifest.is_file(),
            'probed': bool(probed),
        })
    return result


def import_probe(extension_dir, host_root, timeout: float = 20.0, node=None,
                 data_dir=None, extension_id=None) -> Dict[str, Any]:
    """真加载一次扩展（capability 传空 = 只加载不调用），拿回它自己注册的工具。

    走 extension_host.run_capability_sync，不自己拼 argv —— 宿主脚本的调用约定与
    结果解析只允许有一份实现。返回 {ok, tools, error, duration_ms}；
    另外给 installedButDisabled/disabledReason，说明"装上了但没生效"。
    """
    folder = _as_path(extension_dir)
    root = _as_path(host_root)
    outcome: Dict[str, Any] = {'ok': False, 'tools': [], 'error': None, 'duration_ms': 0,
                               'extension': '', 'hostImports': [],
                               'installedButDisabled': False, 'disabledReason': ''}
    if root is None or not is_host_root(root):
        outcome['error'] = {'code': 'host-root-invalid',
                            'message': 'host_root 缺少 %s' % '、'.join(HOST_MARKERS)}
        return outcome
    if folder is None or not folder.is_dir():
        outcome['error'] = {'code': 'extension-missing', 'message': '扩展目录不存在：%s' % extension_dir}
        return outcome
    binary = _node_binary(node)
    if not binary:
        outcome['error'] = node_info(node).get('error') or {
            'code': 'node-missing', 'message': '找不到 node 可执行文件'}
        return outcome
    script = find_host_script(root)
    if script is None:
        outcome['error'] = {'code': 'host-script-missing',
                            'message': '找不到 host-one.mjs（host_root 与兼容层目录都没有）'}
        return outcome
    ident = safe_extension_id(extension_id) or folder.name
    manifest = read_plugin_manifest(folder)
    host_enabled = _host_skill_enabled(root, ident, manifest)
    outcome.update(_disabled_flags(manifest, host_enabled=host_enabled))
    run = extension_host.run_capability_sync(
        node=binary, host_script=str(script), extension=str(folder), source_dir=str(root),
        capability='', timeout=timeout, kind='list',
        data_dir=str(data_dir or root), extension_id=ident, path_label='sweep')
    outcome['duration_ms'] = int(run.get('duration_ms') or 0)
    raw = run.get('raw') or {}
    extension = raw.get('extension') if isinstance(raw.get('extension'), dict) else {}
    declared_id = str((extension or {}).get('id') or ident)
    outcome['extension'] = declared_id
    outcome['loadError'] = str((extension or {}).get('loadError') or '')
    outcome['hooks'] = list((extension or {}).get('hooks') or [])
    outcome['providers'] = list((extension or {}).get('providers') or [])
    if not run.get('ok'):
        code = 'timeout' if run.get('timed_out') else 'load-failed'
        outcome['error'] = {'code': code, 'message': str(run.get('error') or '')[:300]}
        return outcome
    static = _scan_extension(folder)
    outcome['hostImports'] = static['hostImports']
    tools = extension_host.owned_tools(raw, declared_id)
    if not tools:
        declared = [str(item) for item in ((extension or {}).get('tools') or [])]
        tools = [{'id': name, 'name': name, 'description': ''} for name in declared]
    cleaned: List[Dict[str, Any]] = []
    for item in tools:
        has_schema = bool(item.get('inputSchema') or item.get('parameters') or item.get('schema'))
        cleaned.append({'id': str(item.get('id') or ''),
                        'name': str(item.get('name') or item.get('id') or ''),
                        'description': str(item.get('description') or ''),
                        'hasSchema': has_schema or static['inputSchema']})
    outcome['tools'] = cleaned
    outcome['ok'] = True
    outcome.update(_disabled_flags(manifest, tools=cleaned, hooks=outcome['hooks'],
                                   providers=outcome['providers'], loaded=True,
                                   host_enabled=host_enabled))
    return outcome

# ── 市场安装：落位 → 装机 → 真加载一次 ────────────────────────────────────────
def install_market_extension(data_dir=None, zip_path=None, extension_id='', force=False,
                             node=None, probe_timeout: float = 20.0,
                             owner_ids=None, reuse_existing=True) -> Dict[str, Any]:
    """市场包一键落位：备好宿主根 → 装进 <root>/plugins/<id> → 真加载一次探工具。

    安装成功就是成功（ok=True），能不能加载是另一层（probe.ok）——两层混成一个
    错误，用户会以为包没装上而反复重试，真正的问题在加载侧。
    """
    base = _as_path(data_dir) or (ROOT / 'data')
    started = time.perf_counter()
    host = materialize_host_root(base, node=node)
    outcome: Dict[str, Any] = {'ok': False, 'id': '', 'hostRoot': str(host.get('root') or ''),
                               'path': '', 'manifest': {}, 'tools': [], 'hooks': [],
                               'providers': [], 'probe': {}, 'files': 0, 'bytes': 0,
                               'copied': host.get('copied') or 0,
                               'skipped': host.get('skipped') or 0,
                               'reused': bool(host.get('reused')),
                               'source': str(host.get('source') or ''),
                               'node': str((host.get('smoke') or {}).get('node') or ''),
                               'nodeSource': str((host.get('smoke') or {}).get('nodeSource') or ''),
                               'smoke': host.get('smoke') or {},
                               'error': None, 'duration_ms': 0,
                               'alreadyInstalled': False}
    if not host.get('ok'):
        error = dict(host.get('error') or {'code': 'host-not-ready',
                                          'message': '宿主根不可用，扩展没有落点'})
        error['stage'] = 'host'
        outcome['error'] = error
        outcome['duration_ms'] = int((time.perf_counter() - started) * 1000)
        return outcome
    root = _as_path(host.get('root'))
    ident = safe_extension_id(extension_id) or str(read_archive_manifest(zip_path).get('id') or '')
    installed = install_extension(zip_path, root, extension_id=ident or None, force=force)
    outcome.update(id=str(installed.get('id') or ''), files=installed.get('files') or 0,
                   bytes=installed.get('bytes') or 0)
    if not installed.get('ok'):
        error = dict(installed.get('error') or {'code': 'install-failed', 'message': '安装失败'})
        # 重复安装不是失败：目录已经在宿主里就沿用并重新探测。用户点第二次
        # 「安装」时，这里若把 exists 当错误抛回去，界面只会说「安装失败」，
        # 而插件其实早就在、也能跑。要换版本由调用方显式传 force=True。
        existing = root / 'plugins' / str(outcome.get('id') or '')
        if error.get('code') == 'exists' and existing.is_dir() and reuse_existing:
            outcome['ok'] = True
            outcome['alreadyInstalled'] = True
            outcome['reused'] = True
            outcome['path'] = str(existing)
            outcome['files'] = sum(1 for item in existing.rglob('*') if item.is_file())
            outcome['bytes'] = sum(item.stat().st_size for item in existing.rglob('*')
                                   if item.is_file())
        else:
            error['stage'] = 'install'
            outcome['error'] = error
            outcome['duration_ms'] = int((time.perf_counter() - started) * 1000)
            return outcome
    folder = root / 'plugins' / outcome['id']
    manifest = read_plugin_manifest(folder)
    outcome['path'] = str(folder)
    outcome['manifest'] = manifest
    # 宿主自己有开关：config.json 里没写 skills.<id>.enabled 时，宿主按清单的
    # enabledByDefault 兜底，而市场扩展几乎都不声明 → 探测必然 extension-disabled。
    # 先在宿主侧打开再探测，探的是「能不能加载」而不是「开关写没写」；
    # 用户到底要不要启用由 extension_registry 决定，那边改状态时会同步回宿主。
    outcome['hostEnabled'] = set_extension_enabled(root, outcome['id'], True)
    # 有些扩展一加载就校验自己的配置（主人识别没有主人号码就 available()=false），
    # 账号里已经绑好的主人 QQ 是唯一权威来源，装的时候顺手写进它自己的 namespace。
    outcome['ownerSeed'] = seed_owner_settings(root, outcome['id'], owner_ids, manifest)
    probe = import_probe(folder, root, timeout=probe_timeout, node=node,
                         extension_id=safe_extension_id(manifest.get('id')))
    outcome['ok'] = True
    outcome['probe'] = {'ok': bool(probe.get('ok')),
                        'duration_ms': probe.get('duration_ms') or 0,
                        'error': probe.get('error'),
                        'loadError': probe.get('loadError') or ''}
    outcome['tools'] = list(probe.get('tools') or []) if probe.get('ok') else []
    outcome['hooks'] = list(probe.get('hooks') or []) \
        or list(_scan_extension(folder)['hooks'] if folder.is_dir() else [])
    outcome['providers'] = list(probe.get('providers') or [])
    remember_probe(root, outcome['id'], probe)
    outcome['duration_ms'] = int((time.perf_counter() - started) * 1000)
    return outcome
