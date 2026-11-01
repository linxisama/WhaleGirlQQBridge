"""plugin_core.sdk — 给插件用的 PluginAPI、只读 Context、Draft、Action（规格 §3）。"""

from __future__ import annotations

import copy

from .api import (
    PERMISSIONS,
    KernelError,
    PermissionDenied,
    bid as _make_bid,
)
from .audit import Log


# --------------------------------------------------------------------------- Draft
class Draft:
    """出站草稿：{"conversation","text","media","plugin"}。"""

    __slots__ = ("conversation", "text", "media", "plugin")

    def __init__(self, conversation: str, text: str, media=None, plugin: str = "") -> None:
        object.__setattr__(self, "conversation", conversation)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "media", list(media or ()))
        object.__setattr__(self, "plugin", plugin)

    def copy(self, **changes) -> "Draft":
        data = self.as_dict()
        data.update(changes)
        return Draft(**data)

    def as_dict(self) -> dict:
        return {
            "conversation": self.conversation,
            "text": self.text,
            "media": list(self.media),
            "plugin": self.plugin,
        }

    def __eq__(self, other) -> bool:
        return isinstance(other, Draft) and self.as_dict() == other.as_dict()

    def __repr__(self) -> str:
        return "Draft(%r, %r, media=%r, plugin=%r)" % (
            self.conversation,
            self.text,
            self.media,
            self.plugin,
        )


# --------------------------------------------------------------------------- Action
class Action:
    """可序列化动作；审计里原样记录 as_dict()。"""

    __slots__ = ("kind", "args")

    def __init__(self, kind: str, args: dict | None = None) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "args", dict(args or {}))

    @staticmethod
    def send_text(conversation, text) -> "Action":
        return Action("send_text", {"conversation": conversation, "text": text})

    @staticmethod
    def send_media(conversation, media) -> "Action":
        return Action("send_media", {"conversation": conversation, "media": list(media or ())})

    @staticmethod
    def store_set(key, value) -> "Action":
        return Action("store_set", {"key": key, "value": value})

    @staticmethod
    def store_del(key) -> "Action":
        return Action("store_del", {"key": key})

    @staticmethod
    def schedule(delay, event, payload=None) -> "Action":
        return Action("schedule", {"delay": delay, "event": event, "payload": dict(payload or {})})

    @staticmethod
    def emit(event, payload=None) -> "Action":
        return Action("emit", {"event": event, "payload": dict(payload or {})})

    @staticmethod
    def noop() -> "Action":
        return Action("noop", {})

    @staticmethod
    def from_dict(data) -> "Action":
        if isinstance(data, Action):
            return data
        if not isinstance(data, dict):
            raise KernelError("动作必须是 Action 或 dict，收到 %r" % (data,))
        payload = dict(data)
        kind = payload.pop("kind", None) or payload.pop("action", None)
        if not kind:
            raise KernelError("动作缺 kind/action：%r" % (data,))
        return Action(kind, payload)

    def as_dict(self) -> dict:
        out = {"action": self.kind}
        out.update(self.args)
        return out

    def __eq__(self, other) -> bool:
        return isinstance(other, Action) and self.as_dict() == other.as_dict()

    def __repr__(self) -> str:
        return "Action(%s, %r)" % (self.kind, self.args)


# --------------------------------------------------------------------------- Context 内部视图
class ModelView:
    """ctx.model：只给 messages 与 max_chars，不暴露 provider/endpoint/temperature。"""

    __slots__ = ("_chat", "_plugin_id")

    def __init__(self, chat, plugin_id: str) -> None:
        object.__setattr__(self, "_chat", chat)
        object.__setattr__(self, "_plugin_id", plugin_id)

    def chat(self, messages, *, max_chars=None) -> str:
        if not isinstance(messages, list) or not messages:
            raise KernelError("ctx.model.chat 需要非空 messages 列表")
        for item in messages:
            if not isinstance(item, dict) or "role" not in item or "content" not in item:
                raise KernelError("messages 每项必须是 {role, content}")
            if not isinstance(item["content"], str):
                raise KernelError("messages.content 必须是字符串")
        if max_chars is not None and (isinstance(max_chars, bool) or not isinstance(max_chars, int)):
            raise KernelError("max_chars 必须是 int 或 None")
        return self._chat(messages, max_chars=max_chars)

    def __repr__(self) -> str:
        return "ModelView(%s)" % self._plugin_id


class StoreView:
    """ctx.store：本插件命名空间 KV，写操作先过权限闸门。"""

    __slots__ = ("_kv", "_gate", "_plugin_id", "_declared")

    def __init__(self, kv, gate, plugin_id: str, declared) -> None:
        object.__setattr__(self, "_kv", kv)
        object.__setattr__(self, "_gate", gate)
        object.__setattr__(self, "_plugin_id", plugin_id)
        object.__setattr__(self, "_declared", set(declared))

    def get(self, key, default=None):
        return self._kv.get(key, default)

    def keys(self) -> list:
        return self._kv.keys()

    def as_dict(self) -> dict:
        return self._kv.as_dict()

    def _require(self, action: str) -> None:
        if self._gate is not None:
            if self._gate.check(self._plugin_id, self._declared, action):
                return
        elif "store" in self._declared:
            return
        raise PermissionDenied(self._plugin_id, action, "store")

    def set(self, key, value) -> None:
        self._require("store_set")
        self._kv.set(key, value)

    def delete(self, key) -> bool:
        self._require("store_del")
        return self._kv.delete(key)

    def __repr__(self) -> str:
        return "StoreView(%s, %r)" % (self._plugin_id, self._kv.keys())


class Context:
    """只读上下文：__setattr__/__delattr__ 抛 AttributeError。"""

    __slots__ = ("_data",)

    def __init__(self, data: dict) -> None:
        object.__setattr__(self, "_data", dict(data))

    def __setattr__(self, name, value):
        raise AttributeError("Context 只读，不能设置 %r" % (name,))

    def __delattr__(self, name):
        raise AttributeError("Context 只读，不能删除 %r" % (name,))

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError(name) from None

    # 主体提供的两个受控出口
    def can(self, permission) -> bool:
        return bool(self._data["_can"](permission))

    def fetch(self, url, *, method="GET", body=None, timeout=10.0) -> dict:
        return self._data["_fetch"](url, method=method, body=body, timeout=timeout)

    CONTEXT_KEYS = ("session", "sender", "message", "plugin", "host", "cfg")

    def to_dict(self) -> dict:
        """调试/审计用的纯数据快照：只取白名单键，不含日志与模型对象。"""
        out = {}
        for key in self.CONTEXT_KEYS:
            if key in self._data:
                out[key] = copy.deepcopy(self._data[key])
        return out

    def __repr__(self) -> str:
        session = self._data.get("session") or {}
        return "Context(session=%r, plugin=%r)" % (session.get("id"), (self._data.get("plugin") or {}).get("id"))


# --------------------------------------------------------------------------- 声明容器
class Declarations:
    def __init__(self) -> None:
        self.inbound = []
        self.outbound = []
        self.events = {}
        self.commands = {}
        self.timers = []

    def as_dict(self) -> dict:
        return {
            "inbound": len(self.inbound),
            "outbound": len(self.outbound),
            "events": sorted(self.events),
            "commands": sorted(self.commands),
            "timers": len(self.timers),
        }


# --------------------------------------------------------------------------- PluginAPI
class PluginAPI:
    """插件在 setup(api) 里拿到的东西：只登记，不做副作用。"""

    def __init__(self, plugin_id: str, manifest: dict, *, log=None, store=None, audit=None, gate=None, lifecycle=None):
        self._plugin_id = plugin_id
        self._manifest = copy.deepcopy(manifest)
        self._decls = Declarations()
        self._phase = "load"
        self._log = (log or Log()).child(plugin_id)
        self._store = store
        self._audit = audit
        self._gate = gate
        self._lifecycle = lifecycle
        self._declared = set(manifest.get("permissions") or ())
        self._flags = list(manifest.get("permission_flags") or ())
        self._kv = store.kv(plugin_id) if store is not None else None

    # --- 只读属性 ---
    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def manifest(self) -> dict:
        return copy.deepcopy(self._manifest)

    @property
    def log(self) -> Log:
        return self._log

    @property
    def cfg(self) -> dict:
        if self._store is None:
            return {}
        return copy.deepcopy(self._store.read_config(self._plugin_id))

    @property
    def store(self):
        if self._kv is None:
            return None
        return StoreView(self._kv, self._gate, self._plugin_id, self._declared)

    @property
    def declarations(self) -> Declarations:
        return self._decls

    # --- 声明 ---
    def _require_phase(self, what: str) -> None:
        if self._phase not in ("load", "enable"):
            raise KernelError("插件 %s 当前阶段 %r，不能 %s" % (self._plugin_id, self._phase, what))

    def declare_inbound(self, fn) -> None:
        self._require_phase("declare_inbound")
        if not callable(fn):
            raise KernelError("declare_inbound 需要可调用对象")
        self._decls.inbound.append(fn)

    def declare_outbound(self, fn) -> None:
        self._require_phase("declare_outbound")
        if not callable(fn):
            raise KernelError("declare_outbound 需要可调用对象")
        self._decls.outbound.append(fn)

    def on(self, event: str, fn) -> None:
        self._require_phase("on")
        if not isinstance(event, str) or not event:
            raise KernelError("事件名必须是非空字符串")
        if not callable(fn):
            raise KernelError("事件处理必须是可调用对象")
        self._decls.events.setdefault(event, []).append(fn)
        if self._lifecycle is not None:
            self._lifecycle.acquire("subscription", "%s:%s" % (self._plugin_id, event))

    def declare_command(self, name: str, fn) -> None:
        self._require_phase("declare_command")
        if not isinstance(name, str) or not name:
            raise KernelError("指令名必须是非空字符串")
        if not callable(fn):
            raise KernelError("指令处理必须是可调用对象")
        self._decls.commands[name] = fn

    def declare_timer(self, seconds: float, fn) -> None:
        self._require_phase("declare_timer")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise KernelError("定时器周期必须是正数")
        if not callable(fn):
            raise KernelError("定时器处理必须是可调用对象")
        index = len(self._decls.timers)
        self._decls.timers.append((float(seconds), fn))
        if self._lifecycle is not None:
            self._lifecycle.acquire("timer", "%s#%d" % (self._plugin_id, index))

    def acquire(self, kind: str, ref: str | None = None):
        """登记一个插件自己负责释放的资源（connection / thread）。"""
        if self._lifecycle is None:
            raise KernelError("没有生命周期记账，不能 acquire")
        if kind not in ("connection", "thread"):
            raise KernelError("acquire 只接受 connection / thread，收到 %r" % (kind,))
        return self._lifecycle.acquire(kind, ref or "%s:%s" % (self._plugin_id, kind))

    def bid(self, produces: str, priority: int, generate=None, reason: str = ""):
        return _make_bid(produces, priority, generate=generate, reason=reason)

    def can(self, permission) -> bool:
        if permission not in PERMISSIONS:
            return False
        return permission in self._declared

    def write_config(self, values: dict) -> dict:
        """写自己的配置：需要 store + config_write 子标记。"""
        if self._store is None:
            raise KernelError("没有存储层，不能写配置")
        if self._gate is not None and not self._gate.check(
            self._plugin_id, self._declared, "config_write", permission="store"
        ):
            raise PermissionDenied(self._plugin_id, "config_write", "store")
        if "config_write" not in self._flags:
            if self._gate is not None:
                self._gate.check(
                    self._plugin_id, self._declared, "config_write", permission="config_write"
                )
            raise PermissionDenied(self._plugin_id, "config_write", "config_write")
        return self._store.write_config(
            self._plugin_id, values, declared=self._declared, flags=self._flags
        )

    # --- 内核用：阶段推进 ---
    def _set_phase(self, phase: str) -> None:
        self._phase = phase

    def _close(self) -> None:
        self._phase = "unload"
        self._decls = Declarations()
