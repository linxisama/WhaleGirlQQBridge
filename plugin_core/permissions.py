"""plugin_core.permissions — 五项权限 + 子标记映射 + 动作执行前校验（规格 §0.5/§0.6/§8.7）。"""

from __future__ import annotations

from typing import Iterable, Sequence

from .api import PERMISSIONS, PERMISSION_FLAGS, ManifestError

# 动作 → 需要的权限（None = 主体能力，不需要权限）
ACTION_PERMISSIONS = {
    "send_text": "send_message",
    "send_media": "send_message",
    "store_set": "store",
    "store_del": "store",
    "schedule": None,
    "emit": None,
    "noop": None,
}


def normalize_permissions(value, *, field: str = "permissions") -> list:
    if not isinstance(value, list):
        raise ManifestError("bad-permission", "%s 必须是数组" % field)
    out = []
    for item in value:
        if not isinstance(item, str) or item not in PERMISSIONS:
            raise ManifestError(
                "bad-permission",
                "%s 含未知权限 %r，只允许 %s" % (field, item, "/".join(PERMISSIONS)),
            )
        if item not in out:
            out.append(item)
    return out


def normalize_flags(value, declared: Iterable[str]) -> list:
    """校验子标记，并要求它映射到的父权限同时被声明。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError("bad-permission", "permission_flags 必须是数组")
    declared = list(declared)
    out = []
    for item in value:
        if not isinstance(item, str) or item not in PERMISSION_FLAGS:
            raise ManifestError(
                "bad-permission",
                "permission_flags 含未知子标记 %r，只允许 %s"
                % (item, "/".join(PERMISSION_FLAGS)),
            )
        parent = PERMISSION_FLAGS[item]
        if parent not in declared:
            raise ManifestError(
                "bad-permission",
                "子标记 %s 映射到 %s，但 permissions 里没有声明 %s" % (item, parent, parent),
            )
        if item not in out:
            out.append(item)
    return out


def effective_permissions(permissions: Iterable[str], flags: Iterable[str] = ()) -> set:
    result = set(permissions or ())
    for flag in flags or ():
        mapped = PERMISSION_FLAGS.get(flag)
        if mapped:
            result.add(mapped)
    return result


def required_permission(action_kind: str):
    if action_kind not in ACTION_PERMISSIONS:
        raise KeyError("未知动作 %r" % (action_kind,))
    return ACTION_PERMISSIONS[action_kind]


class PermissionGate:
    """执行动作前的权限闸门：未声明 → denied 审计一条 + 进程不崩。"""

    def __init__(self, audit=None, log=None) -> None:
        self.audit = audit
        self.log = log
        self.denied = 0

    def check(
        self,
        plugin_id: str,
        declared: Iterable[str],
        action: str,
        *,
        session: str | None = None,
        permission: str | None = None,
    ) -> bool:
        need = permission if permission is not None else required_permission(action)
        if need is None:
            return True
        if need in set(declared):
            return True
        self.denied += 1
        if self.audit is not None:
            self.audit.write(
                {
                    "event": "denied",
                    "plugin": plugin_id,
                    "action": action,
                    "session": session,
                    "code": "denied",
                    "permission": need,
                }
            )
        if self.log is not None:
            self.log.warn(
                "plugin %s 未声明 %s，动作 %s 被拒绝（session=%s）"
                % (plugin_id, need, action, session)
            )
        return False
