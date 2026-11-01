"""把插件的回复元数据组装成 OneBot 消息段。

背景（2026-10-13）：移植 JS 插件时发现一个硬缺口——`OneBotQQ.send()` 只会发
一个文本段：

    {"type": "text", "data": {"text": text}}

于是 `at-mention`（正文 `@名字` → 真 @ 提醒）这类插件在 Python 侧**无法变成可用**：
真 @ 必须由独立的 `at` 段产生，写在正文里对方收不到提醒。

本模块只做"元数据 → 消息段"的纯组装，不发送、不联网、不读配置，便于离线测试。
调用方（发送链）负责把 `at` 段与文本段按原顺序拼好，并把 `at` 段的 qq 填进去。
"""

from typing import Any, Dict, Iterable, List, Optional

# 允许出现在回复里的消息段类型（白名单，避免插件塞进任意 action）
ALLOWED_TYPES = frozenset({"text", "at", "image", "face", "record"})


def _text_segment(text: str) -> Dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def normalize_mentions(mentions: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """规整提及列表，只保留可用的 QQ 号，去重且保持先后顺序。"""
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in mentions or []:
        if isinstance(item, dict):
            qq = item.get("qq") or item.get("user_id")
        else:
            qq = item
        if qq is None:
            continue
        qq = str(qq).strip()
        if not qq or qq in seen:
            continue
        seen.add(qq)
        result.append({"type": "at", "data": {"qq": qq}})
    return result


def build_reply_segments(reply: str, meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """把回复文本与插件元数据组装成消息段列表。

    meta 支持的键（全部可选）：
      - ``mentions``：要 @ 的 QQ 号列表，元素可以是 ``"12345"``、``12345`` 或
        ``{"qq": "12345"}``。@ 段会被放在文本之前。
      - ``append_mentions``：True 时把 @ 段放在文本之后（默认放在之前）。
      - ``media``：附加媒体段列表，元素形如 ``{"type": "image", "data": {...}}``；
        只接受 ``ALLOWED_TYPES`` 内的类型，其余忽略。
      - ``text``：覆盖 reply 文本（插件改写正文时用）。

    返回的列表至少含一个段；文本为空且没有其它段时返回空列表，由调用方决定
    是否跳过发送（例如"只 @ 不说话了"的情况）。
    """
    meta = meta or {}
    text = meta.get("text", reply)
    text = "" if text is None else str(text)

    segments: List[Dict[str, Any]] = []
    mentions = normalize_mentions(meta.get("mentions"))
    if mentions and not meta.get("append_mentions"):
        segments.extend(mentions)
    if text:
        segments.append(_text_segment(text))
    if mentions and meta.get("append_mentions"):
        segments.extend(mentions)

    for item in meta.get("media") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        data = item.get("data")
        if kind in ALLOWED_TYPES and isinstance(data, dict):
            segments.append({"type": kind, "data": dict(data)})
    # 视频仅由明确的视频分享流程提供，普通插件 media 白名单继续保持不变。
    video=meta.get('video')
    if isinstance(video,dict):
        from .bilibili_share import approved_video
        if approved_video(video.get('url')):
            segments.append({'type':'video','data':{'file':video['url']}})
    return segments


def strip_mentions_from_text(text: str, names: Iterable[str]) -> str:
    """把正文里已被认领的 ``@名字`` 去掉，避免和真 @ 段重复显示。

    只处理形如 ``@名字`` 的整词（后面跟空白、标点或结尾），并且要求 @ 前面不是
    字母/数字/下划线——否则 ``foo@bar`` 这类邮箱式文本会被误删。
    """
    if not text:
        return text
    result = text
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        for candidate in (f"@{name}", f"@ {name}"):
            index = 0
            while True:
                found = result.find(candidate, index)
                if found == -1:
                    break
                end = found + len(candidate)
                following = result[end:end + 1]
                preceding = result[found - 1:found] if found else ""
                if following and (following.isalnum() or following == "_"):
                    index = end
                    continue
                if preceding and (preceding.isalnum() or preceding == "_"):
                    index = end
                    continue
                result = result[:found] + result[end:]
                index = found
    return result
