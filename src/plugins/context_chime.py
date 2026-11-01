"""上下文插话判定：在运行时的异步生成前钩子里完成一次短判定。
判定本身会消耗一次模型调用；拒绝接话可节省后续聊天回复调用。
timeoutMs 是硬超时，判定失败/超时遵循各自配置。兼容离线同步调用。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

_TZ_CST = timezone(timedelta(hours=8))
HISTORY_MAX = 50
_MEDIA_ONLY = re.compile(r'^\[(图片|表情|动图|GIF|视频|语音|文件|合并转发|转发消息)[^\]]*\]$')


def _early_finish(manager):
    """拿到 early_finish 插件（没装或没启用就返回 None）——花额外调用前先问它一句。"""
    try:
        from .early_finish import early_finish_plugin
        return early_finish_plugin(manager)
    except Exception:
        return None


def clip(text, limit: int) -> str:
    """压掉换行与多余空白，超长截断加省略号（对齐 JS 版 clip）。"""
    compact = re.sub(r'\s+', ' ', str(text or '')).strip()
    return compact[:limit] + '…' if len(compact) > limit else compact


def parse_judge(raw) -> Optional[dict]:
    """宽容解析模型输出；解析不出来返回 None。

    容忍 ```json 围栏与前后多余文字；``reply`` 接受 0/1、true/false、是/否。
    """
    text = str(raw or "")
    if not text.strip():
        return None
    match = re.search(r'\{[\s\S]*?\}', text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None

    raw_reply = obj.get("reply", obj.get("respond", obj.get("should_respond",
                  obj.get("answer", obj.get("should_reply")))))
    reply = None
    if isinstance(raw_reply, bool):
        reply = raw_reply
    elif isinstance(raw_reply, (int, float)):
        reply = raw_reply >= 1
    elif isinstance(raw_reply, str):
        reply = bool(re.match(r'^(1|true|yes|y|接|是)$', raw_reply.strip(), re.IGNORECASE))
    if reply is None:
        return None

    try:
        confidence = int(round(float(obj.get("confidence", 0) or 0)))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))
    why = clip(obj.get("why", obj.get("reason", "")), 40)
    return {"respond": reply, "confidence": confidence, "why": why}


def build_judge_messages(bot_name: str, self_nickname: str = "", persona_brief: str = "",
                         recent_lines: str = "", last_line: str = "",
                         prompt_extra: str = "") -> list:
    """构造判定提示词（system + user）。措辞照搬 JS 版，尤其"别读错人"那段。"""
    system_lines = [
        f'你在替一个 QQ 群里的机器人「{bot_name}」做一件小事：判断群里刚来的那条消息，它**该不该主动接话**。',
        '这个机器人平时只被 @ 或被叫到才回话。现在给它加了「看气氛主动接一句」的能力，所以你要像一个人那样判断：这句话里有没有它的戏份。',
        '',
        '【怎么读下面的记录（很重要，别读错人）】',
        '以「我：」开头的行是**机器人自己说过的话**；其它行都是群友。',
        f'机器人叫「{bot_name}」，但群里可能还有别的机器人、或者昵称跟它很像的人 —— 那些**都算群友**，不是它自己说的。「它刚说过话」只认「我：」开头的行。',
        '',
        '【该接（reply=1）】',
        '1. 消息是在跟机器人说话、问它、提到它的名字/身份/梗，或者明显在等它表态；',
        '2. 抛出了一个机器人接得上的话题：提问、求解、争论、玩梗、报新闻、发图求评价、报进度；',
        '3. 群里正热闹地接龙/问答，它接一句会让气氛更好；',
        '4. 有人分享情绪或经历（开心、抱怨、emo、告别、问候），朋友该应一声。',
        '',
        '【不该接（reply=0）】',
        '1. 明显是两个人之间的私事：约时间、私聊内容、互相安排、发文件发链接打卡；',
        '2. 低信息量应答：「嗯」「好」「哈哈」「?」这类，接了就成刷屏；',
        '3. 话题与机器人完全无关，或者消息是在 @ 别人（不是它）；',
        '4. 机器人**刚刚才说过话**，这时候再插一句会显得黏人（上下文里能看到它自己的发言）。',
        '',
        '宁可少接，不要乱接：判错一次"该接却没接"只是少说一句话，判错一次"不该接却接了"就是群里刷屏。拿不准时倾向 reply=0。',
    ]
    if prompt_extra:
        system_lines += ['', '【主人追加的判据】', prompt_extra]
    system_lines += [
        '',
        '只输出一个 JSON 对象，不要任何其它文字、不要 Markdown、不要解释：',
        '{"reply":0或1,"confidence":0到100的整数,"why":"不超过15个字的中文理由"}',
    ]

    user_lines = [f'【机器人】{bot_name}']
    if self_nickname and self_nickname != bot_name:
        user_lines[0] += f'（群里也被叫「{self_nickname}」）'
    if persona_brief:
        user_lines.append(f'【它的人设摘要】{persona_brief}')
    if recent_lines:
        user_lines.append('【最近的群聊（越靠下越新，"我："开头的是机器人自己说的）】\n' + recent_lines)
    user_lines.append(f'【最新一条，要判定的是这条】{last_line}')
    user_lines.append('按上面的规则判断：机器人该不该接这句？只回 JSON。')

    return [
        {"role": "system", "content": "\n".join(system_lines)},
        {"role": "user", "content": "\n\n".join(user_lines)},
    ]


def _list_has(list_text: str, target: str) -> bool:
    if not list_text or not target:
        return False
    parts = [item.strip() for item in re.split(r'[,，\s]+', str(list_text))]
    return str(target).strip() in parts


def today_key(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(_TZ_CST)).strftime("%Y-%m-%d")


class ContextChime(PluginBase):
    META = PluginMeta(
        id="context_chime",
        name="上下文插话判定",
        description=(
            "用一次极短的模型调用判断「这条消息值不值得主动接话」。"
            "⚠️ 机制差异：原版在调模型之前判定，判「不接」= 0 token；本项目只能在 AI 调用前"
            "（同步钩子）里用独立事件循环发判定请求，判「不接」是拦住回复，"
            "**省不下已经花掉的 token**。判定失败默认按「不接」处理（与原版一致，保守侧）。"
        ),
        category="message",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            # 代码里读了 allowPrivate（私聊是否也插背景），schema 里以前没有 → 界面上永远开不起来
            "allowPrivate": FieldSchema(type="bool", label="私聊也插入背景"),
            "cooldownSec": FieldSchema(
                type="int", label="同群冷却(秒)", min=0, max=3600,
                description="默认 12：不让每条消息都打一次模型。",
            ),
            "minChars": FieldSchema(type="int", label="短于多少字不判", min=1, max=200),
            "windowSize": FieldSchema(type="int", label="判定看最近几条", min=2, max=50),
            "judgeProbability": FieldSchema(
                type="float", label="判定采样概率(%)", min=0, max=100,
                description="默认 100（每条都判）。调低可以省钱。",
            ),
            "dailyCallLimit": FieldSchema(type="int", label="单群每日判定上限", min=0, max=100000),
            "confidenceThreshold": FieldSchema(
                type="float", label="自信度下限", min=0, max=100,
                description="默认 0。调高会要求模型更有把握才接。",
            ),
            "onlyGroups": FieldSchema(type="string", label="仅限群(逗号分隔,空=全部)"),
            "denyGroups": FieldSchema(type="string", label="排除群(逗号分隔)"),
            "judgeModel": FieldSchema(
                type="string", label="判定专用模型",
                description="留空 = 沿用聊天主模型。建议用能关思考的小模型，否则 reasoning 会吃光输出预算。",
            ),
            "maxTokens": FieldSchema(type="int", label="判定输出上限(token)", min=8, max=2048),
            "timeoutMs": FieldSchema(type="int", label="判定超时(毫秒)", min=500, max=60000),
            "promptExtra": FieldSchema(type="string", label="追加判据", multiline=True),
            "blockOnJudgeFailure": FieldSchema(
                type="bool", label="判定失败就不接",
                description="默认开，与原版一致（宁可少接，不可乱接）。关掉则失败时照常回复。",
            ),
            "blockOnTimeout": FieldSchema(
                type="bool", label="判定超时也不接",
                description="默认关：判定模型慢的时候仍然让机器人可用。需要和原版完全一致就打开。",
            ),
        },
        default_settings={
            "allowPrivate": False,
            "enabled": False,
            "cooldownSec": 12,
            "minChars": 4,
            "windowSize": 14,
            "judgeProbability": 100.0,
            "dailyCallLimit": 400,
            "confidenceThreshold": 0.0,
            "onlyGroups": "",
            "denyGroups": "",
            "judgeModel": "",
            "maxTokens": 64,
            "timeoutMs": 6000,
            "promptExtra": "",
            "blockOnJudgeFailure": True,
            "blockOnTimeout": False,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        self._last_judge_at: dict = {}
        self._last_text: dict = {}
        self._daily: dict = {}
        self.history: list = []
        self.totals = {"calls": 0, "promptTokens": 0, "completionTokens": 0, "skipped": 0}

    # ── 规则闸门（可独立测试） ────────────────────────────────────────────
    def should_skip(self, chat_key: str, kind: str, chat_id: str, text: str,
                    now: Optional[float] = None) -> Optional[str]:
        """返回跳过原因；可以判定时返回 None。对应 JS 版打请求前的全部早退分支。"""
        if not self.get("enabled"):
            return "已关闭"
        text = str(text or "").strip()
        if not chat_key or not text:
            return "无内容"
        if kind == "private" and not self.get("allowPrivate"):
            return "私聊不判"
        if kind not in ("group", "private"):
            return "非群聊"
        if _list_has(self.get("denyGroups", ""), chat_id):
            return "在黑名单群"
        only = self.get("onlyGroups", "")
        if only and not _list_has(only, chat_id):
            return "不在白名单群"
        if text.startswith("[拍一拍]"):
            return "拍一拍"
        if len(text) < max(1, int(self.get("minChars", 4) or 4)):
            return "消息过短"
        if len(text) <= 12 and _MEDIA_ONLY.match(text):
            return "纯媒体"

        moment = time.time() if now is None else now
        cooldown = max(0, int(self.get("cooldownSec", 12) or 0))
        if moment - float(self._last_judge_at.get(chat_key) or 0) < cooldown:
            return "冷却中"
        if self._last_text.get(chat_key) == text:
            return "重复内容"

        daily_key = f"{today_key()}|{chat_key}"
        used = int(self._daily.get(daily_key) or 0)
        limit = int(self.get("dailyCallLimit", 400) or 0)
        if limit > 0 and used >= limit:
            return "已达当日上限"
        probability = min(100.0, max(0.0, float(self.get("judgeProbability", 100) or 0)))
        if probability < 100 and random.random() * 100 >= probability:
            return "未命中采样"
        return None

    def _reserve(self, chat_key: str, text: str, now: float) -> None:
        """先占冷却与去重名额再发请求：连发消息不会同时打出去。"""
        daily_key = f"{today_key()}|{chat_key}"
        self._last_judge_at[chat_key] = now
        self._last_text[chat_key] = text
        self._daily[daily_key] = int(self._daily.get(daily_key) or 0) + 1

    def remember(self, record: dict) -> None:
        try:
            self.history.append({"ts": time.time(), **record})
            if len(self.history) > HISTORY_MAX:
                del self.history[:len(self.history) - HISTORY_MAX]
        except Exception:
            pass

    def judge_messages(self, *, bot_name: str, self_nickname: str = "", persona_brief: str = "",
                       recent: Optional[list] = None, text: str = "", sender_label: str = "") -> list:
        """按 windowSize 组装判定提示词。"""
        history = list(recent or [])[-max(2, int(self.get("windowSize", 14) or 14)):]
        lines = []
        for item in history:
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                continue
            who = "我" if item.get("self") else clip(item.get("sender_name") or item.get("sender_id") or "群友", 12)
            lines.append(f"{who}：{clip(item.get('text'), 120)}")
        return build_judge_messages(
            bot_name=bot_name,
            self_nickname=self_nickname,
            persona_brief=clip(persona_brief, 220),
            recent_lines="\n".join(lines),
            last_line=f"{clip(sender_label or '群友', 12)}：{clip(text, 200)}",
            prompt_extra=str(self.get("promptExtra") or ""),
        )

    # ── 判定 ──────────────────────────────────────────────────────────────
    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        """判定要不要接话。返回 ``{"block": True}`` 表示不接。

        注意：本钩子是同步的，无法 await；判定请求走线程池（见 ``_ask_judge``），
        所以**不会阻塞事件循环**，但会占用最长 ``timeoutMs`` 的时间。
        """
        if not isinstance(message, dict) or message.get("notice"):
            return None
        if message.get("kind") != "group" or message.get("at"):
            return None
        try:
            text = str(message.get("text") or "")
            chat_key = f"{message.get('kind')}:{message.get('target')}"
            chat_id = str(message.get("target") or "")
            now = time.time()
            skip = self.should_skip(chat_key, message.get("kind"), chat_id, text, now)
            if skip:
                self.totals["skipped"] += 1
                return None

            self._reserve(chat_key, text, now)
            result = self._ask_judge(runtime, message, conversation, text)

            if result.get("skipped"):
                # 判定没做成（超时/出错/没有可用模型）：按设置决定保守方向
                self.remember({"chatKey": chat_key, "text": text, "respond": False,
                               "skipped": result["skipped"]})
                if result.get("block"):
                    return {"block": True}
                return None

            passed = bool(result.get("respond")) and float(result.get("confidence") or 0) >= float(
                self.get("confidenceThreshold", 0) or 0)
            self.remember({"chatKey": chat_key, "text": text, "respond": passed,
                           "confidence": result.get("confidence"), "why": result.get("why"),
                           "ms": result.get("ms")})
            return None if passed else {"block": True}
        except Exception:
            # 判定自身出问题不该让机器人不响应
            return None

    async def on_pre_ai_async(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        """判定要不要接话。返回 ``{"block": True}`` 表示不接。

        运行时异步调用，遵守 timeoutMs，不能在已运行的事件循环里 asyncio.run。
        """
        if not isinstance(message, dict) or message.get("notice"):
            return None
        if message.get("kind") != "group" or message.get("at"):
            return None
        try:
            text = str(message.get("text") or "")
            chat_key = f"{message.get('kind')}:{message.get('target')}"
            chat_id = str(message.get("target") or "")
            now = time.time()
            skip = self.should_skip(chat_key, message.get("kind"), chat_id, text, now)
            if skip:
                self.totals["skipped"] += 1
                return None

            self._reserve(chat_key, text, now)
            result = await self._ask_judge_async(runtime, message, conversation, text)

            if result.get("skipped"):
                # 判定没做成（超时/出错/没有可用模型）：按设置决定保守方向
                self.remember({"chatKey": chat_key, "text": text, "respond": False,
                               "skipped": result["skipped"]})
                if result.get("block"):
                    return {"block": True}
                return None

            passed = bool(result.get("respond")) and float(result.get("confidence") or 0) >= float(
                self.get("confidenceThreshold", 0) or 0)
            self.remember({"chatKey": chat_key, "text": text, "respond": passed,
                           "confidence": result.get("confidence"), "why": result.get("why"),
                           "ms": result.get("ms")})
            return None if passed else {"block": True}
        except Exception:
            # 判定自身出问题不该让机器人不响应
            return None

    def _ask_judge(self, runtime, message: dict, conversation: list, text: str) -> dict:
        """发一次判定请求。返回 ``{respond, confidence, why, ms}`` 或 ``{skipped, block}``。"""
        ai = getattr(runtime, "ai", None)
        if ai is None or not getattr(ai, "configured", False):
            return {"skipped": "没有可用的模型配置",
                    "block": bool(self.get("blockOnJudgeFailure", True))}
        gate = _early_finish(self._manager)
        if gate is not None and not gate.allow_extra(runtime, message, "judge"):
            # 上一轮已经把话交付完、没有遗留动作：这一次判定调用也就没必要花了。
            # 只影响"主动接话"，@ 机器人的消息在这个钩子之前就返回了，永远不会被跳过。
            return {"skipped": "本轮已交付完毕，跳过接话判定（提前收尾）", "block": True}

        messages = self.judge_messages_for(runtime, message, conversation, text)
        timeout_s = max(0.5, float(self.get("timeoutMs", 6000) or 6000) / 1000.0)
        started = time.time()

        try:
            # on_pre_ai 是同步钩子，无法 await。这里用 asyncio.run 在**独立事件循环**里
            # 把判定请求跑完：AIEngine 按当前事件循环缓存 AsyncClient
            # （ai_engine._get_client_for_current_loop），所以换循环会新建客户端，不会
            # 跨循环复用。这样既不需要线程，也不会往事件循环上挂并发回调。
            raw = asyncio.run(asyncio.wait_for(self._judge_async(ai, messages), timeout_s))
        except Exception as exc:
            timed_out = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in str(exc).lower()
            if timed_out:
                return {"skipped": "判定超时",
                        "block": bool(self.get("blockOnTimeout", False))}
            return {"skipped": f"判定失败：{clip(exc, 60)}",
                    "block": bool(self.get("blockOnJudgeFailure", True))}

        parsed = parse_judge(raw)
        self.totals["calls"] += 1
        if not parsed:
            return {"skipped": "输出无法解析", "block": bool(self.get("blockOnJudgeFailure", True)),
                    "ms": int((time.time() - started) * 1000)}
        parsed["ms"] = int((time.time() - started) * 1000)
        return parsed

    async def _ask_judge_async(self, runtime, message: dict, conversation: list, text: str) -> dict:
        """发一次判定请求。返回 ``{respond, confidence, why, ms}`` 或 ``{skipped, block}``。"""
        ai = getattr(runtime, "ai", None)
        if ai is None or not getattr(ai, "configured", False):
            return {"skipped": "没有可用的模型配置",
                    "block": bool(self.get("blockOnJudgeFailure", True))}
        gate = _early_finish(self._manager)
        if gate is not None and not gate.allow_extra(runtime, message, "judge"):
            return {"skipped": "本轮已交付完毕，跳过接话判定（提前收尾）", "block": True}

        messages = self.judge_messages_for(runtime, message, conversation, text)
        timeout_s = max(0.5, float(self.get("timeoutMs", 6000) or 6000) / 1000.0)
        started = time.time()

        try:
            lock = getattr(runtime, 'ai_lock', None)
            if lock is None:
                raw = await asyncio.wait_for(self._judge_async(ai, messages), timeout_s)
            else:
                async with lock:
                    raw = await asyncio.wait_for(self._judge_async(ai, messages), timeout_s)
        except Exception as exc:
            timed_out = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in str(exc).lower()
            if timed_out:
                return {"skipped": "判定超时",
                        "block": bool(self.get("blockOnTimeout", False))}
            return {"skipped": f"判定失败：{clip(exc, 60)}",
                    "block": bool(self.get("blockOnJudgeFailure", True))}

        parsed = parse_judge(raw)
        self.totals["calls"] += 1
        if not parsed:
            return {"skipped": "输出无法解析", "block": bool(self.get("blockOnJudgeFailure", True)),
                    "ms": int((time.time() - started) * 1000)}
        parsed["ms"] = int((time.time() - started) * 1000)
        return parsed

    async def _judge_async(self, ai, messages: list) -> str:
        """用 AIEngine.chat 发判定请求（temperature=0、独立 max_tokens、可选独立模型）。"""
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        user = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
        # "判定专用模型"以前是空设置（写了不读）。判定用便宜小模型能省额度，
        # 所以这里真的把它传给引擎；留空则沿用主模型。
        judge_model = str(self.get("judgeModel", "") or "").strip()
        kwargs = {}
        if judge_model:
            kwargs["model"] = judge_model
        return await ai.chat(
            [{"role": "user", "content": user}],
            system_prompt=system,
            temperature=0.0,
            max_tokens=max(8, int(self.get("maxTokens", 64) or 64)),
            **kwargs,
        )

    def judge_messages_for(self, runtime, message: dict, conversation: list, text: str) -> list:
        """组装判定提示词（供测试与排障直接调用）。"""
        bot_name = "机器人"
        try:
            style = getattr(runtime, "style", None)
            if style is not None:
                bot_name = str(getattr(style.get(), "name", "") or bot_name)
        except Exception:
            pass

        recent = list(getattr(runtime, "recent_messages", []) or [])
        if not recent and conversation:
            for item in conversation:
                if isinstance(item, dict):
                    recent.append({"text": item.get("content"),
                                   "self": item.get("role") == "assistant",
                                   "sender_name": "我" if item.get("role") == "assistant" else ""})
        return self.judge_messages(
            bot_name=bot_name, recent=recent, text=text,
            sender_label=str(message.get("sender_name") or message.get("sender") or ""),
        )
