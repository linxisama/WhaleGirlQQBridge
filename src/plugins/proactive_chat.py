"""proactive_chat.py - 指定群主动开话题: periodically initiate conversation in groups."""
from __future__ import annotations
import asyncio
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema

_TZ_CST = timezone(timedelta(hours=8))


class ProactiveChat(PluginBase):
    META = PluginMeta(
        id="proactive_chat",
        name="指定群主动开话题",
        description="在指定群聊中，长时间无消息后主动开启话题",
        category="social",
        enabled_by_default=False,
        config_schema={
            "enabled":           FieldSchema(type="bool",   label="启用"),
            "groups":            FieldSchema(type="string", label="目标群(逗号分隔)", multiline=False),
            "idleMinutes":       FieldSchema(type="int",    label="触发空闲分钟数", min=1),
            "activeHours":       FieldSchema(type="string", label="活跃时段(HH:MM-HH:MM)"),
            "maxPerDayPerGroup": FieldSchema(type="int",    label="每群每日最多次数", min=1),
            "minGapMinutes":     FieldSchema(type="int",    label="最小间隔(分钟)", min=1),
            "jitterSec":         FieldSchema(type="int",    label="随机抖动(秒)", min=0),
            "useModel":          FieldSchema(type="bool",   label="使用AI生成话题"),
            "topics":            FieldSchema(type="string", label="备用话题(分号分隔)", multiline=True),
            "maxTokens":         FieldSchema(type="int",    label="最大token", min=16),
            "dailyTokenBudget":  FieldSchema(type="int",    label="每日token预算", min=0),
        },
        default_settings={
            "enabled":           False,
            "groups":            "",
            "idleMinutes":       45,
            "activeHours":       "10:00-23:00",
            "maxPerDayPerGroup": 3,
            "minGapMinutes":     60,
            "jitterSec":         45,
            "useModel":          True,
            "topics":            "你们今天都干啥了;刚刷到个东西笑死我了;有没有人打游戏缺一个;中午吃什么啊纠结;最近在追什么剧",
            "maxTokens":         120,
            "dailyTokenBudget":  30000,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {group_id: {"last_sent": float, "today_count": int, "date": str}}
        raw=self.load_state('schedule.json', {})
        self._gstate: dict = raw if isinstance(raw, dict) else {}
        # {日期: 已用 token 估算}，惰性从状态文件加载
        self._budget: Optional[dict] = None

    def _today(self) -> str:
        return datetime.now(_TZ_CST).strftime("%Y-%m-%d")

    # ── 每日 token 预算 ───────────────────────────────────────────────────
    # 这个设置项以前只写在 schema 里、代码从不读，拨了没有任何效果。现在真的记：
    # {日期: 已用 token 估算}，存在插件状态文件里，跨重启保留。
    def _budget_state(self) -> dict:
        if not hasattr(self, "_budget") or self._budget is None:
            raw = self.load_state("proactive_chat_budget.json", {})
            self._budget = raw if isinstance(raw, dict) else {}
        return self._budget

    def spent_today(self) -> int:
        return int(self._budget_state().get(self._today(), 0) or 0)

    def _budget_left(self) -> bool:
        limit = int(self.get("dailyTokenBudget", 30000) or 0)
        if limit <= 0:
            return True      # 0 = 不限制
        return self.spent_today() < limit

    def record_usage(self, prompt_chars: int, reply: str) -> None:
        """粗估本次调用用掉的 token（中文约 1 字 1 token，英文约 4 字符 1 token）。

        只是预算控制用的估算，不追求精确 —— 目的是"别一天烧穿额度"。
        """
        text = str(reply or "")
        estimate = int(prompt_chars / 2) + int(len(text) / 2) + 8
        state = self._budget_state()
        today = self._today()
        state[today] = int(state.get(today, 0) or 0) + max(0, estimate)
        # 只保留最近 7 天，避免状态文件无限增长
        if len(state) > 7:
            for key in sorted(state)[:-7]:
                state.pop(key, None)
        try:
            self.save_state("proactive_chat_budget.json", state)
        except Exception:
            pass

    def _parse_groups(self, val: str) -> list:
        if not val:
            return []
        return [g.strip() for g in val.replace("，", ",").split(",") if g.strip()]

    def _parse_active_hours(self, val: str):
        """Return (start_min, end_min) or (None, None)."""
        try:
            start_s, end_s = val.split("-")
            sh, sm = start_s.strip().split(":")
            eh, em = end_s.strip().split(":")
            return int(sh) * 60 + int(sm), int(eh) * 60 + int(em)
        except Exception:
            return None, None

    def _in_active_hours(self, val: str) -> bool:
        start_min, end_min = self._parse_active_hours(val)
        if start_min is None:
            return True
        now = datetime.now(_TZ_CST)
        now_min = now.hour * 60 + now.minute
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        return now_min >= start_min or now_min <= end_min

    def _last_group_message_time(self, runtime, group_id: str) -> Optional[float]:
        """Query DB for last message timestamp in this group."""
        try:
            if hasattr(runtime,'last_interaction'):
                return runtime.last_interaction('group',group_id)
            cur = runtime.db.execute(
                "SELECT MAX(time) FROM messages WHERE target=? AND kind='group'",
                (group_id,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return float(row[0])
        except Exception:
            pass
        return None

    async def tick(self, runtime) -> None:
        if not self.get("enabled"):
            return
        if runtime.mode != "onebot":
            return
        groups = self._parse_groups(self.get("groups", ""))
        if not groups:
            return
        active_hours = self.get("activeHours", "10:00-23:00")
        if not self._in_active_hours(active_hours):
            return
        if not self._budget_left():
            runtime.note("[proactive_chat] 今日 token 预算已用完，今天不再主动发言")
            return
        idle_secs = self.get("idleMinutes", 45) * 60
        now = time.time()
        for group_id in groups:
            try:
                if not runtime.can_send_proactive('group',group_id) or runtime.has_plugin_outgoing(self.META.id,'group',group_id):continue
                gs = self._gstate.setdefault(group_id, {"last_sent": 0, "today_count": 0, "date": ""})
                today = self._today()
                if gs["date"] != today:
                    gs["date"] = today
                    gs["today_count"] = 0
                if gs["today_count"] >= self.get("maxPerDayPerGroup", 3):
                    continue
                min_gap = self.get("minGapMinutes", 60) * 60
                if now - gs["last_sent"] < min_gap:
                    continue
                last_msg_t = self._last_group_message_time(runtime, group_id)
                if last_msg_t is None or now - last_msg_t < idle_secs:
                    continue
                # Jitter
                jitter = random.randint(0, self.get("jitterSec", 45))
                await asyncio.sleep(jitter)
                if self._last_group_message_time(runtime,group_id)!=last_msg_t:continue
                topic = await self._get_topic(runtime)
                if topic:
                    if self._last_group_message_time(runtime,group_id)!=last_msg_t:continue
                    key=f'proactive:{group_id}:{last_msg_t}:{gs["last_sent"]}'
                    if runtime.queue_plugin_outgoing(self.META.id,key,'group',group_id,topic):
                        runtime.note('主动开话题已加入发送队列')
            except Exception as e:
                runtime.note(f"[proactive_chat] error for {group_id}: {e}")

    def on_proactive_sent(self,kind,target,runtime):
        gs=self._gstate.setdefault(str(target),{'last_sent':0,'today_count':0,'date':''})
        today=self._today()
        if gs.get('date')!=today:gs.update(date=today,today_count=0)
        gs['last_sent']=time.time();gs['today_count']+=1
        self.save_state('schedule.json',self._gstate)

    async def _get_topic(self, runtime) -> str:
        fallbacks = [t.strip() for t in self.get("topics", "").split(";") if t.strip()]
        if not self.get("useModel", True):
            return random.choice(fallbacks) if fallbacks else ""
        prompt = "你是一个活跃的群友，生成一句自然的群聊开场白，不超过20字，不要带解释。"
        try:
            async with runtime.ai_lock:
                result = await runtime.ai.chat(
                [
                    {"role": "system",  "content": prompt},
                    {"role": "user",    "content": "随机生成一句开场白"},
                ],
                system_prompt=runtime.style.full_prompt(),max_tokens=self.get("maxTokens", 120),
            )
            text = (result or "").strip()
            # 记一次用量：dailyTokenBudget 以前是空设置，现在真的算
            self.record_usage(len(prompt), text)
            return text if text else random.choice(fallbacks)
        except Exception:
            return random.choice(fallbacks) if fallbacks else ""
