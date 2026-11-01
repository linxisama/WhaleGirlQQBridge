"""silence_ping.py - 主动私信: proactively DM inactive targets."""
from __future__ import annotations
import asyncio
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema

_TZ_CST = timezone(timedelta(hours=8))


class SilencePing(PluginBase):
    META = PluginMeta(
        id="silence_ping",
        name="主动私信",
        description="当指定用户长时间不说话时，主动发私信关心",
        category="social",
        enabled_by_default=False,
        config_schema={
            "enabled":                FieldSchema(type="bool",   label="启用"),
            "targets":                FieldSchema(type="string", label="目标QQ号(逗号分隔)", multiline=False),
            "targetIdleMinMinutes":   FieldSchema(type="int",    label="触发空闲分钟数", min=1),
            "cooldownMinutes":        FieldSchema(type="int",    label="冷却时间(分钟)", min=1),
            "wakeRange":              FieldSchema(type="string", label="生效时段偏移范围(分钟, 格式15-30)"),
            "quietRange":             FieldSchema(type="string", label="静默时段(HH:MM-HH:MM)"),
            "maxPerDay":              FieldSchema(type="int",    label="每日最多发送次数", min=1),
            "noRepeatWhenUnreplied":  FieldSchema(type="bool",   label="未回复时不重发"),
            "dryRun":                 FieldSchema(type="bool",   label="试运行(不实际发送)"),
        },
        default_settings={
            "enabled":               False,
            "targets":               "",
            "targetIdleMinMinutes":  30,
            "cooldownMinutes":       120,
            "wakeRange":             "15-30",
            "quietRange":            "00:00-07:00",
            "maxPerDay":             20,
            "noRepeatWhenUnreplied": True,
            "dryRun":                False,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {qq_id: {"last_sent": float, "today_count": int, "date": str, "last_replied": float}}
        # 状态文件必须校验类型：以前文件里若是 JSON 数组，下面第一次 setdefault 就抛
        # AttributeError，被 manager 的 print 吞掉 → 插件永久静默失效（用户只看到"没反应"）。
        raw = self.load_state('schedule.json', {})
        self._tstate: dict = raw if isinstance(raw, dict) else {}

    def _today(self) -> str:
        return datetime.now(_TZ_CST).strftime("%Y-%m-%d")

    def _parse_targets(self) -> list:
        val = self.get("targets", "")
        return [t.strip() for t in val.replace("，", ",").split(",") if t.strip()]

    def _in_quiet_range(self) -> bool:
        val = self.get("quietRange", "00:00-07:00")
        try:
            start_s, end_s = val.split("-")
            sh, sm = start_s.strip().split(":")
            eh, em = end_s.strip().split(":")
            start_min = int(sh) * 60 + int(sm)
            end_min   = int(eh) * 60 + int(em)
            now = datetime.now(_TZ_CST)
            now_min = now.hour * 60 + now.minute
            if start_min <= end_min:
                return start_min <= now_min < end_min
            return now_min >= start_min or now_min < end_min
        except Exception:
            return False

    def on_proactive_sent(self,kind,target,runtime):
        ts=self._tstate.setdefault(str(target),{});today=self._today()
        if ts.get('date')!=today:ts.update(date=today,today_count=0)
        ts['last_sent']=time.time();ts['today_count']=ts.get('today_count',0)+1
        self.save_state('schedule.json',self._tstate)

    def _last_dm_time(self, runtime, qq_id: str) -> Optional[float]:
        """Query DB for last message from this user in private chat."""
        try:
            if hasattr(runtime,'last_interaction'):return runtime.last_interaction('private',qq_id)
            cur = runtime.db.execute(
                "SELECT MAX(time) FROM messages WHERE sender_id=? AND kind='private'",
                (qq_id,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return float(row[0])
        except Exception:
            pass
        return None

    def _last_bot_dm_time(self, runtime, qq_id: str) -> Optional[float]:
        """Query DB for last bot message to this user."""
        if hasattr(runtime,'last_outbound'):return runtime.last_outbound('private',qq_id)
        try:
            cur = runtime.db.execute(
                "SELECT MAX(time) FROM bot_messages WHERE target=? AND kind='private'",
                (qq_id,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return float(row[0])
        except Exception:
            pass
        return None

    async def tick(self, runtime) -> None:
        if not self.get("enabled") or getattr(runtime,"mode","")!="onebot":
            return
        if self._in_quiet_range():
            return
        targets = self._parse_targets()
        if not targets:
            return
        now = time.time()
        idle_sec    = self.get("targetIdleMinMinutes", 30) * 60
        cooldown_s  = self.get("cooldownMinutes", 120) * 60
        max_per_day = self.get("maxPerDay", 20)
        today       = self._today()
        for qq_id in targets:
            try:
                ts = self._tstate.setdefault(qq_id, {
                    "last_sent": 0, "today_count": 0, "date": "", "last_replied": 0
                })
                if ts["date"] != today:
                    ts["date"] = today
                    ts["today_count"] = 0
                if ts["today_count"] >= max_per_day:
                    continue
                if now - ts["last_sent"] < cooldown_s:
                    continue
                last_msg = self._last_dm_time(runtime, qq_id)
                if last_msg is None or now - last_msg < idle_sec:
                    continue
                # noRepeatWhenUnreplied: if bot sent last and user hasn't replied since
                if self.get("noRepeatWhenUnreplied", True):
                    last_bot = self._last_bot_dm_time(runtime, qq_id)
                    if last_bot and (last_msg is None or last_msg < last_bot):
                        continue
                # Schedule the jitter instead of sleeping in the shared plugin ticker.
                if not ts.get('due'):
                    try:lo,hi=[int(x) for x in self.get('wakeRange','15-30').split('-')]
                    except Exception:lo,hi=15,30
                    ts['due']=now+random.randint(max(0,min(lo,hi,120)),max(0,min(max(lo,hi),120)))*60
                    ts['inbound']=last_msg
                    self.save_state('schedule.json',self._tstate)
                    continue
                if ts.get('inbound')!=last_msg:
                    ts.pop('due',None);self.save_state('schedule.json',self._tstate);continue
                if now<ts['due']:continue
                if not runtime.can_send_proactive('private',qq_id):continue
                if hasattr(runtime,'queue_plugin_outgoing') and runtime.has_plugin_outgoing('silence_ping','private',qq_id):continue
                msg = await self._gen_message(runtime, qq_id)
                if not msg:
                    continue
                if self.get('dryRun',False):
                    ts.pop('due',None);ts['last_sent']=now
                    runtime.note('[主动私信] 试运行已生成，未发送');self.save_state('schedule.json',self._tstate);continue
                key=f'silence:{qq_id}:{last_msg}'
                if runtime.queue_plugin_outgoing('silence_ping',key,'private',qq_id,msg):
                    ts.pop('due',None);self.save_state('schedule.json',self._tstate)
            except Exception as e:
                runtime.note(f"[silence_ping] error for {qq_id}: {e}")

    async def _gen_message(self, runtime, qq_id: str) -> str:
        try:
            async with runtime.ai_lock:
                result = await runtime.ai.chat(
                [
                    {"role": "system", "content": "你在给一个朋友发私信，他好久没说话了。发一句简短自然的关心话，不超过15字，不要带解释。"},
                    {"role": "user",   "content": "生成一句关心的私信"},
                ],
                system_prompt=runtime.style.full_prompt(),max_tokens=64,
            )
            return (result or "").strip()
        except Exception:
            return ""
