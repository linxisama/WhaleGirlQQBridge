"""群成员名录与"正文 @名字 → 真 @ 段"解析。

移植自 JS 插件 at-mention 的 ``lib/at-resolver.js``（MIT，QQ Agent 项目）。

为什么需要：OneBot 协议里真 @ 只能由独立的 ``at`` 消息段产生；正文里写 ``@某人``
只是普通字符，**对方收不到提醒**。而模型手上通常只有昵称、没有 QQ 号。

设计原则（照搬原版）：**宁可认不出，也绝不猜错**。@ 错人是社交事故，比没 @ 上严重。
因此：完全同名 > 唯一前缀匹配；名字短于 2 字不认；多个候选无法确定唯一时放弃。

中文没有空格分词，``@小明你好`` 里名字到底是"小明"还是"小明你好"无法从字面判断，
所以做法是**拿已知名单去比对正文（最长优先）**，而不是先从正文切出名字再查。

本模块纯逻辑，不联网、不依赖 PyQt6，便于离线测试。
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

# 名字里不可能出现的字符：出现即视为名字结束（标点、空白、换行等）
_STOP_CHARS = set(" \t\r\n,，。.、！!？?；;：:“”\"'‘’（）()[]【】<>《》@#~`|/\\*+=%^&$…—-")
# '@' 前面紧邻这些字符时说明是邮箱之类，不算 @ 人
_EMAIL_PREV = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-")
_MAX_NAME_SCAN = 32      # @ 之后最多看多少个字符
_MIN_NAME_LEN = 2        # 名字短于这个长度不认（单字匹配面太广）


def _is_stop(char: str) -> bool:
    return char in _STOP_CHARS


def _scan_typed(text: str, start: int) -> str:
    """取 ``@`` 之后到停用符为止的一段（最多 32 字）。"""
    end = start
    while end < len(text) and end - start < _MAX_NAME_SCAN and not _is_stop(text[end]):
        end += 1
    return text[start:end]


def _best_match(pool: List[Dict[str, str]], text: str, at_index: int,
                allow_partial: bool, claimed: set) -> Optional[tuple]:
    """在 ``at_index`` 这个 '@' 之后找最长可用匹配。

    返回 ``(member, 被占用的字符数)`` 或 None；占用的字符数不含 '@' 本身。
    """
    after = text[at_index + 1:]
    typed = _scan_typed(text, at_index + 1)
    if not typed:
        return None

    # ① 完全匹配（名字后面紧跟边界或串尾），其中取最长的名字。
    #    要求边界是必须的：`@小明同` 里的「小明」不算完整名字，
    #    否则会把名字截断、留下孤零零的"同"。
    exact = None
    for member in pool:
        name = member["name"]
        if not after.startswith(name):
            continue
        next_index = len(name)
        at_boundary = next_index >= len(after) or _is_stop(after[next_index])
        if not at_boundary:
            continue
        if exact is None or len(name) > len(exact[0]["name"]):
            exact = (member, len(name))
    if exact is not None:
        return exact

    # ② 前缀匹配只在没有完全匹配时启用（群名片后缀没打全：`@小明同` → 「小明同学」）。
    #    两条约束都不能少：
    #    a) typed 必须是某个更长名字的真前缀，否则 `@小明` 会被「小明同学」抢走；
    #    b) typed 不能等于任何人的完整名字 —— 反例 `@小明 @小明 早`：第二次会去
    #       匹配「小明同学」，等于凭空 @ 了一个无关的人。
    if not allow_partial:
        return None
    if len(typed) < _MIN_NAME_LEN:
        return None
    if any(member["name"] == typed for member in pool):
        return None
    partial = None
    for member in pool:
        name = member["name"]
        if member["userId"] in claimed:
            continue
        if len(name) <= len(typed):
            continue
        if not name.startswith(typed):
            continue
        if partial is None or len(name) < len(partial[0]["name"]):
            partial = (member, len(typed))
    return partial


def resolve_mentions(text: str, members: Iterable[dict], max_mentions: int = 5,
                     allow_partial_match: bool = True) -> dict:
    """把正文里的 ``@名字`` 认领成 QQ 号。

    ``members`` 是 ``[{"userId": "123", "name": "小明"}, ...]``。返回::

        {"text": 去掉已认领 @名字 之后的正文,
         "atUserIds": ["123", ...],
         "hits": [{"name": "小明", "userId": "123"}, ...]}

    异常情况一律退回原文本（绝不阻断发送）。
    """
    raw = "" if text is None else str(text)
    result = {"text": raw, "atUserIds": [], "hits": []}
    try:
        if not raw or "@" not in raw:
            return result
        try:
            limit = max(1, min(10, int(max_mentions)))
        except (TypeError, ValueError):
            limit = 5

        pool = []
        for member in members or []:
            if not isinstance(member, dict):
                continue
            user_id = str(member.get("userId") or "").strip()
            name = str(member.get("name") or "").strip()
            if not user_id.isdigit():
                continue
            if len(name) < _MIN_NAME_LEN:
                continue
            pool.append({"userId": user_id, "name": name})
        if not pool:
            return result

        # 名字长的排前面：`小明` 与 `小明同学` 同时在名单里时，先试长的那个。
        pool.sort(key=lambda item: len(item["name"]), reverse=True)

        claimed = set()
        hits = []
        spans = []
        cursor = 0
        while cursor < len(raw):
            at = raw.find("@", cursor)
            if at == -1:
                break
            if at > 0 and raw[at - 1] in _EMAIL_PREV:
                cursor = at + 1
                continue

            # 连写的 @@ 要在同一位置继续比对，否则 `@@小明` 永远认不出来
            lead = at
            while lead < len(raw) and raw[lead] == "@":
                lead += 1
            if lead >= len(raw):
                cursor = at + 1
                continue

            hit = _best_match(pool, raw, lead - 1, allow_partial_match, claimed)
            if hit is None:
                cursor = at + 1
                continue

            member, length = hit
            span = (at, lead + length)
            if member["userId"] in claimed:
                # 同一个人写了第二遍：吃掉这段文字，但不重复 @、不占名额
                spans.append(span)
                cursor = lead + length
                continue
            if len(hits) >= limit:
                cursor = lead + length
                continue

            claimed.add(member["userId"])
            hits.append({"name": member["name"], "userId": member["userId"]})
            spans.append(span)
            cursor = lead + length

        if not hits:
            return result

        # 从后往前删，避免前面的删除让后面的下标错位
        cleaned = raw
        for start, end in reversed(spans):
            cleaned = cleaned[:start] + cleaned[end:]
        cleaned = re.sub(r'^[\s,，、]+', '', cleaned)
        cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned).strip()

        result["text"] = cleaned
        result["atUserIds"] = [hit["userId"] for hit in hits]
        result["hits"] = hits
        return result
    except Exception:
        return {"text": raw, "atUserIds": [], "hits": []}


class MemberDirectory:
    """按会话累积"谁是谁"，供 @ 解析使用。

    本项目拿不到 OneBot 的群成员列表接口（需要额外 action），所以名录靠**观察**：
    每条入站消息都带着发送者的 QQ 号与昵称，攒起来就是一份"最近说过话的人"名单。
    对"@ 刚才说话的人"这个最常见场景足够用。
    """

    def __init__(self, max_per_room: int = 200):
        self._rooms: Dict[str, Dict[str, str]] = {}
        self._max = max(1, int(max_per_room))

    @staticmethod
    def room_key(message: dict) -> str:
        return f"{message.get('kind') or '?'}:{message.get('target') or '?'}"

    def observe(self, message: dict) -> None:
        """从一条消息里记下发送者（只用 QQ 号与昵称，不碰其他字段）。"""
        try:
            if message.get("notice"):
                return
            user_id = str(message.get("sender_id") or "").strip()
            if not user_id.isdigit():
                return
            name = str(message.get("sender_name") or message.get("sender") or "").strip()
            room = self.room_key(message)
            members = self._rooms.setdefault(room, {})
            if name:
                members[user_id] = name
            else:
                members.setdefault(user_id, "")
            # 简单的容量控制：超了就丢掉最早记下的一批（dict 保持插入顺序）
            while len(members) > self._max:
                members.pop(next(iter(members)))
        except Exception:
            return

    def members(self, message: dict) -> List[dict]:
        """返回该会话的 ``[{"userId", "name"}, ...]``。"""
        room = self.room_key(message)
        return [
            {"userId": user_id, "name": name}
            for user_id, name in (self._rooms.get(room) or {}).items()
            if name
        ]

    def forget(self, message: dict) -> None:
        self._rooms.pop(self.room_key(message), None)
