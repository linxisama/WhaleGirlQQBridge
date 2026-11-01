"""结构化日志内核（W13a）：错误优先 + 关键事件，只做存储与查询。

条目形状（三张卡共用的冻结契约）::

    {"id": "<sha1 前 16>", "ts": "2026-10-26T17:05:12+08:00", "level": "error",
     "source": "qq", "message": "一行摘要（≤500 字）",
     "detail": "可选多行文本/traceback（≤8000 字）", "code": "可选短错误码", "extra": {}}

约定：

- 落盘：``<data_dir>/qq-only/logs/app-<YYYYMMDD>.jsonl``，单文件 10MB 滚动、保留 3 份；
  内存环形保留最近 1000 条，供界面秒读。
- 采集口径：异常/失败/警告 + 启动停止、连接、导入安装成功这类关键事件；
  **不记每条聊天正文**。
- 条目恒定包含 8 个键：``detail``/``code`` 为空时是空串，``extra`` 为空时是 ``{}``。
- ``tail()`` / ``export()`` 返回的 ``entries`` **最新在前**（界面第一屏就是最近发生的事）。
- 任何方法都不抛：写盘失败、目录建不出来、坏行跳过一律降级（内存里仍在，返回值不炸）。
- 只用标准库，不引入依赖。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

LEVELS = ("debug", "info", "warning", "error")
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
RING = 1000
ROLL_BYTES = 10 * 1024 * 1024
ROLL_KEEP = 3
LOG_DIR_PARTS = ("qq-only", "logs")
_CLEARED_IDS_MAX = 4000

#: message / detail 的硬上限（字符，不是字节）。
MESSAGE_MAX = 500
DETAIL_MAX = 8000
#: source / code 也是短词，给个兜底上限。
SOURCE_MAX = 40
CODE_MAX = 80
#: 落盘文件命名（跨日换新、滚动顺延都基于它）。
FILE_PREFIX = "app-"
FILE_SUFFIX = ".jsonl"
#: 从文件补齐时单个文件最多回看多少字节（绝不把整个文件读进内存）。
SCAN_BYTES = 512 * 1024
#: 从文件补齐时最多回看几个文件。
SCAN_FILES = 3

_KEYS = ("id", "ts", "level", "source", "message", "detail", "code", "extra")


# ── 小的取值/整形工具（全部不抛） ────────────────────────────────────────────

def _as_text(value: Any) -> str:
    """任何值转字符串；None 当空串，str() 失败也当空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _clip(text: str, cap: int) -> str:
    if cap <= 0 or len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _one_line(text: str) -> str:
    """摘要只留一行：换行/Tab 折成空格。"""
    return " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())


def _norm_level(level: Any) -> str:
    text = _as_text(level).strip().lower()
    return text if text in LEVELS else "info"


def _norm_extra(extra: Any) -> Dict[str, Any]:
    if not isinstance(extra, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in extra.items():
        out[_clip(_as_text(key) or "key", CODE_MAX)] = value
    return out


def _parse_dt(value: Any) -> Optional[datetime]:
    """把 datetime / epoch 秒 / ISO 字符串规整成带时区的 datetime；解析不了返回 None。"""
    try:
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, bool):
            return None
        elif isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(float(value))
        elif isinstance(value, str) and value.strip():
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
    except (ValueError, TypeError, OSError, OverflowError):
        return None
    if moment.tzinfo is None:
        try:
            moment = moment.astimezone()
        except (OSError, OverflowError, ValueError):
            return None
    return moment


def _iso(value: Any = None) -> str:
    """ISO 时间串（秒精度、带本地时区偏移），例如 2026-10-26T17:05:12+08:00。"""
    moment = _parse_dt(value)
    if moment is None:
        moment = datetime.now().astimezone()
    try:
        return moment.replace(microsecond=0).isoformat()
    except (ValueError, OverflowError):
        return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _day_key(ts: Any) -> str:
    """日期键：优先用条目自己的 ts（跨日由它决定文件名），退化到当前本地日期。"""
    text = _as_text(ts)
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        day = text[:10].replace("-", "")
        if day.isdigit():
            return day
    return datetime.now().astimezone().strftime("%Y%m%d")


def _clamp_limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        try:
            number = int(default)
        except (TypeError, ValueError):
            number = DEFAULT_LIMIT
    if number <= 0:
        number = 1
    return min(number, MAX_LIMIT)


def _level_set(level: Any) -> frozenset:
    text = _as_text(level).strip().lower()
    if not text:
        return frozenset()
    return frozenset(part.strip() for part in text.split(",") if part.strip())


# ── 公开的路径与条目构造 ────────────────────────────────────────────────────

def _default_data_dir() -> Path:
    """默认数据目录：与 src/qq_app.py DATA_ROOT 同一口径（源码=仓库根/data）。

    以前直接取 __file__ 的上两级；冻结后 __file__ 在解包目录 _internal 里，日志会写进
    安装目录（只读位置则直接失败），因此改走 src.data_root。
    """
    from .data_root import resolve_data_root
    return resolve_data_root() / "data"


def logs_dir(data_dir: Any = None) -> Path:
    """日志目录 ``<data_dir>/qq-only/logs``（只算路径，不创建）。"""
    if data_dir:
        base = Path(os.path.abspath(str(data_dir)))
    else:
        base = _default_data_dir()
    return base.joinpath(*LOG_DIR_PARTS)


_id_seq = itertools.count(1)


def _entry_id(ts: str, level: str, source: str, message: str, detail: str, code: str) -> str:
    payload = "|".join(
        (
            ts, level, source, message, detail, code,
            str(os.getpid()), str(time.time_ns()), str(next(_id_seq)),
        )
    )
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:16]


def make_entry(
    level: Any,
    source: Any,
    message: Any,
    *,
    detail: Any = "",
    code: Any = "",
    extra: Any = None,
    now: Any = None,
) -> Dict[str, Any]:
    """造一条日志条目（不落盘、不抛）。非法 level 归 info，非字符串入参走 str()。"""
    lvl = _norm_level(level)
    text = _clip(_one_line(_as_text(message)), MESSAGE_MAX)
    body = _clip(_as_text(detail), DETAIL_MAX)
    src = _clip(_one_line(_as_text(source)), SOURCE_MAX)
    trc = _clip(_one_line(_as_text(code)), CODE_MAX)
    extras = _norm_extra(extra)
    ts = _iso(now)
    return {
        "id": _entry_id(ts, lvl, src, text, body, trc),
        "ts": ts,
        "level": lvl,
        "source": src,
        "message": text,
        "detail": body,
        "code": trc,
        "extra": extras,
    }


def _make_matcher(level: Any, source: Any, query: Any, since: Any) -> Callable[[Any], bool]:
    levels = _level_set(level)
    wanted_source = _as_text(source).strip().lower()
    needle = _as_text(query).strip().lower()
    since_dt = _parse_dt(since) if _as_text(since).strip() else None

    def match(entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        if levels and _as_text(entry.get("level")).strip().lower() not in levels:
            return False
        if wanted_source and _as_text(entry.get("source")).strip().lower() != wanted_source:
            return False
        if needle:
            haystack = " ".join(
                _as_text(entry.get(key)) for key in ("message", "detail", "code", "source")
            ).lower()
            if needle not in haystack:
                return False
        if since_dt is not None:
            moment = _parse_dt(entry.get("ts"))
            if moment is None or moment < since_dt:
                return False
        return True

    return match


def _read_tail_entries(path: Path, max_bytes: int = SCAN_BYTES) -> List[Dict[str, Any]]:
    """从文件尾部回看有限字节，返回**最新在前**的条目；坏行跳过。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    start = max(0, size - max(1, int(max_bytes)))
    try:
        with open(path, "rb") as handle:
            if start:
                handle.seek(start)
            blob = handle.read()
    except OSError:
        return []
    lines = blob.split(bytes((10,)))
    if start:
        lines = lines[1:]  # 丢掉可能被截断的首行
    out: List[Dict[str, Any]] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _is_cleared_entry(entry: Any, marker: str, cleared_ids: frozenset) -> bool:
    """clear() 之后不该再出现的条目：当时被清掉的那些，或早于清空时刻的。"""
    if not marker:
        return False
    ident = _as_text(entry.get("id"))
    if ident and ident in cleared_ids:
        return True
    moment = _parse_dt(entry.get("ts"))
    base = _parse_dt(marker)
    if moment is None or base is None:
        return False
    return moment < base


# ── 主类 ────────────────────────────────────────────────────────────────────

class AppLog:
    """线程安全的结构化日志器：内存环形 + jsonl 落盘 + 查询/导出。

    ``clock`` 可注入（返回 datetime 或 ISO 串），用来在测试里造跨日与滚动。
    """

    def __init__(
        self,
        data_dir: Any = None,
        *,
        ring: int = RING,
        roll_bytes: int = ROLL_BYTES,
        roll_keep: int = ROLL_KEEP,
        clock: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.data_dir = Path(os.path.abspath(str(data_dir))) if data_dir else None
        self._dir = logs_dir(data_dir)
        self._lock = threading.RLock()
        try:
            size = int(ring)
        except (TypeError, ValueError):
            size = RING
        self._ring: deque = deque(maxlen=max(1, size))
        try:
            self._roll_bytes = max(1, int(roll_bytes))
        except (TypeError, ValueError):
            self._roll_bytes = ROLL_BYTES
        try:
            self._roll_keep = max(1, int(roll_keep))
        except (TypeError, ValueError):
            self._roll_keep = ROLL_KEEP
        self._clock = clock
        self.write_failures = 0
        self._cleared_ts = ""
        self._cleared_ids: Dict[str, None] = {}

    # -- 写入 --------------------------------------------------------------
    def _now(self) -> Any:
        if self._clock is None:
            return None
        try:
            return self._clock() if callable(self._clock) else self._clock
        except Exception:
            return None

    def record(
        self,
        level: Any,
        source: Any,
        message: Any,
        *,
        detail: Any = "",
        code: Any = "",
        extra: Any = None,
    ) -> Dict[str, Any]:
        """落一条（永不抛）。返回值就是落下的条目（副本）。"""
        try:
            entry = make_entry(
                level, source, message, detail=detail, code=code, extra=extra, now=self._now()
            )
        except Exception:
            self.write_failures += 1
            entry = {
                "id": "",
                "ts": _iso(),
                "level": "info",
                "source": "",
                "message": "",
                "detail": "",
                "code": "",
                "extra": {},
            }
        with self._lock:
            self._ring.append(entry)
            try:
                self._append(entry)
            except Exception:
                self.write_failures += 1
        return dict(entry)

    def error(self, source: Any, message: Any, **kw: Any) -> Dict[str, Any]:
        return self.record("error", source, message, **kw)

    def warning(self, source: Any, message: Any, **kw: Any) -> Dict[str, Any]:
        return self.record("warning", source, message, **kw)

    def info(self, source: Any, message: Any, **kw: Any) -> Dict[str, Any]:
        return self.record("info", source, message, **kw)

    def debug(self, source: Any, message: Any, **kw: Any) -> Dict[str, Any]:
        return self.record("debug", source, message, **kw)

    def record_exception(
        self,
        source: Any,
        message: Any = "",
        exc: Any = None,
        *,
        detail: Any = "",
        code: Any = "",
    ) -> Dict[str, Any]:
        """记一条 error：traceback 自动塞进 detail，没给 code 时用异常类名填 code。"""
        trace = ""
        kind = ""
        try:
            if exc is None:
                active = sys.exc_info()
                if active[0] is not None:
                    kind = active[0].__name__
                    trace = traceback.format_exc()
            else:
                kind = type(exc).__name__
                trace = "".join(
                    traceback.format_exception(
                        type(exc), exc, getattr(exc, "__traceback__", None)
                    )
                )
        except Exception:
            trace = ""
        text = _as_text(message)
        if not text.strip() and exc is not None:
            try:
                text = "{}: {}".format(type(exc).__name__, exc)
            except Exception:
                text = type(exc).__name__
        body = _as_text(detail)
        if trace:
            body = (body + chr(10) + trace) if body else trace
        return self.record("error", source, text, detail=body, code=_as_text(code) or kind)

    # -- 落盘与滚动 --------------------------------------------------------
    def _path_for(self, ts: Any) -> Path:
        return self._dir / "{}{}{}".format(FILE_PREFIX, _day_key(ts), FILE_SUFFIX)

    def _archive_name(self, path: Path, index: int) -> Path:
        return path.with_name("{}.{}{}".format(path.stem, index, path.suffix))

    def _roll(self, path: Path) -> None:
        """app-x.jsonl → app-x.1.jsonl，既有 .1→.2→.3 顺延，.3 覆盖删除。"""
        newest_archive = self._archive_name(path, self._roll_keep)
        try:
            if newest_archive.exists():
                newest_archive.unlink()
        except OSError:
            pass
        for index in range(self._roll_keep - 1, 0, -1):
            source = self._archive_name(path, index)
            target = self._archive_name(path, index + 1)
            if not source.exists():
                continue
            try:
                os.replace(source, target)
            except OSError:
                pass
        try:
            os.replace(path, self._archive_name(path, 1))
        except OSError:
            pass

    @staticmethod
    def _dump(entry: Dict[str, Any]) -> str:
        try:
            return json.dumps(entry, ensure_ascii=False, sort_keys=False, default=str)
        except Exception:
            try:
                return json.dumps(
                    {key: entry.get(key) for key in _KEYS}, ensure_ascii=False, default=str
                )
            except Exception:
                return "{}"

    def _append(self, entry: Dict[str, Any]) -> None:
        path = self._path_for(entry.get("ts"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                too_big = path.stat().st_size >= self._roll_bytes
            except OSError:
                too_big = False
            if too_big:
                self._roll(path)
        line = self._dump(entry)
        with open(path, "a", encoding="utf-8", newline=chr(10)) as handle:
            handle.write(line + chr(10))

    def files(self) -> List[Path]:
        """当前日志目录里的 jsonl 文件（新→旧，按修改时间）。"""
        try:
            found = [item for item in self._dir.glob(FILE_PREFIX + "*" + FILE_SUFFIX)]
        except OSError:
            return []
        found = [item for item in found if item.is_file()]
        found.sort(key=_mtime, reverse=True)
        return found

    def _file_entries(self) -> Iterable[Dict[str, Any]]:
        for path in self.files()[:SCAN_FILES]:
            for entry in _read_tail_entries(path):
                yield entry

    # -- 查询与导出 --------------------------------------------------------
    def _collect(
        self,
        *,
        level: Any = "",
        source: Any = "",
        query: Any = "",
        limit: Any = DEFAULT_LIMIT,
        since: Any = "",
    ) -> Tuple[List[Dict[str, Any]], int]:
        size = _clamp_limit(limit)
        match = _make_matcher(level, source, query, since)
        with self._lock:
            snapshot = list(self._ring)
            cleared_marker = self._cleared_ts
            cleared_ids = frozenset(self._cleared_ids)
        picked: List[Dict[str, Any]] = []
        total = 0
        seen = set()
        for entry in reversed(snapshot):
            if _is_cleared_entry(entry, cleared_marker, cleared_ids):
                continue
            if not match(entry):
                continue
            total += 1
            ident = _as_text(entry.get("id"))
            if ident:
                seen.add(ident)
            if len(picked) < size:
                picked.append(dict(entry))
        if len(picked) < size:
            for entry in self._file_entries():
                ident = _as_text(entry.get("id"))
                if ident and ident in seen:
                    continue
                if _is_cleared_entry(entry, cleared_marker, cleared_ids):
                    continue
                if not match(entry):
                    continue
                if ident:
                    seen.add(ident)
                total += 1
                if len(picked) < size:
                    picked.append(dict(entry))
        return picked, total

    def tail(
        self,
        *,
        level: Any = "",
        source: Any = "",
        query: Any = "",
        limit: Any = DEFAULT_LIMIT,
        since: Any = "",
    ) -> List[Dict[str, Any]]:
        """最近 N 条（最新在前）。level 支持单值或逗号列表，如 error,warning。"""
        try:
            picked, _total = self._collect(
                level=level, source=source, query=query, limit=limit, since=since
            )
        except Exception:
            return []
        return picked

    def export(
        self,
        *,
        level: Any = "",
        source: Any = "",
        query: Any = "",
        limit: Any = 1000,
    ) -> Dict[str, Any]:
        """导出一份 .json（就是端点直接附件下载的那份形状）。"""
        size = _clamp_limit(limit)
        try:
            picked, _total = self._collect(level=level, source=source, query=query, limit=size)
        except Exception:
            picked = []
        return {
            "version": 1,
            "exported_at": _iso(self._now()),
            "filters": {
                "level": _as_text(level),
                "source": _as_text(source),
                "query": _as_text(query),
                "limit": size,
            },
            "count": len(picked),
            "entries": picked,
        }

    def clear(self) -> int:
        """只清内存环，不删已落盘文件；并记下清空时刻，tail() 不会把刚清掉的条目读回来。

        黑名单只增不减（并集）：同一秒内写入的条目 ts 与清空时刻相同，只有 id 能区分先后，
        所以连续 clear() 时不能把上一次的 id 集合丢掉，否则旧条目会从文件里漏回来。
        _cleared_ts 也只会往前走。
        """
        with self._lock:
            count = len(self._ring)
            for entry in self._ring:
                ident = _as_text(entry.get("id"))
                if ident:
                    self._cleared_ids[ident] = None
            while len(self._cleared_ids) > _CLEARED_IDS_MAX:
                self._cleared_ids.pop(next(iter(self._cleared_ids)))
            moment = _iso(self._now())
            if self._cleared_ts:
                try:
                    if _parse_dt(moment) > _parse_dt(self._cleared_ts):
                        self._cleared_ts = moment
                except TypeError:
                    pass
            else:
                self._cleared_ts = moment
            self._ring.clear()
        return count

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            entries = list(self._ring)
        levels: Dict[str, int] = {name: 0 for name in LEVELS}
        sources: Dict[str, int] = {}
        for entry in entries:
            lvl = _as_text(entry.get("level")).strip().lower()
            levels[lvl] = levels.get(lvl, 0) + 1
            src = _as_text(entry.get("source"))
            sources[src] = sources.get(src, 0) + 1
        files = 0
        total_bytes = 0
        for path in self.files():
            files += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
        return {
            "entries": len(entries),
            "bytes": total_bytes,
            "files": files,
            "levels": levels,
            "sources": sources,
        }


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ── 标准 logging 镜像 ───────────────────────────────────────────────────────

def _level_from_number(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


def _logging_level(level: Any) -> int:
    if isinstance(level, bool):
        return logging.WARNING
    if isinstance(level, int):
        return level
    text = _as_text(level).strip().upper()
    value = getattr(logging, text, None) if text else None
    return value if isinstance(value, int) else logging.WARNING


class MirrorHandler(logging.Handler):
    """把标准 logging 的 WARNING+ 镜像进 AppLog（自己写日志时不触发自己）。"""

    _guard = threading.local()

    def __init__(self, app_log: AppLog, level: Any = logging.WARNING) -> None:
        super().__init__(level=_logging_level(level))
        self.app_log = app_log

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(MirrorHandler._guard, "busy", False):
            return
        MirrorHandler._guard.busy = True
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = _as_text(getattr(record, "msg", ""))
            detail = ""
            if record.exc_info:
                try:
                    detail = "".join(traceback.format_exception(*record.exc_info))
                except Exception:
                    detail = ""
            elif record.exc_text:
                detail = _as_text(record.exc_text)
            self.app_log.record(
                _level_from_number(record.levelno),
                "backend",
                message,
                detail=detail,
                code=_as_text(getattr(record, "name", "")),
                extra={
                    "logger": _as_text(getattr(record, "name", "")),
                    "levelname": _as_text(getattr(record, "levelname", "")),
                    "thread": _as_text(getattr(record, "threadName", "")),
                },
            )
        except Exception:
            pass
        finally:
            MirrorHandler._guard.busy = False

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - 基类命名
        """镜像链路自己出错时静默（绝不再往 logging 里吐）。"""
        return None


_INSTALL_LOCK = threading.Lock()


def install_logging_handler(app_log: AppLog, level: Any = "WARNING") -> logging.Handler:
    """把 MirrorHandler 挂到 root logger 上并返回（同一 AppLog 重复调用只挂一个）。"""
    handler = MirrorHandler(app_log, level=level)
    root = logging.getLogger()
    with _INSTALL_LOCK:
        for existing in list(root.handlers):
            if isinstance(existing, MirrorHandler) and existing.app_log is app_log:
                existing.setLevel(handler.level)
                return existing
        root.addHandler(handler)
    return handler


_DEFAULTS: Dict[str, AppLog] = {}
_DEFAULTS_LOCK = threading.Lock()


def default_app_log(data_dir: Any = None) -> AppLog:
    """进程内单例：同一个 data_dir 复用同一个 AppLog。"""
    try:
        key = str(logs_dir(data_dir))
        if os.name == "nt":
            key = key.lower()
    except Exception:
        key = ""
    with _DEFAULTS_LOCK:
        instance = _DEFAULTS.get(key)
        if instance is None:
            instance = AppLog(data_dir)
            _DEFAULTS[key] = instance
        return instance
