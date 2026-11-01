"""pack-sleep-quiet — 睡眠时段静默（从 src/plugins/sleep_mode.py 独立出来的目录 mod）。

时区固定 UTC+8（与内置版一致）：主机在别的时区也不改口径，避免"睡醒时间漂移"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import FieldSchema, PluginBase, PluginMeta

_TZ_CST = timezone(timedelta(hours=8))


class SleepQuiet(PluginBase):
    META = PluginMeta(
        id="pack_sleep_quiet",
        name="睡眠时段静默（独立包）",
        description="在设定的时间段内不回复，被 @ 时可选放行。",
        category="rhythm",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "start_time": FieldSchema(type="string", label="开始时间",
                                      description="格式 HH:MM"),
            "end_time": FieldSchema(type="string", label="结束时间",
                                    description="格式 HH:MM"),
            "allow_at": FieldSchema(type="bool", label="@时仍回复"),
        },
        default_settings={"enabled": False, "start_time": "00:00",
                          "end_time": "07:00", "allow_at": True},
    )

    @staticmethod
    def _parse_hm(text):
        """'HH:MM' -> 分钟数；解析不了按 0 点处理（不抛异常）。"""
        try:
            hour, minute = str(text).split(":")
            return int(hour) * 60 + int(minute)
        except Exception:
            return 0

    @staticmethod
    def _in_window(now_minutes, start_minutes, end_minutes) -> bool:
        """[start, end) 窗口；start > end 表示跨零点。"""
        if start_minutes == end_minutes:
            return False
        if start_minutes < end_minutes:
            return start_minutes <= now_minutes < end_minutes
        return now_minutes >= start_minutes or now_minutes < end_minutes

    def on_block(self, message: dict, runtime) -> bool:
        if not self.get("enabled"):
            return False
        try:
            if not isinstance(message, dict):
                return False
            now = datetime.now(_TZ_CST)
            now_minutes = now.hour * 60 + now.minute
            start = self._parse_hm(self.get("start_time", "00:00"))
            end = self._parse_hm(self.get("end_time", "07:00"))
            if not self._in_window(now_minutes, start, end):
                return False
            if self.get("allow_at", True) and message.get("at"):
                return False
            return True
        except Exception:
            return False
