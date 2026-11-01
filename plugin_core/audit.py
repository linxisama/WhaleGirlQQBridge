"""plugin_core.audit — JSONL 审计（含仲裁记录）+ 轻量日志（规格 §4/§8）。"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


class Audit:
    """落 <data_dir>/audit/*.jsonl；data_dir=None 时只留内存（demo 用，零磁盘写入）。"""

    def __init__(self, data_dir=None, *, clock=time.time) -> None:
        self.clock = clock
        self.records = []
        self.files = []
        self.dir = None
        if data_dir is not None:
            self.dir = Path(data_dir) / "audit"
            self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict) -> dict:
        ts = round(self.clock(), 3)
        rec = {"ts": ts, "ts_iso": _iso(ts)}
        rec.update(event)
        for key in ("event", "code", "plugin", "message_id"):
            if key in rec and key not in ("ts", "ts_iso"):
                pass
        self.records.append(rec)
        if self.dir is not None:
            name = datetime.datetime.fromtimestamp(ts).strftime("%Y%m%d") + ".jsonl"
            path = self.dir / name
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if str(path) not in self.files:
                self.files.append(str(path))
        return rec

    def select(self, **filters) -> list:
        out = []
        for rec in self.records:
            if all(rec.get(k) == v for k, v in filters.items()):
                out.append(rec)
        return out

    def lines(self) -> list:
        return [json.dumps(r, ensure_ascii=False) for r in self.records]

    def read_back(self) -> list:
        """把落盘文件读回来（证明 JSONL 真的可解析）。"""
        rows = []
        for path in self.files:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        return rows


class Log:
    """内核日志：进内存记录 + 可选 sink。子 logger 自动带插件 id。"""

    def __init__(self, name: str = "plugin_core", *, records=None, sink=None) -> None:
        self.name = name
        self.records = records if records is not None else []
        self.sink = sink

    def child(self, name: str) -> "Log":
        return Log("%s.%s" % (self.name, name), records=self.records, sink=self.sink)

    def _emit(self, level: str, msg: str) -> dict:
        rec = {"level": level, "logger": self.name, "msg": msg, "ts": round(time.time(), 3)}
        self.records.append(rec)
        if self.sink is not None:
            self.sink(rec)
        return rec

    def debug(self, msg: str) -> dict:
        return self._emit("debug", msg)

    def info(self, msg: str) -> dict:
        return self._emit("info", msg)

    def warn(self, msg: str) -> dict:
        return self._emit("warn", msg)

    def error(self, msg: str) -> dict:
        return self._emit("error", msg)

    def lines(self) -> list:
        return ["%-5s [%s] %s" % (r["level"].upper(), r["logger"], r["msg"]) for r in self.records]


def stderr_sink(rec: dict) -> None:  # pragma: no cover - CLI 用
    import sys

    sys.stderr.write("%-5s [%s] %s\n" % (rec["level"].upper(), rec["logger"], rec["msg"]))
