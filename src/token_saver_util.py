"""省 token 的公共小工具（纯函数，便于离线测试与多处复用）。

真机实测（2026-10-17，用户 115 轮）：prompt 平均 1897 token、输出只有 25 token，
**98.7% 的消耗在"每轮都重发"的上下文里**：人设提示词、群聊背景、历史轮次。
所以省 token 的主战场是"减少重复发送"，而不是压缩回复。
"""
from __future__ import annotations

import re
import hashlib
import json

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:~…~（）()\"'“”‘’\-]+")


def fingerprint(text) -> str:
    """把一句群聊/对话文本归一成可比较的指纹（去空白标点、小写）。"""
    return _PUNCT.sub("", str(text or "")).lower()[:160]


def dedupe_ambient(context, conversation) -> list:
    """丢掉"已经在对话历史里出现过"的背景消息。

    背景块与历史轮次来自同一个群，同一条消息经常两处都发一遍 —— 用户实测里
    "群聊背景"与"历史对话"重复度很高，去掉纯属白省。返回过滤后的背景列表。
    """
    if not isinstance(context, list):
        return []
    seen = set()
    for item in conversation or []:
        if isinstance(item, dict):
            mark = fingerprint(item.get("content"))
            if mark:
                seen.add(mark)
    result = []
    for item in context:
        if not isinstance(item, dict):
            continue
        mark = fingerprint(item.get("text"))
        if not mark or mark in seen:
            continue
        seen.add(mark)
        result.append(item)
    return result


def dedupe_conversation(conversation) -> int:
    """删掉对话里"内容完全相同"的重复轮次（保留最靠后的一条）。返回删除条数。

    群里同一个人连发三遍同一句、或插件把同一段背景插了两次，都会变成重复的 prompt。
    """
    if not isinstance(conversation, list):
        return 0
    keep_from_end = set()
    removed = 0
    for index in range(len(conversation) - 1, -1, -1):
        item = conversation[index]
        if not isinstance(item, dict):
            continue
        if item.get('role') in ('system','tool') or isinstance(item.get('content'),list):
            continue
        # 长文本仅前160字相同并不代表重复；比较完整正文，避免误删不同的网页或转发。
        body=str(item.get('content') or '').strip()
        mark = (item.get("role"), hashlib.sha256(body.encode()).hexdigest() if body else '')
        if not mark[1]:
            continue
        if mark in keep_from_end:
            del conversation[index]
            removed += 1
            continue
        keep_from_end.add(mark)
    return removed


def cap_total_chars(conversation, limit: int) -> int:
    """把整段对话压到 ``limit`` 字符以内：**只删最早的历史轮次**。

    保护：system 消息（人设）与最后一条（当前这轮）永不删除 —— 删这两样会直接
    让人设失效或答非所问。返回省下的字符数。
    """
    if limit <= 0 or not isinstance(conversation, list) or len(conversation) <= 2:
        return 0
    def size(items):
        return sum(len(str(item.get("content") or "")) for item in items if isinstance(item, dict))
    saved = 0
    index = 1                      # 从第二条开始找可删的（第 0 条可能是 system/背景）
    while index < len(conversation) - 1 and size(conversation) > limit:
        item = conversation[index]
        if isinstance(item, dict) and item.get("role") == "system":
            index += 1
            continue
        before = size(conversation)
        del conversation[index]
        saved += before - size(conversation)
    return max(0, saved)
