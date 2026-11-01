"""QQ Agent 市集目录同步与 Node 扩展包落盘。

市集页面是纯前端，真正的清单来自 ``/api/community/plugins``。这个模块只做
两件低风险的事：把清单缓存到本地；把用户明确选择的 ZIP 安全地放进
``data/qqagent_extensions``，交给 qqagent_bridge 的扩展目录使用。

不在这里执行 Node 代码，也不把下载包当成 Python 插件导入。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

MARKET_URL = "https://kondius.cn/api/community/plugins"
MARKET_PAGE = "https://kondius.cn/qq-agent/plugin-market/"
MARKET_ORIGIN = "https://kondius.cn"
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 96 * 1024 * 1024
MAX_ARCHIVE_FILES = 600
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _clean_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _ID_RE.fullmatch(text) else ""


def _download_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    url = urljoin(MARKET_ORIGIN + "/", raw)
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.netloc != "kondius.cn":
        return ""
    if not parts.path.startswith("/api/community/plugins/"):
        return ""
    if not parts.path.endswith("/download"):
        return ""
    return url


def _external_url(value: Any) -> str:
    """导入链接只允许外部 https；挡掉本机/内网地址，避免被当成 SSRF 跳板。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_unspecified):
            return ""
    return raw


def _download_raw(url: str, timeout: float = 45.0) -> bytes:
    target = _external_url(url)
    if not target:
        raise ValueError("请填写有效的 https 下载链接（不能是本机或内网地址）")
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "WhaleGirlQQ/5.2.49"}) as client:
        with client.stream("GET", target) as response:
            response.raise_for_status()
            # 真实 httpx 响应一定带最终 URL；测试替身可能没有，缺省用已校验的 target。
            final_url = str(getattr(response, "url", "") or target)
            if not _external_url(final_url):
                raise ValueError("下载被重定向到不安全的地址，已拒绝")
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("插件包超过 16 MB，已拒绝")
                chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise ValueError("下载到的插件包为空")
    return raw


def _slug_ident(value: Any) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
    text = text.strip("-._")[:64]
    if text and not re.match(r"[a-z0-9]", text):
        text = "plugin-" + text
    return text if _ID_RE.fullmatch(text) else ""


def _store_archive(data_dir: Path, raw: bytes, ident: str = "", item: dict | None = None,
                   source: str = "market") -> dict:
    """把已经拿到的 ZIP 校验后落到扩展目录，并更新 extensions.json。"""
    if not raw:
        raise ValueError("插件包为空")
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise ValueError("插件包超过 16 MB，已拒绝")
    target_dir = extensions_dir(data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="import.", suffix=".part", dir=str(target_dir))
    candidate = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        manifest, digest = _validate_zip(candidate)
        ident = (_clean_id(ident) or _clean_id(manifest.get("id"))
                 or _slug_ident(manifest.get("name")) or "import-" + digest[:8])
        target = target_dir / f"{ident}.zip"
        os.replace(candidate, target)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise
    entry = item if isinstance(item, dict) else {}
    record = {
        "id": ident,
        "name": str(entry.get("name") or manifest.get("name") or ident)[:200],
        "version": str(entry.get("version") or manifest.get("version") or "")[:40],
        "path": str(target),
        "size": len(raw),
        "sha256": digest,
        "installedAt": time.time(),
        "source": source,
        "capabilities": [str(value) for value in (manifest.get("capabilities") or [])
                         if isinstance(value, str)],
        "manifest": {key: manifest.get(key) for key in
                     ("id", "name", "version", "description", "author") if manifest.get(key)},
    }
    records_path = target_dir / "extensions.json"
    records = []
    if records_path.is_file():
        try:
            old = json.loads(records_path.read_text(encoding="utf-8"))
            if isinstance(old, list):
                records = [value for value in old
                           if isinstance(value, dict) and value.get("id") != ident]
        except (OSError, ValueError):
            records = []
    records.append(record)
    _atomic_write(records_path, json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"))
    return record


def install_url(data_dir: Path, url: str, item: dict | None = None,
                timeout: float = 45.0) -> dict:
    """从外部 https 链接下载一个 ZIP 扩展；不执行、不注册成 Python 插件。"""
    raw = _download_raw(url, timeout=timeout)
    return _store_archive(data_dir, raw, "", item, "url")


def install_zip(data_dir: Path, path, item: dict | None = None) -> dict:
    """导入一个本地 ZIP 扩展；与链接导入共用同一套校验和落盘逻辑。"""
    archive = Path(path)
    if not archive.is_file():
        raise ValueError("本地扩展包不存在：" + str(archive))
    raw = archive.read_bytes()
    return _store_archive(data_dir, raw, "", item, "local")


def normalize(data: Any) -> dict:
    """把市集返回的原始 JSON 收敛成可缓存的清单。"""
    raw_items = data.get("items") if isinstance(data, dict) else data
    categories = data.get("categories") if isinstance(data, dict) else {}
    items: list[dict] = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        ident = _clean_id(raw.get("id"))
        if not ident:
            continue
        status = str(raw.get("status") or "").lower()
        download = _download_url(raw.get("downloadUrl"))
        items.append({
            "id": ident,
            "name": str(raw.get("name") or ident)[:200],
            "description": str(raw.get("description") or "")[:1200],
            "category": str(raw.get("category") or "other")[:40],
            "categoryName": str(raw.get("categoryName") or raw.get("category") or "其他")[:60],
            "version": str(raw.get("version") or "")[:40],
            "author": str(raw.get("author") or "")[:120],
            "updatedAt": str(raw.get("updatedAt") or "")[:40],
            "downloads": int(raw.get("downloads") or 0),
            "status": status or "unknown",
            "downloadUrl": download,
        })
    items.sort(key=lambda item: (-item["downloads"], item["id"]))
    return {
        "ok": True,
        "count": len(items),
        "installable": sum(1 for item in items if item["downloadUrl"]),
        "categories": categories if isinstance(categories, dict) else {},
        "items": items,
        "fetchedAt": time.time(),
    }


def fetch_catalog(timeout: float = 25.0) -> dict:
    """从市集读取当前清单；网络失败由调用方决定是否使用缓存。"""
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "WhaleGirlQQ/5.2.49"}) as client:
        response = client.get(MARKET_URL)
        response.raise_for_status()
        return normalize(response.json())


def catalog_path(data_dir: Path) -> Path:
    return Path(data_dir) / "qqagent_market.json"


def extensions_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "qqagent_extensions"


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def save_catalog(data_dir: Path, data: dict) -> dict:
    normalized = normalize(data)
    _atomic_write(catalog_path(data_dir), json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
    return normalized


def load_catalog(data_dir: Path, refresh: bool = False, timeout: float = 25.0) -> dict:
    """读取清单；refresh 失败时退回旧缓存，而不是让插件页整块打不开。"""
    path = catalog_path(data_dir)
    if not refresh and path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and isinstance(cached.get("items"), list):
                return cached
        except (OSError, ValueError):
            pass
    try:
        data = fetch_catalog(timeout=timeout)
        save_catalog(data_dir, data)
        return data
    except Exception as exc:
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and isinstance(cached.get("items"), list):
                    cached = dict(cached)
                    cached["stale"] = True
                    cached["error"] = str(exc)
                    return cached
            except (OSError, ValueError):
                pass
        raise


def _manifest_from_zip(archive: zipfile.ZipFile) -> dict:
    candidates = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/plugin.json") or name == "plugin.json":
            candidates.append(info)
    candidates.sort(key=lambda info: info.filename.count("/"))
    for info in candidates[:3]:
        try:
            raw = archive.read(info)
            if len(raw) > 512 * 1024:
                continue
            value = json.loads(raw.decode("utf-8-sig"))
            if isinstance(value, dict):
                return value
        except (KeyError, OSError, ValueError, UnicodeError):
            continue
    return {}


def _validate_zip(path: Path) -> tuple[dict, str]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("下载内容不是有效的 ZIP 插件包") from exc
    with archive:
        infos = archive.infolist()
        if not infos:
            raise ValueError("ZIP 插件包是空的")
        if len(infos) > MAX_ARCHIVE_FILES:
            raise ValueError(f"ZIP 内文件过多（最多 {MAX_ARCHIVE_FILES} 个）")
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise ValueError("ZIP 包包含绝对路径")
            parts = [part for part in name.split("/") if part]
            if ".." in parts:
                raise ValueError("ZIP 包包含跳转路径")
            total += max(0, int(info.file_size))
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP 解压后体积过大")
        manifest = _manifest_from_zip(archive)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest, digest


def install_extension(data_dir: Path, item: dict, timeout: float = 45.0) -> dict:
    """下载一个市集插件到扩展目录；只落盘，不执行、不注册成 Python 插件。"""
    if not isinstance(item, dict):
        raise ValueError("插件信息无效")
    ident = _clean_id(item.get("id"))
    url = _download_url(item.get("downloadUrl"))
    if not ident or not url:
        raise ValueError("这个条目没有可下载的插件包")
    raw = _download_raw(url, timeout=timeout)
    return _store_archive(data_dir, raw, ident, item, "market")


def list_extensions(data_dir: Path) -> list[dict]:
    path = extensions_dir(data_dir) / "extensions.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def bridge_catalog_path(data_dir: Path) -> Path:
    return Path(data_dir) / "qqagent_bridge_catalog.json"


def _record_capabilities(record: dict, archive_path: Path) -> list[str]:
    """读取扩展声明的 capabilities；旧记录没有该字段时从 zip 清单补读。"""
    raw = record.get("capabilities") or record.get("manifest", {}).get("capabilities") or []
    if isinstance(raw, str):
        raw = [raw]
    values = [str(value) for value in raw if isinstance(value, str)] if isinstance(raw, list) else []
    if values or not archive_path.is_file():
        return values
    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _manifest_from_zip(archive)
    except (OSError, zipfile.BadZipFile):
        return []
    caps = manifest.get("capabilities") or []
    if isinstance(caps, str):
        caps = [caps]
    return [str(value) for value in caps if isinstance(value, str)] if isinstance(caps, list) else []


# ── 宿主根里已装的真扩展：只读并入，不 materialize、不起 node ─────────────────
_HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_MANIFEST_BYTES = 512 * 1024


def _host_plugins_dir(data_dir: Path) -> Path | None:
    """``<data>/qq-only/qqagent_host/plugins``；宿主根不在就给 None 且不创建目录。

    ``qqagent_host.plugins_dir()`` 会顺带 mkdir（安装链要它这么做），而 catalog 构建
    是只读路径：只在这个目录**已经存在**时才调用它，那时 mkdir 是空操作。
    """
    try:
        from . import qqagent_host
        root = qqagent_host.default_host_root(data_dir)
        if root is None or not Path(root).is_dir():
            return None
        folder = Path(root) / "plugins"
        if not folder.is_dir():
            return None
        return Path(qqagent_host.plugins_dir(root))
    except Exception:
        return None


def _host_manifest(folder: Path) -> dict:
    """宿主扩展清单：plugin.json 用宿主模块的读法，缺失再看 skill.json。"""
    try:
        from . import qqagent_host
        manifest = qqagent_host.read_plugin_manifest(folder)
    except Exception:
        manifest = {}
    if manifest:
        return manifest
    path = folder / "skill.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_bytes()[:MAX_MANIFEST_BYTES].decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _string_list(raw) -> list[str]:
    """清单里的列表字段一律收敛成字符串列表；字符串按单项算。"""
    if isinstance(raw, str):
        raw = [raw]
    return [str(item) for item in raw if isinstance(item, str)] if isinstance(raw, list) else []


def _host_entries(data_dir: Path) -> list[dict]:
    """宿主根 ``plugins/<id>`` 里的真扩展；读不到就空列表（静默跳过，不抛）。

    providers / hooks / tools 取 ``list_installed()`` 的结果：它内部已经是
    「probes.json 实测 > 静态扫描 > 清单声明」的优先级，这里不另立一套，也不起 node。
    """
    folder = _host_plugins_dir(data_dir)
    if folder is None:
        return []
    try:
        from . import qqagent_host
        rows = qqagent_host.list_installed(folder.parent)
    except Exception:
        return []
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = Path(str(row.get("dir") or ""))
        if not path.is_dir():
            continue
        try:
            path.resolve().relative_to(folder.resolve())
        except (OSError, ValueError):
            continue
        manifest = _host_manifest(path)
        # 清单 id 优先（plugin.json，其次 skill.json）；list_installed 的 id 已经是
        # 「plugin.json id 或目录名」，把它排在清单后只是为了让 skill.json 也有份。
        ident = str(manifest.get("id") or row.get("id") or path.name).strip()
        if not _HOST_ID_RE.match(ident):
            ident = path.name if _HOST_ID_RE.match(path.name) else ""
        if not ident:
            continue
        entries.append({
            "id": ident,
            "name": str(row.get("name") or manifest.get("name") or ident)[:200],
            "kind": "plugin",
            "path": str(path),
            # file 指向扩展目录：能力桥的自动发现（extension_host.discover_extensions）
            # 只认 file 这个键，缺了它宿主根里装好的扩展一条都发现不到，桥永远不 ready。
            "file": str(path),
            # 清单声明的 + probes.json 里实测到的，合起来才是完整白名单（实测能多出 provider）。
            "capabilities": list(dict.fromkeys(
                _string_list(manifest.get("capabilities"))
                + _string_list(row.get("providers")))),
            "version": str(row.get("version") or manifest.get("version") or "")[:40],
            "description": str(manifest.get("description") or "")[:1200],
            "providers": _string_list(row.get("providers")),
            "hooks": _string_list(row.get("hooks")),
            "tools": _string_list(row.get("tools")),
            "source": "host",
        })
    return entries


def build_bridge_catalog(data_dir: Path, include_host: bool = True) -> dict:
    """把已安装扩展写成能力桥可读的 catalog；不下载、不执行代码、不写宿主根。

    两类来源并进同一份 catalog：宿主根 ``<data>/qq-only/qqagent_host/plugins/<id>``
    里真装上的扩展排最前（那才是"装进去、能跑"的一份），市场 zip 目录那份照旧产出
    条目；同 id 时 zip 条目带 ``shadowed``，上层按 ``source`` 分辨。宿主根不存在或
    读不了就静默跳过：不抛、不建目录、不 materialize。
    """
    hosts = _host_entries(data_dir) if include_host else []
    entries = list(hosts)
    owned = {str(entry["id"]).lower() for entry in hosts}
    base = extensions_dir(data_dir)
    for record in list_extensions(data_dir):
        ident = _clean_id(record.get("id"))
        archive = Path(str(record.get("path") or ""))
        if not ident or not archive.is_file():
            continue
        try:
            archive.resolve().relative_to(base.resolve())
        except (OSError, ValueError):
            continue
        capabilities = _record_capabilities(record, archive)
        if not capabilities:
            continue
        entry = {
            "id": ident,
            "name": str(record.get("name") or ident)[:200],
            "kind": "plugin",
            "file": str(archive),
            "capabilities": capabilities,
            "tools": [],
            "source": "zip",
        }
        if ident in owned:
            entry["shadowed"] = True
        entries.append(entry)
    target = bridge_catalog_path(data_dir)
    payload = {"plugins": entries, "count": len(entries), "updated": time.time()}
    _atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"path": str(target), "count": len(entries),
            "providers": sorted({name for entry in entries
                                 for name in entry.get("capabilities") or []
                                 if name.startswith(("message.", "reply.", "llm.", "chat."))}),
            "hostCount": len(hosts),
            "zipCount": len(entries) - len(hosts),
            "hostProviders": sorted({name for entry in hosts for name in entry["providers"]})}


def _runtime_roots():
    roots = []
    env = os.environ.get("QQ_AGENT_HOME", "").strip()
    if env:
        roots.append(Path(env))
    try:
        repo = Path(__file__).resolve().parents[1]
        roots.extend([repo, repo.parent / "插件市场"])
    except (IndexError, OSError):
        pass
    try:
        # 冻结版里 __file__ 在 _MEIPASS，源码通常放在安装目录或工作区。
        exe_dir = Path(sys.executable).resolve().parent
        roots.extend([exe_dir, exe_dir.parent, Path.cwd()])
    except (OSError, ValueError):
        pass
    roots.extend([
        Path.home() / "聊天机器人" / "qq机器人",
        Path.home() / "聊天机器人" / "插件市场",
        Path("D:/聊天机器人/qq机器人"),
        Path("D:/聊天机器人/插件市场"),
    ])
    seen, result = set(), []
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            result.append(root)
    return result


def find_runtime() -> tuple[str, str]:
    """在当前机器上寻找 host-one.mjs 与 QQ Agent 0.4 源码。"""
    roots = _runtime_roots()
    host = ""
    for root in roots:
        for candidate in (root / "QQAgent兼容层" / "host-one.mjs", root / "host-one.mjs"):
            if candidate.is_file():
                host = str(candidate)
                break
        if host:
            break
    source = ""
    for root in roots:
        for candidate in (root / ".tmp" / "qq-agent-run", root / "qq-agent-run"):
            if (candidate / "src" / "plugin-loader.js").is_file():
                source = str(candidate)
                break
        if source:
            break
    return host, source
