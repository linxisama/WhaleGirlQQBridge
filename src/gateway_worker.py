"""由网关拉起的「已安装控制台」入口。

安装版没有用户源码目录，网关不能再用 ``bot_worker.py + qq_gui.py`` 那套。
这里复用安装包内的同一套代码：写账号专属配置 → 在 job 指定端口起后端 →
打开控制台窗口并自动启动机器人。入口由 ``gui_launcher.py --gateway-job`` 调用。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

import yaml

# 和 src/app_log.py 的落盘约定一致：<data_dir>/qq-only/logs
LOG_PARTS = ('data', 'qq-only', 'logs')


def _bundle_root() -> Path:
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))


def log_dir(root) -> Path:
    """账号目录下的日志目录（只算路径 + 尽量建出来，建不出来也不抛）。"""
    folder = Path(root).joinpath(*LOG_PARTS)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return folder


def ensure_stdio(root):
    """冻结窗口模式（无控制台）下 sys.stdout / sys.stderr 是 None。

    uvicorn 建日志配置时会调 ``sys.stdout.isatty()``（uvicorn/logging.py:42），
    撞上 None 就抛 AttributeError → ``ValueError: Unable to configure formatter 'default'``，
    进程在窗口出现之前就退掉（实测：安装版 8.8 秒 / 10.7 秒退出，退出码 1，窗口 0 个）。
    traceback 同样没地方可去，所以先把它们接到账号日志目录。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        path = log_dir(root) / f'gateway-console-{time.strftime("%Y%m%d")}.log'
        stream = open(path, 'a', encoding='utf-8', buffering=1)
    except Exception:
        return None
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return stream


def autostart_enabled(env=None) -> bool:
    """网关传 WHALE_GATEWAY_AUTOSTART=0 表示只开窗口、不自动起机器人。"""
    env = os.environ if env is None else env
    return str(env.get('WHALE_GATEWAY_AUTOSTART', '1')).strip() != '0'


def smoke_enabled(env=None) -> bool:
    """与网关 bot_worker.py 的 WHALE_GATEWAY_WORKER_SMOKE 判定对齐：非空即开。"""
    env = os.environ if env is None else env
    return bool(str(env.get('WHALE_GATEWAY_WORKER_SMOKE', '')).strip())


PLUGIN_STORE_FILES = ('qqagent_extensions.json', 'qqagent_bridge_catalog.json')
PLUGIN_STORE_DIRS = ('qqagent_host', 'qqagent_extensions')
BRIDGE_PATH_KEYS = ('hostRoot', 'catalogPath', 'extensionsDir', 'sourceDir',
                    'hostScript', 'extensionPath')


def _merge_missing(base, extra):
    """把 extra 里有、base 里没有的键补进 base；base 已有的一律不动。"""
    if not isinstance(base, dict) or not isinstance(extra, dict):
        return base
    for key, value in extra.items():
        if key not in base:
            base[key] = value
        elif isinstance(base[key], dict) and isinstance(value, dict):
            _merge_missing(base[key], value)
    return base


def _rewrite_bridge_paths(settings, shared_data: Path, own_data: Path):
    """把 qqagent_bridge 里指向共享 data 的路径改到账号自己的 data。

    网关账号原来没有插件库，设置是从共享目录复制来的；路径不改的话，
    账号自己的 hostRoot 仍然指回共享目录，等于换了个地方继续串用。
    """
    bridge = settings.get('qqagent_bridge') if isinstance(settings, dict) else None
    if not isinstance(bridge, dict):
        return settings
    old, new = str(shared_data), str(own_data)
    # 配置可能是手写的，正斜杠/反斜杠都见过；两种写法都要能改到，
    # 否则 Windows 上会留下一条指回共享库的死路径。
    pairs = [(old, new), (old.replace(os.sep, '/'), new.replace(os.sep, '/'))]
    for key in BRIDGE_PATH_KEYS:
        value = bridge.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        for source, target in pairs:
            if text == source or text.startswith(source + '/') or text.startswith(source + os.sep):
                bridge[key] = target + text[len(source):]
                break
    return settings


def _sync_plugin_store(root: Path, user_data: Path):
    """把用户共享插件库搬进网关账号目录（只补缺、不覆盖账号已有设置）。

    5.2.105 以前 prepare 只复制 plugin_settings.json，账号目录永远没有
    qqagent_host/qqagent_extensions 这两个 marker，QQRuntime._plugin_dir()
    就会回退到共享库：插件设置、宿主根、扩展目录和消息库分家（问题 3/4）。
    """
    own_data = Path(root) / 'data'
    own = own_data / 'qq-only'
    shared = Path(user_data) / 'qq-only'
    if not shared.is_dir():
        return
    own.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_STORE_DIRS:
        source = shared / name
        if not source.is_dir():
            continue
        destination = own / name
        if destination.exists():
            continue
        shutil.copytree(source, destination)
    for name in PLUGIN_STORE_FILES:
        source = shared / name
        if source.is_file() and not (own / name).exists():
            shutil.copy2(source, own / name)
    source_settings = shared / 'plugin_settings.json'
    if not source_settings.is_file():
        return
    destination = own / 'plugin_settings.json'
    if destination.is_file():
        try:
            own_values = json.loads(destination.read_text(encoding='utf-8-sig'))
        except Exception:
            own_values = {}
        try:
            shared_values = json.loads(source_settings.read_text(encoding='utf-8-sig'))
        except Exception:
            shared_values = {}
        if isinstance(own_values, dict) and isinstance(shared_values, dict):
            _merge_missing(own_values, shared_values)
            _rewrite_bridge_paths(own_values, shared, own)
            destination.write_text(json.dumps(own_values, ensure_ascii=False, indent=2),
                                   encoding='utf-8')
    else:
        try:
            values = json.loads(source_settings.read_text(encoding='utf-8-sig'))
        except Exception:
            values = {}
        if isinstance(values, dict):
            _rewrite_bridge_paths(values, shared, own)
            destination.write_text(json.dumps(values, ensure_ascii=False, indent=2),
                                   encoding='utf-8')


def prepare(job: dict) -> Path:
    root = Path(job['root'])
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'config.qq.yaml'
    if not target.exists():
        seed = None
        local = os.environ.get('LOCALAPPDATA')
        if local:
            candidate = Path(local) / 'WhaleGirlQQ' / 'config.qq.yaml'
            if candidate.is_file() and candidate.resolve() != target.resolve():
                seed = candidate
        if seed is None:
            candidate = _bundle_root() / 'config.qq.yaml'
            if candidate.is_file():
                seed = candidate
        config = yaml.safe_load(seed.read_text(encoding='utf-8-sig')) if seed and seed.exists() else {}
    else:
        config = yaml.safe_load(target.read_text(encoding='utf-8-sig'))
    config = config if isinstance(config, dict) else {}
    only = config.setdefault('qq_only', {})
    only['mode'] = 'qqbridge'
    only['auto_start'] = False
    bridge = only.setdefault('qqbridge', {})
    bridge.pop('access_token_encrypted', None)
    bridge.update(ws_url=job['ws_url'], http_url='', access_token=job['access_token'],
                  snowluma_exe='')
    target.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')

    styles = _bundle_root() / 'styles'
    if styles.is_dir():
        for path in styles.rglob('*.yaml'):
            output = root / path.relative_to(styles)
            if not output.exists():
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, output)
    local = os.environ.get('LOCALAPPDATA')
    if local:
        user_data = Path(local) / 'WhaleGirlQQ' / 'data'
        _sync_plugin_store(root, user_data)
        user_custom = Path(local) / 'WhaleGirlQQ' / 'styles' / 'custom'
        if user_custom.is_dir():
            for path in user_custom.glob('*.yaml'):
                destination = root / 'styles' / 'custom' / path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy2(path, destination)
    # 账号目录里留一份任务单：插件库/主人号靠它认出「这是网关账号目录」，
    # 入口被别的启动方式（job 放在别处）调用时也能沿用用户那套插件。
    job_file = root / 'launch.json'
    if not job_file.exists():
        job_file.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
    return root


def start_backend(job: dict, root: Path):
    import uvicorn
    from src.qq_app import QQRuntime, create_app

    runtime = QQRuntime(root=root)
    application = create_app(runtime)
    application.state.windowed = True
    application.state.console_mode = 'gateway-account'
    listener = socket.socket()
    listener.bind(('127.0.0.1', int(job['port'])))
    listener.listen(128)
    # log_config=None：uvicorn 默认日志配置会调 sys.stdout.isatty()，冻结窗口模式下
    # sys.stdout 是 None，直接 AttributeError 把控制台弄死（见 ensure_stdio 注释）。
    # 这边本来只要 warning、也不开访问日志，跳过它的日志配置没有损失。
    server = uvicorn.Server(uvicorn.Config(application, log_level='warning',
                                           access_log=False, log_config=None))
    holder = {}

    async def serve():
        holder['loop'] = asyncio.get_running_loop()
        await server.serve(sockets=[listener])

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.1)
    if not server.started:
        raise RuntimeError('账号机器人后台启动失败，请查看 console.log')
    return runtime, application, server, thread, holder


def _report_boot_failure(root: Path, error: BaseException) -> int:
    """后台起不来：写日志 + 弹一个中文提示，别让用户看到「双击没反应」。"""
    detail = traceback.format_exc()
    path = None
    try:
        path = log_dir(root) / 'gateway-boot.log'
        with path.open('a', encoding='utf-8') as output:
            output.write(detail)
    except Exception:
        path = None
    try:
        print(detail)
    except Exception:
        pass
    reason = f'{type(error).__name__}: {error}'
    message = f'机器人后台没能启动：{reason}' + (f'\n\n详细信息已写入：\n{path}' if path else '')
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        QApplication.instance() or QApplication([sys.argv[0]])
        QMessageBox.critical(None, '控制台启动失败', message)
    except Exception:
        pass
    return 1


def report_autostart_failure(root, runtime, error):
    """自动启动机器人失败：留证据、记事件，但不带走后台进程。

    窗口模式有 start_bot() 兜底（失败留在界面上说清楚），无窗口模式原来直接
    往上抛：网关只看得到「机器人后端没了」，用户连原因都找不到（实测首装没配
    模型时点「连接机器人」就是当场消失，日志里只有一段 traceback）。
    """
    reason = f'{type(error).__name__}: {error}'
    path = None
    try:
        path = log_dir(root) / "gateway-boot.log"
        with path.open("a", encoding="utf-8") as output:
            output.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} 自动启动机器人失败：{reason}\n')
            output.write(traceback.format_exc() + "\n")
    except Exception:
        path = None
    text = f'机器人没起来：{reason}' + (f"（详情见 {path}）" if path else "")
    try:
        runtime.note(text)
    except Exception:
        pass
    try:
        print(f"[gateway] {text}")
    except Exception:
        pass
    return path


def main(job_path) -> int:
    try:
        job = json.loads(Path(job_path).read_text(encoding='utf-8'))
    except Exception:
        traceback.print_exc()
        return 1
    root = Path(str(job.get('root') or '.'))
    try:
        root = prepare(job)
        ensure_stdio(root)
        runtime, application, server, thread, holder = start_backend(job, root)
    except BaseException as error:
        return _report_boot_failure(root, error)
    if os.environ.get('WHALE_GATEWAY_HEADLESS') == '1':
        loop = holder.get('loop')
        if loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(runtime.start(), loop).result(timeout=30)
            except BaseException as error:
                report_autostart_failure(root, runtime, error)
        thread.join()
        return 0

    import qq_gui as gui
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    gui.ROOT = root
    gui.GUI_CFG = root / 'data/gui_config.json'
    gui.BASE_URL = f"http://127.0.0.1:{job['port']}"
    gui.AUTO_BACKEND = False
    app = QApplication([sys.argv[0]])
    state = {}

    def notify(title, message):
        """诊断回调：能上界面就上界面，上不了就进日志。"""
        window = state.get('window')
        if window is not None:
            try:
                window.console_tab.set_notice(f'{title}：{message}', ok=False)
                return
            except Exception:
                pass
        try:
            print(f'[gui] {title}：{message}')
        except Exception:
            pass

    # 这条路径原来没装诊断：Qt 槽里的未捕获异常会直接让进程 abort（0xC0000409）。
    try:
        from src.gui.diagnostics import install as install_diagnostics
        install_diagnostics(log_dir(root), notify)
    except Exception:
        pass

    window = gui.MainWindow()
    state['window'] = window
    window.setWindowTitle(f"鲸鱼娘 QQ 控制台 · {job.get('label', '当前账号')}")
    window._load_settings()
    window.console_tab.set_notice('已接入统一框架：本窗口只管理当前 QQ（已自动启动机器人）。', ok=True)
    window._start_backend = lambda: None
    window._start_backend_inprocess = lambda: None
    try:
        from src.gui.tray import ConsoleInstance
        window._instance_server = ConsoleInstance(window, root)
    except Exception:
        pass

    watchdog = QTimer()

    def check_parent():
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, int(job['parent']))
            if not handle:
                app.quit()
            else:
                kernel.CloseHandle(handle)
        except Exception:
            pass
        request = root / 'show-request'
        if request.exists():
            request.unlink(missing_ok=True)
            window.showNormal()
            window.raise_()
            window.activateWindow()

    watchdog.timeout.connect(check_parent)
    watchdog.start(2000)

    def start_bot():
        """自动启动机器人：失败也要留在界面上说清楚，绝不能让窗口跟着消失。"""
        try:
            window._action('start')
        except BaseException as error:
            reason = f'{type(error).__name__}: {error}'
            path = None
            try:
                from src.gui.diagnostics import log_exception
                path = log_exception('gateway 自动启动机器人')
            except Exception:
                pass
            text = f'机器人没起来：{reason}' + (f'（详情见 {path}）' if path else '')
            try:
                window.console_tab.set_notice(text, ok=False)
            except Exception:
                pass
            try:
                print(f'[gateway] {text}')
            except Exception:
                pass

    def grab_smoke():
        try:
            window.grab().save(str(root / 'console-smoke.png'))
        except Exception:
            try:
                from src.gui.diagnostics import log_exception
                log_exception('gateway smoke 截图')
            except Exception:
                pass

    window.show()
    if smoke_enabled():
        # 与网关 bot_worker.py 对齐：2.5 秒截图、3 秒退出（退出码 0）。
        QTimer.singleShot(2500, grab_smoke)
        QTimer.singleShot(3000, app.quit)
    if autostart_enabled():
        QTimer.singleShot(1500, start_bot)
    try:
        return app.exec()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
