"""plugin_core.mock — mock 传输层 / mock 模型 / mock 消息源（规格 §8 T1）。

全部只在内存里记账：不联网、不起后台线程，任何真实协议端都不在此文件里。
"""

from __future__ import annotations

import time

from .api import KernelError


class MockTransport:
    """只记内存的传输层：connect / send / fetch / close。"""

    def __init__(self, *, clock=time.time) -> None:
        self.clock = clock
        self.connected = False
        self.closed = False
        self.connects = 0
        self.history = []

    def connect(self, config=None) -> dict:
        self.connected = True
        self.connects += 1
        return {"transport": "mock", "connected": True, "config": dict(config or {})}

    def send(self, draft) -> dict:
        if not self.connected:
            raise KernelError("mock 传输层未 connect，不能发送")
        rec = {
            "conversation": draft.conversation,
            "text": draft.text,
            "media": list(draft.media),
            "plugin": draft.plugin,
            "ts": round(self.clock(), 3),
        }
        self.history.append(rec)
        return rec

    def fetch(self, url, *, method="GET", body=None, timeout=10.0):
        raise KernelError("离线内核：mock 传输层不发起真实网络请求（url=%r）" % (url,))

    def close(self) -> None:
        self.closed = True
        self.connected = False

    # --- 断言辅助 ---
    def sent_texts(self) -> list:
        return [rec["text"] for rec in self.history]

    def last(self):
        return self.history[-1] if self.history else None

    def reset(self) -> None:
        self.history = []


class MockModel:
    """可换的 mock 模型：记录每次实际发出的 messages，返回确定性文本。"""

    def __init__(self, reply=None, *, prefix="模型回声：") -> None:
        self.reply = reply
        self.prefix = prefix
        self.calls = []
        self.calls_count = 0

    def chat(self, messages, *, max_chars=None) -> str:
        self.calls.append({"messages": [dict(item) for item in messages], "max_chars": max_chars})
        self.calls_count += 1
        text = self._reply(messages)
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
        return text

    def _reply(self, messages) -> str:
        if callable(self.reply):
            return str(self.reply(messages))
        if isinstance(self.reply, str):
            return self.reply
        user = ""
        for item in reversed(messages):
            if item.get("role") == "user":
                user = str(item.get("content") or "")
                break
        return "%s%s" % (self.prefix, user)

    # --- 断言辅助 ---
    def last_prompt(self) -> list:
        return self.calls[-1]["messages"] if self.calls else []

    def system_prompts(self) -> list:
        out = []
        for call in self.calls:
            for item in call["messages"]:
                if item.get("role") == "system":
                    out.append(item["content"])
        return out

    def reset(self) -> None:
        self.calls = []
        self.calls_count = 0


class MockSource:
    """mock 消息源：一条入站消息 → 交给 Host.handle 的字典。

    故意多带 source / raw_seq 两个传输层字段：Host 归一化只认白名单，
    这两个字段必须在下游消失（证明「无上游报文」）。
    """

    def __init__(
        self,
        *,
        session_id="100",
        kind="group",
        session_name="测试群",
        sender=None,
        clock=time.time,
    ) -> None:
        self.session_id = session_id
        self.kind = kind
        self.session_name = session_name
        self.sender = dict(
            sender or {"id": "10001", "name": "测试用户", "role": "member", "is_bot": False}
        )
        self.clock = clock
        self.seq = 0

    def message(
        self,
        text="",
        *,
        message_id=None,
        segments=None,
        mentions=None,
        reply_to=None,
    ) -> dict:
        self.seq += 1
        return {
            "session": {"id": self.session_id, "kind": self.kind, "name": self.session_name},
            "sender": dict(self.sender),
            "message": {
                "id": message_id or "m-%d" % self.seq,
                "text": text,
                "segments": segments if segments is not None else [{"type": "text", "text": text}],
                "mentions": list(mentions or ()),
                "reply_to": reply_to,
                "ts": round(self.clock(), 3),
            },
            "source": "mock",
            "raw_seq": self.seq,
        }


def message(text="", **kwargs) -> dict:
    """一次性构造一条 mock 入站消息。"""
    return MockSource(**{k: kwargs.pop(k) for k in ("session_id", "kind", "session_name", "sender") if k in kwargs}).message(text, **kwargs)
