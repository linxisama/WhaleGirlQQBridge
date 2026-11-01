"""addressee_guard.py - 点名判定: hint when bot's name is mentioned without @."""
from __future__ import annotations
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class AddresseeGuard(PluginBase):
    META = PluginMeta(
        id="addressee_guard",
        name="点名判定",
        description="有人提到机器人名字但没有@时，提示AI考虑是否回应",
        category="intelligence",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
        },
        default_settings={
            "enabled": True,
        },
    )

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        if message.get("at"):
            return None
        if message.get("kind") != "group":
            return None
        try:
            text = message.get("text", "")
            # Fetch bot names from config if available
            bot_names = []
            try:
                cfg = runtime.config
                name = getattr(cfg, "bot_name", None) or getattr(cfg, "name", None)
                if isinstance(name, str) and name:
                    bot_names.append(name)
                # Also try common aliases
                aliases = getattr(cfg, "bot_aliases", None) or getattr(cfg, "aliases", None)
                if isinstance(aliases, (list, tuple)):
                    bot_names.extend(aliases)
            except Exception:
                pass
            if not bot_names:
                bot_names = ["鲸鱼娘", "机器人"]
            if any(n in text for n in bot_names):
                return {
                    "extra_system": "有人提到了你的名字，你可以考虑是否回应。"
                }
        except Exception:
            pass
        return None
