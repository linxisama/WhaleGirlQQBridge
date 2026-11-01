"""typing_indicator.py - 正在输入提示（私聊/群聊可选）。

移植自 JS 插件 ``typing-indicator``（QQ Agent，MIT）。

⚠️ 早先的实现是**空实现**：``on_pre_ai`` 返回 ``{"typing": True}``，而
``PluginManager.run_on_pre_ai`` 只消费 ``block``/``delay``/``extra_system``，
那个键被直接丢掉 —— 插件"装上了但没有任何效果"，也没有报错。

现在真正可用：
- ``on_pre_ai`` 把"发状态"这件事包成一个**零参可调用对象**放进 ``on_ready``，
  由 ``qq_app._work`` 用 ``asyncio.create_task`` 执行、并在发送回复前 await 完
  （见 ``PluginManager.run_on_pre_ai`` 的 docstring）。这样既绕开了
  "``on_pre_ai`` 是同步钩子、不能 await"的限制，又保证状态包早于消息发出。
- 走 OneBot 的 ``set_input_status`` action（NapCat/SnowLuma 等协议端提供；
  ``event_type=1`` 是"正在输入"）。

降级点（相对 JS 版）：
- JS 版在"别人还在打字的最后一条发出后"自动收尾（发 ``event_type`` 收尾包）；
  本项目只能在回复发出时让状态自然过期，没有单独的收尾动作。
- 官方接口没有对应 action，官方模式下本插件不做任何事。
- 协议端不支持该 action 时会返回错误，这里静默忽略（不能因为状态失败影响回复）。
"""

from __future__ import annotations

from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

# QQ NT 的 C2C 输入状态：1 = 正在输入，3 = 正在讲话（录音中）
EVENT_TYPE_TYPING = 1
EVENT_TYPE_SPEAKING = 3


class TypingIndicator(PluginBase):
    META = PluginMeta(
        id="typing_indicator",
        name="正在输入提示",
        description=(
            "私聊（可选群聊）里让机器人像真人一样亮起「正在输入…」：从它决定要回话到消息发出之间，"
            "对方会看到正在输入。零 token：不改提示词、不注册工具、不调模型，只在对的时机补发状态包。"
            "（需要协议端支持 set_input_status；官方接口不支持，此模式下不生效。）"
        ),
        category="utility",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "showTyping": FieldSchema(
                type="bool", label="显示输入提示",
                description="关掉后不再发状态包（与原开关语义一致）。",
            ),
            "privateOnly": FieldSchema(
                type="bool", label="只在私聊生效",
                description="默认开。群聊里「正在输入」观感因协议端而异，需要就关掉这项。",
            ),
            "eventType": FieldSchema(
                type="select", label="状态类型", values=["typing", "speaking"],
                description="typing = 正在输入（默认）；speaking = 正在讲话（录音中）。",
            ),
            "cooldownSec": FieldSchema(
                type="int", label="同一会话冷却（秒）", min=0, max=3600,
                description="默认 5：短时间内连续回复时不重复发状态包。",
            ),
            "minDelayMs": FieldSchema(
                type="int", label="预计很快回时不发（毫秒）", min=0, max=60000,
                description="默认 0（总是发）。填 800 这类值可以避免「秒回」时闪一下正在输入；"
                            "判定依据是上一条消息距今的时间（消息来得太密时就不发状态）。",
            ),
        },
        default_settings={
            "enabled": False,
            "showTyping": True,
            "privateOnly": True,
            "eventType": "typing",
            "cooldownSec": 5,
            "minDelayMs": 0,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {chat_key: 上次发状态的时间戳}
        self._last_sent: dict = {}
        # {chat_key: 上次见到消息的时间戳}（供 minDelayMs 判断"消息来得多密"）
        self._last_message_at: dict = {}

    @staticmethod
    def chat_key(message: dict) -> str:
        return f"{message.get('kind')}:{message.get('target')}"

    def event_type(self) -> int:
        return EVENT_TYPE_SPEAKING if str(self.get("eventType")) == "speaking" else EVENT_TYPE_TYPING

    def should_notify(self, message: dict, now: float, previous_message_at: float = 0.0) -> bool:
        """该不该为这条消息发状态包（纯判定，便于测试）。

        ``previous_message_at`` 是**上一条**消息的到达时间（由调用方在读走之前传入），
        不能让本方法自己去读 ``_last_message_at`` —— 那样拿到的会是刚写进去的当前时间。
        """
        if not isinstance(message, dict) or message.get("notice"):
            return False
        if not self.get("showTyping", True):
            return False
        if self.get("privateOnly", True) and message.get("kind") != "private":
            return False
        if message.get("kind") not in ("private", "group"):
            return False
        target = message.get("target")
        if target is None or str(target).strip() == "":
            return False
        # minDelayMs：消息来得太密时不发状态，避免"秒回"闪一下正在输入
        min_delay_ms = max(0, int(self.get("minDelayMs", 0) or 0))
        if min_delay_ms > 0 and previous_message_at:
            if (now - float(previous_message_at)) * 1000 < min_delay_ms:
                return False
        cooldown = max(0, int(self.get("cooldownSec", 5) or 0))
        last = float(self._last_sent.get(self.chat_key(message)) or 0)
        if cooldown > 0 and now - last < cooldown:
            return False
        return True

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        """把"发正在输入状态"排成一次性异步动作，交给工作循环执行。

        注意：这里**不能**直接 await（``on_pre_ai`` 是同步钩子），所以返回
        ``on_ready`` 回调；真正的动作在 ``_notify`` 里完成。
        """
        if not self.get("enabled"):
            return None
        try:
            import time
            now = time.time()
            key = self.chat_key(message)
            # 先读走"上一条"的时间，再写当前时间，否则 minDelayMs 永远拿到的是当前消息
            previous = float(self._last_message_at.get(key) or 0)
            notify = self.should_notify(message, now, previous)
            self._last_message_at[key] = now
            if not notify:
                return None
            self._last_sent[key] = now
            return {"on_ready": lambda: self._notify(runtime, message)}
        except Exception:
            return None

    async def _notify(self, runtime, message: dict) -> None:
        """真正发状态包。任何失败都吞掉：状态发不出去不该影响回复。"""
        try:
            onebot = getattr(runtime, "onebot", None)
            if onebot is None:
                return
            # 官方接口没有这个 action，直接跳过
            if getattr(runtime, "mode", "") != "onebot":
                return
            user_id = message.get("target")
            await onebot.call("set_input_status", {
                "user_id": int(user_id),
                "event_type": self.event_type(),
            })
        except Exception:
            return
