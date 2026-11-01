"""short_followup.py - 短期跟聊: hint for briefer replies within a follow-up window."""
from __future__ import annotations
import time
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class ShortFollowup(PluginBase):
    META = PluginMeta(
        id="short_followup",
        name="短期跟聊",
        description=(
            "成功回复后，同一群友可在跟聊窗口内继续说话，无需重复 @；回复更简短自然。"
            "「句数上限」会真正写进提示词（以前那一项叫「最大token」，"
            "实际提示里写死了不超过 2 句，拨了没有任何效果）。"
        ),
        category="rhythm",
        enabled_by_default=True,
        config_schema={
            "enabled":           FieldSchema(type="bool", label="启用"),
            "followupWindowSec": FieldSchema(type="int",  label="跟聊窗口(秒)", min=30, max=86400),
            "maxSentences":      FieldSchema(
                type="int", label="跟聊时最多几句", min=1, max=10,
                description="默认 2。会写进给模型的提示词。",
            ),
        },
        default_settings={
            "enabled":           True,
            "followupWindowSec": 300,
            "maxSentences":      2,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {(room, sender_id): last_reply_time}
        self._last_replied: dict = {}

    def sentence_cap(self) -> int:
        # 优先用新键；旧配置里存的是 maxTokens（当年叫"最大token"，实际当句数用），
        # 所以只有在新键缺省时才回退读它。不能写成 self.get("maxSentences", 回退)：
        # default_settings 里永远有 maxSentences，回退分支永远不会走到。
        settings = self.settings if isinstance(self.settings, dict) else {}
        value = settings.get("maxSentences")
        if value is None:
            value = settings.get("maxTokens", self.META.default_settings.get("maxSentences", 2))
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 2

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        try:
            room      = str(message.get("target", ""))
            sender_id = str(message.get("sender_id", ""))
            key       = self.key(message, runtime)
            now       = time.time()
            window    = self.get("followupWindowSec", 300)
            last      = self._last_replied.get(key)
            # Only on_sent can open a window; model failures never count.
            if last is not None and now - last < window:
                cap = self.sentence_cap()
                # 跟聊窗口是"这一轮才成立"的说明 → 排到提示词最后（见「缓存稳定性」）
                return self.volatile_hint(
                    runtime,
                    f"对方刚才刚和你说过话，这是短期跟聊，回应要简短自然，不超过{cap}句。")
        except Exception:
            pass
        return None

    def key(self, message, runtime):
        return (getattr(runtime, 'mode', ''), getattr(runtime, 'account', lambda: '')(),
                message.get('kind'), str(message.get('target', '')), str(message.get('sender_id', '')))

    def eligible(self, message, runtime):
        if not self.enabled or message.get('kind') != 'group' or message.get('notice'):
            return False
        if not message.get('sender_id') or not str(message.get('text') or '').strip():
            return False
        # Explicitly talking to someone else is not a follow-up to the bot.
        mentions = message.get('at_ids') or message.get('mentions') or message.get('ats') or []
        if mentions and not message.get('at'):
            return False
        # 审计 L2：下面这个守卫读的 reply_sender_id 目前没有任何上报路径会写入
        # （官方与普通端都不产出该键），所以它实际不生效 —— 保留是为了兼容未来补上该字段，
        # 但不要把它当成"已在保护"的能力。
        reply_sender = message.get('reply_sender_id')
        if reply_sender and str(reply_sender) != str(getattr(runtime.onebot, 'self_id', '')):
            return False
        now=time.time();last=self._last_replied.get(self.key(message,runtime))
        stamp=message.get('time')
        if isinstance(stamp,(float,int)) and last and stamp < last-1:
            return False
        return bool(last is not None and 0 <= now-last < int(self.get('followupWindowSec',300)))

    def on_sent(self, message, reply, runtime):
        if message.get('sender_id') and reply and not message.get('_error_feedback'):
            now=time.time();self._last_replied[self.key(message,runtime)]=now
            self._last_replied={k:v for k,v in self._last_replied.items() if now-v < 86400}
            while len(self._last_replied)>2000:
                self._last_replied.pop(next(iter(self._last_replied)))
