"""skill 运行时清单生成器（WP8a）。

对市场 zip / 已安装目录里的每个 skill 包做**静态**分层，产出与
.tmp/inv/skill-tools.json 同构的 {meta, stats, skills} 清单，
写入 <data>/qq-only/skill-tools.json 供 skill_tools.load_manifest 使用。

约束：纯标准库、不联网、不执行被扫描包的 JS、分类不依赖 node。
tool_schema.extract_tools 是唯一会触碰包内容的解析器（纯 Python 静态解析）。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import posixpath
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

try:  # 包内导入
    from . import skill_tools
    from . import tool_schema
except ImportError:  # 直接运行 / 测试以仓库根为 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import skill_tools
    from src import tool_schema

MANIFEST_FILE = getattr(skill_tools, "MANIFEST_FILE", "skill-tools.json")
MANIFEST_NAMES = tuple(getattr(skill_tools, "MANIFEST_NAMES", ("skill.json", "plugin.json")))
ENTRY_NAMES = tuple(getattr(skill_tools, "ENTRY_NAMES", ("index.js", "index.mjs", "index.cjs")))
JS_EXTS = (".js", ".mjs", ".cjs")

MANIFEST_VERSION = 1
TIER_ORDER = ("T6-disabled", "T4-host-source", "T5-depends-other", "T3-extra-api", "T2-web-fetch", "T1-ready")
# 扩展 API 里「宿主还需额外实现」的部分：命中即 T3。
EXTRA_APIS = frozenset({"onebot", "qq_sticker"})
# 与本模块分层口径一致：<run>/skills/<pkg>/ 向上 2 层即 0.4 宿主源码目录。
HOST_SOURCE_DEPTH = 2

# 不吞掉 "https://" 里的双斜杠，否则 URL 会被当成行注释截断。
_COMMENT_RE = re.compile(r"(?<!:)//[^\n]*|/\*.*?\*/", re.S)
_QUOTE = "'\""
# 只认**静态** from / require：import(x) 是动态导入（运行期才解析，宿主源码耦合无从判定），
# 裸 import 'x' 也不计——既有清单的 host_imports 正是这个口径（376/376 逐条一致）。
_STATIC_IMPORT_RES = (
    re.compile(r"\bfrom\s*[" + _QUOTE + r"]([^" + _QUOTE + r"\n]+)[" + _QUOTE + r"]"),
    re.compile(r"\brequire\s*\(\s*[" + _QUOTE + r"]([^" + _QUOTE + r"\n]+)[" + _QUOTE + r"]"),
)
_REGISTER_RE = re.compile(r"\bregisterTool\b")
_CTX_MEMBER_RE = re.compile(r"\bctx\s*\.\s*([A-Za-z_$][\w$]*)")
_API_CTX_RE = re.compile(r"\bapi\s*\.\s*ctx\s*\.\s*([A-Za-z_$][\w$]*)")
_URL_RE = re.compile(r"https?://([^/\s\"'" + chr(96) + r"<>)\]]+)")
_LOCAL_HOST_RE = re.compile(
    r"^(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+|\[?::1\]?)$",
    re.I,
)
_BARE_HOST_RE = re.compile(r"^[\d.]+$|^\[[\da-f:]+\]$", re.I)
# 只认形如 a.b / a.b.c 的真域名：排除 https://a.b（占位）、CJK 文本、带中文标点的残片。
_HOST_OK_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")

_TEXT_EXTS = JS_EXTS + (".json", ".md", ".txt", ".html", ".htm", ".css", ".yaml", ".yml", ".xml", ".svg")


# --------------------------------------------------------------------------- 基础工具


def _strip_comments(text):
    return _COMMENT_RE.sub("", text or "")


def _lead_up(path):
    """统计规范化路径开头的 .. 层数。"""
    parts = path.split("/")
    count = 0
    while count < len(parts) and parts[count] == "..":
        count += 1
    return count


def _norm_spec(spec):
    return spec.split("?")[0].split("#")[0].strip()


def _static_imports(text):
    specs = set()
    for pattern in _STATIC_IMPORT_RES:
        for match in pattern.finditer(text):
            specs.add(match.group(1))
    return specs


def _reader_for(path):
    """按路径类型返回 skill_tools 的目录 / zip 读取器。"""
    path = Path(path)
    if path.is_dir():
        return skill_tools._DirReader(path)
    return skill_tools._ZipReader(path)


def _close(reader):
    close = getattr(reader, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _package_root(names):
    """包根 = 最浅的「既有清单、又有入口」的目录；都没有入口时退回最浅清单目录。

    市场里有一批包是双层嵌套（真例 <id>/<id>/index.js）：外层只放了
    skill.json / plugin.json 副本，真包在内层。按「最浅清单目录」取根会把
    entry 拼成 <id>/index.js 而文件根本不存在，整包被判 no-entry。
    """
    manifests = [name for name in names if posixpath.basename(name) in MANIFEST_NAMES]
    if not manifests:
        return None
    name_set = set(names)
    dirs = sorted({posixpath.dirname(name) for name in manifests},
                  key=lambda item: (item.count("/") if item else -1, item))
    for root in dirs:
        for entry in ENTRY_NAMES:
            if (posixpath.join(root, entry) if root else entry) in name_set:
                return root
    return dirs[0]


def _package_entry(root, names):
    for entry in ENTRY_NAMES:
        candidate = posixpath.join(root, entry) if root else entry
        if candidate in names:
            return candidate
    return ""


def _read(reader, name):
    try:
        return reader.read(name) or ""
    except Exception:
        return ""


def _read_big(reader, name, limit=4 * 1024 * 1024):
    """读大文件（reader.read 有 512KB 上限；vendor 里动辄 2MB 的 .mjs 也要扫主机名）。"""
    text = _read(reader, name)
    if text:
        return text
    try:
        if hasattr(reader, "_open"):
            info = reader._open().getinfo(name)
            if info.file_size > limit:
                return ""
            return reader._open().read(info).decode("utf-8", "replace")
        root = getattr(reader, "root", None)
        if root is not None:
            path = Path(root) / name
            if path.is_file() and path.stat().st_size <= limit:
                return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _external_hosts(reader, names):
    """从包内 **JS** 文件里提取外部域名（与既有清单口径一致：只认代码里的 URL）。"""
    hosts = set()
    for name in names:
        if not name.endswith(JS_EXTS):
            continue
        text = _read_big(reader, name)
        if "http" not in text:
            continue
        for match in _URL_RE.finditer(text):
            host = match.group(1).split(":")[0].strip().rstrip(".").lower()
            if not host or "$" in host or "{" in host or "}" in host:
                continue
            if _LOCAL_HOST_RE.match(host) or _BARE_HOST_RE.match(host):
                continue
            if not _HOST_OK_RE.match(host):
                continue
            hosts.add(host)
    return sorted(hosts)


def _ctx_usage(reader, closure):
    usage = set()
    for name in closure or ():
        text = _strip_comments(_read(reader, name))
        usage.update(_CTX_MEMBER_RE.findall(text))
        usage.update(_API_CTX_RE.findall(text))
    return sorted(usage)


def _manifest_decl(reader, root):
    for candidate in MANIFEST_NAMES:
        name = posixpath.join(root, candidate) if root else candidate
        text = _read(reader, name)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _as_list(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return list(raw.keys())
    return [] if raw in (None, "") else [raw]


def _perm_list(manifest):
    return [
        value.strip().lower()
        for value in _as_list(manifest.get("permissions"))
        if isinstance(value, str) and value.strip()
    ]


def _requires_list(manifest):
    return [
        value.strip()
        for value in _as_list(manifest.get("requires"))
        if isinstance(value, str) and value.strip()
    ]


# --------------------------------------------------------------------------- 分层


def _tool_entries(tools, skill_id):
    entries = []
    for tool_id, tool in sorted((tools or {}).items()):
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or tool_id)
        if "__" not in name and skill_id:
            name = skill_id + "__" + name
        entries.append(
            {
                "name": name,
                "description": str(tool.get("description") or "")[:200],
                "hasSchema": bool(tool.get("parameters")),
            }
        )
    return entries


def classify(reader, tools=None, skill_id=""):
    """对**一个** skill 包做静态分层。

    返回 {tier, tierReason, tools, usage, requires, hostImports, permissions, external, entry, root}。
    reader 支持 skill_tools._DirReader / _ZipReader（有 names() / read()）。
    """
    names = list(reader.names())
    name_set = set(names)
    root = _package_root(names)
    entry = _package_entry(root, name_set) if root is not None else ""
    declaration = _manifest_decl(reader, root) if root is not None else {}
    permissions = _perm_list(declaration)
    requires = _requires_list(declaration)

    host_imports = []
    missing = []
    escape_above = []
    register_sites = 0
    for name in names:
        if not name.endswith(JS_EXTS):
            continue
        text = _read(reader, name)
        register_sites += len(_REGISTER_RE.findall(_strip_comments(text)))
        inner = posixpath.relpath(name, root) if root else name
        inner_dir = posixpath.dirname(inner)
        inner_depth = 0 if inner_dir in ("", ".") else inner_dir.count("/") + 1
        for spec in _static_imports(text):
            if not spec.startswith(".."):
                continue
            norm = _norm_spec(spec)
            # 逃逸层级相对**包根**：2 层 = <run>/src（0.4 宿主源码），3 层 = 运行目录之外
            depth = _lead_up(norm) - inner_depth
            host_imports.append(spec)
            if depth >= HOST_SOURCE_DEPTH + 1 and name == entry:
                # 入口自己就往上逃 3 层：越过运行目录，加载期必失败（非入口文件不影响加载）
                escape_above.append(spec)
            elif depth < HOST_SOURCE_DEPTH:
                # 指向同级 skill 包且文件不存在：加载期直接 ERR_MODULE_NOT_FOUND
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), norm))
                inside = root in (None, "") or target == root or target.startswith(root + "/")
                if not inside and target.endswith(JS_EXTS) and target not in name_set:
                    missing.append(spec)

    scan = skill_tools._scan_reader(reader) or {}
    closure = list(scan.get("files") or ([entry] if entry else []))
    usage = _ctx_usage(reader, closure)
    external = _external_hosts(reader, name_set)
    tool_list = _tool_entries(tools, skill_id or (posixpath.basename(root) if root else ""))

    def result(tier, reason):
        return {
            "tier": tier,
            "tierReason": reason,
            "tools": tool_list,
            "usage": usage,
            "requires": requires,
            "hostImports": sorted(set(host_imports)),
            "permissions": permissions,
            "external": external,
            "entry": entry,
            "root": root,
        }

    if root is None:
        return result("T6-disabled", "no-manifest")
    if not entry:
        return result("T6-disabled", "no-entry")
    if missing:
        return result("T6-disabled", "load-failed:%s" % sorted(set(missing))[0])
    if register_sites == 0 and not tool_list:
        # 有一类包天生没有工具、但必须注册才生效（纯提示词 / 纯 hook / 纯 provider）：
        #   · force-send   —— 功能全在 skill.json 的 prompt.sections，
        #                    宿主 getPromptSections()（src/skills/manager.js:440）
        #                    遍历**已注册** skill 时才读得到；
        #   · topic-guard  —— 只导出 hooks，靠 on_before_tool 硬拦；
        #   · video-frames —— 只导出 providers，给核心提供 video.frames；
        #   · account-pool —— 只导出 providers，提供 llm.endpoint-pick 等。
        # 它们都没有工具，原判「无工具就不注册」把整类包判成结构性残缺。
        _side = False
        _decl2 = declaration if isinstance(declaration, dict) else {}
        _prompt = _decl2.get("prompt") if isinstance(_decl2.get("prompt"), dict) else {}
        if _prompt.get("sections"):
            _side = True
        if not _side:
            for _n in names:
                if not _n.endswith(JS_EXTS):
                    continue
                _t = _read(reader, _n)
                if not _t:
                    continue
                _c = _strip_comments(_t)
                if any(_tok in _c for _tok in (
                        "export function hooks", "export const hooks",
                        "export function providers", "export const providers",
                        "export function promptSections", "export const promptSections",
                        "hooks:", "providers:")):
                    _side = True
                    break
        if not _side:
            return result("T6-disabled", "no-tools")
    if escape_above:
        return result("T6-disabled", "escape-run-root:%s" % sorted(set(escape_above))[0])
    if host_imports:
        # 这里故意保持不可达（条件用的是上一行已判成 T6 的 escape_above），别顺手改成
        # `if host_imports: return T4`。实测依据（2026-11-07 17:0x，.tmp/t4_impact.py）：
        # 拿 host_imports 直接判 T4 会让 data/qq-only/skill_extensions/ 里 373 个真实包
        # 中的 85 个从 T1/T2 翻成 T4-host-source，而 BLOCKED_TIERS 对 T4 是「不加载、
        # 不重试」⇒ 85 个包当场全部不再注册（bili / machine-work / acmbot / deep-think
        # / academic-assistant 都在里面）。根因是 2 层上溯并不算不可用：宿主把包拷到
        # <source>/skills-host-<pid>/0000-<id>/（runtime/qqagent-host/host-one.mjs:204），
        # 从包根上溯 2 层正好回到 <source>/，`../../src/config.js` 实测能 import。
        # 真正的加载失败（入口自己上溯 >=3 层）由上面的 escape_above 判成 T6。
        _above = sorted(set(escape_above))
        if _above:
            return result("T4-host-source", "host-source:%s" % _above[0])
    if host_imports:
        _above = sorted(set(escape_above))
        if _above:
            return result("T4-host-source", "host-source:%s" % _above[0])
    if requires:
        return result("T5-depends-other", "requires:%s" % requires[0])
    # onebot / qq_sticker 曾被视为「宿主没开放的额外 API」，实测宿主本来就给：
    #   ctx.onebot  —— runtime/qqagent-host/src/orchestrator.js:958 `onebot: this.onebot`
    #   ctx.stickers —— orchestrator.js:961 `stickers: this.stickers`
    # 工具执行时 ctx 由 tools.js:131-134 的注释明确列出（onebot, store, memory, stickers,
    # sender, session 都在里面）。self-profile 实测 list_tools ok + set_profile 能跑
    # （只因为测试时没有 QQ 上下文才回「拿不到请求者 QQ 号」）。
    # 所以这两个权限不再降级到 T3；真需要额外 API 的包另说。
    if "web_fetch" in permissions:
        return result("T2-web-fetch", "permission:web_fetch")
    if external:
        return result("T2-web-fetch", "external:%s" % external[0])
    return result("T1-ready", "clean")


# --------------------------------------------------------------------------- 清单构建


def _market_zips(market_dir):
    """id -> zip 路径（与 skill_tools._market_index 同口径）。"""
    if not market_dir:
        return {}
    directory = Path(market_dir)
    if not directory.is_dir():
        return {}
    try:
        return dict(skill_tools._market_index(directory))
    except Exception:
        return {path.name[:-4]: path for path in sorted(directory.glob("*.zip"))}


def _installed_dirs(extensions_root):
    if not extensions_root:
        return {}
    directory = Path(extensions_root)
    if not directory.is_dir():
        return {}
    return {child.name: child for child in sorted(directory.iterdir()) if child.is_dir()}


@contextlib.contextmanager
def _tool_budget(seconds=2.0):
    """临时抬高 tool_schema 的解析预算。

    tool_schema.TIME_BUDGET=0.05s 是给「在线扫一个包」用的：机器一忙就降级成
    半成品，工具数会随负载抖动。清单是离线产出的稳定快照，这里临时放宽，
    结束后原样还原（不改 tool_schema.py 本身）。
    """
    original = getattr(tool_schema, "TIME_BUDGET", None)
    try:
        if original is not None:
            tool_schema.TIME_BUDGET = seconds
        yield
    finally:
        if original is not None:
            tool_schema.TIME_BUDGET = original


def _package_dir(root):
    """包根目录：清单落在子目录里时（真例 <id>/<id>/index.js）下潜到清单所在层。

    tool_schema.extract_tools 只读**根目录**的清单，包根多一层就会漏掉清单里声明的 tools。
    """
    base = Path(root)
    try:
        names = [item.relative_to(base).as_posix() for item in base.rglob("*") if item.is_file()]
    except OSError:
        return base
    inner = _package_root(names)
    candidate = base / inner if inner else base
    return candidate if candidate.is_dir() else base


def _extract_tools_for(path):
    """把包（zip 或目录）落到临时目录后调 tool_schema.extract_tools。"""
    from_path = Path(path)
    if from_path.is_dir():
        with _tool_budget():
            return tool_schema.extract_tools(_package_dir(from_path)) or {}
    if not zipfile.is_zipfile(from_path):
        return {}
    with tempfile.TemporaryDirectory(prefix="skill-manifest-") as tmp:
        try:
            with zipfile.ZipFile(from_path) as archive:
                archive.extractall(tmp)
        except Exception:
            return {}
        with _tool_budget():
            return tool_schema.extract_tools(_package_dir(tmp)) or {}


def _skill_entry(skill_id, path, kind):
    started = time.time()
    tools = _extract_tools_for(path)
    reader = _reader_for(path)
    try:
        info = classify(reader, tools=tools, skill_id=skill_id)
    finally:
        _close(reader)
    declaration = _manifest_decl(reader, info["root"]) if info["root"] is not None else {}
    entry = {
        "id": skill_id,
        "name": str(declaration.get("name") or skill_id),
        "version": str(declaration.get("version") or ""),
        "tier": info["tier"],
        "tierReason": info["tierReason"],
        "tools": info["tools"],
        "permissions": info["permissions"],
        "requires": info["requires"],
        "hostImports": info["hostImports"],
        "external": info["external"],
        "usage": info["usage"],
        "entry": info["entry"],
        "source": {"kind": kind, "path": str(path)},
    }
    return entry


def _stats(skills, zips_in_market):
    tier_counts = {tier: 0 for tier in TIER_ORDER}
    tier_tools = {tier: 0 for tier in TIER_ORDER}
    hosts = set()
    tools_total = 0
    for skill in skills:
        tier = skill.get("tier")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        count = len(skill.get("tools") or [])
        tier_tools[tier] = tier_tools.get(tier, 0) + count
        tools_total += count
        hosts.update(skill.get("external") or [])
    return {
        "skillsTotal": len(skills),
        "toolsTotal": tools_total,
        "zipsInMarketDir": zips_in_market,
        "tierCounts": tier_counts,
        "tierToolCounts": tier_tools,
        "skillsWithExternalHosts": sum(1 for skill in skills if skill.get("external")),
        "distinctExternalHosts": len(hosts),
    }


def build_manifest(market_dir=None, extensions_root=None):
    """扫市场 zip + 已安装目录（已安装优先覆盖同名 zip）构建清单。"""
    started = time.time()
    market = market_dir if market_dir is not None else skill_tools.market_dir()
    installed = extensions_root
    if installed is None:
        try:
            installed = skill_tools.extensions_dir(None)
        except Exception:
            installed = None
    zips = _market_zips(market)
    dirs = _installed_dirs(installed)
    skills = []
    for skill_id in sorted(set(zips) | set(dirs)):
        try:
            if skill_id in dirs:
                skills.append(_skill_entry(skill_id, dirs[skill_id], "installed"))
            else:
                skills.append(_skill_entry(skill_id, zips[skill_id], "market"))
        except Exception as exc:  # 单个包坏掉不能拖垮整次构建
            skills.append(
                {
                    "id": skill_id,
                    "name": skill_id,
                    "version": "",
                    "tier": "T6-disabled",
                    "tierReason": "error:%s" % type(exc).__name__,
                    "tools": [],
                    "permissions": [],
                    "requires": [],
                    "hostImports": [],
                    "external": [],
                    "usage": [],
                    "entry": "",
                    "source": {"kind": "unknown", "path": str(zips.get(skill_id) or dirs.get(skill_id))},
                    "elapsedMs": 0,
                }
            )
    meta = {
        "version": MANIFEST_VERSION,
        "generator": "src/skill_manifest.py",
        "writtenAt": int(time.time()),
        "marketDir": str(market or ""),
        "extensionsRoot": str(installed or ""),
        "elapsedMs": int(round((time.time() - started) * 1000)),
        "overrides": sorted(set(zips) & set(dirs)),
    }
    return {"meta": meta, "stats": _stats(skills, len(zips)), "skills": skills}


def default_manifest_path(runtime=None):
    return Path(skill_tools.data_dir(runtime)) / MANIFEST_FILE


def stamp(path=None):
    """清单**内容**指纹（忽略 writtenAt / elapsedMs 这类每次都会变的元数据）。

    文件不存在或不是合法清单时返回 None —— 上层据此判断是否需要重扫。
    """
    target = Path(path) if path is not None else default_manifest_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    meta = data.get("meta") if isinstance(data, dict) else None
    if isinstance(meta, dict):
        for volatile in ("writtenAt", "elapsedMs"):
            meta.pop(volatile, None)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atomic_write(target, manifest, started=None):
    """原子写（tmp + os.replace）；返回写入结果字典。"""
    started = time.time() if started is None else started
    payload = json.dumps(manifest, ensure_ascii=False, indent=1)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "path": str(target),
        "skills": len(manifest["skills"]),
        "writtenAt": manifest["meta"]["writtenAt"],
        "counts": manifest["stats"]["tierCounts"],
        "elapsedMs": int(round((time.time() - started) * 1000)),
        "stamp": stamp(target),
    }


def write_manifest(path=None, **kwargs):
    """构建并原子写入清单（tmp + os.replace）。"""
    started = time.time()
    target = Path(path) if path is not None else default_manifest_path()
    return _atomic_write(target, build_manifest(**kwargs), started)


def ensure_manifest(runtime=None, ttl=86400, force=False, market_dir=None, extensions_root=None):
    """有清单且未过期就复用，否则重建；任何异常都降级为 ok:False，绝不抛、绝不删旧清单。"""
    target = Path(skill_tools.data_dir(runtime)) / MANIFEST_FILE
    time_started = time.time()
    try:
        age = None
        try:
            age = max(0, int(time.time() - target.stat().st_mtime))
        except OSError:
            age = None
        if not force and age is not None and age <= ttl and target.stat().st_size > 2:
            data = {}
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            skills = data.get("skills") or []
            if skills:
                return {
                    "ok": True,
                    "reused": True,
                    "rebuilt": False,
                    "path": str(target),
                    "skills": len(skills),
                    "ageSeconds": age,
                    "stamp": stamp(target),
                    "counts": (data.get("stats") or {}).get("tierCounts"),
                }
        if extensions_root is None:
            try:
                extensions_root = skill_tools.extensions_dir(runtime)
            except Exception:
                extensions_root = None
        if market_dir is None:
            try:
                market_dir = skill_tools.market_dir(runtime=runtime)
            except Exception:
                market_dir = None
        manifest = build_manifest(market_dir=market_dir, extensions_root=extensions_root)
        if not manifest.get("skills"):
            # 扫不到任何 skill 时绝不覆盖旧清单：宁可 continue 用旧的，也不写空清单
            return {
                "ok": False,
                "reused": False,
                "rebuilt": False,
                "path": str(target),
                "error": {"type": "EmptyManifest",
                          "message": "市场目录与已安装目录里都没有可扫描的 skill，保留旧清单"},
                "keptOld": bool(target.exists()),
            }
        result = _atomic_write(target, manifest, started=time_started)
        result.update({"reused": False, "rebuilt": True})
        return result
    except Exception as exc:
        return {
            "ok": False,
            "reused": False,
            "rebuilt": False,
            "path": str(target),
            "error": {"type": type(exc).__name__, "message": str(exc)[:400]},
            "keptOld": bool(target.exists()),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 skill 运行时清单")
    parser.add_argument("--market", default=None, help="skill 市场 zip 目录")
    parser.add_argument("--out", default=None, help="清单输出路径（默认 <data>/qq-only/skill-tools.json）")
    parser.add_argument("--extensions", default=None, help="已安装 skill 目录（默认取运行时目录）")
    parser.add_argument("--force", action="store_true", help="通过 ensure_manifest 强制重扫")
    parser.add_argument("--ensure", action="store_true", help="走 ensure_manifest（带 ttl 复用）而非直接重构")
    args = parser.parse_args(argv)
    if args.ensure:
        result = ensure_manifest(force=args.force)
    else:
        try:
            result = write_manifest(args.out, market_dir=args.market, extensions_root=args.extensions)
        except Exception as exc:
            result = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)[:400]}}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
