"""QQ Agent 能力桥的开箱状态与启用开关。

能力桥本身是 ``qqagent_bridge`` 插件；市场只负责把 Node 扩展落盘。这里把
“配置是否完整、是否已有可执行 profile、是否已启用”集中成一个可测试的状态，
避免 GUI 或 HTTP 路由直接猜插件内部字段。
"""
from __future__ import annotations

from pathlib import Path

from . import qqagent_host


def _path(value, kind: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        if kind == "file" and path.is_file():
            return path
        if kind == "dir" and path.is_dir():
            return path
    except OSError:
        return None
    return None


def _profiles(bridge) -> list[dict]:
    """取已配置 profile；只统计真正带 provider/hook 的条目。"""
    try:
        values = bridge._profiles()
    except Exception:
        return []
    result = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("provider") or item.get("hookName") or "").strip():
            result.append(item)
    return result


def bridge_status(manager) -> dict:
    """返回能力桥的只读状态；不修改任何设置。"""
    plugins = getattr(manager, "_plugins", {}) or {}
    bridge = plugins.get("qqagent_bridge")
    if bridge is None:
        return {
            "loaded": False, "enabled": False, "ready": False,
            "profiles": 0, "missing": ["qqagent_bridge"],
            "settings": {},
        }
    settings = manager.get_settings("qqagent_bridge")
    host = _path(settings.get("hostScript"), "file")
    source = _path(settings.get("sourceDir"), "dir")
    catalog = _path(settings.get("catalogPath"), "file")
    extensions = _path(settings.get("extensionsDir"), "dir")
    owners = [part for part in str(settings.get("ownerIds") or "").split(",")
              if part.strip()]
    profile_items = _profiles(bridge)
    node = qqagent_host.node_info(settings.get("nodeExe") or "")
    missing = []
    if not node.get("ok"):
        missing.append("Node 20+")
    if not host:
        missing.append("host-one.mjs")
    if not source:
        missing.append("QQ Agent 源码目录")
    if not catalog:
        missing.append("扩展 catalog")
    if not extensions:
        missing.append("扩展目录")
    if bool(settings.get("ownerOnly", True)) and not owners:
        missing.append("主人 QQ")
    if not profile_items:
        missing.append("可执行扩展（白名单 provider）")
    return {
        "loaded": True,
        "enabled": bool(settings.get("enabled", False)),
        "ready": not missing,
        "profiles": len(profile_items),
        "providers": sorted({str(item.get("provider") or item.get("hookName"))
                             for item in profile_items}),
        "missing": missing,
        "hostScript": str(host or ""),
        "sourceDir": str(source or ""),
        "catalogPath": str(catalog or ""),
        "extensionsDir": str(extensions or ""),
        "ownerIds": ",".join(owners),
        "node": str(node.get("path") or ""),
        "nodeSource": str(node.get("source") or ""),
        "settings": settings,
    }


def set_bridge_enabled(manager, enabled: bool, confirmed: bool = False) -> dict:
    """启用/停用能力桥。

    启用前必须确认配置完整，且显式传入 ``confirmed=True``；停用永远允许。
    这样市场里“安装完自动配置”不会把 Node 进程直接打开，必须由主人再确认一次。
    """
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值")
    status = bridge_status(manager)
    if not status["loaded"]:
        raise ValueError("能力桥插件未加载")
    if enabled:
        if not confirmed:
            raise ValueError("启用能力桥需要显式确认")
        if not status["ready"]:
            detail = "、".join(status["missing"]) or "配置不完整"
            raise ValueError(f"能力桥还不能启用：缺少 {detail}")
    manager.update_settings("qqagent_bridge", {"enabled": enabled})
    return bridge_status(manager)
