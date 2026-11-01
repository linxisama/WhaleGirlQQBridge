"""mention_nudge_guard.py - 重复艾特忽略: detect excessive @-mentions and hint AI."""
from __future__ import annotations
import time
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class MentionNudgeGuard(PluginBase):
    META = PluginMeta(
        id="mention_nudge_guard",
        name="重复艾特忽略",
        description="同一人短时间内多次@时，提示机器人冷静应对",
        category="rhythm",
        enabled_by_default=True,
        config_schema={
            "enabled":     FieldSchema(type="bool", label="启用"),
            "windowSec":    FieldSchema(type="int",  label="统计窗口(秒)", min=1),
            "maxMentions":  FieldSchema(type="int",  label="最大@次数", min=1),
        },
        default_settings={
            "enabled":    True,
            "windowSec":   60,
            "maxMentions": 3,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {(room, sender_id): [timestamp, ...]}
        self._mentions: dict = {}

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        if not message.get("at"):
            return None
        try:
            room      = str(message.get("target", ""))
            sender_id = str(message.get("sender_id", ""))
            key       = (room, sender_id)
            now       = time.time()
            window    = self.get("windowSec", 60)
            max_m     = self.get("maxMentions", 3)

            times = self._mentions.setdefault(key, [])
            times.append(now)
            times[:] = [t for t in times if now - t <= window]

            if len(times) > max_m:
                return {
                    "extra_system": (
                        f"这个人刚才在{window}秒内@了你{len(times)}次，"
                        "有些啰嗦。你可以自然地提一句，但保持冷静和幽默。"
                    )
                }
        except Exception:
            pass
        return None
