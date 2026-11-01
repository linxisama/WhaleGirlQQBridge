# -*- coding: utf-8 -*-
"""可写数据根的唯一来源：源码模式是仓库根，冻结模式是用户目录。

为什么单独成一个模块：src/qq_app.py 早就按这个口径选目录（DATA_ROOT），但
src/gui/pc_workspace.py、src/extension_registry.py、src/extension_host.py、
src/app_log.py 四处的 default_data_dir() 各自拿 Path(__file__) 往上退一级或两级再拼
'data'。源码模式下那正好是仓库根，看不出问题；PyInstaller 冻结后 __file__ 落在解包
目录 _internal 里，拼出来就是 _internal/data —— 装到 Program Files 这类只读位置时
建目录/写文件直接失败，装到可写位置则悄悄写进安装目录（卸载残留、多用户串数据）。
所以这些回退必须走同一个函数，不要再各自猜 __file__ 的位置。

约束：纯标准库，且不 import src 内任何其它模块 —— 它会被多个底层模块在函数体内懒导入，
这里出现反向依赖就会绕成循环导入。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: 冻结运行时用户数据目录的目录名（%LOCALAPPDATA%/WhaleGirlQQ）。
FROZEN_HOME_NAME = "WhaleGirlQQ"


def resolve_data_root() -> Path:
    """返回可写数据根目录（返回值不保证一定创建成功）。

    冻结运行（PyInstaller 置 sys.frozen 或 sys._MEIPASS）：__file__ 在解包目录
    _internal 里，往那儿写等于往安装目录写，只读安装位置会失败，因此改用
    %LOCALAPPDATA%/WhaleGirlQQ（没有 LOCALAPPDATA 就退到用户主目录）。
    建目录失败不抛异常：本函数只负责给出路径，调用方只算路径时也要能拿到值。

    源码运行：__file__ 是 <仓库根>/src/data_root.py，上一级就是仓库根。
    """
    if getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None):
        local = os.environ.get("LOCALAPPDATA") or str(Path.home())
        folder = Path(local) / FROZEN_HOME_NAME
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return folder
    return Path(__file__).resolve().parents[1]


DATA_ROOT = resolve_data_root()
