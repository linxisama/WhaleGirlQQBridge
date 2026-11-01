"""plugin_core.actions — 动作执行：顺序、序列化、单个失败不中断（规格 §3/§8.6）。"""

from __future__ import annotations

from .api import KernelError
from .sdk import Action


class ActionExecutor:
    def __init__(self, host) -> None:
        self.host = host
        self.gate = host.gate
        self.audit = host.audit
        self.log = host.log

    def run(self, plugin, actions, *, session_id=None) -> dict:
        results = []
        sends = []
        if actions is None:
            actions = []
        if not isinstance(actions, (list, tuple)):
            self.audit.write(
                {
                    "event": "action-invalid",
                    "plugin": plugin.id,
                    "code": "bad-action",
                    "detail": "插件返回的不是动作列表：%r" % (actions,),
                }
            )
            return {"plugin": plugin.id, "results": results, "sends": sends}

        for raw in actions:
            try:
                action = Action.from_dict(raw)
            except KernelError as exc:
                results.append({"action": "?", "status": "bad-action", "detail": str(exc)})
                self.audit.write(
                    {"event": "action-invalid", "plugin": plugin.id, "code": "bad-action", "detail": str(exc)}
                )
                continue

            serialized = action.as_dict()
            try:
                allowed = self.gate.check(
                    plugin.id, plugin.permissions, action.kind, session=session_id
                )
            except KeyError:
                results.append({"action": action.kind, "status": "bad-action"})
                self.audit.write(
                    {
                        "event": "action-invalid",
                        "plugin": plugin.id,
                        "code": "bad-action",
                        "action": serialized,
                        "detail": "未知动作 %r" % (action.kind,),
                    }
                )
                continue
            if not allowed:
                results.append({"action": action.kind, "status": "denied"})
                continue

            try:
                detail = self._apply(plugin, action, session_id, sends)
            except Exception as exc:
                results.append(
                    {"action": action.kind, "status": "failed", "detail": "%s: %s" % (type(exc).__name__, exc)}
                )
                self.audit.write(
                    {
                        "event": "action-failed",
                        "plugin": plugin.id,
                        "code": "action-failed",
                        "action": serialized,
                        "detail": "%s: %s" % (type(exc).__name__, exc),
                    }
                )
                self.log.error("插件 %s 动作 %s 失败：%s" % (plugin.id, action.kind, exc))
                continue

            results.append({"action": action.kind, "status": "ok", "detail": detail})
            self.audit.write(
                {
                    "event": "action",
                    "plugin": plugin.id,
                    "action": serialized,
                    "status": "ok",
                }
            )
        return {"plugin": plugin.id, "results": results, "sends": sends}

    # ------------------------------------------------------------------ 各类动作
    def _apply(self, plugin, action: Action, session_id, sends):
        kind = action.kind
        if kind == "send_text":
            conversation = action.args.get("conversation") or session_id
            text = action.args.get("text")
            if not isinstance(text, str):
                raise KernelError("send_text.text 必须是字符串")
            sends.append(
                {"conversation": str(conversation), "text": text, "media": [], "plugin": plugin.id}
            )
            return "queued"
        if kind == "send_media":
            conversation = action.args.get("conversation") or session_id
            media = action.args.get("media") or []
            if not isinstance(media, list):
                raise KernelError("send_media.media 必须是数组")
            sends.append(
                {"conversation": str(conversation), "text": "", "media": list(media), "plugin": plugin.id}
            )
            return "queued"
        if kind == "store_set":
            plugin.api._kv.set(action.args["key"], action.args.get("value"))
            return "stored"
        if kind == "store_del":
            return "deleted" if plugin.api._kv.delete(action.args["key"]) else "absent"
        if kind == "schedule":
            self.host.schedule(
                action.args.get("delay", 0), action.args.get("event"), action.args.get("payload") or {}
            )
            return "scheduled"
        if kind == "emit":
            return self.host.emit_event(
                action.args.get("event"), action.args.get("payload") or {}, depth=1
            )
        if kind == "noop":
            return "noop"
        raise KernelError("未知动作 %r" % (kind,))
