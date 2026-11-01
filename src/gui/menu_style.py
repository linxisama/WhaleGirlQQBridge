"""弹出层（QMenu / QToolTip / 无父窗口弹窗）的统一浅色样式。

为什么单独成模块：QMenu、QToolTip、QMessageBox(None, ...) 都是**独立顶层窗口**，
主窗口上的 STYLESHEET 继承不到它们；系统是深色模式时它们用默认深色配色，
看起来就是"一块黑框 + 暗色文字"。这里把菜单规则集中成一份可测的字符串，
qq_gui 的 STYLESHEET / 应用级样式、托盘菜单、桌宠菜单都从同一处取，避免各写一遍。

模块级不 import PyQt6：没有 Qt 的环境也能 import 并拿到样式字符串。
"""

DEFAULT_THEME = {
    'card': '#FFFFFF',
    'text': '#1F2733',
    'muted': '#5F6B7A',
    'border': '#D3DAE3',
    'accent': '#2FA8A0',
    'accent_light': '#E2F5F2',
}


def _theme(**theme) -> dict:
    """主题缺省值 + 调用方覆盖；空值忽略，免得传 None 把规则写坏。"""
    merged = dict(DEFAULT_THEME)
    for key, value in theme.items():
        if value:
            merged[key] = value
    return merged


def menu_stylesheet(**theme) -> str:
    """QMenu 的浅色规则（背景/文字/选中/禁用/分隔线）。

    background-color 而不是 background：主窗口那套透明度替换规则
    （qq_gui.MainWindow._apply_panel_transparency 里的 .replace）只匹配
    `background: <CARD>;`，用 background-color 让弹出菜单始终不透明、字看得清。
    """
    t = _theme(**theme)
    return f"""
QMenu {{ background-color: {t['card']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ background-color: transparent; color: {t['text']}; padding: 7px 18px; min-width: 150px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {t['accent_light']}; color: {t['text']}; }}
QMenu::item:disabled {{ color: {t['muted']}; }}
QMenu::separator {{ height: 1px; background-color: {t['border']}; margin: 4px 8px; }}
"""


def style_menu(menu) -> None:
    """给一个 QMenu 装上统一浅色样式。幂等：重复调用结果不变；传 None 或非菜单对象不动手。"""
    if menu is None or not hasattr(menu, 'setStyleSheet'):
        return
    try:
        menu.setStyleSheet(menu_stylesheet())
    except Exception:          # 已经销毁的 C++ 对象
        return
