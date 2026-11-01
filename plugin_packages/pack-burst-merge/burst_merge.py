"""pack-burst-merge — 连发合并（从 src/plugins/burst_merge.py 独立出来的目录 mod）。

只做一件事：同一个人刚发过消息时，给这次 AI 调用加一个 delay。既不组装回复，
也不碰 runtime 的其它成员，因此不依赖主体内部结构。
"""

from __future__ import annotations

import time

from typing import Optional

from . import FieldSchema, PluginBase, PluginMeta


class BurstMerge(PluginBase):
    META = PluginMeta(
        id="pack_burst_merge",
        name="连发合并（独立包）",
        description="同一人连发时先等一会，把几条消息合成一次模型调用。",
        category="rhythm",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "cap_ms": FieldSchema(type="int", label="合并窗口(ms)", min=500, max=30000),
            "quiet_ms": FieldSchema(type="int", label="对方静默多久算说完(ms)",
                                    min=200, max=5000),
        },
        default_settings={"enabled": False, "cap_ms": 8000, "quiet_ms": 600},
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {(房间, 发送者): 上次时间戳}
        self._last: dict = {}

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        try:
            if not isinstance(message, dict):
                return None
            if message.get("_burst_handled"):
                return None
            if message.get("kind") != "group":
                return None
            cap = float(self.get("cap_ms", 8000)) / 1000.0
            cap = max(0.5, min(30.0, cap))
            key = (str(message.get("target") or ""), str(message.get("sender_id") or ""))
            now = time.time()
            last = self._last.get(key)
            self._last[key] = now
            if last is None:
                return None
            elapsed = now - last
            if 0 <= elapsed < cap:
                return {"delay": round(cap - elapsed, 3)}
        except Exception:
            return None
        return None
