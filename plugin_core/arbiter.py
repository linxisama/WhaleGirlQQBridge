"""plugin_core.arbiter — 竞标 / 仲裁 / 合成（规格 §4）。

七条规则逐条落地：竞标阶段绝不调模型；silent 与 main 同场比 priority；
main priority 降序、同优先级按 manifest 声明序；attach 只在 main 胜出后串行；
extra 独立发送但仍过出站管道；无 main 无 extra 什么都不发；只有胜者调 generate。
"""

from __future__ import annotations

import time

from .api import DROPPED_REASONS, KernelError, coerce_bid
from .manifest import capability_index
from .sdk import Draft

INBOUND_STAGES = ("inbound", "generate")
DEFAULT_CAP_INDEX = 10**6
REASON_LABELS = {
    "priority-low": "优先级低",
    "no-trigger": "未命中触发条件",
    "requires-main": "缺少主体",
    "silent-over-main": "沉默优先",
}


class BidEntry:
    """一条原始竞标：插件 + Bid + 声明序下标（capabilities 内的位置）+ 插件内竞标序。"""

    __slots__ = ("plugin", "bid", "cap_index", "order")

    def __init__(self, plugin, bid, cap_index: int, order: int) -> None:
        self.plugin = plugin
        self.bid = bid
        self.cap_index = cap_index
        self.order = order

    def key(self) -> tuple:
        # 契约 §5 规则 2 / D7：priority 降序（取负）→ 同优先级比 manifest 声明序
        # （即 plugin.json 里 capabilities 的下标 cap_index，不按加载序/目录名）
        # → id 字典序兜底 → 同一插件内竞标序，保证排序稳定可复现。
        return (-int(self.bid.priority), self.cap_index, self.plugin.id, self.order)

    def __repr__(self) -> str:
        return "BidEntry(%s, %s, %d)" % (self.plugin.id, self.bid.produces, self.bid.priority)


def _cap_index(manifest, produces) -> int:
    for stage in INBOUND_STAGES:
        index = capability_index(manifest, stage, produces)
        if index < DEFAULT_CAP_INDEX:
            return index
    return DEFAULT_CAP_INDEX


def reason_label(reason: str) -> str:
    return REASON_LABELS.get(reason, reason)


class Arbiter:
    def __init__(self, host) -> None:
        self.host = host
        self.audit = host.audit
        self.log = host.log
        self.outbound = host.outbound
        self.executor = host.action_executor

    # --- 主流程 -----------------------------------------------------
    def run(self, message) -> dict:
        started = time.perf_counter()
        record = {
            "event": "arbitration",
            "message_id": message["message"]["id"],
            "session": message["session"]["key"],
            "bids": [],
            "silent_winner": None,
            "winner": None,
            "dropped": [],
            "generated": [],
            "attached": [],
            "extra": [],
            "sent": [],
            "duration_ms": 0,
        }
        entries, misses = self._collect(message)
        record["bids"] = [
            {
                "plugin": entry.plugin.id,
                "produces": entry.bid.produces,
                "priority": entry.bid.priority,
                "reason": entry.bid.reason,
            }
            for entry in entries
        ]
        for plugin_id, reason in misses:
            record["dropped"].append(
                {
                    "plugin": plugin_id,
                    "produces": None,
                    "priority": None,
                    "reason": reason,
                    "label": REASON_LABELS.get(reason, reason),
                }
            )

        mains = sorted((e for e in entries if e.bid.produces == "main"), key=lambda e: e.key())
        silents = sorted((e for e in entries if e.bid.produces == "silent"), key=lambda e: e.key())
        attaches = sorted((e for e in entries if e.bid.produces == "attach"), key=lambda e: e.key())
        extras = sorted((e for e in entries if e.bid.produces == "extra"), key=lambda e: e.key())

        main_winner = None
        silent_winner = None
        if silents and (not mains or silents[0].bid.priority >= mains[0].bid.priority):
            silent_winner = silents[0]
            record["silent_winner"] = self._winner(silent_winner)
            for entry in silents[1:]:
                self._drop(record, entry, "priority-low", silent_winner.bid.priority)
            for entry in mains:
                self._drop(record, entry, "silent-over-main", silent_winner.bid.priority)
        elif mains:
            main_winner = mains[0]
            record["winner"] = self._winner(main_winner)
            for entry in mains[1:]:
                self._drop(record, entry, "priority-low", main_winner.bid.priority)
            for entry in silents:
                self._drop(record, entry, "priority-low", main_winner.bid.priority)

        sends = []
        if main_winner is not None:
            record["generated"].append(main_winner.plugin.id)
            sends.extend(self._generate(main_winner, message, moment="generate"))

        for entry in attaches:
            if main_winner is None:
                self._drop(record, entry, "requires-main")
                continue
            record["attached"].append(entry.plugin.id)
            sends.extend(self._generate(entry, message, moment="attach"))

        for entry in extras:
            record["extra"].append(entry.plugin.id)
            sends.extend(self._generate(entry, message, moment="extra"))

        record["sent"] = self.host.deliver(sends)

        if (
            not record["sent"]
            and main_winner is None
            and silent_winner is None
            and not extras
            and self.host.default_persona
        ):
            self._default_persona(message, record)

        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        self.audit.write(record)
        self.log.info(
            "仲裁完成 message=%s winner=%s silent=%s sent=%d 用时 %sms"
            % (
                record["message_id"],
                (record["winner"] or {}).get("plugin") if record["winner"] else "-",
                (record["silent_winner"] or {}).get("plugin") if record["silent_winner"] else "-",
                len(record["sent"]),
                record["duration_ms"],
            )
        )
        return record

    # --- 竞标 -------------------------------------------------------
    def _collect(self, message) -> tuple:
        entries = []
        misses = []
        for plugin in self.host.enabled_plugins():
            inbound = plugin.declarations.inbound
            if not inbound:
                continue
            caps = [
                cap
                for cap in (plugin.manifest.get("capabilities") or ())
                if cap.get("stage") in INBOUND_STAGES
            ]
            if not caps:
                self.audit.write(
                    {
                        "event": "bid-skipped",
                        "plugin": plugin.id,
                        "code": "skipped",
                        "reason": "no-capability",
                    }
                )
                continue
            if "read_message" not in plugin.permissions:
                self.audit.write(
                    {
                        "event": "bid-skipped",
                        "plugin": plugin.id,
                        "code": "skipped",
                        "reason": "no-read-permission",
                        "message_id": message["message"]["id"],
                    }
                )
                self.log.warn("插件 %s 未声明 read_message，不进入竞标" % plugin.id)
                continue

            ctx = self.host.build_context(plugin, message)
            got = 0
            for fn in inbound:
                try:
                    plugin.lifecycle.enter("message")
                    raw = fn(ctx)
                except Exception as exc:
                    self.host._hook_failed(plugin, "inbound", exc)
                    continue
                if raw is None:
                    continue
                items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
                for item in items:
                    try:
                        one = coerce_bid(item, plugin.id)
                    except KernelError as exc:
                        self.audit.write(
                            {
                                "event": "bid-invalid",
                                "plugin": plugin.id,
                                "code": "bad-bid",
                                "detail": str(exc),
                            }
                        )
                        continue
                    if one.produces in ("main", "attach", "extra") and one.generate is None:
                        self.audit.write(
                            {
                                "event": "bid-invalid",
                                "plugin": plugin.id,
                                "code": "bad-bid",
                                "detail": "produces=%s 必须给 generate" % one.produces,
                            }
                        )
                        continue
                    entries.append(
                        BidEntry(plugin, one, _cap_index(plugin.manifest, one.produces), got)
                    )
                    got += 1
            if got == 0:
                misses.append((plugin.id, "no-trigger"))
                self.audit.write(
                    {
                        "event": "bid-dropped",
                        "plugin": plugin.id,
                        "code": "no-trigger",
                        "reason": "no-trigger",
                        "message_id": message["message"]["id"],
                    }
                )
        return entries, misses

    # --- 生成 / 动作 / 发送 -----------------------------------------
    def _generate(self, entry, message, *, moment: str) -> list:
        plugin = entry.plugin
        ctx = self.host.build_context(plugin, message)
        try:
            plugin.lifecycle.enter("message")
            raw = entry.bid.generate(ctx)
        except Exception as exc:
            event = {
                "generate": "hook-failed",
                "attach": "attach-failed",
                "extra": "extra-failed",
            }[moment]
            self.audit.write(
                {
                    "event": event,
                    "plugin": plugin.id,
                    "moment": moment,
                    "code": event,
                    "detail": "%s: %s" % (type(exc).__name__, exc),
                    "message_id": message["message"]["id"],
                }
            )
            self.log.error(
                "插件 %s 在 %s 阶段抛异常：%s: %s" % (plugin.id, moment, type(exc).__name__, exc)
            )
            return []
        result = self.executor.run(plugin, raw, session_id=message["session"]["key"])
        return result["sends"]

    def _default_persona(self, message, record) -> None:
        """无插件应答时的兜底：host 内置一句话系统提示 + mock 模型。"""
        messages = self.host.build_prompt(message)
        self.log.info(
            "组 prompt：%s" % (messages,)
        )
        text = self.host.model.chat(messages, max_chars=None)
        self.log.info("调模型：%r" % (text,))
        sends = [
            {
                "conversation": message["session"]["key"],
                "text": str(text),
                "media": [],
                "plugin": "host:default-persona",
            }
        ]
        record["fallback"] = "default-persona"
        record["sent"].extend(self.host.deliver(sends))

    # --- 记录辅助 ---------------------------------------------------
    def _winner(self, entry) -> dict:
        return {
            "plugin": entry.plugin.id,
            "produces": entry.bid.produces,
            "priority": entry.bid.priority,
            "reason": entry.bid.reason,
        }

    def _drop(self, record, entry, reason: str, winner_priority=None) -> dict:
        if reason not in DROPPED_REASONS:
            raise KernelError("未知落败原因 %r" % (reason,))
        item = {
            "plugin": entry.plugin.id,
            "produces": entry.bid.produces,
            "priority": entry.bid.priority,
            "reason": reason,
            "label": reason_label(reason),
        }
        if winner_priority is not None:
            item["winner_priority"] = winner_priority
        record["dropped"].append(item)
        self.audit.write(
            {
                "event": "bid-dropped",
                "plugin": entry.plugin.id,
                "produces": entry.bid.produces,
                "priority": entry.bid.priority,
                "code": reason,
                "reason": reason,
                "winner_priority": winner_priority,
            }
        )
        return item


def draft_of(send) -> Draft:
    """把执行器产出的 send 字典转回 Draft（测试与 demo 便于复用）。"""
    return Draft(
        send.get("conversation") or "",
        send.get("text") or "",
        tuple(send.get("media") or ()),
        send.get("plugin") or "",
    )
