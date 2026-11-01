"""QQ Agent 扩展能力桥：把 provider / hook / tool 三种能力接到鲸鱼娘生命周期。

支持的三种能力（顺序就是推荐顺序）
----------------------------------
1. provider：0.4 宿主里 mod.providers[name](payload)，例如 message.owner-check、
   llm.request-params、reply.local-policy。桥把它们挂到 on_prepare / on_pre_ai /
   on_post_reply。
2. hook：0.4 宿主里的 mod.hooks[name](payload)，例如 before-context、
   before-llm-messages、after-response、before-tool、after-tool、onMessage。
   映射表见 HOOK_EVENT（与市场 hooks-map.json 一致）。
3. tool：扩展用 setup(api).registerTool() 注册的工具，由 list_tools()（同步，给
   src/tool_loop.py 的 register_plugin_tools）/ call_tool(name, arguments)（同步门面，
   内部走异步子进程）暴露给 WP2 的工具环；refresh_tools() 是异步的枚举+缓存刷新。

事件映射
--------
| hook                | Python 事件    | 0.4 宿主加载点          | 返回值语义                |
|---------------------|----------------|-------------------------|---------------------------|
| before-context      | on_pre_ai      | orchestrator.js:776     | 原地改，忽略返回          |
| before-llm-messages | on_pre_ai      | orchestrator.js:876     | 原地改 messages，忽略返回 |
| after-response      | on_post_reply  | orchestrator.js:1016    | 忽略返回                  |
| onMessage           | on_prepare     | orchestrator 消息入口   | 忽略返回                  |
| before-tool         | on_before_tool | orchestrator.js:1140    | 读 {block, reason}        |
| after-tool          | on_after_tool  | orchestrator.js:1151    | 忽略返回                  |

0.4 绝大部分 hook 靠"原地修改 payload 对象"生效，而桥每次调用是一个独立 node
进程，原地修改在进程退出时必然丢失。host-one.mjs 的对策是：hook 返回 undefined 时，
它比较调用前后的 payload，把改过的顶层字段作为 hook_result 带回（例如
before-llm-messages 的 messages、after-response 的 response）；扩展主动返回的值优先。
桥再照结果改写 Python 侧数据（messages 写回会话、response 写回回复、before-tool 的
block/arguments 真正生效）。只返回 undefined 且没改 payload 的扩展仍然是"跑得动但
改不了宿主"，bridge_status() 里能看到 ok=True 而调用方数据未变 —— 不假装修好了。

安全边界（用户必须知情）
------------------------
- 执行扩展 = 在本机执行第三方 JavaScript（host-one.mjs:55-63 动态 import 扩展的
  index.js），它能读环境变量、读写当前用户可访问的文件、联网。这不是沙箱。
- 桥做的是"准入"而不是"隔离沙箱"：只有 enabled 且被显式写进 profiles / 被自动
  发现选中的扩展才会被执行；能力名必须命中 ALLOWED_PROVIDERS / ALLOWED_HOOKS；
  一次调用一个 node 进程、跑完即退、超时强杀；扩展拿不到任何反向调用控制台的
  入口（没有回调句柄，只能看 argv 里的 payload）。
- 工具名对外是白名单式暴露：只认该扩展自己注册的 <扩展 id>__<工具名>，宿主
  自带工具（list_stickers 等）不会出现在 list_tools() 里；配置 toolAllowlist
  之后只有列出的工具可调用。

显式缺口（不伪造）
------------------
0.4 会把 store / memory / session 这类真实运行时对象传进 hook 和 provider，桥这边
没有等价物，一律传 None 并在下面 GAP_FIELDS 里注明；扩展若直接使用它们会抛错，
桥记录为该次调用失败，而不是塞假对象让日志"看起来跑通"。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

from src import extension_host as host
from src.plugins import PluginBase, PluginMeta, FieldSchema

# 默认白名单；不在表里的 provider/hook 直接跳过，避免配置写错就执行任意能力。
ALLOWED_PROVIDERS = {'message.owner-check', 'message.privilege-check', 'message.gate',
                     'reply.local-policy', 'llm.request-params', 'chat.ban-state',
                     'message.identity'}
ALLOWED_HOOKS = {'before-context', 'before-llm-messages', 'onMessage', 'after-response',
                 'before-tool', 'after-tool'}

# 宿主根（兼容宿主）判定与源码扫描上限，供 _host_root / scan_extension_dirs 早期使用。
HOST_ROOT_MARKERS = ('src/config.js', 'src/plugin-loader.js')
SOURCE_SUFFIXES = ('.js', '.mjs', '.cjs')
SOURCE_BUDGET = 2 * 1024 * 1024

EVENT_BY_PROVIDER = {
    'message.owner-check': 'on_prepare',
    'message.privilege-check': 'on_prepare',
    'message.gate': 'on_prepare',
    'chat.ban-state': 'on_prepare',
    'message.identity': 'on_prepare',
    'llm.request-params': 'on_pre_ai',
    'reply.local-policy': 'on_post_reply',
}

# 这三个 provider 在 0.4 文档的"被核心消费的能力名"里查不到调用方，市场里却有扩展
# 声明它们 —— 历史上一度被当成"宿主永不调用"的死能力。本仓库给它们接了自己的消费点
# （src/qq_app.py 的回复门禁、主人提权、机器人身份），所以这里改成"宿主会消费"：
# bridge_status() 用这个集合把三项从 deadInHost 里摘掉，控制台不再报"宿主不支持"。
HOST_CONSUMED_PROVIDERS = {'message.gate', 'message.privilege-check', 'message.identity'}

PROVIDER_PRIORITY = {
    'message.owner-check': 0,
    'message.privilege-check': 1,
    'message.gate': 2,
    'chat.ban-state': 3,
    'message.identity': 4,
    'llm.request-params': 5,
    'reply.local-policy': 6,
}

# hook 名 → Python 事件名；与市场 hooks-map.json 的 hooks 表逐条对齐。
HOOK_EVENT = {
    'before-context': 'on_pre_ai',
    'before-llm-messages': 'on_pre_ai',
    'onMessage': 'on_prepare',
    'after-response': 'on_post_reply',
    'before-tool': 'on_before_tool',
    'after-tool': 'on_after_tool',
}

# 自动发现 hook 型扩展时的偏好顺序：before-tool 是唯一以返回值为契约的 hook，
# 能真正改变宿主行为，所以排第一；然后是只在同进程里原地改、跨进程改不动的几种。
HOOK_DISCOVERY_ORDER = ('before-tool', 'before-context', 'before-llm-messages',
                        'after-response', 'onMessage', 'after-tool')

# 0.4 传真实对象、桥给不出的字段；显式 None + 这里写明来源缺口。
GAP_FIELDS = {
    'store': '0.4 的插件状态存储（plugin-store），桥没有等价物',
    'memory': '0.4 的长期记忆对象，桥没有等价物',
    'session': '0.4 的会话对象（含 sessionId/统计），桥只有 message 快照',
    'selfId': '机器人自身 QQ 号；仅当 message 里带 self_id 时才有值',
    'persona': '人设对象；桥不读 ai_engine 内部状态，只有 message 自带时才映射',
}

TOOL_EVENTS = ('on_before_tool', 'on_after_tool')
TOOL_CACHE_NAME = 'qqagent_bridge_tools.json'
PLUGIN_ID = 'qqagent_bridge'


def _strip_tool_prefix(name) -> str:
    """tool_loop 把插件工具注册成 <插件 id>__<工具全名>；扩展只认得自己的全名。"""
    text = str(name or '')
    prefix = PLUGIN_ID + '__'
    return text[len(prefix):] if text.startswith(prefix) else text


def _run_coro(coro):
    """同步上下文里跑协程：没有事件循环就 asyncio.run，有就换一个线程跑。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box = {}
    def runner():
        try:
            box['value'] = asyncio.run(coro)
        except BaseException as error:  # noqa: BLE001 - 线程里的异常要带回调用方
            box['error'] = error
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if 'error' in box:
        return {'ok': False, 'error': {'kind': 'internal', 'message': str(box['error'])[:200]}}
    return box.get('value', {'ok': False, 'error': {'kind': 'internal', 'message': 'no result'}})


def _capability_names(values) -> list:
    """catalog 里 capabilities 可能是字符串，也可能是 {name/id} 对象。"""
    names = []
    for value in values or []:
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, dict):
            name = str(value.get('name') or value.get('id') or '')
            if name:
                names.append(name)
    return names


def catalog_profiles(catalog_path, extensions_dir='', limit=2) -> list:
    """从 QQ Agent 市场清单生成可直接交给桥的 provider 配置。

    只选编解码层白名单里的 provider；优先没有 host_imports 的包，
    避免一上来就挑中最容易受源码布局影响的扩展。
    """
    path = Path(str(catalog_path or ''))
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    items = data if isinstance(data, list) else (
        data.get('plugins') or data.get('items') or data.get('extensions') or [])
    base = Path(str(extensions_dir or '')).resolve() if extensions_dir else None
    candidates = []
    for item in items:
        if not isinstance(item, dict) or item.get('kind', 'plugin') != 'plugin':
            continue
        providers = [name for name in _capability_names(item.get('capabilities'))
                     if name in ALLOWED_PROVIDERS]
        if not providers:
            continue
        extension = Path(str(item.get('file') or ''))
        # 目录型扩展（<host_root>/plugins/<id>）同样可调用，host-one.mjs 接受目录。
        if not (extension.is_file() or extension.is_dir()):
            continue
        if base is not None:
            try:
                extension.resolve().relative_to(base)
            except (OSError, ValueError):
                continue
        candidates.append((1 if item.get('host_imports') else 0,
                           PROVIDER_PRIORITY.get(providers[0], 99),
                           len(item.get('tools') or []), providers[0], extension, item))
    candidates.sort(key=lambda row: (row[0], row[1], row[2], str(row[5].get('id') or '')))
    cap = max(1, min(8, int(limit or 2)))
    selected = []
    seen = set()
    for row in candidates:
        if row[3] in seen:
            continue
        selected.append(row)
        seen.add(row[3])
        if len(selected) >= cap:
            break
    if len(selected) < cap:
        selected.extend(row for row in candidates if row not in selected)
        selected = selected[:cap]
    profiles = []
    for _, _, _, provider, extension, item in selected:
        profiles.append({
            'extensionPath': str(extension),
            'extensionId': str(item.get('id') or extension.stem),
            'provider': provider,
            'hookName': '',
            'event': EVENT_BY_PROVIDER.get(provider, 'on_prepare'),
            'enabled': True,
            'timeout': 5,
            'label': str(item.get('name') or item.get('id') or extension.stem),
        })
    return profiles


def _source_text(extension_dir, budget=SOURCE_BUDGET) -> str:
    """把扩展的 JS 源码读成一段文本（只在发现阶段用，总量有上限）。"""
    folder = Path(str(extension_dir or ''))
    if not folder.is_dir():
        return ''
    files = sorted(item for item in folder.iterdir()
                   if item.is_file() and item.suffix.lower() in SOURCE_SUFFIXES)
    chunks, total = [], 0
    for item in files[:40]:
        try:
            text = item.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        chunks.append(text)
        total += len(text)
        if total >= budget:
            break
    return '\n'.join(chunks)


# 对象键 / 方括号字面量里的字符串。tool_schema._mask 连字符串内容一起遮，但扩展声明
# provider/hook 一律写成带引号的对象键（host-one.mjs 读 mod.providers[...]），全遮会让
# 真正的声明消失，所以只把这几种位置按原样还原回来。
_KEY_LITERAL_RE = re.compile(r"""(['"])[ ]*\1(?=\s*:)""")
_INDEX_LITERAL_RE = re.compile(r"""\[(['"])[ ]*\1\]""")


def _code_text(src) -> str:
    """只剩代码区的源码文本：注释/正则/普通字符串都被遮掉，长度与换行保持不变。

    走 tool_schema._mask（同事实现的等长遮罩），再用上面两个正则把"对象键"和
    "方括号字面量"还原 —— 只有代码区里成对出现的引号才会被遮罩后还原，注释整段被
    遮光，所以注释里写 `'message.gate':` 也不会被算成声明。模块缺失、遮罩报错或长度
    变了都回退原始文本：发现功能绝不能因为遮罩坏掉而失效。
    """
    src = str(src or '')
    module = _schema_module()
    if module is None:
        return src
    try:
        masked = module._mask(src)
    except Exception:
        return src
    if not isinstance(masked, str) or len(masked) != len(src):
        return src
    out = list(masked)
    for pattern in (_KEY_LITERAL_RE, _INDEX_LITERAL_RE):
        for match in pattern.finditer(masked):
            out[match.start():match.end()] = src[match.start():match.end()]
    return ''.join(out)


# 真的引用宿主的三种写法：import('...') / require('...') / import x from '...'。
# 遮罩后实参内容只剩空格（引号本身保留），所以正则只负责定位实参位置，
# 内容一律按偏移回原始文本读 —— 既不会读到注释里的路径，也不会因为实参被遮而漏判。
_IMPORT_CALL_RE = re.compile(r"""\b(?:import|require)\s*\(\s*(['"])[^'"\n]*\1""")
_IMPORT_FROM_RE = re.compile(r"""\bfrom\s*(['"])[^'"\n]*\1""")
_COMMENT_RE = re.compile(r"""//[^\n]*|/\*[\s\S]*?\*/""")


def _strip_comments(src) -> str:
    """只把注释换成等长空格，字符串原样保留（遮罩模块不可用时的保守回退）。"""
    text = str(src or '')
    return _COMMENT_RE.sub(lambda m: ' ' * len(m.group(0)), text)


def _import_arguments(code, source) -> list:
    """从代码区文本里定位 import/require/from 的实参，再按偏移取 source 里的原文。"""
    found = []
    for pattern in (_IMPORT_CALL_RE, _IMPORT_FROM_RE):
        for match in pattern.finditer(code):
            found.append(source[match.start():match.end()])
    return found


def _host_imports(src) -> list:
    """扩展是否**真的**引用宿主源码（`../..` 相对路径），返回 ['../../'] 或 []。

    口径：只认 import(...) / require(...) 的实参与 import 语句里 `from '...'` 的实参，
    而且必须在代码区。判定走 tool_schema._mask（注释、字符串内容一起遮成空格），再用
    上面的正则按偏移回原始文本取实参 —— 注释里写一句 `import('../../src/config.js')`
    整段被遮光、拿不到 `../..`，骗不到排序优先位；真有
    `await import('../../src/config.js')` 的扩展照样命中。

    不整段保留字符串：README / 说明字符串里出现的示例路径不是引用，那才是主要的误判源。
    遮罩模块缺失、报错或长度变了就退回"只去注释、字符串保留"的保守口径（_strip_comments）：
    仍然不会把注释算成引用，但 `const p = '../../x'; import(p)` 这种变量实参、以及字符串
    里带 '//' 被当成行注释而截掉同行后续代码，都是已知残留的**漏判**（只会少算，不会多算）。
    """
    text = str(src or '')
    if not text:
        return []
    module = _schema_module()
    if module is not None:
        try:
            masked = module._mask(text)
        except Exception:
            masked = None
        if isinstance(masked, str) and len(masked) == len(text):
            return ['../../'] if any('../..' in arg
                                    for arg in _import_arguments(masked, text)) else []
    stripped = _strip_comments(text)
    return ['../../'] if any('../..' in arg
                            for arg in _import_arguments(stripped, stripped)) else []


def scan_extension_dirs(extensions_dir) -> list:
    """没有 catalog.json 时直接扫已安装扩展目录（<host_root>/plugins/<id>/plugin.json）。

    返回形状与 host.discover_extensions() 一致，只是 file 字段是目录（host-one.mjs
    同样接受目录）。providers/hooks 只能靠源码里的字面量判定：清单不在场时这是唯一
    来源，判不出来就是空列表（不猜）；判定前先过 _code_text()，注释和普通字符串里
    提到同名能力不算声明。tools 留空——工具清单要起 node 枚举，交给 refresh_tools()。
    """
    folder = Path(str(extensions_dir or ''))
    if not folder.is_dir():
        return []
    result = []
    for child in sorted(folder.iterdir()):
        if not child.is_dir():
            continue
        manifest = {}
        for name in ('plugin.json', 'skill.json'):
            candidate = child / name
            if not candidate.is_file():
                continue
            try:
                data = json.loads(candidate.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                manifest = data
                break
        if not manifest:
            continue
        ident = str(manifest.get('id') or child.name)
        text = _source_text(child)
        # 能力名只在代码区里认：源码文本匹配会把注释/普通字符串里的名字也算进来。
        code = _code_text(text)
        result.append({'id': ident,
                       'name': str(manifest.get('name') or ident),
                       'file': child,
                       # 只看真引用：注释/普通字符串里写 ../../ 不算（见 _host_imports）。
                       'host_imports': _host_imports(text),
                       'providers': sorted(name for name in ALLOWED_PROVIDERS if name in code),
                       'hooks': sorted(name for name in ALLOWED_HOOKS if name in code),
                       'tools': []})
    return result


def _cached_tool_counts(data_dir=None) -> dict:
    """已缓存的工具数 {扩展 id: 条数}；只读 <data>/qqagent_bridge_tools.json，不起 node。

    缓存缺失或坏掉一律空字典 —— 排序退回目录序，发现功能本身不受影响。
    """
    path = Path(str(data_dir or host.default_data_dir())) / TOOL_CACHE_NAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    table = data.get('extensions') if isinstance(data, dict) else None
    counts = {}
    for key, value in (table or {}).items():
        ident = str(key or '')
        if not ident:
            continue
        tools = value if isinstance(value, list) else (
            value.get('tools') if isinstance(value, dict) else None)
        counts[ident] = len([item for item in tools or [] if isinstance(item, dict)])
    return counts


def _dir_tool_rank(item) -> int:
    """目录扫描候选的排序键（取负 = 工具数降序）；清单路径没有这个字段，恒为 0。"""
    return -int(item.get('_tool_count') or 0)


def _discovered(catalog_path, extensions_dir='', data_dir=None) -> list:
    """清单优先；清单不在场（或筛完没东西）就扫扩展目录。

    目录扫描这条路要定候选顺序：maxProfiles 很小的时候谁排前面就是谁被选中，只看
    目录名会选中空壳扩展。这里给每个候选补上工具数（读缓存，不起 node），由
    catalog_*_profiles() 在截断之前按"工具数降序 → 名字升序"排。
    """
    items = host.discover_extensions(catalog_path, extensions_dir)
    if items:
        return items
    scanned = scan_extension_dirs(extensions_dir)
    counts = _cached_tool_counts(data_dir)
    for item in scanned:
        item['_tool_count'] = int(counts.get(str(item.get('id') or ''),
                                            len(item.get('tools') or [])))
    return scanned


def catalog_hook_profiles(catalog_path, extensions_dir='', limit=2, hooks=None,
                         data_dir=None) -> list:
    """挑出声明了 hook 的扩展，生成可直接派发的 hook profile。

    catalog.json 没有 hooks 字段，host.discover_extensions() 会回退读同目录的
    all-plugins-v4.json（extension.hooks）。市场 512 个扩展里 296 个声明了 hook，
    之前自动发现完全看不到它们。
    """
    wanted = {name for name in (hooks or ALLOWED_HOOKS)} & ALLOWED_HOOKS
    candidates = []
    for item in _discovered(catalog_path, extensions_dir, data_dir):
        names = [name for name in item['hooks'] if name in wanted]
        if not names:
            continue
        for name in names:
            rank = HOOK_DISCOVERY_ORDER.index(name) if name in HOOK_DISCOVERY_ORDER else 99
            candidates.append((_dir_tool_rank(item), 1 if item['host_imports'] else 0,
                               rank, item['id'], item, name))
    candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[5]))
    cap = max(1, min(8, int(limit or 2)))
    profiles = []
    # 去重键必须带扩展 id：before-llm-messages 这类热点 hook 几乎每个插件都会声明，
    # 只按 hook 名去重会让排在前面的扩展独吞，后面插件的同名 hook 永远注册不上。
    used = set()
    for _, _, _, ident, item, name in candidates:
        key = (ident, name)
        if key in used:
            continue
        used.add(key)
        profiles.append({'extensionPath': str(item['file']), 'extensionId': ident,
                         'provider': '', 'hookName': name,
                         'event': HOOK_EVENT.get(name, ''), 'enabled': True, 'timeout': 5,
                         'hostImports': list(item['host_imports']),
                         'label': str(item['name'] or ident)})
        if len(profiles) >= cap:
            break
    return profiles


def catalog_tool_profiles(catalog_path, extensions_dir='', limit=2, data_dir=None) -> list:
    """挑出注册了 tool 的扩展（第三种能力）；工具清单由 list_tools() 现场枚举。"""
    candidates = []
    for item in _discovered(catalog_path, extensions_dir, data_dir):
        if not item['tools']:
            continue
        candidates.append((_dir_tool_rank(item), 1 if item['host_imports'] else 0,
                           len(item['tools']), item['id'], item))
    candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    cap = max(1, min(8, int(limit or 2)))
    profiles = []
    for _, _, _, ident, item in candidates[:cap]:
        profiles.append({'extensionPath': str(item['file']), 'extensionId': ident,
                         'provider': '', 'hookName': '', 'event': '',
                         'tools': list(item['tools']), 'enabled': True, 'timeout': 10,
                         'label': str(item['name'] or ident)})
    return profiles


def discover_profiles(catalog_path, extensions_dir='', limit=2, data_dir=None) -> list:
    """自动发现：provider / hook / tool 轮转取，保证 hook 型扩展也有名额。

    返回的 profile 带 kind 字段，方便控制台显示"这一条是哪种能力"。
    早先版本把 provider 排满后 hook 才捡剩下的名额，而市场里 7 个白名单 provider
    足以填满任何 maxProfiles，296 个 hook 型扩展于是永远进不了自动模式；这里改成
    三条道 1:1:1 轮转。
    """
    cap = max(1, min(8, int(limit or 2)))
    lanes = [
        [dict(item, kind='provider') for item in catalog_profiles(catalog_path, extensions_dir, cap)],
        [dict(item, kind='hook') for item in
         catalog_hook_profiles(catalog_path, extensions_dir, cap, data_dir=data_dir)],
        [dict(item, kind='tool') for item in
         catalog_tool_profiles(catalog_path, extensions_dir, cap, data_dir=data_dir)],
    ]
    profiles = []
    seen = set()
    while len(profiles) < cap and any(lanes):
        for lane in lanes:
            if not lane:
                continue
            item = lane.pop(0)
            key = (item.get('extensionId'), item['kind'],
                   item.get('provider') or item.get('hookName') or item.get('event') or '')
            if key in seen:
                continue
            seen.add(key)
            profiles.append(item)
            if len(profiles) >= cap:
                break
    return profiles[:cap]


# ── 0.4 宿主语义的 payload ───────────────────────────────────────────────────
def chat_context(message) -> dict:
    """orchestrator.js:765-773 的 skillContext。能映射的映射，不能的给 None。"""
    message = message if isinstance(message, dict) else {}
    kind = str(message.get('kind') or '')
    target = str(message.get('target') or '') if kind == 'group' else ''
    sender = str(message.get('sender_id') or '')
    chat_key = '%s:%s' % (kind or 'unknown', target or sender or '')
    return {'chatKey': chat_key, 'kind': kind or None, 'chatId': target or sender or None,
            'chatName': str(message.get('group_name') or message.get('target_name') or '') or None,
            'model': None, 'provider': None,
            'visionEnabled': None, 'searchEnabled': None, 'proactive': None,
            'sessionId': None}


def _entry_from_message(message) -> dict:
    """0.4 的 triggerEntries 是"触发本次回复的若干条消息"；桥一次只看到一条。"""
    message = message if isinstance(message, dict) else {}
    return {'senderId': str(message.get('sender_id') or ''),
            'senderName': str(message.get('sender_name') or message.get('nickname') or ''),
            'mid': str(message.get('message_id') or message.get('msg_id') or ''),
            'text': str(message.get('text') or '')[:1500],
            'media': list(message.get('media') or []) if isinstance(message.get('media'), list) else [],
            'self': False, 'isPoke': False,
            # 入站时就算好的 @ 标记（qq_transports 写的就是 message['at']）。丢了它，
            # owner-identity 之类按"主人在这一批里叫了机器人"判定的人设切换永远不成立。
            'atMe': bool(message.get('at')), 'at': bool(message.get('at'))}


def _messages_from(conversation) -> list:
    """把 Python 会话转成 0.4 的 [{role, content}]；认不出的条目直接丢，不伪造。"""
    if not isinstance(conversation, (list, tuple)):
        return []
    result = []
    for item in conversation:
        if not isinstance(item, dict):
            continue
        role = str(item.get('role') or '')
        if role not in ('system', 'user', 'assistant', 'tool'):
            continue
        content = item.get('content')
        result.append({'role': role,
                       'content': content if isinstance(content, str) else str(content or '')})
    return result


def stage_payload(event, message=None, conversation=None, reply=None, tool_name='',
                  args_raw=None, result=None) -> dict:
    """按事件生成 0.4 语义的 payload：11 个契约字段永远都在，缺的显式 None。"""
    message = message if isinstance(message, dict) else {}
    payload = dict(chat_context(message))
    messages = _messages_from(conversation)
    payload.update({
        'triggerEntries': [_entry_from_message(message)] if message else [],
        'selfId': str(message.get('self_id') or '') or None,
        'persona': message.get('persona') if isinstance(message.get('persona'), dict) else None,
        'messages': messages or None,
        'session': None,
        'response': reply if isinstance(reply, str) else None,
        'toolName': str(tool_name or ''),
        'argsRaw': args_raw if isinstance(args_raw, (dict, list)) else None,
        'result': result if isinstance(result, (dict, list, str, int, float, bool)) else None,
        'store': None,
        'memory': None,
    })
    # 兼容旧版桥的扁平字段（有些第三方扩展可能读过），0.4 语义里没有这些键。
    payload['text'] = str(message.get('text') or '')[:1500]
    payload['userId'] = str(message.get('sender_id') or '')
    payload['groupId'] = (str(message.get('target') or '')
                          if message.get('kind') == 'group' else '')
    payload['tool'] = str(tool_name or '')
    payload['arguments'] = args_raw if isinstance(args_raw, dict) else {}
    return payload


def apply_hook_result(event, value, *, conversation=None, reply=None, arguments=None,
                      result=None) -> dict:
    """把 hook/provider 的返回值真正落到 Python 侧数据上（返回即权威）。"""
    changes = {'changed': False, 'reply': reply, 'arguments': arguments, 'result': result,
               'messages': None, 'block': False, 'reason': '', 'prompt': ''}
    if not isinstance(value, (dict, list, str)):
        return changes
    if event == 'on_pre_ai':
        messages = None
        if isinstance(value, dict) and isinstance(value.get('messages'), list):
            messages = _messages_from(value['messages'])
        elif isinstance(value, list):
            messages = _messages_from(value)
        if messages and isinstance(conversation, list):
            conversation[:] = messages
            changes.update(changed=True, messages=messages)
        if isinstance(value, dict):
            for key in ('extra_system', 'systemPrompt', 'system', 'prompt'):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    changes['prompt'] = text.strip()
                    changes['changed'] = True
                    break
    elif event == 'on_post_reply':
        text = None
        if isinstance(value, dict):
            for key in ('response', 'reply', 'text'):
                if isinstance(value.get(key), str):
                    text = value[key]
                    break
        elif isinstance(value, str):
            text = value
        if isinstance(text, str) and text != (reply or ''):
            changes.update(changed=True, reply=text)
    elif event == 'on_before_tool':
        if isinstance(value, dict):
            if value.get('block'):
                changes.update(changed=True, block=True,
                               reason=str(value.get('reason') or ''))
            raw = value.get('argsRaw') if isinstance(value.get('argsRaw'), dict) else None
            args = raw or (value.get('arguments')
                           if isinstance(value.get('arguments'), dict) else None)
            if args is not None:
                changes.update(changed=True, arguments=args)
    elif event == 'on_after_tool':
        if isinstance(value, dict):
            for key in ('result', 'output', 'value'):
                if key in value:
                    changes.update(changed=True, result=value[key])
                    break
    elif event == 'on_prepare':
        if isinstance(value, dict):
            # rules 是市场里 owner-identity 这类 provider 的契约字段（注入提示词的规则文本），
            # 不认它，扩展算出来的规则就只进了审计、对回复零影响。
            for key in ('extra_system', 'systemPrompt', 'prompt', 'rules'):
                if isinstance(value.get(key), str) and value[key].strip():
                    changes.update(changed=True, prompt=value[key].strip())
                    break
    return changes


# message.identity 只吃这三个标量键。市场里的 qq-identity 返回的是一张函数表
# （resolveUserId/describeMember/listMembers），把函数当覆盖值拼进提示词只会打出
# 一串 <function ...>；认得的键之外扩展多给什么都没用。
IDENTITY_OVERRIDE_KEYS = ('selfId', 'nickname', 'persona')


def gate_verdict(value):
    """message.gate 返回值 → 拦截原因（None 即放行）。

    市场里的 mention-skip 用 `skip`，任务书写法是 `block`，两种都认；只认严格的
    JSON true —— 扩展返回 `{skip: "no"}` 这类噪声不能把消息挡掉（fail-open 方向）。
    """
    if not isinstance(value, dict):
        return None
    for key in ('block', 'skip'):
        if value.get(key) is True:
            reason = value.get('reason') or value.get('message') or ''
            return str(reason).strip()[:200] or key
    return None


def _profile_event(profile) -> str:
    """算出一条 profile 该挂在哪个 Python 事件上。

    hookName 最权威（hook 的身份决定了它的载荷形状），其次是显式 event，
    最后按 provider 名字查表。
    """
    hook_name = str(profile.get('hookName') or '').strip()
    if hook_name:
        return HOOK_EVENT.get(hook_name, '')
    event = str(profile.get('event') or '').strip()
    if event:
        return event
    return EVENT_BY_PROVIDER.get(str(profile.get('provider') or '').strip(), '')


def _capability(profile):
    """(kind, capability, capability_arg)；没有可调用能力时返回 None。"""
    hook_name = str(profile.get('hookName') or '').strip()
    if hook_name:
        if hook_name not in ALLOWED_HOOKS:
            return None
        return ('hook', 'hook', hook_name)
    provider = str(profile.get('provider') or '').strip()
    if provider and provider in ALLOWED_PROVIDERS:
        return ('provider', provider, '')
    return None


PROVIDER_AUDIT_LIMIT = 3
PROVIDER_DIGEST_CHARS = 40


def _provider_digest(value, limit=PROVIDER_DIGEST_CHARS) -> str:
    """provider 返回值压成一行短摘要（审计里够用，不塞整段 JSON）。"""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = ''
    else:
        text = str(value or '')
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit] + '…'


def provider_audit(message, limit=PROVIDER_AUDIT_LIMIT) -> str:
    """把 _remember() 写下的 `_qqagent_provider` 读成一行审计文本（没记录时返回空串）。

    格式 `<provider>=ok|error|timeout(<40 字值摘要>)`，多条用 '; ' 连接，超出的记 '+N'。
    _remember() 只写不读等于死数据：这条消息被哪个 provider 处理过、结果 ok 还是出错/
    超时，只有读出来才有诊断价值 —— src/qq_app.py 的 decision() 记进 decisions 的
    providers 字段（status()['decisions'] 直接可见），提权路径并进已有的 note 行。

    覆盖范围：on_prepare / on_pre_ai / on_post_reply 三个钩子、message.gate、
    message.identity、message.privilege-check（都经过 _remember）。conversation_allowed()
    的白名单判定只有 sender 字符串、没有 message 字典，那条路径不产生记录，也不为它造字典。
    """
    if not isinstance(message, dict):
        return ''
    values = message.get('_qqagent_provider')
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list) or not values:
        return ''
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = PROVIDER_AUDIT_LIMIT
    items = []
    for item in values[:cap]:
        if not isinstance(item, dict):
            continue
        name = str(item.get('profile') or '?')
        state = 'ok' if item.get('ok') else ('timeout' if item.get('timed_out') else 'error')
        digest = _provider_digest(item.get('value'))
        items.append('%s=%s%s' % (name, state, ('(%s)' % digest) if digest else ''))
    extra = len(values) - len(items)
    if extra > 0:
        items.append('+%d' % extra)
    return '; '.join(items)


def _profile_timeout(profile, default=10.0) -> float:
    return host.clamp_timeout(profile.get('timeout', default), host.clamp_timeout(default))


def _extension_id(profile) -> str:
    """扩展 id 的唯一解析规则：先 extensionId，缺失再退回 extensionPath 的目录名。

    准入（_registry_disabled）、工具面、日志、bridge_status 全都用这一份，别再各写一遍。
    """
    ident = str(profile.get('extensionId') or '').strip()
    if ident:
        return ident
    return Path(str(profile.get('extensionPath') or '')).stem


# ── 宿主根派生：别让"装了没反应"落在空字符串上 ──────────────────────────────


def _is_host_root(path) -> bool:
    """宿主根的判定标准：两个 marker 都在（路径存在但缺 src/ 的不算）。"""
    try:
        item = Path(str(path))
    except (TypeError, ValueError):
        return False
    return all((item / name).is_file() for name in HOST_ROOT_MARKERS)


def _host_root(conf, data_dir=None) -> str:
    """真宿主根；找不到返回 ''，绝不抛错。

    顺序：配置里显式给的 hostRoot/sourceDir（且它真是宿主根）→
    src/qqagent_host.resolve_host_root()。qqagent_host 正在被同事改，这里只在
    真要用路径时惰性 import —— import 期拉它等于给插件加载加一条硬依赖。
    """
    conf = conf or {}
    for key in ('hostRoot', 'host_root', 'sourceDir', 'source_dir'):
        raw = str(conf.get(key) or '').strip()
        if raw and _is_host_root(raw):
            return raw
    try:
        from .. import qqagent_host
    except Exception:
        try:
            from src import qqagent_host
        except Exception:
            return ''
    try:
        resolved = qqagent_host.resolve_host_root(data_dir=str(data_dir) if data_dir else None)
    except Exception:
        return ''
    return str(resolved) if resolved else ''


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_HOST_SCRIPT = REPO_ROOT / 'runtime' / 'qqagent-host' / 'host-one.mjs'


def _default_host_script(base) -> str:
    """宿主根里的 host-one.mjs；没有就用仓库自带的 runtime 载荷，别再让桥空手。"""
    candidate = Path(base) / 'host-one.mjs'
    if candidate.is_file():
        return str(candidate)
    return str(RUNTIME_HOST_SCRIPT) if RUNTIME_HOST_SCRIPT.is_file() else ''


def _resolve_paths(conf, data_dir=None) -> dict:
    """用户填了就用用户的；没填的从宿主根派生（sourceDir / hostScript / extensionsDir）。"""
    conf = conf or {}
    root = _host_root(conf, data_dir)
    source_dir = str(conf.get('sourceDir') or conf.get('source_dir') or '').strip()
    host_script = str(conf.get('hostScript') or conf.get('host_script') or '').strip()
    extensions_dir = str(conf.get('extensionsDir') or conf.get('extensions_dir') or '').strip()
    if root:
        base = Path(root)
        if not source_dir:
            source_dir = root
        if not host_script:
            host_script = _default_host_script(base)
        if not extensions_dir:
            candidate = base / 'plugins'
            if candidate.is_dir():
                extensions_dir = str(candidate)
    return {'host_root': root, 'source_dir': source_dir,
            'host_script': host_script, 'extensions_dir': extensions_dir}


# ── 工具参数 schema：来自扩展源码声明，宿主不回传 ──────────────────────────

def _clean_schema(value) -> dict:
    """把工具参数清洗成合法 JSON Schema；脏数据一律降级成空对象 schema。"""
    if not isinstance(value, dict) or str(value.get('type') or '').strip() != 'object':
        return {'type': 'object', 'properties': {}}
    props = value.get('properties')
    if not isinstance(props, dict):
        return {'type': 'object', 'properties': {}}
    clean = {'type': 'object', 'properties': {}}
    for key, spec in props.items():
        name = str(key or '').strip()
        if name and isinstance(spec, dict):
            clean['properties'][name] = dict(spec)
    required = value.get('required')
    if isinstance(required, list):
        keep = [str(item) for item in required if str(item) in clean['properties']]
        if keep:
            clean['required'] = keep
    extra = value.get('additionalProperties')
    if isinstance(extra, bool):
        clean['additionalProperties'] = extra
    return clean


def _schema_module():
    """惰性导入 src/tool_schema.py（同事实现）；缺失或坏掉都只是"没有 schema 来源"。"""
    try:
        from .. import tool_schema
    except Exception:
        try:
            from src import tool_schema
        except Exception:
            return None
    return tool_schema


def _schema_dir(conf, data_dir=None) -> str:
    """扩展源码目录，按安装落点排四路优先级：

    1. profile 自己给的 extension 目录（目录型扩展）；
    2. <sourceDir>/plugins/<id>（开发态 0.4 运行副本、或宿主根）；
    3. <data>/qq-only/qqagent_host/plugins/<id>（控制台准备宿主后的安装落点）；
    4. <data>/qqagent_extensions/<id>（历史落点，保留兼容）。
    都没有就返回 ''，schema 静默回退到空参数，不抛错。
    """
    raw_extension = str(conf.get('extension') or '').strip()
    if raw_extension:
        path = Path(raw_extension)
        if path.is_dir():
            return str(path)
    ident = str(conf.get('extension_id') or '')
    if not ident:
        return ''
    source_dir = str(conf.get('source_dir') or conf.get('sourceDir') or '').strip()
    resolved = _resolve_paths({'sourceDir': source_dir}, data_dir)
    roots = []
    if resolved['source_dir']:
        roots.append(Path(resolved['source_dir']) / 'plugins')
    if data_dir:
        base = Path(str(data_dir))
        roots.append(base / 'qq-only' / 'qqagent_host' / 'plugins')
        roots.append(base / 'qqagent_extensions')
    for root in roots:
        candidate = root / ident
        if candidate.is_dir():
            return str(candidate)
    return ''


def _dir_stamp(path) -> str:
    """目录指纹（mtime+大小）：源码改了就让缓存里的 schema 失效。"""
    if not path:
        return ''
    try:
        stat = os.stat(str(path))
    except OSError:
        return ''
    return '%d-%d' % (int(stat.st_mtime), stat.st_size)


# ── 模块级 API（WP2 工具环 / 验收脚本可直接调用，不依赖插件实例） ────────────
async def list_tools(host_script, source_dir, extension, extension_id='', node='node',
                     timeout=10, data_dir=None) -> dict:
    """枚举一个扩展注册的 tool（只留它自己注册的那些）。"""
    return await host.list_tools(node=node, host_script=host_script, extension=extension,
                                 source_dir=source_dir, timeout=timeout, data_dir=data_dir,
                                 extension_id=extension_id or Path(str(extension)).stem)


async def call_tool(name, arguments=None, *, host_script, source_dir, extension,
                    extension_id='', node='node', timeout=10, data_dir=None,
                    allowed_ids=None) -> dict:
    """调用一个扩展的 tool；allowed_ids 给了就先过白名单，不启动进程。"""
    return await host.call_tool(node=node, host_script=host_script, extension=extension,
                                source_dir=source_dir, tool_id=name,
                                arguments=arguments or {}, timeout=timeout,
                                data_dir=data_dir,
                                extension_id=extension_id or Path(str(extension)).stem,
                                allowed_ids=allowed_ids)


def _ledger_row_counts(data_dir=None) -> dict:
    """账本行数按 kind 拆开；工具枚举（kind='list'）是行数不是派发，单独计。"""
    counts = {'provider': 0, 'hook': 0, 'tool': 0, 'list': 0}
    for row in host.read_log(data_dir):
        kind = str(row.get('kind') or '')
        if kind in counts:
            counts[kind] += 1
    return counts


def _with_row_counts(status, data_dir=None) -> dict:
    """给 host 的状态快照补上 list_rows：calls 是总行数，派发次数看 *_calls。"""
    status = dict(status or {})
    status['list_rows'] = _ledger_row_counts(data_dir)['list']
    return status


def bridge_status(data_dir=None, enabled_extensions=None) -> dict:
    """provider/hook/tool 调用计数与最近错误（读 data/qqagent_bridge_calls.jsonl）。

    `calls` 是**账本总行数**，包含 `kind='list'` 的工具枚举行；要"真实派发次数"
    请看 provider_calls / hook_calls / tool_calls（各自只统计自己那种派发），
    枚举行单独记在 list_rows。恒等式：calls = 三者之和 + list_rows。
    """
    return _with_row_counts(host.bridge_status(data_dir, enabled_extensions), data_dir)



# ── 插件本体 ───────────────────────────────────────────────────────────────
class QqAgentBridge(PluginBase):
    META = PluginMeta(
        id='qqagent_bridge', name='QQ Agent 能力桥', category='utility',
        enabled_by_default=False,
        description='把指定的 QQ Agent provider/hook/tool 接入消息生命周期；仅主人、默认关闭、单扩展隔离。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用能力桥'),
            'hostScript': FieldSchema(type='string', label='host-one.mjs 路径', file_picker='file',
                                      file_extensions=['.mjs'],
                                      description='留空则自动使用控制台准备的兼容宿主根里的 host-one.mjs。'),
            'nodeExe': FieldSchema(type='string', label='Node 可执行文件', description='默认 node；需 Node 20+（运行时载荷 engines 要求 >=20）。'),
            'hostRoot': FieldSchema(type='string', label='宿主根（host-one.mjs 与 src/ 所在目录）',
                                    file_picker='directory',
                                    description='留空由控制台按 data/qq-only/qqagent_host 自动准备；一般不用手填。'),
            'sourceDir': FieldSchema(type='string', label='QQ Agent 0.4 源码目录',
                                     description='留空则自动使用控制台准备的兼容宿主根。'),
            'extensionPath': FieldSchema(type='string', label='扩展 zip', file_picker='file',
                                         file_extensions=['.zip']),
            'catalogPath': FieldSchema(type='string', label='市场清单 catalog.json', file_picker='file',
                                       file_extensions=['.json'],
                                       description='可选。有清单时按清单挑白名单 provider/hook，没有清单时直接扫扩展目录。不会执行清单里的所有扩展。'),
            'extensionsDir': FieldSchema(type='string', label='扩展目录', file_picker='directory',
                                         description='留空则自动使用兼容宿主根下的 plugins 目录。自动模式只从这里挑选扩展，防止误用清单外的路径。'),
            'autoDiscover': FieldSchema(type='bool', label='按市场清单自动选择'),
            'maxProfiles': FieldSchema(type='int', label='自动选择上限', min=1, max=8,
                                       description='每轮最多调用几个扩展；默认 2，越多越慢。'),
            'provider': FieldSchema(type='string', label='provider 名称',
                                    description='例如 message.owner-check；与 hookName 二选一。'),
            'hookName': FieldSchema(type='string', label='hook 名称',
                                    description='before-context / before-llm-messages / after-response / onMessage / before-tool / after-tool；与 provider 二选一。'),
            'event': FieldSchema(type='string', label='挂载事件',
                                 description='on_prepare / on_pre_ai / on_post_reply / on_before_tool / on_after_tool；留空按 provider/hook 名字自动判定。'),
            'ownerOnly': FieldSchema(type='bool', label='只对主人调用'),
            'ownerIds': FieldSchema(type='string', label='主人 QQ 号（逗号分隔）'),
            'privilegeEscalation': FieldSchema(
                type='bool', label='允许 message.privilege-check 提权',
                description='默认关闭。打开后，私聊里"原本不是主人"的发送者，只有扩展明确返回 '
                            'allow=true 才会被当主人（扩展报错/超时/不表态一律仍然不是主人）；'
                            '既有主人判定不会被扩展降级。风险：等于把"谁是主人"交给扩展，'
                            '扩展装错或被人替换会直接拿到主人权限，只在确认扩展可信时打开。'),
            'injectPrompt': FieldSchema(type='bool', label='把 provider 提示注入模型'),
            'timeout': FieldSchema(type='int', label='超时秒数', min=2, max=60),
            'preAiBudgetMs': FieldSchema(type='int', label='同步链路总预算（毫秒）', min=0, max=8000,
                                         description='模型链路目前是同步调用，桥在这里最多占用这么久；0 表示同步链路完全不调用扩展。'),
            'toolAllowlist': FieldSchema(type='string', label='允许的工具名（逗号分隔）',
                                         description='留空表示允许所选扩展自己注册的全部工具；宿主自带工具永远不会被列出。'),
            'profiles': FieldSchema(type='string', label='批量配置（JSON）', multiline=True,
                                    description='可选。数组，每项可覆盖 hostScript/sourceDir/extensionPath/provider/hookName/event/ownerOnly/ownerIds/timeout；最多 8 项。'),
        },
        default_settings={'enabled': False, 'hostScript': '', 'nodeExe': 'node', 'hostRoot': '', 'sourceDir': '', 'extensionPath': '',
                          'catalogPath': '', 'extensionsDir': '', 'autoDiscover': False, 'maxProfiles': 2,
                          'provider': '', 'hookName': '', 'event': '', 'ownerOnly': True, 'ownerIds': '',
                          'privilegeEscalation': False,
                          'injectPrompt': False, 'timeout': 10, 'preAiBudgetMs': 1500, 'toolAllowlist': '',
                          'profiles': ''},
    )

    # ── 配置 ────────────────────────────────────────────────────────────────
    def _data_dir(self):
        manager = getattr(self, '_manager', None)
        for attr in ('data_dir', '_data_dir'):
            value = getattr(manager, attr, None)
            if value:
                return Path(str(value))
        return host.default_data_dir()

    def _budget_ms(self) -> int:
        try:
            return max(0, min(8000, int(self.get('preAiBudgetMs', 1500))))
        except (TypeError, ValueError):
            return 1500

    def _tool_allowlist(self) -> list:
        raw = str(self.get('toolAllowlist', '') or '')
        return [item.strip() for item in raw.split(',') if item.strip()]

    def _registry_disabled(self, ident) -> bool:
        """这个扩展在 extension_registry 里是否停用（唯一权威）。

        兜底语义：registry 模块导入不到、状态文件读不了、或 id 为空时一律返回
        False —— 桥保持既有行为，绝不因为状态存储出问题而停止工作。
        只管 registry 认得的扩展（登记过或能被发现到）：手写 profiles 里的历史
        id 不在其中，继续走既有行为；市场/清单里的扩展则默认停用。
        """
        ident = str(ident or '').strip()
        if not ident:
            return False
        try:
            from ..extension_registry import is_enabled, is_known
        except Exception:
            try:
                from src.extension_registry import is_enabled, is_known
            except Exception:
                return False
        try:
            return bool(is_known(ident, self._data_dir())) and not is_enabled(ident, self._data_dir())
        except Exception:
            return False

    def _profiles(self):
        base = {key: self.get(key) for key in ('hostScript', 'nodeExe', 'sourceDir', 'extensionPath',
                                                'provider', 'hookName', 'event', 'ownerOnly',
                                                'ownerIds', 'injectPrompt', 'timeout')}
        # 三项路径用户没填时由宿主根派生：装好宿主 + 装好扩展就该能跑，不该再手填。
        resolved = _resolve_paths({key: self.get(key, '') for key in
                                   ('hostScript', 'sourceDir', 'extensionsDir')}, self._data_dir())
        base['hostScript'] = resolved['host_script']
        base['sourceDir'] = resolved['source_dir']
        base['extensionsDir'] = resolved['extensions_dir']
        # extensionId 也要参与合并：用户显式写的 id 不能被丢掉，否则准入判断与调用账本
        # 会退回目录名（zip 装的扩展目录名不一定等于 id，停用门禁就会认错人）。
        base['extensionId'] = self.get('extensionId', '') or ''
        raw = str(self.get('profiles', '') or '').strip()
        if not raw:
            if self.get('autoDiscover', False):
                cap = max(1, min(8, int(self.get('maxProfiles', 2) or 2)))
                # maxProfiles 是「最多选几个扩展」（字段说明写的就是扩展数），不是
                # 能力条数：一个插件同时声明 provider + 两个 hook 时，三条能力都要带上，
                # 否则用户把上限设成 4、却只装了 3 个插件，仍会静默少一条能力。
                # 先按上限 8 发现，再滤掉 registry 里停用的扩展，最后按扩展分组截断：
                # 过滤必须排在截断之前，否则停用的扩展会白占名额，自动模式"挑不出人"。
                discovered = discover_profiles(self.get('catalogPath', ''),
                                               resolved['extensions_dir'], 8,
                                               data_dir=self._data_dir())
                discovered = [item for item in discovered
                              if not self._registry_disabled(_extension_id(item))]
                if discovered:
                    picked = []
                    chosen = set()
                    for item in discovered:
                        ident = _extension_id(item)
                        if ident not in chosen:
                            if len(chosen) >= cap:
                                continue
                            chosen.add(ident)
                        picked.append(item)
                    if picked:
                        return [{**base, **item} for item in picked]
            return [base]
        try:
            items = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(items, list):
            return []
        result = []
        for item in items[:8]:
            if not isinstance(item, dict) or item.get('enabled', True) is False:
                continue
            merged = dict(base)
            merged.update({k: item[k] for k in base if k in item})
            result.append(merged)
        return result

    def _event_profiles(self, message, event):
        """挑出该事件该跑的 profile；工具事件没有消息上下文，走单独的准入。"""
        if not self.enabled:
            return []
        message = message if isinstance(message, dict) else {}
        no_owner_gate = bool(message.get('_no_owner_gate'))
        result = []
        for profile in self._profiles():
            if profile.get('enabled', True) is False:
                continue
            # 扩展管理里停用的扩展在这里也要拦住 —— 否则"停用"只对工具面无效果对钩子面无效。
            # 语义与工具面完全一致：registry 看不见的历史手写 profile 照旧派发（is_known 语义）。
            if self._registry_disabled(_extension_id(profile)):
                continue
            if _profile_event(profile) != event:
                continue
            if _capability(profile) is None:
                continue
            if profile.get('ownerOnly', False) and not no_owner_gate:
                wanted = {x.strip() for x in str(profile.get('ownerIds', '') or '').split(',') if x.strip()}
                if str(message.get('sender_id') or '') not in wanted:
                    continue
            result.append(profile)
        return result

    # ── 单次调用 ────────────────────────────────────────────────────────────
    def _settings_for(self, profile) -> dict:
        resolved = _resolve_paths(
            {'hostScript': profile.get('hostScript') or self.get('hostScript', '') or '',
             'sourceDir': profile.get('sourceDir') or self.get('sourceDir', '') or '',
             'extensionsDir': profile.get('extensionsDir') or self.get('extensionsDir', '') or ''},
            self._data_dir())
        return {'node': str(profile.get('nodeExe') or self.get('nodeExe', 'node') or 'node'),
                'host_script': resolved['host_script'],
                'extension': str(profile.get('extensionPath') or ''),
                'source_dir': resolved['source_dir'],
                'extensions_dir': resolved['extensions_dir'],
                'host_root': resolved['host_root'],
                'extension_id': _extension_id(profile),
                'timeout': _profile_timeout(profile, self.get('timeout', 10))}

    def _call_ready(self, profile, conf) -> bool:
        return bool(host_path_ok(conf['host_script']) and Path(conf['extension']).exists()
                    and Path(conf['source_dir']).is_dir())

    async def _stage(self, profile, payload, event, timeout=None):
        """异步调用一个 profile；超时/崩溃都被 extension_host 收敛成结果字典。

        timeout 只在调用方有更紧的截止时间时传（门禁/身份要守住 preAiBudgetMs）：
        取 profile 配置与剩余预算的较小值，下限仍由 extension_host.clamp_timeout 兜住。
        """
        conf = self._settings_for(profile)
        capability = _capability(profile)
        if capability is None:
            return {'ok': False, 'error': 'capability not allowed', 'value': None}
        kind, name, argument = capability
        seconds = conf['timeout'] if timeout is None else min(conf['timeout'], timeout)
        return await host.run_capability(
            node=conf['node'], host_script=conf['host_script'], extension=conf['extension'],
            source_dir=conf['source_dir'], capability=name, capability_arg=argument,
            payload=payload, timeout=seconds, data_dir=self._data_dir(), kind=kind,
            extension_id=conf['extension_id'], path_label='async')

    def _stage_sync(self, profile, payload, timeout=None):
        """同步链路（qq_app 目前同步调用 on_pre_ai）用的调用，受总预算约束。"""
        conf = self._settings_for(profile)
        capability = _capability(profile)
        if capability is None:
            return {'ok': False, 'error': 'capability not allowed', 'value': None}
        kind, name, argument = capability
        seconds = conf['timeout'] if timeout is None else min(conf['timeout'], timeout)
        return host.run_capability_sync(
            node=conf['node'], host_script=conf['host_script'], extension=conf['extension'],
            source_dir=conf['source_dir'], capability=name, capability_arg=argument,
            payload=payload, timeout=seconds, data_dir=self._data_dir(), kind=kind,
            extension_id=conf['extension_id'], path_label='sync')

    def _remember(self, message, result, profile):
        """把 provider/hook 返回值挂到消息上，供后续事件和调试查看（不改基础回复）。

        `_qqagent_provider` 是原始清单，`_qqagent_provider_note` 是 provider_audit()
        压出来的一行摘要 —— qq_app 的审计（decision 的 providers 字段、提权 note）读的是
        后者，写在这里保证"写过就有人读"。
        """
        if not isinstance(result, dict) or 'value' not in result:
            return
        value = result.get('value')
        if not value:
            return
        if not isinstance(message, dict):
            return
        values = message.setdefault('_qqagent_provider', [])
        if not isinstance(values, list):
            values = [values]
            message['_qqagent_provider'] = values
        values.append({'profile': profile.get('provider') or profile.get('hookName'),
                       'ok': bool(result.get('ok')), 'value': value,
                       'timed_out': bool(result.get('timed_out'))})
        message['_qqagent_provider_note'] = provider_audit(message)
        if profile.get('injectPrompt', False):
            message['_qqagent_prompt'] = json.dumps(value, ensure_ascii=False)[:1200]

    def _hint(self, message) -> str:
        hint = str((message or {}).get('_qqagent_prompt') or '').strip()
        return 'QQ Agent 能力结果（只作参考，不得越权）：' + hint if hint else ''

    # ── 生命周期钩子 ────────────────────────────────────────────────────────
    async def on_prepare(self, message, runtime):
        """onMessage hook 与 message.* provider 的落点。manager.run_on_prepare 会 await。"""
        for profile in self._event_profiles(message, 'on_prepare'):
            payload = stage_payload('on_prepare', message=message)
            result = await self._stage(profile, payload, 'on_prepare')
            self._remember(message, result, profile)
            changes = apply_hook_result('on_prepare', result.get('value'))
            hint = changes.get('prompt') or ''
            if hint:
                message['_qqagent_prompt'] = hint[:1200]
            if result.get('error') and runtime is not None and hasattr(runtime, 'note'):
                runtime.note('QQ Agent %s 调用失败：%s' % (profile.get('provider') or profile.get('hookName'),
                                                          str(result['error'])[:160]))

    async def on_pre_ai_async(self, message, conversation, runtime):
        """before-context / before-llm-messages / llm.request-params 的落点（真正 await）。"""
        payload_hooks = ('before-context', 'before-llm-messages')
        events = []
        for profile in self._event_profiles(message, 'on_pre_ai'):
            shape = _capability(profile)
            if shape is None:
                continue
            kind, name, argument = shape
            if kind == 'hook' and argument in payload_hooks:
                events.append((profile, stage_payload('on_pre_ai', message=message,
                                                      conversation=conversation)))
            elif kind == 'provider':
                # llm.request-params 只由 transform_request() 拿真实请求体；
                # 这里没有请求体，再调一次只会拿到 body=None 的壳，白起 node、审计里留假记录。
                if name == 'llm.request-params':
                    continue
                payload = stage_payload('on_pre_ai', message=message, conversation=conversation)
                payload['body'] = None
                payload['api'] = None
                payload['context'] = dict(chat_context(message))
                events.append((profile, payload))
        extra = ''
        for profile, payload in events:
            result = await self._stage(profile, payload, 'on_pre_ai')
            self._remember(message, result, profile)
            changes = apply_hook_result('on_pre_ai', result.get('value'),
                                        conversation=conversation)
            if changes.get('prompt'):
                extra = changes['prompt']
            if result.get('error') and runtime is not None and hasattr(runtime, 'note'):
                runtime.note('QQ Agent %s 调用失败：%s' % (profile.get('provider') or profile.get('hookName'),
                                                          str(result['error'])[:160]))
        extra = extra or self._hint(message)
        return {'extra_system': extra} if extra else {}

    def on_pre_ai(self, message, conversation, runtime):
        """同步兜底：src/qq_app.py 现在走 run_on_pre_ai（同步），这里受总预算约束。

        预算为 0 时完全不调用扩展，只注入消息上已有的结果，避免拖慢回复。
        """
        budget = self._budget_ms()
        if budget > 0:
            deadline = time.monotonic() + budget / 1000.0
            for profile in self._event_profiles(message, 'on_pre_ai'):
                if time.monotonic() >= deadline:
                    break
                shape = _capability(profile)
                if shape is None or (shape[0] == 'provider' and shape[1] == 'llm.request-params'):
                    continue
                payload = stage_payload('on_pre_ai', message=message, conversation=conversation)
                if shape[0] == 'provider':
                    payload['body'] = None
                    payload['api'] = None
                    payload['context'] = dict(chat_context(message))
                result = self._stage_sync(profile, payload)
                self._remember(message, result, profile)
                apply_hook_result('on_pre_ai', result.get('value'), conversation=conversation)
        hint = self._hint(message)
        return {'extra_system': hint} if hint else {}

    async def on_post_reply(self, message, reply, runtime):
        """after-response hook 与 reply.local-policy provider；必须把 reply 原样还回去。"""
        text = reply
        for profile in self._event_profiles(message, 'on_post_reply'):
            payload = stage_payload('on_post_reply', message=message, reply=text)
            payload['entries'] = []
            result = await self._stage(profile, payload, 'on_post_reply')
            self._remember(message, result, profile)
            changes = apply_hook_result('on_post_reply', result.get('value'), reply=text)
            if changes.get('changed') and isinstance(changes.get('reply'), str):
                text = changes['reply']
            if result.get('error') and runtime is not None and hasattr(runtime, 'note'):
                runtime.note('QQ Agent %s 调用失败：%s' % (profile.get('provider') or profile.get('hookName'),
                                                          str(result['error'])[:160]))
        return text if isinstance(text, str) else reply

    async def on_before_tool(self, tool_id, arguments, runtime):
        """before-tool hook：0.4 里唯一以返回值为契约的 hook，block 真正生效。"""
        tool_id = _strip_tool_prefix(tool_id)
        context = {'sender_id': '', '_no_owner_gate': True}
        result = {'arguments': dict(arguments or {})}
        for profile in self._event_profiles(context, 'on_before_tool'):
            payload = stage_payload('on_before_tool', message={'tool': tool_id},
                                    tool_name=tool_id, args_raw=result['arguments'])
            stage = await self._stage(profile, payload, 'on_before_tool')
            self._remember(context, stage, profile)
            changes = apply_hook_result('on_before_tool', stage.get('value'),
                                        arguments=result['arguments'])
            if changes.get('block'):
                result['block'] = True
                result['reason'] = changes.get('reason') or 'blocked by QQ Agent hook'
            if isinstance(changes.get('arguments'), dict):
                result['arguments'] = changes['arguments']
        return result

    async def on_after_tool(self, tool_id, arguments, value, runtime):
        """after-tool hook：返回非 None 即替换工具输出（manager.run_on_after_tool 语义）。"""
        tool_id = _strip_tool_prefix(tool_id)
        context = {'sender_id': '', '_no_owner_gate': True}
        output = value
        # 0.4 的 after-tool 契约里 result 是对象（token-saver 读 result.content 做截断）；
        # 本仓库工具环传的是裸字符串。不包一层，插件永远看不到 content，截断静默失效。
        wrapped = isinstance(value, str)
        for profile in self._event_profiles(context, 'on_after_tool'):
            result_payload = {'content': output} if wrapped else output
            payload = stage_payload('on_after_tool', message={'tool': tool_id}, tool_name=tool_id,
                                    args_raw=arguments or {}, result=result_payload)
            stage = await self._stage(profile, payload, 'on_after_tool')
            self._remember(context, stage, profile)
            changes = apply_hook_result('on_after_tool', stage.get('value'), result=output)
            if changes.get('changed'):
                new_value = changes.get('result')
                if wrapped and isinstance(new_value, dict):
                    new_value = new_value.get('content')
                if isinstance(new_value, str):
                    output = new_value
                elif not wrapped:
                    output = new_value
        return output

    # ── 三个原本"宿主不来调"的 provider：本仓库自己的消费点 ─────────────────
    # 0.4 内核从不询问这三个能力，桥因此把它们接到本控制台的真实决策点上：
    # message.gate 决定这条消息回不回、message.privilege-check 决定陌生人算不算主人、
    # message.identity 决定"机器人是谁"。方向性是硬约束，别改成"扩展说啥就是啥"：
    # 门禁 fail-open（扩展挂不能导致机器人不吭声），提权 fail-closed（扩展说 yes 不能
    # 凭空造出主人权限，除非设置显式打开）。
    def _provider_profiles(self, message, provider):
        """只挑声明了这一个 provider 的 profile。

        on_prepare 上同时挂着 owner-check/gate/identity 好几个 provider，直接复用
        _event_profiles 会把同一事件上的其它 provider 一起再调一遍（重复占预算、账本多行）。
        """
        result = []
        for profile in self._event_profiles(message, EVENT_BY_PROVIDER[provider]):
            shape = _capability(profile)
            if shape is not None and shape[0] == 'provider' and shape[1] == provider:
                result.append(profile)
        return result

    @staticmethod
    def _note_failure(runtime, profile, result):
        if result.get('error') and runtime is not None and hasattr(runtime, 'note'):
            runtime.note('QQ Agent %s 调用失败：%s'
                         % (profile.get('provider') or profile.get('hookName'),
                            str(result['error'])[:160]))

    async def message_gate(self, message, runtime=None) -> dict:
        """message.gate → {'block', 'reason', 'extensionId', 'timedOut'}。

        fail-open：桥关闭、没装扩展、扩展抛错、超时、返回空值，一律按放行处理 ——
        机器人不吭声比多说一句糟得多，扩展出错绝不能变成"机器人没反应"。
        """
        verdict = {'block': False, 'reason': '', 'extensionId': '', 'timedOut': False}
        budget = self._budget_ms()
        if budget <= 0:
            return verdict
        deadline = time.monotonic() + budget / 1000.0
        for profile in self._provider_profiles(message, 'message.gate'):
            left = deadline - time.monotonic()
            if left <= 0:
                verdict['timedOut'] = True
                break
            payload = stage_payload('on_prepare', message=message)
            # 市场里 mention-skip 解构的是 entries；0.4 的规范名是 triggerEntries，两个都给。
            payload['entries'] = list(payload.get('triggerEntries') or [])
            result = await self._stage(profile, payload, 'on_prepare', timeout=left)
            self._remember(message, result, profile)
            if result.get('error'):
                self._note_failure(runtime, profile, result)
                continue
            reason = gate_verdict(result.get('value'))
            if reason is not None:
                verdict.update(block=True, reason=reason,
                               extensionId=_extension_id(profile),
                               timedOut=bool(result.get('timed_out')))
                break
        return verdict

    async def message_identity(self, message, runtime=None) -> dict:
        """message.identity → 只含扩展真的覆盖了的键（selfId/nickname/persona）。

        报错/超时/非标量值一律跳过：调用方拿到的原值继续生效（扩展不表态就换回默认）。
        """
        found = {}
        budget = self._budget_ms()
        if budget <= 0:
            return found
        deadline = time.monotonic() + budget / 1000.0
        for profile in self._provider_profiles(message, 'message.identity'):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            payload = stage_payload('on_prepare', message=message)
            payload['entries'] = list(payload.get('triggerEntries') or [])
            result = await self._stage(profile, payload, 'on_prepare', timeout=left)
            self._remember(message, result, profile)
            if result.get('error'):
                self._note_failure(runtime, profile, result)
                continue
            value = result.get('value')
            if not isinstance(value, dict):
                continue
            for key in IDENTITY_OVERRIDE_KEYS:
                # 先到先得：多个身份扩展打架时以排在前面的为准，后者不覆盖已有值。
                raw = value.get(key)
                if key in found or not isinstance(raw, str) or not raw.strip():
                    continue
                found[key] = raw.strip()[:120]
        return found

    def privilege_allow(self, message, runtime=None) -> dict:
        """message.privilege-check → {'allow', 'reason', 'extensionId', 'timedOut'}。

        fail-closed，且**只用于把陌生人提成主人**这一个方向：
        - 设置 privilegeEscalation 默认 False，那时这里根本不派发（不起进程、不写账本），
          陌生人不会因为装了扩展就被当主人；
        - 打开后也只接受扩展明确 allow=True；报错/超时/不表态一律 False；
        - 调用方只在"原判定不是主人"时才问它，所以既有主人判定不会被扩展降级。
        """
        denied = {'allow': False, 'reason': '', 'extensionId': '', 'timedOut': False}
        if not bool(self.get('privilegeEscalation', False)):
            return denied
        budget = self._budget_ms()
        if budget <= 0:
            return denied
        deadline = time.monotonic() + budget / 1000.0
        for profile in self._provider_profiles(message, 'message.privilege-check'):
            left = deadline - time.monotonic()
            if left <= 0:
                denied['timedOut'] = True
                break
            payload = stage_payload('on_prepare', message=message)
            payload['entries'] = list(payload.get('triggerEntries') or [])
            result = self._stage_sync(profile, payload, timeout=left)
            self._remember(message, result, profile)
            if result.get('error'):
                self._note_failure(runtime, profile, result)
                continue
            value = result.get('value')
            if isinstance(value, dict) and value.get('allow') is True:
                return {'allow': True,
                        'reason': str(value.get('reason') or '').strip()[:200],
                        'extensionId': _extension_id(profile),
                        'timedOut': bool(result.get('timed_out'))}
        return denied

    # ── 给 WP2 工具环用的便捷入口 ──────────────────────────────────────────
    async def transform_messages(self, conversation, message=None) -> list:
        """用 before-llm-messages 改写真实请求的 messages（WP2 传改造后的列表给模型）。"""
        conversation = list(conversation or [])
        payload = stage_payload('on_pre_ai', message=message or {}, conversation=conversation)
        for profile in self._event_profiles(message or {'_no_owner_gate': True}, 'on_pre_ai'):
            shape = _capability(profile)
            if shape is None or shape[0] != 'hook' or shape[2] != 'before-llm-messages':
                continue
            result = await self._stage(profile, payload, 'on_pre_ai')
            apply_hook_result('on_pre_ai', result.get('value'), conversation=conversation)
            payload['messages'] = conversation
        return conversation

    async def transform_request(self, body, context=None) -> dict:
        """用 llm.request-params 改写真实请求体，返回 {'body', 'omitTemperature'}。"""
        body = dict(body or {})
        context = dict(context or {})
        message = {'sender_id': str(context.get('senderId') or ''), '_no_owner_gate': True}
        omit = False
        for profile in self._event_profiles(message, 'on_pre_ai'):
            shape = _capability(profile)
            if shape is None or shape[0] != 'provider':
                continue
            # 0.4 契约：llm.request-params 收到的是完整请求体本身（不是桥的
            # payload 外壳）；token-saver 这类插件直接读 body.tools。
            result = await self._stage(profile, body, 'on_pre_ai')
            value = result.get('value')
            if isinstance(value, dict):
                nested = value.get('body')
                if isinstance(nested, dict) and nested:
                    body = dict(nested)
                    omit = bool(value.get('omitTemperature', omit))
                elif value and 'body' not in value:
                    body = dict(value)
                    omit = bool(value.get('omitTemperature', omit))
        return {'body': body, 'omitTemperature': omit}

    # ── 工具面：tool_loop.register_plugin_tools 的同步契约 ──────────────────
    def _tool_cache_path(self) -> Path:
        return Path(self._data_dir()) / TOOL_CACHE_NAME

    def _load_tool_cache(self) -> dict:
        """读缓存；旧格式 {扩展 id: [工具]} 自动升级成 {'tools','dir','stamp'}。"""
        try:
            data = json.loads(self._tool_cache_path().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        table = data.get('extensions') if isinstance(data, dict) else None
        result = {}
        for key, value in (table or {}).items():
            ident = str(key or '')
            if not ident:
                continue
            if isinstance(value, list):            # 旧格式：只有工具清单
                result[ident] = {'tools': [tool for tool in value if isinstance(tool, dict)],
                                 'dir': '', 'stamp': ''}
            elif isinstance(value, dict):
                tools = value.get('tools')
                result[ident] = {'tools': [tool for tool in tools or [] if isinstance(tool, dict)],
                                 'dir': str(value.get('dir') or ''),
                                 'stamp': str(value.get('stamp') or '')}
        return result

    def _read_tool_cache(self) -> dict:
        """扁平视图 {扩展 id: [工具]}，给只想知道清单的调用方用。"""
        return {key: entry['tools'] for key, entry in self._load_tool_cache().items()}

    def _write_tool_cache(self, table) -> None:
        payload = {}
        for key, value in (table or {}).items():
            entry = value if isinstance(value, dict) else {'tools': value}
            payload[str(key)] = {'tools': [tool for tool in entry.get('tools') or []
                                          if isinstance(tool, dict)],
                                 'dir': str(entry.get('dir') or ''),
                                 'stamp': str(entry.get('stamp') or '')}
        try:
            path = self._tool_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'generated_at': round(time.time(), 3),
                                        'extensions': payload}, ensure_ascii=False, indent=1),
                            encoding='utf-8')
        except (OSError, ValueError):
            pass

    def _allowed_tools(self, entries) -> list:
        allow = set(self._tool_allowlist())
        result = []
        for item in entries or []:
            # id 才是运行时全名（<扩展 id>__<工具名>），name 只是展示名，不能拿来注册。
            name = str(item.get('id') or item.get('name') or '')
            if not name or (allow and name not in allow):
                continue
            result.append({'name': name,
                           'description': str(item.get('description') or ''),
                           'parameters': _clean_schema(item.get('parameters'))})
        return result

    def _enrich_tools(self, tools, conf) -> tuple:
        """用扩展源码声明补 description / parameters；返回 (tools, dir, stamp)。

        tool_schema 不可用、目录缺失、解析失败都只意味着"没有声明"，保持既有行为。
        """
        directory = _schema_dir(conf, self._data_dir())
        stamp = _dir_stamp(directory)
        ident = str(conf.get('extension_id') or '')
        table = {}
        if directory:
            module = _schema_module()
            if module is not None:
                try:
                    table = module.extract_tools(directory) or {}
                except Exception:
                    table = {}
            if not isinstance(table, dict):
                table = {}
        result = []
        for tool in tools or []:
            item = dict(tool)
            name = str(item.get('id') or '')
            short = name[len(ident) + 2:] if ident and name.startswith(ident + '__') else name
            declared = table.get(short) or table.get(name) or {}
            if not isinstance(declared, dict):
                declared = {}
            if declared.get('description') and not item.get('description'):
                item['description'] = str(declared['description'])
            item['parameters'] = _clean_schema(declared.get('parameters'))
            result.append(item)
        return result, directory, stamp

    def _profile_tools_sync(self, profile):
        """同步枚举一个扩展的工具并补 schema；返回 (tools, error, dir, stamp)。"""
        conf = self._settings_for(profile)
        if not self._call_ready(profile, conf):
            return [], 'not ready: %s' % conf['extension'], '', ''
        result = host.run_capability_sync(
            node=conf['node'], host_script=conf['host_script'], extension=conf['extension'],
            source_dir=conf['source_dir'], capability='', payload=None, timeout=conf['timeout'],
            data_dir=self._data_dir(), kind='list', extension_id=conf['extension_id'],
            path_label='sync')
        if result.get('error'):
            return [], str(result['error'])[:200], '', ''
        raw = result.get('raw') or {}
        # 裸 capability='' 枚举会把宿主自带工具（list_stickers/check_holiday…）也带回来，
        # 只保留该扩展自己注册的 <扩展 id>__<工具名>。
        tools, directory, stamp = self._enrich_tools(host.owned_tools(raw, _extension_id(profile)), conf)
        return tools, '', directory, stamp

    def list_tools(self) -> list:
        """同步工具清单（tool_loop.register_plugin_tools 在同步上下文里调它）。

        工具名一律用 host-one 运行时给出的全名 <扩展 id>__<工具名>，不自己回拼。
        清单来自 data/qqagent_bridge_tools.json 缓存；缓存缺失时现枚举一次并写回，
        之后每次启动都读缓存（枚举要起 node 进程，不能每次都压在注册表构建上）。
        """
        if not self.enabled:
            return []
        raw_cache = self._load_tool_cache()
        # registry 是启用态的唯一权威：缓存里已停用扩展的工具一律不暴露，否则
        # 用户停用了却还能调到工具。缓存只剩停用条目时同样不回落到现枚举。
        cache = {key: dict(entry) for key, entry in raw_cache.items()
                 if not self._registry_disabled(key)}
        changed = False
        for ident, entry in cache.items():
            directory = str(entry.get('dir') or '')
            # 源码没变就不重算 schema，更不启 node 进程（cache-hit 必须 0ms 级）。
            if not directory or _dir_stamp(directory) == entry.get('stamp'):
                continue
            tools, _directory, stamp = self._enrich_tools(
                entry.get('tools') or [], {'extension': directory, 'extension_id': ident})
            entry['tools'], entry['stamp'] = tools, stamp
            changed = True
        # 只有"缓存里一条都没有"才现枚举；缓存里只剩停用条目时不回落到现枚举
        # （否则每次调用都会拉起 node，缓存等于没有）。
        if not cache and not raw_cache:
            for profile in self._profiles():
                if not str(profile.get('extensionPath') or ''):
                    continue
                # 现枚举与缓存读同一道 registry 门禁，免得冷/热两次调用给出不同答案。
                if self._registry_disabled(_extension_id(profile)):
                    continue
                tools, _error, directory, stamp = self._profile_tools_sync(profile)
                if tools:
                    cache[_extension_id(profile)] = {'tools': tools, 'dir': directory,
                                                     'stamp': stamp}
                    changed = True
        if changed:
            merged = dict(raw_cache)
            merged.update(cache)          # 只增不删，免得擦掉其它扩展的缓存
            self._write_tool_cache(merged)
        entries = [tool for entry in cache.values() for tool in entry.get('tools') or []]
        return self._allowed_tools(entries)

    async def refresh_tools(self) -> dict:
        """真去枚举每个所选扩展的工具并刷新缓存；返回 {'ok','tools','error'}。"""
        if not self.enabled:
            return {'ok': False, 'tools': [], 'error': 'bridge disabled'}
        table = {}
        errors = []
        for profile in self._profiles():
            if not str(profile.get('extensionPath') or ''):
                continue
            conf = self._settings_for(profile)
            if not self._call_ready(profile, conf):
                continue
            result = await host.list_tools(node=conf['node'], host_script=conf['host_script'],
                                           extension=conf['extension'], source_dir=conf['source_dir'],
                                           timeout=conf['timeout'], data_dir=self._data_dir(),
                                           extension_id=conf['extension_id'])
            tools = [tool for tool in result.get('tools') or [] if isinstance(tool, dict)]
            if tools:
                tools, directory, stamp = self._enrich_tools(tools, conf)
                table[_extension_id(profile)] = {'tools': tools, 'dir': directory,
                                                 'stamp': stamp}
            if result.get('error'):
                errors.append(str(result['error'])[:200])
        if table:
            merged = self._load_tool_cache()
            merged.update(table)
            self._write_tool_cache(merged)
        entries = [tool for entry in table.values() for tool in entry.get('tools') or []]
        return {'ok': not errors, 'tools': self._allowed_tools(entries), 'error': '; '.join(errors)}

    async def _call_tool_async(self, name, arguments=None, timeout=None) -> dict:
        if not self.enabled:
            return {'ok': False, 'tool': name,
                    'error': {'kind': 'disabled', 'message': 'bridge disabled'}}
        allow = set(self._tool_allowlist())
        if allow and name not in allow:
            return {'ok': False, 'tool': name,
                    'error': {'kind': 'denied', 'message': 'tool not in toolAllowlist: %s' % name}}
        # 工具全名是 <扩展 id>__<工具名>，据此直接判定归属扩展是否启用（不启动进程）。
        if '__' in str(name):
            owner = str(name).split('__')[0]
            if self._registry_disabled(owner):
                return {'ok': False, 'tool': name,
                        'error': {'kind': 'disabled',
                                  'message': '扩展 %s 已停用；在「扩展管理」里启用后才能调用' % owner}}
        for item in self._profiles():
            conf = self._settings_for(item)
            if not self._call_ready(item, conf):
                continue
            listed = await host.list_tools(node=conf['node'], host_script=conf['host_script'],
                                           extension=conf['extension'], source_dir=conf['source_dir'],
                                           timeout=conf['timeout'], data_dir=self._data_dir(),
                                           extension_id=conf['extension_id'])
            ids = [tool['id'] for tool in listed.get('tools') or [] if isinstance(tool, dict)]
            if name not in ids:
                continue
            result = await host.call_tool(
                node=conf['node'], host_script=conf['host_script'], extension=conf['extension'],
                source_dir=conf['source_dir'], tool_id=name, arguments=arguments or {},
                timeout=host.clamp_timeout(timeout if timeout is not None else conf['timeout']),
                data_dir=self._data_dir(), extension_id=conf['extension_id'], allowed_ids=ids)
            if result.get('ok'):
                return {'ok': True, 'tool': name, 'value': result.get('value'),
                        'duration_ms': result.get('duration_ms')}
            return {'ok': False, 'tool': name,
                    'error': {'kind': 'timeout' if result.get('timed_out') else 'tool',
                              'message': str(result.get('error') or 'tool failed')[:400]}}
        return {'ok': False, 'tool': name,
                'error': {'kind': 'unavailable', 'message': 'tool not available: %s' % name}}

    def call_tool(self, name, arguments=None, timeout=None) -> dict:
        """同步门面：tool_loop 在工作线程里调用它，返回值必须 JSON 可序列化。

        错误形状与 MCP 侧一致：{'ok': False, 'error': {'kind': ..., 'message': ...}}。
        """
        return _run_coro(self._call_tool_async(name, arguments, timeout))

    def bridge_status(self) -> dict:
        """provider/hook/tool 调用计数 + 最近错误，供控制台端点与验收脚本读取。

        `calls` 是**账本总行数**（含 `kind='list'` 的工具枚举行），不是派发次数；
        provider_calls / hook_calls / tool_calls 才是各自能力的真实派发次数，
        枚举行单独记在 list_rows。恒等式：calls = 三者之和 + list_rows。
        """
        labels = []
        details = []
        for item in self._profiles():
            shape = _capability(item)
            if shape is None:
                continue
            detail = {'extensionId': _extension_id(item), 'kind': shape[0],
                      'capability': shape[2] or shape[1], 'event': _profile_event(item),
                      # 三项曾经的"宿主不支持"已接入（见 HOST_CONSUMED_PROVIDERS 的注释），
                      # 控制台按这个字段报"宿主不支持的能力 N 项"，别再误报。
                      'deadInHost': (shape[0] == 'provider'
                                     and shape[1] in ALLOWED_PROVIDERS
                                     and shape[1] not in HOST_CONSUMED_PROVIDERS)}
            details.append(detail)
            labels.append('%s|%s|%s' % (detail['extensionId'], detail['kind'], detail['capability']))
        status = _with_row_counts(host.bridge_status(self._data_dir(), labels),
                                  self._data_dir())
        status['capabilities'] = details
        status['paths'] = _resolve_paths({key: self.get(key, '') for key in
                                          ('hostScript', 'sourceDir', 'extensionsDir')},
                                         self._data_dir())
        return status


def host_path_ok(path) -> bool:
    """host-one.mjs 必须是真实文件；相对路径不允许（防止被 cwd 影响）。"""
    if not path:
        return False
    item = Path(str(path))
    return item.is_absolute() and item.is_file()
