"""把 sana 图标库分配给程序各处，并把窗口/exe 图标转成 .ico。

设计：
1. 用**固定种子**的随机打乱挑选图标 —— 满足"随机、不要重复太多"，
   同时保证可复现（同一个种子永远得到同一套分配，重建不会突然换脸）。
2. 每个位置拿到**不同**的图标（14 个图标 ≥ 需要的 8 个位置，足够不重复）。
3. ICO 用 PyQt6 的 QImageWriter 写（构建环境没有 Pillow，但 Qt 自带 ico 写入）。

用法：python tools/setup_icons.py
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path(r"C:\Users\16522\Pictures\sana\icons")
ICON_DIR = ROOT / "assets" / "icons"      # 复制进来的图标（随包发布）
ICO_PATH = ROOT / "icon.ico"
MAP_PATH = ROOT / "assets" / "icon-map.json"
SEED = 20261013                            # 固定种子：随机但可复现
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
# 需要图标的位置（键名会被界面与构建脚本读取）
# 尽量覆盖界面里每一处曾经用 emoji 的地方，避免"只有标签页换了、里面还是 emoji"。
SLOTS = [
    "app",             # exe / 窗口 / 任务栏 / 标题栏
    "tab_console",     # 标签页
    "tab_settings",
    "tab_persona",
    "tab_plugin",
    "tab_appearance",
    "dialog",          # 插件设置对话框
    "card_messages",   # 「消息与回复」卡片标题
    "card_events",     # 「运行记录」卡片标题
    "card_appearance", # 「外观设置」卡片标题
    "card_persona_list",   # 「人设列表」
    "card_persona_edit",   # 「人设内容」
    "btn_save",        # 保存类按钮
    "btn_reload",      # 刷新/重载
    "btn_new",         # 新建
    "btn_delete",      # 删除
    "btn_apply",       # 应用设置
    "notice",          # 预留
]


def pick_icons() -> dict:
    """给每个槽位分配一张图标。

    槽位比图标多，所以必然要重复。用**洗牌后循环**而不是纯随机采样：
    - 每一轮把所有图标打乱后依次发完，才进入下一轮 → 每张图都被用满一轮；
    - 因此**不会出现某张图被反复用、而另一张一次没用**的情况；
    - 并且相邻槽位不会撞同一张（同一轮内不重复）。
    种子固定，所以重建得到同一套分配。
    """
    files = sorted(p for p in SOURCE.glob("*.png") if p.is_file())
    if not files:
        raise SystemExit(f"图标目录里没有 PNG：{SOURCE}")

    rng = random.Random(SEED)
    order: list = []
    while len(order) < len(SLOTS):
        batch = files[:]
        rng.shuffle(batch)
        order.extend(batch)

    mapping = {}
    for index, slot in enumerate(SLOTS):
        choice = order[index]
        # 与上一个槽位撞图时，往后换一张，避免相邻重复
        if index and choice.name == mapping[SLOTS[index - 1]]:
            for candidate in order[index + 1:]:
                if candidate.name != mapping[SLOTS[index - 1]]:
                    choice = candidate
                    break
        mapping[slot] = choice.name
    return mapping


def _png_bytes(source_png: Path, side: int, pad: bool = True) -> bytes:
    """把源图缩放成 side×side 的 PNG 字节。

    ``pad=True`` 时先补成方图再缩放（exe 图标是方形，非方图直接缩放会拉变形）。
    """
    from PyQt6.QtCore import QBuffer, QSize, Qt
    from PyQt6.QtGui import QImage, QPainter

    image = QImage(str(source_png))
    if image.isNull():
        raise SystemExit(f"读不出图片：{source_png}")
    if pad:
        biggest = max(image.width(), image.height())
        square = QImage(biggest, biggest, QImage.Format.Format_ARGB32)
        square.fill(Qt.GlobalColor.transparent)
        painter = QPainter(square)
        painter.drawImage((biggest - image.width()) // 2,
                          (biggest - image.height()) // 2, image)
        painter.end()
        image = square
    image = image.scaled(QSize(side, side), Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def write_ico(source_png: Path) -> None:
    """写**多尺寸** .ico。

    Qt 的 QImageWriter("ico") 只写单尺寸，而 Windows 会按场合挑尺寸
    （资源管理器 16/48、任务栏 32、Alt+Tab 256）；单尺寸会让小图标被压缩得很糊。
    这里按 ICO 容器格式自己封装多张 PNG —— 这是标准做法（Vista 以后的 ICO 允许内嵌 PNG）。
    """
    import struct

    entries = []
    for side in ICO_SIZES:
        entries.append((side, _png_bytes(source_png, side)))

    header = struct.pack("<HHH", 0, 1, len(entries))          # reserved, type=icon, count
    offset = len(header) + 16 * len(entries)
    directory = b""
    payload = b""
    for side, data in entries:
        # 宽高 256 在 ICO 目录里用 0 表示
        dimension = 0 if side >= 256 else side
        directory += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32,
                                 len(data), offset)
        payload += data
        offset += len(data)
    ICO_PATH.write_bytes(header + directory + payload)


def main() -> int:
    if not SOURCE.is_dir():
        print(f"找不到图标目录：{SOURCE}")
        return 1

    mapping = pick_icons()
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for slot, name in mapping.items():
        shutil.copy2(SOURCE / name, ICON_DIR / name)
    MAP_PATH.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    write_ico(SOURCE / mapping["app"])

    print(f"已复制 {len(mapping)} 个图标到 {ICON_DIR.relative_to(ROOT)}")
    for slot, name in mapping.items():
        print(f"  {slot:16} <- {name}")
    print(f"exe/窗口图标：{ICO_PATH.name}（{len(ICO_SIZES)} 档尺寸，来自 {mapping['app']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
