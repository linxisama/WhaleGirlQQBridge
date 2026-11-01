"""plugin_core.events — 十个生命周期时刻 + 资源记账（规格 §6）。"""

from __future__ import annotations

import time

from .api import LIFECYCLE_MOMENTS, RESOURCE_KINDS, LifecycleError

ORDERED = ("preLaunch", "load", "enable", "disable", "unload")
RUNTIME = ("message", "timer", "command", "event", "error")
_ORDER = {name: index for index, name in enumerate(ORDERED)}
# 定时器与事件订阅由内核在 unload 代为主持清理（插件侧没有注销 API）；
# 连接与线程必须由插件自己 release，未清 → leak-warning。
KERNEL_OWNED = ("timer", "subscription")
PLUGIN_OWNED = ("connection", "thread")


class Release:
    """acquire 返回的句柄，release 幂等。"""

    def __init__(self, lifecycle: "Lifecycle", kind: str, ref: str) -> None:
        self.lifecycle = lifecycle
        self.kind = kind
        self.ref = ref
        self.released = False

    def release(self) -> bool:
        if self.released:
            return False
        self.released = self.lifecycle.release(self.kind, self.ref)
        return self.released

    def __enter__(self) -> "Release":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class Lifecycle:
    def __init__(self, plugin_id: str, audit=None, log=None, *, clock=time.time) -> None:
        self.plugin_id = plugin_id
        self.audit = audit
        self.log = log
        self.clock = clock
        self.phase = None
        self.trace = []
        self.resources = {kind: [] for kind in RESOURCE_KINDS}

    # --- 时刻 ---
    def enter(self, moment: str, **detail) -> dict:
        if moment not in LIFECYCLE_MOMENTS:
            raise LifecycleError("未知生命周期时刻 %r" % (moment,))
        if self.phase == "unload":
            raise LifecycleError("插件 %s 已 unload，不能再进入 %s" % (self.plugin_id, moment))
        if moment in RUNTIME:
            if self.phase != "enable":
                raise LifecycleError(
                    "插件 %s 当前阶段 %r，收不到 %s（只有 enable 之后才能接事件）"
                    % (self.plugin_id, self.phase, moment)
                )
        else:
            previous = _ORDER.get(self.phase, -1)
            if _ORDER[moment] <= previous:
                raise LifecycleError(
                    "插件 %s 生命周期倒序：%s 之后不能进 %s" % (self.plugin_id, self.phase, moment)
                )
            self.phase = moment
        rec = {"moment": moment, "plugin": self.plugin_id, "phase": self.phase, "ts": round(self.clock(), 3)}
        if detail:
            rec["detail"] = detail
        self.trace.append(rec)
        return rec

    def moments(self) -> list:
        return [rec["moment"] for rec in self.trace]

    # --- 资源记账 ---
    def acquire(self, kind: str, ref: str) -> Release:
        if kind not in RESOURCE_KINDS:
            raise LifecycleError("未知资源种类 %r，只允许 %s" % (kind, "/".join(RESOURCE_KINDS)))
        if ref not in self.resources[kind]:
            self.resources[kind].append(ref)
        return Release(self, kind, ref)

    def release(self, kind: str, ref: str) -> bool:
        bucket = self.resources.get(kind)
        if bucket and ref in bucket:
            bucket.remove(ref)
            return True
        return False

    def outstanding(self) -> dict:
        return {kind: list(refs) for kind, refs in self.resources.items() if refs}

    def unload(self) -> dict:
        """清四样：定时器/事件订阅由内核代清，连接/线程未清则记 leak-warning。"""
        cleaned = {}
        for kind in KERNEL_OWNED:
            if self.resources[kind]:
                cleaned[kind] = list(self.resources[kind])
                self.resources[kind] = []
        leaked = {}
        for kind in PLUGIN_OWNED:
            if self.resources[kind]:
                leaked[kind] = list(self.resources[kind])
                self.resources[kind] = []
        result = {"plugin": self.plugin_id, "cleaned": cleaned, "leaked": leaked}
        if leaked:
            if self.audit is not None:
                self.audit.write(
                    {"event": "leak-warning", "plugin": self.plugin_id, "code": "leak-warning", "leaked": leaked}
                )
            if self.log is not None:
                self.log.warn(
                    "plugin %s unload 时仍有未清理资源：%s"
                    % (self.plugin_id, ", ".join("%s=%s" % (k, v) for k, v in leaked.items()))
                )
        return result
