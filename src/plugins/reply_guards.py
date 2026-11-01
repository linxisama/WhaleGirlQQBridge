"""reply_guards.py - 复读接梗: hint AI to join meme/echo patterns in group chat."""
from __future__ import annotations
import time
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class ReplyGuards(PluginBase):
    META = PluginMeta(
        id="reply_guards",
        name="复读接梗",
        description="群里出现跟风短句时，提示AI自然融入",
        category="social",
        enabled_by_default=False,
        config_schema={
            "enabled":      FieldSchema(type="bool", label="启用"),
            "memeWindowSec":FieldSchema(type="int",  label="梗检测窗口(秒)", min=10),
        },
        default_settings={
            "enabled":       False,
            "memeWindowSec": 120,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {group_id: [(text, sender_id, timestamp), ...]}
        self._history: dict = {}

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        if message.get("kind") != "group":
            return None
        try:
            group_id  = str(message.get("target", ""))
            sender_id = str(message.get("sender_id", ""))
            text      = message.get("text", "").strip()
            if not text or len(text) > 20:
                return None
            now    = time.time()
            window = self.get("memeWindowSec", 120)
            hist   = self._history.setdefault(group_id, [])
            hist.append((text, sender_id, now))
            hist[:] = [(t, s, ts) for t, s, ts in hist if now - ts <= window]
            # Check for 2+ different people saying very similar short text
            recent_texts = [t for t, s, ts in hist if s != sender_id]
            similar_count = sum(1 for t in recent_texts if t == text)
            if similar_count >= 2:
                return {
                    "extra_system": (
                        f"群里有人在跟风说「{text}」，这是一个梗或接龙，"
                        "你可以自然地加入，也可以忽略，随机应变。"
                    )
                }
        except Exception:
            pass
        return None
