"""keyword_reply.py - 关键词自动回复: return preset reply when keyword matches."""
from __future__ import annotations
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema
from ..keyword_rules import read_rule_rows,validate_rule_rows,PREFIX


class KeywordReply(PluginBase):
    META = PluginMeta(
        id="keyword_reply",
        name="关键词自动回复",
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

    def _parse_rules(self):
        return [(key.strip(),reply.strip()) for key,reply in read_rule_rows(self.get('rules',''))
                if key.strip() and reply.strip()]

    def validate_settings(self,settings):
        raw=settings.get('rules','')
        if raw!=self.get('rules','') and raw.lstrip().startswith(PREFIX):
            validate_rule_rows(read_rule_rows(raw))

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        if not self.get("enabled"):
            return None
        try:
            text  = message.get("text", "").lower()
            rules = self._parse_rules()
            for kw, reply in rules:
                if kw.lower() in text:
                    return reply
        except Exception:
            pass
        return None
