"""face_decode.py - QQ 内置表情转译（把 [表情14] 变成 [表情:微笑]）。

移植自 JS 插件 ``face-decode``（QQ Agent，MIT）。

**为什么需要它**：QQ 内置表情到达时是 OneBot 的 ``face`` 段，核心渲染成不透明的
数字占位符（``[表情14]``），模型认不出来。官方 id→名称表见 ``face_names.py``
（293 条，从 JS 版机械转换）。

**两层翻译**（与 JS 版一致）：
1. 传输层（``qq_transports.onebot_message``）现在直接把 ``face`` 段渲染成
   ``[表情:名称]``，并保留 ``media`` 原始段 —— 新消息天然可读；
2. 本插件是**兜底**：整轮对话（含历史存档、合并转发展开文本）里凡是
   ``[表情N]`` 形式的文本都会被翻成 ``[表情:名称]``。它覆盖第 1 层够不到的地方：
   装插件之前存下的旧记录、其它来源产生的旧格式。

纪律（沿袭 JS 版）：**查不到的 id 保持原样**，宁可不译也不许编。
"""

from __future__ import annotations

import re
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

try:
    from .face_names import FACE_NAMES
except Exception:      # 数据表损坏也不该拖垮整个插件系统
    FACE_NAMES = {}

# 数字占位符：[表情14] → [表情:微笑]
FACE_TAG = re.compile(r"\[表情(\d+)\]")


def face_name(face_id) -> str:
    """QQ 表情 id → 官方名；查不到返回空串（调用方保持原样）。"""
    try:
        return FACE_NAMES.get(int(face_id), "")
    except (TypeError, ValueError):
        return ""


def translate_face_tags(text) -> str:
    """把文本里的 ``[表情N]`` 翻成 ``[表情:名称]``；未知 id 原样保留。

    对已经是 ``[表情:名称]`` 的文本天然幂等（正则不匹配）。
    """
    source = "" if text is None else str(text)
    if not source or "[表情" not in source:
        return source

    def replace(match: "re.Match") -> str:
        name = face_name(match.group(1))
        return f"[表情:{name}]" if name else match.group(0)

    return FACE_TAG.sub(replace, source)


def translate_message_content(message: dict) -> bool:
    """就地翻译一条 LLM 消息的 content（字符串或分段列表），返回是否改动过。"""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        updated = translate_face_tags(content)
        if updated != content:
            message["content"] = updated
            return True
        return False
    if isinstance(content, list):
        changed = False
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                updated = translate_face_tags(part["text"])
                if updated != part["text"]:
                    part["text"] = updated
                    changed = True
        return changed
    return False


class FaceDecode(PluginBase):
    META = PluginMeta(
        id="face_decode",
        name="QQ表情转译",
        description=(
            "把 [表情14] 这类数字占位符翻成 [表情:微笑]，让模型读懂 QQ 内置表情。"
            "新消息由传输层直接渲染；本插件兜底历史记录与合并转发里的旧格式。"
            "查不到的 id（限时/季节表情）保持原样，不会瞎编名字。"
        ),
        category="utility",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "userOnly": FieldSchema(
                type="bool", label="只处理用户消息",
                description="默认开。表情占位符只会出现在历史与用户输入里，"
                            "模型输出里出现属异常情况。",
            ),
            "includeAssistant": FieldSchema(
                type="bool", label="也处理模型侧历史",
                description="开启后连 assistant 的历史消息一起翻译（默认关）。",
            ),
        },
        default_settings={
            "enabled": True,
            "userOnly": True,
            "includeAssistant": False,
        },
    )

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        """在把对话交给模型之前翻译表情占位符。

        只改对话内容，不拦消息、不加延迟，所以返回的 hints 里只有 extra_system 为空、
        block 为假 —— 实际做法是就地改 conversation（与 JS 版 before-llm-messages 一致）。
        """
        if not self.get("enabled"):
            return None
        try:
            user_only = bool(self.get("userOnly", True))
            also_assistant = bool(self.get("includeAssistant", False))
            for item in conversation or []:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                if role == "user" or (role == "assistant" and also_assistant and not user_only):
                    translate_message_content(item)
        except Exception:
            # 翻译失败绝不能挡住回复
            return None
        return None
