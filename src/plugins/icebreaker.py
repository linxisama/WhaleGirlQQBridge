"""icebreaker.py - 定时暖场：每隔几小时按人设自动开个话头。

移植自 JS 插件 ``icebreaker``（QQ Agent，MIT）。

行为：在"睡觉时间"（默认 23:30~08:00）之外，每隔「下限~上限」小时随机挑一个时间点，
给指定会话（或自动挑最近活跃的会话）发一条按当前人设现场生成的暖场消息。

本项目对应的挂载点：``tick``（约每 60 秒被调用一次，**可以 await**），
发送走已经补好的 ``runtime.onebot.send_group_msg`` / ``send_private_msg``。

降级点（相对 JS 版）：
- 自动挑会话：JS 版读宿主的会话存档算"最近 7 天有互动"；本项目用 ``runtime.recent_messages``
  （若它存在）里的 room 统计。没有这个数据时，必须在 ``chats`` 里显式填目标，否则不发。
- ``serverPort`` 设置项未移植（JS 版是用来自查控制台端口的，本项目没有这个概念）。
- 冷却是按会话独立随机的（与原版一致），所以不会几个群同时被刷。
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

_TZ_CST = timezone(timedelta(hours=8))
DEFAULT_GUIDE = "自然口语、贴合人设、像随手开个话头；不要提「暖场」两个字，不要群发感。"


def _persona_for(runtime, kind, target):
    """按这个会话的绑定取人设，拿不到才回落全局人设。

    暖场消息是发进具体某个群/私聊的，直接取全局人设会把别处的人设带进来。
    异常一律吞掉：选人设失败不该让暖场停摆。
    """
    try:
        from ..persona_bindings import resolve_persona
        persona = resolve_persona(runtime, {"kind": str(kind), "target": str(target), "text": ""})
        return persona.full_prompt()
    except Exception:
        return runtime.style.full_prompt() if hasattr(runtime, "style") else ""
STATE_FILE = "icebreaker.json"
# group_1059300934 / private_1410897629
_CHAT_KEY = re.compile(r'^(group|private)_(\d+)$')


def parse_chats(raw: str) -> list:
    """解析 ``group_数字, private_数字`` 形式的目标列表（容忍中文逗号与空格）。"""
    result = []
    for item in re.split(r'[,，\s]+', str(raw or "")):
        item = item.strip()
        if not item:
            continue
        match = _CHAT_KEY.match(item)
        if match:
            key = f"{match.group(1)}:{match.group(2)}"
            if key not in result:
                result.append(key)
    return result


def parse_hhmm(value: str, fallback: str = "00:00") -> int:
    """把 ``HH:MM`` 解析成"从零点起的分钟数"；非法输入回退。"""
    text = str(value or "").strip() or fallback
    match = re.match(r'^(\d{1,2})\s*[:：]\s*(\d{1,2})$', text)
    if not match:
        match = re.match(r'^(\d{1,2})$', text)
        if not match:
            return parse_hhmm(fallback, "00:00") if text != fallback else 0
        return max(0, min(23, int(match.group(1)))) * 60
    hour = max(0, min(23, int(match.group(1))))
    minute = max(0, min(59, int(match.group(2))))
    return hour * 60 + minute


def in_quiet_hours(minute_of_day: int, start_minute: int, end_minute: int) -> bool:
    """是否处于睡觉时间。跨零点的区间（如 23:30~08:00）也要算对。"""
    if start_minute == end_minute:
        return False
    if start_minute < end_minute:
        return start_minute <= minute_of_day < end_minute
    return minute_of_day >= start_minute or minute_of_day < end_minute


def now_minute(moment: Optional[datetime] = None) -> int:
    current = moment or datetime.now(_TZ_CST)
    return current.hour * 60 + current.minute


def build_prompt(guide: str = "", max_length: int = 150) -> str:
    """暖场消息的生成要求（对齐 JS 版的口径）。"""
    style = str(guide or "").strip() or DEFAULT_GUIDE
    return (
        f"你现在要主动开个话头（不是回复任何人）。要求：\n"
        f"- {style}\n"
        f"- 只输出这一条消息的正文，不要引号、不要解释、不要分行标题。\n"
        f"- 长度不超过 {max(1, int(max_length))} 字。\n"
        f"- 不要提「系统」「提示词」「插件」这类词。"
    )


def clean_reply(text: str, max_length: int = 150) -> str:
    """清掉模型可能多给的引号/代码围栏，并按上限截断。"""
    result = str(text or "").strip()
    if result.startswith("```"):
        result = re.sub(r'^```[a-zA-Z]*\s*', '', result)
        result = re.sub(r'\s*```$', '', result).strip()
    result = result.strip().strip('"').strip("'").strip()
    result = re.sub(r'^(暖场|开场|话头)[:：]\s*', '', result)
    limit = max(1, int(max_length))
    if len(result) > limit:
        result = result[:limit]
    return result


class Icebreaker(PluginBase):
    META = PluginMeta(
        id="icebreaker",
        name="定时暖场",
        description=(
            "在睡觉时间（默认 23:30~08:00）之外，每隔几小时自动往指定会话发一条符合当前人设的"
            "暖场消息；消息由模型现场生成，每个会话各自随机间隔（不会几个群同时被刷）。"
            "留空目标时尝试用最近的活跃会话；拿不到活跃数据就必须显式填目标。"
        ),
        category="utility",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用定时暖场"),
            "chats": FieldSchema(
                type="string", label="发送目标（逗号分隔）",
                description="例如 group_1059300934, private_1410897629。留空 = 尝试自动挑最近活跃的会话。",
            ),
            "maxChatsPerRound": FieldSchema(
                type="int", label="每轮最多发几个会话", min=1, max=20,
                description="防止一次给太多群发消息显得像广播。默认 3。",
            ),
            "intervalMinHours": FieldSchema(
                type="float", label="间隔下限（小时）", min=0.1, max=72,
                description="每次发完，下次时间在「下限~上限」之间随机取。默认 2。",
            ),
            "intervalMaxHours": FieldSchema(
                type="float", label="间隔上限（小时）", min=0.1, max=168,
                description="默认 6。想更频繁就填小一点，例如 1~3。",
            ),
            "quietStart": FieldSchema(type="string", label="睡觉时间开始（HH:MM）"),
            "quietEnd": FieldSchema(type="string", label="睡觉时间结束（HH:MM）"),
            "maxLength": FieldSchema(type="int", label="单条消息最长多少字", min=10, max=1000),
            "temperature": FieldSchema(
                type="float", label="文风随机度", min=0.0, max=1.5,
                description="默认 0.9，让每次暖场不太一样。",
            ),
            "guide": FieldSchema(
                type="string", label="暖场风格指导（可选）", multiline=True,
                description="留空用默认要求。想定制就写一句，例如“多聊学习考试话题”。",
            ),
            "dryRun": FieldSchema(
                type="bool", label="演练模式（只生成不发送）",
                description="打开后消息只写日志与状态，不真正发出去。调试用。",
            ),
            "minIdleMinutes": FieldSchema(
                type="int", label="会话安静多久才暖场", min=0, max=10080,
                description="默认 0（不看）。本项目扩展：避免刚聊完又去刷一句。",
            ),
        },
        default_settings={
            "enabled": False,
            "chats": "",
            "maxChatsPerRound": 3,
            "intervalMinHours": 2.0,
            "intervalMaxHours": 6.0,
            "quietStart": "23:30",
            "quietEnd": "08:00",
            "maxLength": 150,
            "temperature": 0.9,
            "guide": "",
            "dryRun": False,
            "minIdleMinutes": 0,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {chat_key: {"next_at": float, "last_sent": float, "sent": int}}
        self._plan: dict = {}
        self._loaded = False

    # ── 状态 ──────────────────────────────────────────────────────────────
    def _load(self):
        if self._loaded:
            return
        raw = self.load_state(STATE_FILE, {})
        if isinstance(raw, dict) and isinstance(raw.get("plan"), dict):
            self._plan = {key: value for key, value in raw["plan"].items() if isinstance(value, dict)}
        self._loaded = True

    def _save(self):
        try:
            self.save_state(STATE_FILE, {"plan": self._plan, "saved_at": time.time()})
        except Exception:
            pass

    # ── 计划 ──────────────────────────────────────────────────────────────
    def next_interval_seconds(self, picker=None) -> float:
        """按「下限~上限」随机一个间隔（秒）。上下限填反了也能用。"""
        low = max(0.1, float(self.get("intervalMinHours", 2.0) or 2.0))
        high = max(0.1, float(self.get("intervalMaxHours", 6.0) or 6.0))
        if low > high:
            low, high = high, low
        choose = picker or random.uniform
        return float(choose(low, high)) * 3600.0

    def ensure_plan(self, chats: list, now: Optional[float] = None, picker=None) -> dict:
        """给每个会话安排下次发送时间（新会话随机打散，避免同时开火）。"""
        moment = time.time() if now is None else now
        for chat_key in chats:
            entry = self._plan.get(chat_key)
            if not isinstance(entry, dict) or "next_at" not in entry:
                # 新会话：在 0~间隔上限之间随机一个起点，避免所有群同一秒被发
                spread = self.next_interval_seconds(picker) if picker else self.next_interval_seconds()
                offset = random.uniform(0, spread)
                self._plan[chat_key] = {"next_at": moment + offset, "last_sent": 0.0, "sent": 0}
        return self._plan

    def due_chats(self, chats: list, now: Optional[float] = None, cap: Optional[int] = None) -> list:
        """到点该发的会话（按到期时间从早到晚，受每轮上限约束）。"""
        moment = time.time() if now is None else now
        limit = max(1, int(self.get("maxChatsPerRound", 3) or 3)) if cap is None else max(1, int(cap))
        ready = []
        for chat_key in chats:
            entry = self._plan.get(chat_key)
            if not isinstance(entry, dict):
                continue
            if float(entry.get("next_at") or 0) <= moment:
                ready.append((float(entry.get("next_at") or 0), chat_key))
        ready.sort()
        return [chat_key for _, chat_key in ready[:limit]]

    def mark_sent(self, chat_key: str, now: Optional[float] = None, picker=None) -> None:
        moment = time.time() if now is None else now
        entry = self._plan.setdefault(chat_key, {"next_at": 0.0, "last_sent": 0.0, "sent": 0})
        entry["last_sent"] = moment
        entry["sent"] = int(entry.get("sent") or 0) + 1
        entry["next_at"] = moment + self.next_interval_seconds(picker)

    def is_quiet(self, moment: Optional[datetime] = None) -> bool:
        return in_quiet_hours(
            now_minute(moment),
            parse_hhmm(self.get("quietStart", "23:30"), "23:30"),
            parse_hhmm(self.get("quietEnd", "08:00"), "08:00"),
        )

    # ── 目标 ──────────────────────────────────────────────────────────────
    def resolve_chats(self, runtime) -> list:
        """先看显式配置；留空时尝试从最近活跃消息里推。"""
        configured = parse_chats(self.get("chats", ""))
        if configured:
            return configured
        recent = getattr(runtime, "recent_messages", None)
        if hasattr(runtime,'recent_chats'):return runtime.recent_chats()
        if not isinstance(recent, list):
            return []
        seen = []
        for item in recent:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            target = item.get("target")
            if kind in ("group", "private") and target:
                key = f"{kind}:{target}"
                if key not in seen:
                    seen.append(key)
        return seen

    def _last_activity(self, runtime, chat_key: str, now: float) -> Optional[float]:
        if hasattr(runtime,'last_interaction'):
            kind,_,target=chat_key.partition(':')
            return runtime.last_interaction(kind,target)
        recent = getattr(runtime, "recent_messages", None)
        if not isinstance(recent, list):
            return None
        kind, _, target = chat_key.partition(":")
        latest = None
        for item in recent:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind")) == kind and str(item.get("target")) == target:
                ts = item.get("ts") or item.get("time")
                if isinstance(ts, (int, float)):
                    latest = float(ts) if latest is None else max(latest, float(ts))
        return latest

    # ── 主循环 ────────────────────────────────────────────────────────────
    async def tick(self, runtime) -> None:
        if not self.get("enabled"):
            return
        if runtime is None or getattr(runtime, "mode", "") != "onebot":
            # 官方接口没有主动私聊/群发通道，避免用错通道
            return
        if self.is_quiet():
            return
        try:
            self._load()
            chats = self.resolve_chats(runtime)
            if not chats:
                return
            self.ensure_plan(chats)
            due = self.due_chats(chats)
            if not due:
                return
            threshold = max(0, int(self.get("minIdleMinutes", 0) or 0)) * 60
            for chat_key in due:
                if threshold > 0:
                    last = self._last_activity(runtime, chat_key, time.time())
                    if last is not None and time.time() - last < threshold:
                        continue    # 刚聊过，推迟到下一个时间点
                await self._send_one(runtime, chat_key)
            self._save()
        except Exception as exc:
            print(f"[plugins] icebreaker tick error: {exc}")

    def on_proactive_sent(self,kind,target,runtime):
        self.mark_sent(f'{kind}:{target}');self._save()

    async def _send_one(self, runtime, chat_key: str) -> None:
        ai = getattr(runtime, "ai", None)
        if ai is None or not getattr(ai, "configured", False):
            return
        kind, _, target = chat_key.partition(":")
        prior_activity=runtime.last_interaction(kind,target) if hasattr(runtime,'last_interaction') else None
        if hasattr(runtime,'can_send_proactive'):
            if not runtime.can_send_proactive(kind,target) or runtime.has_plugin_outgoing(self.META.id,kind,target):return
        max_length = max(10, int(self.get("maxLength", 150) or 150))
        try:
            async def generate():
                return await ai.chat(
                    [{"role":"user","content":build_prompt(self.get("guide", ""),max_length)}],
                    system_prompt=_persona_for(runtime, kind, target),
                    temperature=float(self.get("temperature",0.9) or 0.9),max_tokens=max(32,max_length*2))
            if hasattr(runtime,'ai_lock'):
                async with runtime.ai_lock:raw=await generate()
            else:raw=await generate()
        except Exception as exc:
            print(f"[plugins] icebreaker 生成失败（{chat_key}）：{exc}")
            return

        text = clean_reply(raw, max_length)
        if not text:
            return
        if hasattr(runtime,'last_interaction') and runtime.last_interaction(kind,target)!=prior_activity:return
        if self.get("dryRun"):
            self.mark_sent(chat_key)
            print(f"[plugins] icebreaker 演练模式（未发送）-> {chat_key}: {text}")
            return
        if hasattr(runtime,'queue_plugin_outgoing'):
            entry=self._plan[chat_key]
            runtime.queue_plugin_outgoing(self.META.id,f'icebreaker:{chat_key}:{entry["next_at"]}',kind,target,text)
            return
        try:
            if kind == "group":
                await runtime.onebot.send_group_msg(target, text)
            else:
                await runtime.onebot.send_private_msg(target, text)
            self.mark_sent(chat_key)
            print(f"[plugins] icebreaker 已暖场 -> {chat_key}")
        except Exception as exc:
            print(f"[plugins] icebreaker 发送失败（{chat_key}）：{exc}")
