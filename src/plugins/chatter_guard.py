"""chatter_guard.py - 水聊免回：连续水聊就不接话。

对齐 JS 版 ``chatter-guard``（QQ Agent，MIT）的判据。**刻意不用"字数太少"当规则** ——
原版注释写得很清楚：那会把「在吗」「谢谢」「几点」一起误伤；本文件早先的实现正是用
``len(text) < minLength``，属于相反方向，已改正。

只认三类明确的水：
1. 只有表情/图片/卡片，没有一个字；
2. 命中水词表（哈哈、草、6、。。。、笑死…）；
3. 同一个字重复（哈哈哈哈）或全是标点（？？？）。

并且必须**连着 N 条**、且在时间窗内；中间插一句正常话就重新计数。
命中后返回 ``True``（on_block）—— 消息仍会照常存档，只是不叫醒模型（0 token）。
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

# 内置水词表（纯水、没有信息量的）。
# 刻意**不含**「在吗」「谢谢」「好的」这类需要回应的。
DEFAULT_FILLERS = (
    '哈哈', '哈哈哈', '哈哈哈哈', '嘿嘿', '嘻嘻', '呵呵', '噗', '草', '卧槽', '我草',
    '6', '666', '牛', '牛啊', '秀', '好活', '绝了', '笑死', '笑死我', '绷', '乐',
    'emmm', 'emm', '呃', '嗯', '啊这', '这', '?', '？', '。。。', '...', '……',
    '233', 'hhh', 'hhhh', 'xd', 'ojbk', '彳亍', '行吧', 'ok', 'OK',
)

# 存档/渲染里的媒体占位形式：[图片:xxx] [表情:xxx] [卡片：xxx] …
_MEDIA_PLACEHOLDER = re.compile(r'\[[^\]]{0,60}\]')
# 字母、数字、汉字都算"有内容"。
# 判定"没有一个字"时按 Unicode 类别去掉标点与符号（对齐 JS 的 \p{P}\p{S}），
# 而不是只看有没有字母数字汉字 —— 否则「？」「。」会被当成"有内容"。
_HAS_CONTENT = re.compile(r'[0-9A-Za-z\u4e00-\u9fff]')
# 同一个字重复 3 次以上
_REPEAT_CHAR = re.compile(r'^(.)\1{2,}$')
MAX_STREAK_KEYS = 600


def strip_punctuation_and_symbols(text: str) -> str:
    """去掉空白、标点（P*）与符号（S*），对齐 JS 的 ``[\\s\\p{P}\\p{S}]``。"""
    keep = []
    for char in str(text or ""):
        if char.isspace():
            continue
        if unicodedata.category(char)[:1] in ("P", "S"):
            continue
        keep.append(char)
    return "".join(keep)


def split_words(raw: str) -> list:
    """按逗号/顿号/分号/空白切分，去重保序。"""
    result = []
    for item in re.split(r'[,，、;；\s]+', str(raw or "")):
        word = item.strip()
        if word and word not in result:
            result.append(word)
    return result


def is_media_only(text: str) -> bool:
    """只有表情/图片/卡片，没有一个字。

    先去掉 ``[图片:xxx]`` 这类存档占位，再去掉空白/标点/符号；
    剩下什么都没有，就是"纯媒体"（纯标点如「？？？」也归到这一档）。
    """
    stripped = _MEDIA_PLACEHOLDER.sub('', str(text or '')).strip()
    return strip_punctuation_and_symbols(stripped) == ""


def is_filler(text: str, extra_words=None) -> bool:
    """纯水话：水词、同字重复、全标点。"""
    raw = str(text or '').strip()
    if not raw:
        return True
    compacted = re.sub(r'\s+', '', raw)
    words = list(DEFAULT_FILLERS) + list(extra_words or [])
    for word in words:
        if compacted == word:
            return True
        # 「哈哈」这类两字以上的词，重复两遍也算水（哈哈哈哈）
        if len(word) >= 2 and compacted == word * 2:
            return True
    if not _HAS_CONTENT.search(compacted):
        return True                     # 全是标点/符号
    if len(compacted) <= 12 and _REPEAT_CHAR.match(compacted):
        return True                     # 同一个字重复 3 次以上
    return False


def water_reason(text: str, skip_media: bool = True, skip_filler: bool = True,
                 extra_words=None) -> Optional[str]:
    """这条消息算不算"水"；正常消息返回 None。"""
    if skip_media and is_media_only(text):
        return '只有表情/图片'
    if skip_filler and is_filler(text, extra_words):
        return '纯水话'
    return None


class ChatterGuard(PluginBase):
    META = PluginMeta(
        id="chatter_guard",
        name="水聊免回",
        description=(
            "连着发表情包、或者一直说水话的时候，机器人就不接了（连模型都不叫醒，0 token）。"
            "只认三类明确的水：纯表情/图片、命中水词表（哈哈、草、6、。。。、笑死…）、"
            "同一个字重复或全是标点。**不用「字数太少」当判据**——那会误伤「在吗」「谢谢」「几点」。"
            "中间插一句正常话就重新计数。"
        ),
        category="message",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用（连续水聊就免回）"),
            "threshold": FieldSchema(
                type="int", label="连着几条就不回", min=2, max=20,
                description="默认 3，第 3 条起不再接话。设成 1 会变成「只要发水话就不理」，太容易误伤。",
            ),
            "windowSec": FieldSchema(
                type="int", label="多久之内算「连着」", min=10, max=3600,
                description="默认 180（3 分钟）。超过这个间隔就重新计数。",
            ),
            "skipMedia": FieldSchema(type="bool", label="纯表情包/图片算水"),
            "skipFiller": FieldSchema(type="bool", label="纯水话算水"),
            "fillerWords": FieldSchema(
                type="string", label="自己补充的水词",
                description="逗号分隔，会加到内置水词表里。误伤了就往这里删或关掉「纯水话算水」。",
            ),
            "alwaysReplyWhenAtMe": FieldSchema(
                type="bool", label="@ 它时仍然回",
                description="默认关。开着的话，哪怕对方在刷表情包，只要 @ 了它就照常接。",
            ),
            "exemptKeyword": FieldSchema(
                type="string", label="豁免关键词",
                description="正文含这个词的消息一律照常回复（本项目额外提供，JS 版没有）。",
            ),
        },
        default_settings={
            "enabled": False,
            "threshold": 3,
            "windowSec": 180,
            "skipMedia": True,
            "skipFiller": True,
            "fillerWords": "",
            "alwaysReplyWhenAtMe": False,
            "exemptKeyword": "",
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {(会话, 发送者): {"count": int, "at": float}}
        self._streaks: dict = {}

    def _bump_streak(self, chat_key: str, sender_id: str, is_water: bool) -> int:
        """连续计数：返回"连着第几条水"。来了正常话就清零。"""
        key = (chat_key, sender_id)
        window = max(10, int(self.get("windowSec", 180) or 180))
        now = time.time()
        if not is_water:
            self._streaks.pop(key, None)
            return 0
        record = self._streaks.get(key)
        if not record or now - float(record.get("at") or 0) > window:
            self._streaks[key] = {"count": 1, "at": now}
        else:
            record["count"] = int(record.get("count", 0)) + 1
            record["at"] = now
        while len(self._streaks) > MAX_STREAK_KEYS:
            self._streaks.pop(next(iter(self._streaks)))
        return int((self._streaks.get(key) or {}).get("count") or 1)

    def on_block(self, message: dict, runtime) -> bool:
        if not self.get("enabled"):
            return False
        try:
            if not isinstance(message, dict) or message.get("notice"):
                return False
            text = message.get("text", "") or ""
            chat_key = f"{message.get('kind')}:{message.get('target')}"
            sender_id = str(message.get("sender_id") or "").strip()
            if not sender_id:
                # 拿不到 QQ 号就无法按人计连击，宁可不拦
                return False

            # @ 它时仍然回（默认不回）：同时清掉连击，等于重新开始
            if self.get("alwaysReplyWhenAtMe") and message.get("at"):
                self._streaks.pop((chat_key, sender_id), None)
                return False

            keyword = str(self.get("exemptKeyword") or "")
            if keyword and keyword in text:
                return False

            reason = water_reason(
                text,
                skip_media=bool(self.get("skipMedia", True)),
                skip_filler=bool(self.get("skipFiller", True)),
                extra_words=split_words(self.get("fillerWords", "")),
            )
            count = self._bump_streak(chat_key, sender_id, bool(reason))
            if not reason:
                return False
            threshold = max(2, int(self.get("threshold", 3) or 3))
            return count >= threshold
        except Exception:
            # 过滤器坏掉不该让机器人整体不响应
            return False
