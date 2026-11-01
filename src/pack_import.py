"""pack_import.py - 统一导入器的「识别 + 预检」内核（W11-1）。

现存导入面是 4 个互不相通的家族（Python 插件 / QQAgent JS 扩展 / 技能 / 功能包），
清单口径只有文件名 plugin.json 相同。错投的代价不是文案问题：实测 512 个 JS 包喂
Python 安装器 → inspect_ok=0、509 个直接抛 InstallError；50 个 Python 市场包喂 JS
宿主 → OK 0 / FAIL 50。所以入口要先**按包内容**判家族。

本模块只做两件事：

* detect()  —— 按包内容判家族（不按 id、不按来源目录），下钻复用 host-one.mjs 的
  extensionRoot() 语义（最多 4 层、唯一带清单的子目录）；
* inspect() —— 统一 schema 预检，所有分支同形，永不抛异常。

本卡（W11-1）不实现 install()。detect/inspect 全程只读：不写 data/、不写 src/plugins/、
不装任何东西。两个字段是显式占位（诚实说明）：

* tools 是**静态**解析结果（tool_schema.extract_tools，与 skill_manifest 同口径），
  真注册结果由安装阶段的 qqagent_host.import_probe 给出；
* load 在非技能家族恒为 {ok: False, code: "not-loaded"} —— 本卡不做真加载。
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:  # 包内导入
    from . import skill_manifest, skill_tools, tool_schema
except ImportError:  # 直接运行 / 测试以仓库根为 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import skill_manifest, skill_tools, tool_schema

# ── 家族与清单 ────────────────────────────────────────────────────────────────
# 第五种家族 mcp-server：常量单列一份（detect/inspect/install 与 _BRANCHES 都用它），
# 同时必须出现在 KINDS 里 —— KINDS 是「认得的所有家族」的唯一清单，
# tests/test_pack_import.py 的 test_kinds_constant_matches_detected_kinds 逐项核对两者一致。
MCP_KIND = "mcp-server"
# 第六种家族 mod-py：目录 mod（plugin.json + 入口 .py + 包内其余文件整目录落地）。
# 常量同样单列一份（detect/inspect/install 与 _BRANCHES 都用它），也必须出现在 KINDS 里。
MOD_KIND = "mod-py"
KINDS = ("plugin-py", "extension-js", "skill", "feature-pack", MCP_KIND, MOD_KIND, "unknown")
MCP_MANIFEST = "mcp.json"
# 目录 mod 的清单：与 src/plugins/manager.py:28 MOD_MANIFEST_NAME 是同一份文件。
MOD_MANIFEST = "plugin.json"
# 落盘暂存目录前缀：与 _Aside 的 .old-* 一样留在同一个自定义插件目录里（同盘才能 rename）。
MOD_INSTALL_PREFIX = ".install-"
# 目录 mod 的 id 会**逐字**当落盘目录名（manager.py:794-796 的 id_mismatch 核的就是
# 「id == 目录名」），所以先按目录安全筛一遍：只允许字母/数字/下划线/点/横线，首字符是
# 字母或数字，最长 64。中文、空格、斜杠一律拒收，绝不用 normalize_plugin_id 改写
# （改写过的 id 与目录名对不上，装上也是挂不起来的死目录）。
MOD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MOD_ID_RESERVED = frozenset(["CON", "PRN", "AUX", "NUL"]
                            + ["COM%d" % n for n in range(1, 10)]
                            + ["LPT%d" % n for n in range(1, 10)])
MOD_HINT = ("按 src/plugins/manager.py 的 plugin.json 契约改：id（= 目录名）、"
            "api_version=1、entry（入口 .py 的相对路径）三项必须有")
PACK_MANIFEST = "pack.json"
# 与 runtime/qqagent-host/host-one.mjs 的 MANIFEST_NAMES 同序：skill.json 优先。
MANIFEST_NAMES = ("skill.json", "plugin.json")
ALL_MANIFESTS = frozenset((PACK_MANIFEST,) + MANIFEST_NAMES)
DRILL_LIMIT = 4                      # host-one extensionRoot(): depth >= 4 就停
SKIP_DIRS = frozenset(("__MACOSX",))
# 宿主真会因 capabilities/requires 形状拒收的家族，只有走 host-one 读清单的那两种：
# host-one.mjs:69-72 取包根清单（skill.json 优先），:84-91 的 names() 对两个字段做
# 「Array.isArray + 不超 128 项 + 每项是非空字符串」检查，不符 *抛* manifest-invalid。
# plugin-py 走 src/plugin_installer.py 的 plugin.json（全文无 capabilities/requires 读取）、
# feature-pack 走 src/feature_pack.py 的 pack.json（:74-88 只查存在/大小/JSON/对象）、
# mcp-server 的 mcp.json 由独立进程协议消费（src/skill_tools.py:1232），三者都不校验该字段，
# 加闸只会误拒合法包。
CAPABILITY_GATED_KINDS = ("extension-js", "skill")

FEATURE_FORMAT = "whale-feature-pack"
# MCP server 落点与登记表（<data_dir>/qq-only/…）
QQ_ONLY = "qq-only"
MCP_DIR = "mcp_servers"                 # <data_dir>/qq-only/mcp_servers/<id>/
MCP_INDEX = "mcp_servers.json"          # <data_dir>/qq-only/mcp_servers.json
MCP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MCP_UNSUPPORTED = "目前只支持 stdio（command/args），HTTP/SSE 型 MCP server 暂不支持"
# 与 src/qqagent_host.py:66-68 同一口径的解包上限：MCP 包沿用这套安全检查。
MCP_MAX_BYTES = 24 * 1024 * 1024
MCP_MAX_FILES = 4000
PY_PLUGIN_FORMAT = "whale-plugin"
PY_HOST = "WhaleGirlQQ"
JS_EXTS = (".js", ".mjs")
PY_EXTS = (".py",)
CONSOLE_UI = "ui/index.html"

MAX_READ_BYTES = int(getattr(skill_tools, "MAX_SCAN_BYTES", 512 * 1024))
_CJS_MARK = "module.exports"
_ESM_RE = re.compile(r"(?:^|[\n;{])\s*(?:import|export)\b")


# ── 只读包视图：目录 / zip 文件 / 内存 zip / 单文件 ───────────────────────────


class _BytesReader(skill_tools._ZipReader):
    """把内存里的 zip 字节当只读包看待：复用 _ZipReader 的 names/read/entry。"""

    def __init__(self, data: bytes):
        self._raw = data
        self._archive = None

    def _open(self) -> zipfile.ZipFile:
        if self._archive is None:
            self._archive = zipfile.ZipFile(io.BytesIO(self._raw))
        return self._archive

    def exists(self) -> bool:
        return bool(self._raw)


class _FileReader:
    """把单个 .py / .js 文件当只读包看待（与安装器的 LOCAL_SUFFIXES 口径一致）。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def names(self) -> List[str]:
        return [self.path.name]

    def entry(self) -> str:
        return self.path.name

    def read(self, name: str) -> Optional[str]:
        if name != self.path.name:
            return None
        try:
            if not self.path.is_file() or self.path.stat().st_size > MAX_READ_BYTES:
                return None
            return self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def close(self) -> None:
        return None


def _close(reader) -> None:
    closer = getattr(reader, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _problem(code: str, message: str, hint: str = "") -> dict:
    return {"code": code, "message": message, "hint": hint}


def _notice(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _open_pack(path_or_bytes, filename: str = "") -> Tuple[object, str, Optional[dict]]:
    """打开包。返回 (reader, origin, problem)；problem 非空表示打不开（不抛异常）。"""
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        data = bytes(path_or_bytes)
        origin = filename or "<bytes:%d>" % len(data)
        if not data:
            return None, origin, _problem("zip-invalid", "内存包是空的（0 字节）", "重新导出这个包")
        reader = _BytesReader(data)
        if not reader.names():
            _close(reader)
            return None, origin, _problem("zip-invalid", "内存里的 zip 读不出成员清单（损坏）", "重新导出这个包")
        return reader, origin, None
    try:
        target = Path(path_or_bytes)
    except (TypeError, ValueError):
        return None, str(path_or_bytes), _problem("path-invalid", "看不懂这个路径参数", "传目录、zip 或文件路径")
    if not target.exists():
        return None, str(target), _problem("path-missing", "路径不存在：%s" % target, "确认包是否被移动或删除")
    if target.is_dir():
        return skill_tools._DirReader(target), str(target), None
    if zipfile.is_zipfile(target):
        return skill_tools._ZipReader(target), str(target), None
    if target.suffix.lower() == ".zip":
        return None, str(target), _problem("zip-invalid", "压缩包损坏或不是 zip：%s" % target.name, "重新下载这个包")
    return _FileReader(target), str(target), None


# ── 下钻（对齐 host-one 的 extensionRoot()）────────────────────────────────────


def _visible(name: str) -> bool:
    return bool(name) and not name.startswith(".") and name not in SKIP_DIRS


def _find_root(names: List[str]) -> str:
    """返回包根（相对前缀）。逐条对齐 host-one.mjs:60-68 的 extensionRoot()。"""
    current = ""
    for depth in range(DRILL_LIMIT + 1):
        prefix = current + "/" if current else ""
        here = [name[len(prefix):] for name in names if name.startswith(prefix)]
        if any(inner in ALL_MANIFESTS for inner in here):
            return current
        if depth >= DRILL_LIMIT:
            return current
        subs = sorted({inner.split("/")[0] for inner in here if "/" in inner and _visible(inner.split("/")[0])})
        if len(subs) == 1:
            current = (current + "/" + subs[0]) if current else subs[0]
            continue
        matching = [sub for sub in subs if _has_manifest(here, sub)]
        if len(matching) == 1:
            current = (current + "/" + matching[0]) if current else matching[0]
            continue
        return current
    return current


def _has_manifest(here: List[str], sub: str) -> bool:
    """子目录 sub 的直接子文件里是否有清单（= host-one 的 hasManifest(dir)）。"""
    inner_prefix = sub + "/"
    return any(
        name[len(inner_prefix):] in ALL_MANIFESTS
        for name in here
        if name.startswith(inner_prefix)
    )


def _root_names(names: List[str], root: str) -> List[str]:
    """包根这一层的文件名（不带目录前缀）。"""
    prefix = root + "/" if root else ""
    return [name[len(prefix):] for name in names
            if name.startswith(prefix) and "/" not in name[len(prefix):]]


def _root_manifests(at_root: List[str]) -> Dict[str, str]:
    """包根清单：规范名 → 实际文件名（规范名缺失时才按大小写不敏感匹配）。

    为什么要有这条：Windows / macOS 导出的包会把 plugin.json 写成 Plugin.JSON、skill.json
    写成 SKILL.JSON。_classify 里所有 "xxx in at_root" 的精确比较都会漏掉它们，包于是退回
    「无清单」按单文件猜（或直接 unknown），清单里的 id / name / version 全丢。
    规范名存在时行为一字不变，只在缺失时才退到大小写不敏感匹配。
    """
    lowered: Dict[str, str] = {}
    for name in at_root:
        low = name.lower()
        if low not in lowered:
            lowered[low] = name
    found: Dict[str, str] = {}
    for canonical in (PACK_MANIFEST,) + MANIFEST_NAMES:
        if canonical in at_root:
            found[canonical] = canonical
        elif canonical in lowered:
            found[canonical] = lowered[canonical]
    return found


def _read_manifest(read: Callable[[str], Optional[str]], name: str) -> Tuple[dict, Optional[str]]:
    text = read(name)
    if not text:
        return {}, "清单读不出来（缺失或超过 %dKB）" % (MAX_READ_BYTES // 1024)
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except ValueError as exc:
        return {}, "清单不是合法 JSON：%s" % exc
    if not isinstance(data, dict):
        return {}, "清单必须是 JSON 对象"
    return data, None


def _result(kind, manifest_name, manifest, dialect, evidence, confidence, notices=None) -> dict:
    return {
        "kind": kind,
        "manifest_name": manifest_name,
        "manifest": manifest if isinstance(manifest, dict) else {},
        "dialect": dialect,
        "evidence": evidence,
        "confidence": confidence,
        "notices": notices or [],
    }


def _with_manifest_error(found: dict, error) -> dict:
    """把「清单读不出来」的原文挂在 found 上，分类结论一个字不动。

    照 _mcp_result 往 found 里塞 'mcp' 的先例：detect() 只取 6 个键，多这个键对它零影响；
    inspect() 见到它就按 manifest-broken 拒收，不再顺着读不出来的 plugin.json 猜家族。
    """
    if error:
        found["manifest_error"] = str(error)
    return found


def _with_mod_shape(found: dict) -> dict:
    """缺 api_version、但其余形状是目录 mod：记一笔给 inspect() 用。"""
    found["mod_shape_without_api_version"] = True
    return found


def _broken_manifest_problem(error, name: str = "plugin.json") -> dict:
    """包根清单读不出对象：把 _read_manifest 的病历译成「这个文件」说清楚。

    name 取 found["manifest_name"]：skill.json 坏了要按 skill.json 说，不能一律叫 plugin.json。
    """
    detail = str(error or "")
    if detail.startswith("清单必须是 JSON 对象"):
        text = "%s 顶层必须是对象（现在是数组或标量，读不出 id / entry）" % name
    elif detail.startswith("清单"):
        text = name + " " + detail[len("清单"):]
    else:
        text = "%s 读不成清单：%s" % (name, detail or "内容为空或超过大小上限")
    if name == "skill.json":
        hint = "skill.json 必须是 UTF-8 的 JSON 对象：技能要有 id / name / instructions / triggers"
    else:
        hint = ("plugin.json 必须是 UTF-8 的 JSON 对象：目录 mod 要有 id / api_version / entry，"
                ".py 插件要有 name / version / entry")
    return _problem("manifest-broken", text, hint)


def _missing_api_version_problem(manifest: dict) -> dict:
    """id + entry 都在、只是漏了 api_version：按 mod 契约点名病因，不按 .py 插件兜底。"""
    return _problem(
        "missing_api_version",
        "plugin.json 的 id=%r、entry=%r 都在，但缺 api_version=1：这是目录 mod 的清单"
        % (_text(manifest.get("id")), _text(manifest.get("entry"))),
        MOD_HINT)


def _mod_shape_without_api_version(manifest: dict, here) -> bool:
    """没写 api_version，可其余形状是目录 mod 的清单吗？（F2 第三类）

    判据刻意收窄：id 能当目录名 + entry 指向包内 .py + 「name 与 version 都缺」或带 depends。
    取证依据（941 个包 / 566 个带包根 plugin.json）：同时命中这条的包为 0，对既有语料零位移。
    反过来按 permissions / enabled_by_default 判会误伤真 .py 插件
    （插件市场/语音控制台-voice_console-2.6.0.zip 就带 permissions，同形状的 8 个里 1 个）。
    name + version 齐全的那种缺 api_version 清单与真 .py 插件在字段上不可区分，不在这里猜。
    """
    if "api_version" in manifest or "apiVersion" in manifest:
        return False
    ident = _text(manifest.get("id"))
    if not ident or _mod_id_problem(ident):
        return False
    entry = str(manifest.get("entry") or "").strip().lstrip("/")
    if not entry.lower().endswith(PY_EXTS) or entry not in here:
        return False
    thin = not _text(manifest.get("name")) and not _text(manifest.get("version"))
    return bool(thin or manifest.get("depends"))


# ── 决策表（命中即停）────────────────────────────────────────────────────────


def _classify(root: str, names: List[str], read: Callable[[str], Optional[str]]) -> dict:
    """按决策表判家族。返回内部结构，detect() 取 6 个键，inspect() 另取 notices。"""
    prefix = root + "/" if root else ""
    here = [name[len(prefix):] for name in names if name.startswith(prefix)]
    at_root = [name for name in here if "/" not in name]
    evidence = ["包根 = %s" % (root or "(包顶层)")]
    roots = _root_manifests(at_root)

    # 0 多包：包顶层两个以上目录各自带清单 = 一个 zip 塞了多个包，判不了以谁为准。
    #   修复前这种包落到 unknown，problems 说的却是「没有 pack.json / skill.json / plugin.json」
    #   ——清单明明在，提示是假话（反例：p1/plugin.json+p1/index.js 与 p2/... 各一份）。
    subs = sorted({name.split("/")[0] for name in here
                   if "/" in name and _visible(name.split("/")[0])})
    packaged = [sub for sub in subs if _has_manifest(here, sub)]
    if not roots and len(packaged) > 1:
        found = _result("unknown", "", {}, "multiple-packages",
                        evidence + ["顶层 %d 个目录各自带清单：%s"
                                    % (len(packaged), "、".join(packaged))], 0.0)
        found["multiple_packages"] = packaged
        return found

    # 1 功能包：pack.json + format=whale-feature-pack + version=1
    if PACK_MANIFEST in roots:
        manifest, error = _read_manifest(read, prefix + roots[PACK_MANIFEST])
        version = manifest.get("version")
        packed = version == 1 and not isinstance(version, bool)
        if error is None and manifest.get("format") == FEATURE_FORMAT and packed:
            return _result("feature-pack", PACK_MANIFEST, manifest, "whale-feature-pack-v1",
                           evidence + ["pack.json: format=whale-feature-pack, version=1"], 0.98)
        evidence.append("pack.json 存在但不是 whale-feature-pack v1（%s）" % (error or "format/version 不符"))

    # 2b MCP server：包根或下钻 ≤4 层有 mcp.json，且能读出 server 定义。
    #     包根已摆着 pack.json / skill.json / plugin.json 时不抢，既有四家族一行不动。
    if not roots:
        mcp_name = _find_mcp(names)
        if mcp_name:
            manifest, error = _read_manifest(read, mcp_name)
            server_id, server = _mcp_server(manifest)
            if server is not None:
                proof = evidence + ["mcp.json 位于 %s" % mcp_name]
                if server_id:
                    proof.append("mcpServers 里取 %s" % server_id)
                else:
                    proof.append("裸 server 定义（顶层就是 command/args）")
                return _mcp_result(_mcp_root(names), manifest, server_id, server, proof)
            evidence.append("mcp.json 存在但没有可用的 server 定义（%s）"
                            % (error or "无 mcpServers / command / url"))

    # 2 技能：包根有 skill.json（下钻已在上层做完）
    if "skill.json" in roots:
        skill_name = roots["skill.json"]
        manifest, error = _read_manifest(read, prefix + skill_name)
        proof = evidence + ["skill.json 位于包根"]
        if "plugin.json" in roots:
            proof.append("同目录还有 plugin.json，按 skill 优先（与宿主 hasManifest 同序）")
        if error:
            proof.append(error)
        # skill.json 读不出来就不再咬定 skill。修复前这里照样返回 skill 0.99：inspect 说 ok=True
        # （还带一条 skill-tier 分层结论），真装才在 skill_market 里报 bad-manifest——预检与安装
        # 两套结论打架。反例：skill.json='{"id": "x", ' 加一份合法 plugin.json(apiVersion/entry=index.js)
        # 的包，inspect ok=True、install ok=False。现在与 plugin.json 走同一条 manifest-broken 闸门。
        return _with_manifest_error(
            _result("skill", skill_name, manifest, "skill-json", proof, 0.99), error)

    # 3 / 4 Python 插件与 JS 扩展共用 plugin.json，按方言字段分流
    if "plugin.json" in roots:
        plugin_name = roots["plugin.json"]
        manifest, error = _read_manifest(read, prefix + plugin_name)
        entry = str(manifest.get("entry") or "").strip()
        py_files = [name for name in at_root if name.lower().endswith(PY_EXTS)]
        proof = evidence + ["plugin.json 位于包根"]
        if error:
            proof.append(error)
        # 目录 mod 的方言字段是 api_version（snake_case），与 JS 扩展的 apiVersion（camel）
        # 是两个字段、两个家族。先判：带 api_version 的 plugin.json 一律按 mod 清单走，
        # 交给 manager._validate_mod_manifest 定去留（api_version 类型不对也在那里报
        # bad_api_version，不在判家族这一步猜）。
        if "api_version" in manifest:
            return _with_manifest_error(
                _result(MOD_KIND, "plugin.json", manifest, "mod-json",
                        proof + _mod_proof(manifest, entry), 0.96), error)
        # entry 指向包里真实存在的 .py：入口语言说了算，apiVersion 不能把一个 Python 插件变成
        # JS 扩展。反例（修复前实测）：plugin.json={apiVersion:1, entry:"main.py"} + main.py 被判
        # extension-js 0.97，然后当 JS 扩展落进 <data>/qq-only/qqagent_host/plugins/<id>，
        # 入口却是 .py——装完就是一个跑不起来的目录。
        if (entry.lower().endswith(PY_EXTS) and (prefix + entry) in names
                and not _mod_shape_without_api_version(manifest, here)):
            return _with_manifest_error(
                _result("plugin-py", plugin_name, manifest, _py_dialect(manifest, entry),
                        proof + _py_proof(manifest, entry, py_files)
                        + ["entry=%s 是包内真文件，按 Python 插件判" % entry], 0.96), error)
        if "apiVersion" in manifest or entry.lower().endswith(JS_EXTS) or "index.js" in at_root:
            return _with_manifest_error(
                _result("extension-js", plugin_name, manifest, "qqagent-0.4",
                        proof + _js_proof(manifest, entry, at_root), 0.97), error)
        # 第三类：清单读得出来、id 与 entry 都在，就是没写 api_version。判据见
        # _mod_shape_without_api_version 的取证说明；命中就在 inspect() 里报 missing_api_version，
        # 不再冒充 .py 插件往下走（那样只会报出 bad-id /「只支持 .py 或 .zip」这类错案由）。
        if _mod_shape_without_api_version(manifest, here):
            return _with_mod_shape(
                _result("plugin-py", plugin_name, manifest, _py_dialect(manifest, entry),
                        proof + _py_proof(manifest, entry, py_files)
                        + ["id 与 entry(.py) 都在、entry 文件也在包里，但清单没有 api_version"],
                        0.6))
        if manifest.get("format") == PY_PLUGIN_FORMAT or manifest.get("host") == PY_HOST \
                or entry.lower().endswith(PY_EXTS) or len(py_files) == 1:
            return _with_manifest_error(
                _result("plugin-py", plugin_name, manifest, _py_dialect(manifest, entry),
                        proof + _py_proof(manifest, entry, py_files), 0.95), error)
        return _with_manifest_error(
            _result("unknown", plugin_name, manifest, "plugin-json-unclassified",
                    proof + ["plugin.json 既没有 JS 入口线索，也没有 Python 入口线索"], 0.2),
            error)

    # 5 / 6 无清单兜底：包根唯一 .py 含 PluginBase / 包根唯一 .js 导出 setup|register
    # 只认包根（at_root）：下钻到 4 层上限停下的深层文件不算「包内唯一入口」。
    py_files = [name for name in at_root if name.lower().endswith(PY_EXTS)]
    js_files = [name for name in at_root if name.lower().endswith(JS_EXTS)]
    if len(py_files) == 1:
        text = read(prefix + py_files[0]) or ""
        if "PluginBase" in text:
            return _result("plugin-py", "", {}, "py-single-file",
                           evidence + ["无任何清单", "唯一 .py：%s" % py_files[0], "源码含 PluginBase"],
                           0.72, notices=[_notice("no-manifest", "包内没有 plugin.json，按单文件插件处理")])
    if len(js_files) == 1:
        text = read(prefix + js_files[0]) or ""
        if re.search(r"\b(?:setup|register)\b", text):
            return _result("extension-js", "", {}, "js-no-manifest",
                           evidence + ["无任何清单", "唯一 .js：%s" % js_files[0], "入口导出 setup/register"],
                           0.70, notices=[_notice("no-manifest", "包内没有 plugin.json，按单文件扩展处理")])

    # 7 其它
    return _result("unknown", "", {}, "",
                   evidence + ["没有 pack.json / skill.json / plugin.json，也没有可识别的单文件入口"], 0.0)


def _js_proof(manifest: dict, entry: str, at_root: List[str]) -> List[str]:
    proof = []
    if "apiVersion" in manifest:
        proof.append("plugin.json 含 apiVersion=%r" % (manifest.get("apiVersion"),))
    if entry and entry.lower().endswith(JS_EXTS):
        proof.append("entry=%s 是 JS 入口" % entry)
    if "index.js" in at_root:
        proof.append("同目录有 index.js")
    return proof


def _py_proof(manifest: dict, entry: str, py_files: List[str]) -> List[str]:
    proof = []
    if manifest.get("format") == PY_PLUGIN_FORMAT:
        proof.append("plugin.json: format=whale-plugin")
    if manifest.get("host") == PY_HOST:
        proof.append("plugin.json: host=WhaleGirlQQ")
    if entry and entry.lower().endswith(PY_EXTS):
        proof.append("entry=%s 是 Python 入口" % entry)
    if len(py_files) == 1:
        proof.append("同目录唯一 .py：%s" % py_files[0])
    return proof


def _mod_proof(manifest: dict, entry: str) -> List[str]:
    """判成 mod-py 的证据链：只写看到的事实，不在这里判合法性（那是 manager 的活）。"""
    proof = ["plugin.json 含 api_version=%r（目录 mod 方言）" % (manifest.get("api_version"),)]
    if _text(manifest.get("id")):
        proof.append("id=%s（目录 mod 的落盘目录名就是它）" % _text(manifest.get("id")))
    if entry:
        proof.append("entry=%s" % entry)
    return proof


def _py_dialect(manifest: dict, entry: str) -> str:
    if manifest.get("format") == PY_PLUGIN_FORMAT:
        return "whale-plugin-v1"
    if manifest.get("host") == PY_HOST:
        return "whalegirlqq-host"
    if entry.lower().endswith(PY_EXTS):
        return "py-entry"
    return "py-single-file"


# ── MCP server 家族（第五种）──────────────────────────────────────────────────


def _find_mcp(names: List[str]) -> str:
    """在包根与下钻 ≤4 层的可见目录里找 mcp.json，取最浅的一个（同深度按名字排序）。"""
    hits = []
    for name in names:
        parts = name.split("/")
        if parts[-1] != MCP_MANIFEST or len(parts) > DRILL_LIMIT + 1:
            continue
        if not all(_visible(part) for part in parts[:-1]):
            continue
        hits.append((len(parts), name))
    hits.sort()
    return hits[0][1] if hits else ""


def _mcp_root(names: List[str]) -> str:
    """MCP 包的包根 = 选中那个 mcp.json 所在目录。
    比 _find_root 更准：包根只有一个子目录时 _find_root 会继续往下钻（wrap/bin），
    而 mcp.json 明明在上一层（wrap/）。"""
    name = _find_mcp(names)
    return name.rsplit("/", 1)[0] if "/" in name else ""


def _mcp_server(manifest: dict) -> Tuple[str, Optional[dict]]:
    """取出要导入的一个 server：(id, 定义)。只认两种形状：
    {"mcpServers": {"<id>": {"command"/"url": ...}}} 与裸 {"command"/"url": ...}。"""
    servers = manifest.get("mcpServers")
    if isinstance(servers, dict):
        for key in sorted(servers):
            value = servers[key]
            if isinstance(value, dict) and (_text(value.get("command"))
                                            or _text(value.get("url"))):
                return str(key), value
        return "", None
    if _text(manifest.get("command")) or _text(manifest.get("url")):
        return "", manifest
    return "", None


def _mcp_result(root: str, manifest: dict, server_id: str, server: dict,
                evidence: List[str]) -> dict:
    """detect() 只取 6 个键；mcp 键留给 inspect() 的 _fill_mcp_server 用。"""
    found = _result(MCP_KIND, MCP_MANIFEST, manifest, "mcp-json", evidence, 0.96)
    found["mcp"] = {"root": root, "server_id": server_id, "server": server}
    return found


def _pack_stem(reader) -> str:
    """清单没写 id 时用包名兜底：zip 取文件名，目录 / 单文件取名字。"""
    for attr in ("path", "root"):
        value = getattr(reader, attr, None)
        if not value:
            continue
        target = Path(str(value))
        return target.stem if target.suffix.lower() == ".zip" else target.name
    return ""


def _mcp_id(server_id: str, manifest: dict, reader) -> str:
    return server_id or _text(manifest.get("id")) or _pack_stem(reader)


def _absolute_command(command: str) -> Tuple[bool, bool]:
    """(能不能用, 是不是裸可执行名)。带路径分隔符却不是绝对路径 → 不能用。"""
    text = command.strip().strip('"').strip("'")
    if not text:
        return True, False
    if "/" not in text and "\\" not in text:
        return True, True
    try:
        return Path(text).is_absolute(), False
    except (OSError, ValueError):
        return False, False


# ── detect() ──────────────────────────────────────────────────────────────────


def _detect_unknown(reason: str, confidence: float = 0.0) -> dict:
    return {
        "kind": "unknown",
        "root": "",
        "manifest_name": "",
        "manifest": {},
        "dialect": "",
        "evidence": [reason],
        "confidence": confidence,
    }


def _detect_reader(reader, origin: str) -> dict:
    names = [name for name in (reader.names() or []) if name]
    if not names:
        return _detect_unknown("包里没有任何文件（空包或损坏）：%s" % origin)
    root = _find_root(names)
    found = _classify(root, names, reader.read)
    # MCP 家族的包根是 mcp.json 所在目录（见 _mcp_root），别的家族没有 mcp 键、原样不动。
    root = str(((found.get("mcp") or {}).get("root")) or root)
    return {
        "kind": found["kind"],
        "root": root,
        "manifest_name": found["manifest_name"],
        "manifest": found["manifest"],
        "dialect": found["dialect"],
        "evidence": found["evidence"],
        "confidence": found["confidence"],
    }


def detect(path_or_bytes, filename: str = "") -> dict:
    """按包内容判家族。返回 {kind, root, manifest_name, manifest, dialect, evidence, confidence}。

    纯只读、永不抛异常：打不开的包返回 kind="unknown"，原因写在 evidence 里。
    """
    reader = None
    try:
        reader, origin, problem = _open_pack(path_or_bytes, filename)
        if reader is None:
            return _detect_unknown("%s：%s" % (problem["code"], problem["message"]))
        return _detect_reader(reader, origin)
    except Exception as exc:  # 契约：永不抛
        return _detect_unknown("预检异常：%s: %s" % (type(exc).__name__, exc))
    finally:
        _close(reader)


# ── 静态工具清单（只读；zip 安全解到系统临时目录，与 skill_manifest 同口径）────


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """只解安全成员：跳过绝对路径、盘符与 .. 穿越。"""
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if not parts or name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
            continue
        target = dest.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        with archive.open(info) as source, target.open("wb") as sink:
            sink.write(source.read())


def _extract_tools(root: Path) -> Dict[str, dict]:
    try:
        with skill_manifest._tool_budget():
            return tool_schema.extract_tools(skill_manifest._package_dir(root)) or {}
    except Exception:
        return {}


def _tools_for(path) -> Dict[str, dict]:
    """静态解析工具定义。目录直接读；zip 安全解到系统临时目录后读；其它形态返回空。"""
    try:
        target = Path(path)
        if target.is_dir():
            return _extract_tools(target)
        if not zipfile.is_zipfile(target):
            return {}
    except (TypeError, ValueError, OSError):
        return {}
    try:
        with tempfile.TemporaryDirectory(prefix="pack-import-") as tmp:
            with zipfile.ZipFile(target) as archive:
                _safe_extract(archive, Path(tmp))
            return _extract_tools(Path(tmp))
    except Exception:
        return {}


def _pack_size(reader, names: List[str]) -> Tuple[int, int]:
    """文件数 + 总字节数：zip 读中央目录、目录走 stat，都不解压。"""
    opener = getattr(reader, "_open", None)
    if callable(opener):
        try:
            infos = [info for info in opener().infolist() if not info.is_dir()]
            return len(infos), sum(int(info.file_size) for info in infos)
        except Exception:
            pass
    root = getattr(reader, "root", None)
    if root is not None:
        count = total = 0
        try:
            for item in Path(root).rglob("*"):
                if item.is_file():
                    count += 1
                    total += item.stat().st_size
            return count, total
        except OSError:
            return len(names), 0
    path = getattr(reader, "path", None)
    if path is not None:
        try:
            return 1, Path(path).stat().st_size
        except OSError:
            return len(names), 0
    return len(names), 0


# ── inspect() ─────────────────────────────────────────────────────────────────


def _new_result() -> dict:
    """统一 schema：所有分支同形，GUI 只渲染一套。"""
    return {
        "ok": False,
        "kind": "unknown",
        "id": "",
        "name": "",
        "version": "",
        "entry": "",
        "files": 0,
        "bytes": 0,
        "landed": "",
        "target_dir": "",
        "tools": [],
        "hooks": [],
        "providers": [],
        "problems": [],
        "notices": [],
        "load": {"ok": False, "error": "", "code": ""},
        "installed_but_disabled": False,
        "actions": [],
        "duration_ms": 0,
    }


def _finish(result: dict, started: float) -> dict:
    result["ok"] = not result["problems"]
    result["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_str_list(raw) -> List[str]:
    if isinstance(raw, dict):
        return [str(key) for key in raw]
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str) and item.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _fallback_id(path, found: dict) -> str:
    """清单没给 id 时用包名兜底（zip 名 / 目录名）。"""
    if found["manifest_name"]:
        return ""
    try:
        target = Path(path)
    except (TypeError, ValueError):
        return ""
    return target.stem if target.suffix else target.name


def _entry_name(found: dict, root: str, names: List[str], manifest: dict) -> str:
    declared = _text(manifest.get("entry"))
    prefix = root + "/" if root else ""
    at_root = [name[len(prefix):] for name in names
               if name.startswith(prefix) and "/" not in name[len(prefix):]]
    if declared and ((prefix + declared) in names or declared in at_root):
        return declared
    kind = found["kind"]
    if kind == "extension-js":
        for candidate in ("index.js", "index.mjs"):
            if candidate in at_root:
                return candidate
        js_files = [name for name in at_root if name.lower().endswith(JS_EXTS)]
        return js_files[0] if len(js_files) == 1 else ""
    if kind == "plugin-py":
        py_files = [name for name in at_root if name.lower().endswith(PY_EXTS)]
        return py_files[0] if len(py_files) == 1 else ""
    return ""


def _version_text(manifest: dict) -> str:
    version = manifest.get("version")
    if isinstance(version, bool):
        return str(version)
    if isinstance(version, int):
        # B1 方言：整数是格式版本，真实插件版本在 app_version。
        return str(manifest.get("app_version") or version)
    return _text(version)


def inspect(path, *, plugins_dir=None, skills_runtime=None, data_dir=None,
            custom_plugins_dir=None) -> dict:
    """统一预检。返回统一 schema；永不抛异常，所有失败进 problems。

    plugins_dir / skills_runtime / data_dir / custom_plugins_dir 只用来**计算计划落点**
    （target_dir），不会在落点下创建目录、不会在落点下写任何文件（data_dir 只给 mcp-server
    算 <data_dir>/qq-only/mcp_servers/<id>，custom_plugins_dir 只给目录 mod 算
    <custom_plugins_dir>/<id>/）。目录 mod 要跑 manager 的清单校验与逐文件禁用法扫描，
    那需要一个真目录，所以内部解到系统临时目录，跑完即删。
    """
    started = time.perf_counter()
    result = _new_result()
    reader = None
    try:
        reader, origin, problem = _open_pack(path, "")
        if reader is None:
            result["problems"].append(problem)
            return _finish(result, started)
        names = [name for name in (reader.names() or []) if name]
        if not names:
            result["problems"].append(_problem(
                "zip-invalid" if _looks_like_zip(path) else "empty-pack",
                "包里没有任何文件：%s" % origin, "重新导出这个包"))
            return _finish(result, started)

        root = _find_root(names)
        found = _classify(root, names, reader.read)
        result["kind"] = found["kind"]
        result["notices"].extend(found["notices"])
        manifest = found["manifest"]
        result["id"] = _text(manifest.get("id")) or _fallback_id(path, found)
        result["name"] = _text(manifest.get("name")) or result["id"]
        result["version"] = _version_text(manifest)
        result["entry"] = _entry_name(found, root, names, manifest)
        result["files"], result["bytes"] = _pack_size(reader, names)

        # 清单文件名大小写变体（Plugin.JSON / SKILL.JSON）：按清单认了出来，就得在预检里说明，
        # 否则用户看到的是「按单文件插件处理」而清单里的 id / name / version 悄悄丢了。
        renamed = [name for name in _root_manifests(_root_names(names, root)).items()
                   if name[0] != name[1]]
        if renamed:
            result["notices"].append(_notice(
                "manifest-name-case",
                "包根清单文件名大小写不同（%s），已按清单识别"
                % "、".join("%s→%s" % (actual, canonical) for canonical, actual in sorted(renamed))))

        # 一个 zip 塞了多个包：判不了以谁为准，先说清再停（不能落到「没有清单」的假话上）。
        many = found.get("multiple_packages")
        if many:
            result["problems"].append(_problem(
                "multiple-packages",
                "这个压缩包里装了 %d 个包（%s）：判不了以哪一个为准"
                % (len(many), "、".join(many)),
                "一个包压成一个 zip 再导入：包里只留这一个包的目录和它的清单"))
            return _finish(result, started)

        # F2：清单读不出对象 / 缺 api_version 的 mod 布局，病因先说清楚再停，不猜家族。
        broken = found.get("manifest_error")
        if broken:
            result["problems"].append(
                _broken_manifest_problem(broken, found.get("manifest_name") or "plugin.json"))
            return _finish(result, started)
        if found.get("mod_shape_without_api_version"):
            result["problems"].append(_missing_api_version_problem(manifest))
            return _finish(result, started)

        tools_map = _tools_for(path)
        _fill(result, found, root, names, reader, plugins_dir, skills_runtime, tools_map,
              data_dir, custom_plugins_dir, path)
    except Exception as exc:  # 契约：永不抛
        result["problems"].append(_problem(
            "inspect-failed", "预检异常：%s: %s" % (type(exc).__name__, exc),
            "把包和这段报错一起发给维护者"))
    finally:
        _close(reader)
    return _finish(result, started)


def _fill(result: dict, found: dict, root: str, names: List[str], reader,
          plugins_dir, skills_runtime, tools_map: Dict[str, dict], data_dir=None,
          custom_plugins_dir=None, path=None) -> None:
    kind = found["kind"]
    manifest = found["manifest"]
    if kind in CAPABILITY_GATED_KINDS:
        # 单一实现、单一调用点：家族是否加闸只看 CAPABILITY_GATED_KINDS。
        field = _bad_capability_field(manifest)
        if field:
            result["problems"].append(_problem(
                "capability-names-invalid",
                "%s 的 %s 必须是不超过 128 项的能力名字符串数组，现在是 %s"
                % (found.get("manifest_name") or MANIFEST_NAMES[0], field,
                   _short_repr(manifest.get(field))),
                "宿主 host-one.mjs:86 解析包根清单（skill.json / plugin.json）时就是这个口径："
                "不是数组、超过 128 项、或有一项不是非空字符串，一律 manifest-invalid 拒收。"
                "改成能力名字符串数组，例如 [\"demo-js.status\"]"))
    if kind == "feature-pack":
        _fill_feature_pack(result, manifest)
    elif kind == MCP_KIND:
        _fill_mcp_server(result, found, reader, data_dir)
    elif kind == "skill":
        _fill_skill(result, reader, skills_runtime, tools_map)
    elif kind == "plugin-py":
        _fill_plugin_py(result, manifest, plugins_dir, tools_map, root, names, reader)
    elif kind == "extension-js":
        _fill_extension_js(result, root, names, reader, manifest, tools_map)
    elif kind == MOD_KIND:
        _fill_mod_py(result, found, path, root, custom_plugins_dir, data_dir)
    else:
        result["problems"].append(_problem(
            "unrecognized",
            "认不出这是哪一类包（%s）" % "；".join(found["evidence"][-2:]),
            "Python 插件与 JS 扩展的包根应有 plugin.json；技能应有 skill.json；"
            "功能包应有 pack.json（format=whale-feature-pack）"))


def _fill_feature_pack(result: dict, manifest: dict) -> None:
    plugins = _as_str_list(manifest.get("plugins"))
    skills = _as_str_list(manifest.get("skills"))
    preferences = manifest.get("preferences")
    prefs = len(preferences) if isinstance(preferences, (list, dict)) else 0
    result["notices"].append(_notice(
        "feature-pack-contents",
        "功能包：%d 个插件 / %d 个技能 / %d 条偏好（安装时三处同时落）" % (len(plugins), len(skills), prefs)))


def _fill_mcp_server(result: dict, found: dict, reader, data_dir=None) -> None:
    """MCP server：entry=command，tools 恒为空（要等 server 起来才知道有哪些工具）。"""
    picked = found.get("mcp") or {}
    server = picked.get("server") or {}
    manifest = found.get("manifest") or {}
    servers = manifest.get("mcpServers")
    if isinstance(servers, dict) and len(servers) > 1:
        result["notices"].append(_notice(
            "multiple-servers",
            "本包含 %d 个 MCP server，只导入 %s（其余 %d 个没动）"
            % (len(servers), picked.get("server_id") or "(裸定义)", len(servers) - 1)))
    ident = _mcp_id(_text(picked.get("server_id")), manifest, reader)
    command = _text(server.get("command"))
    result["id"] = ident
    result["name"] = _text(server.get("name")) or _text(manifest.get("name")) or ident
    result["entry"] = command
    result["tools"] = []
    result["notices"].append(_notice(
        "tools-at-runtime", "MCP 工具要等 server 起来才知道，导入后用「工具」页刷新查看"))
    result["load"] = _not_loaded()
    usable_id = bool(MCP_ID_RE.match(ident or ""))
    if usable_id and data_dir is not None:
        result["target_dir"] = str(Path(data_dir) / QQ_ONLY / MCP_DIR / ident)
    if not usable_id:
        result["problems"].append(_problem(
            "bad-id", "MCP server id 不能用：%s" % (ident or "空"),
            "id 要以字母或数字开头，只含字母、数字、下划线、点、短横线（1-64 位），"
            "否则会拼出跑出目标目录的落点"))
    if not command:
        result["problems"].append(_problem(
            "unsupported-transport",
            "这个 MCP server 没有 command（只有 url/type=%s），现在拉不起来"
            % (_text(server.get("type")) or _text(server.get("url")) or "空"),
            MCP_UNSUPPORTED))
        return
    usable, bare = _absolute_command(command)
    if not usable:
        result["problems"].append(_problem(
            "command-not-absolute", "command 是相对路径：%r" % command,
            "安装后工作目录会变，相对路径就断了；改成本机可执行文件的绝对路径"))
    elif bare:
        result["notices"].append(_notice(
            "command-on-path",
            "command=%s 是裸可执行名，运行时会从 PATH 解析：确认部署机器上已经装好它" % command))


def _fill_skill(result: dict, reader, skills_runtime, tools_map: Dict[str, dict]) -> None:
    """技能走既有分层判断（src/skill_manifest.classify），tier/reason 进 load 与 notices。"""
    tier, reason, tools = "T6-disabled", "unclassified", []
    try:
        info = skill_manifest.classify(reader, tools=tools_map)
        tier = str(info.get("tier") or tier)
        reason = str(info.get("tierReason") or reason)
        tools = [str(entry.get("name") or "") for entry in (info.get("tools") or []) if entry.get("name")]
    except Exception as exc:
        reason = "classify-failed: %s: %s" % (type(exc).__name__, exc)
    result["tools"] = tools
    result["load"] = {"ok": tier == "T1-ready", "error": "" if tier == "T1-ready" else reason, "code": tier}
    result["notices"].append(_notice("skill-tier", "分层判定 %s：%s" % (tier, reason)))
    if skills_runtime is not None and result["id"]:
        result["target_dir"] = str(Path(skills_runtime) / "skills" / result["id"])


_IMPORT_RE = re.compile(
    r"^[ 	]*(?:from[ 	]+(\.*)([A-Za-z_][\w.]*)?[ 	]+import[ 	]+([\w., 	]+)"
    r"|import[ 	]+([\w., 	]+))",
    re.M)


def _imported_names(source: str) -> set:
    """源码里 import 了哪些顶层模块名（只用来判断「这个模块会不会被丢」）。"""
    found: set = set()
    for match in _IMPORT_RE.finditer(source or ""):
        dots, module, names_from, plain = match.groups()
        if plain:
            for piece in plain.split(","):
                piece = piece.strip().split(" ")[0]
                if piece:
                    found.add(piece.split(".")[0])
        elif module:
            found.add(module.split(".")[0])
        elif dots and names_from:
            for piece in names_from.split(","):
                piece = piece.strip().split(" ")[0]
                if piece:
                    found.add(piece)
    return found


def _py_extras(root: str, names: List[str], entry: str, reader):
    """包根除入口与清单外的其它文件，以及入口真 import 了的包内模块。

    只把**入口真 import 了的**当阻断条件：包里多一个没被 import 的 helper.py 不影响加载
    （tests/test_plugin_import_reliability.py:63-70 就是这个形状，装完是好的），但入口 import
    的兄弟模块一旦被单文件落盘丢掉，插件一加载就 ImportError。
    """
    prefix = root + "/" if root else ""
    entry_rel = str(entry or "").replace("\\", "/")
    while entry_rel.startswith("./"):
        entry_rel = entry_rel[2:]
    extras: List[str] = []
    modules: Dict[str, str] = {}
    for name in names or []:
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel or rel == entry_rel:
            continue
        base = rel.rsplit("/", 1)[-1]
        if base in ALL_MANIFESTS:
            continue
        extras.append(rel)
        if base.lower().endswith(PY_EXTS):
            modules[base.rsplit(".", 1)[0]] = rel
        elif "/" in rel:
            modules.setdefault(rel.split("/")[0], rel)
    if not entry_rel:
        return extras, []
    source = _read_text(reader, prefix + entry_rel) or ""
    return extras, [modules[name] for name in sorted(_imported_names(source)) if name in modules]


def _fill_plugin_py(result: dict, manifest: dict, plugins_dir, tools_map: Dict[str, dict],
                    root: str = "", names: Optional[List[str]] = None, reader=None) -> None:
    version = manifest.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and "app_version" in manifest:
        # 老方言不拒收：_version_text 已把整数 version 换成 app_version，_entry_name 已按包内容
        # 补出唯一 .py（与 src/plugin_installer.py:557-565、598-639 同一口径），这里只留一条告知。
        result["notices"].append(_notice(
            "b1-dialect-normalized",
            "whale-plugin v1 老方言已归一：version=%r 当格式版本，插件版本取 app_version=%r，"
            "入口按包内唯一 .py 补成 %r"
            % (version, result["version"], result["entry"] or "(没有唯一 .py)")))
    if not result["entry"]:
        result["problems"].append(_problem(
            "py-entry-missing",
            "找不到可用的 Python 入口（plugin.json 没有 entry，包内也没有唯一 .py）",
            "把插件源码放进包根，或在 plugin.json 里写 entry"))
    extras, dropped = _py_extras(root, names or [], result["entry"], reader)
    if dropped:
        # 真因：src/plugin_installer.py:811 只从包里取回一个 .py，落盘成 <id>.py，其余全丢。
        # 入口 import 了被丢的模块 → 装完必然 ImportError，宁可预检拦下并给出两条出路。
        result["problems"].append(_problem(
            "py-multi-file",
            "入口 %s 还 import 了包内其它源码（%s）：Python 插件按单文件 <id>.py 落盘，"
            "这些模块会被丢掉，插件一加载就 ImportError"
            % (result["entry"], "、".join(dropped[:4])),
            "把这些模块并进入口文件，或改成目录 mod：plugin.json 写 api_version=1，"
            "装成 <custom_plugins>/<id>/ 整目录、结构与文件名原样保留"))
    elif extras:
        result["notices"].append(_notice(
            "extra-files-not-installed",
            "包里除入口还有 %s：Python 插件只落单文件 %s，这些文件不会装进去"
            % ("、".join(extras[:4]) + ("等 %d 个" % len(extras) if len(extras) > 4 else ""),
               "%s.py" % (result["id"] or "<id>"))))
    result["tools"] = sorted(tools_map)
    if plugins_dir is not None and result["id"]:
        result["target_dir"] = str(Path(plugins_dir) / ("%s.py" % result["id"]))
    result["load"] = _not_loaded()


def _fill_extension_js(result: dict, root: str, names: List[str], reader,
                       manifest: dict, tools_map: Dict[str, dict]) -> None:
    prefix = root + "/" if root else ""
    entry = result["entry"]
    source = _read_text(reader, prefix + entry) if entry else ""
    if _CJS_MARK in source and not _ESM_RE.search(source):
        result["problems"].append(_problem(
            "cjs-unsupported",
            "入口 %s 是 CommonJS（module.exports，且没有任何 ESM export），宿主不支持" % entry,
            "改成 ESM：export async function setup(api) { ... }"))
    if manifest.get("enabledByDefault") is False:
        result["notices"].append(_notice("enabled-by-default-false",
                                        "安装后默认不启用（enabledByDefault: false），需要手动启用"))
    if _wants_console_ui(manifest, prefix, names, reader, entry):
        # 缺控制台页面不是坏包：装得进，只是没有页面、宿主当前也没有控制台页面能力。
        result["notices"].append(_notice(
            "needs-console-ui",
            "包里没有控制台页面 %s：扩展能装，但当前宿主没有控制台页面能力，它的主体加载不了"
            % CONSOLE_UI))
    result["hooks"] = _as_str_list(manifest.get("hooks"))
    result["providers"] = _as_str_list(manifest.get("providers"))
    result["tools"] = sorted(tools_map)
    result["load"] = _not_loaded()


def _read_text(reader, name: str) -> str:
    try:
        return reader.read(name) or ""
    except Exception:
        return ""


def _bad_capability_field(manifest: dict) -> str:
    """宿主 host-one.mjs:84-91 names() 的口径：必须是数组、不超 128 项、每项是非空字符串。

    只对 CAPABILITY_GATED_KINDS 里的家族调用 —— 其余家族的宿主读取端不校验这两个字段。
    """
    for key in ("capabilities", "requires"):
        value = manifest.get(key, [])
        if (not isinstance(value, list) or len(value) > 128
                or not all(isinstance(item, str) and item.strip() for item in value)):
            return key
    return ""


def _short_repr(value, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _wants_console_ui(manifest: dict, prefix: str, names: List[str], reader, entry: str) -> bool:
    """清单或包根 JS 里提到控制台 ui/index.html，但包里没有这个文件 → 需要控制台 UI 宿主。"""
    if CONSOLE_UI in names:
        return False
    for key in ("ui", "consoleUi", "settingsUi"):
        value = manifest.get(key)
        if isinstance(value, (str, dict)) and CONSOLE_UI in json.dumps(value, ensure_ascii=False):
            return True
    texts = [prefix + entry] if entry else []
    for name in names:
        inner = name[len(prefix):] if name.startswith(prefix) else ""
        if inner and "/" not in inner and name.lower().endswith(JS_EXTS):
            texts.append(name)
    for name in dict.fromkeys(texts):
        if CONSOLE_UI in _read_text(reader, name):
            return True
    return False


def _not_loaded() -> dict:
    return {"ok": False, "error": "本卡只做预检，未做真加载", "code": "not-loaded"}


def _looks_like_zip(path) -> bool:
    try:
        target = Path(path)
        if target.is_file():
            with target.open("rb") as handle:
                return handle.read(2) == b"PK"
    except (OSError, TypeError, ValueError):
        pass
    return False

# ── install() ───────────────────────────────────────────────────
#
# 分支表（kind → 复用哪个既有安装器 → 真实落点）：
#   plugin-py    → plugin_installer.install_from_file       → <plugins_dir>/<id>.py
#   extension-js → qqagent_host.materialize_host_root +
#                  install_extension + import_probe          → <data_dir>/qq-only/qqagent_host/plugins/<id>/
#   skill        → skill_market.install                     → <data_root>/data/qq-only/skill_extensions/<id>/
#   feature-pack → feature_pack.install                     → <plugins_dir>/*.py + <data_dir>/agent_learning.json
#   mcp-server   → 本卡自带的解包 + 登记表                    → <data_dir>/qq-only/mcp_servers/<id>/
#                  （不 import 任何 MCP SDK：venv 里没有 mcp 包）
#
# 三条边界：confirmed=False 一律抛 PackImportError（安全闸）；目标根只由调用方显式传入
# （缺了报 no-target，绝不回落到仓库真实 data/）；其余失败一律收敛成 problems，不抛。

import importlib
import shutil
import uuid


FEATURE_LEARNING = "agent_learning.json"


class PackImportError(Exception):
    """未确认就把包落盘。install() 只在 confirmed=False 时抛它，其余失败进 problems。"""


def install(path, *, confirmed: bool = False, kind: str = "", overwrite: bool = False,
            enable=None, plugins_dir=None, skills_runtime=None, data_dir=None,
            custom_plugins_dir=None) -> dict:
    """按 inspect() 判定的家族落盘，返回与 inspect() 同形的 19 键 schema。

    enable 只有技能安装器认（其它家族会记一条 enable-ignored 的 notice）。
    """
    if not confirmed:
        raise PackImportError("未经确认：请先预检并把结果展示给用户，确认后再安装")

    started = time.perf_counter()
    result = inspect(path, plugins_dir=plugins_dir,
                     skills_runtime=_runtime_path(skills_runtime), data_dir=data_dir,
                     custom_plugins_dir=custom_plugins_dir)
    options = {"overwrite": bool(overwrite), "enable": enable,
               "plugins_dir": plugins_dir, "skills_runtime": skills_runtime,
               "data_dir": data_dir, "custom_plugins_dir": custom_plugins_dir}
    try:
        detected = str((detect(path) or {}).get("kind") or "")
        if kind and str(kind) != detected:
            result["problems"].append(_problem(
                "kind-mismatch",
                "指定的家族是 %s，但按包内容判定为 %s" % (kind, detected or "unknown"),
                "不传 kind 按内容装，或换一个真的是 %s 的包" % kind))
        if not result["problems"]:
            branch = _BRANCHES.get(detected)
            if branch is None:
                result["problems"].append(_problem(
                    "unrecognized", "认不出这是哪一类包（%s）" % (detected or "unknown"),
                    "先跑预检确认 kind，再按 kind 安装"))
            else:
                if detected != "skill" and enable is not None:
                    result["notices"].append(_notice(
                        "enable-ignored",
                        "enable 只对技能有效；这个家族的启用状态由宿主登记表决定，本次 enable 未生效"))
                branch(result, path, options)
    except Exception as exc:  # 契约：install() 永不抛
        result["problems"].append(_problem(
            "install-failed", "安装异常：%s: %s" % (type(exc).__name__, exc),
            "把包和这段报错一起发给维护者；actions 里列了本次已写入的位置"))
    return _finish(result, started)


def _load(name: str):
    """取既有安装器模块（. 与 src. 两种导入口径，和本文件顶部的导入兜底一致）。"""
    try:
        return importlib.import_module("." + name, __package__)
    except Exception:
        return importlib.import_module("src." + name)


def _branch_modules(result: dict, *names):
    """一次取齐本分支要用的安装器；取不到就记 problems 并返回 None（不抛）。"""
    try:
        return [_load(name) for name in names]
    except Exception as exc:
        result["problems"].append(_problem(
            "installer-missing",
            "找不到可复用的安装器 %s：%s: %s" % ("/".join(names), type(exc).__name__, exc),
            "确认 src/ 下的安装器文件都在后重试"))
        return None


class _Aside:
    """覆盖前把旧版本挪到同目录的 .old-* 名字：成功提交删除，失败原样挪回。"""

    def __init__(self, path):
        self.path = Path(path) if path is not None else None
        self.stash = None

    def move(self) -> bool:
        if self.path is None or not self.path.exists():
            return False
        stash = self.path.parent / (".old-%s-%s" % (self.path.name, uuid.uuid4().hex))
        try:
            self.path.replace(stash)
        except OSError:
            return False
        self.stash = stash
        return True

    def commit(self) -> None:
        if self.stash is None:
            return
        try:
            if self.stash.is_dir():
                shutil.rmtree(self.stash, ignore_errors=True)
            elif self.stash.exists():
                self.stash.unlink()
        except OSError:
            pass
        self.stash = None

    def restore(self) -> bool:
        if self.stash is None or not self.stash.exists():
            return False
        stash, self.stash = self.stash, None
        try:
            if self.path.is_dir():
                shutil.rmtree(self.path, ignore_errors=True)
            elif self.path.exists():
                self.path.unlink()
            stash.replace(self.path)
        except OSError:
            return False
        return True


class _SkillTarget:
    """skills_runtime 只给路径时包一层 data_root，让 skill_tools / 登记表认得。"""

    def __init__(self, data_root):
        self.data_root = Path(data_root)

    @property
    def root(self):
        return self.data_root


def _skill_runtime(value):
    if value is None:
        return None
    if getattr(value, "data_root", None) or getattr(value, "root", None):
        return value
    try:
        return _SkillTarget(value)
    except (TypeError, ValueError):
        return None


def _runtime_path(value):
    """inspect() 的 skills_runtime 只吃路径：运行时对象取它的数据根，其它原样给它。"""
    if value is None:
        return None
    for name in ("data_root", "root"):
        path = getattr(value, name, None)
        if path:
            return str(path)
    return value


def _install_plugin_py(result: dict, path, options: dict) -> None:
    """Python 插件 → plugin_installer.install_from_file（临时文件 + 原子替换落盘）。"""
    mods = _branch_modules(result, "plugin_installer")
    if mods is None:
        return
    plugin_installer = mods[0]
    if options["plugins_dir"] is None:
        result["problems"].append(_problem(
            "no-target", "缺少 plugins_dir：不知道插件装到哪个目录",
            "传目标插件目录后再装；本模块不会回落到仓库 data/"))
        return
    dest_dir = Path(options["plugins_dir"])
    ident = str(result["id"] or "")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / ("%s.py" % plugin_installer.normalize_plugin_id(ident))
    except Exception as exc:
        result["problems"].append(_problem(
            "bad-id", "插件 id 不能用（%s）：%s" % (ident or "空", exc),
            "插件文件名要用「小写字母开头 + 小写字母/数字/下划线」，长度 2-48"))
        return
    if target.exists() and not options["overwrite"]:
        result["problems"].append(_problem(
            "exists", "同 id 的插件已经装过：%s" % target,
            "确认要替换旧版本就传 overwrite=True（旧文件会先备份成 .bak）"))
        return
    try:
        installed = plugin_installer.install_from_file(
            str(path), dest_dir, ident, True, options["overwrite"]) or {}
    except Exception as exc:  # InstallError 及一切：翻译成 problems
        code = str(getattr(exc, "code", "") or "")
        if code in ("", "error"):
            code = "exists" if "已存在" in str(exc) else "install-failed"
        result["problems"].append(_problem(
            code, str(exc) or type(exc).__name__,
            "按提示改包后重新预检；本次没有半装文件（写入是临时文件 + 原子替换）"))
        return
    landed = Path(str(installed.get("path") or target))
    if not landed.is_file():
        result["problems"].append(_problem(
            "landed-missing", "安装器返回成功，但目标文件不存在：%s" % landed,
            "检查 plugins_dir 是否可写、是否被别的进程删掉"))
        return
    result["landed"] = str(landed)
    result["target_dir"] = str(landed.parent)
    result["id"] = str(installed.get("plugin_id") or ident)
    result["name"] = str(installed.get("name") or result["name"])
    result["bytes"] = int(installed.get("bytes") or result["bytes"] or 0)
    result["installed_but_disabled"] = False
    result["actions"].append("plugin-py: %s" % landed)
    if installed.get("overwritten"):
        result["notices"].append(_notice(
            "overwritten", "已替换旧版本，旧文件备份在 %s" % (installed.get("_backup") or "(未返回)")))
    info = plugin_installer.inspect_local(str(landed), result["id"], dest_dir) or {}
    ok = bool(info.get("ok"))
    problems = [str(one) for one in (info.get("problems") or [])]
    result["load"] = {
        "ok": ok,
        "error": "" if ok else ("；".join(problems) or "落盘复核未通过"),
        "code": "post-install-check",
    }
    result["notices"].append(_notice(
        "python-load",
        "已复核落盘文件的语法与安全检查；真加载发生在插件管理器下次启动时，安装期不执行插件代码"))

def _install_extension_js(result: dict, path, options: dict) -> None:
    """JS 扩展 → 造宿主根 + install_extension 解包 + import_probe 真加载一次。"""
    mods = _branch_modules(result, "qqagent_host")
    if mods is None:
        return
    qqagent_host = mods[0]
    if options["data_dir"] is None:
        result["problems"].append(_problem(
            "no-target", "缺少 data_dir：宿主根要落在 <data_dir>/qq-only/qqagent_host",
            "传运行数据目录后再装；本模块不会回落到仓库 data/"))
        return
    host = qqagent_host.materialize_host_root(options["data_dir"]) or {}
    root = Path(str(host.get("root"))) if host.get("ok") and host.get("root") else None
    if root is None:
        error = host.get("error") or {}
        result["problems"].append(_problem(
            str(error.get("code") or "host-not-ready"),
            "宿主根没准备好，扩展没有落点：%s" % (error.get("message") or "未返回原因"),
            "确认 runtime/qqagent-host 载荷完整、node 可执行后再装"))
        return
    ident = qqagent_host.safe_extension_id(result["id"]) or qqagent_host.safe_extension_id(
        Path(str(path)).stem)
    dest = root / "plugins" / ident if ident else None
    if dest is not None and dest.exists() and not options["overwrite"]:
        result["problems"].append(_problem(
            "exists", "同 id 的扩展已经装过：%s" % dest,
            "确认要替换旧版本就传 overwrite=True（旧目录先挪开，失败会挪回）"))
        return
    aside = _Aside(dest) if dest is not None and options["overwrite"] else None
    installed, folder = {}, None
    try:
        if aside is not None:
            aside.move()
        installed = qqagent_host.install_extension(
            str(path), root, extension_id=ident or None, force=options["overwrite"]) or {}
        if not installed.get("ok"):
            raise RuntimeError(str((installed.get("error") or {}).get("message") or "解包失败"))
        folder = Path(str(installed.get("root") or dest or ""))
        if not folder.is_dir():
            raise RuntimeError("解包返回成功但目录不存在：%s" % folder)
    except Exception as exc:
        if aside is not None:
            aside.restore()
        code = str((installed.get("error") or {}).get("code") or "")
        result["problems"].append(_problem(
            code or "install-failed", str(exc) or type(exc).__name__,
            "检查包是否完整、目录是否被占用后重试；旧版本已挪回（原来装过的话）"))
        return
    if aside is not None:
        aside.commit()
    ext_id = str(installed.get("id") or ident or folder.name)
    result["landed"] = str(folder)
    result["target_dir"] = str(folder)
    result["id"] = ext_id
    result["files"] = int(installed.get("files") or result["files"] or 0)
    result["bytes"] = int(installed.get("bytes") or result["bytes"] or 0)
    result["actions"].append("extension-js: %s" % folder)
    if options["enable"] is not None:
        result["notices"].append(_notice(
            "enable-ignored", "JS 扩展的启用状态记在宿主登记表里，本次 enable 未生效"))

    probe = {}
    try:
        probe = qqagent_host.import_probe(folder, root) or {}
        qqagent_host.remember_probe(root, ext_id, probe)
    except Exception as exc:
        probe = {"ok": False, "error": {"code": "probe-failed",
                 "message": "%s: %s" % (type(exc).__name__, exc)}}
    tools = sorted({str(one.get("name") or one.get("id") or "") for one in (probe.get("tools") or [])} - {""})
    if tools:
        result["tools"] = tools
    hooks = [str(one) for one in (probe.get("hooks") or [])]
    if hooks:
        result["hooks"] = hooks
    providers = [str(one) for one in (probe.get("providers") or [])]
    if providers:
        result["providers"] = providers
    error = probe.get("error") or {}
    load_error = str(error.get("message") or probe.get("loadError") or "")
    load_ok = bool(probe.get("ok"))
    result["load"] = {"ok": load_ok, "error": "" if load_ok else load_error,
                      "code": "loaded" if load_ok else str(error.get("code") or "load-failed")}
    manifest = {}
    try:
        manifest = qqagent_host.read_plugin_manifest(folder) or {}
    except Exception:
        manifest = {}
    default_off = manifest.get("enabledByDefault") is False
    result["installed_but_disabled"] = bool(default_off or not tools or not load_ok)
    if default_off and not any(one.get("code") == "enabled-by-default-false"
                               for one in result["notices"]):
        result["notices"].append(_notice(
            "enabled-by-default-false",
            "清单写了 enabledByDefault: false：装好了但默认不启用，要去扩展列表里手动启用"))
    if not tools:
        result["notices"].append(_notice(
            "no-tools", "重新加载后一个工具都没拿到：装好了但用不上，按「装好未启用」记"))
    if not load_ok:
        result["notices"].append(_notice(
            "load-unverified",
            "装好了但这次没能加载（%s）：按「装好未启用」记，宿主修好后再装一次" % (load_error or "未知原因")))


def _install_skill(result: dict, path, options: dict) -> None:
    """技能 → skill_market.install（staging 解包 + 旧目录换位回滚 + 登记启用态）。"""
    mods = _branch_modules(result, "skill_market")
    if mods is None:
        return
    skill_market = mods[0]
    runtime = _skill_runtime(options["skills_runtime"])
    if runtime is None:
        result["problems"].append(_problem(
            "no-target", "缺少 skills_runtime：不知道技能装到哪个数据根",
            "传运行时对象（有 data_root）或数据根路径后再装"))
        return
    ident = str(result["id"] or "")
    dest = None
    try:
        dest = Path(skill_tools.extensions_dir(runtime)) / ident if ident else None
    except Exception:
        dest = None
    if dest is not None and dest.exists() and not options["overwrite"]:
        result["problems"].append(_problem(
            "exists", "同 id 的技能已经装过：%s" % dest,
            "确认要替换旧版本就传 overwrite=True（旧目录先挪开，失败会挪回）"))
        return
    try:
        outcome = skill_market.install(
            str(path), runtime=runtime, force=options["overwrite"],
            enable=options["enable"]) or {}
    except Exception as exc:
        result["problems"].append(_problem(
            "install-failed", "%s: %s" % (type(exc).__name__, exc),
            "旧版本没被改动（skill_market 换位失败会自动挪回）；改包后重新预检"))
        return
    if not outcome.get("ok"):
        error = outcome.get("error") or {}
        result["problems"].append(_problem(
            str(error.get("code") or "install-failed"),
            str(error.get("message") or "技能安装失败"),
            "确认包根有 skill.json、数据根可写后重试"))
        return
    folder = Path(str(outcome.get("path") or dest or ""))
    if not folder.is_dir():
        result["problems"].append(_problem(
            "landed-missing", "安装器返回成功但目录不存在：%s" % folder,
            "检查数据根是否可写"))
        return
    result["landed"] = str(folder)
    result["target_dir"] = str(folder)
    result["id"] = str(outcome.get("id") or ident)
    if outcome.get("entry"):
        result["entry"] = str(outcome["entry"])
    tools = [str(one) for one in (outcome.get("tools") or []) if str(one)]
    if tools:
        result["tools"] = tools
    result["files"] = int(outcome.get("files") or result["files"] or 0)
    result["bytes"] = int(outcome.get("bytes") or result["bytes"] or 0)
    tier = str(outcome.get("tier") or "")
    note = str(outcome.get("note") or "")
    result["load"] = {"ok": tier == "T1-ready",
                      "error": "" if tier == "T1-ready" else (note or tier or "层级未知"),
                      "code": tier or "unknown-tier"}
    enabled = outcome.get("enabled")
    result["installed_but_disabled"] = enabled is False
    if enabled is False:
        result["notices"].append(_notice(
            "installed-disabled", "技能已落盘但没有启用：%s" % (note or "未给原因")))
    if outcome.get("replaced"):
        result["notices"].append(_notice("overwritten", "已替换旧版本技能目录"))
    result["notices"].append(_notice(
        "skill-target",
        "技能真实落点 %s（skill_market 约定 <data_root>/data/qq-only/skill_extensions/<id>）" % folder))
    result["actions"].append("skill: %s" % folder)

def _install_feature_pack(result: dict, path, options: dict) -> None:
    """功能包 → feature_pack.install（逐条目独立安装，学习库落 <data_dir>）。"""
    mods = _branch_modules(result, "feature_pack", "agent_learning")
    if mods is None:
        return
    feature_pack, agent_learning = mods[0], mods[1]
    if options["plugins_dir"] is None or options["data_dir"] is None:
        result["problems"].append(_problem(
            "no-target", "功能包要把插件落到 plugins_dir、技能与偏好写进学习库，两处都得给",
            "同时传 plugins_dir 与 data_dir 后再装"))
        return
    plugins_dir = Path(options["plugins_dir"])
    learning_path = Path(options["data_dir"]) / FEATURE_LEARNING
    try:
        plugins_dir.mkdir(parents=True, exist_ok=True)
        learning_path.parent.mkdir(parents=True, exist_ok=True)
        before_plugins = {item.name for item in plugins_dir.glob("*.py")}
        before_learning = learning_path.read_bytes() if learning_path.is_file() else None
    except OSError as exc:
        result["problems"].append(_problem(
            "no-target", "目标根不可用：%s: %s" % (type(exc).__name__, exc),
            "换一个可写的插件目录与数据目录后再装"))
        return
    try:
        report = feature_pack.install(
            str(path), plugins_dir, agent_learning.AgentLearning(learning_path),
            True, options["overwrite"]) or {}
    except Exception as exc:
        rolled = _rollback_feature_pack(plugins_dir, before_plugins, learning_path,
                                       before_learning)
        result["problems"].append(_problem(
            str(getattr(exc, "code", "") or "install-failed"),
            "功能包安装失败：%s: %s" % (type(exc).__name__, exc),
            "已回收本次写入（删除 %d 个新插件、学习库还原）；改包后重新预检" % rolled))
        return
    items = list(report.get("results") or [])
    done = [one for one in items if one.get("success")]
    failed = [one for one in items if not one.get("success")]
    paths = [str(one.get("path")) for one in done if one.get("path")]
    missing = [one for one in paths if not Path(one).is_file()]
    result["target_dir"] = str(plugins_dir)
    result["landed"] = paths[0] if paths else str(learning_path)
    result["installed_but_disabled"] = False
    result["actions"].extend(
        "feature-pack: %s" % str(one.get("path") or one.get("file") or one.get("kind") or "?")
        for one in done)
    ok = bool(report.get("success")) and not failed and not missing
    result["load"] = {
        "ok": ok,
        "error": "" if ok else ("；".join(
            str(one.get("message") or one.get("file") or "") for one in failed) or "有条目没装上"),
        "code": "installed" if ok else "partial",
    }
    result["notices"].append(_notice(
        "feature-pack",
        "逐条目独立安装（partial=True）：成功 %d 项、失败 %d 项；已成功条目留在原地，学习库=%s"
        % (len(done), len(failed), learning_path)))
    if missing:
        result["problems"].append(_problem(
            "landed-missing", "有 %d 个插件文件没落到 %s" % (len(missing), plugins_dir),
            "检查磁盘空间与权限后重试"))
    if failed:
        names = "、".join(str(one.get("file") or one.get("kind") or "?") for one in failed[:6])
        result["problems"].append(_problem(
            "partial",
            "功能包只装上了一部分：%d 项成功、%d 项失败（%s）" % (len(done), len(failed), names),
            "把已成功的条目删掉后可重装；本次已在 notices 标注 partial=True"))


def _rollback_feature_pack(plugins_dir, before_plugins, learning_path, before_learning) -> int:
    """功能包中途抛异常时回收：删本次新建的插件文件、学习库回到安装前。返回删除条数。"""
    removed = 0
    try:
        for item in plugins_dir.glob("*.py"):
            if item.name in before_plugins:
                continue
            try:
                item.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    try:
        if before_learning is None:
            if learning_path.is_file():
                learning_path.unlink()
        else:
            learning_path.write_bytes(before_learning)
    except OSError:
        pass
    return removed


def _rmtree(path) -> None:
    """删掉不留的临时目录；失败也不抛（调用方只看 problems）。"""
    try:
        shutil.rmtree(str(path), ignore_errors=True)
    except Exception:
        pass


class _McpReject(Exception):
    """MCP 包解包被安全检查挡下：code/message/hint 直接翻译成 problems。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _pack_plan(path, root_fn=None) -> Tuple[str, List[str]]:
    """重算包根与成员清单：安装阶段解包要按包根剥掉外层目录。

    root_fn 决定「包根」口径，默认 MCP 的 _mcp_root；目录 mod 传 _find_root（就是 inspect
    判家族时用的那个函数），两边算出来的包根必然一致，剥出来的结构才对得上。
    """
    pick_root = root_fn or _mcp_root
    reader = None
    try:
        reader, _origin, problem = _open_pack(path, "")
        if reader is None:
            return "", []
        names = [name for name in (reader.names() or []) if name]
        return pick_root(names), names
    except Exception:
        return "", []
    finally:
        _close(reader)


def _stage_mcp(path, staging: Path, manifest: str = MCP_MANIFEST, root_fn=None) -> dict:
    """把包根下的内容解到 staging（沿用 24MB / 4000 条 / 越界路径 / 软链的检查）。

    目录 mod 复用这套检查，只换清单名（plugin.json）与包根口径（_find_root）。
    """
    root, names = _pack_plan(path, root_fn)
    if not names:
        raise _McpReject("empty-pack", "包里没有任何文件：%s" % path, "重新导出这个包")
    prefix = root + "/" if root else ""
    target = Path(str(path))
    try:
        if target.is_dir():
            return _stage_mcp_dir(target, staging, prefix, manifest)
        if zipfile.is_zipfile(target):
            return _stage_mcp_zip(target, staging, prefix, manifest)
        if target.is_file():
            return _stage_mcp_file(target, staging, manifest)
    except _McpReject:
        raise
    except OSError as exc:
        raise _McpReject("install-failed", "读包失败：%s: %s" % (type(exc).__name__, exc),
                         "确认包完整、目标目录可写后重试")
    raise _McpReject("path-invalid", "看不懂这个包路径：%s" % target, "传目录、zip 或 mcp.json")


def _stage_mcp_file(source: Path, staging: Path, manifest: str = MCP_MANIFEST) -> dict:
    """单文件形态（直接把一个 mcp.json 拖进来）：原样落成 <id>/mcp.json。"""
    data = source.read_bytes()
    if len(data) > MCP_MAX_BYTES:
        raise _McpReject("zip-too-large", "文件过大（%dMB，上限 %dMB）"
                         % (len(data) // (1024 * 1024), MCP_MAX_BYTES // (1024 * 1024)),
                         "MCP 包上限 24MB")
    staging.mkdir(parents=True, exist_ok=True)
    (staging / manifest).write_bytes(data)
    return {"files": 1, "bytes": len(data)}


def _stage_mcp_dir(source: Path, staging: Path, prefix: str,
                   manifest: str = MCP_MANIFEST) -> dict:
    """目录包：只取包根以下的文件，软链与超限一律拒绝（不留半装文件）。"""
    picked: List[str] = []
    total = 0
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source).as_posix()
        if prefix and not relative.startswith(prefix):
            continue
        if item.is_symlink():
            raise _McpReject("zip-symlink", "包里含软链：%s" % relative, "去掉软链后重新打包")
        if not item.is_file():
            continue
        picked.append(relative)
        total += item.stat().st_size
        if len(picked) > MCP_MAX_FILES:
            raise _McpReject("zip-too-many-files", "包里文件过多（上限 %d 个）" % MCP_MAX_FILES,
                             "只放 server 本体与 mcp.json")
        if total > MCP_MAX_BYTES:
            raise _McpReject("zip-too-large", "包解压后过大（上限 %dMB）" % (MCP_MAX_BYTES // (1024 * 1024)),
                             "MCP 包上限 24MB")
    staging.mkdir(parents=True, exist_ok=True)
    for relative in picked:
        inner = relative[len(prefix):] if prefix else relative
        if not inner:
            continue
        dest = staging.joinpath(*[part for part in inner.split("/") if part not in ("", ".")])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source / relative), str(dest))
    _require_manifest(staging, prefix, manifest)
    return {"files": len(picked), "bytes": total}


def _stage_mcp_zip(source: Path, staging: Path, prefix: str,
                   manifest: str = MCP_MANIFEST) -> dict:
    """zip 包：与 qqagent_host.install_extension 同一套安全检查后再落 staging。"""
    try:
        archive = zipfile.ZipFile(str(source))
    except (OSError, zipfile.BadZipFile) as exc:
        raise _McpReject("zip-invalid", "压缩包打不开：%s" % exc, "重新下载这个包")
    files = total = 0
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MCP_MAX_FILES:
            raise _McpReject("zip-too-many-files", "压缩包里文件过多（%d 个，上限 %d）"
                             % (len(infos), MCP_MAX_FILES), "只放 server 本体与 mcp.json")
        size = sum(int(info.file_size) for info in infos)
        if size > MCP_MAX_BYTES:
            raise _McpReject("zip-too-large", "压缩包解压后过大（%dMB，上限 %dMB）"
                             % (size // (1024 * 1024), MCP_MAX_BYTES // (1024 * 1024)),
                             "MCP 包上限 24MB")
        staging.mkdir(parents=True, exist_ok=True)
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if not parts or name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
                raise _McpReject("zip-unsafe-path", "压缩包里有越界路径：%s" % info.filename,
                                 "去掉绝对路径与 .. 后重新打包")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise _McpReject("zip-symlink", "压缩包含软链：%s" % info.filename,
                                 "去掉软链后重新打包")
            relative = name[len(prefix):] if prefix and name.startswith(prefix) else name
            relative = relative.lstrip("/")
            if not relative:
                continue
            dest = staging.joinpath(*[part for part in relative.split("/")
                                      if part not in ("", ".")])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as handle:
                data = handle.read()
            dest.write_bytes(data)
            files += 1
            total += len(data)
    _require_manifest(staging, prefix, manifest)
    return {"files": files, "bytes": total}


def _require_manifest(staging: Path, prefix: str, manifest: str = MCP_MANIFEST) -> None:
    if not (staging / manifest).is_file():
        raise _McpReject("mcp-missing" if manifest == MCP_MANIFEST else "mod-missing",
                         "解出来的目录里没有 %s（包根前缀=%r）" % (manifest, prefix),
                         "确认 %s 在包根或唯一外层目录里" % manifest)


def _mcp_entry(target: Path, ident: str, source: str) -> dict:
    """登记表里的一条：从**落盘后**的 mcp.json 重新读，顺带证明文件齐全。"""
    try:
        manifest = json.loads((target / MCP_MANIFEST).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("落盘的 %s 读不出来：%s" % (MCP_MANIFEST, exc))
    if not isinstance(manifest, dict):
        raise ValueError("落盘的 %s 不是 JSON 对象" % MCP_MANIFEST)
    _server_id, server = _mcp_server(manifest)
    if server is None:
        raise ValueError("落盘的 %s 里没有可用的 server 定义" % MCP_MANIFEST)
    raw_args = server.get("args")
    args = [str(one) for one in raw_args] if isinstance(raw_args, list) else []
    raw_env = server.get("env")
    env = ({str(key): str(value) for key, value in raw_env.items()}
           if isinstance(raw_env, dict) else {})
    cwd = _text(server.get("cwd"))
    try:
        cwd_path = Path(cwd) if cwd and Path(cwd).is_absolute() else (target / cwd if cwd else target)
    except (OSError, ValueError):
        cwd_path = target
    return {
        "id": ident,
        "name": _text(server.get("name")) or _text(manifest.get("name")) or ident,
        "command": _text(server.get("command")),
        "args": args,
        "env": env,
        "cwd": str(cwd_path),
        "enabled": True,
        "source_package": source,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _write_mcp_index(data_root: Path, entry: dict) -> str:
    """幂等写 <data_dir>/qq-only/mcp_servers.json：同 id 就地覆盖，新 id 追加。"""
    index_path = data_root / QQ_ONLY / MCP_INDEX
    index_path.parent.mkdir(parents=True, exist_ok=True)
    servers: List[dict] = []
    if index_path.is_file():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8-sig") or "{}")
        except (OSError, ValueError) as exc:
            raise ValueError("登记表不是合法 JSON（%s）：%s" % (index_path, exc))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("servers"), list):
            raise ValueError("登记表结构不对（%s）：要 version=1 + servers 列表" % index_path)
        servers = [one for one in loaded["servers"] if isinstance(one, dict)]
    ident = str(entry.get("id") or "")
    replaced = [one for one in servers if str(one.get("id") or "") != ident]
    position = next((index for index, one in enumerate(servers)
                     if str(one.get("id") or "") == ident), len(replaced))
    replaced.insert(min(position, len(replaced)), entry)
    staging = index_path.parent / (".%s.tmp-%s" % (MCP_INDEX, uuid.uuid4().hex))
    staging.write_text(json.dumps({"version": 1, "servers": replaced},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(index_path)
    return str(index_path)


def _install_mcp_server(result: dict, path, options: dict) -> None:
    """MCP server → <data_dir>/qq-only/mcp_servers/<id>/ + 幂等登记 mcp_servers.json。"""
    if options["data_dir"] is None:
        result["problems"].append(_problem(
            "no-target", "缺少 data_dir：MCP server 要落到 <data_dir>/qq-only/mcp_servers",
            "传运行数据目录后再装；本模块不会回落到仓库 data/"))
        return
    ident = str(result["id"] or "")
    if not MCP_ID_RE.match(ident or ""):
        return                                  # bad-id 已在预检的 problems 里报过
    data_root = Path(options["data_dir"])
    base = data_root / QQ_ONLY / MCP_DIR
    target = base / ident
    if target.exists() and not options["overwrite"]:
        result["problems"].append(_problem(
            "exists", "同 id 的 MCP server 已经装过：%s" % target,
            "确认要替换旧版本就传 overwrite=True（旧目录先挪开，失败会挪回）"))
        return
    staging = base / (".install-%s" % uuid.uuid4().hex)
    try:
        base.mkdir(parents=True, exist_ok=True)
        staged = _stage_mcp(path, staging)
    except _McpReject as exc:
        _rmtree(staging)
        result["problems"].append(_problem(exc.code, exc.message, exc.hint))
        return
    except Exception as exc:  # 任何意外：一条 problems，不留半装文件
        _rmtree(staging)
        result["problems"].append(_problem(
            "install-failed", "解包失败：%s: %s" % (type(exc).__name__, exc),
            "确认包完整、目标目录可写后重试；旧版本没有被改动"))
        return
    aside = _Aside(target) if options["overwrite"] else None
    if aside is not None:
        aside.move()
    try:
        staging.replace(target)
    except OSError as exc:
        _rmtree(staging)
        if aside is not None:
            aside.restore()
        result["problems"].append(_problem(
            "install-failed", "目录落盘失败：%s: %s" % (type(exc).__name__, exc),
            "确认目标目录没有被占用后重试；旧版本已挪回"))
        return
    try:
        index_path = _write_mcp_index(data_root, _mcp_entry(target, ident, str(path)))
    except Exception as exc:
        _rmtree(target)
        if aside is not None:
            aside.restore()
        result["problems"].append(_problem(
            "register-failed", "落盘成功但登记表写不进去：%s: %s" % (type(exc).__name__, exc),
            "确认 <data_dir>/qq-only 可写后重试；本次已回收刚解出的目录"))
        return
    if aside is not None:
        aside.commit()
    result["landed"] = str(target)
    result["target_dir"] = str(target)
    result["files"] = int(staged.get("files") or result["files"] or 0)
    result["bytes"] = int(staged.get("bytes") or result["bytes"] or 0)
    result["installed_but_disabled"] = False
    result["load"] = {"ok": False, "code": "not-started",
                      "error": "MCP server 由宿主启动时才拉起，安装期只做落盘与登记"}
    result["actions"].append("mcp-server: %s" % target)
    result["notices"].append(_notice(
        "mcp-registered", "已登记到 %s（按 id 覆盖，重复安装不会多出一条）" % index_path))


# ── 目录 mod（第六种家族）──────────────────────────────────────────────────────
#
# 与其它五家的区别：这一家**不用** src/plugin_installer.py 那套 .py/.zip 落盘（那条路只
# 产出扁平 <id>.py，plugin.json 与包内子目录全丢），而是把包根以下整棵目录树落到
# <custom_plugins_dir>/<id>/。清单校验与禁用法扫描都不另写：前者用 manager 的
# _validate_mod_manifest，后者用 plugin_installer.check_source，判据只有一份实现。


def _custom_plugins_root(custom_plugins_dir, data_dir) -> Optional[Path]:
    """<custom_plugins_dir>：显式传值优先，否则 <data_dir>/custom_plugins。

    与 src/plugins/manager.py:399-414 同口径：manager 自己拿不到 custom_plugins_dir 时
    就落到 <data_dir>/custom_plugins，这里照抄那条兜底，不另发明规则。
    """
    if custom_plugins_dir is not None and str(custom_plugins_dir).strip():
        return Path(str(custom_plugins_dir).strip())
    if data_dir is not None and str(data_dir).strip():
        return Path(str(data_dir).strip()) / "custom_plugins"
    return None


def _mod_id_problem(ident: str) -> str:
    """id 会逐字当落盘目录名，先按目录安全筛一遍；返回中文原因，空串=能用。

    空 id 不在这里报：那是清单缺字段，留给 manager._validate_mod_manifest 的
    missing_id（唯一权威口径），本函数只管「这个名字当目录名行不行」。
    """
    if not ident:
        return ""
    if not MOD_ID_RE.match(ident):
        return ("id %r 不能当目录名：只允许字母、数字、下划线、点、横线，首字符是字母或数字，"
                "最长 64 个字符（中文、空格、斜杠、冒号都不行）" % (ident,))
    if ident.endswith("."):
        return "id %r 以点结尾：Windows 会把这个目录名截断，不能当目录名" % (ident,)
    if ident.split(".")[0].upper() in MOD_ID_RESERVED:
        return "id %r 是 Windows 保留设备名，不能当目录名" % (ident,)
    return ""


def _mod_source_name(path, root: str) -> str:
    """包里那层 mod 目录名：zip 内唯一外层目录取 root，直接给目录时取目录名。

    manager._validate_mod_manifest 的 id_mismatch 核的是「id == 它看见的那个目录名」；
    预检拿不到落盘后的目录，只能核包里这层，口径对齐。单文件包（比如单独一个
    plugin.json）没有目录名可比，返回空串、不参与这条判断。
    """
    if root:
        return root.rsplit("/", 1)[-1]
    try:
        target = Path(str(path))
    except (TypeError, ValueError):
        return ""
    return target.name if target.is_dir() else ""


def _mod_validator(result: dict):
    """取 manager 的权威校验器 PluginManager._validate_mod_manifest。

    用 __new__ 造空壳，不真构造 manager：这个方法只读模块级常量（MOD_MANIFEST_NAME /
    MOD_API_VERSION / MOD_PERMISSIONS）和 ModManifestError，不碰 self 状态、不建目录、
    不写 settings，所以空壳调用的结论与真 manager 一字不差，也不会顺手扫一遍插件目录。
    """
    mods = _branch_modules(result, "plugins.manager")
    if mods is None:
        return None
    cls = getattr(mods[0], "PluginManager", None)
    if cls is None:
        result["problems"].append(_problem(
            "installer-missing", "src/plugins/manager.py 里没有 PluginManager 类",
            "确认 src/plugins/manager.py 没被改动后重试"))
        return None
    return cls.__new__(cls)


def _mod_manifest_problems(result: dict, validator, folder: Path) -> Optional[dict]:
    """跑 manager._validate_mod_manifest，把它的拒载码原样翻译成 problems。

    清单校验只有这一份实现：这里不另写「id 是不是字符串 / api_version 是不是整数」之类的
    判断，否则两边迟早漂移——装上了却挂不起来是最难查的一种坏。返回归一后的清单，被拒收
    时返回 None（码与中文消息已进 problems）。
    """
    if validator is None:
        return None
    try:
        _entry, manifest = validator._validate_mod_manifest(folder, folder / MOD_MANIFEST)
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "")
        if not code:            # 不是 ModManifestError：别把真 bug 伪装成一个拒载码
            raise
        result["problems"].append(_problem(code, str(exc) or code, MOD_HINT))
        return None
    return manifest if isinstance(manifest, dict) else None


# 目录 mod 不收的文件后缀（F4）。选择依据是「双击或在 shell 里能直接跑起来」：
# Windows 可执行/驱动/安装器（exe dll pyd com scr sys cpl msi msix）、类 Unix 共享库与
# macOS 产物（so dylib）、JVM 可执行（jar class）、脚本宿主（bat cmd ps1 psm1 vbs vbe
# wsf hta sh bash zsh command）、宿主会加载的 JS 与 Python 字节码/无窗口启动器
# （js mjs cjs pyw pyc pyo）。.py 不在这张表里：它继续走 plugin_installer.check_source。
MOD_FORBIDDEN_EXTS = (
    ".exe", ".dll", ".pyd", ".com", ".scr", ".sys", ".cpl", ".msi", ".msix",
    ".so", ".dylib", ".jar", ".class",
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".wsf", ".hta",
    ".sh", ".bash", ".zsh", ".command",
    ".js", ".mjs", ".cjs", ".pyw", ".pyc", ".pyo",
)
# 双扩展名走私：evil.py.txt / run.bat.txt 这种把可执行内容换个末段后缀塞进来。
# 中间任何一段命中可执行后缀（含 .py / .js）就拒——末段是什么都不改变「这是个脚本」。
MOD_SMUGGLE_EXTS = tuple(sorted(set(MOD_FORBIDDEN_EXTS) | set(PY_EXTS) | set(JS_EXTS)))


def _mod_banned_suffix(item: Path) -> str:
    """命中黑名单时返回命中的那一段后缀（'.pyd'），干净文件返回空串。"""
    parts = item.name.lower().split(".")
    if len(parts) < 2:
        return ""
    last = "." + parts[-1]
    if last in MOD_FORBIDDEN_EXTS:
        return last
    for segment in parts[1:-1]:
        if ("." + segment) in MOD_SMUGGLE_EXTS:
            return "." + segment
    return ""


def _mod_forbidden_files(result: dict, folder: Path) -> bool:
    """可执行文件 / 脚本宿主一律拒收并点名（F4）。返回是否命中。"""
    hit_any = False
    for item in sorted(folder.rglob("*")):
        if not item.is_file():
            continue
        suffix = _mod_banned_suffix(item)
        if not suffix:
            continue
        hit_any = True
        result["problems"].append(_problem(
            "mod-forbidden-file",
            "%s 带 %s 后缀：目录 mod 不收可执行文件、动态库和脚本宿主（.py.txt 这种双扩展名也算）"
            % (item.relative_to(folder).as_posix(), suffix),
            "从包里删掉这个文件再重新打包：目录 mod 只接受 plugin.json、.py 源码和普通数据文件"))
    return hit_any


def _scan_mod_sources(result: dict, folder: Path) -> None:
    """逐文件跑 plugin_installer.check_source 的禁用法扫描：入口与包内其它 .py 都算。

    check_source 末尾还要「源码里得引用 PluginBase / PluginMeta」——那是单文件 .py 插件的
    口径（插件必须是个类），目录 mod 的入口是模块不是插件类，这两条对 mod 过严，所以只滤
    掉以「看起来不是插件」开头的那两句；禁用法命中一条都不放过，命中即拒收并报出触发它的
    那一条（子进程/网络/系统调用/动态执行都在 src/plugin_installer.py:53-77 里）。
    """
    mods = _branch_modules(result, "plugin_installer")
    if mods is None:
        return
    check_source = getattr(mods[0], "check_source", None)
    if not callable(check_source):
        result["problems"].append(_problem(
            "installer-missing", "src/plugin_installer.py 里没有 check_source",
            "确认 src/plugin_installer.py 没被改动后重试"))
        return
    _mod_forbidden_files(result, folder)
    for item in sorted(folder.rglob("*.py")):
        if not item.is_file():
            continue
        relative = item.relative_to(folder).as_posix()
        try:
            source = item.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            result["problems"].append(_problem(
                "security-forbidden", "%s 读不出来：%s" % (relative, exc),
                "把包放到本地可读的路径后再试"))
            continue
        try:
            hits = check_source(source) or []
        except Exception as exc:   # 扫描器自己抛了：不能当成「干净」
            result["problems"].append(_problem(
                "security-forbidden", "%s 过不了禁用法扫描：%s: %s"
                % (relative, type(exc).__name__, exc),
                "把这段报错一起发给维护者"))
            continue
        for hit in hits:
            text = str(hit)
            if text.startswith("看起来不是插件"):
                continue
            syntax = text.startswith("不是合法的 Python 代码")
            result["problems"].append(_problem(
                "py-syntax" if syntax else "security-forbidden",
                "%s %s" % (relative, text),
                "先把语法错修好再重新打包" if syntax else
                "禁用法命中就拒收：去掉这一处后重新打包（扫描口径与 src/plugin_installer.py "
                "装 .py 插件时完全一致）"))


def _fill_mod_py(result: dict, found: dict, path, root: str,
                 custom_plugins_dir, data_dir) -> None:
    """目录 mod 的预检：承诺的落点是整目录 <custom_plugins_dir>/<id>/，不是 <id>.py。

    校验与安装走同一份代码（_stage_mcp + _mod_manifest_problems + _scan_mod_sources）：
    预检时解到系统临时目录跑一遍，装的时候再对落盘前的 staging 跑一遍，所以预检说行、
    安装就一定说行，不会出现「预检全绿、一装就拒」。
    """
    manifest = found.get("manifest") or {}
    ident = _text(manifest.get("id"))
    result["id"] = ident
    result["entry"] = _text(manifest.get("entry")) or result["entry"]
    result["tools"] = []
    result["load"] = _not_loaded()
    dest_root = _custom_plugins_root(custom_plugins_dir, data_dir)
    reason = _mod_id_problem(ident)
    if dest_root is not None and ident and not reason:
        result["target_dir"] = str(dest_root / ident)
    if dest_root is None:
        result["problems"].append(_problem(
            "no-target",
            "缺少 custom_plugins_dir：目录 mod 要落到 <custom_plugins_dir>/<id>/",
            "传宿主插件管理器的自定义插件目录（或 data_dir）后再预检；本模块不回落仓库 data/"))
    if reason:
        result["problems"].append(_problem(
            "bad-mod-id", "目录 mod 的 id 不能用：%s" % reason, MOD_HINT))
        return
    source_name = _mod_source_name(path, root)
    if ident and source_name and source_name != ident:
        result["problems"].append(_problem(
            "id_mismatch",
            "plugin.json 的 id 是 %r，但包里这层目录叫 %r：落盘后 manager 按 id == 目录名 核，"
            "必然挂不起来" % (ident, source_name),
            "把目录改名成 %r（或把 id 改成 %r）后重新打包" % (ident, source_name)))
        return
    validator = _mod_validator(result)
    holding = None
    try:
        holding = Path(tempfile.mkdtemp(prefix="mod-inspect-"))
        # 没写 id 时借包内目录名搭个临时落脚点，好让 manager 报 missing_id 而不是
        # 我们自造一条码；这个目录名只活在校验用的临时目录里，落盘永远用 id。
        folder = holding / (ident or source_name or "mod")
        staged = _stage_mcp(path, folder, MOD_MANIFEST, _find_root)
        checked = _mod_manifest_problems(result, validator, folder)
        _scan_mod_sources(result, folder)
        if checked is None:
            return
        result["id"] = _text(checked.get("id")) or result["id"]
        result["name"] = _text(checked.get("name")) or result["name"]
        result["entry"] = _text(checked.get("entry")) or result["entry"]
        result["files"] = int(staged.get("files") or 0)
        result["bytes"] = int(staged.get("bytes") or 0)
        result["installed_but_disabled"] = checked.get("enabled_by_default") is False
        if result["installed_but_disabled"]:
            result["notices"].append(_notice(
                "disabled-by-default",
                "plugin.json 写了 enabled_by_default=false：装上后默认不启用"))
    except _McpReject as exc:
        result["problems"].append(_problem(exc.code, exc.message, exc.hint))
    finally:
        if holding is not None:
            _rmtree(holding)
    if result["target_dir"] and Path(result["target_dir"]).exists():
        result["notices"].append(_notice(
            "overwrite-required",
            "同 id 的目录已经在那里：%s（安装要 overwrite=True，旧目录先挪成 .old-*，失败会挪回）"
            % result["target_dir"]))


def _rollback_hint(stashed: bool, restored: bool, stash, target) -> str:
    """回滚提示只说真话（F3）：旧版本现在到底在哪，按实际结果分三种写。

    数据安全由 _Aside 保证（move 失败就一个字节都不动），但提示词老唱「旧版本已挪回」：
    Windows 下旧目录被占用时 move() 会先失败，那句话就成了假的。
    """
    if not stashed:
        return "确认目标目录没有被占用后重试；旧版本没有被改动（原地没动过）"
    if restored:
        return "确认目标目录没有被占用后重试；旧版本已挪回原位"
    return ("确认目标目录没有被占用后重试；旧版本没挪回来，完整留在 %s，"
            "把它改名回 %s 就能恢复" % (stash or ".old-*", target))


def _install_mod_py(result: dict, path, options: dict) -> None:
    """目录 mod → <custom_plugins_dir>/<id>/（plugin.json + 入口 .py + 其余文件保结构）。

    落盘骨架照 _install_mcp_server：先解到同目录的 .install-* staging，清单校验与禁用法
    扫描都过了才 replace 到目标位；覆盖时旧目录先由 _Aside 挪成 .old-*，中途任何失败都
    挪回来。staging 里那一层目录必须就叫 id —— manager 的 id_mismatch 核的就是目录名。
    """
    dest_root = _custom_plugins_root(options.get("custom_plugins_dir"),
                                     options.get("data_dir"))
    if dest_root is None:
        result["problems"].append(_problem(
            "no-target", "缺少 custom_plugins_dir：目录 mod 要落到 <custom_plugins_dir>/<id>/",
            "传宿主插件管理器的自定义插件目录（或 data_dir）后再装；本模块不回落仓库 data/"))
        return
    ident = str(result["id"] or "")
    if not ident or _mod_id_problem(ident):
        return                  # missing_id / bad-mod-id 已在预检的 problems 里报过，不重复报
    target = dest_root / ident
    if target.exists() and not options["overwrite"]:
        result["problems"].append(_problem(
            "exists", "同 id 的目录 mod 已经装过：%s" % target,
            "确认要替换旧版本就传 overwrite=True（旧目录先挪成 .old-*，失败会挪回）"))
        return
    staging_root = dest_root / ("%s%s" % (MOD_INSTALL_PREFIX, uuid.uuid4().hex))
    staging = staging_root / ident
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        staged = _stage_mcp(path, staging, MOD_MANIFEST, _find_root)
    except _McpReject as exc:
        _rmtree(staging_root)
        result["problems"].append(_problem(exc.code, exc.message, exc.hint))
        return
    except Exception as exc:   # 任何意外：一条 problems，不留半装文件
        _rmtree(staging_root)
        result["problems"].append(_problem(
            "install-failed", "解包失败：%s: %s" % (type(exc).__name__, exc),
            "确认包完整、目标目录可写后重试；旧版本没有被改动"))
        return
    aside = None
    stashed = False
    try:
        checked = _mod_manifest_problems(result, _mod_validator(result), staging)
        if checked is None:
            _rmtree(staging_root)
            return              # 拒载码已进 problems，目标位没动过
        _scan_mod_sources(result, staging)
        if result["problems"]:
            _rmtree(staging_root)
            return              # 禁用法 / 可执行文件命中即拒收：不留半装目录
        aside = _Aside(target) if options["overwrite"] else None
        # move() 自己吞掉 OSError 只返回 False（Windows 上旧目录被占着就是这条路），
        # 返回值必须收下来：回滚提示说不说「已挪回」，全看这一步到底挪没挪成。
        stashed = bool(aside is not None and aside.move())
        try:
            staging.replace(target)
        except OSError as exc:
            _rmtree(staging_root)
            restored = bool(aside is not None and aside.restore())
            result["problems"].append(_problem(
                "install-failed", "目录落盘失败：%s: %s" % (type(exc).__name__, exc),
                _rollback_hint(stashed, restored, getattr(aside, "stash", None), target)))
            return
        if aside is not None:
            aside.commit()
    except Exception as exc:
        _rmtree(staging_root)
        restored = bool(aside is not None and aside.restore())
        result["problems"].append(_problem(
            "install-failed", "安装异常：%s: %s" % (type(exc).__name__, exc),
            _rollback_hint(stashed, restored, getattr(aside, "stash", None), target)))
        return
    _rmtree(staging_root)       # 只剩 .install-* 空壳，一并收掉
    result["landed"] = str(target)
    result["target_dir"] = str(target)
    result["id"] = ident
    result["name"] = _text(checked.get("name")) or result["name"]
    result["entry"] = _text(checked.get("entry")) or result["entry"]
    result["files"] = int(staged.get("files") or result["files"] or 0)
    result["bytes"] = int(staged.get("bytes") or result["bytes"] or 0)
    result["installed_but_disabled"] = checked.get("enabled_by_default") is False
    result["load"] = {"ok": False, "code": "not-loaded",
                      "error": "目录 mod 由 PluginManager 挂载，安装期只做落盘（不 import mod 代码）"}
    result["actions"].append("mod-py: %s" % target)
    result["notices"].append(_notice(
        "mod-mount-pending",
        "已落盘到 %s；下一次 PluginManager 扫描（宿主启动或 sync_mods()）才会挂载它"
        % target))


_BRANCHES = {
    "plugin-py": _install_plugin_py,
    "extension-js": _install_extension_js,
    "skill": _install_skill,
    "feature-pack": _install_feature_pack,
    MCP_KIND: _install_mcp_server,
    MOD_KIND: _install_mod_py,
}
