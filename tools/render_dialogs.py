r"""把"添加插件"弹窗渲染成 PNG，用于肉眼核对样式（不依赖真实显示器）。

用法：<venv>\Scripts\python.exe tools/render_dialogs.py [输出目录]

注意：离线渲染环境**没有字体**，中文会显示成方框 —— 这是渲染环境限制，
不是界面 bug。看的是**布局、配色、控件状态**是否正确。
"""
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QSize, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
import qq_gui  # noqa: E402

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / ".tmp" / "dialog-shots")
OUT.mkdir(parents=True, exist_ok=True)


def shot(widget, name):
    widget.show()
    app.processEvents()
    path = OUT / f"{name}.png"
    widget.grab().save(str(path), "PNG")
    print(f"  已保存 {path}")


PREVIEW = ('from src.plugins import FieldSchema, PluginBase, PluginMeta\n\n\n'
           'class MyPlugin(PluginBase):\n'
           '    META = PluginMeta(\n'
           '        id="my_plugin",\n'
           '        name="我的插件",\n'
           '        config_schema={"enabled": FieldSchema(type="bool", label="启用")},\n'
           '        default_settings={"enabled": False},\n'
           '    )\n')

# 1) 空白状态
dialog = qq_gui.PluginInstallDialog()
dialog.resize(QSize(760, 660))
shot(dialog, "1-空白")

# 2) 检查通过
dialog.url.setText("https://example.com/my_plugin.py")
dialog.plugin_id.setText("my_plugin")
dialog.result.setTextFormat(Qt.TextFormat.PlainText)
dialog.result.setText(
    "检查通过，可以安装。（来源：网址）\n"
    "  类型：单个 .py 文件\n"
    "  实际来源：https://example.com/my_plugin.py\n"
    "  插件标识：my_plugin\n"
    "  大小：2 KB，约 42 行\n"
    "  确认无误后再点「确认安装」。")
dialog.preview.setPlainText(PREVIEW)
dialog.btn_install.setEnabled(True)
shot(dialog, "2-检查通过")

# 3) 检查未通过（安全闸门）
dialog.result.setTextFormat(Qt.TextFormat.RichText)
dialog.result.setText(
    "<b>安全检查未通过</b>（来源：本地文件），不允许安装：<br>"
    "&nbsp;&nbsp;· 含被禁止的用法：动态执行代码 eval<br>"
    "&nbsp;&nbsp;· 含被禁止的用法：启动子进程 subprocess")
dialog.btn_install.setEnabled(False)
shot(dialog, "3-检查未通过")

# 4) 插件设置弹窗（同一套样式基座）
from src.plugins.manager import PluginManager  # noqa: E402

manager = PluginManager(pathlib.Path(tempfile.mkdtemp()))
entry = next(p for p in manager.list_plugins() if p["id"] == "pixiv_illust")
config = qq_gui.PluginConfigDialog(entry)
config.resize(QSize(720, 620))
shot(config, "4-插件设置")

print()
print("样式表是否套上：")
print("  PluginInstallDialog:", bool(dialog.styleSheet()))
print("  PluginConfigDialog :", bool(config.styleSheet()))
