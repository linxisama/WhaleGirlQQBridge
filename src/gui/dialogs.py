"""二次确认与提示弹窗：中文按钮 + 与应用一致的浅色外观。

系统自带的 ``QMessageBox`` 在这台机器上有两个毛病：

- 按钮文案是英文 ``Yes`` / ``No``（Qt 跟随系统区域，不跟随应用的界面语言）；
- 标题栏跟随 Windows 深色主题，弹在浅色界面中间就是一条黑边（截图里的"二层窗口"）。

这里统一收口成中文按钮 + 浅色标题栏，并且**危险操作的默认按钮是"取消"**：
用户直接按回车不会误删东西。
"""
from __future__ import annotations

import ctypes

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QVBoxLayout)

DWMWA_USE_IMMERSIVE_DARK_MODE = 20

_QSS = (
    "QMessageBox, QMessageBox QWidget { background: #F4F6F9; color: #26364B; }"
    "QMessageBox QLabel, QMessageBox QLabel#qt_msgbox_label,"
    " QMessageBox QLabel#qt_msgbox_informativelabel { color: #26364B;"
    " background: transparent; font-size: 13px; }"
    "QMessageBox QPushButton { background: #2FA8A0; color: #FFFFFF; border: 0;"
    " border-radius: 7px; padding: 8px 18px; min-width: 84px; font-weight: bold; }"
    "QMessageBox QPushButton:hover { background: #218E88; }"
    "QMessageBox QPushButton:disabled { background: #E1E8F0; color: #68778C; }"
)
_DANGER_QSS = (
    "QMessageBox QPushButton#danger_accept { background: #C0504D; color: #FFFFFF; }"
    "QMessageBox QPushButton#danger_accept:hover { background: #A63F3C; }"
)
_ICONS = {
    "info": QMessageBox.Icon.Information,
    "warning": QMessageBox.Icon.Warning,
    "error": QMessageBox.Icon.Critical,
    "question": QMessageBox.Icon.Question,
}


def _light_title_bar(widget) -> None:
    """把弹窗标题栏强制成浅色，和浅色界面统一；失败不影响功能。"""
    from .qt_locale import light_palette
    from PyQt6.QtCore import Qt
    widget.setPalette(light_palette())
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    widget.setWindowOpacity(1.0)
    try:
        hwnd = int(widget.winId())
        value = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


_KIND_COLORS = {
    "info": ("#2F80ED", "i"),
    "warning": ("#D99100", "!"),
    "error": ("#C0392B", "×"),
    "question": ("#2FA8A0", "?"),
}


class _MessageDialog(QDialog):
    """自绘提示/确认框：不依赖系统深色 QMessageBox 的底色。"""

    def __init__(self, parent, title: str, text: str, *, detail: str = "",
                 kind: str = "info", ok_text: str = "确定",
                 cancel_text: str = "", danger: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMaximumWidth(620)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QDialog { background: #F4F6F9; }"
            "QLabel { color: #1F2733; background: transparent; font-size: 13px; }"
            "QLabel#msg_detail { color: #5F6B7A; font-size: 12px; }"
            "QPushButton { background: #E7ECF3; color: #1F2733; border: 0;"
            " border-radius: 7px; padding: 8px 18px; min-width: 84px; font-weight: bold; }"
            "QPushButton:hover { background: #DCE4EE; }"
            "QPushButton#msg_accept { background: #2FA8A0; color: #FFFFFF; }"
            "QPushButton#msg_accept:hover { background: #218E88; }"
            "QPushButton#danger_accept { background: #C0504D; color: #FFFFFF; }"
            "QPushButton#danger_accept:hover { background: #A63F3C; }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(14)

        row = QHBoxLayout()
        row.setSpacing(14)
        color, glyph = _KIND_COLORS.get(kind, _KIND_COLORS["info"])
        icon = QLabel(glyph)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(36, 36)
        icon.setStyleSheet(
            f"background: {color}; color: #FFFFFF; border-radius: 18px;"
            " font-size: 20px; font-weight: bold;")
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(8)
        message = QLabel(text)
        message.setWordWrap(True)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(message)
        if detail:
            extra = QLabel(detail)
            extra.setObjectName("msg_detail")
            extra.setWordWrap(True)
            body.addWidget(extra)
        row.addLayout(body, 1)
        outer.addLayout(row)

        buttons = QDialogButtonBox()
        if cancel_text:
            cancel = buttons.addButton(cancel_text, QDialogButtonBox.ButtonRole.RejectRole)
            cancel.setObjectName("msg_cancel")
        accept = buttons.addButton(ok_text, QDialogButtonBox.ButtonRole.AcceptRole)
        accept.setObjectName("danger_accept" if danger else "msg_accept")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if danger and cancel_text:
            cancel.setDefault(True)
            accept.setDefault(False)
        else:
            accept.setDefault(True)
        outer.addWidget(buttons)
        _light_title_bar(self)

    def exec(self) -> int:
        return super().exec()


def ask_confirm(parent, title: str, text: str, *, detail: str = "", ok_text: str = "确定",
                cancel_text: str = "取消", kind: str = "warning", danger: bool = False) -> bool:
    """二次确认；返回是否点了确认按钮。

    ``danger=True`` 时确认按钮用红色，并且**默认按钮是"取消"**——
    删除类操作不该靠一路回车就能完成。
    """
    dialog = _MessageDialog(
        parent, title, text, detail=detail, kind=kind, ok_text=ok_text,
        cancel_text=cancel_text, danger=danger)
    return dialog.exec() == QDialog.DialogCode.Accepted


def tell(parent, title: str, text: str, *, kind: str = "info", ok_text: str = "知道了") -> None:
    """单按钮提示（成功 / 失败 / 警告都走这里）。"""
    _MessageDialog(parent, title, text, kind=kind, ok_text=ok_text).exec()


def _choice_box(parent, title: str, text: str, *, detail: str = "",
                options: list | None = None, kind: str = "question"):
    """构建多选一对话框并返回 ``(box, {按钮: 返回值})`` —— 拆出来便于离线测量布局。"""
    entries = [(str(label), value) for label, value in (options or []) if str(label)]
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    box.setIcon(_ICONS.get(kind, QMessageBox.Icon.Question))
    buttons = {}
    for label, value in entries:
        role = (QMessageBox.ButtonRole.RejectRole if not str(value)
                else QMessageBox.ButtonRole.AcceptRole)
        buttons[box.addButton(label, role)] = value
    # 按最长的按钮文字算最小宽度：宁可对话框宽一点，也不要把"只识别未识别的"截成"别未识别的"
    widest = 0
    for button in buttons:
        try:
            widest = max(widest, button.fontMetrics().horizontalAdvance(button.text()) + 34)
        except Exception:
            continue
    min_width = max(96, min(220, widest))
    box.setStyleSheet(_QSS + f"\nQMessageBox QPushButton {{ min-width: {min_width}px; }}")
    cancel = next((button for button, value in buttons.items() if not str(value)), None)
    if cancel is not None:
        box.setDefaultButton(cancel)     # 默认落在"取消"上，避免一路回车就把钱花了
        box.setEscapeButton(cancel)
    _light_title_bar(box)
    return box, buttons


def ask_choice(parent, title: str, text: str, *, detail: str = "",
               options: list | None = None, kind: str = "question"):
    """多选一对话框：``options`` 是 ``[(按钮文字, 返回值), …]``，返回被选中项的返回值。

    用途：需要"二选一以上"的确认（例如表情库一键识别要区分"只补空缺"和"全部重来"）。
    取消/Esc 返回空串，调用方按"用户放弃"处理即可。

    **按钮不截断**：QMessageBox 会按"最长的正文"限制整体宽度，按钮文字一长就被省略成
    "别未识别的（7"（真机截图）。这里给按钮加最小宽度，并让按钮至少放得下自己的文字。
    """
    entries = [(str(label), value) for label, value in (options or []) if str(label)]
    if not entries:
        return ""
    box, buttons = _choice_box(parent, title, text, detail=detail,
                               options=entries, kind=kind)
    box.exec()
    clicked = box.clickedButton()
    if clicked is None:
        return ""
    value = buttons.get(clicked, "")
    # QMessageBox 关闭(Esc)时 clickedButton 可能是 None，也可能是取消按钮 —— 两种都当放弃
    return "" if value == "" else str(value)

def ask_text(parent, title: str, label: str, *, default: str = "",
             ok_text: str = "确定", cancel_text: str = "取消") -> tuple:
    """单行输入（替代 QInputDialog：原生版在深色主题下是黑底）。

    返回 ``(文本, 是否确定)``；取消时文本为空串。
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(360)
    dialog.setStyleSheet(
        "QDialog { background: #F4F6F9; }"
        "QLabel { color: #26364B; font-size: 13px; }"
        "QLineEdit { background: #FFFFFF; color: #1F2733; border: 1px solid #D3DAE3;"
        " border-radius: 7px; padding: 8px 10px; }"
        "QPushButton { background: #2FA8A0; color: #FFFFFF; border: 0; border-radius: 7px;"
        " padding: 8px 18px; min-width: 84px; font-weight: bold; }"
        "QPushButton:hover { background: #218E88; }")
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 18, 20, 16)
    layout.setSpacing(10)
    caption = QLabel(label)
    caption.setWordWrap(True)
    layout.addWidget(caption)
    field = QLineEdit(default)
    layout.addWidget(field)
    buttons = QDialogButtonBox()
    cancel = buttons.addButton(cancel_text, QDialogButtonBox.ButtonRole.RejectRole)
    accept = buttons.addButton(ok_text, QDialogButtonBox.ButtonRole.AcceptRole)
    cancel.setStyleSheet("background: #E7ECF3; color: #1F2733;")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    field.setFocus()
    field.selectAll()
    _light_title_bar(dialog)
    ok = dialog.exec() == QDialog.DialogCode.Accepted
    return (field.text(), ok)
