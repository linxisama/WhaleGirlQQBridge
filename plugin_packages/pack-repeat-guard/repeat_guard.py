"""pack-repeat-guard — 复读闸门（从 src/plugins/anti_repeat.py 独立出来的目录 mod）。

与内置版的差别只在边界：这里只依赖 ModRuntime 暴露的设置读取（`self.get`），
不访问 bot / plugins / db，因此可以单独装到别人机器上而不用改主体代码。
"""

from __future__ import annotations

import hashlib
import json
import time

from . import FieldSchema, PluginBase, PluginMeta


class RepeatGuard(PluginBase):
    META = PluginMeta(
        id="pack_repeat_guard",
        name="复读闸门（独立包）",
        description="同一群里同一句话超过阈值就静默丢弃，拦在入队前。",
        category="rhythm",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "max_repeat": FieldSchema(type="int", label="最多重复次数", min=1, max=20),
            "window_sec": FieldSchema(type="int", label="统计窗口(秒)", min=1, max=600),
        },
        default_settings={"enabled": False, "max_repeat": 3, "window_sec": 60},
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {(房间, 文本[, 图片指纹]): [时间戳, ...]}
        self._seen: dict = {}

    def on_block(self, message: dict, runtime) -> bool:
        if not self.get("enabled"):
            return False
        try:
            if not isinstance(message, dict) or message.get("kind") != "group":
                return False
            text = str(message.get("text") or "").strip()
            if not text:
                return False
            key = [str(message.get("target") or ""), text]
            media = message.get("media") or []
            if any(isinstance(item, dict) and item.get("type") == "image"
                   for item in media):
                blob = json.dumps(media, ensure_ascii=False, sort_keys=True, default=str)
                key.append(hashlib.sha256(blob.encode("utf-8")).hexdigest())
            window = self.get("window_sec", 60)
            limit = self.get("max_repeat", 3)
            now = time.time()
            stamps = self._seen.setdefault(tuple(key), [])
            stamps.append(now)
            stamps[:] = [stamp for stamp in stamps if now - stamp <= window]
            return len(stamps) > limit
        except Exception:
            return False
