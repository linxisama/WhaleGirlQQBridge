"""Qt 界面语言与文件对话框策略。

**真机问题**（用户截图，1.6.9）：安全模式下设置了 ``AA_DontUseNativeDialogs``，
于是「选择背景图片」等所有文件对话框都退回 **Qt 自绘版**；偏偏 Qt 的 zh_CN
翻译没加载、颜色又跟随系统主题，结果弹出一个**英文 + 深色**的窗口，
和中文浅色界面完全不一致，看起来像"别的软件"。

策略（2026-10 用户真机反馈后定为一条）：

1. **默认永远用系统对话框**（Windows 上自带中文、浅色、可点选、和资源管理器一致）；
2. 只有排查用的环境变量 ``WHALEGIRL_NATIVE_DIALOGS=0`` 才退回 Qt 自绘，
   并且那时**必须**加载 zh_CN 翻译 + 套用浅色配色。

以前安全模式会退回自绘（怕远程桌面里原生框拖垮进程）。实测代价是用户拿到一个
"点不动、认不出"的窗口：列表行没有系统选择行为、没有快速访问栏、没有新建文件夹
和搜索框。所以安全模式不再改变这个选择。

翻译文件 ``qtbase_zh_CN.qm`` 必须随包一起发出去（PyInstaller 不一定会收集），
所以 ``ensure_chinese()`` 会把找过的路径都记下来，打包自检里能直接看到结果。
"""
from __future__ import annotations

import os
from pathlib import Path

_QM_NAME = "qtbase_zh_CN.qm"
_loaded: dict = {"loaded": False, "path": "", "reason": "", "searched": []}


def candidate_dirs(bundle: Path | None = None) -> list[Path]:
    """按可能性列出翻译目录：Qt 自带 → PyQt6 包内 → 打包解包目录 → 环境变量覆盖。"""
    dirs: list[Path] = []
    override = os.environ.get("WHALEGIRL_QT_TRANSLATIONS", "").strip()
    if override:
        dirs.append(Path(override))
    try:
        from PyQt6.QtCore import QLibraryInfo
        dirs.append(Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)))
    except Exception:
        pass
    try:
        import PyQt6
        dirs.append(Path(PyQt6.__file__).resolve().parent / "Qt6" / "translations")
    except Exception:
        pass
    if bundle:
        dirs.append(Path(bundle) / "Qt6" / "translations")
        dirs.append(Path(bundle) / "translations")
    seen, result = set(), []
    for item in dirs:
        key = str(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def find_translation(bundle: Path | None = None) -> Path | None:
    for folder in candidate_dirs(bundle):
        candidate = folder / _QM_NAME
        if candidate.is_file():
            return candidate
    return None


def ensure_chinese(app, bundle: Path | None = None) -> dict:
    """给 QApplication 装上 Qt 自带的简体中文翻译；返回可诊断的结果。

    装完 Qt 自带的那份之后，再挂一层**兜底翻译**（``install_fallback``）：
    Qt 6.11 的文件对话框标签在源码里是 ``&Look in:`` / ``Files of &type:``，
    而 qtbase_zh_CN.qm 里的键没有 ``&``，匹配不上就会回落英文 —— 用户截图里
    「选择背景图片」正是这两个标签还是英文。
    """
    global _loaded
    if _loaded.get("loaded"):
        return dict(_loaded)
    searched = [str(item) for item in candidate_dirs(bundle)]
    result = {"loaded": False, "path": "", "reason": "", "searched": searched}
    try:
        from PyQt6.QtCore import QTranslator
        path = find_translation(bundle)
        if path is None:
            result["reason"] = f"没找到 {_QM_NAME}（打包时要把 Qt6/translations 一起带上）"
        else:
            translator = QTranslator(app)          # 挂在 app 上，随 app 一起销毁
            if translator.load(str(path)):
                app.installTranslator(translator)
                app._whalegirl_translator = translator    # 防被回收
                result.update(loaded=True, path=str(path))
            else:
                result["reason"] = f"{path} 加载失败"
    except Exception as exc:                        # 翻译只是锦上添花，绝不因此起不来
        result["reason"] = f"{type(exc).__name__}: {exc}"
    result["fallback"] = install_fallback(app)
    _loaded = dict(result)
    return dict(result)


# Qt 自带翻译在这个版本里对不上的串（键带不带 & 的差别）。
# 只覆盖文件对话框，避免抢掉 Qt 其它更合适的译文。
FALLBACK_TEXT = {
    "Look in:": "位置：",
    "&Look in:": "位置：",
    "Files of type:": "文件类型：",
    "Files of &type:": "文件类型：",
    "File &name:": "文件名称：",
    "File name:": "文件名称：",
    "&Open": "打开",
    "&Save": "保存",
    "Cancel": "取消",
    "&Cancel": "取消",
    "Back": "后退",
    "Parent Directory": "上级目录",
    "Create New Folder": "新建文件夹",
    "List View": "列表视图",
    "Detail View": "详细信息",
}
_FALLBACK_CONTEXTS = ("QFileDialog", "QFileDialogBase", "QFileSystemModel")


def fallback_translator_class():
    """构造兜底翻译器类（延迟到运行时，避免模块导入就依赖 QtWidgets）。"""
    from PyQt6.QtCore import QTranslator

    class _Fallback(QTranslator):
        def translate(self, context, source, disambiguation=None, n=-1):  # noqa: N802
            if context in _FALLBACK_CONTEXTS and source in FALLBACK_TEXT:
                return FALLBACK_TEXT[source]
            # 必须返回 None（= null QString）而不是空字符串：Qt 的查找循环按
            # `if (!result.isNull()) return result;` 判定，Python 里返回 "" 会造出一个
            # "空但非 null" 的 QString，于是**后面的官方翻译全被挡住**
            # （实测：装了兜底翻译后，QMessageBox 的"确定/取消"变回英文、Qt 自带译文全失效）。
            return None

        def isEmpty(self):  # noqa: N802
            return False

    return _Fallback


def install_fallback(app) -> bool:
    """装上兜底翻译器；已装过就返回 True（幂等）。"""
    if getattr(app, "_whalegirl_fallback", None) is not None:
        return True
    try:
        translator = fallback_translator_class()(app)
        app.installTranslator(translator)
        app._whalegirl_fallback = translator
        return True
    except Exception:
        return False


def translation_status() -> dict:
    """最近一次 ensure_chinese() 的结果（自检 / 诊断用）。"""
    return dict(_loaded or {})


def should_use_native_dialogs(safe_mode: bool = False, env: dict | None = None) -> bool:
    """要不要用系统对话框 —— 默认永远要。

    ``safe_mode`` 只为兼容既有调用点（``qq_gui.main``、打包自检）保留，
    **不再改变结果**：以前安全模式退回 Qt 自绘（理由是"远程桌面 + 多个虚拟显示
    适配器"里原生框可能拖垮进程），换来的却是一个用户根本点不动的自绘文件框——
    列表行没有系统的选择行为、没有快速访问栏、没有新建文件夹和搜索框，用户原话
    「文件的选择窗口不对劲，这个不能点击选择」。原生框由 Windows 自己本地化，
    中文、可点选、和资源管理器一致，所以常规路径必须出系统框。

    ``WHALEGIRL_NATIVE_DIALOGS=1/true/yes/on`` 与 ``0/false/no/off`` 仍可强制覆盖本函数。
    注意它只管这一层：``qq_gui.main`` 在拿到 False 之后还会再看配置
    ``native_file_dialogs``（默认 True）并把原生打开，所以真要退回自绘后备
    （``file_dialogs._styled``），得在配置里把 ``native_file_dialogs`` 设为 false，
    或在该次调用上写 ``force_light=True``。
    """
    env = os.environ if env is None else env
    override = str(env.get("WHALEGIRL_NATIVE_DIALOGS", "")).strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return True


def light_palette():
    """给 Qt 自绘对话框用的浅色配色（与主界面一致，不受系统深色影响）。"""
    from PyQt6.QtGui import QColor, QPalette
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#F4F6F9"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#26364B"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#EEF6FA"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#26364B"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#EEF6FA"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#26364B"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2E9E93"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#26364B"))
    return palette
