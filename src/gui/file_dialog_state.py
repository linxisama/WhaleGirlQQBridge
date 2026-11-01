"""记住文件对话框上次选中的目录（极小持久化，读写失败一律静默）。

用户以前每次点「选择背景图片」都从 图片 目录重新开始，等于每次都要重新翻一遍路径。
这里只存一个目录字符串：``<data>/qq-only/file_dialog_last.json``。

``<data>`` 与 bot 的真实数据根同一个口径（``src/log_api.py`` 的 ``data_dir_of``）；
控制台的设置页拿不到 bot，退回同样的控制台数据目录 ``%LOCALAPPDATA%\\WhaleGirlQQ\\data``
（与 ``qq_gui.ROOT``、``desktop_runtime.appearance_config_path()`` 同一处）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_FILE = "file_dialog_last.json"
_NAMESPACE = "qq-only"


def _console_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "WhaleGirlQQ" / "data"


def state_path(bot=None) -> Path:
    """状态文件路径：``<data>/qq-only/file_dialog_last.json``。"""
    data_dir = ""
    if bot is not None:
        try:
            from ..log_api import data_dir_of
            data_dir = data_dir_of(bot)
        except Exception:              # log_api 出问题不该影响文件框
            data_dir = ""
    base = Path(data_dir) if data_dir else _console_data_dir()
    return base / _NAMESPACE / _FILE


def last_directory(fallback: str = "", bot=None) -> str:
    """上次选中的目录；文件缺失/损坏/目录已不存在时返回 fallback（本身也可能为空）。"""
    directory = ""
    try:
        payload = json.loads(state_path(bot).read_text(encoding="utf-8"))
        directory = str(payload.get("directory") or "")
    except Exception:                  # 没文件、坏 JSON、权限问题：静默回退
        directory = ""
    if directory and Path(directory).is_dir():
        return directory
    return fallback or ""


def remember(path_value, bot=None) -> bool:
    """记下一个刚选中的文件/目录所在目录；成功返回 True，任何失败静默返回 False。"""
    try:
        if not str(path_value or "").strip():   # 空串会变成当前目录，必须挡掉
            return False
        target = Path(str(path_value))
        directory = target if target.is_dir() else target.parent
        if not directory.is_dir():
            return False
        path = state_path(bot)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"directory": str(directory)}, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except Exception:
        return False


__all__ = ["state_path", "last_directory", "remember"]
