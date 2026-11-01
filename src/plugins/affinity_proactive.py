"""affinity_proactive.py - 好感度主动开话题（按等级排期 + 概率掷骰）。

移植自 JS 插件 ``affinity-proactive``（QQ Agent，MIT）。核心是**机械调度**：
到点该不该主动说话由规则决定，不经过模型判断；只有"说什么"才叫模型。

⚠️ 与 JS 版的差异（降级点，已写进插件说明）
- 本项目没有好感度技能存档（JS 读 ``data/affinity.json``），分数改由设置项
  ``affinityScores`` 显式给出（形如 ``group:123=95,private:456=140``）；
- JS 优先走技能的 ``affinity.send`` 能力（核心发送队列，带限频/去重/留档），
  本项目直接走 ``runtime.onebot.send_*``，因此**绕过了队列的限频与留档**；
- JS 会在"有未读消息"时让路，本项目拿不到未读计数，只能按 ``lastSentAt`` 与
  ``idleMinMs`` 判断（见 ``should_send`` 的 ``has_unread`` 参数）。

审核发现（记录在案）：本文件在核心审阅时修正过 5 处 ——
① 原稿自造了一个 ``PluginBase``，导致 PluginManager 用 ``issubclass`` 扫不到插件
（装上也不生效且不报错）；改为从 ``src.plugins`` 导入真基类与 ``PluginMeta``/``FieldSchema``。
② ``ATTITUDE_BRIEF`` 被改写成了自编文案，已逐字换回 JS 原文。
③ ``should_send`` 把 ``PROACTIVE_TABLE[level].min`` 当成"闲置多久"用（那是**排期间隔**），
改为使用设置的 ``idleMinMs``，并按设置传入静默时段。
④ ``should_send`` 的静默时段被硬编码成 0~8，改为参数传入。
⑤ 状态落盘改用基类的 ``load_state``/``save_state``，并保留 version 字段。
"""

from __future__ import annotations
from datetime import datetime,timezone,timedelta
_TZ_CST=timezone(timedelta(hours=8))

import random
import re
import time
from typing import Callable, Dict, Optional

from . import PluginBase, PluginMeta, FieldSchema

# 等级阈值（与 JS 版 skills/affinity 的 LEVELS 一致）
LEVEL_MINS = [0, 20, 50, 90, 140, 200]
LEVEL_NAMES = ['陌生', '面熟', '熟络', '亲近', '交心', '挚友']

# 主动参数表（每个等级：检查间隔下限毫秒、上限毫秒、命中概率）
PROACTIVE_TABLE = [
    (3600000, 7200000, 0.02),
    (2700000, 5400000, 0.05),
    (1800000, 5400000, 0.10),
    (1800000, 3600000, 0.25),
    (3600000, 7200000, 0.15),
    (5400000, 10800000, 0.08),
]

# 主动生成时给模型的等级语气简表（JS 原文，逐字保留）
ATTITUDE_BRIEF = [
    '礼貌、简短、有距离但不冷；几乎不主动；不接玩笑；私人问题模糊带过',
    '稍微放松、多给半句；极少主动（出现时可能先说「来了」）；能接一句玩笑但不追',
    '自然友好、接得住话；偶尔主动但话说一半；顺手逗一下；允许绕着说',
    '明显放松、有温度但不黏；会主动分享日常；偶尔泄露一点真实情绪',
    '亲近自然、偶尔柔软；会记挂对方；做具体的小事而不是只说',
    '真实坦率、不端着；可以只是一句「嗯」，也可以忽然说一句重的；稀有但真诚',
]

_TMP_PREFIX = re.compile(r'^(主动|暖场|开场|话头)[:：]\s*')


def level_of(score) -> int:
    """按 LEVEL_MINS 取等级；非数字或低于首个阈值取 0。"""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return 0
    level = 0
    for index, threshold in enumerate(LEVEL_MINS):
        if float(score) >= threshold:
            level = index
        else:
            break
    return level


def in_quiet_hours(hour: int, start_hour: int, end_hour: int) -> bool:
    """是否处于静默时段；跨零点（start > end）也成立；start == end 表示不静默。"""
    start = max(0, min(23, int(start_hour)))
    end = max(0, min(23, int(end_hour)))
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _split_list(raw: str) -> list:
    return [item.strip() for item in re.split(r'[,，\s]+', str(raw or "")) if item.strip()]


def in_whitelist(chat_key: str, cfg: dict) -> bool:
    """白名单口径（与 JS 设置页一致）：deny 优先；allow 非空必须命中；
    两者都空时必须显式开 ``allowAllWhenEmpty``。

    allow/deny 允许写 ``group:123`` 或只写 ``123``（只写数字时按 kind 匹配）。
    """
    match = re.match(r'^(group|private):(\d+)$', str(chat_key or ""))
    if not match:
        return False
    kind, chat_id = match.group(1), match.group(2)

    def listed(raw: str) -> bool:
        for item in _split_list(raw):
            if ":" in item:
                item_kind, _, item_id = item.partition(":")
                if item_kind == kind and item_id == chat_id:
                    return True
            elif item == chat_id:
                return True
        return False

    allow = str(cfg.get("allowList") or "")
    deny = str(cfg.get("denyList") or "")
    if listed(deny):
        return False
    if allow.strip():
        return listed(allow)
    # allow 为空、deny 非空：这是"只拉黑"口径 —— 没被 deny 就放行。
    # 两者都空时必须显式开 allowAllWhenEmpty，避免"没配就到处发"。
    if deny.strip():
        return True
    return bool(cfg.get("allowAllWhenEmpty", False))


def next_check_at(level: int, now: float, picker: Optional[Callable[[float, float], float]] = None) -> float:
    """按该等级的检查间隔（毫秒）随机一个下次检查时间。"""
    safe_level = max(0, min(len(PROACTIVE_TABLE) - 1, int(level)))
    min_ms, max_ms, _ = PROACTIVE_TABLE[safe_level]
    low, high = min_ms / 1000.0, max_ms / 1000.0
    choose = picker or random.uniform
    return float(now) + float(choose(low, high))


def should_send(level: int, last_interaction_ts: Optional[float], has_unread: bool,
                now: float, idle_min_ms: float = 600000.0,
                quiet_start_hour: int = 0, quiet_end_hour: int = 8,
                random_value: Optional[float] = None) -> dict:
    """按 JS 版流程判定本轮要不要主动开口。

    注意：判"刚互动过"用的是 ``idle_min_ms``（设置项，默认 10 分钟），
    **不是** ``PROACTIVE_TABLE`` 里的检查间隔 —— 后者是"隔多久看一次"。
    """
    safe_level = max(0, min(len(PROACTIVE_TABLE) - 1, int(level)))
    probability = PROACTIVE_TABLE[safe_level][2]

    hour = time.localtime(float(now)).tm_hour
    if in_quiet_hours(hour, quiet_start_hour, quiet_end_hour):
        return {"send": False, "reason": "静默时段"}

    if last_interaction_ts is not None:
        if float(now) - float(last_interaction_ts) < max(0.0, float(idle_min_ms)) / 1000.0:
            return {"send": False, "reason": "刚互动过"}

    if has_unread:
        return {"send": False, "reason": "有未读消息"}

    roll = random.random() if random_value is None else float(random_value)
    if roll >= probability:
        return {"send": False, "reason": "未命中概率"}
    return {"send": True, "reason": "命中概率"}


def build_proactive_prompt(level: int, persona_brief: str = "", guide: str = "",
                           max_length: int = 160) -> str:
    """主动发言的生成要求；必须带上该等级的语气简表。"""
    safe_level = max(0, min(len(ATTITUDE_BRIEF) - 1, int(level)))
    brief = ATTITUDE_BRIEF[safe_level]
    parts = [
        f"你现在要主动开个话头（不是回复任何人）。当前关系等级：{LEVEL_NAMES[safe_level]}。",
        f"语气要求：{brief}",
    ]
    if str(persona_brief or "").strip():
        parts.append(f"你的人设：{str(persona_brief).strip()}")
    if str(guide or "").strip():
        parts.append(f"额外要求：{str(guide).strip()}")
    parts.append(f"只输出这一条消息的正文，不要解释、不要引号、不要代码围栏；不超过 {max(1, int(max_length))} 字。")
    return "\n".join(parts)


def clean_reply(text: str, max_length: int = 160) -> str:
    """去掉代码围栏、引号与「主动：」之类前缀，并按上限截断。"""
    result = str(text or "").strip()
    if result.startswith("```"):
        lines = result.split("\n")
        if len(lines) > 2:
            lines = lines[1:-1]
        result = "\n".join(lines).strip()
    result = result.strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in "\"'“”":
        result = result[1:-1].strip()
    result = _TMP_PREFIX.sub("", result).strip()
    limit = max(1, int(max_length))
    return result[:limit] if len(result) > limit else result


def escape_cq_text(text: str) -> str:
    """转义 CQ 码特殊字符。顺序很重要：先 & 再其它，否则会把刚转义的 & 再转一遍。"""
    result = str(text or "")
    result = result.replace("&", "&amp;")
    result = result.replace("[", "&#91;")
    result = result.replace("]", "&#93;")
    result = result.replace(",", "&#44;")
    return result


def parse_scores(raw: str) -> Dict[str, float]:
    """解析 ``group:123=95,private:456=140`` 形式的好感度分数。"""
    scores: Dict[str, float] = {}
    for item in _split_list(raw):
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        if not re.match(r'^(group|private):\d+$', key):
            continue
        try:
            scores[key] = float(value.strip())
        except ValueError:
            continue
    return scores


class AffinityProactive(PluginBase):
    META = PluginMeta(
        id="affinity_proactive",
        name="好感度主动开话题",
        description=(
            "按好感度等级排期，到点时按等级概率主动开一句（越高越自然、越不靠频率）。"
            "（降级说明：本项目没有好感度技能存档，分数请在「好感度分数」里显式填写；"
            "发送走宿主队列并留档；未回复时不继续追问；拿不到未读计数，"
            "只能按上次发言时间判断。）"
        ),
        category="utility",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "affinityScores": FieldSchema(
                type="string", label="好感度分数",
                description="形如 group:123=95,private:456=140；键必须写成 group:数字 或 private:数字。",
            ),
            "scanIntervalSec": FieldSchema(
                type="int", label="扫描间隔（秒）", min=10, max=3600,
                description="默认 60。tick 每约 60 秒跑一次，这里只做下限保护。",
            ),
            "quietStartHour": FieldSchema(type="int", label="静默开始（小时）", min=0, max=23),
            "quietEndHour": FieldSchema(type="int", label="静默结束（小时）", min=0, max=23),
            "idleMinMs": FieldSchema(
                type="int", label="距上次互动多久才主动（毫秒）", min=0, max=86400000,
                description="默认 600000（10 分钟）。",
            ),
            "maxTokens": FieldSchema(type="int", label="生成上限（token）", min=16, max=2048),
            "allowList": FieldSchema(
                type="string", label="只对这些会话主动",
                description="逗号分隔；可写 group:123 或只写 123。留空且 deny 也空时，需要开「空名单时放行」。",
            ),
            "denyList": FieldSchema(type="string", label="不对这些会话主动",
                                    description="优先级高于 allow。"),
            "allowAllWhenEmpty": FieldSchema(type="bool", label="空名单时放行"),
            "guide": FieldSchema(type="string", label="额外风格要求", multiline=True),
        },
        default_settings={
            "enabled": False,
            "affinityScores": "",
            "scanIntervalSec": 60,
            "quietStartHour": 0,
            "quietEndHour": 8,
            "idleMinMs": 600000,
            "maxTokens": 160,
            "allowList": "",
            "denyList": "",
            "allowAllWhenEmpty": False,
            "guide": "",
        },
    )

    STATE_FILE = "affinity_proactive.json"

    def __init__(self, manager):
        super().__init__(manager)
        self._state = None
        self._last_scan = 0.0

    # ── 状态 ──────────────────────────────────────────────────────────────
    def state(self) -> dict:
        if self._state is None:
            raw = self.load_state(self.STATE_FILE, {})
            chats = raw.get("chats") if isinstance(raw, dict) else None
            self._state = {"version": 1, "chats": chats if isinstance(chats, dict) else {}}
        return self._state

    def persist(self) -> None:
        try:
            self.save_state(self.STATE_FILE, {"version": 1, "chats": self.state()["chats"]})
        except Exception:
            pass

    def reschedule(self, chat_key: str, level: int, now: float,
                   picker: Optional[Callable[[float, float], float]] = None,
                   sent: bool = False) -> None:
        chat = self.state()["chats"].setdefault(chat_key, {})
        chat["nextCheckAt"] = next_check_at(level, now, picker)
        if sent:
            chat["lastSentAt"] = now

    # ── 目标 ──────────────────────────────────────────────────────────────
    def targets(self) -> Dict[str, float]:
        """要主动的会话与其分数。

        顺序：**「群友好感度」插件的实时分数**（装了并开着就用它，不用手填）→
        手填的 ``affinityScores`` → 回退 ``allowList``（分数按 0 算）。
        """
        live = getattr(self._manager, "_plugins", {}).get("affinity")
        if live is not None and live.enabled:
            try:
                scores = live.chat_scores()
            except Exception:
                scores = {}
            if scores:
                return {key: float(value) for key, value in scores.items()}
        configured = parse_scores(self.get("affinityScores", ""))
        if configured:
            return configured
        fallback = {}
        for item in _split_list(self.get("allowList", "")):
            key = item if ":" in item else f"group:{item}"
            if re.match(r'^(group|private):\d+$', key):
                fallback[key] = 0.0
        return fallback

    def whitelisted(self, chat_key: str) -> bool:
        return in_whitelist(chat_key, {
            "allowList": self.get("allowList", ""),
            "denyList": self.get("denyList", ""),
            "allowAllWhenEmpty": self.get("allowAllWhenEmpty", False),
        })

    # ── 主循环 ────────────────────────────────────────────────────────────
    async def tick(self, runtime) -> None:
        if not self.get("enabled"):
            return
        if runtime is None or getattr(runtime, "mode", "") != "onebot":
            # 主动发送只走 OneBot；官方接口没有主动群发/私聊通道
            return
        try:
            now = time.time()
            # "扫描间隔"以前是空设置（写了不读）。tick 本身约 60 秒一次，
            # 这里按设置做限流，避免把间隔调大之后仍然每分钟扫一遍。
            interval = max(10, int(self.get("scanIntervalSec", 60) or 60))
            if now - float(getattr(self, "_last_scan", 0.0) or 0.0) < interval:
                return
            self._last_scan = now
            for chat_key, score in self.targets().items():
                try:
                    await self._tick_one(runtime, chat_key, float(score), now)
                except Exception as exc:
                    # 单个会话出错不影响其余会话
                    print(f"[plugins] affinity_proactive {chat_key} 出错：{exc}")
        except Exception as exc:
            print(f"[plugins] affinity_proactive tick error: {exc}")

    async def _tick_one(self, runtime, chat_key: str, score: float, now: float) -> None:
        if not self.whitelisted(chat_key):
            return
        level = level_of(score)
        chat = self.state()["chats"].get(chat_key) or {}
        if now < float(chat.get("nextCheckAt") or 0):
            return

        kind,_,target=chat_key.partition(':')
        last=getattr(runtime,'last_interaction',lambda *_:None)(kind,target)
        outgoing=getattr(runtime,'last_outbound',lambda *_:None)(kind,target)
        if outgoing and (not last or last<outgoing):return
        if hasattr(runtime,'has_plugin_outgoing') and runtime.has_plugin_outgoing('affinity_proactive',kind,target):return
        decision = should_send(
            level=level,
            last_interaction_ts=max(last or 0,chat.get("lastSentAt") or 0) or None,
            has_unread=False,      # 本项目拿不到未读计数（降级点）
            now=now,
            idle_min_ms=self.get("idleMinMs", 600000),
            quiet_start_hour=self.get("quietStartHour", 0),
            quiet_end_hour=self.get("quietEndHour", 8),
        )
        if not decision["send"]:
            # 静默时段只重新排期；其它原因同样排到下一个检查点
            self.reschedule(chat_key, level, now)
            self.persist()
            return

        ai = getattr(runtime, "ai", None)
        if ai is None or not getattr(ai, "configured", False):
            return
        max_tokens = max(16, int(self.get("maxTokens", 160) or 160))
        prompt = build_proactive_prompt(level, guide=self.get("guide", ""), max_length=max_tokens)
        import asyncio
        async with getattr(runtime,'ai_lock',asyncio.Lock()):
            persona=getattr(getattr(runtime,'style',None),'get',lambda:None)()
            look=getattr(getattr(runtime,'style',None),'appearance_text',lambda _s:'')
            system=persona.prompt_text() if persona else None
            if system and persona is not None:
                note=look(persona)
                if note:system=f"{system}\n\n{note}"
            raw = await ai.chat([{"role": "user", "content": prompt}],system_prompt=system, temperature=0.9, max_tokens=max_tokens)
        text = clean_reply(raw, max_tokens)
        if not text:
            return

        kind, _, target = chat_key.partition(":")
        if hasattr(runtime,'queue_plugin_outgoing'):
            key=f'affinity:{chat_key}:{int(now//60)}'
            if runtime.queue_plugin_outgoing('affinity_proactive',key,kind,target,text):
                self.reschedule(chat_key,level,now);self.persist()
            return
        if kind == "group":
            await runtime.onebot.send_group_msg(int(target), escape_cq_text(text))
        else:
            await runtime.onebot.send_private_msg(int(target), escape_cq_text(text))
        # 只有确实发出后才记 lastSentAt 并重排
        self.reschedule(chat_key, level, now, sent=True)
        self.persist()

    def on_proactive_sent(self,kind,target,runtime):
        chat_key=f'{kind}:{target}';score=self.targets().get(chat_key,0)
        self.reschedule(chat_key,level_of(score),time.time(),sent=True);self.persist()

    def _in_quiet_range(self):
        return in_quiet_hours(datetime.now(_TZ_CST).hour,self.get('quietStartHour',0),self.get('quietEndHour',8))
