"""anti_repeat.py - 打断复读: block bot from repeating the same response too often."""
from __future__ import annotations
import time
import hashlib
import json
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class AntiRepeat(PluginBase):
    META = PluginMeta(
        id="anti_repeat",
        name="打断复读",
        description="同一消息被多次触发时，阻止机器人重复回复",
        category="rhythm",
        enabled_by_default=True,
        config_schema={
            "enabled":        FieldSchema(type="bool", label="启用"),
            "maxRepeat":      FieldSchema(type="int",  label="最多重复次数", min=1),
            "blockWindowSec": FieldSchema(type="int",  label="统计窗口(秒)", min=1),
        },
        default_settings={
            "enabled":        True,
            "maxRepeat":      3,
            "blockWindowSec": 60,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {(group_id, text): [timestamp, ...]}
        self._seen: dict = {}

    def on_block(self, message: dict, runtime) -> bool:
        if not self.get("enabled"):
            return False
        if message.get("kind") != "group":
            return False
        try:
            group_id = str(message.get("target", ""))
            text     = message.get("text", "").strip()
            if not text:
                return False
            key     = (group_id, text)
            # Different pictures all render as [图片]. Include their identity so
            # the guard cannot discard unrelated stickers before vision runs.
            media = message.get("media") or []
            if any(isinstance(item, dict) and item.get("type") == "image" for item in media):
                identity = json.dumps(media, ensure_ascii=False, sort_keys=True, default=str)
                key += (hashlib.sha256(identity.encode("utf-8")).hexdigest(),)
            window  = self.get("blockWindowSec", 60)
            max_rep = self.get("maxRepeat", 3)
            now     = time.time()
            times   = self._seen.setdefault(key, [])
            times.append(now)
            times[:] = [t for t in times if now - t <= window]
            return len(times) > max_rep
        except Exception:
            return False
