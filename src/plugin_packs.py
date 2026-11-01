"""src/plugin_packs.py — 插件包目录的只读扫描与校验（纯标准库，不依赖 Qt）。

为什么要单独一层：`plugin_packages/` 是「用户自己手动放进去」的包仓库，宿主挂载前需要
先回答两个问题——这个包能不能挂、坏在哪里。这里只读：不安装、不写宿主状态、不 import
包里的任何代码。校验口径对齐 `src/plugins/manager.py:808` 的 `_validate_mod_manifest`
（id 必须等于目录名、api_version 必须是 1、entry 必须在包内且真实存在），再补上安装器
认的 `host` / `min_host_version`（参照 `plugin_packages/voice_console/plugin.json:7-8`）。

返回的都是纯 dict，便于 `/api/plugins/custom` 之类的路由直接序列化。
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

MANIFEST_NAME = "plugin.json"
SKIP_DIR_NAMES = frozenset({"__pycache__"})

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$")
ENTRY_RE = re.compile(r"^(?P<path>[^:]+\.py)(?::(?P<func>[A-Za-z_][A-Za-z0-9_]*))?$")

REQUIRED_FIELDS = ("id", "name", "version", "entry", "permissions", "description")
OPTIONAL_FIELDS = ("api_version", "host", "min_host_version", "depends",
                   "enabled_by_default", "category", "license", "source")

# 与 src/plugins/__init__.py:197-206 同源：目录 mod 声明的 api 版本与权限取值。
MOD_API_VERSION = 1
MOD_PERMISSIONS = frozenset({
    "read_message", "send_message", "store_data", "network", "manage_settings",
    "register_tool",
})
# 已登记过的宿主标识（src/runtime_identity.py:4 APP_ID 与安装器侧的 Bridge 写法）。
KNOWN_HOSTS = ("WhaleGirlQQ", "WhaleGirlQQBridge")

# 上限：清单 64KB 与 plugin_installer 同口径；ZIP 规模挡住"解压炸弹"式的误放。
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ZIP_FILES = 2000
MAX_ZIP_BYTES = 64 * 1024 * 1024

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


# ── 路径与宿主版本 ──────────────────────────────────────────────────────────
def repo_root() -> Path:
    """仓库根（本文件在 <root>/src/ 下）。"""
    return Path(__file__).resolve().parents[1]


def default_packs_dir(root=None) -> Path:
    return (Path(root) if root is not None else repo_root()) / "plugin_packages"


def host_version(root=None) -> str:
    """宿主版本：先读根目录 version.txt，读不到退回 src/runtime_identity.APP_VERSION。"""
    base = Path(root) if root is not None else repo_root()
    try:
        text = (base / "version.txt").read_text("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    if text:
        return text
    try:
        from .runtime_identity import APP_VERSION
        return str(APP_VERSION)
    except Exception:
        return ""


def _version_key(text):
    """'5.2.100' -> (5,2,100)；解析不了返回 None（不猜）。"""
    matched = re.match(r"^\s*(\d+(?:\.\d+)*)", str(text or ""))
    if not matched:
        return None
    return tuple(int(part) for part in matched.group(1).split("."))


def _is_unsafe_rel(rel) -> bool:
    """相对路径是否越界：绝对路径、盘符、或含 .. 段。"""
    text = str(rel or "").replace("\\", "/").strip()
    if not text:
        return True
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return True
    return ".." in text.split("/")


def _issue(code, message, severity=SEVERITY_ERROR, field_name=None) -> dict:
    return {"code": code, "message": message, "severity": severity, "field": field_name}


def _pack(*, source, path, dir_name="", data=None, issues=None) -> dict:
    """把一份（可能残缺的）清单折成统一形状；ok 只看有没有 error 级问题。"""
    data = data if isinstance(data, dict) else {}
    issues = list(issues or [])

    def text_of(key):
        value = data.get(key)
        return value if isinstance(value, str) else ""

    permissions = data.get("permissions")
    depends = data.get("depends")
    pack = {
        "id": text_of("id") or (dir_name or ""),
        "name": text_of("name"),
        "version": text_of("version"),
        "entry": text_of("entry"),
        "host": text_of("host"),
        "min_host_version": text_of("min_host_version"),
        "permissions": [item for item in permissions if isinstance(item, str)]
        if isinstance(permissions, list) else [],
        "description": text_of("description"),
        "api_version": data.get("api_version"),
        "depends": [item for item in depends if isinstance(item, str)]
        if isinstance(depends, list) else [],
        "source": source,
        "path": str(path),
        "issues": issues,
    }
    pack["ok"] = not any(item["severity"] == SEVERITY_ERROR for item in issues)
    return pack


# ── 清单校验（目录与 ZIP 共用）──────────────────────────────────────────────
def validate_manifest(data, *, dir_name=None, host_version_text="", entry_exists=None):
    """校验一份 plugin.json 的内容，返回问题列表（空列表＝合格）。

    entry_exists 是回调：目录扫描传"文件在不在包里"，ZIP 扫描传"名字在不在 namelist"，
    这样两边口径一致，也说明本模块**从不落盘、从不执行入口**。
    """
    issues = []

    def err(code, message, field_name=None):
        issues.append(_issue(code, message, SEVERITY_ERROR, field_name))

    def warn(code, message, field_name=None):
        issues.append(_issue(code, message, SEVERITY_WARNING, field_name))

    if not isinstance(data, dict):
        err("bad-manifest", "plugin.json 顶层必须是对象 {…}")
        return issues

    for field_name in REQUIRED_FIELDS:
        if field_name not in data:
            err("missing-field", "缺必填字段 %s" % field_name, field_name)

    if "id" in data:
        plugin_id = data["id"]
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            err("bad-id", "id 必须是非空字符串", "id")
        elif not ID_RE.match(plugin_id):
            err("bad-id",
                "id 必须匹配 ^[a-z0-9][a-z0-9_-]{0,63}$，收到 %r" % (plugin_id,), "id")
        elif dir_name is not None and plugin_id != dir_name:
            err("id-mismatch",
                "id %r 与目录名 %r 不一致（管理器按这条拒载）"
                % (plugin_id, dir_name), "id")

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            err("bad-field", "name 必须是非空字符串", "name")

    if "version" in data:
        version = data["version"]
        if not isinstance(version, str):
            err("bad-version", "version 必须是字符串（例 1.0.0）", "version")
        elif not VERSION_RE.match(version):
            err("bad-version", "version 必须是 semver（例 1.0.0），收到 %r" % (version,),
                "version")

    if "description" in data and not isinstance(data["description"], str):
        err("bad-field", "description 必须是字符串", "description")

    if "permissions" in data:
        permissions = data["permissions"]
        if not isinstance(permissions, list):
            err("bad-permissions", "permissions 必须是字符串数组", "permissions")
        else:
            broken = [item for item in permissions
                      if not isinstance(item, str) or not item.strip()]
            if broken:
                err("bad-permissions", "permissions 里有非字符串/空项：%r" % (broken,),
                    "permissions")
            unknown = sorted({item for item in permissions
                              if isinstance(item, str) and item.strip()}
                             - MOD_PERMISSIONS)
            if unknown:
                warn("unknown-permission",
                     "permissions 含声明清单外的取值 %s：只作声明，宿主不会据此拦截动作"
                     % (unknown,), "permissions")

    if "entry" in data:
        entry = data["entry"]
        matched = ENTRY_RE.match(entry) if isinstance(entry, str) else None
        if isinstance(entry, str) and not entry.strip():
            err("missing-entry", "entry 必须是非空相对路径字符串", "entry")
        elif matched is None:
            err("bad-entry",
                "entry 必须是包内 .py 相对路径（可写 文件.py:函数名），收到 %r" % (entry,),
                "entry")
        else:
            rel = matched.group("path")
            if _is_unsafe_rel(rel):
                err("entry-escape",
                    "entry %r 越出包目录（不许绝对路径或 ..）" % (rel,), "entry")
            elif entry_exists is not None and not entry_exists(rel):
                err("entry-missing", "entry %r 指向的文件在包里不存在" % (rel,), "entry")

    if "api_version" in data:
        api_version = data["api_version"]
        if isinstance(api_version, bool) or not isinstance(api_version, int):
            err("bad-api-version", "api_version 必须是整数", "api_version")
        elif api_version != MOD_API_VERSION:
            err("api-version-unsupported",
                "api_version=%s 不受支持，目录 mod 只认 %s"
                % (api_version, MOD_API_VERSION), "api_version")
    else:
        warn("missing-api-version",
             "没有 api_version：按目录 mod 挂载时必须是 %s（本条只提示包形态）"
             % MOD_API_VERSION, "api_version")

    if "host" in data:
        host = data["host"]
        if not isinstance(host, str) or not host.strip():
            err("bad-field", "host 必须是非空字符串", "host")
        elif host not in KNOWN_HOSTS:
            warn("unknown-host", "host=%r 不在已登记取值 %s 里" % (host, list(KNOWN_HOSTS)),
                 "host")

    if "min_host_version" in data:
        minimum = data["min_host_version"]
        if not isinstance(minimum, str) or not minimum.strip():
            err("bad-min-host-version", "min_host_version 必须是非空字符串（例 5.1.2）",
                "min_host_version")
        else:
            need = _version_key(minimum)
            have = _version_key(host_version_text)
            if need is None:
                err("bad-min-host-version",
                    "min_host_version=%r 不是 x.y.z 形式" % (minimum,), "min_host_version")
            elif have is None:
                warn("host-version-unknown",
                     "读不到宿主版本，未校验 min_host_version=%s" % minimum,
                     "min_host_version")
            elif need > have:
                err("host-too-old",
                    "需要宿主 ≥ %s，当前宿主是 %s" % (minimum, host_version_text),
                    "min_host_version")

    if "depends" in data:
        depends = data["depends"]
        if not isinstance(depends, list) or any(
                not isinstance(item, str) or not item.strip() for item in depends):
            err("bad-depends", "depends 必须是 mod id 字符串数组", "depends")

    return issues


# ── 目录包 ──────────────────────────────────────────────────────────────────
def inspect_dir(pack_dir, *, host_version_text="", dir_name=None, source="dir") -> dict:
    """只读校验一个包目录；任何一步失败都折成该包自己的 issues，不抛异常。"""
    pack_dir = Path(pack_dir)
    name = dir_name if dir_name is not None else pack_dir.name
    manifest = pack_dir / MANIFEST_NAME
    if not pack_dir.is_dir():
        return _pack(source=source, path=pack_dir, dir_name=name,
                     issues=[_issue("dir-missing", "包目录不存在：%s" % pack_dir)])
    if not manifest.is_file():
        return _pack(source=source, path=pack_dir, dir_name=name,
                     issues=[_issue("missing-manifest",
                                    "%s 下没有 %s" % (name, MANIFEST_NAME))])
    try:
        size = manifest.stat().st_size
    except OSError as exc:
        return _pack(source=source, path=pack_dir, dir_name=name,
                     issues=[_issue("unreadable-manifest", "清单读不到：%s" % exc)])
    if size > MAX_MANIFEST_BYTES:
        return _pack(source=source, path=pack_dir, dir_name=name,
                     issues=[_issue("manifest-too-large",
                                    "%s 是 %d 字节，超过上限 %d"
                                    % (MANIFEST_NAME, size, MAX_MANIFEST_BYTES))])
    try:
        text = manifest.read_text("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _pack(source=source, path=pack_dir, dir_name=name,
                     issues=[_issue("bad-json",
                                    "%s 必须是 UTF-8 且可读：%s" % (MANIFEST_NAME, exc))])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return _pack(source=source, path=pack_dir, dir_name=name,
                     issues=[_issue("bad-json",
                                    "%s 不是合法 JSON：%s" % (MANIFEST_NAME, exc))])

    root = pack_dir.resolve()

    def entry_exists(rel):
        target = (root / rel).resolve()
        return root in target.parents and target.is_file()

    issues = validate_manifest(data, dir_name=name,
                               host_version_text=host_version_text,
                               entry_exists=entry_exists)
    pack = _pack(source=source, path=pack_dir, dir_name=name, data=data, issues=issues)
    pack["files"] = sum(1 for item in pack_dir.rglob("*") if item.is_file())
    return pack


# ── ZIP 包 ──────────────────────────────────────────────────────────────────
def inspect_zip(zip_path, *, host_version_text="") -> dict:
    """只探测不解压：从 namelist 判定包根、读清单、按名字确认入口。"""
    zip_path = Path(zip_path)
    stem = zip_path.stem
    if not zip_path.is_file():
        return _pack(source="zip", path=zip_path, dir_name=stem,
                     issues=[_issue("dir-missing", "ZIP 不存在：%s" % zip_path)])
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos if not info.is_dir()]
            total = sum(info.file_size for info in infos)
            if len(names) > MAX_ZIP_FILES:
                return _pack(source="zip", path=zip_path, dir_name=stem,
                             issues=[_issue("zip-too-many-files",
                                            "ZIP 里 %d 个文件，超过上限 %d"
                                            % (len(names), MAX_ZIP_FILES))])
            if total > MAX_ZIP_BYTES:
                return _pack(source="zip", path=zip_path, dir_name=stem,
                             issues=[_issue("zip-too-large",
                                            "解压后 %d 字节，超过上限 %d"
                                            % (total, MAX_ZIP_BYTES))])
            unsafe = sorted(name for name in names if _is_unsafe_rel(name))
            if unsafe:
                return _pack(source="zip", path=zip_path, dir_name=stem,
                             issues=[_issue("zip-unsafe-path",
                                            "ZIP 里有越界路径 %s（不许绝对路径或 ..）"
                                            % (unsafe[:3],))])
            candidates = []
            if MANIFEST_NAME in names:
                candidates.append(("", MANIFEST_NAME))
            for name in names:
                if name.count("/") == 1 and name.endswith("/" + MANIFEST_NAME):
                    candidates.append((name.split("/")[0], name))
            if not candidates:
                return _pack(source="zip", path=zip_path, dir_name=stem,
                             issues=[_issue("missing-manifest",
                                            "ZIP 根目录或唯一顶层目录里没有 %s"
                                            % MANIFEST_NAME)])
            if len(candidates) > 1:
                return _pack(source="zip", path=zip_path, dir_name=stem,
                             issues=[_issue("zip-multiple-packs",
                                            "ZIP 里有多个 %s（%s），一次只装一个包"
                                            % (MANIFEST_NAME,
                                               sorted(item[1] for item in candidates)))])
            prefix, manifest_name = candidates[0]
            raw = archive.read(manifest_name)
            if len(raw) > MAX_MANIFEST_BYTES:
                return _pack(source="zip", path=zip_path, dir_name=stem,
                             issues=[_issue("manifest-too-large",
                                            "%s 是 %d 字节，超过上限 %d"
                                            % (manifest_name, len(raw),
                                               MAX_MANIFEST_BYTES))])
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        return _pack(source="zip", path=zip_path, dir_name=stem,
                     issues=[_issue("bad-zip", "不是可读的 ZIP：%s" % exc)])

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _pack(source="zip", path=zip_path, dir_name=stem,
                     issues=[_issue("bad-json", "清单必须是 UTF-8：%s" % exc)])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return _pack(source="zip", path=zip_path, dir_name=stem,
                     issues=[_issue("bad-json", "清单不是合法 JSON：%s" % exc)])

    inner = set(names)

    def entry_exists(rel):
        return ("%s/%s" % (prefix, rel) if prefix else rel) in inner

    issues = validate_manifest(data, dir_name=prefix or None,
                               host_version_text=host_version_text,
                               entry_exists=entry_exists)
    pack = _pack(source="zip", path=zip_path, dir_name=prefix or stem, data=data,
                 issues=issues)
    pack["files"] = len(names)
    pack["bytes"] = total
    return pack


# ── 目录级扫描 ──────────────────────────────────────────────────────────────
def scan(directory=None, *, root=None, host_version_text=None, include_zip=True) -> dict:
    """扫一个包目录：目录包 + ZIP 包，逐包给细节，再筛重复 id。

    一个包坏掉只影响它自己：其它包照样出现在返回列表里，report["ok"] 才是整体结论。
    """
    base = Path(directory) if directory is not None else default_packs_dir(root)
    if host_version_text is None:
        host_version_text = host_version(root)
    report = {
        "directory": str(base),
        "host_version": host_version_text,
        "packs": [],
        "issues": [],
        "ok": True,
    }
    if not base.is_dir():
        report["issues"].append(_issue("dir-missing", "插件包目录不存在：%s" % base))
        report["ok"] = False
        report["counts"] = {"packs": 0, "ok": 0, "bad": 0}
        return report
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        report["issues"].append(_issue("dir-unreadable", "插件包目录读不了：%s" % exc))
        report["ok"] = False
        report["counts"] = {"packs": 0, "ok": 0, "bad": 0}
        return report

    for item in entries:
        if item.name.startswith(".") or item.name in SKIP_DIR_NAMES:
            continue
        try:
            if item.is_dir():
                report["packs"].append(
                    inspect_dir(item, host_version_text=host_version_text))
            elif include_zip and item.suffix.lower() == ".zip":
                report["packs"].append(
                    inspect_zip(item, host_version_text=host_version_text))
        except OSError as exc:
            report["packs"].append(_pack(
                source="dir" if item.is_dir() else "zip", path=item,
                dir_name=item.stem,
                issues=[_issue("dir-unreadable", "读不了：%s" % exc)]))

    seen = {}
    for pack in report["packs"]:
        pack_id = pack.get("id") or ""
        if not pack_id:
            continue
        if pack_id in seen:
            pack["issues"].append(_issue(
                "duplicate-id",
                "id %r 与 %s 重复（先出现者保留）" % (pack_id, seen[pack_id]["path"]),
                SEVERITY_ERROR, "id"))
            pack["ok"] = False
            report["issues"].append(_issue(
                "duplicate-id",
                "id %r 出现多次：%s 与 %s"
                % (pack_id, seen[pack_id]["path"], pack["path"])))
        else:
            seen[pack_id] = pack

    bad = [pack for pack in report["packs"] if not pack["ok"]]
    report["counts"] = {"packs": len(report["packs"]),
                        "ok": len(report["packs"]) - len(bad),
                        "bad": len(bad)}
    report["ok"] = not bad and not any(
        item["severity"] == SEVERITY_ERROR for item in report["issues"])
    return report


def main(argv=None) -> int:
    """预检 CLI：python -m src.plugin_packs [目录]。只打印，不安装。"""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    report = scan(args[0] if args else None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
