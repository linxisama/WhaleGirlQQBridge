"""burst_merge.py - 连发合并: delay AI call when same sender just sent a message."""
from __future__ import annotations
import time
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class BurstMerge(PluginBase):
    META = PluginMeta(
        id="burst_merge",
        name="连发合并",
        description=("同一人在短时间内连续发消息时，等他发完再一起回复。"
                     "被 @ 或被叫昵称时**不等待**、立刻回；其余情况对方静默 quietMs（默认 0.6 秒）"
                     "就开口，最长不超过合并窗口 capMs。"),
        category="rhythm",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "capMs":   FieldSchema(type="int",  label="合并窗口(ms)", min=500, max=30000),
            "quietMs": FieldSchema(type="int",  label="对方静默多久就开口(ms)", min=200, max=5000),
        },
        default_settings={
            "enabled": True,
            "capMs":   8000,
            "quietMs": 600,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # key: (room_id, sender_id) -> last_seen timestamp (float)
        self._last_msg: dict = {}

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if message.get('_burst_handled'):
            return None
        if not self.get("enabled"):
            return None
        # 连发合并只对群聊生效 —— 私聊不需要等待对方"发完"
        if message.get("kind") != "group":
            return None
        try:
            cap_sec = self.get("capMs", 8000) / 1000.0
            # 按次计费（中转站每次固定扣费）时把合并窗口放宽：同一个人连发的几条消息
            # 合成一次调用 = 少扣几次钱。上限 30 秒，别让用户干等。
            try:
                if getattr(getattr(self, "_manager", None), "per_call_mode", False):
                    cap_sec = min(30.0, max(cap_sec, cap_sec * 2.5))
            except Exception:
                pass
            room = message.get("target", "")
            sender_id = message.get("sender_id", "")
            key = (room, sender_id)
            now = time.time()
            last = self._last_msg.get(key)
            self._last_msg[key] = now
            if last is not None:
                elapsed = now - last
                if elapsed < cap_sec:
                    remaining = cap_sec - elapsed
                    return {"delay": remaining}
        except Exception:
            pass
        return None
