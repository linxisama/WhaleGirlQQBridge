# -*- coding: utf-8 -*-
"""Skill 市场后端（WP12a）：市场定位、市场/已安装列表、安装、卸载、汇总。

纯标准库，不联网、不执行 skill 里的 JS、不起 node；import 期不读盘、不建目录。

四个既有约定一律复用（都只读，不改对方实现）：
* 已安装位置 = src/skill_tools.py:127 extensions_dir(runtime) = <data>/qq-only/skill_extensions；
* 分层与工具名 = src/skill_manifest.py:240 classify / 清单快照 skill_tools.load_manifest；
* 启用态 = src/extension_registry.py:160 is_enabled / :168 is_known；
* 安全解包口径对齐 src/skill_tools.py:233-276 与 src/plugin_installer.py:474-499：
  拒绝路径穿越 / 绝对路径 / 盘符 / 符号链接，条目数与解压总量设上限，
  临时目录解包 + 原子 rename，失败绝不留半成品。

返回结构里的 error 统一是 None 或 {'code','message'}；各函数只在文档写明的
位置多给字段（install 的 enabled / manifest，list_installed 的 known）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

try:  # 包内导入
    from . import extension_registry, skill_tools
except ImportError:  # pragma: no cover - 仅以仓库根为 sys.path 直接跑脚本时需要
    from src import extension_registry, skill_tools

# ── 可配置来源 ───────────────────────────────────────────────────────────────
MARKET_ENV = 'QQAGENT_SKILL_MARKET'
#: 历史环境变量名（src/skill_tools.py 早期只认这个），一起接受，避免两套口径。
LEGACY_MARKET_ENV = 'SKILL_MARKET_DIR'
MARKET_CONFIG_KEY = 'qq_only.skill_market_dir'
#: 默认候选（存在才用）。只作最后兜底，任何一项都可以被上面两个来源覆盖。
# 这一项是**开发机**的绝对路径：用户机上必然不命中，_market_candidates 会继续退到
# <程序目录>/插件市场/Skill市场 与 <上一级>/插件市场/Skill市场（见下）。发布包不带市场
# 本体，所以用户机首次进技能市场是空的 —— 要用 QQAGENT_SKILL_MARKET 或
# qq_only.skill_market_dir 指到自己的市场目录。
DEFAULT_MARKET_DIRS = (r'D:\聊天机器人\插件市场\Skill市场',)
MARKET_DIRNAME = 'Skill市场'

# ── 限额（对齐 plugin_installer / skill_tools 的口径） ───────────────────────
MAX_ENTRIES = 4000
MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_MANIFEST_DEPTH = 3
EXTRACT_CHUNK = 64 * 1024

#: 列表与安装都认两种清单：市场里 3 个老包只有 plugin.json（见 .tmp/inv/wp12a-report.md）。
MARKET_MANIFEST_NAMES = ('skill.json', 'plugin.json')
#: 同一层出现两份清单时优先 skill.json（只作排序键，不是"只认"）。
SKILL_MANIFEST_NAME = 'skill.json'
#: 清单缺 entry/main 时的入口回退（与 skill_tools.ENTRY_NAMES 的首选对齐）。
ENTRY_FALLBACK = 'index.js'
#: 这些层级"装上也不该自动启用"（口径同 src/qq_app.py:2435 _EXTENSION_BLOCKED_TIERS）。
BLOCKED_TIERS = ('T4-host-source', 'T6-disabled')

ID_HINT = '[0-9A-Za-z][0-9A-Za-z._-]{0,63}'
ID_PATTERN = getattr(skill_tools, 'SKILL_ID_RE', None) or re.compile('^' + ID_HINT + '$')


class MarketError(RuntimeError):
    """本模块的失败原因；code 直接进返回值的 error.code。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code or 'error')


def _error(code, message):
    return {'code': str(code or 'error'), 'message': str(message)}


# ── 基础工具 ─────────────────────────────────────────────────────────────────
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_dir(path) -> bool:
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def _config_get(config, key, default=None):
    """读配置：先按 ConfigManager 的点号键（src/config_manager.py:352），再按嵌套 dict。"""
    if config is None:
        return default
    getter = getattr(config, 'get', None)
    if callable(getter):
        value = None
        try:
            value = getter(key, default)
        except TypeError:
            try:
                value = getter(key)
            except Exception:
                value = None
        except Exception:
            value = None
        if value not in (None, ''):
            return value
    current = config
    for part in str(key).split('.'):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return default
    return default if current in (None, '') else current


# ── 市场目录 ─────────────────────────────────────────────────────────────────
def _market_candidates(config=None, runtime=None):
    """按优先级列出候选市场目录；只算路径，不判断存在性、不创建。"""
    if config is None:
        config = getattr(runtime, 'config', None)
    ordered = []
    for name in (MARKET_ENV, LEGACY_MARKET_ENV):
        env = os.environ.get(name, '').strip()
        if env:
            ordered.append(Path(env))
    configured = _config_get(config, MARKET_CONFIG_KEY)
    if configured:
        ordered.append(Path(str(configured)))
    for raw in DEFAULT_MARKET_DIRS:
        ordered.append(Path(str(raw)))
    repo = _repo_root()
    ordered.append(repo / '插件市场' / MARKET_DIRNAME)
    ordered.append(repo.parent / '插件市场' / MARKET_DIRNAME)
    seen, unique = set(), []
    for path in ordered:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def market_dir(config=None, runtime=None):
    """定位市场目录：环境变量 > 配置 > 默认候选；全都不可用返回 None。

    显式参数（list_market 的 market、install 的 zip_path）比它更优先。纯只读探测：
    绝不创建目录，也绝不因为候选不存在而报错。
    """
    for candidate in _market_candidates(config, runtime):
        if _is_dir(candidate):
            return candidate
    return None


# ── 安全解包 ─────────────────────────────────────────────────────────────────
def _safe_entry(name) -> str:
    """拒绝路径穿越 / 绝对路径 / Windows 盘符（口径同 skill_tools.py:233-239）。"""
    value = str(name or '').replace('\\', '/')
    parts = value.split('/')
    if not value or value.startswith('/') or '..' in parts or ':' in parts[0]:
        raise MarketError('unsafe-path', '包内路径不安全：%s' % value[:80])
    return value


def _is_symlink(info) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _open_archive(path):
    """打开 zip 并做安全体检；不合法抛 MarketError（调用方负责 close）。"""
    target = Path(path)
    if not target.is_file():
        raise MarketError('missing', '找不到 zip：%s' % target)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise MarketError('unreadable', '读不了 zip：%s' % exc) from None
    if size > MAX_ARCHIVE_BYTES:
        raise MarketError('too-large', 'zip 过大（%dKB，上限 %dKB）'
                          % (size // 1024, MAX_ARCHIVE_BYTES // 1024))
    try:
        archive = zipfile.ZipFile(target)
    except (zipfile.BadZipFile, OSError):
        raise MarketError('bad-zip', '这不是有效的 zip：%s' % target.name) from None
    try:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise MarketError('too-many-entries',
                              '条目过多（%d，上限 %d）' % (len(infos), MAX_ENTRIES))
        total = 0
        for info in infos:
            _safe_entry(info.filename)
            if _is_symlink(info):
                raise MarketError('symlink', '包内有软链，拒绝安装：%s' % info.filename[:80])
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise MarketError('too-large', '解压后过大（上限 %dMB）'
                                  % (MAX_TOTAL_BYTES // 1024 // 1024))
    except Exception:
        archive.close()
        raise
    return archive


def _archive_manifest(archive, names=None):
    """取包里最浅的一份清单，返回 (相对路径, dict)；找不到/坏了抛 MarketError。"""
    wanted = tuple(names or MARKET_MANIFEST_NAMES)
    found = None
    for info in archive.infolist():
        name = _safe_entry(info.filename)
        parts = name.split('/')
        if parts[-1] not in wanted or len(parts) > MAX_MANIFEST_DEPTH:
            continue
        key = (len(parts), parts[-1] != SKILL_MANIFEST_NAME, name)
        if found is None or key < found[0]:
            found = (key, info)
    if found is None:
        raise MarketError('no-manifest', '包里没有 %s' % ' / '.join(wanted))
    info = found[1]
    if info.file_size > MAX_MANIFEST_BYTES:
        raise MarketError('manifest-too-large', '清单文件过大（上限 %dKB）'
                          % (MAX_MANIFEST_BYTES // 1024))
    try:
        data = json.loads(archive.read(info).decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise MarketError('bad-manifest', '清单不是有效的 UTF-8 JSON') from None
    if not isinstance(data, dict):
        raise MarketError('bad-manifest', '清单顶层必须是 JSON 对象')
    return _safe_entry(info.filename), data


def _manifest_id(meta: dict) -> str:
    ident = str(meta.get('id') or '').strip()
    if not ID_PATTERN.match(ident):
        raise MarketError('bad-id', 'skill id 不合法：%r（要求 %s）' % (ident, ID_HINT))
    return ident


# ── 清单快照 / 静态分层（只读） ──────────────────────────────────────────────
def _snapshot_index(runtime=None) -> dict:
    """id → {tier, tools, name, version, summary}；取自 skill 清单快照，坏了就当没有。

    只认本 runtime 数据目录里的那份快照（skill_tools.load_manifest 还带开发机兜底
    .tmp/inv/skill-tools.json）：市场层级必须与「这个控制台自己的清单」同源，否则开发机
    看着有层级、用户机全空，两边表现不一致。没有快照就返回空，由界面提示先重建清单。
    """
    index = {}
    path = Path(skill_tools.data_dir(runtime)) / skill_tools.MANIFEST_FILE
    try:
        if not path.is_file():
            return index
        loaded = skill_tools.load_manifest(str(path)) or {}
    except Exception:
        return index
    for skill in loaded.get('skills') or []:
        if not isinstance(skill, dict):
            continue
        ident = str(skill.get('id') or '').strip()
        if not ident:
            continue
        tools = [str(tool.get('name')) for tool in (skill.get('tools') or [])
                 if isinstance(tool, dict) and str(tool.get('name') or '')]
        index[ident] = {'tier': str(skill.get('tier') or ''), 'tools': tools,
                        'name': str(skill.get('name') or ''),
                        'version': str(skill.get('version') or ''),
                        'summary': str(skill.get('summary') or '')}
    return index


def _classify_detail(directory) -> dict:
    """静态分层一个已安装目录（只读 skill_manifest.classify）；取不到返回 {}、不报错。"""
    try:
        from . import skill_manifest
    except ImportError:
        return {}
    reader_cls = getattr(skill_tools, '_DirReader', None)
    if reader_cls is None:
        return {}
    try:
        return dict(skill_manifest.classify(reader_cls(Path(directory))) or {})
    except Exception:
        return {}


def _classify_local(directory):
    """只要 (tier, tools) 的调用方用这个；细节走 _classify_detail。"""
    info = _classify_detail(directory)
    tools = [str(tool.get('name')) for tool in (info.get('tools') or [])
             if isinstance(tool, dict) and str(tool.get('name') or '')]
    return str(info.get('tier') or ''), tools


def _tool_names(raw) -> list:
    names = []
    for tool in raw or []:
        if isinstance(tool, dict) and str(tool.get('name') or ''):
            names.append(str(tool['name']))
        elif isinstance(tool, str) and tool:
            names.append(tool)
    return names


def _dir_manifest(directory) -> dict:
    """读已安装目录里最浅的一份清单（skill.json 优先）；失败返回 {}。"""
    base = Path(directory)
    candidates = []
    try:
        for child in base.iterdir():
            if child.is_file() and child.name in MARKET_MANIFEST_NAMES:
                candidates.append(child)
            elif child.is_dir():
                for inner in child.iterdir():
                    if inner.is_file() and inner.name in MARKET_MANIFEST_NAMES:
                        candidates.append(inner)
    except OSError:
        return {}
    try:
        ordered = sorted(candidates, key=lambda path: (len(path.relative_to(base).parts),
                                                       path.name != SKILL_MANIFEST_NAME))
    except ValueError:
        return {}
    for path in ordered:
        try:
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                continue
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


# ── 入口名（清单 entry/main 优先，缺了回退 index.js） ────────────────────────
def _entry_names() -> tuple:
    """入口候选：对齐 src/skill_tools.py:46 ENTRY_NAMES，拿不到就只认 index.js。"""
    names = getattr(skill_tools, 'ENTRY_NAMES', None) or ()
    picked = tuple(str(name) for name in names if str(name))
    return picked or (ENTRY_FALLBACK,)


def _declared_entry(meta) -> str:
    """清单声明的入口（entry 优先、其次 main）；去掉 ./ 前缀，穿越写法一律不认。"""
    raw = str((meta or {}).get('entry') or (meta or {}).get('main') or '').strip()
    raw = raw.replace('\\', '/')
    while raw.startswith('./'):
        raw = raw[2:]
    if not raw or '..' in raw.split('/'):
        return ''
    return raw


def _pick_entry(meta, available=()) -> str:
    """入口名：清单 entry/main 优先；缺了按 ENTRY_NAMES 顺序找，最后回退 index.js。"""
    declared = _declared_entry(meta)
    if declared:
        return declared
    have = set(available or ())
    for name in _entry_names():
        if name in have:
            return name
    return ENTRY_FALLBACK


def _top_level_names(archive, prefix='') -> set:
    """包根一层的文件名（用于判断回退入口是否存在）；坏条目跳过。"""
    base = (prefix + '/') if prefix else ''
    names = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        try:
            name = _safe_entry(info.filename)
        except MarketError:
            continue
        if base and not name.startswith(base):
            continue
        rest = name[len(base):]
        if rest and '/' not in rest:
            names.add(rest)
    return names


def _dir_top_names(directory) -> set:
    """已安装目录第一层的文件名；读不了就当空。"""
    try:
        return {child.name for child in Path(directory).iterdir() if child.is_file()}
    except OSError:
        return set()


# ── 启用态（extension_registry 只读 / 通过既有 set_enabled 写） ──────────────
def _registry_data_dir(runtime):
    """extension_registry 的 data_dir（= <root>/data）；没显式 runtime 时用它的默认值。"""
    for attr in ('data_root', 'root'):
        if getattr(runtime, attr, None):
            try:
                return Path(skill_tools.data_dir(runtime)).parent
            except Exception:
                return None
    return None


def _registry_flag(name, ident, data_dir=None) -> bool:
    func = getattr(extension_registry, name, None)
    if not callable(func):
        return False
    try:
        return bool(func(ident, data_dir))
    except TypeError:
        try:
            return bool(func(ident))
        except Exception:
            return False
    except Exception:
        return False


def _set_enabled(ident, flag, data_dir=None, note=None):
    """写启用态（复用 extension_registry.set_enabled）；写不进去返回 None，不抛。"""
    setter = getattr(extension_registry, 'set_enabled', None)
    if not callable(setter):
        return None
    attempts = (
        lambda: setter(ident, flag, data_dir, source='market', note=note),
        lambda: setter(ident, flag, data_dir, source='market'),
        lambda: setter(ident, flag, source='market', note=note),
        lambda: setter(ident, flag, source='market'),
    )
    entry = None
    for attempt in attempts:
        try:
            entry = attempt()
            break
        except TypeError:  # 对方的签名更老，退一步再试
            continue
        except Exception:
            return None
    else:
        return None
    return bool((entry or {}).get('enabled'))


def _enable_decision(ident, tier, blockers, data_dir, enable) -> dict:
    """装上后怎么落启用态；返回 {'enabled','note','branch'}。

    branch 与 src/qq_app.py:2438 _register_installed_extension 的五分支同义：
    kept（不动已登记）/ default（首次登记默认停用）/ off（调用方选停用）/
    blocked（T4、T6 或带阻塞原因：照常装好但不启用）/ on（装上并启用）。
    """
    if enable is None:
        current = None
        try:
            current = (extension_registry.load(data_dir)['extensions'] or {}).get(ident)
        except Exception:
            current = None
        if isinstance(current, dict):
            return {'enabled': bool(current.get('enabled')),
                    'note': str(current.get('note') or ''),
                    'branch': 'kept'}
        note = '市场安装，默认停用'
        return {'enabled': _set_enabled(ident, False, data_dir, note=note),
                'note': note, 'branch': 'default'}
    if not enable:
        note = '市场安装，按你的选择停用'
        return {'enabled': _set_enabled(ident, False, data_dir, note=note),
                'note': note, 'branch': 'off'}
    reasons = [str(one) for one in (blockers or []) if str(one)]
    if tier in BLOCKED_TIERS or reasons:
        reason = tier or '层级未知'
        if reasons:
            reason += '（阻塞原因：%s）' % '、'.join(reasons)
        note = '市场安装，未启用：%s' % reason
        return {'enabled': _set_enabled(ident, False, data_dir, note=note),
                'note': note, 'branch': 'blocked'}
    note = '市场安装并启用'
    return {'enabled': _set_enabled(ident, True, data_dir, note=note),
            'note': note, 'branch': 'on'}


# ── 市场列表 ─────────────────────────────────────────────────────────────────
def _read_zip_entry(path):
    """读一个市场 zip 的 (id, 清单, 字节数)；坏包抛 MarketError / BadZipFile。"""
    target = Path(path)
    with zipfile.ZipFile(target) as archive:
        _, meta = _archive_manifest(archive)
        ident = _manifest_id(meta)
    return ident, meta, int(target.stat().st_size)


def list_market(market=None, runtime=None) -> dict:
    """扫市场 zip，返回 {'ok','count','items','broken','brokenItems','market','error'}。

    items 每条 {'id','name','version','description','file','bytes','installed','tier','tools'}。
    tier/tools 从清单快照（skill_manifest 产物）取，取不到就留空 —— 不因此报错。
    单个坏 zip 只跳过并计入 broken/brokenItems，不中断整次扫描。
    """
    chosen = Path(market) if market else market_dir(runtime=runtime)
    result = {'ok': False, 'count': 0, 'items': [], 'broken': 0, 'brokenItems': [],
              'market': str(chosen or ''), 'error': None}
    if chosen is None or not _is_dir(chosen):
        result['error'] = _error('market-missing',
                                 '找不到 Skill 市场目录（可用环境变量 %s 或配置 %s 指定）'
                                 % (MARKET_ENV, MARKET_CONFIG_KEY))
        return result
    try:
        paths = sorted(Path(chosen).glob('*.zip'))
    except OSError as exc:
        result['error'] = _error('market-unreadable', '读不了市场目录：%s' % exc)
        return result
    snapshot = _snapshot_index(runtime)
    root = Path(skill_tools.extensions_dir(runtime))
    items = []
    for path in paths:
        try:
            ident, meta, size = _read_zip_entry(path)
        except Exception:
            result['broken'] += 1
            result['brokenItems'].append(path.name)
            continue
        entry = snapshot.get(ident) or {}
        items.append({
            'id': ident,
            'name': str(meta.get('name') or entry.get('name') or ident),
            'version': str(meta.get('version') or entry.get('version') or ''),
            'description': str(meta.get('description') or entry.get('summary') or ''),
            'file': path.name,
            'bytes': size,
            'installed': _is_dir(root / ident),
            'tier': str(entry.get('tier') or ''),
            'tools': list(entry.get('tools') or []),
        })
    items.sort(key=lambda item: (item['id'], item['file']))
    result['brokenItems'].sort()
    # tierFilled：层级已知的包数。清单快照没建过时是 0，界面据此提示"先重建清单"。
    result.update(ok=True, count=len(items), items=items,
                  tierFilled=sum(1 for item in items if item.get('tier')))
    return result


def list_installed(runtime=None) -> dict:
    """已安装 skill 列表，返回 {'ok','count','items','root','error'}。

    items 每条 {'id','path','name','version','entry','tier','tools','enabled',
    'known','blocked'}；enabled/known 直接取 extension_registry 的既有语义
    （未登记 = 停用）；blocked 是层级阻塞原因，'' 表示层级没拦它。
    """
    root = Path(skill_tools.extensions_dir(runtime))
    data_dir = _registry_data_dir(runtime)
    result = {'ok': True, 'count': 0, 'items': [], 'root': str(root), 'error': None}
    try:
        children = sorted((child for child in root.iterdir()
                           if child.is_dir() and not child.name.startswith('.')),
                          key=lambda path: path.name)
    except OSError:
        children = []
    snapshot = _snapshot_index(runtime) if children else {}
    items = []
    for child in children:
        meta = _dir_manifest(child)
        entry = snapshot.get(child.name) or {}
        tools = list(entry.get('tools') or []) or _tool_names(meta.get('tools'))
        tier = str(entry.get('tier') or '')
        if not tier:  # 清单里没有这个 skill 时就地分层，别把层级留空
            detail = _classify_detail(child)
            tier = str(detail.get('tier') or '')
            if not tools:
                tools = [str(tool.get('name')) for tool in (detail.get('tools') or [])
                         if isinstance(tool, dict) and str(tool.get('name') or '')]
        items.append({
            'id': child.name,
            'path': str(child),
            'name': str(meta.get('name') or entry.get('name') or child.name),
            'version': str(meta.get('version') or entry.get('version') or ''),
            'entry': _pick_entry(meta, _dir_top_names(child)),
            'tier': tier,
            'tools': tools,
            'enabled': _registry_flag('is_enabled', child.name, data_dir),
            'known': _registry_flag('is_known', child.name, data_dir),
            'blocked': tier if tier in BLOCKED_TIERS else '',
        })
    result.update(count=len(items), items=items)
    return result


# ── 安装 ─────────────────────────────────────────────────────────────────────
def _extract_archive(archive, staging) -> tuple:
    """逐个条目落到临时目录，返回 (文件数, 解压字节数)；异常往上抛，临时目录由调用方删。"""
    base = Path(staging)
    resolved = base.resolve()
    files = 0
    total = 0
    for info in archive.infolist():
        name = _safe_entry(info.filename)
        if info.is_dir():
            (base / name).mkdir(parents=True, exist_ok=True)
            continue
        target = base / name
        try:
            target.resolve().relative_to(resolved)
        except (OSError, ValueError):
            raise MarketError('unsafe-path', '包内路径逃出安装目录：%s' % name[:80]) from None
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, target.open('wb') as handle:
            shutil.copyfileobj(source, handle, EXTRACT_CHUNK)
        files += 1
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise MarketError('too-large', '解压后过大（上限 %dMB）'
                              % (MAX_TOTAL_BYTES // 1024 // 1024))
    return files, total


def _swap_into_place(package, dest, ident) -> bool:
    """把临时目录里解好的包原子挪到 dest（先挪走旧目录，失败回滚），返回 replaced。"""
    dest = Path(dest)
    backup = None
    replaced = False
    if dest.exists():
        backup = dest.parent / ('.old-%s-%s' % (ident, uuid.uuid4().hex))
        dest.replace(backup)
        replaced = True
    try:
        Path(package).replace(dest)
    except OSError:
        if backup is not None and backup.exists():
            try:
                backup.replace(dest)
            except OSError:
                pass
        raise
    if backup is not None and backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    return replaced


def _installed_placement(directory, ident, detail=None, runtime=None) -> tuple:
    """落位后的 (tier, tools)：先看清单快照，取不到再静态分层，都没有就留空。"""
    entry = _snapshot_index(runtime).get(ident) or {}
    tier = str(entry.get('tier') or '')
    tools = list(entry.get('tools') or [])
    if tier and tools:
        return tier, tools
    if detail is None:
        detail = _classify_detail(directory)
    extra_tools = [str(tool.get('name')) for tool in (detail.get('tools') or [])
                   if isinstance(tool, dict) and str(tool.get('name') or '')]
    return tier or str(detail.get('tier') or ''), tools or extra_tools


def install(zip_path, runtime=None, force=False, enable=True) -> dict:
    """安全解包一个 skill zip 到 <data>/skill_extensions/<id>。

    返回 {'ok','id','path','entry','files','bytes','replaced','tier','tools',
    'enabled','note','manifest','error'}。清单认 skill.json / plugin.json；
    清单缺 entry/main 时回退 index.js（对齐 skill_tools.ENTRY_NAMES）。
    已存在且 force=False → ok=False、error.code='exists'；任何失败都不留半成品
    （临时目录解包 + 原子 rename，失败时目标目录保持原样或不存在）。
    enable 默认 True：装上即启用，但 tier 属 T4/T6 或带阻塞原因时照常装好、
    登记为停用并回中文 note；显式 enable=None 才"不动已登记状态"（kept/default 分支）。
    """
    result = {'ok': False, 'id': '', 'path': '', 'entry': '', 'files': 0, 'bytes': 0,
              'replaced': False, 'tier': '', 'tools': [], 'enabled': None, 'note': '',
              'manifest': {}, 'error': None}
    try:
        archive = _open_archive(zip_path)
    except MarketError as exc:
        result['error'] = _error(exc.code, str(exc))
        return result
    root = Path(skill_tools.extensions_dir(runtime))
    staging = None
    try:
        manifest_rel, meta = _archive_manifest(archive)
        ident = _manifest_id(meta)
        prefix = manifest_rel.rsplit('/', 1)[0] if '/' in manifest_rel else ''
        entry = _pick_entry(meta, _top_level_names(archive, prefix))
        dest = root / ident
        result.update(id=ident, path=str(dest), entry=entry,
                      manifest={'id': ident,
                                'name': str(meta.get('name') or ''),
                                'version': str(meta.get('version') or ''),
                                'entry': entry})
        if dest.is_dir() and not force:
            result['error'] = _error('exists', '已安装 %s；要覆盖请传 force=True（目录 %s）'
                                     % (ident, dest))
            return result
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.install-%s-' % ident, dir=str(root)))
        files, total = _extract_archive(archive, staging)
        package = (staging / manifest_rel).parent if '/' in manifest_rel else staging
        replaced = _swap_into_place(package, dest, ident)
        detail = _classify_detail(dest)
        tier, tools = _installed_placement(dest, ident, detail, runtime)
        state = _enable_decision(ident, tier, detail.get('blockers'),
                                 _registry_data_dir(runtime), enable)
        result.update(ok=True, files=files, bytes=total, replaced=replaced,
                      tier=tier, tools=tools, enabled=state['enabled'],
                      note=state['note'])
        return result
    except MarketError as exc:
        result['error'] = _error(exc.code, str(exc))
        return result
    except (OSError, zipfile.BadZipFile) as exc:
        result['error'] = _error('io-error', '%s: %s' % (type(exc).__name__, exc))
        return result
    finally:
        archive.close()
        if staging is not None:
            try:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            except OSError:
                pass


# ── 卸载 ─────────────────────────────────────────────────────────────────────
def uninstall(skill_id, runtime=None) -> dict:
    """只删 <data>/skill_extensions/<id> 这一层；id 不合法或不在根内一律不删。"""
    ident = str(skill_id or '').strip()
    result = {'ok': False, 'id': ident, 'path': '', 'removed': False, 'error': None}
    if not ID_PATTERN.match(ident):
        result['error'] = _error('bad-id', 'skill id 不合法：%r（要求 %s）' % (ident, ID_HINT))
        return result
    root = Path(skill_tools.extensions_dir(runtime))
    dest = root / ident
    result['path'] = str(dest)
    try:
        dest.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        result['error'] = _error('outside-root', '目标不在已安装根目录内：%s' % dest)
        return result
    if not dest.is_dir():
        result['error'] = _error('missing', '没有安装 %s（%s 不存在）' % (ident, dest))
        return result
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        result['error'] = _error('remove-failed', '删不掉 %s：%s' % (dest, exc))
        return result
    result.update(ok=True, removed=True)
    return result


# ── 汇总 ─────────────────────────────────────────────────────────────────────
def summary(runtime=None) -> dict:
    """市场/已安装/可注册性汇总：{'installed','marketCount','eligible','blocked','marketDir','error'}。

    eligible/blocked 来自 skill_tools.eligible_skills(...) 的 counts（eligible / skipped）；
    该调用异常时降级为 None，不拖垮汇总。
    """
    result = {'installed': 0, 'marketCount': 0, 'eligible': None, 'blocked': None,
              'marketDir': '', 'error': None}
    chosen = market_dir(runtime=runtime)
    result['marketDir'] = str(chosen or '')
    market = list_market(market=chosen, runtime=runtime)
    result['marketCount'] = int(market.get('count') or 0)
    if not market.get('ok'):
        result['error'] = market.get('error')
    result['installed'] = int(list_installed(runtime=runtime).get('count') or 0)
    # 只需要"这批包里有几个能注册"：清单快照没建过就给 None（界面显示 —），
    # 不能拿别的目录/别的快照去凑数字，否则汇总与列表说的不是同一批包。
    manifest_path = Path(skill_tools.data_dir(runtime)) / skill_tools.MANIFEST_FILE
    if manifest_path.is_file():
        try:
            counts = (skill_tools.eligible_skills(runtime=runtime) or {}).get('counts') or {}
        except Exception:
            counts = {}
        result['eligible'] = counts.get('eligible')
        result['blocked'] = counts.get('skipped')
    return result
