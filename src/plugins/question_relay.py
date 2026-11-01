"""question_relay.py - 问号接龙: join a chain of question-mark-only messages."""
from __future__ import annotations
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class QuestionRelay(PluginBase):
    META = PluginMeta(
        id="question_relay",
        name="问号接龙",
        description="群里连续发问号时，机器人跟着发一个问号",
        category="social",
        enabled_by_default=False,
        config_schema={
            "enabled":       FieldSchema(type="bool",   label="启用"),
            "relayChar":     FieldSchema(type="string", label="接龙字符"),
            "questionChars": FieldSchema(type="string", label="触发字符集"),
            "minChain":      FieldSchema(type="int",    label="最小链长", min=1),
            "lookback":      FieldSchema(type="int",    label="回溯条数", min=1),
        },
        default_settings={
            "enabled":       False,
            "relayChar":     "？",
            "questionChars": "?？",
            "minChain":      2,
            "lookback":      30,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {group_id: [text, ...]}  — recent messages
        self._history: dict = {}
        # {group_id: bool}  — bot already joined current chain
        self._joined: dict = {}

    def _is_question(self, text: str) -> bool:
        q_chars = self.get("questionChars", "?？")
        return bool(text) and all(ch in q_chars for ch in text.strip())

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        if not self.get("enabled"):
            return None
        if message.get("kind") != "group":
            return None
        try:
            group_id = str(message.get("target", ""))
            text     = message.get("text", "").strip()

            hist = self._history.setdefault(group_id, [])

            if not self._is_question(text):
                # Chain broken — reset
                hist.append(text)
                hist[:] = hist[-self.get("lookback", 30):]
                self._joined[group_id] = False
                return None

            hist.append(text)
            hist[:] = hist[-self.get("lookback", 30):]

            # Count consecutive question messages from tail
            chain = 0
            for t in reversed(hist):
                if self._is_question(t):
                    chain += 1
                else:
                    break

            if chain >= self.get("minChain", 2) and not self._joined.get(group_id):
                self._joined[group_id] = True
                return self.get("relayChar", "？")
        except Exception:
            pass
        return None
