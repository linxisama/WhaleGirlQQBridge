"""插件设置的**一键导入/导出**（JSON 文件），带 schema 校验。

需求来源（用户）："设置个可以一键导入和导出的按钮"。做在插件设置窗口里，
导出当前插件的设置、导入一份之前导出的设置，导入时按 schema 校验并把问题列出来
（而不是静默丢弃或把坏值写进配置）。

设计约束：
- 只处理**该插件自己的**设置，文件里带 ``plugin`` 与 ``version`` 便于日后兼容；
- 导入时未知键忽略并报告、类型/范围不符的键回退默认并报告 —— 与
  ``PluginManager`` 加载时的行为一致，避免"界面显示的值和磁盘不一致"；
- 纯函数（不碰 Qt、不碰磁盘以外的东西），便于离线测试。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

FORMAT_VERSION = 1


def dump_settings(path, plugin_id: str, settings: dict) -> Path:
    """把设置写成 JSON 文件，返回实际路径。"""
    target = Path(path)
    target.write_text(json.dumps({
        "format": "whalegirl-plugin-settings",
        "version": FORMAT_VERSION,
        "plugin": str(plugin_id),
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "settings": dict(settings or {}),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def _coerce(spec: dict, value, key: str):
    """按 FieldSchema 的声明做一次宽松转换；不合法就抛 ValueError。"""
    kind = str(spec.get("type") or "string")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "on", "是"}:
            return True
        if text in {"false", "0", "no", "off", "否"}:
            return False
        raise ValueError(f"{key} 需要是开关值（true/false）")
    if kind == "int":
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需要整数，收到 {value!r}")
        low, high = spec.get("min"), spec.get("max")
        if low is not None and number < int(low):
            raise ValueError(f"{key} 不能小于 {low}")
        if high is not None and number > int(high):
            raise ValueError(f"{key} 不能大于 {high}")
        return number
    if kind == "float":
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需要数字，收到 {value!r}")
    # string / select / text：转成字符串；select 校验取值域
    text = str(value)
    options = spec.get("options") or spec.get("choices")
    if options and text not in {str(item) for item in options}:
        raise ValueError(f"{key} 只能是 {options} 之一")
    return text


def load_settings(path, schema: dict, defaults: dict) -> tuple[dict, list[str]]:
    """读取导出的设置文件，按 schema 校验。

    返回 ``(可用设置, 问题清单)``：问题清单里是"未知键 / 类型不符已回退 / 文件格式不对"这类
    人话说明，界面直接显示给用户；**不会**因为个别键有问题就整体失败。
    """
    problems: list[str] = []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {}, [f"读不出这个文件（需要本程序导出的 JSON）：{exc}"]
    if not isinstance(data, dict):
        return {}, ["文件内容不是对象（需要本程序导出的 JSON）"]
    if data.get("format") not in (None, "whalegirl-plugin-settings"):
        problems.append(f"文件格式标记是 {data.get('format')!r}，仍按本程序格式尝试读取")
    raw = data.get("settings")
    if not isinstance(raw, dict):
        return {}, problems + ["文件里没有 settings 对象"]
    schema = schema if isinstance(schema, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    result = dict(defaults)
    for key, value in raw.items():
        if key not in schema:
            problems.append(f"未知设置项 {key} 已忽略")
            continue
        try:
            result[key] = _coerce(schema[key] if isinstance(schema[key], dict) else {}, value, key)
        except ValueError as exc:
            problems.append(f"{exc}，已回退默认值 {defaults.get(key)!r}")
    return result, problems
