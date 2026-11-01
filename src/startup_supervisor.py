"""启动监护只依赖标准库，Qt 访问违规不能把监护进程一起带走。"""
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


CHILD_LOG_NAME = 'gui-child.log'
QT_MESSAGES_NAME = 'qt-messages.log'
CRASH_REPORT_NAME = 'desktop-crash.json'
CHILD_LOG_MAX_BYTES = 8 * 1024 * 1024


def logs_dir():
    """诊断目录；只算路径不创建，调用方自己决定要不要 mkdir。"""
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'WhaleGirlQQ/logs'


def child_log_path(directory=None):
    return (logs_dir() if directory is None else Path(directory)) / CHILD_LOG_NAME


def qt_messages_path(directory=None):
    return (logs_dir() if directory is None else Path(directory)) / QT_MESSAGES_NAME


def crash_report_path(directory=None):
    return (logs_dir() if directory is None else Path(directory)) / CRASH_REPORT_NAME


def hex_code(code):
    """把退出码写成 Windows 风格的 0xXXXXXXXX；3221226505 -> 0xC0000409。"""
    try:
        return '0x%08X' % (int(code) & 0xFFFFFFFF)
    except (TypeError, ValueError):
        return 'unknown'


def rotate_log(path, limit=CHILD_LOG_MAX_BYTES):
    """单文件超过 limit 就改写成 <name>.1（只留一份备份），任何失败都静默。"""
    path = Path(path)
    try:
        if path.stat().st_size < limit:
            return False
    except OSError:
        return False
    try:
        backup = Path(str(path) + '.1')
        backup.unlink(missing_ok=True)
        path.rename(backup)
        return True
    except OSError:
        pass
    try:       # 改名失败（被占用）也不能让日志从此不写：直接清空重来
        path.write_text('', encoding='utf-8')
    except OSError:
        pass
    return False


def open_child_log(path, limit=CHILD_LOG_MAX_BYTES):
    """子进程输出落到这里追加写；打不开就返回 None，调用方退回 DEVNULL。"""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(path, limit)
        # 二进制追加：子进程按自己的编码（GBK / UTF-8 / Qt）写，取证时不能转码，
        # 否则 pyinstaller 引导信息或 GBK traceback 会变成替换字符。
        return path.open('ab')
    except OSError:
        return None


def last_stage_lines(count=3):
    """最近 count 条 stage 行的原文（不做解析，原样保留便于贴进报告）。"""
    try:
        text = (logs_dir() / 'startup-stages.jsonl').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-count:]


def record_gui_crash(code, *, pid=None, ready=False, safe_mode=None, child_log=None,
                     directory=None):
    """子进程非 0 退出时留下可复查的证据：desktop-crash.json + gui-crash stage。

    safe_mode 由调用方补（watch_gui 不知道这次是不是兼容模式），先写 None，
    supervise 拿到结果后用 stamp_crash_safe_mode() 回填。
    """
    folder = logs_dir() if directory is None else Path(directory)
    payload = {
        'time': time.time(),
        'pid': os.getpid() if pid is None else pid,
        'code': code,
        'code_hex': hex_code(code),
        'ready': bool(ready),
        'safe_mode': safe_mode,
        'gui_child_log': str(child_log_path(folder)),
        'qt_messages_log': str(qt_messages_path(folder)),
        'stages': last_stage_lines(3),
    }
    try:
        folder.mkdir(parents=True, exist_ok=True)
        crash_report_path(folder).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError:
        pass
    stage('gui-crash', code=code, code_hex=payload['code_hex'])
    return payload


def stamp_crash_safe_mode(safe_mode, *, fresh=120.0):
    """把这次尝试的兼容模式标记回填进崩溃报告；报告不存在或是旧的就什么也不做。"""
    path = crash_report_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    try:
        if time.time() - float(data.get('time', 0)) > fresh:
            return False
    except (TypeError, ValueError):
        return False
    data['safe_mode'] = bool(safe_mode)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError:
        return False
    return True


def stage(name, **details):
    try:
        from .runtime_identity import APP_VERSION
        folder = logs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'startup-stages.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(dict(time=time.time(), pid=os.getpid(), version=APP_VERSION,
                                         stage=name, **details), ensure_ascii=False) + '\n')
    except OSError:
        pass


def mark_ready():
    stage('event-loop-ready')
    name = os.environ.get('WHALEGIRL_GUI_READY_FILE')
    if name:
        try:
            Path(name).write_text(str(os.getpid()), encoding='ascii')
        except OSError:
            pass


@dataclass(frozen=True)
class GuiResult:
    code: int
    ready: bool
    timed_out: bool = False


def terminate_child(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        # PyInstaller onefile 包含引导父进程，单杀父进程会留下真正的窗口进程。
        subprocess.run([str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/taskkill.exe'),
                        '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10,
                       creationflags=0x08000000)
    else:
        process.kill()
    process.wait(timeout=10)


def watch_gui(command, ready_file, *, timeout=45, popen=subprocess.Popen,
              monotonic=time.monotonic, sleep=time.sleep, terminate=terminate_child,
              child_log=None):
    ready_file = Path(ready_file)
    ready_file.unlink(missing_ok=True)
    env = dict(os.environ, WHALEGIRL_GUI_READY_FILE=str(ready_file))
    # 子进程的 stdout/stderr 以前接 DEVNULL：Qt 的 qFatal 原文和 Python traceback
    # 全被丢掉，原生崩溃就成了查不出根因的黑洞。现在追加进 <logs>/gui-child.log。
    log_path = child_log_path() if child_log is None else Path(child_log)
    handle = open_child_log(log_path)
    stream = subprocess.DEVNULL if handle is None else handle
    try:
        process = popen(command, env=env, close_fds=True, stdin=subprocess.DEVNULL,
                        stdout=stream, stderr=stream,
                        creationflags=0x08000000 if os.name == 'nt' else 0)
    except OSError:
        if handle is not None:
            handle.close()
        raise
    started = monotonic()
    ready = False
    try:
        while True:
            ready = ready or ready_file.is_file()
            code = process.poll()
            if code is not None:
                if handle is not None:      # 先落盘再取证，报告里引用的日志才是完整的
                    try:
                        handle.close()
                    except OSError:
                        pass
                    handle = None
                if code:
                    record_gui_crash(code, pid=process.pid, ready=ready, child_log=log_path)
                return GuiResult(code, ready)
            if not ready and monotonic() - started >= timeout:
                terminate(process)
                return GuiResult(process.returncode or 1, False, True)
            sleep(.2)
    except BaseException:
        terminate(process)
        raise
    finally:
        ready_file.unlink(missing_ok=True)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def supervise(attempt, fallback, *, safe_mode=False, is_native_crash):
    """普通窗口最多一次、兼容窗口最多一次；仍失败时交给桌面错误提示。"""
    modes = [True] if safe_mode else [False, True]
    for safe in modes:
        result = attempt(safe)
        stage('gui-exit', safe_mode=safe, code=result.code,
              ready=result.ready, timed_out=result.timed_out)
        if result.code and not result.timed_out:
            stamp_crash_safe_mode(safe)     # watch_gui 不知道兼容模式，由这里回填
        if result.code == 0 and not result.timed_out:
            return 0
        if not result.timed_out and not is_native_crash(result.code):
            # Python 异常已有具体错误日志，不通过重复启动同一配置掩盖它。
            break
    stage('desktop-attempts-exhausted')
    return fallback('桌面窗口未能正常启动，启动过程和退出码已记录。')
