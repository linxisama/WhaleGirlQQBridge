"""plugin_installer.py - 从网址/仓库安装插件，以及卸载自定义插件。

⚠️ 安全前提（很重要，不要弱化）
远程安装等于**把外部代码装进本程序并会被 import 执行**。所以这里的原则是：

1. **只允许 https**（拒绝 http/其它协议，防中间人替换代码）；
2. **必须显式确认**：调用方（界面）要先拿到 ``inspect_source()`` 的预检结果给用户看，
   再由用户点"确认安装"才走 ``install_from_url(..., confirmed=True)``；
3. **拒绝明显的危险内容**（``os.system`` / ``subprocess`` / ``eval`` / ``exec`` /
   ``socket`` / ``shutil.rmtree`` 等），命中就拒绝并说明命中了哪一条；判断走
   ``ast``/``tokenize``，注释与字符串字面量里的同名文字不算（说明里写一句
   "不要用 os.system" 不该让插件装不进来）；
4. **大小与数量上限**，防止塞一个超大包；
5. 只允许 **单个 .py 文件** 或 **包含 .py 与 plugin.json 的 zip**，其它一律拒绝；
6. 自定义插件与内置插件**分开记账**（``custom_plugins.json``），
   内置插件不允许通过这里删除。

安装器不"理解"插件语义：它只负责把**一个合规的 .py** 落到 ``src/plugins/<id>.py``
并登记到自定义清单；是否真的能用由插件自身与运行时决定。
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
import tokenize
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

# ── 限额 ──────────────────────────────────────────────────────────────────────
MAX_FILE_BYTES = 512 * 1024          # 单个 .py 上限 512KB
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024  # zip 上限 8MB
MAX_ARCHIVE_FILES = 200              # zip 内文件数上限
FETCH_TIMEOUT = 20

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
PLUGIN_ID_HINT = "小写字母开头，只含小写字母/数字/下划线，长度 2-48，例如 my_plugin"
MANIFEST_NAME = "plugin.json"
MANIFEST_MAX_BYTES = 64 * 1024        # plugin.json 上限 64KB
MANIFEST_ENTRY_FIELDS = ("name", "version", "entry")

# 危险用法必须按**真实代码**判断：注释或字符串字面量里写着 os.system 不算危险，
# 反过来，真代码里的调用也不能因为写在说明旁边就被放过。三张表：整模块禁用、
# 属性调用禁用、内置函数调用禁用。
FORBIDDEN_MODULES: Dict[str, str] = {
    "subprocess": "启动子进程 subprocess",
    "socket": "原始网络套接字 socket",
    "ctypes": "调用底层 API ctypes",
    "winreg": "修改注册表 winreg",
}
FORBIDDEN_CALLS: Dict[Tuple[str, str], str] = {
    ("os", "system"): "调用系统命令 os.system",
    ("os", "popen"): "调用系统命令 os.popen",
    ("os", "remove"): "删除文件 os.remove",
    ("os", "unlink"): "删除文件 os.unlink",
    ("shutil", "rmtree"): "递归删除目录 shutil.rmtree",
    ("pickle", "load"): "反序列化 pickle（可执行任意代码）",
    ("pickle", "loads"): "反序列化 pickle（可执行任意代码）",
}
FORBIDDEN_BUILTINS: Dict[str, str] = {
    "eval": "动态执行代码 eval",
    "exec": "动态执行代码 exec",
    "__import__": "动态导入 __import__",
}
# 兜底正则：AST 看不出形状的可疑写法，只对「剥掉注释与字符串」后的文本匹配。
FORBIDDEN_PATTERNS: List[Tuple[str, str]] = [
    (r"base64\.b64decode\s*\([^)]*\)\s*\)?\s*\)?\s*$", "可疑的 base64 载荷"),
]

_LITERAL_TOKENS = {tokenize.COMMENT, tokenize.STRING} | {
    getattr(tokenize, name) for name in ("FSTRING_MIDDLE",) if hasattr(tokenize, name)}


class InstallError(Exception):
    """安装失败（原因面向用户，可直接显示）。

    ``code`` 是给机器看的短标识（missing / bad-zip / encoding …）：预检不再抛异常后，
    它随 ``{'ok': False, 'error_code': ...}`` 一起返回给界面。
    """

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


# ── 源码检查 ──────────────────────────────────────────────────────────────────
def _strip_literals(code: str) -> str:
    """把注释与字符串字面量抹成空格，返回只剩真实代码的文本（行号/长度不变）。"""
    lines = code.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return code          # 剥不掉就当没剥：宁可误报，也不放过
    padded = [list(line) for line in lines]
    for token in tokens:
        if token.type not in _LITERAL_TOKENS:
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            if row - 1 >= len(padded):
                break
            line = padded[row - 1]
            first = start_col if row == start_row else 0
            last = end_col if row == end_row else len(line)
            for index in range(first, min(last, len(line))):
                if line[index] != "\n":
                    line[index] = " "
    return "".join("".join(line) for line in padded)


def _structural_problems(tree) -> List[str]:
    """用 AST 找真实的危险用法；别名导入（import os as o / from os import system）也认。"""
    found: List[str] = []

    def add(reason: str) -> None:
        if reason and reason not in found:
            found.append(reason)

    modules: Dict[str, str] = {}         # 局部名 -> 模块根名
    imported_calls: Dict[str, str] = {}  # from os import system 里的 system -> 原因
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                modules[alias.asname or root] = root
                add(FORBIDDEN_MODULES.get(root, ""))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            root = node.module.split(".")[0]
            add(FORBIDDEN_MODULES.get(root, ""))
            for alias in node.names:
                reason = FORBIDDEN_CALLS.get((root, alias.name), "")
                if reason:
                    imported_calls[alias.asname or alias.name] = reason
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            add(FORBIDDEN_BUILTINS.get(function.id, "")
                or imported_calls.get(function.id, ""))
        elif isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            module = modules.get(function.value.id, function.value.id)
            add(FORBIDDEN_CALLS.get((module, function.attr), "")
                or FORBIDDEN_MODULES.get(module, ""))
    return found


def check_source(code: str) -> List[str]:
    """检查源码是否可接受；返回命中的问题列表（空 = 通过）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"不是合法的 Python 代码：第 {exc.lineno} 行 {exc.msg}"]
    except ValueError as exc:      # 含空字节等二进制垃圾
        return [f"不是合法的 Python 代码：{exc}"]

    python_code = _strip_literals(code)
    hits = _structural_problems(tree)
    for pattern, reason in FORBIDDEN_PATTERNS:
        if reason not in hits and re.search(pattern, python_code):
            hits.append(reason)

    problems = [f"含被禁止的用法：{reason}" for reason in hits]
    # 必须是插件：只认真实代码里的引用，注释里写一句 PluginBase 挡不住也支持不了
    if "PluginBase" not in python_code:
        problems.append("看起来不是插件：没有引用 PluginBase")
    if "PluginMeta" not in python_code:
        problems.append("看起来不是插件：没有引用 PluginMeta")
    return problems


def normalize_plugin_id(raw: str) -> str:
    """规范化插件标识；不合法就抛 InstallError（标识会作为文件名）。"""
    value = str(raw or "").strip().lower().replace("-", "_")
    if not PLUGIN_ID_RE.match(value):
        raise InstallError("插件标识只能用「小写字母开头 + 小写字母/数字/下划线」，长度 2-48")
    return value


def code_fingerprint(code: str) -> str:
    """预检和安装共用的源码指纹。

    绑定的是最终会被写入的 Python 源码，而不是原始文件：zip 里换掉未使用的
    辅助文件不影响安装结果，但入口源码被替换必须重新预检。
    """
    return hashlib.sha256(str(code or "").encode("utf-8")).hexdigest()


def _check_fingerprint(code: str, expected: str) -> None:
    """安装前确认用户确认过的源码没有被换掉。"""
    if expected and expected != code_fingerprint(code):
        raise InstallError("插件内容在预检后已变化，请重新预检并确认后再安装")


# ── 本地文件 ──────────────────────────────────────────────────────────────────
LOCAL_SUFFIXES = (".py", ".zip")


def _read_local(path: Path, limit: int) -> bytes:
    """读取本地文件；路径不合法/读不到都抛 InstallError（消息面向用户）。"""
    if not path.is_file():
        raise InstallError(f"找不到文件：{path}", "missing")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InstallError(f"读取失败：{type(exc).__name__}: {exc}", "io-error") from None
    if size > limit:
        raise InstallError(f"文件过大（{size // 1024}KB），上限 {limit // 1024}KB", "too-large")
    try:
        return path.read_bytes()
    except Exception as exc:
        raise InstallError(f"读取失败：{type(exc).__name__}: {exc}", "io-error") from None


def _write_plugin(path: Path, code: str) -> None:
    """先写完整临时文件再替换，磁盘异常时保留原插件。"""
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(code, encoding='utf-8', newline='\n')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _resolve_local(path: Path) -> str:
    """判断本地文件类型，返回 ``"file"`` 或 ``"archive"``。"""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "file"
    if suffix == ".zip":
        return "archive"
    raise InstallError("本地文件只支持 .py 或 .zip")


# 插件声明通常写成 name="…"（关键字参数），也可能是 "name": "…"（字典），两种都认。
META_FIELD = re.compile(r"""\b(name|description|version|author)\b\s*[:=]\s*['"]([^'"\n]{0,200})['"]""")


def plugin_metadata(code: str) -> Dict:
    """从源码里读插件自述（名称/说明/版本/作者），只用于展示，不执行代码。"""
    result = {}
    for field, value in META_FIELD.findall(code or ''):
        result.setdefault(field, value.strip())
    return result


def declared_plugin_id(code: str) -> str:
    """只读取类中 META = PluginMeta(id=...) 的字面量，不执行插件。"""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return ""
    ids = set()
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for node in cls.body:
            if not isinstance(node, ast.Assign) or not any(
                    isinstance(t, ast.Name) and t.id == 'META' for t in node.targets):
                continue
            call = node.value
            if not isinstance(call, ast.Call) or getattr(call.func, 'id', '') != 'PluginMeta':
                continue
            value = next((kw.value for kw in call.keywords if kw.arg == 'id'), None)
            if value is None and call.args:
                value = call.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                ids.add(value.value)
    if len(ids) > 1:
        raise InstallError('一个文件声明了多个插件，请分别导出为独立插件包再导入')
    return next(iter(ids), '')


def _suggested_id(code: str, filename: str, wanted: str = '') -> str:
    return wanted or normalize_plugin_id(declared_plugin_id(code) or filename)


def expand_local_paths(paths) -> list[Path]:
    """把「文件或文件夹」展开成待检查的插件文件列表。

    文件夹只取顶层 .py（跳过 __init__.py），与"单文件插件"的安装模型一致；
    这样用户把插件文件夹整个拖进来也能装，不必先自己压 zip。
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    if not isinstance(paths, list) or not paths:
        raise InstallError("请至少选择一个插件文件")
    if len(paths) > 50:
        raise InstallError("一次最多导入 50 个插件")
    result = []
    for raw in paths:
        target = Path(str(raw))
        if target.is_dir():
            found = sorted(child for child in target.iterdir()
                           if child.is_file() and child.suffix.lower() in LOCAL_SUFFIXES
                           and child.name != '__init__.py')
            if not found:
                raise InstallError(f"文件夹里没有可导入的 .py / .zip 插件：{target}")
            result.extend(found)
        else:
            result.append(target)
    unique = list(dict.fromkeys(path.resolve() for path in result))
    if len(unique) > 50:
        raise InstallError(f'展开后有 {len(unique)} 个插件，一次最多导入 50 个；请分批选择')
    return unique


def missing_imports(code: str) -> list:
    """静态列出本机没装的第三方依赖；只做提示，不阻断安装。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split('.')[0])
    stdlib = getattr(sys, 'stdlib_module_names', set())
    result = []
    for name in sorted(names):
        if name in stdlib or name in ('src', 'plugins', '__future__'):
            continue
        try:
            if importlib.util.find_spec(name) is None:
                result.append(name)
        except (ImportError, ValueError):
            result.append(name)
    return result


def _inspect_failure(target: Path, kind: str, error: str, error_code: str) -> Dict:
    """预检失败也要给出与成功同形的结果，界面照常渲染，不必区分两套字段。"""
    return {
        "ok": False,
        "kind": kind,
        "resolved_url": f"本地文件：{target}",
        "path": str(target),
        "plugin_id": "",
        "name": "",
        "description": "",
        "version": "",
        "author": "",
        "bytes": 0,
        "lines": 0,
        "fingerprint": "",
        "problems": [error],
        "notices": [],
        "missing": [],
        "existing": False,
        "builtin": False,
        "preview": "",
        "error": error,
        "error_code": error_code,
    }


def _inspect_info(target: Path, kind: str, wanted: str, plugins_dir) -> Dict:
    problems: List[str] = []
    notices: List[str] = []
    manifest: Dict = {}
    if kind == "file":
        data = _read_local(target, MAX_FILE_BYTES)
        code = _decode(data, python=True)
        suggested = _suggested_id(code, target.stem, wanted)
    else:
        data = _read_local(target, MAX_ARCHIVE_BYTES)
        archive_name, code = _pick_from_archive(data, wanted, problems, notices, manifest)
        suggested = _suggested_id(code, archive_name, wanted)

    problems.extend(check_source(code))
    meta = plugin_metadata(code)
    try:
        from .plugins.manager import PLUGIN_MODULES
        builtin = any(str(item) == str(suggested) for item in PLUGIN_MODULES)
    except Exception:
        builtin = False
    return {
        "ok": not problems,
        "kind": kind,
        "resolved_url": f"本地文件：{target}",
        "path": str(target),
        "plugin_id": suggested,
        # 老包（B1 方言）的源码不写版本号，清单里才有：展示优先信源码，其次信清单。
        "name": meta.get("name") or str(manifest.get("name") or "") or suggested,
        "description": meta.get("description") or str(manifest.get("description") or ""),
        "version": meta.get("version") or str(manifest.get("version") or ""),
        "author": meta.get("author") or "",
        "bytes": len(data),
        "lines": code.count("\n") + 1,
        "fingerprint": code_fingerprint(code),
        "problems": problems,
        "notices": notices,
        "missing": missing_imports(code),
        "existing": bool(plugins_dir and (Path(plugins_dir) / f"{suggested}.py").is_file()),
        "builtin": builtin,
        "preview": "\n".join(code.splitlines()[:40]),
        "error": "",
        "error_code": "",
    }


def inspect_local(path, plugin_id: str = "", plugins_dir=None) -> Dict:
    """检查本地文件，**不写入任何文件**。返回给用户确认用的信息。

    与远程版本的区别只有"来源"：安全检查（危险用法、必须是插件）完全一样，
    本地文件同样是要被 import 执行的代码，不能因为"是本地的"就放松。

    扩展名不受支持时仍抛 InstallError（/api/plugins/inspect-local 靠它回 400，
    见 tests/test_plugin_market_routes.py）；其余失败一律返回结构化结果：路径不
    存在、zip 损坏、清单缺失/写坏、编码不对、二进制垃圾都只是"这个包不能用"，
    不该让调用方吃到异常。真正属于我们自己 bug 的异常（NameError 等）继续抛出。
    """
    target = Path(path)
    kind = _resolve_local(target)
    wanted = normalize_plugin_id(plugin_id) if plugin_id else ""
    try:
        return _inspect_info(target, kind, wanted, plugins_dir)
    except InstallError as exc:
        return _inspect_failure(target, kind, str(exc), getattr(exc, "code", "install-error"))
    except (OSError, ValueError, KeyError, RuntimeError, SyntaxError,
            UnicodeError, zipfile.BadZipFile) as exc:
        return _inspect_failure(target, kind, f"预检失败：{type(exc).__name__}: {exc}",
                                "inspect-error")


def install_from_file(path, plugins_dir: Path, plugin_id: str = "",
                      confirmed: bool = False, overwrite: bool = False,
                      fingerprint: str = "") -> Dict:
    """安装本地插件文件。同样必须 ``confirmed=True``。

    为什么本地文件也要确认：它和远程下载的代码一样会被 import 执行，
    而且界面上要先把"这是个什么插件、有没有危险用法"给用户看过。
    """
    if not confirmed:
        raise InstallError("未经确认：请先预检并把结果展示给用户，确认后再安装")

    target = Path(path)
    kind = _resolve_local(target)
    wanted = normalize_plugin_id(plugin_id) if plugin_id else ""

    if kind == "file":
        code = _decode(_read_local(target, MAX_FILE_BYTES), python=True)
        suggested = _suggested_id(code, target.stem, wanted)
    else:
        archive_name, code = _pick_from_archive(
            _read_local(target, MAX_ARCHIVE_BYTES), wanted)
        suggested = _suggested_id(code, archive_name, wanted)

    _check_fingerprint(code, fingerprint)
    problems = check_source(code)
    if problems:
        raise InstallError("安全检查未通过：" + "；".join(problems))

    plugins_dir = Path(plugins_dir)
    plugins_dir.mkdir(parents=True, exist_ok=True)
    destination = plugins_dir / f"{suggested}.py"
    existed = destination.exists()
    if existed and not overwrite:
        raise InstallError(f"已存在同名插件 {suggested}.py；要覆盖请明确选择覆盖")

    backup = ""
    if existed:
        backup = str(destination.with_name(f"{suggested}.py.bak-{uuid.uuid4().hex}"))
        shutil.copy2(destination, backup)
    _write_plugin(destination, code)
    return {"plugin_id": suggested, "path": str(destination), "bytes": len(code),
            "name": plugin_metadata(code).get("name") or suggested,
            "source": f"本地文件：{target}", "overwritten": existed,
            "_backup": backup}


# ── 来源解析 ──────────────────────────────────────────────────────────────────
def resolve_source(url: str) -> Tuple[str, str]:
    """把用户填的地址规范化，返回 ``(kind, url)``。

    ``kind`` 是 ``"file"``（单个 .py）或 ``"archive"``（zip）。
    支持：
    - ``https://.../xxx.zip`` → archive
    - ``https://.../xxx.py``  → file
    - GitHub 仓库页 / raw 页 → 自动转成默认分支的 zip 或 raw 文件
    """
    raw = str(url or "").strip()
    if not raw:
        raise InstallError("请填写插件地址")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise InstallError("只允许 https 地址（防止代码在传输中被替换）")
    if not parsed.netloc:
        raise InstallError("地址不完整")

    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    # GitHub 仓库主页 → 默认分支 zip
    if host in ("github.com", "www.github.com"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1].removesuffix(".git")
            # /blob/<branch>/<file>.py → raw
            if len(parts) >= 4 and parts[2] == "blob":
                branch = parts[3]
                rest = "/".join(parts[4:])
                return "file", f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{rest}"
            if len(parts) >= 4 and parts[2] in ("tree", "releases"):
                return "archive", f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{parts[3]}"
            return "archive", f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/main"
        raise InstallError("GitHub 地址格式不对")

    if path.lower().endswith(".zip"):
        return "archive", raw
    if path.lower().endswith(".py"):
        return "file", raw
    # 末段没有扩展名时按仓库/homepage 处理，尝试当 zip
    raise InstallError("地址需要指向 .py 文件、.zip 压缩包，或 GitHub 仓库页")


def _fetch(url: str, limit: int) -> bytes:
    import httpx

    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
    except Exception as exc:
        raise InstallError(f"下载失败：{type(exc).__name__}: {exc}") from None
    if response.status_code != 200:
        raise InstallError(f"下载失败：HTTP {response.status_code}")
    data = response.content
    if len(data) > limit:
        raise InstallError(f"内容过大（{len(data) // 1024}KB），上限 {limit // 1024}KB")
    return data


def _decode(data: bytes, python: bool = False) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 带 BOM 的 UTF-16：Windows 记事本「Unicode」另存就是这个样子。
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    # 国区最常见的非 UTF-8 源码是 GBK/GB18030。gb18030 几乎不会解码失败，所以对源码额外
    # 要求「解出来仍是合法 Python」，否则宁可报编码错，也不要把乱码当源码装进去。
    try:
        text = data.decode("gb18030")
    except UnicodeDecodeError:
        text = None
    if text is not None and not python:
        return text
    if text is not None:
        try:
            compile(text, "<packed-source>", "exec")
            return text
        except (SyntaxError, ValueError):
            pass
    raise InstallError("文件不是 UTF-8 文本（按 GB18030 也解不出合法源码）", "encoding")


def _text_version(value) -> str:
    """清单里的 version 可能是字符串（新方言）或整数（B1 老方言），统一成展示文本。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()


def _legacy_manifest(meta: Dict) -> bool:
    """B1 老方言：version 是整数，并带 app_version 键（市场 50 个包里 42 个如此）。

    老导出器写的 entry 常常缺失或对不上包内容，所以这类清单允许按包内容定位入口；
    新方言（version 是字符串、没有 app_version）保持严格——写错入口就是写错。
    """
    version = meta.get("version")
    numeric = isinstance(version, (int, float)) and not isinstance(version, bool)
    return numeric or "app_version" in meta


def _archive_read(archive, name: str) -> bytes:
    """读 zip 成员；损坏/加密/CRC 错等读取异常统一收敛成 InstallError，预检不许崩。"""
    try:
        return archive.read(name)
    except Exception as exc:      # zipfile 与 zlib 抛的异常类型不统一，只能宽抓
        raise InstallError(
            f"压缩包内容读取失败：{type(exc).__name__}: {exc}", "bad-zip") from None


def _entry_candidates(archive, manifest_name: str, pys: List[str]) -> List[str]:
    """与清单同目录、可能当入口的 .py（排除 __init__.py 与超大文件）。"""
    folder = Path(manifest_name).parent
    result = []
    for name in pys:
        if Path(name).parent != folder or Path(name).name == "__init__.py":
            continue
        try:
            size = archive.getinfo(name).file_size
        except KeyError:
            continue
        if size <= MAX_FILE_BYTES:
            result.append(name)
    return sorted(result)


def _declares_plugin(archive, name: str) -> bool:
    body = _archive_read(archive, name)
    return b"PluginMeta" in body and b"PluginBase" in body


def _resolve_entry(archive, meta: Dict, manifest_name: str, pys: List[str],
                   notices: List[str]):
    """定位入口源码，返回 (入口文件名, 必须修的问题)。

    清单写了且文件在，就用它；清单写了但文件不在，老方言回退到包内唯一的源码；
    清单没写，只在"包内容无歧义"时回退（唯一源码 / 只有一份声明了插件），
    多份源码都像插件就报错并列出候选，让人补 entry。
    """
    candidates = _entry_candidates(archive, manifest_name, pys)
    markers = [name for name in candidates if _declares_plugin(archive, name)]
    entry = meta.get("entry") or meta.get("main")
    if entry is None:
        notices.append("plugin.json 缺少必填字段 entry（插件源码文件名，例如 main.py）")
        if len(candidates) == 1:
            return candidates[0], []
        if len(markers) == 1:
            return markers[0], []
        if not candidates:
            return None, ["压缩包里没有可用的 .py 入口（只有 __init__.py）"]
        shown = "、".join(Path(name).as_posix() for name in (markers or candidates))
        return None, [
            f"压缩包里有多份源码（{shown}），plugin.json 也没有声明 entry："
            "无法确定安装哪个，请补一份 entry"]
    if not isinstance(entry, str) or not entry.strip():
        return None, [f"plugin.json 的 entry 必须是非空字符串，收到 {entry!r}"]

    declared = (Path(manifest_name).parent / entry.strip()).as_posix()
    if declared in pys:
        return declared, []
    fallback = candidates or [name for name in pys if Path(name).name != "__init__.py"]
    if _legacy_manifest(meta):
        if len(fallback) == 1:
            notices.append(
                f"plugin.json 声明的入口 {entry} 不存在，已回退到包内唯一的源码 "
                + Path(fallback[0]).name)
            return fallback[0], []
        shown = "、".join(Path(name).as_posix() for name in (fallback or pys))
        if shown:
            return None, [
                f"plugin.json 声明的 Python 入口不存在：{entry}；包内有 "
                f"{len(fallback or pys)} 份源码（{shown}），无法确定安装哪个，请修正 entry"]
    return None, [f"plugin.json 声明的 Python 入口不存在：{entry}"]


def _check_manifest(archive, manifest_name: str, pys: List[str]):
    """校验一份 plugin.json，返回 (必须修, 只提示, 入口文件, 声明的 id, 清单摘要)。

    两项都是给人看的中文句子，都会进预检的 problems（界面红字）。区别在安装时：
    "必须修"会拦下安装，"只提示"不拦——历史上缺 name/version 的包一直能装，
    老包和新写的简化包不能被这次收紧弄坏。

    清单摘要给预检展示用：老包的源码里往往没写版本号，只能信清单。
    """
    blocking: List[str] = []
    notices: List[str] = []
    try:
        raw = _archive_read(archive, manifest_name)
    except InstallError as exc:
        return [f"{manifest_name} 读取失败：{exc}"], notices, None, "", {}
    if len(raw) > MANIFEST_MAX_BYTES:
        return [f"plugin.json 过大（{len(raw) // 1024}KB，上限 64KB）"], notices, None, "", {}
    try:
        text = _decode(raw)
    except InstallError:
        return ["plugin.json 不是 UTF-8 文本"], notices, None, "", {}
    try:
        meta = json.loads(text)
    except ValueError as exc:
        return [f"plugin.json 不是有效的 JSON：{exc}"], notices, None, "", {}
    if not isinstance(meta, dict):
        return ['plugin.json 的顶层必须是一个 JSON 对象（形如 {"name": ...}）'], notices, None, "", {}

    manifest_id = ""
    declared_id = meta.get("id")
    if declared_id is not None:
        if not isinstance(declared_id, str):
            blocking.append(
                f"plugin.json 的 id 必须是字符串，收到 {type(declared_id).__name__}")
        elif not PLUGIN_ID_RE.match(declared_id):
            blocking.append(
                f"plugin.json 的 id 不合法：{declared_id!r}（要求 {PLUGIN_ID_HINT}）")
        else:
            manifest_id = declared_id

    name = meta.get("name")
    if name is None:
        notices.append("plugin.json 缺少必填字段 name")
    elif not isinstance(name, str) or not name.strip():
        blocking.append(f"plugin.json 的 name 必须是非空字符串，收到 {name!r}")

    # B1 老方言把 version 写成整数（1），并用 app_version 记宿主版本：那是正常清单，
    # 不是坏清单，照常解析；只有 true/false、空串、对象这类才是真写错。
    version = meta.get("version")
    if version is None:
        notices.append("plugin.json 缺少必填字段 version")
    elif isinstance(version, bool) or not isinstance(version, (str, int, float)):
        blocking.append(f"plugin.json 的 version 必须是字符串或整数，收到 {version!r}")
    elif isinstance(version, str) and not version.strip():
        blocking.append(f"plugin.json 的 version 必须是非空字符串，收到 {version!r}")
    app_version = meta.get("app_version")
    if app_version is not None and not isinstance(app_version, str):
        notices.append(f"plugin.json 的 app_version 建议写成字符串，收到 {app_version!r}")

    entry_target, entry_problems = _resolve_entry(
        archive, meta, manifest_name, pys, notices)
    blocking.extend(entry_problems)
    summary = {
        "id": manifest_id,
        "name": name.strip() if isinstance(name, str) else "",
        "version": _text_version(version),
        "entry": entry_target or "",
        "legacy": _legacy_manifest(meta),
    }
    return blocking, notices, entry_target, manifest_id, summary


_IMPORT_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+(\.*)([A-Za-z_][\w.]*)?[ \t]+import[ \t]+([\w., \t]+)"
    r"|import[ \t]+([\w., \t]+))",
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


def _dropped_sources(archive, target: str) -> List[str]:
    """入口 import 了、但单文件落盘会丢掉的包内模块。

    只认「入口真 import 了的」：包里多一个没被 import 的 helper.py 不影响加载（既有用例
    tests/test_plugin_import_reliability.py:63-70 就是这种包），而被 import 的兄弟模块一丢，
    插件加载就 ImportError。
    """
    parent = target.rsplit("/", 1)[0] if "/" in target else ""
    prefix = parent + "/" if parent else ""
    modules: Dict[str, str] = {}
    for name in archive.namelist():
        if name.endswith("/") or name == target or not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if rel.lower().endswith(".py"):
            modules[rel[:-3]] = rel
        elif "/" in rel:
            modules.setdefault(rel.split("/")[0], rel)
    if not modules:
        return []
    try:
        source = _decode(_archive_read(archive, target), python=True)
    except InstallError:
        return []
    return [modules[name] for name in sorted(_imported_names(source)) if name in modules]


def _pick_from_archive(data: bytes, wanted: Optional[str],
                       problems: Optional[List[str]] = None,
                       notices_out: Optional[List[str]] = None,
                       meta_out: Optional[Dict] = None) -> Tuple[str, str]:
    """从 zip 里挑出插件源文件，返回 ``(filename, code)``。

    ``problems`` 传入列表表示"这是预检，不要抛异常"：结构性问题全部写进列表给界面
    标红，同时仍尽量挑一个文件出来做预览。传 ``None``（安装路径）时，必须修的问题
    直接抛 ``InstallError``，安装因此不会因为包结构不对而落半个文件。

    ``notices_out`` 传入列表时，只提醒、不挡安装的提示写进它，``problems`` 只留必须
    修的问题（界面据此决定能不能装）；不传则两类都进 ``problems``（旧行为不变）。

    ``meta_out`` 传入 dict 时，把清单里解析出的 id/name/version/entry 写进去供预检
    展示：老包的源码里常常没写版本号，只能信清单。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        raise InstallError("这不是一个有效的 zip 压缩包", "bad-zip") from None

    names = [n for n in archive.namelist() if not n.endswith("/")]
    if len(archive.infolist()) > MAX_ARCHIVE_FILES:
        raise InstallError(f"压缩包内文件过多（{len(names)} 个）", "too-many-entries")
    if sum(info.file_size for info in archive.infolist()) > 24 * 1024 * 1024:
        raise InstallError('压缩包解压后过大（上限 24MB）', "too-large")
    # 防目录穿越
    for name in names:
        if ".." in Path(name).parts or Path(name).is_absolute():
            raise InstallError(f"压缩包内含可疑路径：{name}", "unsafe-path")

    pys = [n for n in names if n.lower().endswith(".py")]
    if not pys:
        raise InstallError("压缩包里没有 .py 文件；QQ Agent/Node 扩展需要通过 QQ Agent 能力桥接入，"
                           "不能作为 Python 插件安装", "no-source")

    target = None
    blocking: List[str] = []
    notices: List[str] = []
    manifests = [name for name in names if Path(name).name.lower() == MANIFEST_NAME]
    if len(manifests) > 1:
        blocking.append(
            "压缩包里有多份 plugin.json（" + "、".join(sorted(manifests))
            + "）：一个插件包只能有一份，无法判断以哪个为准")
    elif len(manifests) == 1:
        blocking, notices, target, manifest_id, summary = _check_manifest(
            archive, manifests[0], pys)
        if meta_out is not None:
            meta_out.update(summary)
        if target is None and manifest_id:
            target = next((name for name in pys if Path(name).stem == manifest_id), None)
    if wanted:
        for name in pys:
            if Path(name).stem.lower() == wanted:
                target = name
                break
    if target is None:
        # 优先 index.py / main.py，其次任何不是 __init__ 的
        for preferred in ("index.py", "main.py", "plugin.py"):
            for name in pys:
                if Path(name).name.lower() == preferred:
                    target = name
                    break
            if target:
                break
    if target is None:
        # 仓库中可能先出现测试/辅助文件；优先真正声明插件的源码。
        candidates = [n for n in pys if Path(n).name != "__init__.py"
                      and archive.getinfo(n).file_size <= MAX_FILE_BYTES
                      and _declares_plugin(archive, n)]
        if not candidates:
            candidates = [n for n in pys if Path(n).name != '__init__.py']
        target = (candidates or pys)[0]

    dropped = _dropped_sources(archive, target)
    if dropped:
        # 真因：下面只从包里取回一个 .py 落成 <id>.py，其余源码全丢。入口 import 了被丢的模块，
        # 装完必然 ImportError——预检要说清，真装要拒收。
        message = ("入口 %s 还 import 了包内其它源码（%s）：插件目录只放单文件 <id>.py，"
                   "装进去这些模块会被丢掉，插件一加载就 ImportError。把它们并进入口文件，"
                   "或改成目录 mod（plugin.json 写 api_version=1）"
                   % (target, "、".join(dropped[:4])))
        if problems is not None:
            problems.append(message)
        else:
            raise InstallError(message, "multi-source")

    if problems is not None:
        problems.extend(blocking)
        if notices_out is None:
            problems.extend(notices)
        else:
            notices_out.extend(notices)
        if not manifests and not wanted and not any(
                Path(n).name.lower() in ("index.py", "main.py", "plugin.py") for n in pys):
            # 没有 plugin.json 时只能靠文件名猜：猜得出就放行（单文件包历来如此），
            # 猜不出（多份源码都声明了插件）就明确要求补一份清单。
            markers = [n for n in pys if Path(n).name != "__init__.py"
                       and archive.getinfo(n).file_size <= MAX_FILE_BYTES
                       and _declares_plugin(archive, n)]
            if len(markers) > 1:
                problems.append(
                    "压缩包里没有 plugin.json，且多份源码都声明了插件（"
                    + "、".join(sorted(markers))
                    + "）：无法确定安装哪个，请补一份 plugin.json 声明 name、version、entry")
    elif blocking:
        raise InstallError("；".join(blocking), "invalid-package")

    if archive.getinfo(target).file_size > MAX_FILE_BYTES:
        raise InstallError("插件源文件过大", "too-large")
    return Path(target).stem, _decode(_archive_read(archive, target), python=True)


# ── 预检（给界面显示，不落盘） ────────────────────────────────────────────────
def inspect_source(url: str, plugin_id: str = "") -> Dict:
    """下载并检查，但**不写入任何文件**。返回给用户确认用的信息。"""
    kind, resolved = resolve_source(url)
    wanted = normalize_plugin_id(plugin_id) if plugin_id else ""

    problems: List[str] = []
    notices: List[str] = []
    manifest: Dict = {}
    if kind == "file":
        data = _fetch(resolved, MAX_FILE_BYTES)
        name = Path(urlparse(resolved).path).stem
        code = _decode(data, python=True)
        suggested = _suggested_id(code, name or 'download', wanted)
    else:
        data = _fetch(resolved, MAX_ARCHIVE_BYTES)
        name, code = _pick_from_archive(data, wanted, problems, notices, manifest)
        suggested = _suggested_id(code, name, wanted)

    problems.extend(check_source(code))
    meta = plugin_metadata(code)
    return {
        "ok": not problems,
        "kind": kind,
        "resolved_url": resolved,
        "plugin_id": suggested,
        # 与本地预检一致：源码里没写版本号时用清单里的（老包都没写）。
        "name": meta.get("name") or str(manifest.get("name") or "") or suggested,
        "version": meta.get("version") or str(manifest.get("version") or ""),
        "bytes": len(data),
        "lines": code.count("\n") + 1,
        "fingerprint": code_fingerprint(code),
        "problems": problems,
        "notices": notices,
        "preview": "\n".join(code.splitlines()[:40]),
    }


# ── 安装 / 卸载 ───────────────────────────────────────────────────────────────
def install_from_url(url: str, plugins_dir: Path, plugin_id: str = "",
                     confirmed: bool = False, overwrite: bool = False,
                     fingerprint: str = "") -> Dict:
    """下载并安装插件。

    ``confirmed`` 必须为 True —— 这是有意设置的"人工闸门"：远程代码会被 import 执行，
    不允许静默安装。
    """
    if not confirmed:
        raise InstallError("未经确认：请先预检并把结果展示给用户，确认后再安装")

    kind, resolved = resolve_source(url)
    wanted = normalize_plugin_id(plugin_id) if plugin_id else ""

    if kind == "file":
        code = _decode(_fetch(resolved, MAX_FILE_BYTES), python=True)
        suggested = _suggested_id(code, Path(urlparse(resolved).path).stem, wanted)
    else:
        name, code = _pick_from_archive(_fetch(resolved, MAX_ARCHIVE_BYTES), wanted)
        suggested = _suggested_id(code, name, wanted)

    _check_fingerprint(code, fingerprint)
    problems = check_source(code)
    if problems:
        raise InstallError("安全检查未通过：" + "；".join(problems))

    plugins_dir = Path(plugins_dir)
    plugins_dir.mkdir(parents=True, exist_ok=True)
    target = plugins_dir / f"{suggested}.py"
    if target.exists() and not overwrite:
        raise InstallError(f"已存在同名插件 {suggested}.py；要覆盖请明确选择覆盖")

    existed = target.exists()
    backup = ""
    if existed:
        backup = str(target.with_name(f"{suggested}.py.bak-{uuid.uuid4().hex}"))
        shutil.copy2(target, backup)
    _write_plugin(target, code)
    return {"plugin_id": suggested, "path": str(target), "bytes": len(code),
            "source": resolved, "overwritten": existed, "_backup": backup}
