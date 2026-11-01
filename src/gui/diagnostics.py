"""记录 GUI 槽异常，避免 PyQt 默认异常处理直接终止整个控制台。"""
import faulthandler
import functools
import inspect
import sys
import traceback
from pathlib import Path

_crash_file = None
_folder = None


def install(folder, notify):
    global _crash_file, _folder
    folder = Path(folder)
    _folder = folder
    folder.mkdir(parents=True, exist_ok=True)
    _crash_file = open(folder / 'native-crash.log', 'a', encoding='utf-8', buffering=1)
    faulthandler.enable(_crash_file, all_threads=True)

    def report(kind, value, tb):
        if issubclass(kind, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(kind, value, tb)
            return
        path = folder / 'ui-errors.log'
        with path.open('a', encoding='utf-8') as output:
            traceback.print_exception(kind, value, tb, file=output)
        notify('操作未完成，错误已记录', f'请将此文件用于排查：\n{path}')

    sys.excepthook = report


def log_exception(where: str = "") -> Path | None:
    """把当前异常写进 ui-errors.log，返回文件路径（写不进去就返回 None）。"""
    folder = _folder
    if folder is None:
        import os
        folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WhaleGirlQQ" / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "ui-errors.log"
        with path.open("a", encoding="utf-8") as output:
            if where:
                output.write(f"\n---- {where} ----\n")
            traceback.print_exc(file=output)
        return path
    except Exception:
        return None


def _page_notice(instance, text: str) -> None:
    """尽量把"这一块显示失败"写到页面上；没有提示控件就静默。"""
    method = getattr(instance, "set_notice", None)
    if callable(method):
        try:
            method(text, ok=False)
            return
        except Exception:
            pass
    label = getattr(instance, "notice", None)
    if label is not None:
        try:
            label.setText(text)          # QLabel 路径
            return
        except Exception:
            pass
    try:
        instance.setWindowTitle(f"{instance.windowTitle()}（部分内容显示失败）")
    except Exception:
        pass


def safe_slot(func=None, *, label: str = ""):
    """把"由定时器/信号触发的刷新入口"包一层，异常只留痕 + 页面提示。

    为什么需要它：Qt 槽里未捕获的异常，**装了自定义 excepthook 会弹"操作未完成"、
    没装（`--self-test` 路径）会让整个进程 abort（0xC0000409）**。
    逐个页面加 try/except 容易漏，所以给这些入口统一套一层：
    任何一处漏改都不会再变成弹窗或进程崩溃。

    **参数裁剪（踩过的坑）**：PyQt 连接信号时，如果槽函数声明了固定参数，
    多出来的信号参数会被自动丢掉（`clicked(bool)` 接 `def refresh(self)` 是合法的）。
    一旦用 ``*args`` 包装，PyQt 就会把信号参数**全传进来** →
    ``refresh() takes 1 positional argument but 2 were given``（真机截图出现过）。
    所以这里按原函数的签名把多余参数裁掉，保持"包装前后行为一致"。

    用法::

        @safe_slot
        def refresh(self): ...

        @safe_slot(label="控制台")
        def populate(self, data): ...
    """
    def wrap(function):
        name = label or f"{function.__qualname__}"
        try:
            parameters = inspect.signature(function).parameters.values()
            max_positional = sum(1 for item in parameters
                                 if item.kind in (item.POSITIONAL_ONLY,
                                                  item.POSITIONAL_OR_KEYWORD))
            has_varargs = any(item.kind == item.VAR_POSITIONAL for item in parameters)
        except (TypeError, ValueError):
            max_positional, has_varargs = 0, True

        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if not has_varargs and len(args) > max_positional:
                args = args[:max_positional]      # 丢掉 Qt 信号多带的参数
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                path = log_exception(name)
                detail = (f"{name} 显示失败（已跳过这一块）：{type(exc).__name__}: {exc}"
                          + (f"；详情见 {path}" if path else ""))
                if args:
                    _page_notice(args[0], detail)
                try:
                    print(f"[gui] {detail}")
                except Exception:
                    pass
                return None

        return wrapper

    return wrap(func) if callable(func) else wrap
