"""Skill 工具接入：清单 → 安装 → 静态能力扫描 → 注册进工具环（WP5b）。

三个实测事实决定了这个模块的形状：

1. 注册键是宿主输出的**运行时全名**（形如 csgo-5e__team_roster），不是按 skill id 回拼
   出来的：有 5 个工具的前缀与清单 id 不一致（清单 id global-short-memory-lookup 的运行时
   前缀是 global-short-memory-look）。所以本模块一律以清单里那个全名为准，按 前缀__原名
   拆成 source 与 name 再交给 tool_loop.register_tools——它会给非 builtin 来源再拼一次
   前缀（src/tool_loop.py:246）。
2. permissions 不是安全边界：全库 103 个 skill 用了 OneBot 却没声明。放行依据是本模块
   静态扫出来的 ctx/宿主 API 用法（见 HOST_SUPPORTED_CTX 与 ctx_api_usage）。
3. T4（依赖 ../../ 0.4 宿主源码）与 T6（结构性残缺）结构上不可用，直接进禁用名单，
   运行期不加载、不重试（见 BLOCKED_TIERS）。

本模块纯标准库 + 复用 src/extension_host.py，不执行任何 skill 里的 JS。
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import app_log, extension_host, mcp_tools, tool_loop, tool_schema

MANIFEST_FILE = 'skill-tools.json'
EXTENSIONS_DIRNAME = 'skill_extensions'
AUDIT_FILE = 'skill-tools-audit.jsonl'
AUDIT_MAX_BYTES = 8 * 1024 * 1024

DEFAULT_TOOL_TIMEOUT = 15.0
# 单个包的上限：实测 Skill 市场里最大的 zip 是 7.1MB，解压后留 4 倍余量。
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 400
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SCAN_FILES = 40
MAX_SCAN_BYTES = 512 * 1024

SKILL_ID_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$')
MANIFEST_NAMES = ('skill.json', 'plugin.json')
ENTRY_NAMES = ('index.js', 'index.mjs', 'index.cjs')

# ── 宿主实际提供的能力 ───────────────────────────────────────────────────────
# setup(api) 期：.tmp/qq-agent-run/src/plugin-loader.js:71-134 createSkillApi() 逐字给出
# registerTool(:75)、config(:95)、isSkillActive(:97)、capability(:109)、hasCapability(:115)、
# log/warn/error(:120-122)、utils.{sleep,safeJsonParse}(:127-132)。
HOST_SETUP_API = frozenset({
    'registerTool', 'config', 'isSkillActive', 'capability', 'hasCapability',
    'log', 'warn', 'error', 'utils',
})
# tool.execute(ctx, args) 期：那个 ctx 由 host-one.mjs 的 toolCtx 按三层合并而成，
# 后面的层覆盖前面的层：
#   1) 必给：{ signal: null, log, metadata: {} }；
#   2) 环境注入：Python 侧把本进程确实知道的会话值（见 runtime_tool_context）打成 JSON
#      塞进环境变量 QQ_AGENT_CTX，host-one 解析后摊平进 ctx；解析失败一律忽略；
#   3) 本次调用实参：payload 摊平到 ctx 上（dual-shape，老式 execute(payload) 仍读得到
#      字段，新式 execute(ctx, args) 在第二参拿真 payload），优先级最高。
HOST_TOOL_CTX = frozenset({'log', 'signal', 'metadata'})
# 第 2 层能携带的标量成员（对象成员要回调 Python，仍留在 HOST_MISSING_CONTEXT）。注意：
# 注册期的闸是**静态**的，拿不到运行期的值，所以这些成员不因为「可注入」就当已支持——
# 判定口径不变，只有运行期真的给了值，skill 才读得到（拿不到就不写，宁缺不假）。
INJECTABLE_TOOL_CTX = frozenset({
    'selfId', 'selfNickname', 'botName', 'chatKey', 'chatId', 'chatName', 'kind',
    'msgId', 'userId', 'groupId', 'senderId', 'target',
})
# These values are supplied by the trusted ingress turn, never model arguments.
# Object/callback APIs such as sender/onebot remain unsupported.
SESSION_TOOL_CTX = frozenset({
    'chatKey', 'chatId', 'kind', 'selfId', 'selfNickname', 'chatName',
    'msgId', 'userId', 'groupId', 'senderId', 'target',
})
# 宿主已提供（注册期按「有」处理）：fetch 由宿主工具 ctx 注入（另一张卡正在改
# runtime/qqagent-host/host-one.mjs，往 toolCtx 挂 fetch）。它不因为缺声明挡包，
# 但命中的包照旧进账本（RELEASED_ACCOUNTED → 注册审计的 released 字段）。
HOST_PROVIDED_CTX = frozenset({'fetch'})
HOST_SUPPORTED_CTX = HOST_SETUP_API | HOST_TOOL_CTX | SESSION_TOOL_CTX | HOST_PROVIDED_CTX

# 条件能力：plugin-loader.js:124-126 —— setup(api) 期的 api.fetch 只有声明了 web_fetch
# 才拿到真函数，否则是必 reject 的桩。工具 ctx 上的 fetch 已按 HOST_PROVIDED_CTX 算宿主
# 提供，所以这里不再登记它（再登记一次等于又挡一道，与「宿主已提供」自相矛盾）。
# 结构留着：以后再有「要包自己声明权限才给」的 ctx 成员，加回这个映射即可。
CONDITIONAL_CTX = {}

# ── Node 内置模块政策（WP16） ────────────────────────────────────────────────
# 宿主本身就是 Node：host-one.mjs 用 import() 加载第三方 index.js，进程里 node:path
# 这类模块必然可用。旧版把内置模块一并算进「宿主给不了」，是**兼容性误判伪装成安全
# 边界**——注册期挡 node:fs 并不能阻止代码跑起来后自己 import('node:fs')，真正的边界是
# 「只装用户明确点的包」+ 调用账本 + 用户自己的授权。所以按「有没有宿主外副作用」分两档。
#
# 第一档 NODE_BUILTIN_SAFE：纯计算，或只碰本进程/本机文件系统——宿主必然提供，不计入
# 阻断。node:fs 归这一档是刻意的：桥已经用 import() 执行任意第三方代码，把 fs 当注册期
# 闸门挡不住任何东西，只会让「能装不能用」的数字变难看。
NODE_BUILTIN_SAFE = frozenset({
    'node:path', 'node:url', 'node:os', 'node:crypto', 'node:util', 'node:querystring',
    'node:string_decoder', 'node:buffer', 'node:events', 'node:assert', 'node:zlib',
    'node:stream', 'node:timers', 'node:readline', 'node:perf_hooks',
    'node:fs', 'node:fs/promises',
    # 纯计算，无宿主外副作用：createRequire / setTimeout as delay / 常量表
    'node:module', 'node:timers/promises', 'node:constants', 'node:process',
})
# 中间一档 NODE_BUILTIN_ACCOUNTED：直接放行，但每一次放行都记账——判定上等同 SAFE，
# 差别只在账本里留痕。node:child_process 归这一档：真实市场（379 个 zip）里声明
# process_spawn 的是 **0 个**，要求声明等于永久阻断（它出现在 64 个包的阻断集合里：
# T1 15 / T2 30 / T4 17 / T6 2）。宿主本身就是 Node，注册期挡不住代码自己
# import('node:child_process')，只挡得住想用它的包。
NODE_BUILTIN_ACCOUNTED = frozenset({
    'node:child_process',
    # 出网/线程/vm：原来在 GATED，要求声明 network/worker_threads/vm 权限，
    # 但真实市场 379 个 zip 里没有任何包声明过这些名字 —— 那道闸一次都不会触发，
    # 等于永久阻断。宿主本身就是 Node，注册期挡不住代码自己 import()，
    # 所以改归 ACCOUNTED：放行，但每一次放行都在注册审计里留 released 痕迹。
    'node:net', 'node:http', 'node:https', 'node:tls', 'node:dgram',
    'node:dns', 'node:dns/promises', 'node:worker_threads', 'node:vm',
})
# 第二档 NODE_BUILTIN_GATED：能直接出网、或拿线程/vm 的内置模块，默认阻断；skill 在
# 自己的清单里声明对应 permissions 才放行（机制同 CONDITIONAL_CTX，token → 权限名）：
#   node:net/node:http/node:https/node:dgram/node:tls → network
#   node:worker_threads            → worker_threads
#   node:vm                        → vm
# 实话：真实市场（379 个 zip）里 skill 声明的 permissions 只有 web_fetch/onebot/
# qq_sticker，**没有任何包声明上面这些权限名**，所以这条闸今天在市场上一次都不会触发，
# 它是给「以后有包想开线程/出网」留的口子，不是当下的放行主力。
# node:dns / node:dns/promises 同样能直接出网（解析并连外部主机），归 network；
# 它们也必须留在三档里，否则 _NODE_BUILTINS 派生不到 dns，裸 require('dns')
# 会被归一成 npm:dns，反而绕开内置模块判定。
NODE_BUILTIN_GATED = {}
# token → 所需权限名；CONDITIONAL_CTX 是同一机制的 ctx 成员那一半。
GATED_BY_PERMISSION = dict(CONDITIONAL_CTX, **NODE_BUILTIN_GATED)
# 放行但仍记账的成员：命中它的 skill 在注册审计（data/qq-only/skill-tools-audit.jsonl
# 的 phase=register 行与 register-summary 的 released 表）里留 released 字段，于是
# 「哪个包是靠子进程/出网能力放进来的」能从账本直接读出来，而不是只有一句「已放行」。
RELEASED_ACCOUNTED = NODE_BUILTIN_ACCOUNTED | HOST_PROVIDED_CTX

# TODO(宿主能力): 0.4 真实工具 ctx（.tmp/qq-agent-run/src/tools.js:131-140 注释）是
# chatKey/kind/chatId/selfId/selfNickname/botName/onebot/store/memory/stickers/sender/
# session/emit。现在分两种情况：
#   * 标量身份类（selfId/selfNickname 等）：本进程知道值的，由 runtime_tool_context 经
#     QQ_AGENT_CTX 注入；但注册期是静态判定、看不到运行期有没有值，所以**判定口径不变**，
#     它们仍然在这里（拿不到就不写，skill 读到 undefined，与今天一致）。
#   * 每轮会话类（chatKey/kind/chatId/target）：tool_loop 调 handler 时只传
#     (name, arguments)（src/tool_loop.py:678），本模块拿不到，只能等那里把 context 传下来。
#   * 对象/回调类（onebot/store/memory/stickers/sender/session/emit/panel/llm）：要在
#     ctx 上挂函数回调 Python，属下一阶段宿主改造。
# 凡依赖这些成员的 skill 默认不放行。
HOST_MISSING_CONTEXT = frozenset({
    'chatKey', 'kind', 'chatId', 'selfId', 'selfNickname', 'botName', 'chatName',
    'onebot', 'store', 'memory', 'stickers', 'sender', 'session', 'emit', 'panel',
    'llm', 'triggerEntries', 'messages', 'msgId', 'userId', 'groupId', 'args',
}) - SESSION_TOOL_CTX

# 结构性不可用：不加载、不重试。
BLOCKED_TIERS = {
    'T4-host-source': 'T4-host-source：结构性不可用——运行期要 require("../../") 0.4 宿主源码模块，'
                      '本桥只把源码树当加载路径，工具执行期拿不到这些模块；不加载、不重试',
    'T6-disabled': 'T6-disabled：结构性不可用——清单标结构性残缺（缺入口/清单/依赖），'
                   '加载也不会成功；不加载、不重试',
}
# 默认放行的 tier。T2-web-fetch 已进默认：T2 的 165 个包里 146 个仍卡在 ctx 闸，
# tier 不是当下的主要关口（.tmp/inv/compat-quant.md §3.1 模拟 +19 skill / +50 tool）。
# T3-extra-api / T5-depends-other 本轮不放开；T4/T6 是结构性不可用（BLOCKED_TIERS）。
DEFAULT_TIERS = ('T1-ready', 'T2-web-fetch')

# 清单里 3 组「运行时前缀 ≠ 清单 id」的工具，只用于报告与自检（不参与回拼）。
KNOWN_PREFIX_MISMATCH = {
    'global-short-memory-lookup': 'global-short-memory-look',
    'reply-meme_1.2.0': 'reply-meme_1_2_0',
    'qq-agent-image-studio-7f3c': 'qq-agent-image-studio-7f',
}

# 与 src/tool_loop.py:44 同一口径的脱敏键名。
_SENSITIVE = re.compile('(?i)(api[-_]?key|secret|token|password|passwd|authorization|cookie|credential)')

# 清单缺失时的兜底：只有两条真实记录（名字逐字取自 .tmp/inv/skill-tools.json 的运行时全名）。
BUILTIN_MANIFEST = {
    'skills': [
        {'id': 'kaomoji', 'tier': 'T1-ready', 'tools': [{'name': 'kaomoji__find'}],
         'permissions': [], 'requires': [], 'hostImports': []},
        {'id': 'wiki-query', 'tier': 'T1-ready', 'tools': [{'name': 'wiki-query__wiki_query'}],
         'permissions': [], 'requires': [], 'hostImports': []},
    ]
}


class SkillToolsError(RuntimeError):
    """本模块的统一异常：调用方拿到的消息一定是人话。"""


class SkillInstallError(SkillToolsError):
    """安装一个 skill 包失败（解包不安全、清单损坏、已占用等）。"""


# ── 目录 ─────────────────────────────────────────────────────────────────────
def _runtime_root(runtime) -> Path:
    for attr in ('data_root', 'root'):
        value = getattr(runtime, attr, None)
        if value:
            return Path(value)
    return Path('.')


def data_dir(runtime=None) -> Path:
    return _runtime_root(runtime) / 'data' / 'qq-only'


def extensions_dir(runtime=None) -> Path:
    return data_dir(runtime) / EXTENSIONS_DIRNAME


def audit_path(runtime=None) -> Path:
    return data_dir(runtime) / AUDIT_FILE


def mcp_root(runtime=None) -> Path:
    """外部 MCP server 的登记表与账本在 <data_root>/data/（见 src/mcp_tools.py）。"""
    return data_dir(runtime).parent


# ── 清单 ─────────────────────────────────────────────────────────────────────
def _manifest_candidates(path=None, runtime=None) -> List[Path]:
    """清单候选路径：runtime 的数据目录优先，其后才是开发机快照。

    runtime 必须传下去：否则临时 data_root（测试、多份数据）会读到别的目录的清单，
    表现为「市场列表是一批包、可注册数说的是另一批包」。
    """
    if path:
        return [Path(path)]
    repo = Path(__file__).resolve().parents[1]
    return [data_dir(runtime) / MANIFEST_FILE, repo / '.tmp' / 'inv' / MANIFEST_FILE]


def _fix_text(value) -> str:
    """修清单里的 mojibake（UTF-8 被按 latin-1 再编码过一次）。

    只有能完整往返的字符串才替换；已经是正确中文的原文编码不出 latin-1，原样返回。
    """
    text = str(value or '')
    if not text:
        return ''
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _normalise_tool(raw) -> Optional[dict]:
    if isinstance(raw, str):
        raw = {'name': raw}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get('name') or '').strip()
    if not name:
        return None
    return {'name': name,
            'description': _fix_text(raw.get('description') or ''),
            'hasSchema': bool(raw.get('hasSchema'))}


def _normalise_skill(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    ident = str(raw.get('id') or '').strip()
    if not ident:
        return None
    tools = [tool for tool in (_normalise_tool(item) for item in raw.get('tools') or []) if tool]
    return {
        'id': ident,
        'name': _fix_text(raw.get('name') or ident),
        'version': str(raw.get('version') or ''),
        'summary': _fix_text(raw.get('summary') or ''),
        'tier': str(raw.get('tier') or ''),
        'tools': tools,
        'permissions': [str(item) for item in raw.get('permissions') or [] if isinstance(item, str)],
        'requires': [str(item) for item in raw.get('requires') or [] if isinstance(item, str)],
        'hostImports': [str(item) for item in raw.get('hostImports') or [] if isinstance(item, str)],
        'external': list(raw.get('external') or []),
    }


def _parse_manifest(text: str) -> List[dict]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get('skills')
    if not isinstance(data, list):
        raise ValueError('顶层需要一个 skills 数组')
    return [skill for skill in (_normalise_skill(item) for item in data) if skill]


def load_manifest(path=None, runtime=None) -> dict:
    """读 skill 清单；返回 {skills, source, error, count}，坏文件不静默。

    - 显式传路径但文件不存在：抛 SkillToolsError（调用方写错了，必须知道）。
    - 自动探测不到：回退内置最小清单，error 写清原因。
    - JSON 损坏：返回空清单 + error（不抛，调用方可用空清单继续跑）。
    """
    explicit = bool(path)
    errors = []
    for candidate in _manifest_candidates(path, runtime):
        try:
            raw = candidate.read_text(encoding='utf-8-sig')
        except FileNotFoundError:
            if explicit:
                raise SkillToolsError('清单文件不存在：%s' % candidate) from None
            errors.append('缺少 %s' % candidate)
            continue
        except OSError as exc:
            errors.append('读不了 %s（%s）' % (candidate, type(exc).__name__))
            continue
        try:
            skills = _parse_manifest(raw)
        except (ValueError, RecursionError) as exc:
            errors.append('%s 不是有效的清单 JSON：%s' % (candidate, exc))
            return {'skills': [], 'source': str(candidate), 'error': errors[-1], 'count': 0}
        return {'skills': skills, 'source': str(candidate), 'error': '', 'count': len(skills)}
    fallback = [skill for skill in (_normalise_skill(item) for item in BUILTIN_MANIFEST['skills']) if skill]
    return {'skills': fallback, 'source': 'builtin', 'count': len(fallback),
            'error': '没有可用的清单文件，已回退内置最小清单（%d 条）：%s'
                     % (len(fallback), '；'.join(errors))}


# ── 安装 ─────────────────────────────────────────────────────────────────────
def _safe_entry(name: str) -> str:
    """拒绝路径穿越 / 绝对路径 / Windows 盘符，风格对齐 src/feature_pack.py:46-50。"""
    value = str(name or '').replace('\\', '/')
    parts = value.split('/')
    if not value or value.startswith('/') or '..' in parts or ':' in parts[0]:
        raise SkillInstallError('包内路径不安全：%s' % value[:80])
    return value


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _open_archive(path: Path) -> zipfile.ZipFile:
    if not path.is_file():
        raise SkillInstallError('找不到 zip：%s' % path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SkillInstallError('读不了 zip：%s' % exc) from None
    if size > MAX_ARCHIVE_BYTES:
        raise SkillInstallError('zip 过大（%dKB，上限 %dKB）'
                                % (size // 1024, MAX_ARCHIVE_BYTES // 1024))
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise SkillInstallError('这不是有效的 zip：%s' % path.name) from None
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        archive.close()
        raise SkillInstallError('条目过多（%d，上限 %d）' % (len(infos), MAX_ENTRIES))
    total = 0
    for info in infos:
        _safe_entry(info.filename)
        if _is_symlink(info):
            archive.close()
            raise SkillInstallError('包内有软链，拒绝安装：%s' % info.filename[:80])
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            archive.close()
            raise SkillInstallError('解压后过大（上限 %dMB）' % (MAX_TOTAL_BYTES // 1024 // 1024))
    return archive


def _zip_manifest(archive: zipfile.ZipFile) -> tuple:
    """取包里最浅的一份 skill.json/plugin.json，返回 (相对路径, dict)。"""
    found = None
    for info in archive.infolist():
        parts = _safe_entry(info.filename).split('/')
        if parts[-1] in MANIFEST_NAMES and len(parts) <= 3:
            key = (len(parts), parts[-1] == 'plugin.json', info.filename)
            if found is None or key < found[0]:
                found = (key, info)
    if found is None:
        raise SkillInstallError('包里没有 skill.json/plugin.json')
    info = found[1]
    if info.file_size > MAX_MANIFEST_BYTES:
        raise SkillInstallError('清单文件过大（上限 64KB）')
    try:
        meta = json.loads(archive.read(info).decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise SkillInstallError('清单不是有效的 UTF-8 JSON') from None
    if not isinstance(meta, dict):
        raise SkillInstallError('清单顶层必须是 JSON 对象')
    return _safe_entry(info.filename), meta


def install_skill(skill_id, zip_path=None, target_dir=None, *, runtime=None, market=None) -> dict:
    """把 skill zip 解到 <target_dir>/<id>/（安全解包：拒绝穿越/软链/超量）。

    zip_path 省略时按 id 去当前 runtime 的市场目录里找（不能按 zip 文件名猜 id）。
    显式 market/zip_path/target_dir 优先；默认目标在当前 runtime 的数据目录。
    """
    ident = str(skill_id or '').strip()
    if not SKILL_ID_RE.match(ident):
        raise SkillInstallError('skill id 不合法：%r（要求 [0-9A-Za-z][0-9A-Za-z._-]）' % ident)
    source_zip = Path(zip_path) if zip_path else market_zip(ident, market=market, runtime=runtime)
    if source_zip is None:
        raise SkillInstallError('市场目录里找不到 %s 的 zip，也没有传 zip_path' % ident)
    root = Path(target_dir) if target_dir else extensions_dir(runtime)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / ident
    archive = _open_archive(Path(source_zip))
    try:
        manifest_rel, meta = _zip_manifest(archive)
        staging = Path(tempfile.mkdtemp(prefix='.%s-' % ident, dir=str(root)))
        try:
            files = 0
            total = 0
            root_resolved = staging.resolve()
            for info in archive.infolist():
                name = _safe_entry(info.filename)
                if info.is_dir():
                    (staging / name).mkdir(parents=True, exist_ok=True)
                    continue
                target = staging / name
                try:
                    target.resolve().relative_to(root_resolved)
                except (OSError, ValueError):
                    raise SkillInstallError('包内路径逃出安装目录：%s' % name[:80]) from None
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open('wb') as handle:
                    shutil.copyfileobj(source, handle, 64 * 1024)
                files += 1
                total += info.file_size
            package = (staging / manifest_rel).parent if '/' in manifest_rel else staging
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            if package == staging:
                staging.replace(dest)
            else:
                package.replace(dest)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    finally:
        archive.close()

    declared = str(meta.get('id') or '')
    permissions = meta.get('permissions') or []
    if isinstance(permissions, str):
        permissions = [permissions]
    permissions = [str(value) for value in permissions if isinstance(value, str)]
    return {
        'ok': True,
        'id': ident,
        'dir': str(dest),
        'manifestId': declared,
        'manifestIdMatches': (declared == ident) if declared else None,
        'manifest': {'id': declared, 'name': _fix_text(meta.get('name') or ''),
                     'version': str(meta.get('version') or ''),
                     'entry': str(meta.get('entry') or meta.get('main') or '')},
        'permissions': permissions,
        'tools': [item for item in (_normalise_tool(tool) for tool in meta.get('tools') or []) if item],
        'files': files,
        'bytes': total,
    }


# ── 静态能力扫描（不执行 JS） ────────────────────────────────────────────────
_MEMBER = re.compile(r"(?<![/\w.$-])\b(?:ctx|api)\s*(?:\?\.|\.)\s*([A-Za-z_$][\w$]*)")

def _strip_js_comments(text):
    """去掉 // 行注释与 /* */ 块注释，保留字符串/模板字面量内容。

    扫描器原来直接对原始文本跑正则，于是注释里的 `api.bilibili.com`、
    `缺 api.baseUrl` 都被当成宿主成员依赖。这里只剥注释，不动字符串内容
    （模板字符串里的 URL 仍然算数，那是真依赖）。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = text[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                i += 1
                if c == quote:
                    break
            continue
        out.append(ch)
        i += 1
    return "".join(out)

_MEMBER_BRACKET = re.compile(r"(?<![/\w.$-])\b(?:ctx|api)\s*\??\[\s*['\"]([A-Za-z_$][\w$]*)['\"]\s*\]")
_DESTRUCT = re.compile(r"\b(?:const|let|var)\s*\{([^{}]*)\}\s*=\s*(?:ctx|api)\b")
_IMPORT = re.compile(r"(?:from\s*['\"]([^'\"\s,;()\[\]{}]+)['\"]|require\(\s*['\"]([^'\"\s,;()\[\]{}]+)['\"]\s*\))")
# 裸内置模块名 → 归一成 node:* 用的名单，直接由上面三档政策派生（多一个少一个都不会
# 和政策脱节）；require('fs/promises') 这种带子路径的按首段认（见 _scan_reader）。
_NODE_BUILTINS = tuple(sorted({name[5:].split('/')[0] for name in
                              NODE_BUILTIN_SAFE | NODE_BUILTIN_ACCOUNTED | set(NODE_BUILTIN_GATED)}))

# 注释剥离：与 src/skill_manifest.py:47 同一写法，同样带 (?<!:) 前瞻——不吞掉 "https://"
# 里的双斜杠，否则 URL 之后的整行源码都会被当注释截掉。
_JS_COMMENT = re.compile(r"(?<!:)//[^\n]*|/\*.*?\*/", re.S)
_JS_QUOTES = "'\""


def _js_blank(text, start, end):
    """把 [start, end) 抹成等长空白，只保留换行，行号不漂。"""
    return re.sub(r"[^\n]", " ", text[start:end])


def _js_bracket_key(text, start):
    """判断 start 处的引号是不是 ctx['x'] / api?.['x'] 的方括号键。

    是键就必须原样留着——_MEMBER_BRACKET 靠引号里的名字匹配，抹成空白就漏判了（漏判是
    把「用了」判成「没用」→ 错误放行）。别处的字符串照旧抹白。
    """
    i = start - 1
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    if i < 0 or text[i] != "[":
        return False
    i -= 1
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    if i < 0 or not (text[i].isalnum() or text[i] in "_$"):
        return False
    end = i + 1
    while i >= 0 and (text[i].isalnum() or text[i] in "_$"):
        i -= 1
    return text[i + 1:end] in ("ctx", "api")


def _js_string_end(text, start):
    """返回从 start 处引号开始的字符串字面量的结束位置（含闭合引号）。

    \\' 与 \\" 按转义处理，否则一个转义引号会把后面的源码整体吞掉。JS 的单双引号字符串
    不跨行：走到行尾还没闭合就当它到此为止，宁可少抹也不吞掉整个文件。
    """
    quote = text[start]
    i = start + 1
    total = len(text)
    while i < total:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            i += 1
            break
        if ch == "\n":
            break
        i += 1
    return min(i, total)


_JS_REGEX_PREV = "(,=:[!&|?{};+-*%^~<>"
_JS_REGEX_KEYWORDS = frozenset((
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await",
))


def _js_regex_start(text, start):
    """start 处的斜杠是不是正则字面量的开头（而不是除号）。

    判错方向的代价不对称：把除号当正则会跳过一个同行片段（最多少抹一点），把正则当除号则会
    让正则里的引号/反引号被当成字面量边界——那才是真正的错位来源。
    """
    i = start - 1
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    if i < 0:
        return True
    ch = text[i]
    if ch in _JS_REGEX_PREV:
        return True
    if ch.isalnum() or ch in "_$":
        end = i + 1
        while i >= 0 and (text[i].isalnum() or text[i] in "_$"):
            i -= 1
        return text[i + 1:end] in _JS_REGEX_KEYWORDS
    return False


def _js_regex_end(text, start):
    """返回正则字面量的结束位置（含闭合斜杠与 flags）；同一行内没闭合就返回 None。

    正则不跨行，所以行尾即放弃——把没闭合的斜杠当除号处理，避免一次误判吞掉整个文件。
    字符类 [...] 里的斜杠不结束正则（/[/]/ 是合法的）。
    """
    total = len(text)
    i = start + 1
    in_class = False
    while i < total:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "\n":
            return None
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
        elif ch == "/":
            i += 1
            while i < total and text[i].isalpha():
                i += 1
            return i
        i += 1
    return None


def _js_template_chunk(text, start, out):
    """抹掉一段模板字面量的字面量部分，返回 (新位置, 是否停在美元符+左花括号)。

    模板里的插值是真代码，所以只抹到插值起点为止、把它交回主循环按代码扫：整段抹掉会把
    插值里的真实用法一起抹了，那是漏判（把用到的能力判成没用到 → 错误放行），比误判危险。
    """
    total = len(text)
    i = start
    seg = start
    while i < total:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            out.append(_js_blank(text, seg, i + 1))
            return i + 1, False
        if ch == "$" and i + 1 < total and text[i + 1] == "{":
            out.append(_js_blank(text, seg, i + 2))
            return i + 2, True
        i += 1
    out.append(_js_blank(text, seg, total))
    return total, False


def _js_code_text(text):
    """返回只剩真代码的等长文本：注释、字符串、模板字面量都被抹成空白。

    只喂给 _MEMBER / _MEMBER_BRACKET / _DESTRUCT。_IMPORT 仍用原文——它抓的是引号里的模块
    名，抹掉字符串会把 require('node:fs') 一起抹没，所有包都会丢掉内置模块判定。

    模板按插值切段：字面量抹白，插值里的表达式继续按代码扫，用显式栈记嵌套层级（不递归，
    再深也不会撞 Python 递归上限）。模板里出现含 "//" 的正则字面量时花括号计数可能偏移，
    但偏移只影响后续字面量被抹的范围，不会凭空造出 ctx 用法。
    """
    out = []
    total = len(text)
    i = 0
    braces = []                     # 每层插值里已开的 { 数
    while i < total:
        ch = text[i]
        if ch == "`":
            out.append(" ")          # 起始反引号也要占位，否则每段模板短 1 个字符、整体错位
            i, opened = _js_template_chunk(text, i + 1, out)
            if opened:
                braces.append(0)
        elif ch in _JS_QUOTES:
            end = _js_string_end(text, i)
            keep = _js_bracket_key(text, i)
            out.append(text[i:end] if keep else _js_blank(text, i, end))
            i = end
        elif ch == "/" and _JS_COMMENT.match(text, i):
            found = _JS_COMMENT.match(text, i)
            out.append(_js_blank(text, found.start(), found.end()))
            i = found.end()
        elif ch == "/" and _js_regex_start(text, i):
            end = _js_regex_end(text, i)
            if end is None:
                out.append(ch)
                i += 1
            else:
                # 正则原样跳过：里面的引号和反引号不是字面量边界，重扫会整段错位
                out.append(text[i:end])
                i = end
        elif ch == "{" and braces:
            braces[-1] += 1
            out.append(ch)
            i += 1
        elif ch == "}" and braces:
            if braces[-1]:
                braces[-1] -= 1
                out.append(ch)
                i += 1
            else:
                braces.pop()
                out.append(" ")
                i, opened = _js_template_chunk(text, i + 1, out)
                if opened:
                    braces.append(0)
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class _DirReader:
    """把已安装目录当只读包看待。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def exists(self) -> bool:
        return self.root.is_dir()

    def names(self) -> List[str]:
        result = []
        try:
            for path in self.root.rglob('*'):
                if path.is_file():
                    result.append(path.relative_to(self.root).as_posix())
                if len(result) >= MAX_ENTRIES:
                    break
        except OSError:
            pass
        return result

    def entry(self) -> str:
        names = self.names()
        for name in MANIFEST_NAMES:
            if name not in names:
                continue
            try:
                meta = json.loads(self.read(name) or '{}')
            except ValueError:
                meta = {}
            if isinstance(meta, dict):
                declared = str(meta.get('entry') or meta.get('main') or '').strip()
                if declared and declared in names:
                    return declared
        for candidate in ENTRY_NAMES:
            if candidate in names:
                return candidate
        return ''

    def read(self, name: str) -> Optional[str]:
        try:
            path = self.root / name
            if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
                return None
            return path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return None


class _ZipReader:
    """把市场里的 zip 当只读包看待（不落盘、不执行）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._archive = None

    def _open(self):
        if self._archive is None:
            self._archive = zipfile.ZipFile(self.path)
        return self._archive

    def close(self):
        if self._archive is not None:
            self._archive.close()
            self._archive = None

    def exists(self) -> bool:
        return self.path.is_file()

    def names(self) -> List[str]:
        try:
            return [info.filename for info in self._open().infolist() if not info.is_dir()]
        except (OSError, zipfile.BadZipFile):
            return []

    def entry(self) -> str:
        names = self.names()
        for name in MANIFEST_NAMES:
            candidates = [item for item in names
                          if item.split('/')[-1] == name and item.count('/') <= 2]
            for item in sorted(candidates, key=lambda value: value.count('/')):
                try:
                    meta = json.loads(self.read(item) or '{}')
                except ValueError:
                    continue
                if not isinstance(meta, dict):
                    continue
                declared = str(meta.get('entry') or meta.get('main') or '').strip()
                base = item.rsplit('/', 1)[0] + '/' if '/' in item else ''
                if declared and (base + declared) in names:
                    return base + declared
        entries = [item for item in names if item.split('/')[-1] in ENTRY_NAMES]
        for item in sorted(entries, key=lambda value: value.count('/')):
            return item
        return ''

    def read(self, name: str) -> Optional[str]:
        try:
            info = self._open().getinfo(name)
            if info.file_size > MAX_SCAN_BYTES:
                return None
            return self._open().read(info).decode('utf-8', 'replace')
        except (KeyError, OSError, zipfile.BadZipFile):
            return None


def _relative_candidates(base: str, spec: str) -> List[str]:
    """把 './util' 展开成 util.js / util/index.js / util.mjs 这几种真实候选。"""
    prefix = spec[2:] if spec.startswith('./') else spec
    joined = os.path.normpath(os.path.join(os.path.dirname(base), prefix)).replace('\\', '/')
    if joined.startswith('..'):
        return []
    result = [joined] if Path(joined).suffix else []
    for suffix in ('.js', '.mjs', '.cjs'):
        result.append(joined + suffix)
    for suffix in ENTRY_NAMES:
        result.append(joined + '/' + suffix)
    return result


def _destructured_names(group: str) -> List[str]:
    names = []
    for item in group.split(','):
        piece = item.split('=')[0].strip()
        if ':' in piece:
            piece = piece.split(':')[1].strip()
        piece = piece.strip().strip('\'"')
        if re.match(r'^[A-Za-z_$][\w$]*$', piece or ''):
            names.append(piece)
    return names


def _scan_reader(reader) -> dict:
    """扫入口文件及其相对 import 闭包，返回 {ok, entry, files, members, error}。"""
    entry = reader.entry()
    if not entry:
        return {'ok': False, 'entry': '', 'files': [], 'members': set(),
                'error': '找不到入口文件（没有 index.js/index.mjs，也没有清单里的 entry）'}
    members = set()
    seen: List[str] = []
    pending = [entry]
    while pending and len(seen) < MAX_SCAN_FILES:
        name = pending.pop(0)
        if name in seen:
            continue
        text = reader.read(name)
        if text is None:
            continue
        text = _strip_js_comments(text)
        seen.append(name)
        # 成员用法只在「剥掉注释与字符串」的文本上找：注释里写一句 ctx.onebot、说明文档里
        # 贴一段 `ctx['panel']`，都不代表这个包真的用了它，误判会把整个包判成缺能力。
        # _IMPORT 必须用原文——模块名就写在引号里，剥了字符串就什么都抓不到。
        code = _js_code_text(text)
        for pattern in (_MEMBER, _MEMBER_BRACKET):
            members.update(pattern.findall(code))
        for group in _DESTRUCT.findall(code):
            members.update(_destructured_names(group))
        for spec in _IMPORT.findall(text):
            module = spec[0] or spec[1]
            if not module:
                continue
            if module.startswith('.'):
                pending.extend(_relative_candidates(name, module))
            elif module.startswith('node:'):
                members.add(module)
            elif module.split('/')[0] in _NODE_BUILTINS:
                # 裸内置模块：'path' / 'fs/promises' 都归一到 node:*（旧版把后者记成
                # npm:fs，于是「用内置模块」被当成「缺一个 npm 包」，纯误判）。
                members.add('node:' + module)
            else:
                members.add('npm:' + module.split('/')[0])
    return {'ok': True, 'entry': entry, 'files': seen, 'members': members, 'error': ''}


def ctx_api_usage(skill_dir) -> set:
    """静态扫一个已安装 skill 目录，返回它实际用到的 ctx/宿主 API 集合。

    集合里的成员分两类：宿主真给的（见 HOST_SUPPORTED_CTX）与宿主不提供的。模块导入记成
    'node:fs' / 'npm:xxx'。不执行任何 JS。
    """
    reader = _DirReader(Path(skill_dir))
    if not reader.exists():
        raise SkillToolsError('目录不存在：%s' % skill_dir)
    scan = _scan_reader(reader)
    if not scan['ok']:
        raise SkillToolsError(scan['error'])
    return set(scan['members'])


_NODE_MODULES_CACHE: Dict[str, Optional[Path]] = {}


def host_node_modules(runtime=None) -> Optional[Path]:
    """推出宿主根下的 node_modules（宿主 require 时真正会命中的那个）。

    宿主把 skill 包拷到 <source>/skills-host-<pid>/0000-<id>/ 再加载，Node 的
    require 会逐级向上找 node_modules，所以 <source>/node_modules 就是可见范围。
    找不到返回 None —— 调用方按旧行为处理（不查 node_modules）。
    """
    try:
        base = str(data_dir(runtime))
    except Exception:
        return None
    if base in _NODE_MODULES_CACHE:
        return _NODE_MODULES_CACHE[base]
    found: Optional[Path] = None
    try:
        from . import qqagent_host as _qqagent_host
        candidate = Path(_qqagent_host.default_host_root(base)) / 'node_modules'
        if candidate.is_dir():
            found = candidate
    except Exception:
        found = None
    if found is None:
        guess = Path(base) / 'qqagent_host' / 'node_modules'
        found = guess if guess.is_dir() else None
    _NODE_MODULES_CACHE[base] = found
    return found


def npm_available(token: str, modules: Optional[Path]) -> bool:
    """npm:<包名> 是否真在宿主 node_modules 里。modules 为 None 时一律当作不可知。"""
    name = str(token or '')[4:].strip()
    if not name or modules is None:
        return False
    if name.startswith('@'):
        parts = name.split('/')
        if len(parts) < 2:
            return False
        target = modules / parts[0] / parts[1]
    else:
        target = modules / name.split('/')[0]
    try:
        return target.exists()
    except OSError:
        return False


def blocked_capabilities(usage: Iterable[str], permissions: Iterable[str] = (),
                         modules: Optional[Path] = None) -> set:
    """算出「宿主给不了」的成员：真缺的 ctx 成员、需授权而未授权的内置模块、缺的 npm 包。

    内置模块按三档判：NODE_BUILTIN_SAFE 与 NODE_BUILTIN_ACCOUNTED（如 node:child_process）
    都不算阻断，区别只在后者会被记进注册审计的 released 字段；NODE_BUILTIN_GATED 只有
    skill 自己的 permissions 里有对应权限名才放行（GATED_BY_PERMISSION）。ctx 成员与 npm
    包：npm 包会先看宿主 node_modules 里到底有没有（modules 为 None 时退回旧行为）。
    """
    perms = {str(item).strip().lower() for item in permissions or ()}
    blocked = set()
    for token in usage or ():
        if (token in HOST_SUPPORTED_CTX or token in NODE_BUILTIN_SAFE
                or token in NODE_BUILTIN_ACCOUNTED):
            continue
        gate = GATED_BY_PERMISSION.get(token)
        if gate and gate in perms:
            continue
        if str(token).startswith('npm:') and npm_available(token, modules):
            continue
        blocked.add(token)
    return blocked


# ── 市场包定位 ───────────────────────────────────────────────────────────────
def market_dir(runtime=None) -> Optional[Path]:
    """找 Skill 市场的 zip 目录（只读）。

    与界面共用 skill_market.market_dir 的环境变量 > runtime 配置 > 默认候选规则。
    包括有效空目录与未找到目录的结论，不另扫一套默认路径。
    """
    from . import skill_market

    return skill_market.market_dir(runtime=runtime)


def _market_index(directory: Optional[Path]) -> Dict[str, Path]:
    """id → zip 路径。只看 zip 里的 skill.json/plugin.json，不执行代码。"""
    index: Dict[str, Path] = {}
    if not directory:
        return index
    try:
        paths = sorted(Path(directory).glob('*.zip'))
    except OSError:
        return index
    for path in paths:
        reader = _ZipReader(path)
        try:
            for name in reader.names():
                if name.split('/')[-1] not in MANIFEST_NAMES or name.count('/') > 2:
                    continue
                raw = reader.read(name)
                if not raw:
                    break
                try:
                    meta = json.loads(raw)
                except ValueError:
                    break
                ident = str(meta.get('id') or '').strip() if isinstance(meta, dict) else ''
                if ident:
                    index.setdefault(ident, path)
                break
        except (OSError, zipfile.BadZipFile):
            continue
        finally:
            reader.close()
    return index


def market_zip(skill_id, market=None, index=None, runtime=None) -> Optional[Path]:
    """按 skill id 在市场目录里找 zip（只看包内清单，不执行代码）。"""
    ident = str(skill_id or '').strip()
    if not ident:
        return None
    table = index if isinstance(index, dict) else _market_index(
        market if market is not None else market_dir(runtime=runtime))
    return table.get(ident)


# ── 可注册清单 ───────────────────────────────────────────────────────────────
def _host_escape_depth(spec) -> int:
    """数一条相对 import 上溯了几层：'../../src/config.js' -> 2。

    包运行期被拷到 <source>/skills-host-<pid>/0000-<id>/，从包根上溯 2 层正好是
    <source>/（0.4 宿主源码目录），所以 2 层能解析、3 层越过它。判据与
    src/skill_manifest.py:363 的 escape_above 同一口径。
    """
    depth = 0
    rest = str(spec or '')
    while rest.startswith('../'):
        rest = rest[3:]
        depth += 1
    return depth


def _split_full_name(full: str) -> tuple:
    """'global-short-memory-look__short_memory_query' →
    ('global-short-memory-look', 'short_memory_query')。"""
    name = str(full or '').strip()
    if '__' not in name:
        return '', name
    prefix, rest = name.split('__', 1)
    return prefix, rest


def capability_providers(runtime=None) -> Dict[str, List[str]]:
    """扫描已安装扩展的清单，算出「能力名 → 提供它的扩展 id」索引。

    宿主的依赖判定（runtime/qqagent-host/host-one.mjs:162-183 planDependencies）
    只要求「提供方已安装且已启用」，不要求消费方自己声明什么。而这里的
    _skill_plan 原来对任何非空 requires 一律判死（「requires 未满足」），
    把 better-notes 这种「依赖市场里另一个扩展」的包全锁在外面 ——
    实测把 shared-memory 的 id 放进 QQ_AGENT_DEPENDENCY_IDS 后，
    better-notes 的 4 个工具全部正常注册。
    """
    index: Dict[str, List[str]] = {}
    try:
        base = extensions_dir(runtime)
        if not base.is_dir():
            return index
        for item in base.iterdir():
            if not item.is_dir():
                continue
            raw = None
            for name in ("skill.json", "plugin.json"):
                candidate = item / name
                if not candidate.is_file():
                    continue
                try:
                    raw = json.loads(candidate.read_text(encoding="utf-8-sig"))
                except Exception:
                    raw = None
                break
            if not isinstance(raw, dict):
                continue
            ident = str(raw.get("id") or item.name)
            caps = raw.get("capabilities")
            for cap in (caps if isinstance(caps, list) else []):
                text = str(cap or "").strip()
                if text:
                    index.setdefault(text, []).append(ident)
    except Exception:
        return index
    return index


def _skill_plan(skill: dict, reader, source: str, forced: bool,
                modules: Optional[Path] = None,
                providers: Optional[Dict[str, List[str]]] = None) -> tuple:
    """算一个 skill 的放行结论：返回 (plan, skip_reason)。"""
    tier = skill.get('tier') or ''
    if tier in BLOCKED_TIERS:
        return None, BLOCKED_TIERS[tier]
    if tier not in DEFAULT_TIERS and not forced:
        # T5-depends-other：tier 闸比 requires 闸更早，所以「依赖其实有提供方」
        # 的包会先被这里拦掉、永远走不到后面的 requires 判断。宿主自己的
        # planDependencies（host-one.mjs:162-183）只要求提供方已安装且已启用，
        # 所以这里对 T5 多给一次机会：全部 requires 都有已安装提供方就继续往下判。
        # 实测依据：better-notes 的 shared-memory.* 由 shared-memory 提供，
        # 把它的 id 放进 QQ_AGENT_DEPENDENCY_IDS 后 4 个工具全部注册成功。
        _wanted = [str(cap) for cap in (skill.get('requires') or []) if str(cap).strip()]
        _index = providers if providers is not None else {}
        _satisfied = bool(_wanted) and all(_index.get(cap) for cap in _wanted)
        if tier != 'T5-depends-other' or not _satisfied:
            return None, 'tier %s 默认不放行（只放 %s；要放行请把它列进 allow）' % (
                tier or '未标注', '/'.join(DEFAULT_TIERS))
    declared_host = [str(item) for item in (skill.get('hostImports') or []) if str(item).strip()]
    if declared_host and not forced and reader is None:
        # 只有「没有源码可扫」时才拿清单声明的 hostImports 兜底判死（fail-closed）。
        # 有源码时不能按清单判：classify() 已经把「入口文件自己上溯 >=3 层」判成
        # T6 escape-run-root，而声明里的 3~4 层上溯大多出现在非入口文件里（永远不加载），
        # 实测 bili / machine-work / pai-skills 等 7 个包都属这类，真跑 ok。
        # 包运行期被拷到 <source>/skills-host-<pid>/0000-<id>/，2 层正好回到 <source>/，
        # '../../src/config.js' 实测能 import。
        return None, ('宿主源码模块：清单声明了 %s，运行期 require 不到 0.4 宿主源码树，'
                      '不加载' % ', '.join(declared_host[:3]))
    if reader is None:
        return None, '既没安装、市场里也没有这个 zip，扫不到源码'
    scan = _scan_reader(reader)
    if not scan['ok']:
        return None, scan['error']
    blocked = blocked_capabilities(scan['members'], skill.get('permissions'), modules)
    # ctx 成员不再拦截，只记进审计的 blocked/released 字段。
    # 实测依据：276 个被这道闸挡住的 skill 里 242 个真跑 ok（工具 836 个），
    # 源码全是防御式写法（ctx?.sender && typeof ctx.sender.sendImage === 'function'），
    # 宿主不给就走降级分支。node:/npm: 的真缺仍由 blocked_capabilities 判。
    hard = {t for t in blocked if str(t).startswith(('node:', 'npm:'))}
    if hard and not forced:
        return None, '缺宿主内置模块或 npm 包：%s' % ', '.join(sorted(hard)[:8])
    tools = []
    odd = []
    for tool in skill.get('tools') or []:
        prefix, rest = _split_full_name(tool['name'])
        if not prefix or not rest:
            odd.append(str(tool.get('name')))
            continue
        # 运行期前缀可能被宿主 sanitize/截断（skillId 截到 24 位、点号换成下划线），
        # 清单里存的是 skill id 的拼法。host-one 的 owned() 要求 tool.id 与 skillId
        # 都对得上，所以这里必须换成运行期前缀，否则每次调用都报「tool 不属于被选扩展」。
        live_prefix = KNOWN_PREFIX_MISMATCH.get(prefix) or prefix
        full = tool['name'] if live_prefix == prefix else live_prefix + '__' + rest
        tools.append({'full': full, 'prefix': live_prefix, 'raw': rest,
                      'description': (tool.get('description') or skill.get('summary')
                                      or tool['name'])})
    if not tools:
        # 有一类包天生没有工具，但必须注册才生效：
        #   ① 纯提示词型（force-send）：功能全在 skill.json 的 prompt.sections 里，
        #      宿主 getPromptSections()（runtime/qqagent-host/src/skills/manager.js:440）
        #      遍历已注册 skill 时才读得到；
        #   ② 纯 hook 型（topic-guard）：只导出 hooks，靠 on_before_tool 硬拦；
        #   ③ 纯 provider 型（video-frames / account-pool）：只导出 providers，
        #      给核心提供 video.frames / llm.endpoint-pick 等能力。
        # 这三种都没有工具，原判「无工具就不注册」会把它们全挡死。
        # 判据：源码里出现 hooks / providers / promptSections 导出，或清单里有 prompt.sections。
        # 注意：清单里的 prompt 不在 skill 条目上（skill 是 skill-tools.json 的摘要），
        # 必须回 reader 读原始清单才拿得到。
        _decl = skill.get('prompt') if isinstance(skill.get('prompt'), dict) else {}
        _has_prompt = bool((_decl.get('sections') or [])) if _decl else False
        if not _has_prompt:
            try:
                _all_names = reader.names() or []
                for _mn in MANIFEST_NAMES:
                    if _mn not in _all_names:
                        continue
                    _meta = json.loads(reader.read(_mn) or '{}')
                    if isinstance(_meta, dict):
                        _p = _meta.get('prompt')
                        if isinstance(_p, dict) and (_p.get('sections') or []):
                            _has_prompt = True
                            break
            except Exception:
                pass
        _code = ''
        try:
            for _name in (scan.get('files') or []):
                _text = reader.read(_name)
                if _text:
                    _code += _strip_js_comments(_text)
        except Exception:
            _code = ''
        _side = any(token in _code for token in
                    ('export function hooks', 'export const hooks',
                     'export function providers', 'export const providers',
                     'export function promptSections', 'export const promptSections',
                     'hooks:', 'providers:'))
        if not (_has_prompt or _side):
            return None, '清单里的工具名不是运行时全名（缺 __），无法按宿主输出的名字注册：%s' % (
                ', '.join(odd[:3]) or '无工具')
    wanted = [str(cap) for cap in (skill.get('requires') or []) if str(cap).strip()]
    if wanted:
        index = providers if providers is not None else {}
        missing = [cap for cap in wanted if not index.get(cap)]
        if missing and not forced:
            return None, 'requires 没有提供方：%s' % ', '.join(missing[:4])
    return {'id': skill['id'], 'tier': tier, 'name': skill.get('name') or skill['id'],
            'tools': tools, 'usage': sorted(scan['members']), 'blocked': sorted(blocked),
            # 靠哪道闸放行的（只记「放行但仍记账」的那几个成员）：写进注册审计的 released。
            'released': sorted(set(scan['members']) & RELEASED_ACCOUNTED),
            'permissions': skill.get('permissions') or [], 'entry': scan['entry'],
            'files': scan['files'], 'source': source, 'forced': forced}, ''


def _count_by(items: List[dict], key: str) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for item in items:
        value = item.get(key) or ''
        result[value] = result.get(value, 0) + 1
    return result


def eligible_skills(allow=None, manifest=None, runtime=None, market=None) -> dict:
    """算出可注册清单：tier + ctx 用法 + requires + hostImports 四道闸。

    allow 是**额外放行**的 skill id 列表（覆盖 tier 与 ctx 闸门，但不覆盖 T4/T6——那两个
    是结构性不可用）。返回 {eligible, skipped, counts}。
    """
    allow_list = [str(item) for item in (allow or [])]
    allow_set = set(allow_list)
    loaded = (manifest if isinstance(manifest, dict) and 'skills' in manifest
              else load_manifest(runtime=runtime))
    skills = loaded.get('skills') or []
    chosen_market = market if market is not None else market_dir(runtime=runtime)
    index = _market_index(chosen_market)
    modules = host_node_modules(runtime)
    providers = capability_providers(runtime)
    eligible, skipped = [], []
    by_tier: Dict[str, int] = {}
    by_capability: Dict[str, int] = {}
    for skill in skills:
        ident = skill['id']
        tier = skill.get('tier') or '未标注'
        by_tier[tier] = by_tier.get(tier, 0) + 1
        installed = extensions_dir(runtime) / ident
        reader = None
        source = ''
        if _DirReader(installed).exists():
            reader, source = _DirReader(installed), 'installed:%s' % installed
        elif ident in index:
            reader, source = _ZipReader(index[ident]), 'zip:%s' % index[ident]
        try:
            plan, reason = _skill_plan(skill, reader, source, ident in allow_set,
                                       modules, providers)
        finally:
            if isinstance(reader, _ZipReader):
                reader.close()
        if plan is None:
            skipped.append({'id': ident, 'tier': tier, 'reason': reason})
            continue
        for token in plan['usage']:
            by_capability[token] = by_capability.get(token, 0) + 1
        eligible.append(plan)
    counts = {
        'manifest': len(skills),
        'eligible': len(eligible),
        'skipped': len(skipped),
        'tools': sum(len(plan['tools']) for plan in eligible),
        'byTier': by_tier,
        'byEligibleTier': _count_by(eligible, 'tier'),
        'byCapability': dict(sorted(by_capability.items(), key=lambda item: -item[1])),
        'allow': allow_list,
        'manifestSource': loaded.get('source') or '',
        'manifestError': loaded.get('error') or '',
        'market': str(chosen_market or ''),
    }
    # 外部 MCP server 的工具并进同一份工具面：没装外部 server 时这里只多一次 is_file()。
    # 失败/停用的 server 不会出现在 external 里，所以既有数字一个都不变（counts['skillTools']
    # 留底，用来证明 tools 的增量只来自外部）。
    external = _mcp_tools(runtime)
    if external:
        counts['skillTools'] = counts['tools']
        counts['mcpTools'] = len(external)
        counts['tools'] += len(external)
    return {'eligible': eligible, 'skipped': skipped, 'counts': counts,
            'mcpTools': external}


# ── 注册 ─────────────────────────────────────────────────────────────────────
def _redact(value, depth=0):
    """与 src/tool_loop.py:523 同一口径的脱敏。"""
    if depth > 4:
        return '...'
    if isinstance(value, dict):
        return {key: ('***' if _SENSITIVE.search(str(key)) else _redact(item, depth + 1))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:20]]
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:400] + '...'
    return value


def _summarise(value, limit=600) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit] + '...'


def _audit(runtime, record: dict) -> str:
    """写一条 skill-tools 审计；写失败只是少一条记录。"""
    path = audit_path(runtime)
    payload = {'ts': round(time.time(), 3)}
    payload.update(_redact(record))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > AUDIT_MAX_BYTES:
                path.replace(path.with_name(path.stem + '.previous.jsonl'))
        except OSError:
            pass
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + chr(10))
    except (OSError, ValueError, TypeError):
        return ''
    return str(path)


def find_runtime_paths(host_script=None, source_dir=None, node=None) -> dict:
    """定位 node / host-one.mjs / 0.4 源码树；显式参数 > 环境变量 > 复用市场模块的搜索。"""
    def _pick(explicit, env_name, finder):
        if explicit:
            return str(explicit)
        env = os.environ.get(env_name)
        if env:
            return env
        try:
            return str(finder() or '')
        except Exception:
            return ''

    def _find(index):
        from . import qqagent_market
        return qqagent_market.find_runtime()[index]

    return {
        'node': str(node or os.environ.get('QQ_AGENT_NODE') or shutil.which('node') or ''),
        'host_script': _pick(host_script, 'QQ_AGENT_HOST', lambda: _find(0)),
        'source_dir': _pick(source_dir, 'QQ_AGENT_SOURCE', lambda: _find(1)),
    }


# ── tool ctx 注入（子进程环境通道） ──────────────────────────────────────────
# 只填 extension_host.call_tool 的 extra_env：由它拼进这一个 node 子进程的 env，
# 本模块从不写 os.environ（进程环境里的键会被同进程任何子进程看到，且逼着并发调用串行）。
CTX_ENV = 'QQ_AGENT_CTX'
_TOOL_TURN = ContextVar('whale_skill_tool_turn', default=None)


@contextmanager
def tool_context(runtime, turn):
    """Bind trusted ingress metadata to this async task and its worker threads.

    A runtime identity check prevents an unrelated account from reading the turn.
    Always reset the token, including cancellation and nested invocations.
    """
    token = _TOOL_TURN.set((runtime, dict(turn) if isinstance(turn, dict) else {}))
    try:
        yield
    finally:
        _TOOL_TURN.reset(token)


def runtime_tool_context(runtime, turn=None) -> dict:
    """本进程确实知道、不会说谎的 tool ctx 值；拿不到就不写（宁缺不假）。

    Account identity is read live. Per-message fields come only from the explicit
    ingress turn or tool_context binding; callback APIs are never fabricated.
    """
    values = {}
    getter = getattr(runtime, 'account', None)
    if callable(getter):
        try:
            self_id = str(getter() or '').strip()
        except Exception:
            self_id = ''
        if self_id:
            values['selfId'] = self_id
    for name in ('onebot', 'bridge'):
        gateway = getattr(runtime, name, None)
        nickname = str(getattr(gateway, 'nickname', '') or '').strip()
        if nickname:
            values.setdefault('selfNickname', nickname)
    if turn is None:
        bound = _TOOL_TURN.get()
        if bound is not None and bound[0] is runtime:
            turn = bound[1]
    if isinstance(turn, dict):
        def scalar(value):
            return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ''
        kind = scalar(turn.get('kind'))
        target = scalar(turn.get('target'))
        if kind in ('group', 'private') and target:
            values.update(kind=kind, chatId=target, target=target,
                          chatKey=kind + ':' + target)
            if kind == 'group':
                values['groupId'] = target
            sender = scalar(turn.get('sender_id')) or (target if kind == 'private' else '')
            if sender:
                values.update(userId=sender, senderId=sender)
            message_id = scalar(turn.get('id'))
            if message_id:
                values['msgId'] = message_id
            chat_name = scalar(turn.get('group_name') if kind == 'group' else turn.get('sender_name'))
            if chat_name:
                values['chatName'] = chat_name
    return {key: value for key, value in values.items() if key in INJECTABLE_TOOL_CTX}


def context_env(context) -> str:
    """把 ctx 值收成 QQ_AGENT_CTX 的 JSON；只留可注入的标量键，一个都没有就返回 ''。

    context 可以是 dict，也可以是「每次调用现取」的 callable（账号信息重连后会变）。
    """
    values = context() if callable(context) else context
    if not isinstance(values, dict):
        return ''
    payload = {}
    for key, value in values.items():
        if key not in INJECTABLE_TOOL_CTX or not isinstance(value, (str, int, float, bool)):
            continue
        text = str(value)
        if text:
            payload[key] = text
    if not payload:
        return ''
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError):
        return ''


# ── 失败日志：技能没跑起来时把原因写进结构化日志（src/app_log.py）───────────
#: 同一 (source, 工具, 错误码) 的去重窗口（秒）：坏技能不能把 1000 条环形缓冲刷满。
FAILURE_LOG_WINDOW = 60.0
_FAILURE_LOG_AT: Dict[tuple, float] = {}


def _failure_log_gate(key: tuple) -> bool:
    """窗口内同一个 key 只放行一次；顺手清掉已出窗的旧键，防长期累积。"""
    now = time.monotonic()
    last = _FAILURE_LOG_AT.get(key)
    if last is not None and now - last < FAILURE_LOG_WINDOW:
        return False
    _FAILURE_LOG_AT[key] = now
    if len(_FAILURE_LOG_AT) > 256:
        for stale in [k for k, at in _FAILURE_LOG_AT.items()
                      if now - at >= FAILURE_LOG_WINDOW]:
            _FAILURE_LOG_AT.pop(stale, None)
    return True


def _failure_detail(value) -> str:
    """结构化返回压成一段文本：JSON 优先，退到 repr，再截到 DETAIL_MAX。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    return text[:app_log.DETAIL_MAX]


def _log_skill_failure(data_dir, *, tool_id, extension_id, code, message, payload) -> None:
    """技能工具失败落一条 error（source='skill'）；整段只吞不抛，不影响返回值。"""
    try:
        if not _failure_log_gate(('skill', tool_id, code)):
            return
        app_log.default_app_log(data_dir).error(
            'skill', str(message)[:app_log.MESSAGE_MAX], detail=_failure_detail(payload),
            code=str(code or '')[:app_log.CODE_MAX],
            extra={'tool': tool_id, 'extension': extension_id})
    except Exception:
        pass


# ── 外部 MCP server 的接线（实现全在 src/mcp_tools.py） ─────────────────────
def _mcp_tools(runtime=None) -> List[dict]:
    """外部 MCP 工具：只有 enabled 且枚举成功的 server 才出条目；任何失败折成空表。"""
    try:
        return mcp_tools.tool_list(mcp_root(runtime))
    except Exception:
        return []


def _mcp_delegate(qq_only_dir, tool_id, arguments):
    """把工具环的一次调用委派给外部 MCP server；不是 MCP 工具就返回 None。

    返回形状与技能 handler 一致（ok/tool/result 或 ok/tool/error），所以调用方无需区分。
    data_dir 在这里是 <data_root>/data/qq-only（register_skills 传的就是 data_dir(runtime)），
    mcp_tools 认得出这个形态，也认得出上一级的数据根。
    """
    if '__' not in str(tool_id or ''):
        return None
    try:
        provider = mcp_tools.provider_for(qq_only_dir)
        if not provider.owns(tool_id):
            return None
        outcome = provider.call(tool_id, arguments)
    except Exception as exc:                       # 兜底：委派层抛异常会打断整条主链路
        outcome = {'ok': False, 'code': 'internal',
                   'message': '%s: %s' % (type(exc).__name__, exc)}
    if outcome.get('ok'):
        return {'ok': True, 'tool': tool_id,
                'result': {'content': outcome.get('content') or [],
                           'isError': bool(outcome.get('isError'))}}
    return {'ok': False, 'tool': tool_id,
            'error': {'code': str(outcome.get('code') or 'mcp-error'),
                      'message': str(outcome.get('message') or '外部 MCP 工具调用失败')[:300]}}


def build_tool_handler(*, node, host_script, source_dir, extension, extension_id,
                       mapping, timeout=None, data_dir=None, context=None,
                       required_context=()) -> object:
    """造一个 async handler(name, arguments)：tool_loop 只 await 协程函数。

    mapping 是 原名 → 运行时全名。tool_loop 用 ToolSpec.raw_name 回调
    （src/tool_loop.py:678），两种名字都认。超时留 10% 余量，保证结构化错误先于工具环
    外层超时落地。context 是 dict 或 callable（见 runtime_tool_context）：只有它给出真值
    时才会把 QQ_AGENT_CTX 放进这一个子进程的 env；为 None/空时行为与旧版逐字一致。
    """
    seconds = float(timeout or DEFAULT_TOOL_TIMEOUT)
    if seconds <= 0:
        seconds = DEFAULT_TOOL_TIMEOUT
    budget = max(0.01, seconds * 0.9)

    async def handler(name, arguments):
        tool_id = mapping.get(str(name or '')) or str(name or '')
        payload = arguments if isinstance(arguments, dict) else {}
        # 外部 MCP 工具在这里分叉：不是 MCP 工具时 _mcp_delegate 返回 None，下面一字不动。
        delegated = _mcp_delegate(data_dir, tool_id, payload)
        if delegated is not None:
            return delegated
        env_ctx = context_env(context)
        injected = json.loads(env_ctx) if env_ctx else {}
        missing = sorted(set(required_context) - injected.keys())
        if missing:
            outcome = {'ok': False, 'tool': tool_id,
                       'error': {'code': 'missing-context',
                                 'message': '当前调用缺少真实会话上下文：' + ', '.join(missing)}}
            _log_skill_failure(data_dir, tool_id=tool_id, extension_id=extension_id,
                               code='missing-context',
                               message='技能工具 %s 没跑起来：%s' % (
                                   tool_id, outcome['error']['message']),
                               payload=outcome)
            return outcome
        # ctx 只走 extra_env 进这一个子进程；env 为空时连这个键都不传（与旧版逐字一致）。
        extra_env = {CTX_ENV: env_ctx} if env_ctx else {}
        try:
            outcome = await asyncio.wait_for(
                extension_host.call_tool(
                    node=node, host_script=host_script, extension=extension,
                    source_dir=source_dir, tool_id=tool_id, arguments=payload,
                    timeout=seconds, data_dir=data_dir, extension_id=extension_id,
                    allowed_ids=list(mapping.values()), extra_env=extra_env),
                timeout=budget)
        except asyncio.TimeoutError:
            outcome = {'ok': False, 'tool': tool_id,
                       'error': {'code': 'timeout', 'message': '工具超时（%gs）' % seconds}}
            _log_skill_failure(data_dir, tool_id=tool_id, extension_id=extension_id,
                               code='timeout',
                               message='技能工具 %s 超时（%gs）' % (tool_id, seconds),
                               payload=outcome)
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = {'ok': False, 'tool': tool_id,
                       'error': {'code': 'handler-error',
                                 'message': '%s: %s' % (type(exc).__name__, exc)}}
            _log_skill_failure(data_dir, tool_id=tool_id, extension_id=extension_id,
                               code='handler-error',
                               message='技能工具 %s 执行抛异常：%s' % (
                                   tool_id, outcome['error']['message']),
                               payload=outcome)
            return outcome
        if outcome.get('ok'):
            return {'ok': True, 'tool': tool_id, 'result': outcome.get('value')}
        message = str(outcome.get('error') or 'extension-error')
        code = 'extension-error'
        if 'timeout' in message.lower():
            code = 'timeout'
        elif '白名单' in message:
            code = 'not-allowed'
        elif '不属于这个扩展' in message:
            code = 'not-owned'
        outcome = {'ok': False, 'tool': tool_id,
                   'error': {'code': code, 'message': message[:300]}}
        _log_skill_failure(data_dir, tool_id=tool_id, extension_id=extension_id,
                           code=code,
                           message='技能工具 %s 调用失败：%s' % (tool_id, message),
                           payload=outcome)
        return outcome

    return handler


def _tool_timeout(runtime) -> float:
    try:
        settings = tool_loop.loop_settings(getattr(runtime, 'config', None))
        return float(getattr(settings, 'tool_timeout', DEFAULT_TOOL_TIMEOUT) or DEFAULT_TOOL_TIMEOUT)
    except Exception:
        return DEFAULT_TOOL_TIMEOUT


def register_skills(runtime, allow=None, limit=None, manifest=None,
                    extensions_root=None, host_script=None, source_dir=None,
                    node=None, timeout=None) -> dict:
    """把可注册的 skill 工具逐个注册进这个 runtime 的工具表。

    单个 skill 装/注册失败只记进 failed，不影响其他；返回
    {registered, skipped, failed, counts}。
    """
    started = time.time()
    plan = eligible_skills(allow=allow, manifest=manifest, runtime=runtime)
    paths = find_runtime_paths(host_script, source_dir, node)
    seconds = float(timeout or _tool_timeout(runtime))
    root = Path(extensions_root) if extensions_root else extensions_dir(runtime)
    registered, skipped, failed = [], list(plan['skipped']), []
    candidates = plan['eligible']
    wanted = set(str(item) for item in (allow or []))
    if wanted:
        for extra in candidates:
            if extra['id'] not in wanted:
                skipped.append({'id': extra['id'], 'tier': extra['tier'],
                                'reason': '不在 allow 白名单里'})
        candidates = [item for item in candidates if item['id'] in wanted]
    if limit is not None:
        cut = max(0, int(limit))
        for extra in candidates[cut:]:
            skipped.append({'id': extra['id'], 'tier': extra['tier'],
                            'reason': 'limit=%s 截断' % limit})
        candidates = candidates[:cut]
    # 外部 MCP server 的工具与 node/host-one.mjs 无关（独立进程 + 自己的协议），
    # 所以这段排在 env 早退之前：宿主脚本不可用也不许吃掉外部工具。
    registry = tool_loop.registry_for(runtime)
    # 外部 MCP server 的工具：与扩展工具同一张表、同一个 handler，靠 _mcp_delegate 分叉。
    # 注册键必须与 provider 算出的 id 一致（tool_loop 拼出来的也是 <serverId>__<toolName>）。
    mcp_registered: List[str] = []
    mcp_entries = _mcp_tools(runtime)
    if mcp_entries:
        groups: Dict[str, List[dict]] = {}
        for entry in mcp_entries:
            groups.setdefault(str(entry.get('server') or ''), []).append(entry)
        for server_id, entries in groups.items():
            try:
                registry.unregister_source(server_id)   # 重名会被加成 __2，重注册必须先摘
            except Exception:
                pass
            specs = [{'name': entry['name'],
                      'description': entry.get('description') or entry['name'],
                      'parameters': entry.get('parameters') or {}} for entry in entries]
            handler = build_tool_handler(
                node=paths['node'], host_script=paths['host_script'],
                source_dir=paths['source_dir'], extension='', extension_id=server_id,
                mapping={}, timeout=seconds, data_dir=data_dir(runtime))
            try:
                tool_loop.register_tools(runtime, server_id, specs, handler=handler)
            except Exception as exc:
                failed.append({'id': server_id,
                               'error': '%s: %s' % (type(exc).__name__, exc)})
                continue
            live = set(tool_loop.registry_for(runtime).names())
            for entry in entries:
                if entry['id'] in live:
                    mcp_registered.append(entry['id'])
                else:
                    skipped.append({'id': server_id, 'tier': 'mcp',
                                    'reason': '注册后工具表里没有期望的键 %s' % entry['id']})
    problem = ''
    if not paths['node']:
        problem = '找不到 node 可执行文件'
    elif not paths['host_script'] or not Path(paths['host_script']).is_file():
        problem = 'host-one.mjs 不可用（找不到宿主脚本）'
    elif not paths['source_dir'] or not Path(paths['source_dir']).is_dir():
        problem = '0.4 源码树不可用（找不到 src/plugin-loader.js）'
    if problem:
        for item in candidates:
            skipped.append({'id': item['id'], 'tier': item['tier'], 'reason': problem})
        _audit(runtime, {'phase': 'register-skip', 'reason': problem, 'ok': False,
                         'skills': [item['id'] for item in candidates]})
        counts = {'manifest': plan['counts']['manifest'],
                  'eligible': len(plan['eligible']), 'registered': 0,
                  'registeredTools': 0, 'skipped': len(skipped), 'failed': 0,
                  'env': paths, 'envProblem': problem,
                  'elapsedMs': int((time.time() - started) * 1000)}
        if mcp_registered:
            counts['mcpTools'] = len(mcp_registered)
        report = {'registered': registered, 'skipped': skipped, 'failed': failed,
                  'counts': counts}
        if mcp_registered:
            report['mcpTools'] = list(mcp_registered)
        return report

    # 先摘掉本批 skill 的旧工具（重名会被 tool_loop 加成 __2，重复调用必须幂等）
    for prefix in {tool['prefix'] for item in candidates for tool in item['tools']}:
        try:
            registry.unregister_source(prefix)
        except Exception:
            continue

    for item in candidates:
        package = root / item['id']
        if not package.is_dir():
            skipped.append({'id': item['id'], 'tier': item['tier'],
                            'reason': '没有安装目录 %s（先 install_skill）' % package})
            continue
        groups: Dict[str, List[dict]] = {}
        for tool in item['tools']:
            groups.setdefault(tool['prefix'], []).append(tool)
        names = []
        for prefix, tools in groups.items():
            specs = []
            for tool in tools:
                # schema 从源码静态抠（src/tool_schema.py）：宿主 owned_tools 不回传
                # parameters，清单也只留了 hasSchema 布尔；写死空对象会让模型只能猜参数名
                # （实测 wiki_query 传 keyword 就被拒「缺少要查的名字」）。
                parameters = tool_schema.schema_for(package, tool['raw'])
                specs.append({'name': tool['raw'],
                              'description': tool['description'] or tool['full'],
                              'parameters': parameters})
            handler = build_tool_handler(
                node=paths['node'], host_script=paths['host_script'],
                source_dir=paths['source_dir'], extension=str(package),
                extension_id=prefix, mapping={tool['raw']: tool['full'] for tool in tools},
                timeout=seconds, data_dir=data_dir(runtime),
                # 现取而不是注册时快照：账号/昵称重连后会变，快照就会说谎。
                context=lambda: runtime_tool_context(runtime),
                required_context=set(item['usage']) & SESSION_TOOL_CTX)
            try:
                tool_loop.register_tools(runtime, prefix, specs, handler=handler)
            except Exception as exc:
                failed.append({'id': item['id'],
                               'error': '%s: %s' % (type(exc).__name__, exc)})
                _audit(runtime, {'phase': 'register-error', 'skill': item['id'],
                                 'source': prefix, 'error': str(exc)[:300], 'ok': False})
                names = []
                break
            registered_names = tool_loop.registry_for(runtime).names()
            for tool in tools:
                # 工具表里的键是 _clean_name(source + '__' + raw_name)，点号等会被洗成下划线
                # （reply-meme_1.2.0 → reply-meme_1_2_0）；清单里的 full 是原始拼法。
                # 两边必须用同一口径比，否则这类 skill 明明注册成功也会被记成失败。
                if tool_loop._clean_name(tool['full']) in registered_names:
                    names.append(tool['full'])
                else:
                    failed.append({'id': item['id'],
                                   'error': '注册后工具表里没有期望的键 %s（可能被静态清单 '
                                            'hidden 或重名加后缀）' % tool['full']})
        if names:
            registered.append({'id': item['id'], 'tier': item['tier'], 'tools': names,
                               'released': item['released']})
            record = {'phase': 'register', 'skill': item['id'], 'tier': item['tier'],
                      'tools': names, 'source': item['source'], 'ok': True, 'count': len(names)}
            if item['released']:
                # 放行仍记账：把「靠子进程/出网能力放进来」写进既有账本，供事后按 skill
                # id 与 data/qqagent_bridge_calls.jsonl 的 extension 列对齐。
                record['released'] = item['released']
            _audit(runtime, record)
    counts = {'manifest': plan['counts']['manifest'], 'eligible': len(plan['eligible']),
              'registered': len(registered),
              'registeredTools': sum(len(item['tools']) for item in registered),
              'skipped': len(skipped), 'failed': len(failed), 'env': paths,
              'elapsedMs': int((time.time() - started) * 1000)}
    if mcp_registered:
        counts['mcpTools'] = len(mcp_registered)
    summary = {'phase': 'register-summary', 'ok': True,
               'counts': {key: value for key, value in counts.items() if key != 'env'},
               'registered': [item['id'] for item in registered]}
    if mcp_registered:
        summary['mcpTools'] = list(mcp_registered)
    released = {item['id']: item['released'] for item in registered if item['released']}
    if released:
        summary['released'] = released
    _audit(runtime, summary)
    return {'registered': registered, 'skipped': skipped, 'failed': failed, 'counts': counts}
