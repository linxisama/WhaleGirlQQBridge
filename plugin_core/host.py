"""plugin_core.host — Host：把 loader / arbiter / actions / outbound / store 拼成主体（规格 §8）。

生命周期十个时刻在 Host 上真实落地：preLaunch → load → enable →（message/command/event）
→ disable → unload；error 由插件抛异常后回调。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import MappingProxyType

from .actions import ActionExecutor
from .api import (
    API_VERSION,
    HOST_VERSION,
    SEGMENT_TYPES,
    FreezeViolation,
    KernelError,
    PermissionDenied,
)
from .arbiter import Arbiter
from .audit import Audit, Log
from .events import Lifecycle
from .loader import Loader
from .mock import MockModel, MockTransport
from .outbound import OutboundPipeline
from .permissions import PermissionGate
from .sdk import Context, Draft, ModelView, StoreView
from .store import Store

DEFAULT_SYSTEM_PROMPT = "你是插件内核的默认人格：用中文直接、简短地回答用户。"

SESSION_KINDS = ("group", "private")
SENDER_ROLES = ("owner", "admin", "member")
SEGMENT_KEYS = {
    "text": ("type", "text"),
    "image": ("type", "url", "file", "size"),
    "voice": ("type", "url", "duration"),
    "file": ("type", "name", "url", "size"),
    "face": ("type", "id"),
}
EMPTY_MESSAGE = MappingProxyType(
    {
        "session": {"id": "", "kind": "group", "name": "", "key": "g:"},
        "sender": {"id": "", "name": "", "role": "member", "is_bot": False},
        "message": {
            "id": "",
            "text": "",
            "segments": [],
            "mentions": [],
            "reply_to": None,
            "ts": 0.0,
        },
    }
)


def _freeze(value):
    """把清单递归冻成不可变结构（tuple + MappingProxyType）。"""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _text_of(segments) -> str:
    parts = []
    for seg in segments or ():
        if seg.get("type") == "text":
            parts.append(str(seg.get("text") or ""))
    return "".join(parts)


def normalize_segments(segments) -> list:
    """只保留白名单段类型与白名单字段；未知类型丢弃。"""
    out = []
    for seg in segments or ():
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        keys = SEGMENT_KEYS.get(kind)
        if keys is None:
            continue
        out.append({key: seg.get(key) for key in keys if key in seg})
    return out


def normalize_message(raw) -> dict:
    """入站原始字典 → 归一化上下文（白名单，绝不透传上游原始报文）。"""
    if not isinstance(raw, dict):
        raise KernelError("入站消息必须是 dict，收到 %s" % type(raw).__name__)
    session = raw.get("session")
    if not isinstance(session, dict):
        raise KernelError("入站消息缺少 session 段")
    session_id = session.get("id")
    if session_id in (None, ""):
        raise KernelError("入站消息缺少 session.id")
    kind = str(session.get("kind") or "group")
    if kind not in SESSION_KINDS:
        raise KernelError("session.kind 只接受 %s，收到 %r" % ("/".join(SESSION_KINDS), kind))
    sender = raw.get("sender") or {}
    if not isinstance(sender, dict):
        raise KernelError("入站消息 sender 段必须是 dict")
    sender_id = sender.get("id")
    if sender_id in (None, ""):
        raise KernelError("入站消息缺少 sender.id")
    role = str(sender.get("role") or "member")
    if role not in SENDER_ROLES:
        role = "member"
    body = raw.get("message")
    if not isinstance(body, dict):
        raise KernelError("入站消息缺少 message 段")
    segments = normalize_segments(body.get("segments"))
    text = body.get("text")
    text = _text_of(segments) if text in (None, "") else str(text)
    key = ("g:" if kind == "group" else "p:") + str(session_id)
    return {
        "session": {
            "id": str(session_id),
            "kind": kind,
            "name": str(session.get("name") or ""),
            "key": key,
        },
        "sender": {
            "id": str(sender_id),
            "name": str(sender.get("name") or ""),
            "role": role,
            "is_bot": bool(sender.get("is_bot", False)),
        },
        "message": {
            "id": str(body.get("id") or ""),
            "text": text,
            "segments": segments,
            "mentions": [str(item) for item in (body.get("mentions") or ())],
            "reply_to": body.get("reply_to"),
            "ts": body.get("ts"),
        },
    }


class Host:
    """插件内核主体。"""

    def __init__(
        self,
        plugins_dir=None,
        data_dir=None,
        transport=None,
        model=None,
        *,
        host_version=HOST_VERSION,
        audit=None,
        logger=None,
        config=None,
        default_persona=True,
        clock=time.time,
    ) -> None:
        self.clock = clock
        self.plugins_dir = Path(plugins_dir) if plugins_dir is not None else None
        self.data_dir = Path(data_dir) if data_dir is not None else None
        if self.data_dir is not None:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = dict(config or {})
        self.host_version = host_version
        self.default_persona = default_persona
        self.transport = transport if transport is not None else MockTransport(clock=clock)
        self.model = model if model is not None else MockModel()
        self.audit = audit if audit is not None else Audit(self.data_dir, clock=clock)
        self.log = logger if logger is not None else Log("plugin_core")
        self.gate = PermissionGate(self.audit, self.log.child("permissions"))
        self.store = Store(self.data_dir)
        self.loader = self._new_loader()
        self.outbound = OutboundPipeline(self)
        self.action_executor = ActionExecutor(self)
        self.arbiter = Arbiter(self)
        self.lifecycle = Lifecycle(
            "host", audit=self.audit, log=self.log.child("lifecycle"), clock=clock
        )
        self.plugins = {}
        self.refused = []
        self.scheduled = []
        self.sessions = {}
        self.session_loaded = False
        self.current_message = None
        self.frozen = False
        self.started = False
        self._snapshot = ()

    # --- 构造期辅助 -------------------------------------------------
    def _new_loader(self) -> Loader:
        return Loader(
            self.plugins_dir,
            self.audit,
            self.log.child("loader"),
            self.gate,
            self.store,
        )

    def _session_path(self) -> Path:
        if self.data_dir is None:
            raise KernelError("没有 data_dir，无法落盘会话")
        return self.data_dir / "session.json"

    # --- 启动 / 关闭 ------------------------------------------------
    def start(self):
        """preLaunch → 连上传输层 → load → enable → freeze。"""
        if self.started:
            return self.freeze_snapshot()
        self.lifecycle.enter("preLaunch")
        self.log.info("preLaunch：配置迁移与会话恢复在连上协议端之前完成")
        self.load_session()
        connected = self.transport.connect(self.config)
        self.audit.write(
            {
                "event": "transport-connected",
                "code": "connected",
                "transport": type(self.transport).__name__,
                "detail": json.dumps(connected, ensure_ascii=False, sort_keys=True),
            }
        )
        self.log.info("传输层已连上：%s" % type(self.transport).__name__)
        self.lifecycle.enter("load")
        self._load_and_enable()
        self.started = True
        return self.freeze_snapshot()

    def _load_and_enable(self) -> None:
        result = self.loader.load_tree()
        self.plugins = {plugin.id: plugin for plugin in result["loaded"]}
        self.refused = result["refused"]
        self.lifecycle.enter("enable")
        for plugin in self.plugins.values():
            plugin.lifecycle.enter("enable")
            plugin.api._set_phase("enable")
            plugin.enabled = True
            self.audit.write(
                {
                    "event": "plugin-enabled",
                    "plugin": plugin.id,
                    "version": plugin.manifest.get("version"),
                    "capabilities": len(plugin.manifest.get("capabilities") or ()),
                }
            )
        self._freeze()
        self.log.info(
            "加载完成：%d 个插件，%d 个拒载，已 freeze"
            % (len(self.plugins), len(self.refused))
        )

    def _freeze(self) -> tuple:
        self.frozen = True
        self._snapshot = tuple(_freeze(plugin.snapshot()) for plugin in self.plugins.values())
        self.audit.write(
            {
                "event": "frozen",
                "code": "frozen",
                "moment": "start",
                "plugins": [plugin.id for plugin in self.plugins.values()],
            }
        )
        return self._snapshot

    def freeze_snapshot(self) -> tuple:
        """不可变插件清单：tuple of frozen dict。"""
        return self._snapshot

    def _teardown(self, plugin, *, reason: str) -> dict:
        """disable → unload，并把 unload 四样清理结果记审计。"""
        plugin.api._set_phase("disable")
        plugin.lifecycle.enter("disable", reason=reason)
        plugin.api._set_phase("unload")
        plugin.lifecycle.enter("unload")
        info = plugin.lifecycle.unload()
        plugin.api._close()
        plugin.enabled = False
        self.audit.write(
            {
                "event": "plugin-unloaded",
                "plugin": plugin.id,
                "reason": reason,
                "cleaned": info["cleaned"],
                "leaked": info["leaked"],
            }
        )
        return info

    def reload_all(self):
        """只支持整批重载：全部 unload 后重新扫描目录。"""
        self.log.info("整批重载：先 unload 全部插件")
        for plugin in list(self.plugins.values()):
            self._teardown(plugin, reason="reload")
        self.plugins = {}
        self.refused = []
        self.frozen = False
        self.loader = self._new_loader()
        self.lifecycle = Lifecycle(
            "host", audit=self.audit, log=self.log.child("lifecycle"), clock=self.clock
        )
        self.lifecycle.enter("preLaunch", reason="reload")
        self.lifecycle.enter("load", reason="reload")
        self._load_and_enable()
        self.audit.write(
            {
                "event": "reloaded",
                "code": "reload",
                "plugins": [plugin.id for plugin in self.plugins.values()],
                "refused": len(self.refused),
            }
        )
        return self.freeze_snapshot()

    def stop(self) -> dict:
        for plugin in list(self.plugins.values()):
            self._teardown(plugin, reason="stop")
        self.lifecycle.enter("disable")
        self.lifecycle.enter("unload")
        self.transport.close()
        self.audit.write({"event": "stopped", "code": "stopped", "plugins": len(self.plugins)})
        self.started = False
        return {"stopped": True, "plugins": len(self.plugins)}

    # --- 运行期增删（freeze 后一律拒绝）------------------------------
    def add_plugin(self, plugin_dir) -> dict:
        plugin_dir = Path(plugin_dir)
        if self.frozen:
            self.audit.write(
                {
                    "event": "frozen",
                    "code": "frozen",
                    "moment": "add-plugin",
                    "plugin": plugin_dir.name,
                    "dir": str(plugin_dir),
                }
            )
            self.log.warn("已 freeze，拒绝运行期新增插件 %s" % plugin_dir.name)
            raise FreezeViolation("加载完成即 freeze：拒绝 add_plugin(%s)" % plugin_dir.name)
        result = self.loader.load_tree([plugin_dir])
        if not result["loaded"]:
            detail = result["refused"][0] if result["refused"] else {}
            raise KernelError("add_plugin 失败：%s" % detail.get("code", "unknown"))
        added = []
        for plugin in result["loaded"]:
            plugin.lifecycle.enter("enable")
            plugin.api._set_phase("enable")
            plugin.enabled = True
            self.plugins[plugin.id] = plugin
            added.append(plugin.id)
        self._freeze()
        return {"added": added, "plugins": [plugin.id for plugin in self.plugins.values()]}

    def uninstall(self, plugin_id, *, data_policy=None) -> dict:
        plugin = self.plugins.get(plugin_id)
        if plugin is None:
            self.audit.write(
                {"event": "uninstall-refused", "plugin": plugin_id, "code": "unknown-plugin"}
            )
            raise KernelError("没有叫 %r 的已加载插件" % (plugin_id,))
        info = self._teardown(plugin, reason="uninstall")
        del self.plugins[plugin_id]
        policy = data_policy or plugin.manifest.get("data_policy") or "retain"
        removed = self.store.uninstall(plugin_id, policy)
        self._snapshot = tuple(_freeze(item.snapshot()) for item in self.plugins.values())
        record = {
            "plugin": plugin_id,
            "data_policy": policy,
            "removed": removed["removed"],
            "leaked": info["leaked"],
        }
        self.audit.write({"event": "uninstalled", **record})
        self.log.info(
            "已卸载 %s（data_policy=%s，清理 %s）" % (plugin_id, policy, removed["removed"])
        )
        return record

    # --- 运行期查询 -------------------------------------------------
    def enabled_plugins(self) -> list:
        return [plugin for plugin in self.plugins.values() if plugin.enabled]

    def get_plugin(self, plugin_id):
        return self.plugins.get(plugin_id)

    def build_prompt(self, message=None, *, persona=None) -> list:
        message = message or self.current_message or EMPTY_MESSAGE
        system = persona or self.config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        messages = [{"role": "system", "content": system}]
        session_key = message["session"]["key"]
        turns = (self.sessions.get(session_key) or {}).get("turns") or []
        for turn in turns[-3:]:
            if turn.get("user"):
                messages.append({"role": "user", "content": turn["user"]})
            if turn.get("reply"):
                messages.append({"role": "assistant", "content": turn["reply"]})
        messages.append({"role": "user", "content": message["message"]["text"]})
        return messages

    def build_context(self, plugin, message=None) -> Context:
        message = message or self.current_message or EMPTY_MESSAGE
        data = {
            "session": dict(message["session"]),
            "sender": dict(message["sender"]),
            "message": {
                "id": message["message"]["id"],
                "text": message["message"]["text"],
                "segments": [dict(seg) for seg in message["message"]["segments"]],
                "mentions": list(message["message"]["mentions"]),
                "reply_to": message["message"]["reply_to"],
                "ts": message["message"]["ts"],
            },
            "plugin": {"id": plugin.id, "version": plugin.manifest.get("version")},
            "host": {"version": self.host_version, "api_version": API_VERSION},
            "cfg": plugin.api.cfg,
            "store": StoreView(plugin.api._kv, self.gate, plugin.id, plugin.permissions),
            "log": plugin.api.log,
            "model": ModelView(self.model.chat, plugin.id),
            "_can": plugin.api.can,
            "_fetch": self._make_fetch(plugin),
        }
        return Context(data)

    def _make_fetch(self, plugin):
        def _fetch(url, *, method="GET", body=None, timeout=10.0):
            if "network" not in plugin.permissions:
                self.gate.check(plugin.id, plugin.permissions, "fetch", permission="network")
                raise PermissionDenied(plugin.id, "fetch", "network")
            return self.transport.fetch(url, method=method, body=body, timeout=timeout)

        return _fetch

    # --- 出站 -------------------------------------------------------
    def deliver(self, sends) -> list:
        """把动作执行器产出的 send 列表逐条推过出站管道再发出去。"""
        delivered = []
        for send in sends or ():
            draft = Draft(
                send.get("conversation") or EMPTY_MESSAGE["session"]["key"],
                send.get("text") or "",
                tuple(send.get("media") or ()),
                send.get("plugin") or "",
            )
            outcome = self.outbound.run(draft)
            final = outcome["draft"]
            if outcome["cancelled"] or final is None:
                self.log.info("出站被 %s 取消，未发送" % (outcome["by"],))
                continue
            self.transport.send(final)
            delivered.append(
                {
                    "conversation": final.conversation,
                    "text": final.text,
                    "media": list(final.media),
                    "plugin": send.get("plugin") or "",
                }
            )
        return delivered

    # --- 消息入口 ---------------------------------------------------
    def handle(self, message) -> dict:
        if not self.started:
            raise KernelError("Host 未 start，不能 handle")
        normalized = normalize_message(message)
        self.current_message = normalized
        self.lifecycle.enter("message")
        self.log.info(
            "归一化完成 session=%s sender=%s text=%r segments=%d"
            % (
                normalized["session"]["key"],
                normalized["sender"]["id"],
                normalized["message"]["text"],
                len(normalized["message"]["segments"]),
            )
        )
        record = self.arbiter.run(normalized)
        self.remember(normalized, record)
        return record

    def handle_command(self, name, message=None, payload=None) -> dict:
        message = message or self.current_message or EMPTY_MESSAGE
        delivered = []
        for plugin in self.enabled_plugins():
            handler = plugin.declarations.commands.get(name)
            if handler is None:
                continue
            self.lifecycle.enter("command", name=name)
            ctx = self.build_context(plugin, message)
            try:
                actions = handler(ctx, dict(payload or {}))
            except Exception as exc:  # 插件异常一律隔离
                self._hook_failed(plugin, "command", exc)
                continue
            sends = self.action_executor.run(
                plugin, actions, session_id=message["session"]["key"]
            )["sends"]
            delivered.extend(self.deliver(sends))
        return {"command": name, "sent": delivered}

    def emit_event(self, event, payload=None, *, depth=1) -> dict:
        message = self.current_message or EMPTY_MESSAGE
        delivered = []
        for plugin in self.enabled_plugins():
            handlers = plugin.declarations.events.get(event) or ()
            for handler in handlers:
                self.lifecycle.enter("event", name=event)
                ctx = self.build_context(plugin, message)
                try:
                    actions = handler(ctx, dict(payload or {}))
                except Exception as exc:
                    self._hook_failed(plugin, "event", exc)
                    continue
                sends = self.action_executor.run(
                    plugin, actions, session_id=message["session"]["key"]
                )["sends"]
                delivered.extend(self.deliver(sends))
        return {"event": event, "sent": delivered}

    def schedule(self, delay, event, payload=None) -> dict:
        entry = {
            "delay": float(delay),
            "event": event,
            "payload": dict(payload or {}),
            "due_ts": round(self.clock() + float(delay), 3),
        }
        self.scheduled.append(entry)
        self.audit.write({"event": "scheduled", "code": "scheduled", **entry})
        return entry

    def run_timers(self, now=None) -> dict:
        """把到点的定时任务派给插件注册的 timer 回调（mock 时钟下由调用方驱动）。"""
        now = self.clock() if now is None else now
        due = [item for item in self.scheduled if item["due_ts"] <= now]
        self.scheduled = [item for item in self.scheduled if item["due_ts"] > now]
        fired = []
        for entry in due:
            for plugin in self.enabled_plugins():
                for handler in plugin.declarations.timers:
                    self.lifecycle.enter("timer", event=entry["event"])
                    ctx = self.build_context(plugin)
                    try:
                        actions = handler(ctx, entry["payload"])
                    except Exception as exc:
                        self._hook_failed(plugin, "timer", exc)
                        continue
                    self.action_executor.run(plugin, actions, session_id=None)
                    fired.append({"plugin": plugin.id, "event": entry["event"]})
        return {"fired": fired, "remaining": len(self.scheduled)}

    def _hook_failed(self, plugin, moment, exc) -> dict:
        record = self.audit.write(
            {
                "event": "hook-failed",
                "plugin": plugin.id,
                "moment": moment,
                "code": "hook-failed",
                "detail": "%s: %s" % (type(exc).__name__, exc),
            }
        )
        self.log.error("插件 %s 在 %s 时刻抛异常：%s: %s" % (plugin.id, moment, type(exc).__name__, exc))
        try:
            plugin.lifecycle.enter("error", moment=moment, error=type(exc).__name__)
        except Exception:
            pass
        return record

    # --- 会话落盘 ---------------------------------------------------
    def remember(self, message, record) -> None:
        texts = [item["text"] for item in (record.get("sent") or ()) if item.get("text")]
        if not texts:
            return
        session_key = message["session"]["key"]
        bucket = self.sessions.setdefault(
            session_key,
            {
                "kind": message["session"]["kind"],
                "name": message["session"].get("name") or "",
                "turns": [],
            },
        )
        bucket["turns"].append(
            {
                "user": message["message"]["text"],
                "reply": "\n".join(texts),
                "ts": round(self.clock(), 3),
            }
        )
        bucket["turns"] = bucket["turns"][-50:]
        self.save_session()

    def save_session(self) -> Path:
        path = self._session_path()
        payload = {
            "version": 1,
            "host_version": self.host_version,
            "updated_ts": round(self.clock(), 3),
            "turns": sum(len(bucket["turns"]) for bucket in self.sessions.values()),
            "sessions": self.sessions,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_session(self) -> dict:
        path = self._session_path()
        self.sessions = {}
        self.session_loaded = False
        if not path.is_file():
            return self.sessions
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.log.warn("会话文件读不动，按空会话启动：%s: %s" % (type(exc).__name__, exc))
            return self.sessions
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if isinstance(sessions, dict):
            for key, bucket in sessions.items():
                turns = bucket.get("turns") if isinstance(bucket, dict) else None
                self.sessions[key] = {
                    "kind": (bucket or {}).get("kind") or "group",
                    "name": (bucket or {}).get("name") or "",
                    "turns": list(turns or ()),
                }
            self.session_loaded = True
        self.log.info("会话已恢复：%d 个会话，%d 轮"
                      % (len(self.sessions), sum(len(b["turns"]) for b in self.sessions.values())))
        return self.sessions
