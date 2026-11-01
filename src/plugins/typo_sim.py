"""typo_sim.py - 打字错误模拟: randomly inject a typo into replies."""
from __future__ import annotations
import random
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema

# Common adjacent-key swaps on a pinyin keyboard (simplified)
_SWAPS = "abcdefghijklmnopqrstuvwxyz"


class TypoSim(PluginBase):
    META = PluginMeta(
        id="typo_sim",
        name="打字错误模拟",
        description="随机在回复中注入打字错误，更像真人",
        category="personality",
        enabled_by_default=False,
        config_schema={
            "enabled":     FieldSchema(type="bool", label="启用"),
            "typoRate":    FieldSchema(type="int",  label="触发概率%", min=0, max=100),
            "corrections": FieldSchema(type="bool", label="发送更正消息"),
        },
        default_settings={
            "enabled":     False,
            "typoRate":    3,
            "corrections": True,
        },
    )

    def _inject_typo(self, text: str):
        """Return (typo_text, corrected_char_index) or (text, None)."""
        # Only work on ASCII/alphanumeric characters to avoid breaking CJK
        candidates = [i for i, ch in enumerate(text) if ch.isascii() and ch.isalpha()]
        if not candidates:
            # For CJK, just drop a random character
            if len(text) < 4:
                return text, None
            idx = random.randint(1, len(text) - 2)
            return text[:idx] + text[idx + 1:], text[idx]
        idx = random.choice(candidates)
        original = text[idx]
        replacement = random.choice(_SWAPS.replace(original.lower(), ""))
        if original.isupper():
            replacement = replacement.upper()
        return text[:idx] + replacement + text[idx + 1:], original

    async def on_post_reply(self, message: dict, reply: str, runtime) -> str:
        if not self.get("enabled") or not reply:
            return reply
        try:
            rate = self.get("typoRate", 3)
            if random.randint(1, 100) > rate:
                return reply
            typo_text, original = self._inject_typo(reply)
            if original is None:
                return reply
            if self.get("corrections", True):
                return f"{typo_text}\x01PACE:800\x01哦不对，{reply}"
            return typo_text
        except Exception:
            return reply
