"""poke_counter.py - 戳一戳统计与回复。

移植自 JS 插件 ``poke-counter``（QQ Agent，MIT）。原版包装宿主的
``globalThis.wuwaEmojiPokeSend``；本项目的对应入口是**戳一戳通知事件**
（``message["notice"] == "poke"``，见 src/qq_transports.py 的 onebot_notice）。

功能：
1. 每次被戳，今日计数 +1，累计持久化到插件状态文件；
2. 对一个数字执行预设运算（默认 ``n + 1``），结果可播报；
3. 按概率从预设文本里随机抽一条回复，支持 ``{n}``（当前数值）与 ``{count}``
   （今日被戳次数）占位符。

与 JS 版的两处差异（安全与降级）：
- **表达式求值**：JS 用 ``new Function`` 执行任意 JS；Python 版改为**安全的算术
  求值器**（只允许数字、``n``、四则运算与括号、少量数学函数），非法表达式保留原值。
  这样用户填错的表达式不会变成任意代码执行。
- **发送方式**：JS 版用 ``sender.sendTextBatch`` 主动发；本项目通过
  ``on_ingest`` 返回字符串走发送管道（同样不经过模型），因此冷却与概率逻辑不变。
"""

from __future__ import annotations

import ast
import json
import math
import operator
import os
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

# 安全表达式求值允许的运算符与函数
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
}
# 幂运算防炸：指数绝对值上限，避免 9**9**9 这类把机器算死
_MAX_POW = 512
_MAX_ABS_RESULT = 1e15


class ExpressionError(ValueError):
    """表达式非法（会保留原数值，不影响计数）。"""


def _eval_node(node, n):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, n)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ExpressionError("只允许数字常量")
        return node.value
    if isinstance(node, ast.Name):
        if node.id != "n":
            raise ExpressionError("只允许变量 n")
        return n
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError("不支持的运算符")
        left = _eval_node(node.left, n)
        right = _eval_node(node.right, n)
        if op is operator.pow and abs(right) > _MAX_POW:
            raise ExpressionError("指数过大")
        if op in (operator.truediv, operator.floordiv, operator.mod) and right == 0:
            raise ExpressionError("除数为零")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError("不支持的一元运算符")
        return op(_eval_node(node.operand, n))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise ExpressionError("不允许的函数调用")
        if node.keywords:
            raise ExpressionError("不允许关键字参数")
        return _ALLOWED_FUNCS[node.func.id](*[_eval_node(arg, n) for arg in node.args])
    raise ExpressionError(f"不允许的表达式节点：{type(node).__name__}")


def apply_expression(expression: str, n: float) -> float:
    """按表达式算出新数值；任何问题都返回原数值（绝不抛给调用方）。

    支持形如 ``n + 1``、``n * 2 + 1``、``round(n / 2)``、``min(n + 5, 100)`` 的写法。
    """
    text = str(expression or "").strip() or "n + 1"
    try:
        tree = ast.parse(text, mode="eval")
        result = _eval_node(tree, n)
    except ExpressionError:
        return n
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return n
    except RecursionError:
        return n
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        return n
    try:
        result = float(result)
    except (OverflowError, ValueError):
        return n
    if not math.isfinite(result) or abs(result) > _MAX_ABS_RESULT:
        return n
    return int(result) if float(result).is_integer() else result


def today_key(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(timezone(timedelta(hours=8)))
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"


def build_reply_text(texts: str, n, count: int, show_number: bool = True,
                     show_daily: bool = True, picker=None) -> str:
    """按 JS 版规则组装回复文本：随机抽一行 + 占位符替换 + 可选的自动追加。"""
    lines = [line.strip() for line in str(texts or "").splitlines() if line.strip()]
    choose = picker or random.choice
    chosen = choose(lines) if lines else ""

    if chosen:
        chosen = chosen.replace("{n}", str(n)).replace("{count}", str(count))

    extra = []
    mentions_number = any("{n}" in line for line in lines)
    mentions_count = any("{count}" in line for line in lines)
    if show_number and not mentions_number:
        extra.append(f"当前数值：{n}")
    if show_daily and not mentions_count:
        extra.append(f"今天被戳 {count} 次了")

    parts = []
    if chosen:
        parts.append(chosen)
    if extra:
        parts.append("，".join(extra))
    return "，".join(parts) or f"今天被戳 {count} 次了，当前数值：{n}"


class PokeCounter(PluginBase):
    META = PluginMeta(
        id="poke_counter",
        name="戳一戳统计",
        description=(
            "统计每天被戳一戳的次数；被戳时按概率回复预设文本，并对一个数字执行预设运算后播报结果。"
            "普通QQ支持真实戳一戳；官方接入用 /戳一戳 指令。/戳一戳统计 查询次数；回复不经模型。"
        ),
        category="utility",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "replyProbability": FieldSchema(
                type="float", label="回复概率", min=0.0, max=1.0,
                description="头像戳一戳：0~1，0.3 = 三成概率回复；指令会直接回复，冷却中仍反馈计数。",
            ),
            "cooldownMs": FieldSchema(
                type="int", label="冷却时间（毫秒）", min=0, max=600000,
                description="同一会话两次回复的最小间隔，防止连续戳时刷屏。0 = 不冷却。",
            ),
            "replyTexts": FieldSchema(
                type="string", label="预设回复文本", multiline=True,
                description="每行一条，被戳时随机抽一条。占位符：{n} = 当前数值，{count} = 今日被戳次数。",
            ),
            "showNumber": FieldSchema(
                type="bool", label="回复中带上当前数值",
                description="文本里已写 {n} 时以它为准；否则在回复末尾自动追加。",
            ),
            "showDailyCount": FieldSchema(
                type="bool", label="回复中带上今日次数",
                description="文本里已写 {count} 时以它为准；否则自动追加。",
            ),
            "expression": FieldSchema(
                type="string", label="数字运算表达式",
                description="每次被戳对当前数值 n 执行的运算，例：n + 1、n * 2 + 1、round(n / 2)。"
                            "只支持数字、n、四则运算、括号与 abs/round/min/max/floor/ceil/sqrt。",
            ),
            "initialValue": FieldSchema(type="float", label="初始数值"),
            "perGroup": FieldSchema(
                type="bool", label="按群独立统计",
                description="关闭：所有群共用一份计数与数值；开启：每个群各自计数、各自维护数值。",
            ),
        },
        default_settings={
            "enabled": False,
            "replyProbability": 0.3,
            "cooldownMs": 3000,
            "replyTexts": "别戳了啦\n戳够了没有\n今天第 {count} 次了哦\n再戳我要生气了 {n}",
            "showNumber": True,
            "showDailyCount": True,
            "expression": "n + 1",
            "initialValue": 0,
            "perGroup": False,
        },
    )

    STATE_FILE = "poke_counter.json"

    def __init__(self, manager):
        super().__init__(manager)
        # chatKey -> 上次回复时间戳
        self._last_reply_at: dict = {}
        self._data = None

    # ── 数据读写 ──────────────────────────────────────────────────────────
    @staticmethod
    def _empty_block(initial: float = 0) -> dict:
        return {"daily": {}, "total": 0, "number": initial}

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        initial = float(self.get("initialValue", 0) or 0)
        raw = self.load_state(self.STATE_FILE, {})
        if not isinstance(raw, dict):
            raw = {}
        # 旧版把 {daily,total,number} 直接写在顶层 → 迁到 global
        if raw.get("daily") and not raw.get("global"):
            data = {
                "global": {"daily": raw.get("daily") or {}, "total": int(raw.get("total") or 0),
                           "number": raw.get("number") or initial},
                "groups": raw.get("groups") if isinstance(raw.get("groups"), dict) else {},
            }
        else:
            global_block = raw.get("global")
            data = {
                "global": global_block if isinstance(global_block, dict) else self._empty_block(initial),
                "groups": raw.get("groups") if isinstance(raw.get("groups"), dict) else {},
            }
        # 补齐缺失字段，避免旧数据缺键导致 KeyError
        for block in [data["global"], *data["groups"].values()]:
            if not isinstance(block, dict):
                continue
            block.setdefault("daily", {})
            block.setdefault("total", 0)
            block.setdefault("number", initial)
        self._data = data
        return data

    def _save(self) -> None:
        try:
            # save_state 内部已是"写文件"语义；这里再保证不因异常影响计数
            self.save_state(self.STATE_FILE, self._data or {})
        except Exception:
            pass

    def _block_for(self, data: dict, chat_key: str) -> dict:
        if not self.get("perGroup"):
            return data["global"]
        return data["groups"].setdefault(chat_key, self._empty_block(float(self.get("initialValue", 0) or 0)))

    # ── 主逻辑 ────────────────────────────────────────────────────────────
    @staticmethod
    def command(message):
        if not isinstance(message, dict) or message.get('notice'):
            return ''
        text = str(message.get('text') or '').strip()
        return {'/戳一戳': 'poke', '/戳戳': 'poke', '/戳一戳统计': 'stats'}.get(text, '')

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        command = self.command(message)
        notice = isinstance(message, dict) and message.get('notice') == 'poke'
        # 官方模式没有 poke 事件：qq_app 会把"@ 了机器人但没说话"标成 _soft_poke，
        # 这里也照戳处理（统计 + 按概率回模板），否则官方模式永远统计不到。
        soft = isinstance(message, dict) and bool(message.get('_soft_poke'))
        if not command and not (notice or soft):
            return None
        if not self.get("enabled"):
            return '戳一戳统计尚未启用，请在插件中心启用。' if command else ''
        if notice and not soft:
            details = message.get('notice_data') or {}
            if not isinstance(details, dict):
                return ''
            own = str(getattr(getattr(runtime, 'onebot', None), 'self_id', '') or message.get('self_id') or '')
            if (runtime is not None and getattr(runtime, 'mode', '') != 'onebot') or not own:
                return ''
            if str(details.get('poked') or '') != own or str(details.get('poker') or '') in ('', own):
                return ''
        try:
            if command == 'stats':
                block = self._block_for(self._load(), f"{message.get('kind')}:{message.get('target')}")
                return f"今天被戳 {block['daily'].get(today_key(), 0)} 次，累计 {block['total']} 次，当前数值：{block['number']}。"
            return self._handle_poke(message, command=bool(command))
        except Exception:
            return '戳一戳统计暂时失败，请检查插件设置。' if command else ''

    def _handle_poke(self, message: dict, command=False) -> Optional[str]:
        data = self._load()
        chat_key = f"{message.get('kind')}:{message.get('target')}"
        block = self._block_for(data, chat_key)
        today = today_key()

        block["daily"][today] = int(block["daily"].get(today, 0)) + 1
        block["total"] = int(block.get("total", 0)) + 1
        count_today = block["daily"][today]
        block["number"] = apply_expression(self.get("expression", "n + 1"), float(block.get("number") or 0))
        self._save()

        now = time.time()
        cooldown = max(0, int(self.get("cooldownMs", 3000) or 0))
        last_at = float(self._last_reply_at.get(chat_key) or 0)
        in_cooldown = cooldown > 0 and (now - last_at) * 1000 < cooldown
        probability = min(1.0, max(0.0, float(self.get("replyProbability", 0.3) or 0)))
        if in_cooldown:
            # 冷却中：命令回"已记录"，普通戳**不吞事件**（返回 None 让后续插件/AI 决定），
            # 以前返回 '' 表示"已处理不回"，等于把整条戳事件吞掉 —— 真机表现是"戳了没反应"。
            return f"已记录：今天被戳 {count_today} 次，当前数值：{block['number']}（回复冷却中）。" if command else None
        if not command and random.random() >= probability:
            # 统计已记录；"要不要回、怎么回"交给 probability_reply / AI 流程。
            return None

        text = build_reply_text(
            self.get("replyTexts", ""),
            block["number"],
            count_today,
            show_number=bool(self.get("showNumber", True)),
            show_daily=bool(self.get("showDailyCount", True)),
        )
        self._last_reply_at[chat_key] = now
        return text
