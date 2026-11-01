"""原生文件选择框的独立子进程（后端只读它 stdout 上的那行 JSON）。

为什么必须另起进程（2026-10 实测）：Tk/Tcl 的解释器只能在创建它的那个线程里销毁。
后端的 4 条 browse 路由跑在 uvicorn 的工作线程上，以前在 ``asyncio.to_thread`` 里
直接 ``tkinter.Tk()``：第一次调用在工作线程里留下一个 Tcl 解释器，第二次调用回收它时
Tcl 在错误线程里删 async handler，整个后端进程以 ``0x80000003`` 退出，服务端末行是
``Tcl_AsyncDelete: async handler deleted by the wrong thread``。
把 Tk 关进子进程以后它有自己的主线程，父进程只用 ``subprocess.run`` 读一行 JSON，
Tcl 永远不会出现在后端进程里 —— 用户点两下"浏览"不该杀掉整个服务。

协议：子进程 stdout 输出一行 JSON，``{"paths": [...]}`` 或 ``{"error": "..."}``；
用户取消也返回 ``{"paths": []}`` 且退出码 0；非零退出码表示这次弹框不可用。
"""
from __future__ import annotations

__all__ = ['PickerError', 'MODE_FILE', 'MODE_MANY', 'MODE_DIR', 'MODE_FLAGS',
           'pick', 'run_child']

import json
import os
import subprocess
import sys
from pathlib import Path

MODE_FILE = '--pick-file'
MODE_MANY = '--pick-many'
MODE_DIR = '--pick-dir'
MODE_FLAGS = (MODE_FILE, MODE_MANY, MODE_DIR)

# 用户可能对着对话框想很久，所以默认给足；卡死的子进程必须能被收掉，所以有上限。
DEFAULT_TIMEOUT = 300.0
TIMEOUT_ENV = 'WHALEGIRL_PICKER_TIMEOUT'

# 冻结态是窗口程序，拉子进程不该再闪一个控制台黑框。
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class PickerError(RuntimeError):
    """父进程侧的可读失败（调用方一律映射成 HTTP 400，不是 500）。"""


def _parse_args(argv: list) -> tuple:
    """解析子进程命令行，返回 (mode, title, filetypes, error)。"""
    mode = ''
    title = ''
    filetypes = []
    rest = list(argv)
    while rest:
        item = rest.pop(0)
        if item in MODE_FLAGS:
            mode = item
        elif item == '--title' and rest:
            title = rest.pop(0)
        elif item == '--filetypes' and rest:
            try:
                data = json.loads(rest.pop(0))
            except Exception:
                return mode, title, [], '文件类型参数不是合法 JSON'
            if not isinstance(data, list):
                return mode, title, [], '文件类型参数必须是列表'
            for entry in data:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    filetypes.append((str(entry[0]), str(entry[1])))
        else:
            return mode, title, [], f'不认识的参数：{item}'
    if not mode:
        return '', title, [], '缺少选择模式（' + ' / '.join(MODE_FLAGS) + '）'
    return mode, title, filetypes, ''


def _ask(mode: str, title: str, filetypes: list) -> list:
    """在子进程自己的主线程里弹原生框；这里是唯一 import tkinter 的地方。"""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        options = {'title': title}
        if filetypes:
            options['filetypes'] = filetypes
        if mode == MODE_DIR:
            chosen = filedialog.askdirectory(**options)
        elif mode == MODE_MANY:
            chosen = filedialog.askopenfilenames(**options)
        else:
            chosen = filedialog.askopenfilename(**options)
    finally:
        root.destroy()
    if not chosen:
        return []
    if isinstance(chosen, (list, tuple)):
        return [str(item) for item in chosen if str(item or '')]
    return [str(chosen)]


def _emit(payload: dict) -> None:
    """子进程唯一往 stdout 写的东西：一行 JSON。"""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def run_child(argv: list) -> int:
    """子进程主函数：弹框，把结果写成一行 JSON，用退出码表示这次是否可用。"""
    try:
        # 冻结态的 stdout 可能是 cp936；父进程按 utf-8 解码，中文路径会烂掉。
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    mode, title, filetypes, error = _parse_args(argv)
    if error:
        _emit({'error': error})
        return 2
    try:
        paths = _ask(mode, title, filetypes)
    except Exception as exc:
        _emit({'error': f'无法打开原生选择框：{type(exc).__name__}: {exc}'})
        return 1
    _emit({'paths': paths})
    return 0


def _timeout() -> float:
    """超时可被环境变量覆盖：排查用，也让单测不必真等 5 分钟。"""
    raw = str(os.environ.get(TIMEOUT_ENV, '') or '').strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_TIMEOUT
        if value > 0:
            return value
    return DEFAULT_TIMEOUT


def _command(mode: str, title: str, filetypes) -> list:
    """冻结态没有 .py 可执行，只能回填同一个 exe 的隐藏入口参数。"""
    if getattr(sys, 'frozen', False):
        command = [sys.executable, mode]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), mode]
    if title:
        command += ['--title', str(title)]
    if filetypes:
        command += ['--filetypes', json.dumps([list(item) for item in filetypes],
                                             ensure_ascii=False)]
    return command


def _payload_of(raw) -> dict:
    """只认 JSON 对象；Tk 偶尔往 stdout 里写别的东西，所以也扫最后一行。"""
    text = (raw or b'').decode('utf-8', 'replace').strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    for line in reversed(text.splitlines()):
        try:
            data = json.loads(line.strip())
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _stderr_tail(raw) -> str:
    return (raw or b'').decode('utf-8', 'replace').strip()[-300:]


def _spawn(command: list, wait: float):
    """真正起子进程。单独抽出来只因为测试必须顶掉它——测试里绝不能真弹对话框。"""
    return subprocess.run(command, capture_output=True, timeout=wait,
                          creationflags=_NO_WINDOW)


def pick(mode: str, title: str = '', filetypes=None,
         timeout: float | None = None) -> list:
    """父进程侧：起子进程弹框，读回它 stdout 的 JSON，返回路径列表。

    任何失败都抛 PickerError（带可读原因）—— 调用方只负责把它变成 400，
    既不会 500，也不会让后端进程退出。
    """
    if mode not in MODE_FLAGS:
        raise PickerError(f'未知的选择模式：{mode}')
    wait = _timeout() if timeout is None else float(timeout)
    try:
        completed = _spawn(_command(mode, title, filetypes), wait)
    except subprocess.TimeoutExpired:
        raise PickerError(f'选择框 {wait:g} 秒内没有返回，已放弃（可重试）') from None
    except Exception as exc:
        raise PickerError(f'无法启动选择框进程：{type(exc).__name__}: {exc}') from None
    payload = _payload_of(completed.stdout)
    if completed.returncode != 0:
        reason = str((payload or {}).get('error') or '') or _stderr_tail(completed.stderr)
        raise PickerError(f'选择框进程异常退出：{reason or completed.returncode}')
    if payload is None:
        raise PickerError('选择框进程没有返回合法 JSON（可能不是图形环境）')
    error = str(payload.get('error') or '')
    if error:
        raise PickerError(error)
    paths = payload.get('paths')
    if not isinstance(paths, list):
        raise PickerError('选择框进程返回的 JSON 里缺少 paths 列表')
    return [str(item) for item in paths]


if __name__ == '__main__':
    sys.exit(run_child(sys.argv[1:]))
