"""目录 mod 样例：关键词自动回复。

挂载点就是目录本身 —— 把整个目录拷到 <data_dir>/custom_plugins/mod-keyword-reply/，
管理器扫到 plugin.json 就自动挂载，不需要 register_custom（见 manager._scan_mod_dirs）。
plugin.json 的 id 必须与目录名逐字相同（manager.py:463 的 id_mismatch 规则）。

行为对齐内置插件 src/plugins/keyword_reply.py：同样的触发条件、同样的回复文案，
只是配置从钩子拿到的 ModRuntime.settings 读，而不是插件自己的 self.get()。
"""
from __future__ import annotations
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema
from ..keyword_rules import PREFIX, read_rule_rows, validate_rule_rows


class KeywordReplyMod(PluginBase):
    META = PluginMeta(
        id="mod-keyword-reply",
        name="关键词自动回复（mod 样例）",
        description="消息包含关键词时直接返回预设回复，不经过AI",
        category="utility",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool",   label="启用"),
            "rules":   FieldSchema(type="string", label="回复规则", multiline=True,editor="keyword_rules",
                description="逐条填写关键词和回复，支持多行回复；多条同时命中时按从上到下的顺序回复。"),
        },
        default_settings={
            "enabled": False,
            "rules":   "",
        },
    )

    @staticmethod
    def _parse_rules(raw):
        return [(key.strip(), reply.strip()) for key, reply in read_rule_rows(raw)
                if key.strip() and reply.strip()]

    def validate_settings(self, settings):
        raw = settings.get('rules', '')
        if raw != self.get('rules', '') and raw.lstrip().startswith(PREFIX):
            validate_rule_rows(read_rule_rows(raw))

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        # mod 的钩子拿到的是只读门面 ModRuntime：设置通过它读，拿不到时才回落到自身设置。
        settings = getattr(runtime, "settings", None)
        if not isinstance(settings, dict):
            settings = self.settings
        if not settings.get("enabled", self.META.default_settings.get("enabled", False)):
            return None
        try:
            text = message.get("text", "").lower()
            for keyword, reply in self._parse_rules(settings.get("rules", "")):
                if keyword.lower() in text:
                    return reply
        except Exception:
            pass
        return None
