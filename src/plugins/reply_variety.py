"""reply_variety.py - 回复多样化：机器人别把自己的上一句当范本复读。

真机现象（2026-10-17 用户截图，官方模式）：5 分钟里 19 条回复全是同一个套路
（"哟，…本鲸…不服开帖，我奉陪"）。`anti_repeat` 只挡"同一条入站消息被重复触发"，
挡不住"模型每次换句话但结构、开场、口头禅都一样"。

做法：生成完先和**本会话最近几条自己的回复**比相似度（字符二元组 Jaccard）。
太像就带着一句明确指令重生成一次（只用一次额外调用，不做无限重试）；
仍然像就用新的这条，并在运行记录里留一句说明，避免"悄悄多发一次请求"。
"""
from __future__ import annotations

import re

from . import PluginBase, PluginMeta, FieldSchema

_SPLIT = re.compile(r"[\s，。！？、；：,.!?;:~…~（）()\"'“”‘’]+")


def _bigrams(text: str) -> set[str]:
    """字符二元组集合：中文短句上比词袋稳，也不用引入分词依赖。"""
    clean = _SPLIT.sub("", str(text or ""))
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[index:index + 2] for index in range(len(clean) - 1)}


def _ngrams(text: str, size: int) -> set[str]:
    clean = _SPLIT.sub("", str(text or ""))
    if len(clean) < size:
        return {clean} if clean else set()
    return {clean[index:index + size] for index in range(len(clean) - size + 1)}


def similarity(left: str, right: str) -> float:
    """0~1 的"重复程度"，专门针对"换个说法但用词/结构照抄"的复读。

    单纯 Jaccard 对长句太宽松：真机那对复读（"哟，又拿这个糊弄本鲸？我手撕过的键盘侠…"
    vs "笑死，就你？本鲸手撕过的键盘侠…"）Jaccard 只有 0.39，可读起来明显是同一套。
    所以用两个信号的均值：
    - **包含度**：候选句有多少二元组能在历史里找到（复读会照抄短语）；
    - **三字片段重合度**：抓"本鲸 / 不服开帖 / 手撕过的键盘侠"这类口头禅与固定搭配。

    实测（用户真实 19 条）：复读对 ≈0.52，正常新回复 0.0~0.17 → 默认阈值 45% 有余量。
    """
    if not str(left or "").strip() or not str(right or "").strip():
        return 0.0
    first, second = _bigrams(left), _bigrams(right)
    containment = len(first & second) / len(first) if first else 0.0
    first3, second3 = _ngrams(left, 3), _ngrams(right, 3)
    shared3 = len(first3 & second3) / len(first3) if first3 else 0.0
    return 0.5 * containment + 0.5 * shared3


def too_similar(reply: str, recent: list, threshold: float = 0.45) -> str:
    """返回最像的那条历史回复（没有超过阈值就返回空串）。"""
    reply = str(reply or "").strip()
    if len(reply) < 6:
        return ""
    worst, worst_score = "", 0.0
    for item in recent:
        item = str(item or "").strip()
        if not item:
            continue
        score = similarity(reply, item)
        if score > worst_score:
            worst, worst_score = item, score
    return worst if worst_score >= threshold else ""


class ReplyVariety(PluginBase):
    META = PluginMeta(
        id="reply_variety",
        name="回复多样化（防复读）",
        description=("回复和本会话最近几条自己的话太像时，带指令重生成一次，避免"
                     "每句都是同一个开场/口头禅（例如连续回十几条\"不服开帖\"）。"),
        category="rhythm",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用防复读"),
            "thresholdPercent": FieldSchema(type="int", label="相似度阈值%", min=30, max=95,
                                            description="越高越宽松。默认 45：与最近一条有一半以上用词/结构重合就算重复（真机复读对实测 0.52，正常新回复多在 0.2 以下）。"),
            "lookback": FieldSchema(type="int", label="对比最近几条回复", min=1, max=8),
            "retryOnce": FieldSchema(type="bool", label="太像时重生成一次",
                                     description="关闭后只做统计与提示，不会额外调用模型。"),
        },
        default_settings={
            "enabled": True,
            "thresholdPercent": 45,
            "lookback": 3,
            "retryOnce": True,
        },
    )

    # ── 查询：本会话最近几条机器人回复 ─────────────────────────────────
    def recent_replies(self, message: dict, runtime) -> list:
        db = getattr(runtime, "db", None)
        if db is None or not message:
            return []
        mode = str(getattr(runtime, "mode", "") or "")
        try:
            account = runtime.account() if callable(getattr(runtime, "account", None)) else ""
        except Exception:
            account = ""
        room = f"{message.get('kind')}:{message.get('target')}"
        limit = max(1, min(8, int(self.get("lookback", 3) or 3)))
        try:
            rows = db.execute(
                "SELECT reply FROM inbox WHERE mode=? AND account=? AND room=? AND state='sent'"
                " AND COALESCE(reply,'')<>'' ORDER BY created DESC LIMIT ?",
                (mode, account, room, limit)).fetchall()
        except Exception:
            return []
        return [str(row[0]) for row in rows if row and row[0]]

    # ── 生成后的检查 ────────────────────────────────────────────────
    async def refine(self, engine, conversation, reply, message, runtime, **kwargs) -> str:
        if not self.enabled or not reply:
            return reply
        try:
            recent = self.recent_replies(message, runtime)
            if not recent:
                return reply                     # 第一次说话，没有可比的
            threshold = max(0.3, min(0.95, int(self.get("thresholdPercent", 55) or 55) / 100))
            clash = too_similar(reply, recent, threshold)
            if not clash:
                return reply
            if not self.get("retryOnce", True):
                runtime.note(f"[防复读] 这条回复和之前很像（{threshold:.0%} 阈值），已提示但未重生成")
                return reply
            nudge = {
                "role": "user",
                "content": ("[系统提示] 你上一句是：" + clash[:80] +
                            "\n这次别再用同样的开场、同样的口头禅、同样的句式结尾。"
                            "换个具体角度回应，句子更短一些，只输出这一句回复本身。"),
            }
            # 重生成也要省 token：只带 system + 当前这轮，不要把整段历史再发一遍
            # （真机单轮 prompt 平均 1897 token，原样重发等于双倍花钱）。
            trimmed = []
            for item in conversation or []:
                if isinstance(item, dict) and item.get("role") == "system":
                    trimmed.append(item)
            last_user = next((item for item in reversed(conversation or [])
                              if isinstance(item, dict) and item.get("role") == "user"), None)
            if last_user:
                trimmed.append(last_user)
            retry = await engine.chat(trimmed + [nudge] if trimmed else [nudge], **kwargs)
            if not retry:
                return reply
            if too_similar(retry, recent, threshold):
                runtime.note("[防复读] 重生成后仍然像，先发这一版（不再重复请求）")
            else:
                runtime.note("[防复读] 这条回复和之前太像，已换一种说法重生成")
            return retry
        except Exception as exc:                 # 任何异常都不能影响正常回复
            try:
                runtime.note(f"[防复读] 检查失败已跳过：{type(exc).__name__}: {exc}")
            except Exception:
                pass
            return reply
