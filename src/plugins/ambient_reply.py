"""ambient_reply.py - 群聊随机接话：不 @ 也能偶尔自然回一句。

默认关闭。开启后只统计"没有被点名"的普通群消息，按概率 + 冷却 + 每小时上限
决定是否让这条消息进入正常 AI 回复流程（不会为每条消息都付出一次模型调用）。
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from . import FieldSchema, PluginBase, PluginMeta

_TZ_CST = timezone(timedelta(hours=8))


class AmbientReply(PluginBase):
    META = PluginMeta(
        id="ambient_reply",
        name="群聊随机接话",
        description=(
            "监控群聊，在没人 @ 机器人时按概率偶尔自然接话（像群友一样参与聊天）。"
            "带冷却与每小时上限，避免刷屏；只对已监听群生效，不处理命令与通知。"
        ),
        category="social",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "groups": FieldSchema(type="string", label="目标群(逗号分隔,留空=全部)",
                                  description="留空表示对所有已监听群生效"),
            "percent": FieldSchema(type="int", label="接话概率%", min=1, max=100),
            "cooldownSec": FieldSchema(type="int", label="同群冷却(秒)", min=10),
            "maxPerHour": FieldSchema(type="int", label="每群每小时上限", min=1),
            "minChars": FieldSchema(type="int", label="最短消息字数", min=1),
            "activeHours": FieldSchema(type="string", label="活跃时段(HH:MM-HH:MM)"),
            "boostQuestion": FieldSchema(type="int", label="带问句额外概率%", min=0, max=50),
            "ignorePrefix": FieldSchema(type="string", label="忽略开头字符"),
        },
        default_settings={
            "enabled": True,
            "groups": "",
            "percent": 12,
            "cooldownSec": 90,
            "maxPerHour": 20,
            "minChars": 2,
            "activeHours": "08:00-24:00",
            "boostQuestion": 10,
            "ignorePrefix": "/#",
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {group_id: [最近接话时间, ...]}，只保留最近一小时
        self._recent: dict[str, list[float]] = {}

    # ── 内部判定 ─────────────────────────────────────────────────────
    def _groups(self) -> set[str]:
        raw = str(self.get("groups", "") or "").replace("，", ",")
        return {item.strip() for item in raw.split(",") if item.strip()}

    @staticmethod
    def _in_hours(spec: str, now: datetime) -> bool:
        try:
            start, end = str(spec).split("-", 1)
            sh, sm = (int(x) for x in start.strip().split(":"))
            eh, em = (int(x) for x in end.strip().split(":"))
        except Exception:
            return True
        minute = now.hour * 60 + now.minute
        begin, finish = sh * 60 + sm, eh * 60 + em
        if finish <= begin:          # 跨零点，例如 22:00-02:00
            return minute >= begin or minute < finish
        return begin <= minute < finish

    def _eligible(self, message: dict, now: float) -> bool:
        if message.get("kind") != "group" or message.get("notice"):
            return False
        text = str(message.get("text") or "").strip()
        if len(text) < int(self.get("minChars", 4) or 4):
            return False
        prefix = str(self.get("ignorePrefix", "/#") or "")
        if text and prefix and text[0] in prefix:
            return False
        if message.get("at"):
            return False              # 点名消息走正常流程，不占随机额度
        target = str(message.get("target") or "")
        groups = self._groups()
        if groups and target not in groups:
            return False
        if not self._in_hours(str(self.get("activeHours", "") or ""), datetime.now(_TZ_CST)):
            return False
        return True

    # ── 钩子 ───────────────────────────────────────────────────────
    def wants_reply(self, message: dict, runtime) -> bool:
        """返回 True 表示"让我也插一句"，这条消息进入正常 AI 回复。"""
        now = time.time()
        if not self._eligible(message, now):
            return False
        group = str(message.get("target") or "")
        recent = [t for t in self._recent.get(group, []) if now - t < 3600]
        self._recent[group] = recent
        cooldown = float(self.get("cooldownSec", 180) or 0)
        if recent and cooldown > 0 and now - recent[-1] < cooldown:
            return False
        if len(recent) >= int(self.get("maxPerHour", 8) or 8):
            return False
        chance = float(self.get("percent", 6) or 0)
        text = str(message.get("text") or "")
        if any(mark in text for mark in ("?", "？", "吗", "怎么", "为什么", "啥", "呢")):
            chance += float(self.get("boostQuestion", 0) or 0)
        if random.random() * 100 >= chance:
            return False
        recent.append(now)
        return True
