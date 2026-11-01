"""send_pacing.py - 打字速度: inject pacing markers between sentences."""
from __future__ import annotations
import random
import re
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema

_SPLIT_RE = re.compile(r'([。！？!?\n]+)')


def has_media(meta) -> bool:
    """回复元数据里是否带媒体段（图片/表情等）。

    ``meta`` 形如 ``{'mentions': [...], 'media': [...]}``（见 src/plugin_segments.py）。
    """
    if not isinstance(meta, dict):
        return False
    media = meta.get("media")
    return isinstance(media, (list, tuple)) and len(media) > 0


# 入站消息里出现这些占位符，说明这条是"配着媒体"来的（见 qq_transports.onebot_message）
MEDIA_HINTS = ("[图片]", "[表情", "[语音]", "[视频]", "[文件:", "[合并转发]", "[卡片消息")


def inbound_has_media(message) -> bool:
    """入站消息是否带媒体（图片/表情/语音/转发/卡片）。

    用途：``mediaPacing`` 关掉时，带媒体的这一轮不再拆句 —— 机器人刚发了图，
    文字再拆成两条会像漏发。只看**入站**消息，不看 reply_meta：
    reply_meta 是要发出去的段，用它判断会把收到的图原样发回去。
    """
    if not isinstance(message, dict):
        return False
    media = message.get("media")
    if isinstance(media, (list, tuple)) and len(media) > 0:
        return True
    text = str(message.get("text") or "")
    return any(hint in text for hint in MEDIA_HINTS)


class SendPacing(PluginBase):
    META = PluginMeta(
        id="send_pacing",
        name="打字速度",
        description=(
            "在句子之间插入打字延迟标记，模拟真人打字节奏。"
            "「媒体消息也分段」关掉时，带图片/表情的回复不再拆句 —— "
            "否则媒体段与文字会被拆开成两条消息，看起来像机器人漏发了图。"
        ),
        category="rhythm",
        enabled_by_default=False,
        config_schema={
            "enabled":        FieldSchema(type="bool", label="启用"),
            "minMsPerChar":   FieldSchema(type="int",  label="每字最小延迟(ms)", min=0),
            "maxMsPerChar":   FieldSchema(type="int",  label="每字最大延迟(ms)", min=0),
            "baseMs":         FieldSchema(type="int",  label="基础延迟(ms)",     min=0),
            "minMs":          FieldSchema(type="int",  label="最小总延迟(ms)",   min=0),
            "maxMs":          FieldSchema(type="int",  label="最大总延迟(ms)",   min=0),
            "mediaPacing":    FieldSchema(type="bool", label="媒体消息也分段"),
        },
        default_settings={
            "enabled":      False,
            "minMsPerChar": 700,
            "maxMsPerChar": 1100,
            "baseMs":       0,
            "minMs":        300,
            "maxMs":        60000,
            "mediaPacing":  True,
        },
    )

    def _calc_delay(self, text: str) -> int:
        min_c = self.get("minMsPerChar", 700)
        max_c = self.get("maxMsPerChar", 1100)
        base  = self.get("baseMs", 0)
        min_t = self.get("minMs", 300)
        max_t = self.get("maxMs", 60000)
        if max_c < min_c:
            min_c, max_c = max_c, min_c
        per_char = sum(random.randint(min_c, max_c) for _ in text if not text.isspace())
        total = base + per_char
        return max(min_t, min(max_t, total))

    async def on_post_reply(self, message: dict, reply: str, runtime) -> str:
        if not self.get("enabled") or not reply:
            return reply
        try:
            # 带媒体的这一轮默认不拆句（mediaPacing=False 时）：机器人刚发了图，
            # 文字再拆成两条会像漏发。以前这个开关根本没被读过。
            if not self.get("mediaPacing", True) and inbound_has_media(message):
                return reply
            parts = _SPLIT_RE.split(reply)
            # Rebuild sentences: content + their trailing punctuation
            sentences = []
            i = 0
            while i < len(parts):
                chunk = parts[i]
                if i + 1 < len(parts) and _SPLIT_RE.match(parts[i + 1]):
                    chunk += parts[i + 1]
                    i += 2
                else:
                    i += 1
                chunk = chunk.strip()
                if chunk:
                    sentences.append(chunk)

            if len(sentences) <= 1:
                return reply

            result_parts = [sentences[0]]
            for sent in sentences[1:]:
                delay_ms = self._calc_delay(sent)
                result_parts.append(f"\x01PACE:{delay_ms}\x01")
                result_parts.append(sent)
            return "".join(result_parts)
        except Exception:
            return reply
