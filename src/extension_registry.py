# -*- coding: utf-8 -*-
"""QQ Agent 扩展启用状态的唯一权威存储。

问题：市场里 512 个扩展"可发现"，能力桥一旦按清单自动选择就会拉起一串 Node
子进程；而"装了什么 / 允许跑什么"此前没有任何持久化的地方 —— 能力桥的
settings 只是覆盖层，市场清单每次都会把 512 个重新算一遍。

这里把"允许跑哪些扩展"落成一处：`data/qq-only/qqagent_extensions.json`。
其它模块（能力桥、控制台端点、GUI）只读这一份状态，不再各自解释清单。

设计约束：
* 纯标准库、**不在 import 时读盘或拉进程**（懒加载，缺文件即默认状态）。
* 缺省一律 `enabled=False`：市场扩展默认关闭，避免一次性拉起几百个 Node。
* 状态文件损坏 → 降级为空状态，不抛异常（宁可全关，也不误开）。
* 发现结果（tier/blockers/toolCount）只是缓存，权威只有 enabled/note。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_NAME = 'qqagent_extensions.json'
STATE_VERSION = 1
#: 发现结果的进程内缓存时长（秒）；扫描只读本地清单，不必每次重算。
SCAN_TTL = 120.0
MANIFEST_NAMES = ('all-plugins-v4.json', 'all-plugins-v3.json', 'all-plugins-v2.json')
CATALOG_NAMES = ('catalog.json',)
#: 市场导出目录（相对仓库上一级）。只是"可选资产"，没有就只看到已安装层。
EXPORT_DIRS = ('插件市场/QQAgent兼容层',)
#: 层级名沿用 .tmp/inv/skill-tools.json 的实际风格（T1-ready…T6-disabled）。
TIER_ORDER = ('T1-ready', 'T2-web-fetch', 'T3-extra-api', 'T4-host-source',
              'T5-depends-other', 'T6-disabled')

_STATE_CACHE: Dict[str, tuple] = {}
_SCAN_CACHE: Dict[str, dict] = {}


# ── 路径 ────────────────────────────────────────────────────────────────────
def root_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def default_data_dir() -> Path:
    """<data_dir>：交给 src.data_root（源码=仓库根/data，冻结=%LOCALAPPDATA%/WhaleGirlQQ/data）。

    以前是 root_dir()/data；冻结后 root_dir() 落在解包目录 _internal 里，写进去等于
    写安装目录，只读安装位置会直接失败。
    """
    from .data_root import resolve_data_root
    return resolve_data_root() / 'data'


def _data(data_dir=None) -> Path:
    return Path(str(data_dir)) if data_dir else default_data_dir()


def market_extensions_dir(data_dir=None) -> Path:
    """市场扩展 zip 的落地目录（与 qqagent_market.extensions_dir 同一个位置）。"""
    return _data(data_dir) / 'qqagent_extensions'


def state_path(data_dir=None) -> Path:
    return _data(data_dir) / 'qq-only' / STATE_NAME


def default_state() -> dict:
    return {'version': STATE_VERSION, 'extensions': {}}


# ── 状态读写 ────────────────────────────────────────────────────────────────
def _normalize_state(raw) -> dict:
    state = default_state()
    if not isinstance(raw, dict):
        return state
    try:
        version = int(raw.get('version') or STATE_VERSION)
    except (TypeError, ValueError):
        version = STATE_VERSION
    state['version'] = version or STATE_VERSION
    extensions = raw.get('extensions')
    if not isinstance(extensions, dict):
        return state
    for ident, item in extensions.items():
        ident = str(ident or '').strip()
        if not ident or not isinstance(item, dict):
            continue
        entry = {'enabled': bool(item.get('enabled')),
                 'updatedAt': str(item.get('updatedAt') or ''),
                 'source': str(item.get('source') or 'market'),
                 'note': str(item.get('note') or '')}
        if entry['source'] not in ('builtin', 'market'):
            entry['source'] = 'market'
        state['extensions'][ident] = entry
    return state


def load(data_dir=None) -> dict:
    """读状态；文件缺失/损坏/半截都降级为空状态（绝不抛）。"""
    path = state_path(data_dir)
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        _STATE_CACHE.pop(key, None)
        return default_state()
    cached = _STATE_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return json.loads(json.dumps(cached[2]))          # 深拷贝，防调用方改缓存
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return default_state()
    state = _normalize_state(raw)
    _STATE_CACHE[key] = (stat.st_mtime_ns, stat.st_size, state)
    return json.loads(json.dumps(state))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():                                    # 覆盖前留一份 .bak
        try:
            path.with_suffix(path.suffix + '.bak').write_bytes(path.read_bytes())
        except OSError:
            pass
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def save(state, data_dir=None) -> dict:
    """整份写回（原子写 + 备份）；返回规范化后的状态。"""
    normalized = _normalize_state(state if isinstance(state, dict) else {})
    path = state_path(data_dir)
    try:
        _atomic_write(path, json.dumps(normalized, ensure_ascii=False, indent=2))
    except OSError:
        return normalized                                   # 写不进去也别让调用方崩
    _STATE_CACHE.pop(str(path), None)
    return normalized


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _entry(state: dict, ident: str) -> dict:
    item = state.get('extensions', {}).get(ident)
    return item if isinstance(item, dict) else {}


def is_enabled(ident, data_dir=None) -> bool:
    """未登记过的扩展一律视为停用（市场默认关闭）。"""
    ident = str(ident or '').strip()
    if not ident:
        return False
    return bool(_entry(load(data_dir), ident).get('enabled'))


def is_known(ident, data_dir=None) -> bool:
    """这个扩展是否"归扩展管理管"：状态文件里登记过，或能被发现到。

    桥靠它区分"用户在扩展管理里管过的扩展"和"手写 profiles 里的历史 id"。
    后者没有任何登记记录，桥上保持既有行为——否则升级后默认全停会让原本能用
    的手写配置突然失效。
    """
    ident = str(ident or '').strip()
    if not ident:
        return False
    if ident in load(data_dir)['extensions']:
        return True
    try:
        found = discover(data_dir)
    except Exception:
        return False
    return any(str(item.get('id')) == ident for item in (found.get('items') or []))


def enabled_ids(data_dir=None) -> List[str]:
    state = load(data_dir)
    return sorted(ident for ident, item in state['extensions'].items() if item.get('enabled'))


def disabled_ids(data_dir=None) -> List[str]:
    state = load(data_dir)
    return sorted(ident for ident, item in state['extensions'].items() if not item.get('enabled'))


def set_enabled(ident, enabled, data_dir=None, source='', note=None) -> dict:
    """改一个扩展的启用态；返回改后的条目（含 id）。"""
    ident = str(ident or '').strip()
    if not ident:
        raise ValueError('扩展 id 不能为空')
    state = load(data_dir)
    current = _entry(state, ident)
    entry = {'enabled': bool(enabled),
             'updatedAt': _now(),
             'source': str(source or current.get('source') or 'market'),
             'note': str(note if note is not None else current.get('note') or '')}
    if entry['source'] not in ('builtin', 'market'):
        entry['source'] = 'market'
    state['extensions'][ident] = entry
    save(state, data_dir)
    _mirror_host_enabled(ident, entry['enabled'], data_dir)
    return {'id': ident, **entry}


def _mirror_host_enabled(ident, enabled, data_dir=None) -> bool:
    """把启用态同步到宿主自己的 config.json（``<host_root>/data-host-one/config.json``）。

    权威存储是这份状态文件，但宿主不读它：host-one.mjs 只读自己 config.json 里的
    ``skills.<id>.enabled``。两边不一致时界面显示「已启用」而宿主拒绝加载，用户看到的
    现象就是「开了、就是没反应」。宿主模块不在或写盘失败都不许影响状态保存 —— 这里只是同步。
    """
    try:
        from . import qqagent_host
    except Exception:
        return False
    try:
        root = qqagent_host.default_host_root(data_dir)
        return bool(qqagent_host.set_extension_enabled(root, ident, enabled).get('ok'))
    except Exception:
        return False


def set_many(ids, enabled, data_dir=None) -> dict:
    """批量改启用态；幂等（重复设置同一值只刷新时间戳）。"""
    targets = []
    for item in (ids or []):
        ident = str(item or '').strip()
        if ident and ident not in targets:
            targets.append(ident)
    state = load(data_dir)
    updated = []
    stamp = _now()
    for ident in targets:
        current = _entry(state, ident)
        entry = {'enabled': bool(enabled), 'updatedAt': stamp,
                 'source': str(current.get('source') or 'market'),
                 'note': str(current.get('note') or '')}
        state['extensions'][ident] = entry
        updated.append({'id': ident, **entry})
    if updated:
        save(state, data_dir)
        for item in updated:
            _mirror_host_enabled(item['id'], item['enabled'], data_dir)
    return {'ok': True, 'updated': updated, 'count': len(updated),
            'enabled': bool(enabled)}


# ── 发现：市场清单（可选资产）+ 已安装目录 ──────────────────────────────────
def _load_json(path) -> Any:
    try:
        return json.loads(Path(str(path)).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _as_list(value) -> List[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple)):
        return []
    names = []
    for item in value:
        if isinstance(item, str) and item:
            names.append(item)
        elif isinstance(item, dict):
            name = str(item.get('name') or item.get('id') or '')
            if name:
                names.append(name)
    return names


def manifest_dir(data_dir=None) -> Optional[Path]:
    """找市场导出目录（含 all-plugins-v4.json）。找不到返回 None。

    顺序：`QQAGENT_MANIFEST_DIR` 环境变量 → 数据目录/扩展目录旁 → 仓库上一级的
    市场导出目录。全是只读探测，不创建任何东西。
    """
    candidates: List[Path] = []
    env = os.environ.get('QQAGENT_MANIFEST_DIR', '').strip()
    if env:
        candidates.append(Path(env))
    base = _data(data_dir)
    candidates.extend([base, market_extensions_dir(data_dir), base.parent,
                       base.parent / '插件市场' / 'QQAgent兼容层'])
    for name in EXPORT_DIRS:
        candidates.append(root_dir().parent / name)
    seen = set()
    for folder in candidates:
        try:
            key = str(folder.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        for name in MANIFEST_NAMES:
            if (folder / name).is_file():
                return folder
    return None


def _manifest_index(folder: Optional[Path]) -> Dict[str, dict]:
    """id → {tools, hooks, providers, loadError, ok, file}；来自 v4 清单。"""
    index: Dict[str, dict] = {}
    if folder is None:
        return index
    for name in MANIFEST_NAMES:
        data = _load_json(folder / name)
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict) or not row.get('id'):
                continue
            ident = str(row['id'])
            if ident in index:
                continue
            ext = row.get('extension') if isinstance(row.get('extension'), dict) else {}
            index[ident] = {
                'name': str(row.get('name') or ext.get('name') or ident),
                'tools': _as_list(ext.get('tools') or row.get('tools')),
                'hooks': _as_list(ext.get('hooks') or row.get('hooks')),
                'providers': _as_list(ext.get('providers') or row.get('capabilities')),
                'loadError': str(ext.get('loadError') or row.get('error') or ''),
                'ok': row.get('ok') is not False,
                'file': str(row.get('file') or ''),
                'external': _as_list(row.get('external')),
                'permissions': _as_list(row.get('permissions')),
            }
        if index:
            break
    return index


def _catalog_index(folder: Optional[Path]) -> Dict[str, dict]:
    """id → {host_imports, capabilities}；来自市场导出的 catalog.json。"""
    index: Dict[str, dict] = {}
    if folder is None:
        return index
    for name in CATALOG_NAMES:
        data = _load_json(folder / name)
        items = data
        if isinstance(data, dict):
            items = data.get('plugins') or data.get('items') or data.get('extensions') or []
        if not isinstance(items, list):
            continue
        for row in items:
            if not isinstance(row, dict) or not row.get('id'):
                continue
            ident = str(row['id'])
            if ident in index:
                continue
            index[ident] = {'host_imports': _as_list(row.get('host_imports')),
                            'capabilities': _as_list(row.get('capabilities'))}
        if index:
            break
    return index


def _tier(record: dict) -> tuple:
    """按声明推算层级与阻塞原因。

    T2/T3 只在清单真给出 external/permissions 时才判定 —— 没给的字段不猜。
    """
    blockers: List[str] = []
    flags: List[str] = []
    if str(record.get('loadError') or ''):
        return 'T6-disabled', ['load-error: %s' % str(record['loadError'])[:160]], flags
    if record.get('ok') is False:
        return 'T6-disabled', ['load-failed'], flags
    if not record.get('installed') and not record.get('fileExists'):
        blockers.append('zip-missing')
    # 宿主根里装好的扩展（source='host'）本身就躺在带 src/ 的 0.4 运行时旁边，
    # ../../src 能解析；再按 zip 时代的 host-source 判 T4，会让真探测已通过的
    # 市场插件永远登记成停用。zip-only 那份仍按原来口径阻断。
    if record.get('host_imports') and record.get('source') != 'host':
        blockers.append('host-source')
        flags.append('host-imports')
    if record.get('depends'):
        blockers.append('depends-other')
    if record.get('permissions'):
        flags.append('uses-api')
    if record.get('external'):
        blockers.append('web-fetch')
        flags.append('uses-external')
    if 'zip-missing' in blockers:
        return 'T6-disabled', blockers, flags
    if 'host-source' in blockers:
        return 'T4-host-source', blockers, flags
    if 'depends-other' in blockers:
        return 'T5-depends-other', blockers, flags
    if 'uses-api' in flags:
        return 'T3-extra-api', blockers, flags
    if 'web-fetch' in blockers:
        return 'T2-web-fetch', blockers, flags
    return 'T1-ready', blockers, flags


def _host_root_state(data_dir=None) -> dict:
    """宿主运行时现状：插件跑起来要它；没准备也只是少一层，不抛异常。"""
    state = {'ready': False, 'root': '', 'source': '', 'error': ''}
    try:
        from . import qqagent_host
        root = qqagent_host.default_host_root(_data(data_dir))
        state['root'] = str(root)
        candidates = qqagent_host.runtime_source_candidates()
        state['source'] = str(candidates[0]) if candidates else ''
        state['ready'] = qqagent_host.host_root_ready(root)
    except Exception as exc:
        state['error'] = str(exc)[:200]
    return state


def _installed_records(data_dir=None) -> Dict[str, dict]:
    """已安装层：宿主根里的扩展 + bridge catalog + extensions.json 记录。

    宿主根（<data>/qq-only/qqagent_host/plugins）排最前：那才是"装进去、能跑"的
    那一份；市场目录里只是下载到的 zip。读失败静默降级，别让一层坏了拖垮整份清单。
    """
    base = market_extensions_dir(data_dir)
    records: Dict[str, dict] = {}
    try:
        from . import qqagent_host
        host_root = qqagent_host.default_host_root(_data(data_dir))
        for row in qqagent_host.list_installed(host_root):
            ident = str(row.get('id') or '')
            if not ident:
                continue
            records[ident] = {'id': ident,
                              'name': str(row.get('name') or ident),
                              'path': str(row.get('dir') or ''),
                              'tools': _as_list(row.get('tools')),
                              'hooks': _as_list(row.get('hooks')),
                              'providers': _as_list(row.get('providers')),
                              'host_imports': _as_list(row.get('hostImports')),
                              'source': 'host',
                              'fileExists': True}
    except Exception:
        pass
    catalog = _data(data_dir) / 'qqagent_bridge_catalog.json'
    if catalog.is_file() and base.is_dir():
        try:
            from . import extension_host
            for item in extension_host.discover_extensions(catalog, str(base)):
                ident = str(item.get('id') or '')
                if not ident:
                    continue
                records[ident] = {'id': ident, 'name': str(item.get('name') or ident),
                                  'path': str(item.get('file') or ''),
                                  'tools': list(item.get('tools') or []),
                                  'hooks': list(item.get('hooks') or []),
                                  'providers': list(item.get('providers') or []),
                                  'fileExists': True}
        except Exception:
            pass
    try:
        from . import qqagent_market
        for row in qqagent_market.list_extensions(_data(data_dir)):
            ident = str(row.get('id') or '')
            if not ident or ident in records:
                continue
            path = Path(str(row.get('path') or ''))
            records[ident] = {'id': ident, 'name': str(row.get('name') or ident),
                              'path': str(path), 'tools': _as_list(row.get('tools')),
                              'hooks': _as_list(row.get('hooks')),
                              'providers': _as_list(row.get('capabilities')),
                              'fileExists': path.is_file()}
    except Exception:
        pass
    return records


def discover(data_dir=None, refresh: bool = False) -> dict:
    """合并发现结果：已安装层 + 市场清单层 + 状态里手工登记的条目。

    返回 {'items': [...], 'scannedAt': iso, 'stale': bool, 'error': str}；
    任何一层读失败都只少了那一层，不抛异常。
    """
    key = str(state_path(data_dir))
    now = time.time()
    cached = _SCAN_CACHE.get(key)
    if cached and not refresh and now - cached['ts'] < SCAN_TTL:
        payload = dict(cached['payload'])
        payload['stale'] = True
        return payload
    error = ''
    folder: Optional[Path] = None
    items: Dict[str, dict] = {}
    try:
        folder = manifest_dir(data_dir)
        manifest = _manifest_index(folder)
        catalog = _catalog_index(folder)
        installed = _installed_records(data_dir)
        for ident, record in installed.items():
            meta = manifest.get(ident) or {}
            extra = catalog.get(ident) or {}
            merged = {'id': ident,
                      'name': str(record.get('name') or meta.get('name') or ident),
                      'layer': 'installed', 'installed': True,
                      'path': str(record.get('path') or ''),
                      'fileExists': bool(record.get('fileExists')),
                      'tools': list(record.get('tools') or meta.get('tools') or []),
                      'hooks': list(record.get('hooks') or meta.get('hooks') or []),
                      'providers': list(record.get('providers') or meta.get('providers') or []),
                      'loadError': str(meta.get('loadError') or ''),
                      'ok': bool(meta.get('ok', True)),
                      'source': str(record.get('source') or 'catalog'),
                      'host_imports': list(record.get('host_imports')
                                            or extra.get('host_imports') or []),
                      'external': list(meta.get('external') or []),
                      'permissions': list(meta.get('permissions') or []),
                      'depends': []}
            merged['tier'], merged['blockers'], merged['flags'] = _tier(merged)
            items[ident] = merged
        for ident, meta in manifest.items():
            if ident in items:
                continue
            extra = catalog.get(ident) or {}
            file_path = Path(str(meta.get('file') or ''))
            merged = {'id': ident, 'name': meta.get('name') or ident,
                      'layer': 'catalog', 'installed': False, 'path': str(file_path),
                      'fileExists': file_path.is_file(),
                      'tools': list(meta.get('tools') or []),
                      'hooks': list(meta.get('hooks') or []),
                      'providers': list(meta.get('providers') or []),
                      'loadError': str(meta.get('loadError') or ''),
                      'ok': bool(meta.get('ok', True)),
                      'source': 'catalog',
                      'host_imports': list(extra.get('host_imports') or []),
                      'external': list(meta.get('external') or []),
                      'permissions': list(meta.get('permissions') or []),
                      'depends': []}
            merged['tier'], merged['blockers'], merged['flags'] = _tier(merged)
            items[ident] = merged
    except Exception as exc:                                # 发现失败不当成致命错误
        error = str(exc)[:200]
    state = load(data_dir)
    for ident, entry in state['extensions'].items():
        if ident in items:
            continue
        source = str(entry.get('source') or 'market')
        items[ident] = {'id': ident, 'name': ident,
                        'layer': 'builtin' if source == 'builtin' else 'catalog',
                        'installed': False, 'path': '', 'fileExists': False,
                        'tools': [], 'hooks': [], 'providers': [],
                        'loadError': '', 'ok': True, 'host_imports': [],
                        'external': [], 'permissions': [], 'depends': [],
                        'tier': 'T1-ready' if entry.get('enabled') else 'T6-disabled',
                        'blockers': [], 'flags': ['state-only'], 'source': source}
    payload = {'items': list(items.values()), 'scannedAt': _now(), 'stale': False,
               'error': error, 'manifestDir': str(folder or '')}
    _SCAN_CACHE[key] = {'ts': now, 'payload': payload}
    return payload


def list_extensions(data_dir=None, refresh: bool = False, layer='', q='') -> List[dict]:
    """合并"发现结果 + 启用态"，可选按层与关键字过滤；只读。"""
    found = discover(data_dir, refresh)
    state = load(data_dir)
    entries = state['extensions']
    needle = str(q or '').strip().lower()
    want = str(layer or '').strip().lower()
    result = []
    for record in found['items']:
        ident = str(record.get('id') or '')
        if not ident:
            continue
        declared = entries.get(ident) or {}
        source = str(declared.get('source') or record.get('source')
                     or ('builtin' if record.get('layer') == 'builtin' else 'market'))
        item = {'id': ident,
                'name': str(record.get('name') or ident),
                'layer': str(record.get('layer') or 'catalog'),
                'source': source,
                'installed': bool(record.get('installed')),
                'enabled': bool(declared.get('enabled')),
                'tier': str(record.get('tier') or 'T1-ready'),
                'blockers': list(record.get('blockers') or []),
                'flags': list(record.get('flags') or []),
                'tools': list(record.get('tools') or []),
                'hooks': list(record.get('hooks') or []),
                'providers': list(record.get('providers') or []),
                'toolCount': len(record.get('tools') or []),
                'hookCount': len(record.get('hooks') or []),
                'providerCount': len(record.get('providers') or []),
                'path': str(record.get('path') or ''),
                'note': str(declared.get('note') or ''),
                'updatedAt': str(declared.get('updatedAt') or '')}
        if want and want not in ('all', '*') and item['layer'] != want and item['source'] != want:
            continue
        if needle:
            haystack = ' '.join([item['id'], item['name'], item['tier'],
                                 ' '.join(item['tools']), ' '.join(item['providers'])]).lower()
            if needle not in haystack:
                continue
        result.append(item)
    result.sort(key=lambda row: (0 if row['enabled'] else 1, row['layer'], row['id']))
    return result


def summary(data_dir=None, refresh: bool = False) -> dict:
    """各层计数 / 已启用数 / 可调用工具数；GUI 状态栏与端点共用。"""
    found = discover(data_dir, refresh)
    items = list_extensions(data_dir, refresh)
    layers: Dict[str, int] = {}
    tiers: Dict[str, int] = {}
    enabled = 0
    tools_total = 0
    tools_enabled = 0
    hooks_enabled = 0
    for item in items:
        layers[item['layer']] = layers.get(item['layer'], 0) + 1
        tiers[item['tier']] = tiers.get(item['tier'], 0) + 1
        tools_total += int(item['toolCount'])
        if item['enabled']:
            enabled += 1
            tools_enabled += int(item['toolCount'])
            hooks_enabled += int(item['hookCount'])
    host_root = _host_root_state(data_dir)
    plugins_prefix = ''
    if host_root['root']:
        plugins_prefix = host_root['root'].rstrip('/\\') + os.sep + 'plugins'
    host_root['installed'] = sum(
        1 for item in items
        if plugins_prefix and str(item.get('path') or '').startswith(plugins_prefix))
    return {'total': len(items), 'enabled': enabled, 'layers': layers, 'tiers': tiers,
            'toolCount': tools_total, 'enabledToolCount': tools_enabled,
            'enabledHookCount': hooks_enabled,
            'hostRoot': host_root,
            'statePath': str(state_path(data_dir)),
            'manifestDir': found.get('manifestDir') or '',
            'scannedAt': found.get('scannedAt') or '',
            'stale': bool(found.get('stale')),
            'error': found.get('error') or ''}


def scan(data_dir=None, refresh: bool = False) -> dict:
    """端点/控制台用的整份快照：发现结果 + 启用态 + 汇总。"""
    items = list_extensions(data_dir, refresh)
    return {'items': items, 'summary': summary(data_dir, refresh)}


def invalidate() -> None:
    """清掉进程内缓存（测试与"刚安装完"之后用）。"""
    _SCAN_CACHE.clear()
    _STATE_CACHE.clear()
