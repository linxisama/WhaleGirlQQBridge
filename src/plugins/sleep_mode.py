"""sleep_mode.py - 睡眠时段: block responses during configured sleep hours."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema

_TZ_CST = timezone(timedelta(hours=8))


class SleepMode(PluginBase):
    META = PluginMeta(
        id="sleep_mode",
        name="睡眠时段",
        description="在指定时间段内不回复消息",
        category="rhythm",
        enabled_by_default=False,
        config_schema={
            "enabled":   FieldSchema(type="bool",   label="启用"),
            "startTime": FieldSchema(type="string", label="开始时间", description="格式 HH:MM"),
            "endTime":   FieldSchema(type="string", label="结束时间", description="格式 HH:MM"),
            "allowAt":   FieldSchema(type="bool",   label="@时仍回复"),
        },
        default_settings={
            "enabled":   False,
            "startTime": "00:00",
            "endTime":   "07:00",
            "allowAt":   True,
        },
    )

    @staticmethod
    def _parse_hm(s: str):
        """Return (hour, minute) from 'HH:MM' string."""
        try:
            h, m = s.split(":")
            return int(h), int(m)
        except Exception:
            return 0, 0

    @staticmethod
    def _in_window(now_minutes: int, start_minutes: int, end_minutes: int) -> bool:
        """Check if now is in [start, end) window, supporting cross-midnight."""
        if start_minutes <= end_minutes:
            return start_minutes <= now_minutes < end_minutes
        # cross-midnight
        return now_minutes >= start_minutes or now_minutes < end_minutes

    def on_block(self, message: dict, runtime) -> bool:
        if not self.get("enabled"):
            return False
        try:
            now = datetime.now(_TZ_CST)
            now_min = now.hour * 60 + now.minute
            sh, sm = self._parse_hm(self.get("startTime", "00:00"))
            eh, em = self._parse_hm(self.get("endTime",   "07:00"))
            start_min = sh * 60 + sm
            end_min   = eh * 60 + em
            if not self._in_window(now_min, start_min, end_min):
                return False
            # In sleep window — allow @-mentions if allowAt
            if self.get("allowAt", True) and message.get("at"):
                return False
            return True
        except Exception:
            return False
