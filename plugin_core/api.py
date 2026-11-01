"""plugin_core.api — API 版本、常量、异常、Bid 构造器。

权威规格：D:/qq机器人5.0/docs/插件内核-API-v1.md
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Iterable

API_VERSION = 1
PLUGIN_API_VERSION = "1.0.0"
HOST_VERSION = "1.0.0"

STAGES = ("inbound", "generate", "outbound")
PRODUCES = ("main", "attach", "extra", "silent")
OUTBOUND_PRODUCES = ("rewrite", "split", "cancel")
ALL_PRODUCES = PRODUCES + OUTBOUND_PRODUCES
SIDES = ("local", "group", "both")
DATA_POLICIES = ("retain", "purge")

PERMISSIONS = ("read_message", "send_message", "store", "network", "admin")
PERMISSION_LABELS = {
    "read_message": "读消息",
    "send_message": "发消息",
    "store": "存数据",
    "network": "联网",
    "admin": "管理权限",
}
PERMISSION_FLAGS = {"config_write": "store", "exec": "admin", "read_env": "admin"}

ROLES = ("owner", "admin", "member")
SEGMENT_TYPES = ("text", "image", "voice", "file", "face")

DROPPED_REASONS = ("priority-low", "no-trigger", "requires-main", "silent-over-main")

LIFECYCLE_MOMENTS = (
    "preLaunch",
    "load",
    "enable",
    "disable",
    "unload",
    "message",
    "timer",
    "command",
    "event",
    "error",
)
RESOURCE_KINDS = ("timer", "subscription", "connection", "thread")

REQUIRED_FIELDS = (
    "id",
    "name",
    "version",
    "api_version",
    "author",
    "description",
    "entry",
    "permissions",
    "capabilities",
    "side",
    "data_policy",
    "upstream",
    "native",
)
OPTIONAL_FIELDS = ("permission_flags", "storage", "config_schema")
MANIFEST_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

REFUSAL_CODES = (
    "bad-json",
    "bad-manifest",
    "missing-field",
    "bad-id",
    "id-mismatch",
    "bad-api-version",
    "api-version-mismatch",
    "entry-missing",
    "entry-escape",
    "bad-entry",
    "setup-error",
    "duplicate-id",
    "bad-permission",
    "bad-capability",
    "bad-data-policy",
    "bad-side",
    "bad-upstream",
)

ACTION_KINDS = ("send_text", "send_media", "store_set", "store_del", "schedule", "emit", "noop")


class KernelError(Exception):
    """内核异常基类。"""

    code = "kernel-error"


class ManifestError(KernelError):
    """manifest 校验失败，code 取 REFUSAL_CODES 之一。"""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in REFUSAL_CODES:
            raise ValueError("未知拒载码: %r" % (code,))
        super().__init__("%s: %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = detail


class PermissionDenied(KernelError):
    """权限未声明而尝试执行动作。"""

    code = "denied"

    def __init__(self, plugin_id: str, action: str, permission: str | None) -> None:
        super().__init__("plugin %s 未声明权限 %s，动作 %s 被拒绝" % (plugin_id, permission, action))
        self.plugin_id = plugin_id
        self.action = action
        self.permission = permission


class FreezeViolation(KernelError):
    """加载完成即 freeze，运行期增删插件被拒绝。"""

    code = "frozen"

    def __init__(self, detail: str = "plugin set is frozen") -> None:
        super().__init__(detail)
        self.detail = detail


class LifecycleError(KernelError):
    """生命周期时刻顺序错误。"""

    code = "lifecycle"


class HookFailed(KernelError):
    """插件回调抛异常（已隔离）。"""

    code = "hook-failed"

    def __init__(self, plugin_id: str, moment: str, error: BaseException) -> None:
        super().__init__("%s@%s: %s: %s" % (plugin_id, moment, type(error).__name__, error))
        self.plugin_id = plugin_id
        self.moment = moment


@dataclasses.dataclass
class Bid:
    """一次竞标。produces ∈ main/attach/extra/silent。"""

    produces: str
    priority: int
    generate: Callable[..., Any] | None = None
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "produces": self.produces,
            "priority": self.priority,
            "reason": self.reason,
        }


def bid(produces: str, priority: int, generate: Callable[..., Any] | None = None, reason: str = "") -> Bid:
    """api.bid("main", 80, generate=fn, reason="关键词命中")"""
    if produces not in PRODUCES:
        raise ValueError("produces 必须是 %s 之一，收到 %r" % ("/".join(PRODUCES), produces))
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("priority 必须是 int，收到 %r" % (priority,))
    if produces == "silent":
        if generate is not None and not callable(generate):
            raise ValueError("generate 必须可调用")
        return Bid(produces, priority, generate, reason)
    if not callable(generate):
        raise ValueError("produces=%s 必须提供可调用的 generate" % produces)
    return Bid(produces, priority, generate, reason)


def coerce_bid(value: Any, plugin_id: str) -> Bid:
    """把插件返回值里的 Bid / dict 统一成 Bid。"""
    if isinstance(value, Bid):
        return value
    if isinstance(value, dict):
        return bid(
            value.get("produces"),
            value.get("priority"),
            value.get("generate"),
            value.get("reason", ""),
        )
    raise ValueError("plugin %s 返回了无法识别的竞标 %r" % (plugin_id, value))
