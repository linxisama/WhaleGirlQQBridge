"""外观背景图的纯逻辑辅助：导入、解析、清理。

本模块不导入 PyQt6、不弹对话框、不做界面接线：GUI 接线由核心在 qq_gui.py 里完成。

重要实测结论（2026-10-13，PyQt6 6.11 / Windows）：
Qt 的样式表引擎**不能**用来显示用户自选的背景图，原因有两条，均已实测确认：
  1. Qt 样式表不支持 `background-size`，写了会被忽略（启动时打印 Unknown property）。
  2. 样式表加载 `file:///` 图片时无法处理中文与空格路径，
     报 “Could not create pixmap from file:\\\\\\D:\\??????????\\...”，即使路径不做百分号编码也读不到。
因此真正的背景绘制由 qq_gui.py 用 QPixmap 自行绘制（QPixmap 对同样的路径加载正常）。
本模块只负责文件层的导入、路径解析与清理，不输出任何 CSS。
"""

import hashlib
import os
import shutil
from pathlib import Path

ALLOWED_EXTS = frozenset({"png", "jpg", "jpeg", "webp", "bmp", "gif"})
BACKGROUND_DIR = ("data", "backgrounds")


def import_background(source, root):
    """把用户选择的图片复制进 root/data/backgrounds/，返回可写入配置的相对路径。

    文件名用文件内容的 sha256 摘要加原扩展名，因此同一个文件重复导入会复用同一
    目标名，不会产生副本。返回值是相对 root 的 POSIX 形式字符串，例如
    ``data/backgrounds/<64位摘要>.png``。

    ValueError: 源路径不存在、不是普通文件，或扩展名不在 ALLOWED_EXTS 内。
    """
    src = Path(source)
    if not src.exists() or not src.is_file():
        raise ValueError(f"源文件不存在或不是文件: {source}")

    ext = src.suffix.lower()
    if ext.lstrip(".") not in ALLOWED_EXTS:
        raise ValueError(f"不支持的图片扩展名: {ext or '(无扩展名)'}")

    digest = hashlib.sha256()
    with open(src, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)

    target_dir = Path(root).joinpath(*BACKGROUND_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_name = f"{digest.hexdigest()}{ext}"
    target = target_dir / target_name
    if not target.exists():
        shutil.copy2(src, target)
    return "/".join(BACKGROUND_DIR) + "/" + target_name


def resolve_background(rel, root):
    """把配置里的相对路径解析成绝对 Path。

    返回 None 的情况：rel 为空或 None、解析后越出 root、目标不存在或不是文件。
    本函数在任何输入下都不抛异常。
    """
    if not rel or not isinstance(rel, (str, os.PathLike)):
        return None
    try:
        base = Path(root).resolve()
        candidate = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def clear_background(rel, root):
    """删除背景图文件并清理空目录。

    rel 为 None 或空字符串时直接返回；目标不存在时静默返回；
    路径越出 root 时抛 ValueError（安全违规不能静默跳过）。
    """
    if not rel:
        return
    base = Path(root).resolve()
    try:
        candidate = (base / rel).resolve()
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解析背景图路径: {rel}") from exc
    try:
        candidate.relative_to(base)
    except ValueError:
        raise ValueError(f"背景图路径越出项目根目录: {rel}") from None
    if not candidate.is_file():
        return
    candidate.unlink()

    background_dir = base.joinpath(*BACKGROUND_DIR)
    if candidate.parent == background_dir and background_dir.is_dir() and not any(background_dir.iterdir()):
        background_dir.rmdir()
        data_dir = base / "data"
        if data_dir.is_dir() and not any(data_dir.iterdir()):
            data_dir.rmdir()


DEFAULT_BACKGROUND = ("assets", "backgrounds", "default.jpg")


def ensure_default_background(cfg, root, bundle=None):
    """没有自定义背景时，把内置的浅色默认背景复制到用户目录并使用它。

    只影响 ``background_image`` 为空的配置；用户选过图片的系统不会被动。
    返回传入的 cfg（就地修改），方便调用方直接继续 _apply_bg。
    """
    if not isinstance(cfg, dict) or str(cfg.get("background_image") or "").strip():
        return cfg
    if cfg.get("default_background") is False:
        return cfg
    source = Path(bundle or root).joinpath(*DEFAULT_BACKGROUND)
    if not source.is_file():
        return cfg
    try:
        cfg["background_image"] = import_background(source, root)
    except (OSError, ValueError):
        pass
    return cfg
