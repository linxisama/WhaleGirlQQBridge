"""控制台 GUI 配置的读写与校验。纯逻辑，不依赖 PyQt6。"""
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path

DEFAULTS = {
    "opacity": 0.70,
    # 银白主题的默认底色（与 qq_gui.BG 保持一致）
    "bg_color": "#F4F6F9",
    "geometry": {},
    "background_image": "",
    # True 表示"还允许内置默认背景"；用户主动清除后置 False，不再自动套用。
    "default_background": True,
    # 外观改版：blur = 云母片模糊度 0~1；transparency = 透明度 0~1。
    # transparency 为 0 表示完全不透明（看不到桌面），此时窗口按纯色绘制。
    "blur": 0.70,
    "transparency": 0.0,
    # 文件选择一律用系统原生对话框；只有显式关掉才退回自绘（兼容模式也可开）
    "native_file_dialogs": True,
}

# 同时保存 transparency 与旧 opacity 两个字段：opacity 是 transparency 的换算值，
# 让还没升级的读取方也能拿到一份合理的外观设置。
CONFIG_VERSION = 2
GEOMETRY_KEYS = ("w", "h", "x", "y")
OPACITY_MIN = 0.3
OPACITY_MAX = 1.0
RATIO_KEYS = ("blur", "transparency")
_HEX_CHARS = set("0123456789abcdefABCDEF")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_valid_hex_color(value):
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return False
    return all(ch in _HEX_CHARS for ch in value[1:])


def _sanitize_geometry(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in GEOMETRY_KEYS:
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            result[key] = item
    return result


def clamp_ratio(value, fallback):
    """把 0~1 的比例值规整成 float；非数字或越界一律回退。"""
    if not _is_number(value):
        return fallback
    number = float(value)
    if number < 0.0 or number > 1.0:
        return fallback
    return number


def migrate(cfg):
    """旧配置迁移（只在缺少 config_version 标记时做一次）。

    版本 1 只认识 opacity = 窗口不透明度（0.3~1.0，默认 0.70）。版本 2 改成
    transparency = 1 - opacity，并新增 blur。不迁移的话，老用户的窗口会从
    "七成不透明" 突然变成默认的完全透明。

    同时处理主题换色：旧版深色底（#0B1220 / #0c1625 一类的深蓝）在银白主题下
    会把整个窗口压成灰色，因此旧配置里凡是深色底一律换成新的银白默认值。
    """
    if not isinstance(cfg, dict):
        return cfg
    if int(cfg.get("config_version") or 0) >= CONFIG_VERSION:
        return cfg

    cfg = dict(cfg)
    if "transparency" not in cfg:
        legacy = cfg.get("opacity")
        if _is_number(legacy) and OPACITY_MIN <= float(legacy) <= OPACITY_MAX:
            cfg["transparency"] = round(1.0 - float(legacy), 4)

    color = cfg.get("bg_color")
    if isinstance(color, str) and color.strip().lower() in LEGACY_DARK_DEFAULTS:
        cfg["bg_color"] = DEFAULTS["bg_color"]
    return cfg


# 只替换旧版**默认**的深蓝底色（用户没改过就是它）。用户自己挑的深色不动 ——
# 换主题不该把人家特意选的颜色抹掉。
LEGACY_DARK_DEFAULTS = frozenset({
    "#0b1220",  # 旧版 BG 默认值
    "#0c1625",  # 旧版输入框/标签底色
    "#131f31",  # 旧版卡片色
})


def sanitize(cfg):
    """按字段校验并回退，返回全新字典（深拷贝）。未知字段原样保留。"""
    clean = deepcopy(DEFAULTS)
    if not isinstance(cfg, dict):
        return clean

    for key, value in cfg.items():
        if key not in DEFAULTS:
            clean[key] = deepcopy(value)

    opacity = cfg.get("opacity")
    if _is_number(opacity) and OPACITY_MIN <= float(opacity) <= OPACITY_MAX:
        clean["opacity"] = float(opacity)

    bg_color = cfg.get("bg_color")
    if _is_valid_hex_color(bg_color):
        clean["bg_color"] = bg_color

    if "geometry" in cfg:
        clean["geometry"] = _sanitize_geometry(cfg.get("geometry"))

    background_image = cfg.get("background_image")
    if isinstance(background_image, str):
        clean["background_image"] = background_image
    if isinstance(cfg.get("default_background"), bool):
        clean["default_background"] = cfg["default_background"]

    for key in RATIO_KEYS:
        if key in cfg:
            clean[key] = clamp_ratio(cfg.get(key), DEFAULTS[key])

    return clean


def load_gui_config(path):
    """读取配置；文件缺失、空文件、非法 JSON 或结构损坏都回退默认值，绝不抛异常。"""
    if not path or not isinstance(path, (str, bytes, os.PathLike)):
        return deepcopy(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return deepcopy(DEFAULTS)
    if not content.strip():
        return deepcopy(DEFAULTS)
    try:
        data = json.loads(content)
    except ValueError:
        return deepcopy(DEFAULTS)
    return sanitize(migrate(data))


def save_gui_config(path, cfg):
    """先 sanitize 再原子写盘（临时文件 + os.replace），UTF-8 + LF。

    写盘失败时删除临时文件、保留原文件，并把异常上抛给调用方提示。
    注意：绝不修改调用方传进来的字典 —— 早先的实现会就地写入 config_version 与
    opacity，调用方若传的是模块级 DEFAULTS，就会把全局默认值一起改掉。
    """
    clean = sanitize(migrate(deepcopy(cfg) if isinstance(cfg, dict) else cfg))
    clean["config_version"] = CONFIG_VERSION
    # 同步一份旧字段，兼容尚未升级的读取方
    clean["opacity"] = round(1.0 - float(clean.get("transparency", 0.0)), 4)
    directory = os.path.dirname(os.path.abspath(path)) or os.getcwd()
    os.makedirs(directory, exist_ok=True)
    handle_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix="gui_cfg_")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(clean, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def ensure_migrated(path):
    """读取并**落盘**迁移结果，返回可用的配置。

    `migrate()` 只在读取时换算，不写盘 —— 于是老用户的窗口会以旧值渲染一整个会话，
    直到他哪天改了外观才顺手写回。首次启动时调用本函数，让迁移立刻生效。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return load_gui_config(path)
    config = load_gui_config(path)
    needs_write = False
    if isinstance(raw, dict):
        needs_write = "transparency" not in raw or int(raw.get("config_version") or 0) < CONFIG_VERSION
        if str(raw.get("bg_color", "")).strip().lower() in LEGACY_DARK_DEFAULTS:
            needs_write = True
    if needs_write:
        try:
            save_gui_config(path, config)
        except OSError:
            pass
    return config
