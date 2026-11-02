"""plugin_core_bridge.py — 把 plugin_core 内核接到 WhaleGirl QQ 运行时。

设计边界：
- 插件目录：<账号数据目录>/plugin_core/plugins/<id>/plugin.json；
- 内核数据：<账号数据目录>/plugin_core/data（审计、KV、会话）；
- 只有目录里至少有一个合法插件目录时才启动 Host，空目录不增加启动开销；
- 插件内核处理过的消息（record.bids 非空）由调用方发送 record.sent，
  不再走旧插件钩子或 AI，避免同一句话被回复两次；
- 内核没有插件认领时返回 None，调用方继续原有 AI/旧插件链路。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from plugin_core import Host, KernelError, ManifestError, normalize_manifest


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _segment_from_media(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 QQ 入站 media 转成 plugin_core 白名单段。"""
    if not isinstance(item, dict):
        return None
    kind = _text(item.get("type")).lower()
    if kind == "image":
        seg = {"type": "image"}
        for key in ("url", "file", "size"):
            if item.get(key) not in (None, ""):
                seg[key] = item[key]
        return seg if seg.get("url") or seg.get("file") else None
    if kind in ("record", "voice"):
        seg = {"type": "voice"}
        for key in ("url", "duration"):
            if item.get(key) not in (None, ""):
                seg[key] = item[key]
        return seg if seg.get("url") else None
    if kind == "file":
        seg = {"type": "file"}
        for key in ("name", "url", "size"):
            if item.get(key) not in (None, ""):
                seg[key] = item[key]
        return seg if seg.get("url") or seg.get("name") else None
    if kind == "face":
        face_id = item.get("id")
        return {"type": "face", "id": face_id} if face_id not in (None, "") else None
    if kind == "video":
        # 内核没有 video 段；以文件段交给插件，至少能看到附件存在。
        seg = {"type": "file", "name": _text(item.get("name") or "视频")}
        if item.get("url"):
            seg["url"] = item["url"]
        return seg
    return None


def build_raw_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """QQ 运行时消息 → plugin_core.normalize_message 能接受的原始结构。"""
    text = _text(message.get("text"))
    segments: List[Dict[str, Any]] = []
    if text:
        segments.append({"type": "text", "text": text})
    for item in message.get("media") or []:
        seg = _segment_from_media(item)
        if seg:
            segments.append(seg)
    sender_id = _text(message.get("sender_id") or message.get("sender"))
    sender_name = _text(message.get("sender_name") or message.get("sender"))
    role = _text(message.get("sender_role") or message.get("role") or "member").lower()
    if role not in ("owner", "admin", "member"):
        role = "owner" if message.get("is_owner") else "member"
    return {
        "session": {
            "id": _text(message.get("target")),
            "kind": _text(message.get("kind") or "group"),
            "name": _text(message.get("group_name") or message.get("target_name") or ""),
        },
        "sender": {
            "id": sender_id,
            "name": sender_name,
            "role": role,
            "is_bot": bool(message.get("is_bot") or message.get("sender_is_bot")),
        },
        "message": {
            "id": _text(message.get("id") or message.get("message_id")),
            "text": text,
            "segments": segments,
            "mentions": [str(item) for item in (message.get("at_ids") or [])],
            "reply_to": message.get("reply_to") or message.get("_quote_ref"),
            "ts": message.get("ts") or message.get("time") or time.time(),
        },
    }


def media_to_reply_meta(media: Any) -> List[Dict[str, Any]]:
    """plugin_core 的媒体列表 → build_reply_segments 接受的 {type,data}。"""
    out: List[Dict[str, Any]] = []
    for item in media or []:
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("type")).lower()
        if kind == "image":
            data = {key: item[key] for key in ("url", "file", "size") if item.get(key) not in (None, "")}
            if data:
                out.append({"type": "image", "data": data})
        elif kind in ("voice", "record"):
            source = item.get("url") or item.get("file")
            if source:
                out.append({"type": "record", "data": {"file": source}})
        elif kind == "face":
            face_id = item.get("id")
            if face_id not in (None, ""):
                out.append({"type": "face", "data": {"id": face_id}})
        # file 段当前 QQ 发送层没有通用上传入口，跳过而不是伪装成图片。
    return out


_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_ZIP_FILES = 4000
_MAX_ZIP_BYTES = 200 * 1024 * 1024


def _safe_zip_name(name: Any) -> str:
    raw = _text(name).replace("\\", "/").strip()
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return ""
    parts = PurePosixPath(raw).parts
    if any(part in ("", ".", "..") for part in parts):
        return ""
    return "/".join(parts)


def _extract_zip(archive: Path, dest: Path) -> None:
    total = 0
    try:
        zf = zipfile.ZipFile(str(archive))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("不是有效的 zip 包：%s" % (exc,)) from None
    with zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ZIP_FILES:
            raise ValueError("zip 文件数超过 %d 个" % _MAX_ZIP_FILES)
        for info in infos:
            safe = _safe_zip_name(info.filename)
            if not safe:
                raise ValueError("zip 含有不安全路径：%s" % info.filename)
            total += max(0, int(info.file_size or 0))
            if total > _MAX_ZIP_BYTES:
                raise ValueError("zip 解压后超过 %d MB" % (_MAX_ZIP_BYTES // 1024 // 1024))
        zf.extractall(str(dest))


def _manifest_root(base: Path) -> Path:
    base = Path(base)
    if (base / "plugin.json").is_file():
        return base
    candidates = []
    try:
        for item in base.iterdir():
            if item.is_dir() and not item.name.startswith(".") and item.name != "__MACOSX":
                if (item / "plugin.json").is_file():
                    candidates.append(item)
    except OSError as exc:
        raise ValueError("读不到插件目录：%s" % (exc,)) from None
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError("zip 里有多个插件目录，请一次只装一个")
    raise ValueError("找不到 plugin.json（应位于 zip 根目录或唯一的子目录）")


def _read_manifest(root: Path) -> Dict[str, Any]:
    path = root / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("plugin.json 读取失败：%s" % (exc,)) from None
    if not isinstance(data, dict):
        raise ValueError("plugin.json 必须是对象")
    try:
        return normalize_manifest(data, None)
    except ManifestError as exc:
        raise ValueError("插件清单无效：%s" % (exc,)) from None


def _check_entry(root: Path, manifest: Dict[str, Any]) -> None:
    entry = _text(manifest.get("entry"))
    rel = entry.split(":", 1)[0].strip()
    if not rel:
        raise ValueError("插件清单缺少 entry")
    base = root.resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError("插件入口越出插件目录：%s" % rel) from None
    if not target.is_file():
        raise ValueError("插件入口文件不存在：%s" % rel)


class _CollectorTransport:
    """真实 QQ 发送由调用方完成；这里只做 plugin_core 的传输层适配。

    Host.deliver 会把最终 Draft 交给 transport.send。Bridge 记录一份历史用于
    排障，真正的发送使用 record['sent']，因此这里不直接调用 QQ 协议。
    """

    def __init__(self, bridge: "PluginCoreBridge") -> None:
        self.bridge = bridge
        self.connected = False
        self.closed = False
        self.history: List[Dict[str, Any]] = []

    def connect(self, config=None) -> dict:
        self.connected = True
        return {"transport": "whalegirl-qq", "connected": True, "config": dict(config or {})}

    def send(self, draft) -> dict:
        if not self.connected:
            raise KernelError("QQ 传输层未 connect，不能发送")
        rec = {
            "conversation": draft.conversation,
            "text": draft.text,
            "media": list(draft.media),
            "plugin": draft.plugin,
            "ts": time.time(),
        }
        self.history.append(rec)
        return rec

    def fetch(self, url, *, method="GET", body=None, timeout=10.0):
        """插件声明 network 后由内核调用；使用标准库，不引入额外依赖。"""
        data = None
        if body is not None:
            data = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(str(url), data=data, method=str(method or "GET").upper())
        if isinstance(body, str):
            request.add_header("Content-Type", "text/plain; charset=utf-8")
        try:
            with urllib.request.urlopen(request, timeout=float(timeout or 10.0)) as response:
                raw = response.read(2 * 1024 * 1024)
                charset = response.headers.get_content_charset() or "utf-8"
                return {
                    "status": int(getattr(response, "status", 200) or 200),
                    "url": response.geturl(),
                    "text": raw.decode(charset, errors="replace"),
                    "headers": dict(response.headers.items()),
                }
        except urllib.error.HTTPError as exc:
            raw = exc.read(512 * 1024)
            charset = exc.headers.get_content_charset() if exc.headers else "utf-8"
            return {
                "status": int(exc.code),
                "url": str(url),
                "text": raw.decode(charset or "utf-8", errors="replace"),
                "headers": dict(exc.headers.items()) if exc.headers else {},
            }

    def close(self) -> None:
        self.closed = True
        self.connected = False


class _ModelAdapter:
    """把运行时异步 AIEngine 适配成 plugin_core 的同步 ctx.model.chat。"""

    def __init__(self, bridge: "PluginCoreBridge") -> None:
        self.bridge = bridge

    def chat(self, messages, *, max_chars=None) -> str:
        runtime = self.bridge.runtime
        ai = getattr(runtime, "ai", None)
        if ai is None:
            return ""
        loop = self.bridge.loop
        if loop is None or not loop.is_running():
            return ""
        system_parts: List[str] = []
        chat_messages: List[Dict[str, str]] = []
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            role = _text(item.get("role") or "user")
            content = _text(item.get("content"))
            if role == "system":
                system_parts.append(content)
            else:
                chat_messages.append({"role": role, "content": content})
        try:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                raise KernelError("ctx.model.chat 不能在主事件循环线程里同步调用")
            async def _invoke():
                lock = getattr(runtime, "ai_lock", None)
                if lock is None:
                    return await ai.chat(
                        messages=chat_messages, system_prompt="\n".join(system_parts))
                async with lock:
                    return await ai.chat(
                        messages=chat_messages, system_prompt="\n".join(system_parts))

            future = asyncio.run_coroutine_threadsafe(_invoke(), loop)
            result = future.result(timeout=90)
        except Exception as exc:
            if isinstance(exc, KernelError):
                raise
            raise KernelError("模型调用失败：%s" % (exc,)) from None
        text = result if isinstance(result, str) else ""
        if max_chars is not None and len(text) > int(max_chars):
            text = text[: int(max_chars)]
        return text


class PluginCoreBridge:
    """plugin_core Host 的生命周期与消息入口。"""

    def __init__(self, root, runtime=None) -> None:
        self.root = Path(root)
        self.plugins_dir = self.root / "plugins"
        self.data_dir = self.root / "data"
        self.runtime = runtime
        self.loop = None
        self.host: Optional[Host] = None
        self.transport: Optional[_CollectorTransport] = None
        self.last_error = ""
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.host is not None and self.host.started)

    def _has_plugins(self) -> bool:
        if not self.plugins_dir.is_dir():
            return False
        try:
            for item in self.plugins_dir.iterdir():
                if (item.is_dir() and not item.name.startswith(".")
                        and (item / "plugin.json").is_file()):
                    return True
        except OSError:
            return False
        return False

    def install(self, source: str, *, overwrite: bool = False) -> Dict[str, Any]:
        """安装一个目录式 plugin_core 插件（目录或单插件 zip）。

        只接受带合法 plugin.json 的插件；先复制到 .staging，再原子换位到
        plugins/<id>。reload 失败会回滚到旧版本，避免坏插件把已有插件顶掉。
        """
        raw = _text(source).strip()
        if not raw:
            raise ValueError("请填写插件目录或 zip 路径")
        source_path = Path(raw).expanduser()
        if not source_path.exists():
            raise ValueError("插件源不存在：%s" % source_path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = None
        staging = None
        backup = None
        try:
            if source_path.is_file():
                if source_path.suffix.lower() != ".zip":
                    raise ValueError("只支持插件目录或 .zip 包")
                temp_dir = tempfile.TemporaryDirectory(prefix="whalegirl-plugin-")
                extracted = Path(temp_dir.name)
                _extract_zip(source_path, extracted)
                root = _manifest_root(extracted)
            elif source_path.is_dir():
                root = _manifest_root(source_path)
            else:
                raise ValueError("插件源必须是目录或 .zip 包")
            manifest = _read_manifest(root)
            plugin_id = _text(manifest.get("id")).strip()
            if not _PLUGIN_ID_RE.match(plugin_id):
                raise ValueError("插件 id 不合法：%r" % plugin_id)
            _check_entry(root, manifest)
            target = self.plugins_dir / plugin_id
            try:
                if target.resolve() == root.resolve():
                    raise ValueError("不能从已安装目录原地重装，请选择其他来源")
            except OSError:
                pass
            if target.exists() and not overwrite:
                raise ValueError("插件 %s 已安装；如需覆盖请勾选覆盖" % plugin_id)

            staging_root = self.root / ".staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            staging = staging_root / (plugin_id + "-" + uuid.uuid4().hex)
            shutil.copytree(
                root, staging,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
            )
            if target.exists():
                backup_root = self.root / ".backup"
                backup_root.mkdir(parents=True, exist_ok=True)
                backup = backup_root / (plugin_id + "-" + uuid.uuid4().hex)
                target.rename(backup)
            try:
                staging.rename(target)
            except Exception:
                if backup is not None and backup.exists() and not target.exists():
                    backup.rename(target)
                raise
            try:
                payload = self.reload()
            except Exception:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                if backup is not None and backup.exists():
                    backup.rename(target)
                try:
                    self.reload()
                except Exception:
                    pass
                raise
            if backup is not None and backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            payload.update({
                "installed": True,
                "plugin": {
                    "id": plugin_id,
                    "name": _text(manifest.get("name")),
                    "version": _text(manifest.get("version")),
                },
            })
            return payload
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if temp_dir is not None:
                temp_dir.cleanup()

    def uninstall(self, plugin_id: str) -> Dict[str, Any]:
        """移除一个目录式插件；移除最后一个插件时停止内核 Host。"""
        ident = _text(plugin_id).strip()
        if not _PLUGIN_ID_RE.match(ident):
            raise ValueError("插件 id 不合法：%r" % ident)
        target = self.plugins_dir / ident
        if not target.is_dir():
            raise ValueError("没有安装插件 %s" % ident)
        backup_root = self.root / ".backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / (ident + "-" + uuid.uuid4().hex)
        target.rename(backup)
        try:
            if not self._has_plugins():
                self.close()
                payload = self.status()
            else:
                payload = self.reload()
        except Exception:
            if backup.exists() and not target.exists():
                backup.rename(target)
            try:
                self.reload()
            except Exception:
                pass
            raise
        shutil.rmtree(backup, ignore_errors=True)
        payload.update({"uninstalled": True, "id": ident})
        return payload

    def start(self) -> Dict[str, Any]:
        if self.host is not None:
            return self.status()
        self.root.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self._has_plugins():
            return self.status()
        try:
            transport = _CollectorTransport(self)
            host = Host(
                self.plugins_dir,
                self.data_dir,
                transport=transport,
                model=_ModelAdapter(self),
                default_persona=False,
            )
            host.start()
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.host = None
            self.transport = None
            raise
        self.transport = transport
        self.host = host
        self.last_error = ""
        return self.status()

    async def start_async(self) -> Dict[str, Any]:
        self.loop = asyncio.get_running_loop()
        return await asyncio.to_thread(self.start)

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """处理一条 QQ 消息；没有插件认领时返回 None。"""
        if not self.available:
            return None
        try:
            raw = build_raw_message(message)
            with self._lock:
                record = self.host.handle(raw)
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            return None
        if not isinstance(record, dict):
            return None
        return record

    def reload(self) -> Dict[str, Any]:
        if self.host is None:
            return self.start()
        try:
            self.host.reload_all()
            self.last_error = ""
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            raise
        return self.status()

    async def reload_async(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.reload)

    def close(self) -> Dict[str, Any]:
        if self.host is None:
            return {"stopped": True, "plugins": 0}
        try:
            result = self.host.stop()
        finally:
            self.host = None
            self.transport = None
        return result

    async def close_async(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.close)

    def status(self) -> Dict[str, Any]:
        host = self.host
        plugins: List[Dict[str, Any]] = []
        refused: List[Dict[str, Any]] = []
        if host is not None:
            for plugin in host.plugins.values():
                plugins.append({
                    "id": plugin.id,
                    "version": _text((plugin.manifest or {}).get("version")),
                    "name": _text((plugin.manifest or {}).get("name")),
                })
            for item in host.refused or []:
                if isinstance(item, dict):
                    refused.append({
                        "id": _text(item.get("id") or item.get("plugin") or ""),
                        "reason": _text(item.get("reason") or item.get("error") or item),
                    })
                else:
                    refused.append({"id": "", "reason": _text(item)})
        return {
            "available": self.available,
            "started": bool(host is not None and host.started),
            "pluginsDir": str(self.plugins_dir),
            "dataDir": str(self.data_dir),
            "plugins": plugins,
            "refused": refused,
            "lastError": self.last_error,
        }
