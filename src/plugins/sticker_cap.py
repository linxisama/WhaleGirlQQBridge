"""sticker_cap.py - 表情包库上限（防撑爆）。

移植自 JS 插件 ``sticker-cap``（QQ Agent，MIT）。JS 版的用途：防止有人一直发
表情包让机器人存，把库和磁盘撑爆。

与运行时 StickerLibrary 相连：只清理索引登记的收藏，不删除手动图库、索引文件或外部目录。
默认只统计；关闭“只统计不删除”才真正执行LRU清理。收集入口在sticker_steal。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

# 表情库默认位置（相对项目根）：本项目还没有真正的表情库，这里作为将来落点
DEFAULT_STICKER_DIR = "data/qq-only/stickers"
DEFAULT_MAX_ITEMS = 300
DEFAULT_MAX_MB = 200


def parse_id_list(raw) -> set:
    """解析逗号/空格/中文逗号分隔的 QQ 号列表为 set（用于屏蔽名单）。"""
    import re
    return {item for item in re.split(r"[,，\s]+", str(raw or "")) if item}


def dir_usage(path: Path) -> tuple:
    """返回 ``(文件数, 总字节)``；目录不存在返回 ``(0, 0)``，不抛异常。"""
    total = 0
    count = 0
    try:
        if not path.is_dir():
            return 0, 0
        for item in path.rglob("*"):
            if item.is_file():
                count += 1
                total += item.stat().st_size
    except OSError:
        return count, total
    return count, total


def pick_evictions(entries: list, max_items: int) -> list:
    """按"最久没用"挑出要淘汰的条目。

    ``entries`` 元素形如 ``{"name": ..., "used_at": 时间戳, "size": 字节}``；
    返回应删除的条目（从最旧开始）。``max_items <= 0`` 表示不限制，返回空列表。
    """
    if max_items <= 0:
        return []
    ordered = sorted(entries, key=lambda e: float(e.get("used_at") or 0))
    excess = len(ordered) - int(max_items)
    return ordered[:excess] if excess > 0 else []


class StickerCap(PluginBase):
    META = PluginMeta(
        id="sticker_cap",
        name="表情包库上限",
        description=(
            "防止有人一直发表情包把库和磁盘撑爆：按数量上限做 LRU 淘汰、按磁盘配额清理、"
            "可屏蔽指定人的表情。"
            "只管理机器人实际收藏的表情库和索引，不删除外部目录或手动图库；"
            "收集由“偷表情包”插件完成，/表情容量 可查看用量。"
        ),
        category="media",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "stickerDir": FieldSchema(
                type="string", label="表情库目录",
                description="兼容旧配置；当前运行时固定管理 data/sticker_library，只会清理索引登记的收藏图片。",
                file_picker="directory",
            ),
            "maxItems": FieldSchema(
                type="int", label="最多存多少个", min=0, max=100000,
                description="0 = 不限制。超过后按最久未用淘汰。",
            ),
            "maxSizeMb": FieldSchema(
                type="int", label="磁盘配额（MB）", min=0, max=102400,
                description="0 = 不限制。超过后按最久未用删除，直到降到配额以内。",
            ),
            "blockedSenders": FieldSchema(
                type="string", label="不收这些人的表情",
                description="逗号分隔 QQ 号。",
            ),
            "dryRun": FieldSchema(
                type="bool", label="只统计不删除",
                description="默认开（安全）：先看看会删多少，确认后再关掉真正执行。",
            ),
            "checkIntervalSec": FieldSchema(
                type="int", label="检查间隔（秒）", min=60, max=86400,
                description="默认 3600。",
            ),
        },
        default_settings={
            "enabled": False,
            "stickerDir": DEFAULT_STICKER_DIR,
            "maxItems": DEFAULT_MAX_ITEMS,
            "maxSizeMb": DEFAULT_MAX_MB,
            "blockedSenders": "",
            "dryRun": True,
            "checkIntervalSec": 3600,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        self._last_check = 0.0
        self._last_report = ""

    def blocked(self, sender_id) -> bool:
        return str(sender_id or "") in parse_id_list(self.get("blockedSenders", ""))

    def sticker_dir(self) -> Path:
        raw = str(self.get("stickerDir") or DEFAULT_STICKER_DIR)
        root = Path(getattr(self._manager, "state_dir", ".")).resolve()
        # state_dir 指向 data/qq-only，往上找到项目根更稳；找不到就用相对路径本身
        for candidate in (root.parent.parent, root.parent, root):
            if (candidate / "src").is_dir():
                return (candidate / raw).resolve()
        return (root / raw).resolve()

    def usage(self) -> dict:
        """当前表情库用量（不依赖表情库存在）。"""
        path = self.sticker_dir()
        count, total = dir_usage(path)
        return {"dir": str(path), "exists": path.is_dir(), "count": count,
                "bytes": total, "mb": round(total / (1024 * 1024), 2)}

    def enforce(self, runtime=None, dry_override=None) -> dict:
        """按配置做一次清理；返回处理结果（dryRun 时只统计不删）。"""
        if runtime is not None:
            store=getattr(runtime,'stickers',None)
            if store is None:return {'usage':{'count':0,'bytes':0},'dryRun':True,'removed':0,'freed_bytes':0,'reason':'本地表情库不可用'}
            entries=store.entries()
            total=sum(v['bytes'] for v in entries)
            ordered=sorted(entries,key=lambda e:(e.get('last_used',0),e.get('created',0)))
            max_items=int(self.get('maxItems',DEFAULT_MAX_ITEMS) or 0)
            max_mb=int(self.get('maxSizeMb',DEFAULT_MAX_MB) or 0)
            remaining=len(entries);remaining_bytes=total;doomed=[]
            for entry in ordered:
                if (max_items<=0 or remaining<=max_items) and (max_mb<=0 or remaining_bytes<=max_mb*1024*1024):break
                doomed.append(entry);remaining-=1;remaining_bytes-=entry['bytes']
            dry=bool(self.get('dryRun',True)) if dry_override is None else bool(dry_override)
            if not dry:
                for entry in doomed:store.remove(entry['id'])
            return {'usage':{'dir':str(store.images),'exists':True,'count':len(entries),'bytes':total,'mb':round(total/(1024*1024),2)},
                    'dryRun':dry,'removed':len(doomed),'freed_bytes':sum(v['bytes'] for v in doomed),
                    'reason':f'{"只统计：可清理" if dry else "已清理"} {len(doomed)} 张本地收藏表情'}
        import os
        usage = self.usage()
        max_items = int(self.get("maxItems", DEFAULT_MAX_ITEMS) or 0)
        max_mb = int(self.get("maxSizeMb", DEFAULT_MAX_MB) or 0)
        dry = bool(self.get("dryRun", True))
        report = {"usage": usage, "dryRun": dry, "removed": 0, "freed_bytes": 0,
                  "reason": ""}
        if not usage["exists"]:
            report["reason"] = "表情库目录不存在，无需清理"
            return report

        path = Path(usage["dir"])
        try:
            files = [f for f in path.rglob("*") if f.is_file()]
            entries = [{"path": f, "name": f.name, "used_at": f.stat().st_mtime,
                        "size": f.stat().st_size} for f in files]
        except OSError as exc:
            report["reason"] = f"读目录失败：{exc}"
            return report

        doomed = list(pick_evictions(entries, max_items))
        # 再按磁盘配额淘汰：从剩余最旧的开始
        remaining = [e for e in sorted(entries, key=lambda e: e["used_at"]) if e not in doomed]
        total = sum(e["size"] for e in doomed)
        limit = max_mb * 1024 * 1024
        if limit > 0:
            for entry in remaining:
                if total <= limit and usage["bytes"] - total <= limit:
                    break
                doomed.append(entry)
                total += entry["size"]

        report["removed"] = len(doomed)
        report["freed_bytes"] = total
        if not doomed:
            report["reason"] = "未超过上限"
            return report
        if dry:
            report["reason"] = f"只统计：本应删除 {len(doomed)} 个，可释放 {total} 字节"
            return report
        removed = 0
        freed = 0
        for entry in doomed:
            try:
                entry["path"].unlink()
                removed += 1
                freed += entry["size"]
            except OSError:
                continue
        report["removed"] = removed
        report["freed_bytes"] = freed
        report["reason"] = f"已删除 {removed} 个，释放 {freed} 字节"
        return report

    def on_ingest(self, message, runtime):
        if str(message.get('text','')).strip()!='/表情容量':return None
        report=self.enforce(runtime,dry_override=True)
        usage=report['usage']
        return f"本地收藏 {usage['count']} 张，占用 {usage['bytes']/1024/1024:.2f} MB。{report['reason']}；查询不会删除图片。"

    async def observe(self,message,runtime):
        if message.get('notice'):return
        # 每条消息都全库扫描的代价太大：默认上限 800 张时，entries() 会对每个条目
        # deepcopy + is_file()（≈800 次 stat），dryRun 关掉后连删除也变成"每条消息一次"。
        # 这里和 tick 用同一个间隔（checkIntervalSec，默认 1 小时）节流；
        # 想立刻清理可以发 `/表情容量`，那条命令走的是即时查询路径。
        if not self.get("enabled"):
            return
        interval = max(60, int(self.get("checkIntervalSec", 3600) or 3600))
        now = time.time()
        if now - self._last_check < interval:
            return
        self._last_check = now
        self.enforce(runtime)

    async def tick(self, runtime) -> None:
        if not self.get("enabled"):
            return
        interval = max(60, int(self.get("checkIntervalSec", 3600) or 3600))
        now = time.time()
        if now - self._last_check < interval:
            return
        self._last_check = now
        report = self.enforce(runtime)
        self._last_report = report.get("reason", "")
        print(f"[plugins] sticker_cap: {self._last_report}")
