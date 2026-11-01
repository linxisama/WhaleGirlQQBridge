"""文件对话框统一入口：常规路径一律用**系统原生**文件框。

为什么不让各处直接调 ``QFileDialog.getOpenFileName``：
- 要能一处兜住排查用的自绘后备（``force_light=True`` 或
  ``WHALEGIRL_NATIVE_DIALOGS=0``），那条路径必须自绘 + 浅色 + 中文标签；
- 常规路径出系统框后什么都不用操心：中文、可点选、和资源管理器一致，
  由 Windows 自己本地化（用户要的就是这个）。

历史：安全模式曾经打开 ``AA_DontUseNativeDialogs``（怕远程桌面里原生框崩溃），
结果用户拿到"点不动、认不出"的自绘窗口。现在 ``qt_locale.should_use_native_dialogs``
默认永远返回 True，自绘只剩排查用途。
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QColorDialog, QFileDialog, QTreeView

from .qt_locale import light_palette, should_use_native_dialogs


def native_dialogs_enabled() -> bool:
    """当前进程是否使用系统对话框（由 qq_gui.main() 按是否安全模式设置）。"""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return True
    try:
        return not app.testAttribute(
            __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    except Exception:
        return True


def _styled(dialog: QFileDialog, accept_text: str = "打开") -> QFileDialog:
    """**仅排查/测试用的自绘后备**：把 Qt 对话框统一成中文 + 浅色。

    常规路径不经过这里——``open_file``/``open_files``/``save_file``/``choose_dir``
    在 ``native_dialogs_enabled()`` 为真时直接调系统的静态方法。走到这里的只有
    ``force_light=True`` 和 ``WHALEGIRL_NATIVE_DIALOGS=0`` 这两条排查路径，
    以及 ``src/gui/smoke.py`` 的自检。

    光装 Qt 的 zh_CN 翻译还不够：Qt 6.11 里这两个标签的键是
    ``&Look in:`` / ``Files of &type:``，而翻译文件里的键没有 ``&``，
    匹配不上就显示英文（用户截图里的「Look in:」「Files of type:」）。
    这里用官方 API 直接给定中文标签，任何 Qt 版本都不会再漏。
    """
    # 自绘后备必须在创建窗口句柄前关掉原生，否则 Windows 深色原生文件框会抢先接管，
    # 浅色 QSS 与手写中文标签就全白做了。
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setPalette(light_palette())
    # Palette 会被主窗口的透明/深色 QSS 覆盖，必须同时隔离文件列表与输入框的绘制样式。
    dialog.setStyleSheet('''
        QFileDialog, QFileDialog QWidget { background-color: #f5f7fb; color: #18314d; }
        QFileDialog QAbstractItemView, QFileDialog QLineEdit, QFileDialog QComboBox {
            background-color: #ffffff; color: #18314d; selection-background-color: #2da9a2;
            selection-color: #ffffff; }
        QFileDialog QAbstractItemView::item:selected { background-color: #2da9a2; color: #ffffff; }
        QFileDialog QPushButton { background-color: #e6edf4; color: #18314d; padding: 5px 10px; }
        QFileDialog QPushButton:disabled { color: #8491a0; }
        QFileDialog QHeaderView::section { background-color: #e6edf4; color: #18314d; }
        QFileDialog QToolButton { background-color: #e6edf4; color: #18314d; }
    ''')
    if isinstance(dialog, QFileDialog) and hasattr(dialog, 'setAttribute'):
        dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        dialog.setWindowOpacity(1.0)
        dialog.setAutoFillBackground(True)
    labels = {
        QFileDialog.DialogLabel.LookIn: "位置：",
        QFileDialog.DialogLabel.FileName: "文件名称：",
        QFileDialog.DialogLabel.FileType: "文件类型：",
        QFileDialog.DialogLabel.Accept: accept_text,
        QFileDialog.DialogLabel.Reject: "取消",
    }
    for label, text in labels.items():
        try:
            dialog.setLabelText(label, text)
        except Exception:            # 个别 Qt 版本枚举不全，缺哪个就跳过哪个
            pass
    # Qt 的文件列表“类型”列来自系统 MIME/图标提供器，Qt 翻译文件缺键时会显示
    # File Folder / application/json。隐藏这一列，避免中文界面里夹英文；文件类型
    # 仍由底部筛选器表达。
    try:
        view = dialog.findChild(QTreeView)
        if view is not None:
            view.setColumnHidden(2, True)
    except Exception:
        pass
    return dialog


def _run(dialog: QFileDialog) -> tuple[list[str], str]:
    from .dialogs import _light_title_bar
    if hasattr(dialog, 'winId'):
        _light_title_bar(dialog)
    try:
        if dialog.exec():
            return list(dialog.selectedFiles()), dialog.selectedNameFilter()
        return [], ""
    finally:
        # QFileDialog 持有文件系统模型；反复导入时不要一直挂在主窗下面。
        if hasattr(dialog, 'deleteLater'):
            dialog.deleteLater()


def choose_color(parent, initial: str = "", title: str = "选择颜色") -> QColor:
    """取色：固定浅色自绘对话框。

    系统取色框会跟随 Windows 深色主题变成黑底（用户截图里就是黑底），
    与浅色控制台不搭，所以这里始终自己画。
    """
    dialog = QColorDialog(QColor(initial) if initial else QColor("#ffffff"), parent)
    if title:
        dialog.setWindowTitle(title)
    dialog.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
    dialog.setPalette(light_palette())
    dialog.setStyleSheet('''
        QColorDialog, QColorDialog QWidget { background-color: #f5f7fb; color: #18314d; }
        QColorDialog QLineEdit, QColorDialog QSpinBox {
            background-color: #ffffff; color: #18314d; border: 1px solid #c9d6e2; }
        QColorDialog QPushButton { background-color: #e6edf4; color: #18314d; padding: 5px 10px; }
        QColorDialog QPushButton:default { background-color: #2da9a2; color: #ffffff; }
    ''')
    if dialog.exec():
        return dialog.currentColor()
    return QColor()


def open_file(parent, title: str, directory: str = "", file_filter: str = "",
              force_light: bool = False) -> tuple[str, str]:
    """选单个文件；返回 (路径, 过滤器)，取消返回 ("", "")。

    默认使用系统原生文件框（中文、可点击、和资源管理器一致）；
    force_light=True 只在需要测试或特殊浅色主题时强制自绘。
    """
    if native_dialogs_enabled() and not force_light:
        path, chosen = QFileDialog.getOpenFileName(parent, title, directory, file_filter)
        return path or "", chosen or ""
    dialog = _styled(QFileDialog(parent, title, directory, file_filter))
    dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    files, chosen = _run(dialog)
    return (files[0] if files else ""), chosen


def open_files(parent, title: str, directory: str = "", file_filter: str = "",
               force_light: bool = False) -> tuple[list[str], str]:
    """选多个文件。"""
    if native_dialogs_enabled() and not force_light:
        paths, chosen = QFileDialog.getOpenFileNames(parent, title, directory, file_filter)
        return list(paths or []), chosen or ""
    dialog = _styled(QFileDialog(parent, title, directory, file_filter))
    dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    files, chosen = _run(dialog)
    return files, chosen


def save_file(parent, title: str, path: str = "", file_filter: str = "",
              force_light: bool = False) -> tuple[str, str]:
    """另存为。"""
    if native_dialogs_enabled() and not force_light:
        chosen_path, chosen = QFileDialog.getSaveFileName(parent, title, path, file_filter)
        return chosen_path or "", chosen or ""
    dialog = _styled(QFileDialog(parent, title, path, file_filter), accept_text="保存")
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    files, chosen = _run(dialog)
    return (files[0] if files else ""), chosen


def choose_dir(parent, title: str, directory: str = "", force_light: bool = False) -> str:
    """选目录。"""
    if native_dialogs_enabled() and not force_light:
        return QFileDialog.getExistingDirectory(parent, title, directory) or ""
    dialog = _styled(QFileDialog(parent, title, directory), accept_text="选择文件夹")
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    files, _ = _run(dialog)
    return files[0] if files else ""


__all__ = ["open_file", "open_files", "save_file", "choose_dir", "choose_color",
           "native_dialogs_enabled"]
