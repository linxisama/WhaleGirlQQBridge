"""plugin_core.outbound — 出站管道（规格 §5）。

串行；priority 小的先执行；返回 None 即取消并短路；改写结果不再进管道（防回灌）；
中间件必须有 send_message，否则跳过并审计 outbound-denied。
"""

from __future__ import annotations

from .manifest import capability_index
from .sdk import Draft


class OutboundPipeline:
    def __init__(self, host) -> None:
        self.host = host
        self.audit = host.audit
        self.log = host.log
        self.passes = {}

    def candidates(self) -> list:
        """(priority, 声明序, 下标, plugin, fn)，priority 升序。"""
        rows = []
        for plugin in self.host.enabled_plugins():
            if not plugin.declarations.outbound:
                continue
            priority = self._priority(plugin.manifest)
            declared_index = capability_index(plugin.manifest, "outbound")
            for index, fn in enumerate(plugin.declarations.outbound):
                rows.append((priority, declared_index, index, plugin, fn))
        rows.sort(key=lambda row: (row[0], row[1], row[3].id, row[2]))
        return [(row[0], row[3], row[4]) for row in rows]

    @staticmethod
    def _priority(manifest) -> int:
        values = [
            cap.get("priority", 0)
            for cap in manifest.get("capabilities", ())
            if cap.get("stage") == "outbound"
        ]
        return min(values) if values else 0

    def run(self, draft: Draft) -> dict:
        trace = []
        state = draft
        candidates = self.candidates()
        for priority, plugin, fn in candidates:
            if "send_message" not in plugin.permissions:
                self.audit.write(
                    {
                        "event": "outbound-denied",
                        "plugin": plugin.id,
                        "action": "outbound",
                        "session": state.conversation,
                        "code": "denied",
                        "permission": "send_message",
                    }
                )
                trace.append({"plugin": plugin.id, "priority": priority, "status": "denied"})
                continue
            ctx = self.host.build_context(plugin, self.host.current_message)
            self.passes[plugin.id] = self.passes.get(plugin.id, 0) + 1
            try:
                result = fn(ctx, state)
            except Exception as exc:
                self.audit.write(
                    {
                        "event": "outbound-failed",
                        "plugin": plugin.id,
                        "code": "outbound-failed",
                        "detail": "%s: %s" % (type(exc).__name__, exc),
                    }
                )
                self.log.error("插件 %s 出站中间件抛异常：%s" % (plugin.id, exc))
                trace.append({"plugin": plugin.id, "priority": priority, "status": "failed"})
                continue
            if result is None:
                self.audit.write(
                    {
                        "event": "outbound-cancelled",
                        "plugin": plugin.id,
                        "session": state.conversation,
                        "code": "outbound-cancelled",
                        "text": state.text,
                    }
                )
                trace.append({"plugin": plugin.id, "priority": priority, "status": "cancelled"})
                return {"draft": None, "cancelled": True, "by": plugin.id, "trace": trace, "passes": dict(self.passes)}
            if isinstance(result, Draft):
                state = result
            elif isinstance(result, dict):
                state = Draft(
                    result.get("conversation", state.conversation),
                    result.get("text", state.text),
                    result.get("media", state.media),
                    result.get("plugin", plugin.id),
                )
            else:
                self.audit.write(
                    {
                        "event": "outbound-failed",
                        "plugin": plugin.id,
                        "code": "outbound-failed",
                        "detail": "中间件必须返回 Draft 或 None，收到 %r" % (type(result).__name__,),
                    }
                )
                trace.append({"plugin": plugin.id, "priority": priority, "status": "bad-return"})
                continue
            trace.append(
                {"plugin": plugin.id, "priority": priority, "status": "rewritten", "text": state.text}
            )
            # 关键：改写结果不再放回候选列表 —— 一条消息只走一遍（防回灌）
        return {"draft": state, "cancelled": False, "by": None, "trace": trace, "passes": dict(self.passes)}
