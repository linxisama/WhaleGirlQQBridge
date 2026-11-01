"""turn_complete.py - 自动判断"他这一轮说完了没有"。

移植自 JS 插件 ``turn-complete``（QQ Agent）。JS 版挂在宿主的唤醒倒计时仲裁上
（能力 ``wake.burst-policy`` / ``wake.first-delay``），Python 运行时没有跨轮倒计时，
因此按本项目的钩子语义**降级实现**：

- 判定逻辑（``verdict``）逐条照搬：结尾悬着 / 落在连接词上 / 太短 → 还没说完；
  像问句、只是喊人、说得比较长 → 说完了。
- 判定为"还没说完"时，用 ``on_pre_ai`` 的 ``delay`` 把这一轮往后推（有封顶），
  近似 JS 版的"续期"，但**只在当前这一轮内有效**：下一轮会重新起算。
- 判定为"说完了"时不缩短等待（JS 版会把倒计时提到 hurryMs；Python 侧没有可提前的
  倒计时，只能不干预）。
- 多人同时说话时，把说话人摘要写进 ``extra_system``，让模型自己判断该接谁。

未移植：``wake.first-delay`` 的小模型判定（需要"插件发起一次模型调用"的接口，
当前运行时没有）；``hurryMs`` 提前回复（没有可提前的倒计时）。
"""

from __future__ import annotations

import re
import time
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

# 结尾的装饰（引号、括号）先剥掉，再判断真正的结尾。
_TAIL_DECOR_RE = re.compile(r'[”"’\'）)\]】》」』]+$')
# 句末标点：有它更确定，但没有也照样能判。
_END_PUNCT_RE = re.compile(r'[。！？!?]$')
# 结尾悬着的标点：话明显没说完。
_OPEN_TAIL_RE = re.compile(r'[，,、；;：:…~～—\-（(【\[「『《<“"]$')
# 结尾落在这些词上，基本就是话说一半。
_CONTINUER_TAIL = (
    '然后', '但是', '不过', '而且', '所以', '因为', '就是', '那个', '还有',
    '对了', '另外', '其实', '反正', '要不', '不然', '再说', '顺便', '的话', '我',
)
# 只是喊一声：正题往往还在后面。
_OPENER_RE = re.compile(
    r'^(在吗|在么|在不在|有人在吗|有人吗|你好|您好|你好呀|哈喽|hi|hello|hey|在干嘛|忙吗|有空吗)[~～!！?？。.\s]*$',
    re.IGNORECASE,
)
# 问句特征：聊天里没人打问号，只能看这些词。
_QUESTION_RE = re.compile(r'(吗|呢|嘛|多少|什么|怎么|咋|哪|谁|几|能不能|可不可以|是不是|有没有|要不要|行不行|好不好|可以|帮我|请问|求)')

SHORT_CHARS = 6          # 短到多少字以内算"看不太出来"
RAPID_MS = 1800          # 跟上一条隔多久之内算"还在连着打字"
FRAGMENT_CHARS = 4       # 上一条只有这么短，就当他是在一个字一个字打字
BATCH_TTL_SEC = 600      # 说话人摘要保留时间
MAX_BATCHES = 80
# "先开口的人"这个锚点多久之后失效。超过它，换个人说话就算新的一轮，
# 否则锚点会把会话永久钉在第一个说话的人身上。
ANCHOR_WINDOW_SEC = 120


def verdict(text: str) -> dict:
    """判断这一条消息"像不像说完了"。

    返回 ``{"verdict": "done"|"unfinished", "why": str}``。
    刻意不留"说不准"档：那一档两头不讨好，实测会插嘴。
    """
    raw = str(text or '').strip()
    if not raw:
        return {"verdict": "unfinished", "why": "这条消息是空的"}
    tail = _TAIL_DECOR_RE.sub('', raw).strip() or raw

    if _OPEN_TAIL_RE.search(tail):
        return {"verdict": "unfinished", "why": f"结尾是「{tail[-1]}」，话明显悬着"}

    for word in _CONTINUER_TAIL:
        if tail.endswith(word):
            return {"verdict": "unfinished", "why": f"结尾是「{word}」，像是在等下一句"}

    if _OPENER_RE.match(tail):
        return {"verdict": "done", "why": "只是在喊人，该应声了"}

    short = len(tail) <= SHORT_CHARS
    # 短但说得完整的问句：他问完了，在等你答。
    # 必须够长（>=3 字）：单独一个「吗」「呢」只是那句话的尾巴。
    if short and len(tail) >= 3 and _QUESTION_RE.search(tail):
        return {"verdict": "done", "why": f"短短一句问话（{len(tail)} 字），他问完了"}

    if short:
        return {"verdict": "unfinished", "why": f"只有 {len(tail)} 个字，多半还没说完"}

    return {
        "verdict": "done",
        "why": "结尾是句末标点，说完了" if _END_PUNCT_RE.search(tail) else "这句话说得比较完整",
    }


def summarize(entries) -> dict:
    """按发送者归拢一批消息，返回 ``{"speakers": [{"id","name","count"}], "total": n}``。

    机器人自己发的（``self`` 字段为真）不计入 —— 那不算"别人在说话"。
    """
    order = []
    by_id = {}
    total = 0

    def speaker_name(item):
        sid = str(item.get("sender") or item.get("senderId") or '').strip()
        nick = str(item.get("sender_name") or item.get("senderName") or '').strip()
        return nick or (f"QQ:{sid}" if sid else "未知的人")

    for item in entries or []:
        if not isinstance(item, dict) or item.get("self") is True:
            continue
        total += 1
        sid = str(item.get("sender") or item.get("senderId") or '').strip() or 'unknown'
        entry = by_id.get(sid)
        if entry is None:
            entry = {"id": sid, "name": speaker_name(item), "count": 0}
            by_id[sid] = entry
            order.append(entry)
        entry["count"] += 1
        if entry["name"].startswith("QQ:") or entry["name"] == "未知的人":
            better = speaker_name(item)
            if better and not better.startswith("QQ:") and better != "未知的人":
                entry["name"] = better
    return {"speakers": order, "total": total}


class TurnComplete(PluginBase):
    META = PluginMeta(
        id="turn_complete",
        name="对话完成判定",
        description=(
            "同一个人连着发消息时，别在人家话还没说完的时候插嘴。"
            "按内容判定：结尾悬着、落在连接词上、或者只有几个字 → 再多等一会儿；"
            "像一句问话、只是在喊人、说得比较长 → 照常回。"
            "（移植说明：JS 版靠宿主的唤醒倒计时仲裁，本项目只在当前一轮内延后等待，"
            "不会提前回复；多人同时说话时把说话人摘要告诉模型。）"
        ),
        category="message",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "contentJudge": FieldSchema(
                type="bool", label="按内容判定",
                description="关掉后只按节奏（消息间隔）判定，不分析文字内容。",
            ),
            "quietMs": FieldSchema(
                type="int", label="没说完再等(毫秒)", min=0, max=120000,
                description="判定为“还没说完”时，把这一轮往后推这么久。",
            ),
            "capMs": FieldSchema(
                type="int", label="一轮最多等(毫秒)", min=0, max=600000,
                description="封顶：对方一直发、一直没说完，也不会一直不吭声。0 = 完全不等待。",
            ),
            "anchorOnly": FieldSchema(
                type="bool", label="只认先开口的人",
                description="开启时，群里别人插话不会被算进他这一轮。",
            ),
            "tellModel": FieldSchema(
                type="bool", label="把说话人告诉模型",
                description="多人同时说话时，把“谁在说、各说了几条”写进提示词。",
            ),
            "maxDelayMs": FieldSchema(
                type="int", label="单轮延迟上限(毫秒)", min=0, max=120000,
                description="本项目安全阀：无论判定结果如何，单轮延后不超过这个值。",
            ),
        },
        default_settings={
            "enabled": False,
            "contentJudge": True,
            "quietMs": 6000,
            "capMs": 20000,
            "anchorOnly": True,
            "tellModel": True,
            "maxDelayMs": 8000,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # chatKey -> {"speakers": [...], "total": n, "ts": float}
        self._batches: dict = {}
        # chatKey -> {"ts": float, "len": int, "anchor": str, "round_start": float}
        self._marks: dict = {}

    # ── 内部 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _chat_key(message: dict) -> str:
        kind = message.get("kind") or "?"
        target = message.get("target") or "?"
        return f"{kind}:{target}"

    def _prune(self, now: float):
        for key, value in list(self._batches.items()):
            if now - float(value.get("ts") or 0) > BATCH_TTL_SEC:
                self._batches.pop(key, None)
        while len(self._batches) > MAX_BATCHES:
            self._batches.pop(next(iter(self._batches)), None)

    def _rapid_follow_up(self, chat_key: str, now: float) -> Optional[int]:
        """跟上一条只隔几百毫秒 → 他还在连着打字。返回间隔毫秒，判不出来返回 None。"""
        mark = self._marks.get(chat_key)
        if not mark:
            return None
        gap_ms = (now - float(mark.get("ts") or 0)) * 1000
        if gap_ms < 0 or gap_ms > 24 * 60 * 60 * 1000:
            return None
        return int(gap_ms) if gap_ms <= RAPID_MS else None

    def _fragment_mode(self, chat_key: str, text: str) -> bool:
        """上一条也很短、这一条也短 → 他在一个字一个字往外打。"""
        mark = self._marks.get(chat_key)
        if not mark:
            return False
        return int(mark.get("len") or 0) <= FRAGMENT_CHARS and len(str(text or '').strip()) <= SHORT_CHARS

    # ── 钩子 ──────────────────────────────────────────────────────────────
    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        try:
            if message.get("notice"):
                return None
            chat_key = self._chat_key(message)
            now = time.time()
            text = message.get("text") or ""
            sender = str(message.get("sender") or "")

            cap_ms = max(0, int(self.get("capMs", 20000) or 0))
            if cap_ms <= 0:
                return None

            anchor_only = bool(self.get("anchorOnly", True))
            if anchor_only:
                mark = self._marks.get(chat_key) or {}
                anchor = str(mark.get("anchor") or "")
                anchor_fresh = (now - float(mark.get("ts") or 0)) <= ANCHOR_WINDOW_SEC
                # 只认"先开口的那个人"，但锚点必须还在有效期内：否则一轮结束后
                # 下一个人永远接不上话（锚点会被当成永远有效的旧记录）。
                if anchor and anchor_fresh and anchor != sender:
                    return None

            if self.get("contentJudge", True):
                judged = verdict(text)
            else:
                judged = {"verdict": "unfinished", "why": "还没停下来"}

            rapid = self._rapid_follow_up(chat_key, now)
            fragment = self.get("contentJudge", True) and self._fragment_mode(chat_key, text)
            if judged["verdict"] == "done" and (rapid is not None or fragment):
                judged = {
                    "verdict": "unfinished",
                    "why": (f"跟上一条只隔 {rapid}ms，还在一块儿打字" if rapid is not None
                            else "上一条也只有几个字，像是在一个字一个字往外打"),
                }

            self._marks[chat_key] = {"ts": now, "len": len(text.strip()), "anchor": sender}

            result = {}
            if self.get("tellModel", True):
                note = self._speaker_note(chat_key, now, message, judged)
                if note:
                    # 本轮发言人摘要每轮都不同 → 排到提示词最后（见「缓存稳定性」）
                    result.update(self.volatile_hint(runtime, note))

            if judged["verdict"] == "unfinished":
                delay_ms = min(max(0, int(self.get("quietMs", 6000) or 0)), cap_ms)
                delay_ms = min(delay_ms, max(0, int(self.get("maxDelayMs", 8000) or 0)))
                if delay_ms > 0:
                    result["delay"] = delay_ms / 1000.0
            return result or None
        except Exception:
            # 判定出错绝不该影响"什么时候回复"：按不干预处理。
            return None

    def _speaker_note(self, chat_key: str, now: float, message: dict, judged: dict) -> str:
        """累计这一轮的说话人摘要并生成提示词片段。

        无论判定结果是 done 还是 unfinished 都必须记一笔：否则"说完了"的那些消息
        不会进入下一批的上下文，多人说话时就会漏报。
        """
        try:
            entry = {"sender": message.get("sender"), "sender_name": message.get("sender"),
                     "text": message.get("text") or ""}
            history = self._batches.get(chat_key) or {}
            previous = history.get("entries") or []
            # 本条已经由 _marks 记过；这里只累计别人的与更早的批次
            batch = [item for item in list(previous)[-9:] if item.get("sender") != entry["sender"]]
            batch.append(entry)
            info = summarize(batch)
            self._batches[chat_key] = {"speakers": info["speakers"], "total": info["total"],
                                      "entries": batch, "ts": now}
            self._prune(now)

            parts = []
            if judged["verdict"] == "unfinished":
                parts.append(
                    "【这一批可能还没说完】最后一条看起来像话说到一半："
                    f"{judged['why']}。如果判断他确实还没说完，可以不说话、直接结束这一轮；"
                    "但如果他明显在等你答话（问了你什么、喊了你），就别憋着。"
                )
            others = [s for s in info["speakers"] if s["id"] != str(message.get("sender") or "")]
            if len(info["speakers"]) >= 2 and others:
                who = "、".join(f"{s['name']}（{s['count']} 条）" for s in info["speakers"])
                parts.append(
                    f"【这一批不只一个人在说话】一共 {len(info['speakers'])} 个人：{who}。"
                    "别把它们当成同一个人连着说的一串话；想接谁的话，先确认那句是谁说的。"
                )
            return "\n".join(parts)
        except Exception:
            return ""
