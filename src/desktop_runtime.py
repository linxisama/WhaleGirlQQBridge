"""Qt 导入前配置桌面兼容环境；不创建网页或替换用户配置。"""
import json
import os
import sys
from pathlib import Path

from .startup_supervisor import (child_log_path, crash_report_path, hex_code, logs_dir,
                                 qt_messages_path, stage)

QT_PACKAGES = {'PyQt6': '6.11.0', 'PyQt6-Qt6': '6.11.2', 'PyQt6-sip': '13.13.0'}


def remote_session():
    if os.name != 'nt':
        return False
    if os.environ.get('SESSIONNAME', '').upper().startswith('RDP-'):
        return True
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))
    except (OSError, AttributeError):
        return False


def configure(safe_mode, *, native_test=False, environment=None, windows=None, bundle=None):
    env = os.environ if environment is None else environment
    windows = os.name == 'nt' if windows is None else windows
    if bundle is None and getattr(sys, 'frozen', False):
        bundle = getattr(sys, '_MEIPASS', None)
    if bundle:
        plugins = Path(bundle) / 'PyQt6/Qt6/plugins'
        if plugins.is_dir():
            # 不让其他软件遗留的 Qt 插件路径混进本程序固定版本的运行库。
            env['QT_PLUGIN_PATH'] = str(plugins)
            env['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(plugins / 'platforms')
    platform = env.get('QT_QPA_PLATFORM', '').split(':', 1)[0].strip().lower()
    if windows and (native_test or platform not in {'offscreen', 'minimal'}):
        # 不能加 dialogs=none：它会让 Qt 在兼容模式下强制走自绘文件框，
        # 即使用户配置和 AA_DontUseNativeDialogs 都指向系统原生框。
        env['QT_QPA_PLATFORM'] = ('windows:fontengine=gdi:nodirectwrite:dpiawareness=0'
                                  if safe_mode else 'windows')
    if safe_mode:
        env['QT_OPENGL'] = 'software'
        env['QT_QUICK_BACKEND'] = 'software'
        env['QSG_RHI_PREFER_SOFTWARE_RENDERER'] = '1'
        env['QT_STYLE_OVERRIDE'] = 'Fusion'
    return env


# 用户看到的必须是"能自救"的一句话，而不是只有一个退出码。
CRASH_EXPLANATIONS = {
    0xC0000409: '界面组件异常退出（多半是线程或绘制相关的原生错误）',
    0xC0000005: '访问了不属于它的内存（显卡驱动或图形库冲突最常见）',
    0xC0000374: '程序堆内存被破坏',
    0xC00000FD: '栈空间用尽（递归或 Qt 初始化爆栈）',
    0xC000001D: '执行了非法指令（CPU 不支持或二进制损坏）',
    0xC0000094: '整数除零',
}


def crash_explanation(code):
    try:
        value = int(code) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return '退出码未知'
    if value == 0:
        return '正常退出'
    return CRASH_EXPLANATIONS.get(value, '程序非正常结束（该退出码还没有收录解释）')


def crash_details():
    """读上一次的 desktop-crash.json；没有或读不动就返回空 dict。"""
    try:
        data = json.loads(crash_report_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def startup_failure_text(reason, details=None, logs=None):
    """失败弹窗正文（纯函数：不弹窗、不读全局，便于离线测试）。"""
    details = crash_details() if details is None else details
    logs = logs_dir() if logs is None else Path(logs)
    child = details.get('gui_child_log') or str(child_log_path(logs))
    qt = details.get('qt_messages_log') or str(qt_messages_path(logs))
    code = details.get('code')
    if code is None:
        head = '① 退出码：本次没有拿到窗口进程的退出码（多半是启动就失败了，或者启动超时被结束）。'
    else:
        head = '① 退出码：%s —— %s' % (hex_code(code), crash_explanation(code))
    return '\n'.join([
        reason, '',
        head, '',
        '② 详细日志（出问题时请把这两个文件发我）：',
        '   · 窗口进程输出：%s' % child,
        '   · Qt 消息（含致命错误原文）：%s' % qt, '',
        '③ 下一步：',
        '   · 点“是(Y)”＝以兼容模式重试（软件渲染 + GDI 字体，最不容易撞图形驱动）；',
        '   · 还是不行就双击安装目录的“收集诊断.cmd”，把诊断包发我；',
        '   · 原配置与对话记录不会被改动。',
    ])


def retry_in_safe_mode():
    """用户点了“以兼容模式重试”：另起一个 --safe-mode 进程。"""
    import subprocess
    if getattr(sys, 'frozen', False):
        command = [sys.executable, '--safe-mode']
    else:
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'gui_launcher.py'),
                   '--safe-mode']
    try:
        subprocess.Popen(command, cwd=os.getcwd(), close_fds=True,
                         creationflags=0x08000000 if os.name == 'nt' else 0)
        return True
    except OSError:
        return False


# 无人值守入口：这些参数背后没有人在屏幕前点按模态框，弹一次就把进程挂死在桌面上。
NON_INTERACTIVE_ARGUMENTS = ('--backend-only', '--self-test', '--native-self-test')
NON_INTERACTIVE_ENV = 'WHALEGIRL_NO_MODAL'
INTERACTIVE_STATION = 'winsta0'
UOI_NAME = 2                     # GetUserObjectInformationW 的 UOI_NAME
FAILURE_TITLE = '鲸鱼娘 QQ 桌面控制台启动失败'
CONSOLE_TITLE = '鲸鱼娘 QQ 控制台'
YES_NO_BOX = 0x04 | 0x10         # 是(Y)=兼容模式重试，否(N)=退出
OK_BOX = 0x10


def desktop_station():
    """当前进程所在的窗口站名；查不到（非 Windows 或调用失败）返回 None。

    只有交互式桌面才是 WinSta0：服务会话、计划任务拿到的是 Service-0x0-… 之类，
    在那个窗口站上弹 MessageBoxW 没有人能点，只会把无人值守启动一直挂住。
    """
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetProcessWindowStation.restype = wintypes.HANDLE
        user32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        user32.GetUserObjectInformationW.restype = wintypes.BOOL
        handle = user32.GetProcessWindowStation()
        if not handle:
            return None
        needed = wintypes.DWORD(0)
        user32.GetUserObjectInformationW(handle, UOI_NAME, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_unicode_buffer(min(max(int(needed.value), 32), 512))
        if not user32.GetUserObjectInformationW(handle, UOI_NAME, buffer,
                                               ctypes.sizeof(buffer), ctypes.byref(needed)):
            return None
        return buffer.value or None
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def interactive_session(argv=None, *, windows=None, station=None):
    """能不能弹模态框：三关都过才算交互（纯判定，不弹窗、不读崩溃报告，便于离线测试）。

    ① 命令行不是后台/自动化入口；② 在 Windows 上；③ 窗口站是 WinSta0。
    窗口站查不到时按交互处理 —— 宁可多弹一次，也不能把正常桌面启动的提示吞掉。
    """
    arguments = list(sys.argv if argv is None else argv)
    if any(flag in arguments for flag in NON_INTERACTIVE_ARGUMENTS):
        return False
    if os.environ.get(NON_INTERACTIVE_ENV, '').strip() not in ('', '0'):
        return False
    if not (os.name == 'nt' if windows is None else bool(windows)):
        return False
    name = (desktop_station() if station is None else station) or ''
    return not name or name.strip().lower() == INTERACTIVE_STATION


def message_box(text, title=FAILURE_TITLE, buttons=YES_NO_BOX):
    """原生模态框；只用 user32，不引入任何 GUI 依赖（只在交互模式下调用）。"""
    import ctypes
    return ctypes.windll.user32.MessageBoxW(None, text, title, buttons)


def headless_failure_text(reason, logs=None):
    """非交互模式的失败正文：不要求人点任何按钮，直接给出要处理的文件。"""
    logs = logs_dir() if logs is None else Path(logs)
    return '\n'.join([
        '鲸鱼娘 QQ 桌面控制台启动失败。', '',
        reason, '',
        '本次是非交互启动（后台/自动化，没有可交互的桌面会话），因此不弹窗，直接以非零退出码结束。',
        '启动日志：%s' % (Path(logs) / 'startup.log'),
        '分阶段记录：%s' % (Path(logs) / 'startup-stages.jsonl'),
    ])


def report_failure(text, *, stream=None, logs=None):
    """把失败原文写到 stderr 和 <logs>/startup.log：打包后没有控制台也留得下证据。"""
    line = text if text.endswith('\n') else text + '\n'
    stream = sys.stderr if stream is None else stream
    if stream is not None:
        try:
            stream.write(line)
            stream.flush()
        except (OSError, ValueError):
            pass
    try:
        folder = logs_dir() if logs is None else Path(logs)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'startup.log').open('a', encoding='utf-8') as output:
            output.write(line)
    except OSError:
        pass


def failed(reason, *, messagebox=None, interactive=None, argv=None, stream=None):
    """启动失败的唯一出口：交互模式弹框，非交互模式只写日志并以 1 结束。

    实测缺陷（2026-10-27）：--backend-only 失败也走到这里，弹出无父窗口、无超时的
    模态框，无人值守启动被拦在用户桌面上等人点按钮。是否弹框必须先过
    interactive_session()，非交互时改成 stderr + logs/startup.log + 非零退出码。
    """
    stage('desktop-start-failed', message=reason)
    if interactive is None:
        interactive = interactive_session(argv)
    if interactive:
        try:
            answer = (message_box if messagebox is None
                      else messagebox)(startup_failure_text(reason))
            if answer == 6:            # IDYES
                retry_in_safe_mode()
        except Exception:
            pass
        return 1
    report_failure(headless_failure_text(reason), stream=stream)
    return 1


# 外观相关的键：`--reset-ui` 只清这些，绝不碰模型/接入/人设/聊天记录
APPEARANCE_KEYS = ('transparency', 'panel_transparency', 'blur', 'bg_color',
                   'background_image', 'safe_mode', 'geometry')


def appearance_config_path():
    """用户外观配置文件（%LOCALAPPDATA%\\WhaleGirlQQ\\data\\gui_config.json）。"""
    base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
    return Path(base) / 'WhaleGirlQQ' / 'data' / 'gui_config.json'


def reset_appearance(path=None):
    """把外观恢复默认，返回被清掉的键名列表。

    为什么需要：窗口透明度拉满时 opacity 会降到看不清（甚至点不到），
    而这个值存在 gui_config.json 里 —— **重装不覆盖用户数据**，所以"重装也没用"。
    给一条不依赖界面的自救通道：鲸鱼娘QQ.exe --reset-ui
    """
    import json

    target = Path(path) if path else appearance_config_path()
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding='utf-8') or '{}')
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    removed = [key for key in APPEARANCE_KEYS if key in data]
    for key in removed:
        data.pop(key, None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return removed
