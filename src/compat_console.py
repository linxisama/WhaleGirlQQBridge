"""无需创建 Qt 窗口的本机控制台；同一程序、同一数据目录。"""
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from . import desktop_runtime
from .runtime_identity import backend_identity_error
from .startup_supervisor import stage

URL = 'http://127.0.0.1:8090'
DB_RELATIVE = 'data/qq-only/messages.sqlite3'


class StartupError(RuntimeError):
    """启动前的环境/数据文件问题；消息本身就是给用户照着做的一句话。"""


def db_path(root):
    """用户消息库的完整路径（错误信息必须指出到底该处理哪个文件）。"""
    return Path(root) / DB_RELATIVE


def database_error_message(root, error):
    """把库/文件类异常翻译成指向具体路径、可直接照做的一句话（纯函数，便于测试）。"""
    target = db_path(root)
    if isinstance(error, sqlite3.DatabaseError):
        return ('数据库文件无法读取：%s\n'
                '它已经不是有效的 SQLite 数据库（复制中途断电、磁盘写满、被别的程序改写都会这样）。\n'
                '请先备份该文件，再删除它后重新启动；本程序不会自动删除、改名或重建你的数据。\n'
                '原始错误：%s: %s' % (target, type(error).__name__, error))
    if isinstance(error, FileNotFoundError):
        return ('找不到启动所需的文件或目录：%s\n'
                '请检查数据目录是否被移动、磁盘是否还在（例如移动硬盘没接）。\n'
                '原始错误：%s' % (target, error))
    return '启动时访问数据文件失败：%s\n原始错误：%s: %s' % (
        target, type(error).__name__, error)


def startup_error_message(root, error):
    """转换启动异常；已经是 StartupError 就原样用它的可读消息。"""
    if isinstance(error, StartupError):
        return str(error)
    return database_error_message(root, error)


def environment_error_message(environment, target, error):
    """数据目录建不出来时先点出是哪个环境变量、值是多少。"""
    value = (environment or {}).get('LOCALAPPDATA') or '(未设置)'
    return ('环境变量 LOCALAPPDATA 指向的路径不可用：%s\n'
            '本程序的数据目录建不出来：%s\n'
            '原始错误：%s: %s\n'
            '请把 LOCALAPPDATA 改成一个存在的本地盘符'
            '（正常值形如 C:\\Users\\<用户名>\\AppData\\Local）后重新启动；'
            '本程序不会删除或搬动你的任何数据。'
            % (value, target, type(error).__name__, error))


def runtime_paths(environment=None, frozen=None):
    """返回 (数据目录, 资源目录)；数据目录不可用时抛 StartupError，不是裸 FileNotFoundError。

    实测：LOCALAPPDATA 指向不存在的盘符（Z:\\）时，旧版直接抛
    FileNotFoundError: [WinError 3] 系统找不到指定的路径。: 'Z:\\'，
    用户只看到 WinError 号，不知道该改哪里。
    """
    env = os.environ if environment is None else environment
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else bool(frozen)
    bundle = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    if frozen:
        root = Path(env.get('LOCALAPPDATA') or str(Path.home())) / 'WhaleGirlQQ'
    else:
        root = Path(__file__).resolve().parents[1]
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if frozen:
            raise StartupError(environment_error_message(env, root, exc)) from None
        raise StartupError('数据目录不可用：%s\n原始错误：%s: %s\n请检查该目录的权限。'
                           % (root, type(exc).__name__, exc)) from None
    if frozen:
        for resource in (bundle / 'styles').rglob('*.yaml'):
            target = root / resource.relative_to(bundle)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(resource, target)
    return root, bundle


def probe():
    try:
        with build_opener(ProxyHandler({})).open(URL + '/api/runtime', timeout=1) as response:
            value = json.loads(response.read(16384))
            return value if isinstance(value, dict) else {}
    except Exception:
        return None


def inform(text, *, messagebox=None, interactive=None, argv=None, stream=None):
    """提示用户：交互模式弹一次原生框，非交互模式只写 stderr + startup.log。"""
    stage('compat-error', message=text)
    if not (desktop_runtime.interactive_session(argv) if interactive is None
            else bool(interactive)):
        desktop_runtime.report_failure(text, stream=stream)
        return
    if os.name == 'nt':
        try:
            box = desktop_runtime.message_box if messagebox is None else messagebox
            box(text, desktop_runtime.CONSOLE_TITLE, desktop_runtime.OK_BOX)
        except Exception:
            pass


def prepare_app(root, reason=''):
    """建后台应用；库/目录类故障转成指着具体文件的可读错误，绝不自动动用户数据。"""
    from .qq_app import QQRuntime, create_app
    target = db_path(root)
    try:
        runtime = QQRuntime(root=root, config_path=root / 'config.qq.yaml', db_path=target)
        app = create_app(runtime)
    except (sqlite3.DatabaseError, sqlite3.OperationalError, FileNotFoundError) as exc:
        # 坏库/坏路径不许静默降级，也不许自动改名或重建：用户数据优先，宁可拒绝启动。
        stage('compat-prepare-failed', error=type(exc).__name__, path=str(target))
        raise StartupError(database_error_message(root, exc)) from None
    app.state.console_mode = 'browser'
    app.state.compat_reason = reason
    return app


def open_when_ready(root, opener, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        identity = probe()
        if identity and not backend_identity_error(identity, root):
            stage('browser-ready')
            if not opener(URL):
                inform('后台已启动，但浏览器未能自动打开。请在浏览器输入：' + URL)
            return
        time.sleep(.25)
    inform('兼容控制台未能在 30 秒内启动，请查看数据目录 logs/startup-stages.jsonl。')


def main(reason='', *, open_browser=True, messagebox=None, stream=None):
    # 2 = 启动前的环境/数据文件问题：日志和 stderr 里已写清要处理哪个文件，不再弹框。
    try:
        root, _ = runtime_paths()
    except StartupError as exc:
        stage('compat-path-failed', error=str(exc)[:200])
        desktop_runtime.report_failure(str(exc), stream=stream)
        return 2
    identity = probe()
    if identity is not None:
        error = backend_identity_error(identity, root)
        if error:
            inform(error + '\n请关闭旧控制台后重开兼容控制台；不会自动结束其他程序。',
                   messagebox=messagebox, stream=stream)
            return 1
        if open_browser:
            if not webbrowser.open(URL):
                inform('请在浏览器打开本机控制台：' + URL, messagebox=messagebox, stream=stream)
        return 0
    try:
        app = prepare_app(root, reason)
    except (StartupError, sqlite3.DatabaseError, sqlite3.OperationalError,
            FileNotFoundError) as exc:
        desktop_runtime.report_failure(startup_error_message(root, exc), stream=stream)
        return 2
    if open_browser:
        threading.Thread(target=open_when_ready, args=(root, webbrowser.open),
                         name='compat-browser', daemon=True).start()
    stage('compat-server-starting', root=str(root), open_browser=open_browser)
    import uvicorn
    try:
        # 仅监听回环地址，继续使用原有本机访问检查，不开放远程管理端口。
        uvicorn.run(app, host='127.0.0.1', port=8090, log_level='warning', log_config=None)
    except SystemExit as exc:
        if exc.code:
            inform('兼容控制台启动失败：本机 8090 端口可能被占用，请查看启动日志。',
                   messagebox=messagebox, stream=stream)
        return int(exc.code or 0)
    return 0
