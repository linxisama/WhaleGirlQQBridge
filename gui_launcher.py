"""Frozen entry point with persistent startup diagnostics + crash recovery.

为什么需要这一层（2026-10-15 实测）：

用户的另一台电脑（Win10 + RDP 远程桌面 + Parsec/UU/微软三个虚拟显示适配器）上，
程序**每次双击都必然原生崩溃**：`QtCore.pyd` 里 `0xC0000005` 访问违规，
崩在 GUI 初始化阶段（窗口还没显示），用户看到的就是"双击完全没反应"。

原生崩溃不会执行 Python 的 finally；2026-10-16 新日志还确认安全模式也会崩。
因此父进程不加载 Qt，仅监护 --gui-worker 子进程：普通模式失败后最多尝试一次
安全模式；仍失败或启动超时则记录错误并显示原生提示，不启动网页。
日志只证明 window.show 阶段访问违规，不能据此认定远程显示是唯一根因。
"""
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

# 原生崩溃退出码（Windows）：访问违规 / 栈缓冲溢出 / 堆损坏 / 非法指令 / 除零
CRASH_EXIT_CODES = {
    0xC0000005, 0xC0000409, 0xC0000374, 0xC000001D, 0xC0000094,
    0xC00000FD,          # 栈溢出
}


def _data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WhaleGirlQQ"


def _logs_dir() -> Path:
    folder = _data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


QT_MESSAGES_MAX_BYTES = 8 * 1024 * 1024
_QT_LOG_STREAM = None
_QT_HANDLER = None


def _rotate_log(path: Path, limit: int) -> None:
    """超过 limit 就改写成 <name>.1；任何失败都静默（诊断不能拖垮启动）。"""
    try:
        if path.stat().st_size < limit:
            return
    except OSError:
        return
    try:
        backup = Path(str(path) + '.1')
        backup.unlink(missing_ok=True)
        path.rename(backup)
    except OSError:
        try:
            path.write_text('', encoding='utf-8')
        except OSError:
            pass


def install_qt_message_handler(folder) -> bool:
    """把 Qt 日志（含 qFatal 原文）追加进 <logs>/qt-messages.log。

    必须在 QApplication 创建之前装：qFatal 走的是消息处理器，装晚了就只剩一个
    退出码。返回是否装上；任何异常都吞掉，装不上也不影响启动。
    """
    global _QT_LOG_STREAM, _QT_HANDLER
    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return False
    levels = {}
    try:
        levels = {QtMsgType.QtDebugMsg: 'DEBUG', QtMsgType.QtInfoMsg: 'INFO',
                  QtMsgType.QtWarningMsg: 'WARNING', QtMsgType.QtCriticalMsg: 'CRITICAL',
                  QtMsgType.QtFatalMsg: 'FATAL'}
    except Exception:
        return False
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'qt-messages.log'
        _rotate_log(path, QT_MESSAGES_MAX_BYTES)
        _QT_LOG_STREAM = open(path, 'a', encoding='utf-8', errors='replace', buffering=1)
    except Exception:
        _QT_LOG_STREAM = None
        return False

    def handler(mode, context, message):
        # 这个函数在 Qt 自己的线程里被调用，抛出去会把进程直接带走。
        try:
            stream = _QT_LOG_STREAM
            if stream is None:
                return
            now = time.time()
            stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
            level = levels.get(mode, str(mode))
            stream.write('%s.%03d [Qt/%s] %s\n' % (stamp, int(now * 1000) % 1000, level, message))
            stream.flush()
        except Exception:
            return

    _QT_HANDLER = handler        # 留住引用：处理器被回收后 Qt 会调用到悬空指针
    try:
        qInstallMessageHandler(handler)
    except Exception:
        return False
    return True


def crash_recovery_plan(exit_code, safe_mode: bool) -> str:
    """崩了之后该怎么办（纯函数，便于离线测试）。

    - ``'none'``          ：正常退出/普通错误，不重开
    - ``'relaunch-safe'`` ：原生崩溃且当前不是安全模式 → 用安全模式再起一次
    """
    if safe_mode or exit_code is None:
        return 'none'
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        return 'none'
    unsigned = code & 0xFFFFFFFF
    if unsigned in CRASH_EXIT_CODES or code < 0:
        # 负数也是异常终止（PyInstaller/Windows 会把崩溃码转成负数）
        return 'relaunch-safe' if code != 0 else 'none'
    return 'none'


def marker_path() -> Path:
    return _logs_dir() / "launching.json"


def read_marker() -> dict:
    try:
        data = json.loads(marker_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_marker(safe_mode: bool) -> None:
    """记下"这次启动开始了"。正常退出时会删掉它。"""
    try:
        marker_path().write_text(json.dumps({
            "at": __import__("time").time(), "pid": os.getpid(),
            "safe_mode": bool(safe_mode), "argv": sys.argv[1:][:6],
        }), encoding="utf-8")
    except Exception:
        pass


def clear_marker() -> None:
    try:
        marker_path().unlink(missing_ok=True)
    except Exception:
        pass


def previous_launch_crashed() -> bool:
    """上次启动是不是没退干净（标记还在 + 那个进程已经没了 = 崩了）。"""
    data = read_marker()
    if not data:
        return False
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED
            if not handle:
                return True
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        os.kill(pid, 0)
        return False
    except Exception:
        return True


def config_safe_mode() -> bool:
    """外观页勾过的"兼容模式"存在 gui_config.json 里，Qt 加载前就要读出来。"""
    candidates = [_data_dir() / "data" / "gui_config.json"]
    if not getattr(sys, "frozen", False):
        candidates.append(Path(__file__).resolve().parent / "data" / "gui_config.json")
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("safe_mode"):
            return True
    return False


def _relaunch_in_safe_mode() -> None:
    """用安全模式重新起一个进程（当前进程马上就要退了）。"""
    command = ([sys.executable, "--safe-mode"] if getattr(sys, "frozen", False)
               else [sys.executable, str(Path(__file__).resolve()), "--safe-mode"])
    subprocess.Popen(command, cwd=os.getcwd(), close_fds=True)


def _run_mcp(argv) -> int:
    """内置 stdio MCP 宿主：--mcp memory|bot|tools|check。

    PyInstaller windowed 程序在父进程用管道启动时标准流一般可用；但少数启动器
    会留下 None/closed 的 sys.stdin/out/err，这里用 fd 0/1/2 兜底，保证
    JSON-RPC 的每一行都能进出。必须在 main() 之前调用，不能碰 Qt/单实例。
    """
    import io

    streams = []
    for name, fd, mode, errors in (('stdin', 0, 'r', 'replace'),
                                   ('stdout', 1, 'w', 'strict'),
                                   ('stderr', 2, 'w', 'replace')):
        stream = getattr(sys, name, None)
        if stream is None or getattr(stream, 'closed', False):
            try:
                stream = io.TextIOWrapper(
                    io.FileIO(fd, mode, closefd=False),
                    encoding='utf-8', errors=errors, line_buffering=True)
            except OSError:
                stream = io.StringIO()
        else:
            reconfigure = getattr(stream, 'reconfigure', None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding='utf-8', errors=errors, newline='\n')
                except (ValueError, OSError):
                    pass
        streams.append(stream)
    sys.stdin, sys.stdout, sys.stderr = streams
    try:
        from src.dsh_mcp_entry import run
        return int(run(argv))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--meme-worker":
        # 表情子进程不是控制台实例，不能覆盖主窗口的崩溃标记或触发安全模式重启。
        from src.meme_worker import main as meme_main
        meme_main(sys.argv[2], sys.argv[3])
        return 0
    if "--pet-window" in sys.argv:
        index = sys.argv.index("--pet-window")
        if index + 1 >= len(sys.argv):
            return 2
        from src.pet_window import main as pet_main
        return pet_main(sys.argv[index + 1])
    if "--gateway-job" in sys.argv:
        # 网关拉起已安装版：账号/端口/中继都由 job 指定，走独立后端，不碰用户默认 27499。
        index = sys.argv.index("--gateway-job")
        if index + 1 >= len(sys.argv):
            return 2
        from src.gateway_worker import main as gateway_main
        return gateway_main(sys.argv[index + 1])
    try:
        folder = _logs_dir()
    except OSError as exc:
        # 连日志目录都建不出来（LOCALAPPDATA 指向不存在的盘）时没有 startup.log 可写，
        # 但也不能甩 WinError 栈：消息要指明是哪个环境变量，且照旧走 failed()——
        # 非交互模式只写 stderr 不弹框，交互模式才弹。
        from src.compat_console import environment_error_message
        from src.desktop_runtime import failed
        return failed(environment_error_message(os.environ, _data_dir() / 'logs', exc))
    previous_stdout, previous_stderr = sys.stdout, sys.stderr
    with open(folder / "startup.log", "a", encoding="utf-8", buffering=1) as log:
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
        try:
            return _dispatch(folder, log)
        except Exception:
            traceback.print_exc(file=log)
            from src.desktop_runtime import failed
            return failed('详细信息已写入：\n' + str(folder / 'startup.log'))
        finally:
            sys.stdout, sys.stderr = previous_stdout, previous_stderr


def _dispatch(folder, log):
    # 外观自救：窗口透明到看不见时用户点不到界面，用 `--reset-ui` 恢复默认外观
    # （只清外观相关键，不碰配置、聊天记录、人设）。
    if '--reset-ui' in sys.argv:
        sys.argv[:] = [arg for arg in sys.argv if arg != '--reset-ui']
        try:
            from src.desktop_runtime import reset_appearance
            removed = reset_appearance()
            print(f"[reset-ui] 已重置外观设置：{removed or '（本来就没有自定义值）'}", flush=True)
            try:
                log(f"[reset-ui] 已重置外观：{removed}")
            except Exception:
                pass
        except Exception as exc:
            print(f"[reset-ui] 重置失败：{type(exc).__name__}: {exc}", flush=True)
    # 旧版残留快捷入口也恢复为桌面，不再因旧参数打开浏览器。
    if '--browser-mode' in sys.argv:
        sys.argv[:] = [arg for arg in sys.argv if arg not in {'--browser-mode', '--no-open-browser'}]
    if '--backend-only' in sys.argv:
        from src.compat_console import main as compat_main
        return compat_main(open_browser=False)
    test_flag = next((flag for flag in ('--self-test', '--native-self-test') if flag in sys.argv), None)
    if '--gui-worker' not in sys.argv and not test_flag:
        from src.startup_supervisor import watch_gui, supervise, GuiResult, stage
        from src.desktop_runtime import remote_session
        import uuid
        original = [arg for arg in sys.argv[1:] if arg != '--safe-mode']
        safe = ('--safe-mode' in sys.argv or config_safe_mode() or previous_launch_crashed()
                or os.environ.get('WHALEGIRL_SAFE_MODE') == '1' or remote_session())
        def attempt(use_safe):
            command = ([sys.executable] if getattr(sys, 'frozen', False)
                       else [sys.executable, str(Path(__file__).resolve())])
            command += ['--gui-worker', *original]
            if use_safe:
                command.append('--safe-mode')
            try:
                return watch_gui(command, folder / ('ready-' + uuid.uuid4().hex))
            except OSError as exc:
                stage('child-start-failed', error=type(exc).__name__)
                return GuiResult(1, False)
        from src.desktop_runtime import failed
        return supervise(attempt, failed, safe_mode=safe,
                         is_native_crash=lambda code: crash_recovery_plan(code, False) == 'relaunch-safe')

    safe_mode = "--safe-mode" in sys.argv
    if not safe_mode and not test_flag and (config_safe_mode() or previous_launch_crashed()):
        safe_mode = True
        sys.argv.append("--safe-mode")
        log.write(f"[launcher] 上次启动没退干净或已开启兼容模式 → 本次以安全模式启动\n")

    write_marker(safe_mode)
    from src.desktop_runtime import configure
    configure(safe_mode, native_test=test_flag == '--native-self-test')
    exit_code = None
    # QApplication 创建前也要记录原生异常；旧版诊断装得太晚，会丢失 Qt 导入/初始化崩溃。
    import faulthandler
    native_log = open(folder / 'native-crash.log', 'a', encoding='utf-8', buffering=1)
    faulthandler.enable(native_log, all_threads=True)
    try:
        from src.startup_supervisor import stage
        stage('qt-import-starting', safe_mode=safe_mode)
        if '--gui-worker' in sys.argv:
            # 必须在 QApplication 创建（即 import qq_gui 之后调用 main）之前装。
            install_qt_message_handler(folder)
        import qq_gui
        stage('qt-imported', safe_mode=safe_mode)
        if test_flag:
            from src.gui.smoke import run
            run(qq_gui, Path(sys.argv[sys.argv.index(test_flag) + 1]), native=test_flag == '--native-self-test')
            exit_code = 0
        else:
            qq_gui.main()
            exit_code = 0
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 0
        raise
    except BaseException:
        traceback.print_exc(file=log)
        if test_flag:
            Path(sys.argv[sys.argv.index(test_flag) + 1]).with_suffix(
                ".error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        exit_code = 1
        return 1
    finally:
        if exit_code == 0:
            clear_marker()
        # 原生崩溃不会执行 finally；由外层监护进程读取退出码再决定后续动作。
    return 0


if __name__ == "__main__":
    # 内置 MCP 宿主：必须在 main()/Qt/单实例之前；它要一直占用 stdin/stdout。
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp":
        raise SystemExit(_run_mcp(sys.argv[1:]))
    # 原生文件选择框：冻结态的 --pick-* 必须在这里就地转给子进程实现，
    # 不能进 main()——那条路要装 Qt 消息钩子、写启动标记，选个文件不该碰这些。
    # 只在参数像选择模式时才导入：万一冻结包里漏了这个模块，也不能拖垮正常启动。
    if len(sys.argv) > 1 and sys.argv[1].startswith("--pick-"):
        from src.native_picker import MODE_FLAGS, run_child

        if sys.argv[1] in MODE_FLAGS:
            raise SystemExit(run_child(sys.argv[1:]))
    raise SystemExit(main())
