"""repeat_echo.py - 群聊复读机: echo when multiple people say the same text."""
from __future__ import annotations
import re
import time
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema

# 媒体占位形式（见 src/qq_transports.py）：[图片] [表情:微笑] [表情14] [表情] [语音]
# [视频] [文件:x] [合并转发] [卡片消息:标题]。这些**不是**"有人在说同一句话"。
# 注意 [表情\d*]：查不到名字的表情保持数字形式（[表情99999]），也要算占位。
MEDIA_PLACEHOLDER_RE = re.compile(
    r"^\[(?:图片|表情\d*|语音|视频|文件|合并转发|卡片消息)(?::[^\]]*)?\]$"
)


def is_echoable(text: str) -> bool:
    """这段文本值不值得复读。

    纯媒体占位不复读：传输层保留媒体段之后，多人发表情包会得到相同的
    ``[表情:微笑]`` 文本，复读它就成了刷屏 —— 那不是"复读"，是噪声。
    纯符号（哈哈、。。。）仍然算，那确实是群友在复读。
    """
    value = str(text or "").strip()
    if not value:
        return False
    return not MEDIA_PLACEHOLDER_RE.match(value)


class RepeatEcho(PluginBase):
    META = PluginMeta(
        id="repeat_echo",
        name="群聊复读机",
        description=(
            "当多人说了相同的话时，机器人跟着复读。"
            "纯媒体占位（[图片]、[表情:微笑]、[语音]…）不复读 —— "
            "多人发表情包会得到相同占位文本，复读它只是刷屏。"
        ),
        category="social",
        enabled_by_default=False,
        config_schema={
            "enabled":          FieldSchema(type="bool", label="启用"),
            "pairWindowSec":    FieldSchema(type="int",  label="配对窗口(秒)", min=10),
            "maxLen":           FieldSchema(type="int",  label="最大长度(0=不限)", min=0),
            "cooldownSec":      FieldSchema(type="int",  label="复读冷却(秒)", min=0),
            "maxPerWindow":     FieldSchema(type="int",  label="窗口内最多复读次数", min=1),
            "windowMin":        FieldSchema(type="int",  label="统计窗口(分钟)", min=1),
            "maxPerHour":       FieldSchema(type="int",  label="每小时最多复读次数", min=1),
            "sameTextBlockMin": FieldSchema(type="int",  label="同一文本复读屏蔽(分钟)", min=0),
            "onlyWhenIdle":     FieldSchema(
                type="bool", label="只在没人说话时复读",
                description="打开后，若最近 windowMin 内有别的消息活动则跳过，避免插话。",
            ),
        },
        default_settings={
            "enabled":          False,
            "pairWindowSec":    180,
            "maxLen":           0,
            "cooldownSec":      90,
            "maxPerWindow":     4,
            "windowMin":        30,
            "maxPerHour":       15,
            "sameTextBlockMin": 30,
            "onlyWhenIdle":     False,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {group_id: [(text, sender_id, timestamp), ...]}
        self._history: dict = {}
        # {group_id: [echo_timestamp, ...]}
        self._echoes: dict = {}
        # {group_id: {text: last_echo_timestamp}}
        self._text_echoed: dict = {}
        # {group_id: 最近一条消息时间}（给 onlyWhenIdle 用）
        self._last_activity: dict = {}

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        if not self.get("enabled"):
            return None
        if message.get("kind") != "group":
            return None
        # 通知事件（拍一拍/撤回/入群等）不是聊天内容
        if message.get("notice"):
            return None
        try:
            text = str(message.get("text", "") or "").strip()
            if not is_echoable(text):
                return None

            max_len = self.get("maxLen", 0)
            if max_len > 0 and len(text) > max_len:
                return None

            group_id  = str(message.get("target", ""))
            sender_id = str(message.get("sender_id", ""))
            now = time.time()

            # 先看在不在"安静期"（此时才知道这条消息之前的活动时间）
            previous_activity = self._last_activity.get(group_id)
            self._last_activity[group_id] = now
            if self.get("onlyWhenIdle") and previous_activity is not None:
                idle_sec = self.get("pairWindowSec", 180)
                if now - previous_activity < idle_sec:
                    # 还有人在说话，不插话；但仍要维护历史以便安静后能配对
                    self._push_history(group_id, text, sender_id, now)
                    return None

            self._push_history(group_id, text, sender_id, now)
            hist = self._history[group_id]

            # Find pair: same text from a DIFFERENT sender within window
            senders_for_text = {
                s for t, s, ts in hist if t == text and s != sender_id
            }
            if not senders_for_text:
                return None

            # Cooldown
            echo_list = self._echoes.setdefault(group_id, [])
            cooldown = self.get("cooldownSec", 90)
            if echo_list and now - echo_list[-1] < cooldown:
                return None

            # Same-text block
            text_map = self._text_echoed.setdefault(group_id, {})
            block_sec = self.get("sameTextBlockMin", 30) * 60
            if block_sec > 0 and text in text_map and now - text_map[text] < block_sec:
                return None

            # Window quota
            window_sec = self.get("windowMin", 30) * 60
            recent_echoes = [ts for ts in echo_list if now - ts <= window_sec]
            if len(recent_echoes) >= self.get("maxPerWindow", 4):
                return None

            # Hourly quota
            hourly = [ts for ts in echo_list if now - ts <= 3600]
            if len(hourly) >= self.get("maxPerHour", 15):
                return None

            # Echo!
            echo_list.append(now)
            echo_list[:] = [ts for ts in echo_list if now - ts <= 3600]
            text_map[text] = now
            return text
        except Exception:
            return None

    def _push_history(self, group_id: str, text: str, sender_id: str, now: float) -> None:
        hist = self._history.setdefault(group_id, [])
        hist.append((text, sender_id, now))
        pair_window = self.get("pairWindowSec", 180)
        hist[:] = [(t, s, ts) for t, s, ts in hist if now - ts <= pair_window]
