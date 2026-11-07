#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鲸鱼娘 QQ 机器人 · 原生 Windows 控制台
- PyQt6 无边框窗口，自定义标题栏（拖拽 / 最小化 / 最大化 / 关闭）
- Windows 11 云母片效果（Mica），默认不透明度 70%
- 背景色可自定义（叠加在 Mica 层之上）
- 通过 HTTP 轮询本地 FastAPI（http://127.0.0.1:<端口>/api/，端口见 resolve_port()）
- 自动在后台启动 FastAPI 服务
"""
import ctypes
import json
import logging
import os
import pathlib
import subprocess
import shutil
import sys
import threading
import queue
import time
from pathlib import Path

import base64
import requests
from PyQt6.QtCore import (QEvent, QObject, QPoint, QRect, QSize, Qt, QThread, QTimer,
                          QUrl, pyqtSignal, pyqtSlot)
from PyQt6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath,
                         QPalette, QPen, QPixmap)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLayout, QLineEdit,
    QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizeGrip,
    QSlider, QSizePolicy, QSpinBox, QSplitter, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QHeaderView,
)

from src.gui.config import ensure_migrated as load_gui_config, save_gui_config
from src.gui.agent_settings import AgentGrowthGroup, AgentToolsGroup
from src.gui.appearance import (
    import_background, resolve_background, clear_background, ensure_default_background,
)
from src.gui.dialogs import ask_confirm, ask_text, tell
from src.plugin_files import import_plugin_files, merge_uploaded_files, upload_options, IMAGE_EXTENSIONS
from src.gui.keyword_rules import KeywordRulesEditor
from src.gui.stickers import StickerLibraryDialog
from src.gui import file_dialog_state, file_dialogs
from src.gui.diagnostics import safe_slot
from src.gui.menu_style import menu_stylesheet, style_menu
from src.gui.panel_requests import PanelRequests
from src.mention import explain as mention_explain
from src.mention import split_aliases as split_wake_words
from src import plugin_settings_io
from src.runtime_identity import APP_ID, APP_VERSION, backend_identity_error
from src.bot_control_api import write_endpoint as write_console_endpoint

# ── 路径 ───────────────────────────────────────────────────────────────────────
# 打包成单文件 exe（PyInstaller --onefile）后，__file__ 指向**临时解包目录**
# (_MEIxxxx)，而不是 exe 所在目录。如果还按 __file__ 推路径，配置、背景图、
# 插件状态都会写进临时目录 —— 关掉程序就全丢（用户看到的是"设置不保存"）。
# 所以：冻结运行时，可写数据一律放在 exe 旁边；只有随包资源才用解包目录。
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WhaleGirlQQ"
    ROOT.mkdir(parents=True, exist_ok=True)
    BUNDLE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    for resource in (BUNDLE / "styles").rglob("*.yaml"):
        target = ROOT / resource.relative_to(BUNDLE)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(resource, target)
else:
    ROOT = Path(__file__).resolve().parent
    BUNDLE = ROOT
GUI_CFG = ROOT / "data" / "gui_config.json"

# ── 控制台端口：唯一真相源 ────────────────────────────────────────────────────
# 优先级：命令行 --port N > 环境变量 WHALEGIRL_PORT > config.qq.yaml 的
# qq.console_port > 兜底 27499。PORT / BASE_URL / 起后端的 --port / 端口冲突提示
# 全部读这一份结果，任何地方都不许再写死端口数字。
DEFAULT_CONSOLE_PORT = 27499
CONSOLE_PORT_ENV = "WHALEGIRL_PORT"
_LOG = logging.getLogger(__name__)


def _port_or_none(value):
    """把候选值转成 1-65535 的端口；不合法返回 None（由调用方记日志并降级）。"""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _argv_port(argv):
    """取 --port N / --port=N 的原始值；没带就返回 None。"""
    items = list(argv or [])
    for index, item in enumerate(items):
        text = str(item)
        if text == "--port" and index + 1 < len(items):
            return items[index + 1]
        if text.startswith("--port="):
            return text.split("=", 1)[1]
    return None


def _config_port(config_path):
    """读 config.qq.yaml 的 qq.console_port；读不到返回 None。

    必须挂在已有的 qq: 段下：src/qq_app.py 只保留 ai/qq/style/qq_only 四段，
    顶层新键保存时会被丢掉。
    """
    try:
        import yaml
        with Path(config_path).open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return (data.get("qq") or {}).get("console_port")
    except Exception:
        return None


def resolve_port(argv=None, environ=None, config_path=None) -> int:
    """解析控制台端口：--port N > WHALEGIRL_PORT > qq.console_port > 27499。

    非法值（非数字、不在 1-65535）只记一句日志并降级到下一条来源，绝不抛异常——
    端口配错也不该让控制台起不来。
    """
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    config_path = (ROOT / "config.qq.yaml") if config_path is None else config_path
    sources = (
        ("命令行 --port", _argv_port(argv)),
        ("环境变量 " + CONSOLE_PORT_ENV, environ.get(CONSOLE_PORT_ENV)),
        ("config.qq.yaml 的 qq.console_port", _config_port(config_path)),
    )
    for name, raw in sources:
        if raw is None:
            continue
        port = _port_or_none(raw)
        if port is None:
            _LOG.warning("控制台端口来源 %s 的值 %r 不合法（需 1-65535），已改用下一级来源", name, raw)
            continue
        return port
    return DEFAULT_CONSOLE_PORT


def backend_argv(port: int) -> list:
    """起后台子进程（非冻结运行时）的完整命令行；端口只认传入的这一份。"""
    return [sys.executable, "-m", "uvicorn", "src.qq_app:create_app",
            "--factory", "--host", "127.0.0.1", "--port", str(int(port))]


PORT = resolve_port()
BASE_URL = f"http://127.0.0.1:{PORT}"
# ── 兼容模式（安全模式）────────────────────────────────────────────────────────
# 触发条件（任一）：
#   1. 命令行带 --safe-mode（gui_launcher 在"上次原生崩溃"或"用户勾了兼容模式"时加）；
#   2. gui_config.json 里 safe_mode=true（外观页勾选）。
# 安全模式做的事：系统边框代替无边框、不要半透明/云母/背景图、强制软件渲染、
# 不设高 DPI 取整策略（远程桌面缩放也是嫌疑点）。这些正是 Qt 在
# "远程会话 + 多个虚拟显示适配器"下做平台集成时会崩的地方（实测 0xC0000005）。
SAFE_MODE = ("--safe-mode" in sys.argv) or os.environ.get("WHALEGIRL_SAFE_MODE") == "1"
if SAFE_MODE:
    os.environ.setdefault("QT_OPENGL", "software")
# 离屏（QT_QPA_PLATFORM=offscreen）只出现在测试与打包自检里：那种环境不该去探测/启动
# 真实后台 —— 否则测试跑久了会弹模态对话框、甚至占住控制台端口。
AUTO_BACKEND = os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen"


def local_request(method, url, **kwargs):
    """本机后台不经过系统代理，避免其他电脑的全局代理截走控制台请求。"""
    kwargs['proxies'] = {'http': None, 'https': None, 'all': None}
    return getattr(requests, method)(url, **kwargs)


# 图片过滤：与 src/gui/appearance.py 的扩展名白名单保持一致
IMAGE_FILTER = "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"

# ── 图标 ──────────────────────────────────────────────────────────────────────
# 图标来自 assets/icons（由 tools/setup_icons.py 从图标库随机、不重复地分配后复制进来），
# 分配表在 assets/icon-map.json。打包后这些资源在 BUNDLE 里，所以走 BUNDLE 而不是 ROOT。
ICON_DIR = BUNDLE / "assets" / "icons"
try:
    ICON_MAP = json.loads((BUNDLE / "assets" / "icon-map.json").read_text("utf-8"))
except Exception:
    ICON_MAP = {}


def slot_icon(name: str) -> QIcon:
    """按槽位名取图标；取不到返回空 QIcon（Qt 会用默认图标，不会崩）。

    名字带 slot_ 前缀是刻意的：TitleBar 里有个局部变量叫 ``icon``，
    若这里也叫 ``icon`` 容易在后续编辑中撞名（Python 会把它当局部变量）。
    """
    filename = ICON_MAP.get(name)
    if not filename:
        return QIcon()
    path = ICON_DIR / filename
    return QIcon(str(path)) if path.is_file() else QIcon()


def app_icon() -> QIcon:
    """任务栏/窗口用的主图标：优先多尺寸 ico，退回分配表里的那个 PNG。"""
    ico = BUNDLE / "icon.ico"
    if ico.is_file():
        return QIcon(str(ico))
    return slot_icon("app")

# ── 配色（银白色 / 亮色主题）─────────────────────────────────────────────────
BG       = "#F4F6F9"   # 银白：窗口与容器底色
SILVER_TOP = "#F7F9FC"  # 模糊蒙层的银白目标色
CARD     = "#FFFFFF"   # 卡片
BORDER   = "#D3DAE3"   # 银灰描边
ACCENT   = "#2FA8A0"   # 青色点缀（亮底上要够深才够对比）
ACCENT2  = "#248780"
TEXT     = "#1F2733"   # 深灰正文
MUTED    = "#5F6B7A"   # 次级文字
ERROR    = "#C0392B"
SUCCESS  = "#1E8E5A"
FIELD    = "#FFFFFF"   # 输入框底色
ACCENT_FG = "#FFFFFF"  # 青色按钮上的文字

# 弹出层规则：QMenu / QToolTip / 无父窗口的 QMessageBox / 下拉列表 / 文件对话框
# 都是**独立顶层窗口**，MainWindow 上的样式继承不到它们，系统深色模式下就退回
# 默认深色配色（黑框 + 暗字）。这段写进 STYLESHEET，DIALOG_STYLESHEET 与窗口级样式
# 一起继承；main() 里再把它装到 QApplication 上，无父窗口的弹窗才盖得到。
POPUP_STYLESHEET = menu_stylesheet(
    card=CARD, text=TEXT, muted=MUTED, border=BORDER, accent=ACCENT,
) + f"""
QToolTip {{ background-color: {CARD}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px 8px; }}
QDialog {{ background: {BG}; color: {TEXT}; }}
QMessageBox {{ background: {BG}; }}
QMessageBox QLabel {{ color: {TEXT}; background: transparent; }}
QMessageBox QWidget {{ background: {BG}; }}
QMessageBox QPushButton {{ min-width: 76px; }}
QComboBox QAbstractItemView {{
    background-color: {FIELD}; color: {TEXT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT}; selection-color: {ACCENT_FG};
}}
QFileDialog {{ background: {BG}; }}
QFileDialog QListView, QFileDialog QTreeView, QFileDialog QTableView {{
    background-color: {FIELD}; color: {TEXT}; border: 1px solid {BORDER};
}}
QFileDialog QLineEdit {{ background-color: {FIELD}; color: {TEXT}; }}
QFileDialog QLabel, QFileDialog QCheckBox {{ color: {TEXT}; }}
"""

STYLESHEET = f"""
QWidget {{ background: transparent; color: {TEXT}; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 13px; }}
QLabel {{ color: {TEXT}; background: transparent; border: none; }}
/* 表头必须有实色底：QHeaderView 继承全局透明背景时，Windows 原生样式只会画出黑底。 */
QHeaderView {{ background: #F7F9FC; }}
QHeaderView::section {{
    background: #F7F9FC; color: {TEXT};
    border: 0; border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER};
    padding: 4px 6px; font-weight: 600;
}}
QTableCornerButton::section {{ background: #F7F9FC; border: 0; }}
QFrame[panel="true"] {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLineEdit, QTextEdit, QComboBox {{
    background: {FIELD}; border: 1px solid {BORDER}; border-radius: 7px;
    color: {TEXT}; padding: 8px; selection-background-color: {ACCENT};
}}
QComboBox::drop-down {{ border: 0; }}
QComboBox::down-arrow {{ color: {MUTED}; }}
QPushButton {{
    background: {ACCENT}; color: {ACCENT_FG}; border: 0; border-radius: 7px;
    padding: 8px 16px; font-weight: bold;
}}
QPushButton:hover {{ background: #37bdb4; }}
QPushButton:pressed {{ background: {ACCENT2}; }}
QPushButton:disabled {{ background: #E3E8EE; color: {MUTED}; }}
QPushButton.secondary {{
    background: #E8EDF3; color: {TEXT}; border: 1px solid {BORDER};
}}
QPushButton.secondary:hover {{ background: #DDE5EE; }}
QPushButton.danger {{ background: #C0392B; color: #FFFFFF; }}
QScrollBar:vertical {{ background: #EDF1F5; width: 8px; border-radius: 4px; }}
QScrollBar::handle:vertical {{ background: #C2CCD8; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; border: 0; padding: 10px 18px;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{ background: {CARD}; color: {ACCENT}; border: 1px solid {BORDER}; border-bottom: 0; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QCheckBox {{ color: {TEXT}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; background: {FIELD};
    border: 1px solid {BORDER}; border-radius: 4px;
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSlider::groove:horizontal {{
    height: 4px; background: {BORDER}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 16px; height: 16px; margin: -6px 0;
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
""" + POPUP_STYLESHEET

# 弹窗用的样式。
# STYLESHEET 里的 POPUP_STYLESHEET 已经带着 QDialog/QMessageBox/QMenu 的规则，
# 这份是给"手动装样式的对话框"再补输入框提示色用的（继承上面全部规则）。
DIALOG_STYLESHEET = STYLESHEET + f"""
QDialog {{ background: {BG}; }}
QMessageBox {{ background: {BG}; }}
QMessageBox QLabel {{ color: {TEXT}; background: transparent; }}
QMessageBox QWidget {{ background: {BG}; }}
QLineEdit {{ background: {FIELD}; color: {TEXT}; }}
QLineEdit::placeholder {{ color: {MUTED}; }}
QTextEdit {{ background: {FIELD}; color: {TEXT}; }}
"""


def _safe_float(value, default: float, low: float, high: float) -> float:
    """界面用的安全数值转换：手写配置里的 "高"/None/列表一律回退默认值并夹在区间内。

    Qt 槽里抛异常会弹"操作未完成"（装 excepthook 时）或直接 abort 进程
    （self-test 路径没装 excepthook），所以界面读取外部数据时不能裸 float()/int()。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:              # NaN
        return float(default)
    return max(low, min(high, number))


def backend_error(timeout=2):
    try:
        response=local_request('get', f'{BASE_URL}/api/runtime',timeout=timeout)
        if response.status_code!=200:
            return f'检测到旧版或其他后台占用{PORT}。请关闭旧后台后重新打开当前控制台。'
        return backend_identity_error(response.json(),ROOT)
    except Exception:
        return '当前后台尚未连接，请稍等；若持续出现，请重新打开控制台。'


def backend_probe(timeout: float = 1.5):
    """探测控制台端口上到底有什么：返回 ``/api/runtime`` 的 JSON，探测不到返回 None。

    与 ``backend_error`` 的区别：那个只回一句中文错误，这个把"是谁占着"的原始信息
    交给调用方判断 —— 是我们自己的后台就该接管，是别人的进程才该提示用户。
    """
    try:
        response = local_request('get', f'{BASE_URL}/api/runtime', timeout=timeout)
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def plan_backend_start(probe) -> str:
    """根据端口探测结果决定启动方式（纯函数，便于离线测试）。

    - ``None``                              → ``start``：端口空着，自己起后台
    - 本程序 + 有窗口                        → ``focus``：另一个控制台还开着（复用或接管）
    - 本程序 + 没有窗口                      → ``takeover``：上次留下的无窗口僵尸，请它退出
    - 别的程序（或返回体不是我们的 app 标记）→ ``foreign``：提示用户，绝不静默退出
    """
    if not probe:
        return 'start'
    if str(probe.get('app') or '') != APP_ID:
        return 'foreign'
    return 'focus' if probe.get('windowed') else 'takeover'


def request_backend_quit(timeout: float = 3.0) -> bool:
    """请旧后台优雅退出（停机器人 → 关进程）。返回是否成功发出请求。"""
    try:
        response = local_request('post', f'{BASE_URL}/api/runtime/quit', json={}, timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def wait_port_free(timeout: float = 8.0, interval: float = 0.3) -> bool:
    """等控制台端口空出来（旧后台退出后会释放）。"""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if backend_probe(timeout=0.5) is None:
            # 再确认一次：正在关闭的后台可能短暂不回包
            _time.sleep(interval)
            if backend_probe(timeout=0.5) is None:
                return True
        _time.sleep(interval)
    return False


def api_get(path: str, timeout: int = 3) -> dict:
    error=backend_error(min(timeout,2))
    if error:
        return {'success':False,'message':error}
    try:
        return local_request('get', f"{BASE_URL}/api/{path}", timeout=timeout).json()
    except Exception:
        return {}


def api_post(path: str, data: dict = None, timeout: int = 10) -> dict:
    error=backend_error(min(timeout,2))
    if error:
        return {'success':False,'message':error}
    try:
        return local_request('post', f"{BASE_URL}/api/{path}", json=data or {}, timeout=timeout).json()
    except Exception as e:
        return {"success": False, "message": str(e)}


def api_delete(path: str, timeout: int = 15) -> dict:
    """DELETE 请求。失败时返回 {success: False, message}，不抛异常。"""
    error=backend_error(min(timeout,2))
    if error:
        return {'success':False,'message':error}
    try:
        return local_request('delete', f"{BASE_URL}/api/{path}", timeout=timeout).json()
    except Exception as e:
        return {"success": False, "message": str(e)}


_API_TASKS = set()


class ApiTask(QThread):
    """通用"后台跑一次接口调用"线程（避免 Qt 主线程被网络/模型调用卡住）。

    背景（用户反馈"QQ 端异常卡死"）：控制台里有些按钮会**同步**调自己的后台接口，
    而这些接口有时要等模型（形象图识别实测 9 秒）、等网页、甚至等机器人重启。
    同步调用会把 Qt 事件循环堵住 —— Windows 会在应用日志里记 `AppHangTransient`，
    用户看到的就是"未响应/卡死"。所有超过一两百毫秒的调用都应该走这里。

    用法：``task = ApiTask(kind, path, payload, timeout)`` 然后连 ``completed`` 信号。
    ``kind`` 只是回传给回调用来区分是哪个请求（沿用原有字符串，回调逻辑不用改）。
    """

    completed = pyqtSignal(str, dict)

    def __init__(self, kind: str, path: str, payload: dict | None = None,
                 timeout: int = 30, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.path = path
        self.payload = payload
        self.timeout = timeout
        self.result: dict | None = None      # 供"轮询兜底"读取（见 _ApiDispatcher）
        # 弹窗可能先关闭；独立保留到 finished，避免随控件销毁而提前回收线程。
        _API_TASKS.add(self)
        self.finished.connect(lambda: _API_TASKS.discard(self))

    def run(self):                                  # pragma: no cover - 线程体靠集成测
        try:
            if str(self.path).startswith('GET '):
                result = api_get(str(self.path)[4:].strip(), timeout=self.timeout)
            else:
                result = api_post(self.path, self.payload, timeout=self.timeout)
        except Exception as exc:
            result = {"success": False, "message": str(exc)}
        if not isinstance(result, dict):
            result = {"success": False, "message": "后台返回了无法解析的内容"}
        self.result = result
        self.completed.emit(self.kind, result)


class _ApiDispatcher(QObject):
    """把后台线程的结果**转回 Qt 主线程**再执行回调。

    两条投递路径（都只在主线程执行回调，重复投递由 pop 保证只跑一次）：

    1. ``completed`` 信号（有接收者 QObject，Qt 会排队投递到主线程）；
    2. **定时器兜底轮询**：某些环境（例如测试里手动 ``processEvents``、或事件循环被
       别的嵌套循环占着）排队信号可能迟迟不投递，实测出现过"任务 finished 了、
       回调还挂着"的情况。定时器到点后看 ``isFinished()`` 直接取结果，
       保证回调一定会跑，不会再出现"按钮转圈圈没反应"。
    """

    def __init__(self, owner):
        super().__init__(owner)
        self._calls: dict = {}
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._poll)

    def watch(self, task, callback, holder) -> None:
        self._calls[task] = (callback, holder)
        task.completed.connect(self._deliver)
        self._start_timer()

    # 定时器是控件的子对象：控件（含本对象）被删掉之后，任何一次迟到的
    # 超时/信号都会踩到已删除的 QTimer，PyQt 里这就是未捕获 RuntimeError
    # → 进程直接 abort。三个入口统一挡一下，别让它变成偶发闪退。
    def _start_timer(self) -> None:
        try:
            if not self._timer.isActive():
                self._timer.start()
        except RuntimeError:
            self._calls.clear()

    def _stop_timer(self) -> None:
        try:
            self._timer.stop()
        except RuntimeError:
            pass

    @pyqtSlot(str, dict)
    def _deliver(self, _kind: str, result: dict) -> None:
        # completed 发出时 run() 可能尚未返回；等 finished 后才释放线程。
        try:
            task = self.sender()
            if task is not None and task.isFinished():
                self._finish(task)
        except RuntimeError:
            self._calls.clear()

    def _poll(self) -> None:
        try:
            for task in list(self._calls):
                if task.isFinished():
                    self._finish(task)
        except RuntimeError:
            self._calls.clear()
        if not self._calls:
            self._stop_timer()

    def _finish(self, task) -> None:
        entry = self._calls.pop(task, None)
        if entry is None:                     # 已经投递过（信号与轮询只跑一次）
            return
        callback, holder = entry
        if holder is not None:
            try:
                holder.remove(task)
            except ValueError:
                pass
        if callback is not None:
            callback(task.result if task.result is not None else {})
        # 最后一个任务收工就停表：留着空转的定时器只会活到控件销毁之后再去踩空。
        if not self._calls:
            self._stop_timer()


def run_api_async(widget, path: str, payload: dict | None = None, *, timeout: int = 30,
                  callback=None, busy: str = "") -> "ApiTask":
    """在后台线程调一次接口，完成后回到 Qt 主线程执行 ``callback(result)``。

    **凡是可能超过一两百毫秒的调用都该走这里**：同步调接口会把 Qt 事件循环堵住，
    Windows 会在应用日志里记 `AppHangTransient`，用户看到的就是"未响应/卡死"
    （实测：形象图识别 9 秒、插件市场浏览最长 180 秒、保存配置会重启机器人）。
    """
    task = ApiTask(path, path, payload, timeout)
    holder = getattr(widget, "_api_tasks", None)
    if holder is None:
        holder = widget._api_tasks = []
    holder.append(task)
    owner = widget
    while owner.parentWidget() is not None:
        owner = owner.parentWidget()
    task._owner_window = owner
    dispatcher = getattr(widget, "_api_dispatcher", None)
    if dispatcher is None:
        dispatcher = widget._api_dispatcher = _ApiDispatcher(widget)
    dispatcher.watch(task, callback, holder)
    if busy:
        label = getattr(widget, "notice", None)
        if label is not None:
            label.setText(busy)
    task.start()
    return task


_PROBE_TASKS = set()


class StatusTask(ApiTask):
    def run(self):
        try:
            self.result = api_get('status')
        except Exception as exc:
            self.result = {'success': False, 'message': str(exc)}


class ApiProbeWorker(QThread):
    """后台线程里调一次控制台 API（获取模型列表 / 测试连接）。

    探测最长要等上游 20 秒，必须在 Qt 主线程之外跑，否则整个窗口卡住。
    回调只带一个 dict，和 src/gui/conversation.py 的 ReviewWorker 同一套写法。
    """
    completed = pyqtSignal(dict)

    def __init__(self, path: str, payload: dict, timeout: int = 40):
        super().__init__()
        self.path = path
        self.payload = payload
        self.timeout = timeout
        _PROBE_TASKS.add(self)
        self.finished.connect(lambda: _PROBE_TASKS.discard(self))

    def run(self):
        try:
            result = api_post(self.path, self.payload, timeout=self.timeout)
        except Exception as exc:
            result = {"success": False, "ok": False, "message": f"控制台未响应：{exc}"}
        if not isinstance(result, dict):
            result = {"success": False, "ok": False, "message": "控制台返回内容无法解析"}
        self.result = result
        self.completed.emit(result)


# DWM 属性号
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
# 系统背景类型：1=无 2=云母 3=亚克力 4=标签页云母
BACKDROP_NONE, BACKDROP_MICA, BACKDROP_ACRYLIC, BACKDROP_TABBED = 1, 2, 3, 4


def apply_mica(hwnd: int, dark: bool = False, backdrop: int = BACKDROP_MICA) -> bool:
    """对窗口句柄应用 Windows 11 云母片（Mica）。

    ``backdrop`` 选系统背景类型（见上面的 BACKDROP_*）。系统的云母片本身**没有**
    强度参数，所以"模糊度"由两层实现：这里决定用哪种背板（0 = 不用），
    窗口内的银白蒙层再按模糊度决定透出多少（见 BackgroundFrame）。
    """
    try:
        dwm = ctypes.windll.dwmapi
        mode = ctypes.c_int(1 if dark else 0)
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(mode), ctypes.sizeof(mode))
        value = ctypes.c_int(int(backdrop))
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(value), ctypes.sizeof(value))
        return True
    except Exception:
        return False


def backdrop_for_blur(blur: float, available: bool = True) -> int:
    """按模糊度挑系统背景类型：0 → 不用背板；低模糊用亚克力；其余用云母。

    系统只提供几种固定背板，没有连续强度参数，所以这里做离散映射，
    连续变化由窗口内的蒙层负责。
    """
    if not available or blur <= 0.0:
        return BACKDROP_NONE
    if blur < 0.5:
        return BACKDROP_ACRYLIC
    return BACKDROP_MICA


def scrim_alpha_for_blur(blur: float) -> float:
    """模糊度 → 银白蒙层的不透明度。

    0 = 完全看得清桌面/背景图；1 = 基本只剩银白，细节全被盖住（观感上就是"很糊"）。
    """
    try:
        value = float(blur)
    except (TypeError, ValueError):
        value = 0.0
    value = min(max(value, 0.0), 1.0)
    return value * 0.85


def image_scrim_alpha(blur: float, blur_explicit: bool, has_image: bool = True) -> float:
    """有背景图时，盖在图片上的银白蒙层强度。

    用户没动过模糊度滑块时返回 0：选了背景图就先看到图本身，想加蒙层自己拖滑块；
    拖过之后按 scrim_alpha_for_blur 的老公式叠加（公式与无图时完全一致）。
    "没动过" = 配置里没有 blur_explicit 或其为 False（老配置即如此）。
    """
    if not has_image or not blur_explicit:
        return 0.0
    return scrim_alpha_for_blur(blur)


PANEL_GLASS_EXPONENT = 1.9  # 有背景图时面板白量的衰减指数，见 panel_alpha


def panel_alpha(panel_transparency: float, has_image: bool = False) -> float:
    """「面板透明度」滑块 → 面板白底的不透明度（0~1）。这是唯一的口径来源。

    没有背景图时是线性 `1 - 滑块`：底面本来就是纯色，白底薄一点只是页面发灰。

    有背景图时同一块白底就是盖在照片上的纱：默认 35% 滑块给出白色通道 166/255，
    实测照片饱和度只剩 35%、整片区域白化 65% —— 用户看到的就是"整个界面发白"。
    这时白量按 `(1 - 滑块) ** 1.9` 衰减：滑块两端语义不变（0 = 面板完全不透明、
    100% = 完全透明），中间档明显变薄。默认 35% → 112/255，真实背景图最暗处
    （用户那张图的深色裙摆，RGB 81,82,104）正文对比度仍有 4.62:1，WCAG AA 要 4.5。
    """
    try:
        panel = float(panel_transparency)
    except (TypeError, ValueError):
        panel = 0.0
    panel = min(max(panel, 0.0), 1.0)
    clear = 1.0 - panel
    if has_image:
        clear = clear ** PANEL_GLASS_EXPONENT
    return clear


def overlay_color(base: str, blur: float) -> QColor:
    """把"底色 + 模糊蒙层"合成成一个纯色，供自绘使用。

    返回**不透明**颜色：模糊度 0 时是底色本身，随模糊度升高向银白（#F7F9FC）靠拢
    —— 在完全不用云母片（没有真正背景模糊可看）时，"模糊度"就靠这个体现。
    """
    color = QColor(base)
    if not color.isValid():
        color = QColor(BG)
    mix = scrim_alpha_for_blur(blur)
    if mix <= 0:
        return color
    silver = QColor(SILVER_TOP)
    blend = min(mix * 0.45, 0.45)  # 上限 45%：保持"银白"而不是越调越白得发飘
    return QColor(
        int(color.red() * (1 - blend) + silver.red() * blend),
        int(color.green() * (1 - blend) + silver.green() * blend),
        int(color.blue() * (1 - blend) + silver.blue() * blend),
    )


MIN_WINDOW_OPACITY = 0.35      # 窗口不透明度的下限（再低就看不清、点不到）
MAX_TRANSPARENCY_PERCENT = 80  # 滑块上限：再高就只透桌面，等于自废


def window_opacity_from_transparency(transparency: float) -> float:
    """透明度 → Qt 的窗口不透明度。

    ⚠️ Qt 在不透明度 == 1.0 时把窗口当作完全不透明，会丢掉 WA_TranslucentBackground
    的逐像素透明，云母片背板也随之消失。所以"0% 透明"映射到 0.999（视觉上完全不透明，
    但保留透明通道）。

    注意副作用：正因为映射到 0.999，"0% 透明"时背板仍会极轻微地透出来。想要纯银白，
    把背景叠加色设成不透明的银白即可完全盖住。
    """
    try:
        value = float(transparency)
    except (TypeError, ValueError):
        value = 0.0
    value = min(max(value, 0.0), 1.0)
    # 下限 0.35：以前 100% 透明 = opacity 0.0，窗口**完全看不见也点不到**，
    # 而这个值存在 gui_config.json 里，重装都不覆盖用户数据 → 用户以为"重装也修不好"。
    return round(min(max(1.0 - value, MIN_WINDOW_OPACITY), 0.999), 4)


class BackgroundFrame(QFrame):
    """容器：先铺纯色底，再按 cover 语义绘制背景图，保证不变形且不留黑边。

    为什么不用样式表 background-image：Qt 样式表不支持 background-size，
    且读取中文/空格路径的图片会失败（实测 Could not create pixmap）。
    QPixmap 对同样的路径加载正常，所以这里自绘。
    """

    def __init__(self):
        super().__init__()
        self._color = BG
        self._pixmap: QPixmap | None = None
        self._scaled: QPixmap | None = None
        self._scaled_size = (0, 0)
        self._blur = 0.70
        self._blur_explicit = False  # 用户是否亲手设置过模糊度（未设置 = 有图也不叠蒙层）
        self._alpha = 1.0  # 容器自身填充的不透明度，由"透明度"反推

    def set_background_color(self, color: str):
        self._color = color or BG
        self.update()

    def set_background_pixmap(self, pixmap: QPixmap | None):
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._scaled = None
        self._scaled_size = (0, 0)
        self.update()

    def has_background_image(self) -> bool:
        """真的画出了一张背景图吗——面板白底要不要让路给照片，全看这一条。"""
        return self._pixmap is not None

    def _scaled_pixmap(self, width: int, height: int):
        """按窗口尺寸缓存缩放结果：以前每次重绘都对原图做一次平滑缩放，
        4K 背景会让整窗口拖动/滚动明显发卡，文字也跟着发虚。"""
        pixmap = self._pixmap
        if pixmap is None:
            return None
        # 按设备像素比缩放：150% 缩放下如果只按逻辑尺寸缩放，图片会被放大插值，
        # 看起来整窗发虚（用户反馈"控制台有些模糊"）。多缩一份物理像素就清晰了。
        try:
            ratio = float(self.devicePixelRatioF()) or 1.0
        except Exception:
            ratio = 1.0
        key = (int(width), int(height), round(ratio, 3))
        if self._scaled is not None and self._scaled_size == key:
            return self._scaled
        target = self._scaled_from(pixmap, width, height, ratio)
        target.setDevicePixelRatio(ratio)
        self._scaled = target
        self._scaled_size = key
        return self._scaled

    @staticmethod
    def _scaled_from(pixmap, width, height, ratio):
        target_w, target_h = int(width * ratio), int(height * ratio)
        # 大图一次缩到很小容易糊：先按 2 的幂逐级减半，再做最后一次平滑缩放。
        work = pixmap
        while work.width() > target_w * 2 and work.height() > target_h * 2:
            work = work.scaled(max(target_w, work.width() // 2), max(target_h, work.height() // 2),
                               Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.FastTransformation)
        return work.scaled(target_w, target_h,
                           Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)

    def set_blur(self, blur: float, explicit: bool | None = None):
        """0~1：越高，盖在背景上的银白蒙层越厚 = 看起来越糊。

        explicit=True 表示用户主动拖过模糊度滑块，只有这时才在背景图上叠蒙层；
        传 None（老调用点）表示不改动"是否显式设置过"的现有状态。
        """
        try:
            value = float(blur)
        except (TypeError, ValueError):
            value = 0.0
        self._blur = min(max(value, 0.0), 1.0)
        if explicit is not None:
            self._blur_explicit = bool(explicit)
        self.update()

    def set_fill_alpha(self, alpha: float):
        """容器填充的不透明度 = 1 - 窗口透明度。

        必须跟着窗口透明度走：容器如果永远画成不透明，把"透明度"调高也只会
        穿透到桌面被这层挡住 —— 滑块看起来就没用了。
        """
        try:
            value = float(alpha)
        except (TypeError, ValueError):
            value = 1.0
        self._alpha = min(max(value, 0.0), 1.0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        # 底色 = 叠加色与银白蒙层按模糊度混合。0% 透明（窗口不透明）时，
        # 这个底就是用户看到的全部，所以模糊度必须在这里就体现出来。
        base = overlay_color(self._color, self._blur)
        base.setAlphaF(self._alpha)
        painter.fillRect(self.rect(), base)
        pixmap = self._pixmap
        if pixmap is not None:
            area = self.rect()
            if area.width() > 0 and area.height() > 0:
                scale = max(area.width() / pixmap.width(), area.height() / pixmap.height())
                width = max(1, int(pixmap.width() * scale))
                height = max(1, int(pixmap.height() * scale))
                target = QRect(
                    int((area.width() - width) / 2),
                    int((area.height() - height) / 2),
                    width,
                    height,
                )
                if self._alpha < 1.0:
                    painter.setOpacity(self._alpha)
                scaled = self._scaled_pixmap(width, height)
                painter.drawPixmap(target, scaled if scaled is not None else pixmap)
                painter.setOpacity(1.0)
                # 有背景图时，蒙层改画在图片之上，否则"模糊度"对图片毫无作用；
                # 但用户没动过滑块就先不叠，背景图按原样显示（默认看得清图）。
                alpha = image_scrim_alpha(self._blur, self._blur_explicit)
                if alpha > 0:
                    scrim = QColor(255, 255, 255)
                    scrim.setAlphaF(alpha * self._alpha)
                    painter.fillRect(area, scrim)
        painter.end()
        super().paintEvent(event)


# ── 自定义标题栏 ──────────────────────────────────────────────────────────────
class TitleBar(QWidget):
    """可拖拽的自定义标题栏，包含最小化/最大化/关闭按钮。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedHeight(44)
        self._drag_pos = None
        self._maximized = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.setSpacing(8)

        # 图标 + 标题（图标来自图标库，不再是 emoji）
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(22, 22))
        mark.setFixedSize(24, 24)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel(f"鲸鱼娘 · QQ 控制台 {APP_VERSION}")
        title.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-weight: bold;")

        layout.addWidget(mark)
        layout.addWidget(title)
        layout.addStretch()

        # 窗口按钮
        for text, slot, cls in [("─", self._minimize, ""), ("□", self._toggle_max, ""), ("✕", self._close, "danger")]:
            btn = QPushButton(text)
            if slot == self._toggle_max:
                self._max_button = btn
            btn.setFixedSize(38, 32)
            btn.setProperty("class", cls)
            btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {MUTED}; border: 0; border-radius: 6px; font-size: 14px; }}
                QPushButton:hover {{ background: {'#7a1f1f' if cls=='danger' else '#253748'}; color: {TEXT}; }}
            """)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    def _minimize(self):
        self.window().showMinimized()

    def _toggle_max(self):
        """按**窗口真实状态**切换，不再依赖自己记的布尔值。

        以前用 self._maximized：窗口若是双击标题栏最大化的、或被系统 Aero Snap 拉满的，
        这个标记是错的，点按钮就会"看着没反应"（用户反馈过）。
        """
        w = self.window()
        if w.isMaximized():
            w.showNormal()
        else:
            w.showMaximized()
        self._sync_max_glyph()

    def _sync_max_glyph(self):
        """最大化时按钮显示"还原"符号（❐），否则显示"□"。"""
        button = getattr(self, "_max_button", None)
        if button is not None:
            button.setText("❐" if self.window().isMaximized() else "□")

    def _close(self):
        self.window().close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, event):
        self._toggle_max()


# ── 状态卡片 ──────────────────────────────────────────────────────────────────
class StatusCard(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setProperty("panel", True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.value_lbl = QLabel("读取中")
        self.value_lbl.setStyleSheet(f"color: {TEXT}; font-size: 15px; font-weight: bold;")
        self.value_lbl.setWordWrap(True)
        self.detail_lbl = QLabel("")
        self.detail_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.detail_lbl.setWordWrap(True)
        layout.addWidget(self.title_lbl)
        layout.addWidget(self.value_lbl)
        layout.addWidget(self.detail_lbl)

    def update(self, value: str, detail: str = "", ok: bool | None = None):
        self.value_lbl.setText(value)
        self.detail_lbl.setText(detail)
        if ok is True:
            self.value_lbl.setStyleSheet(f"color: {SUCCESS}; font-size: 15px; font-weight: bold;")
        elif ok is False:
            self.value_lbl.setStyleSheet(f"color: {ERROR}; font-size: 15px; font-weight: bold;")
        else:
            self.value_lbl.setStyleSheet(f"color: {TEXT}; font-size: 15px; font-weight: bold;")


# ── 控制台标签页 ──────────────────────────────────────────────────────────────
class ConsoleTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # 状态卡片行
        cards_layout = QHBoxLayout()
        self.card_running = StatusCard("机器人服务")
        self.card_conn = StatusCard("QQ 接入")
        self.card_model = StatusCard("模型 / API")
        self.card_memory = StatusCard("记忆 / 省 token")
        for c in [self.card_running, self.card_conn, self.card_model, self.card_memory]:
            cards_layout.addWidget(c)
        layout.addLayout(cards_layout)

        # 操作按钮
        btn_layout = QHBoxLayout()
        self.btn_start = QPushButton("▶ 启动机器人")
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setProperty("class", "secondary")
        self.btn_test_ai = QPushButton("测试 AI")
        self.btn_test_ai.setProperty("class", "secondary")
        self.btn_test_qq = QPushButton("测试 QQ")
        self.btn_test_qq.setProperty("class", "secondary")
        self.btn_capabilities = QPushButton("平台能力")
        self.btn_capabilities.setProperty("class", "secondary")
        self.btn_capabilities.setToolTip(
            "检查本账号的官方接口能力（群@消息、单聊、按钮交互是否开通）；需要先停止机器人")
        self.btn_voice = QPushButton("语音组件")
        self.btn_voice.setProperty("class", "secondary")
        self.btn_voice.setToolTip("检查本地 WhaleVoice 组件；本机朗读/状态")
        self.btn_voice.clicked.connect(self.show_voice_status)
        self.btn_send_voice = QPushButton("发送语音")
        self.btn_send_voice.setProperty("class", "secondary")
        self.btn_send_voice.setToolTip("把本机 WAV/MP3/SILK 作为 QQ 语音条发送；仅普通QQ/QQBridge")
        self.btn_send_voice.clicked.connect(self.show_voice_send)
        self.notice = QLabel("正在连接后端服务…")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        for b in [self.btn_start, self.btn_stop, self.btn_test_ai, self.btn_test_qq,
                  self.btn_voice, self.btn_send_voice, self.btn_capabilities]:
            btn_layout.addWidget(b)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 普通端（OneBot）才能做的事：撤回 / 群资料 / 成员列表
        onebot_row = QHBoxLayout()
        onebot_title = QLabel("普通端能力")
        onebot_title.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        onebot_row.addWidget(onebot_title)
        self.btn_recall = QPushButton("撤回消息")
        self.btn_recall.setProperty("class", "secondary")
        self.btn_recall.clicked.connect(self.recall_message)
        self.btn_group_info = QPushButton("群资料")
        self.btn_group_info.setProperty("class", "secondary")
        self.btn_group_info.clicked.connect(self.show_group_info)
        self.btn_send_text = QPushButton("发消息")
        self.btn_send_text.setProperty("class", "secondary")
        self.btn_send_text.clicked.connect(self.send_text_message)
        self.btn_send_image = QPushButton("发送图片")
        self.btn_send_image.setProperty("class", "secondary")
        self.btn_send_image.clicked.connect(lambda: self.send_media('image'))
        self.btn_send_file = QPushButton("发送文件")
        self.btn_send_file.setProperty("class", "secondary")
        self.btn_send_file.clicked.connect(lambda: self.send_media('file'))
        self.btn_group_members = QPushButton("成员列表")
        self.btn_group_members.setProperty("class", "secondary")
        self.btn_group_members.clicked.connect(self.show_group_members)
        for button in (self.btn_recall, self.btn_group_info, self.btn_group_members,
                       self.btn_send_image, self.btn_send_file):
            onebot_row.addWidget(button)
        onebot_row.addStretch()
        layout.addLayout(onebot_row)
        # 提示独占一行，避免长错误/兼容模式说明把按钮和窗口撑出屏幕。
        layout.addWidget(self.notice)

        # 消息 + 事件
        splitter = QSplitter(Qt.Orientation.Horizontal)
        msg_widget = QWidget()
        msg_layout = QVBoxLayout(msg_widget)
        msg_layout.setContentsMargins(0, 0, 0, 0)
        msg_layout.addWidget(icon_row("消息与回复", "card_messages"))
        self.msg_view = QTextEdit()
        self.msg_view.setReadOnly(True)
        msg_layout.addWidget(self.msg_view)

        evt_widget = QWidget()
        evt_layout = QVBoxLayout(evt_widget)
        evt_layout.setContentsMargins(0, 0, 0, 0)
        evt_layout.addWidget(icon_row("运行记录", "card_events"))
        self.evt_view = QTextEdit()
        self.evt_view.setReadOnly(True)
        evt_layout.addWidget(self.evt_view)

        splitter.addWidget(msg_widget)
        splitter.addWidget(evt_widget)
        splitter.setSizes([600, 400])
        layout.addWidget(splitter, 1)
        self.apply_panel_alpha(1.0)

    def apply_panel_alpha(self, alpha):
        # 局部白底样式会盖过主窗口的透明度；消息和日志也要使用同一面板设置。
        channel = round(255 * max(0.0, min(1.0, float(alpha))))
        for view in (self.msg_view, self.evt_view):
            view.setStyleSheet(
                f"QTextEdit {{ background: rgba(255, 255, 255, {channel}); "
                f"border: 1px solid {BORDER}; border-radius: 10px; }}")

    @safe_slot(label="控制台")
    def refresh(self, data: dict):
        # 整页字段一律走 .get + 安全转换：后台返回少一个键、或某个值不是预期类型时，
        # 以前这一页会整块空白（异常被 safe_slot 兜住，但用户看不到任何数据）。
        # 现在"能显示的照常显示，坏的那一行显示占位符"。
        if not isinstance(data, dict):
            data = {}
        running = bool(data.get("running", False))
        self.card_running.update(("运行中 · 回复已暂停" if data.get("reply_paused") else "运行中") if running else "已停止", ok=running)

        mode = str(data.get("mode") or "official")
        from src.connection_status import connection_status
        connection=connection_status(data)
        self.card_conn.update(connection['title'],detail=connection['detail'],ok=connection['ok'])

        model_detail = str(data.get("ai_test") or "")
        # 按次计费的中转站每次请求固定扣费：把"本次运行调用了几次"显示出来，
        # 用户才能判断省没省（按 token 计费时调用次数意义不大，也显示）。
        try:
            calls = int(data.get("calls") or 0)
        except (TypeError, ValueError):
            calls = 0
        if calls > 0:
            suffix = (f"本次运行已调用 {calls} 次（按次计费 ≈ {calls} 积分）"
                      if str(data.get("billing_mode") or "") == "per_call"
                      else f"本次运行已调用 {calls} 次")
            model_detail = f"{model_detail} · {suffix}" if model_detail else suffix
        self.card_model.update(str(data.get("model") or "未配置"), model_detail)

        memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        try:
            saved = int(usage.get("saved_tokens") or 0)
            total = int(usage.get("total_tokens") or 0)
            manual = int(memory.get("manual") or 0)
            confirmed = int(memory.get("confirmed") or 0)
            candidates = int(memory.get("candidates") or 0)
        except (TypeError, ValueError):
            saved = total = manual = confirmed = candidates = 0
        latency = data.get("latency") if isinstance(data.get("latency"), dict) else {}
        try:
            avg_ms = float(latency.get("total_ms") or 0)
            latency_note = f" · 平均 {avg_ms / 1000:.2f}s" if avg_ms > 0 else ""
        except (TypeError, ValueError):
            latency_note = ""
        self.card_memory.update(
            f"手动 {manual} · 确认 {confirmed}",
            f"候选 {candidates} · 已省 {saved} tok · 累计 {total} tok{latency_note}")

        # 状态文案必须覆盖后端写过的所有状态：以前缺 error_replied 等，
        # 控制台直接显示英文原样（用户截图里就出现了 error_replied）。
        state_map = {"pending": "等待", "sending": "发送中", "sent": "已回复",
                     "failed": "失败", "partial": "文字已发·图片未完成", "uncertain": "待核实",
                     "expired": "过期", "error_replied": "已回复（超时兜底）",
                     "feedback_sending": "兜底提示发送中", "paused": "已暂停",
                     "blocked": "已拦下", "cancelled": "已取消", "merged": "已合并",
                     "done": "已完成", "executing": "执行中", "ready": "待确认",
                     "rejected": "已拒绝"}
        raw_messages = data.get("messages")
        msgs = [m for m in (raw_messages if isinstance(raw_messages, list) else [])[:15]
                if isinstance(m, dict)]
        html = ""
        for m in msgs:
            try:
                ts = time.strftime("%H:%M:%S", time.localtime(float(m.get("time") or 0)))
            except (TypeError, ValueError, OSError):
                ts = "--:--:--"
            state_key = m.get("state")
            state = state_map.get(state_key, str(state_key or "")) if isinstance(state_key, str) else str(state_key or "")
            # 标出会话类型与对方：以前只显示一个哈希 key，群聊和私聊分不清
            kind = str(m.get("kind") or "")
            where = ("群 " if kind == "group" else "私聊 " if kind == "private" else "")
            target = str(m.get("target") or "")
            sender = str(m.get("sender") or "")
            label = f"{ts} · {where}{target[:10] + '…' if len(target) > 10 else target} · {sender} · {state}"
            # 本轮花了多少 token：prompt 是大头（实测平均 1897），显示出来才知道省没省
            try:
                tokens = int(m.get("tokens") or 0)
            except (TypeError, ValueError):
                tokens = 0
            if tokens > 0:
                label += f" · {tokens} tok"
            html += f'<p style="color:{MUTED};font-size:11px;margin:2px 0">{label}</p>'
            html += f'<p style="margin:2px 0 6px">{str(m.get("text") or "")[:120]}</p>'
            reply = m.get("reply")
            if reply:
                html += f'<p style="color:{SUCCESS};margin:2px 0 10px">{str(reply)[:120]}</p>'
        self.msg_view.setHtml(html or f'<p style="color:{MUTED}">暂无消息</p>')

        raw_events = data.get("events")
        events = [ev for ev in (raw_events if isinstance(raw_events, list) else [])[-20:]
                  if isinstance(ev, dict)]
        evt_html = ""
        for ev in reversed(events):
            try:
                ts = time.strftime("%H:%M:%S", time.localtime(float(ev.get("time") or 0)))
            except (TypeError, ValueError, OSError):
                ts = "--:--:--"
            evt_html += (f'<p style="color:{MUTED};font-size:11px;margin:1px 0">{ts}</p>'
                         f'<p style="margin:1px 0 6px">{str(ev.get("text") or "")}</p>')
        self.evt_view.setHtml(evt_html or f'<p style="color:{MUTED}">暂无记录</p>')

    def run_onebot_action(self, action: str, params: dict) -> dict:
        """调用白名单 OneBot 动作，并把失败原因原样交给界面（不吞错误）。"""
        result = api_post('onebot/action', {'action': action, 'params': params}) or {}
        if result.get('success') is not True:
            message = str(result.get('message') or result.get('code') or '未知错误')
            tell(self, f'{action} 失败', message, kind='warning')
            return {}
        return result.get('data') or {}

    def send_text_message(self):
        """主动发一条文字消息（群或单聊），走 send_group_msg / send_private_msg。"""
        target, ok = ask_text(self, '发消息', '群号（或 QQ 号）：')
        if not ok or not target.strip():
            return
        text, ok = ask_text(self, '发消息', '内容：')
        if not ok or not text.strip():
            return
        target = target.strip()
        is_group = target.isdigit() and len(target) >= 5
        action = 'send_group_msg' if is_group else 'send_private_msg'
        key = 'group_id' if is_group else 'user_id'
        data = self.run_onebot_action(action, {key: int(target) if target.isdigit() else target,
                                               'message': text})
        if data:
            self.notice.setText('消息已发送。')

    def send_media(self, kind: str):
        """发送图片或文件：选文件 → 填目标 → 走 /api/media/send。"""
        path, _ = file_dialogs.open_file(
            self, '选择要发送的文件', str(pathlib.Path.home() / 'Pictures'),
            '图片 (*.png *.jpg *.jpeg *.gif *.webp *.bmp)' if kind == 'image' else '所有文件 (*)')
        if not path:
            return
        target, ok = ask_text(self, '发送' + ('图片' if kind == 'image' else '文件'),
                              '群号（或 QQ 号）：')
        if not ok or not target.strip():
            return
        target_type = 'group' if target.strip().isdigit() and len(target.strip()) >= 5 else 'private'
        result = api_post('media/send', {'target_type': target_type,
                                         'target_id': target.strip(), 'kind': kind,
                                         'path': path}) or {}
        if result.get('success') is True:
            self.notice.setText(f'已发送{"图片" if kind == "image" else "文件"}。')
        else:
            tell(self, '发送失败',
                 str(result.get('message') or result.get('code') or '未知错误'), kind='warning')

    def recall_message(self):
        """撤回一条消息：输入 message_id，调用 delete_msg。"""
        text, ok = ask_text(self, '撤回消息', '要撤回的消息 ID：')
        if not ok or not text.strip():
            return
        data = self.run_onebot_action('delete_msg', {'message_id': text.strip()})
        if data:
            self.notice.setText('已请求撤回该消息。')

    def show_group_info(self):
        group, ok = ask_text(self, '群资料', '群号：')
        if not ok or not group.strip():
            return
        data = self.run_onebot_action('get_group_info', {'group_id': int(group.strip()) if group.strip().isdigit() else group.strip()})
        if not data:
            return
        lines = [f'{key}: {value}' for key, value in list(data.items())[:20]]
        tell(self, '群资料', '\n'.join(lines) or '（空）', ok_text='知道了')

    def show_group_members(self):
        group, ok = ask_text(self, '成员列表', '群号：')
        if not ok or not group.strip():
            return
        data = self.run_onebot_action('get_group_member_list', {'group_id': int(group.strip()) if group.strip().isdigit() else group.strip()})
        members = data if isinstance(data, list) else []
        lines = []
        for item in members[:200]:
            if isinstance(item, dict):
                lines.append(f"{item.get('user_id')}  {item.get('card') or item.get('nickname') or ''}")
        tell(self, '成员列表',
             f'共 {len(members)} 人（显示前 {len(lines)} 条）\n\n' + '\n'.join(lines),
             ok_text='知道了')

    def show_voice_status(self):
        """本地语音组件状态；不发送 QQ 语音。"""
        from src import voice_client
        try:
            body = voice_client.health()
            voices = voice_client.voices()[:8]
            lines = [f"本地语音组件在线：{body.get('service', 'whale-voice')} {body.get('version', '')}",
                     '用途：本机朗读/试听，不发送 QQ 语音。',
                     '音色：' + ('、'.join(voices) if voices else '未返回系统音色')]
        except Exception as exc:
            lines = ['本地语音组件未启动或未安装。',
                     str(exc)[:180],
                     '',
                     '安装：插件市场\\语音组件\\WhaleVoice-Setup-1.0.0.exe',
                     '安装后从开始菜单启动，再点此按钮。']
        tell(self, '本地语音组件', '\n'.join(lines), ok_text='知道了')

    def show_voice_send(self):
        """发送本机语音条；只在普通 QQ/QQBridge 模式可用。"""
        from src.gui.file_dialogs import open_file
        dialog = QDialog(self)
        dialog.setWindowTitle('发送 QQ 语音')
        dialog.resize(480, 240)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        target = QComboBox(); target.addItem('群聊', 'group'); target.addItem('私聊', 'private')
        target_id = QLineEdit(); target_id.setPlaceholderText('群号或 QQ 号')
        file_row = QHBoxLayout(); file_edit = QLineEdit(); file_edit.setReadOnly(True)
        choose = QPushButton('选择语音')
        def choose_file():
            path, _ = open_file(dialog, '选择要发送的语音', '', '语音 (*.wav *.mp3 *.silk *.amr *.ogg *.m4a)')
            if path:
                file_edit.setText(path)
        choose.clicked.connect(choose_file)
        file_row.addWidget(file_edit, 1); file_row.addWidget(choose)
        form.addRow('会话类型', target); form.addRow('目标号码', target_id); form.addRow('语音文件', file_row)
        layout.addLayout(form)
        status = QLabel(''); status.setWordWrap(True); layout.addWidget(status)
        row = QHBoxLayout(); row.addStretch()
        send = QPushButton('发送'); cancel = QPushButton('取消')
        cancel.clicked.connect(dialog.reject)
        def do_send():
            if not target_id.text().strip() or not file_edit.text().strip():
                status.setText('请填写目标号码并选择语音文件。'); return
            status.setText('正在发送…'); send.setEnabled(False)
            result = api_post('voice/send', {'target_type': target.currentData(),
                                              'target_id': target_id.text().strip(),
                                              'path': file_edit.text().strip()}, timeout=60)
            if result.get('success'):
                status.setText('已发送语音。'); send.setEnabled(True)
                QTimer.singleShot(700, dialog.accept)
            else:
                status.setText(str(result.get('message') or result.get('code') or '发送失败'))
                send.setEnabled(True)
        send.clicked.connect(do_send)
        row.addWidget(send); row.addWidget(cancel); layout.addLayout(row)
        dialog.exec()

    def set_notice(self, text: str, ok: bool = True):
        self.notice.setStyleSheet(f"color: {SUCCESS if ok else ERROR}; font-size: 12px;")
        self.notice.setText(text)


# ── 设置标签页 ────────────────────────────────────────────────────────────────
class RowEditor(QWidget):
    """按行编辑：每行几个输入框 + 删除，底部「添加一行」。"""

    def __init__(self, fields, add_text="添加一行", parent=None):
        super().__init__(parent)
        self.fields = list(fields)
        self._rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(6)
        layout.addLayout(self._rows_box)
        self.add_button = QPushButton(add_text)
        self.add_button.setProperty("class", "secondary")
        self.add_button.clicked.connect(lambda: self.add_row())
        layout.addWidget(self.add_button)
        self.add_row()

    def add_row(self, values=None):
        values = list(values or [])
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        edits = []
        for index, (label, placeholder) in enumerate(self.fields):
            edit = QLineEdit(str(values[index]) if index < len(values) else "")
            edit.setPlaceholderText(placeholder)
            if "secret" in label.lower() or "密钥" in label or "Secret" in placeholder:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            row_layout.addWidget(edit, 2 if index == 0 else 1)
            edits.append(edit)
        remove = QPushButton("删除")
        remove.setProperty("class", "secondary")
        remove.setFixedWidth(60)
        row_layout.addWidget(remove)
        entry = {"row": row, "edits": edits}
        remove.clicked.connect(lambda: self.remove_row(entry))
        self._rows.append(entry)
        self._rows_box.addWidget(row)
        return entry

    def remove_row(self, entry, keep_min=True):
        if entry not in self._rows:
            return
        self._rows.remove(entry)
        entry["row"].setParent(None)
        entry["row"].deleteLater()
        if keep_min and not self._rows:
            self.add_row()

    def _clear(self):
        for entry in list(self._rows):
            self.remove_row(entry, keep_min=False)

    def set_text(self, text):
        self._clear()
        for line in str(text or "").splitlines():
            parts = [part.strip() for part in line.split("|")]
            if any(parts):
                self.add_row(parts)
        if not self._rows:
            self.add_row()

    def to_text(self):
        lines = []
        for entry in self._rows:
            parts = [edit.text().strip() for edit in entry["edits"]]
            if any(parts):
                lines.append("|".join(parts))
        return "\n".join(lines)

    # 兼容旧调用（原来这里是 QTextEdit）：只影响文本读写，不改变行编辑界面
    def setPlainText(self, text):           # noqa: N802
        self.set_text(text)

    def toPlainText(self):                  # noqa: N802
        return self.to_text()

    def setFixedHeight(self, *_args):       # noqa: N802
        return None


class SettingsTab(QWidget):
    def __init__(self):
        super().__init__()
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: 0; }")
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        def section(title):
            frame = QFrame()
            frame.setProperty("panel", True)
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(18, 14, 18, 18)
            fl.setSpacing(8)
            lbl = QLabel(title)
            lbl.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-weight: bold;")
            fl.addWidget(lbl)
            return frame, fl

        def field(parent_layout, label_text, widget):
            lbl = QLabel(label_text)
            lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px; margin-top: 6px;")
            parent_layout.addWidget(lbl)
            if isinstance(widget, QLayout):   # 一行里要放多个控件（输入框 + 按钮）时传布局
                parent_layout.addLayout(widget)
            else:
                parent_layout.addWidget(widget)

        # QQ 接入
        sec1, fl1 = section("01 · QQ 接入")
        self.mode = QComboBox()
        self.mode.addItem("QQ 官方机器人 · AppID / AppSecret", "official")
        # 合并入口：运行模式仍然是 qqbridge（机器人出站连统一接入的 OneBot 中继），
        # 但界面上不再要求用户填地址/令牌 —— 登录与地址都由启动器接管。
        self.mode.addItem("普通 QQ · 统一接入（由启动器自动登录）", "qqbridge")
        # OneBot 11：本程序当服务端，协议端（NapCat / SnowLuma / Lagrange…）反向 WS 连进来。
        # 以前下拉里没有它，populate 又把 onebot 显示成 qqbridge，于是保存一次就把
        # 已经配好的 onebot 模式改写成 qqbridge —— 用户看到的是"我明明选了 OneBot，保存就变"。
        self.mode.addItem("普通 QQ · OneBot 11 协议端（外部客户端连进来）", "onebot")
        self.mode.currentIndexChanged.connect(self._toggle_mode)
        field(fl1, "接入方式", self.mode)

        self.official_frame = QWidget()
        ofl = QVBoxLayout(self.official_frame)
        ofl.setContentsMargins(0, 0, 0, 0)
        ofl.setSpacing(8)
        self.app_id = QLineEdit()
        self.app_id.setPlaceholderText("AppID")
        self.app_secret = QLineEdit()
        self.app_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.app_secret.setPlaceholderText("AppSecret（留空保留已保存值）")
        self.sandbox = QCheckBox("使用沙箱环境")
        field(ofl, "AppID", self.app_id)
        field(ofl, "AppSecret", self.app_secret)
        ofl.addWidget(self.sandbox)
        fl1.addWidget(self.official_frame)

        self.onebot_frame = QWidget()
        obfl = QVBoxLayout(self.onebot_frame)
        obfl.setContentsMargins(0, 0, 0, 0)
        self.onebot_token = QLineEdit()
        self.onebot_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.onebot_token.setPlaceholderText("连接令牌（留空保留已保存值）")
        info = QLabel(f"连接地址：ws://127.0.0.1:{PORT}/qq/onebot  类型：Universal  格式：array")
        info.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        info.setWordWrap(True)
        field(obfl, "OneBot 令牌", self.onebot_token)
        obfl.addWidget(info)
        fl1.addWidget(self.onebot_frame)
        self.onebot_frame.setVisible(False)

        # QQ 账号直连（内置 SnowLuma 协议端）：本程序出站连 OneBot，登录扫码在 SnowLuma
        # WebUI 完成；填好连接地址即可，令牌与 exe 路径留空则用默认。
        # 统一接入：由启动器（WhaleQQGateway）负责引擎、扫码和 OneBot 中继，
        # 这里只放一个"打开控制台"按钮，避免两套地址/令牌把人绕晕。
        self.launcher_frame = QWidget()
        lfl = QVBoxLayout(self.launcher_frame)
        lfl.setContentsMargins(0, 0, 0, 0)
        lfl.setSpacing(6)
        self.launcher_status = QLabel("")
        self.launcher_status.setWordWrap(True)
        self.launcher_status.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.launcher_button = QPushButton("打开统一接入控制台（扫码登录）")
        self.launcher_button.clicked.connect(self.open_launcher)
        lfl.addWidget(self.launcher_button)
        lfl.addWidget(self.launcher_status)
        fl1.addWidget(self.launcher_frame)
        self.launcher_frame.setVisible(False)

        self.qqb_frame = QWidget()
        qbfl = QVBoxLayout(self.qqb_frame)
        qbfl.setContentsMargins(0, 0, 0, 0)
        qbfl.setSpacing(8)
        self.qqb_ws = QLineEdit()
        self.qqb_ws.setPlaceholderText("SnowLuma OneBot WebSocket 地址，如 ws://127.0.0.1:3001")
        self.qqb_http = QLineEdit()
        self.qqb_http.setPlaceholderText("SnowLuma OneBot HTTP 地址，如 http://127.0.0.1:3000（可选）")
        self.qqb_token = QLineEdit()
        self.qqb_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.qqb_token.setPlaceholderText("OneBot 访问令牌（无令牌留空）")
        self.qqb_exe = QLineEdit()
        self.qqb_exe.setPlaceholderText("SnowLuma 可执行文件路径（留空则不自动拉起，需手动启动）")
        qb_hint = QLabel("说明：QQ 号在 SnowLuma WebUI 里扫码登录；启动机器人后请留意扫码，"
                         "填 SnowLuma exe 路径可让本程序自动拉起协议端。")
        qb_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        qb_hint.setWordWrap(True)
        field(qbfl, "WebSocket 地址", self.qqb_ws)
        field(qbfl, "HTTP 地址", self.qqb_http)
        field(qbfl, "访问令牌", self.qqb_token)
        field(qbfl, "协议端路径", self.qqb_exe)
        qbfl.addWidget(qb_hint)

        # 反向 WebSocket：由本程序监听，协议端连进来（不依赖引擎端口）
        self.reverse_enabled = QCheckBox("启用反向 WebSocket（由本程序监听，协议端连进来）")
        self.reverse_enabled.setToolTip(
            "勾选后不用再填上面的 WebSocket 地址：把 SnowLuma/NapCat 配成"
            "「WebSocket 服务器 / 反向 WS」，连到下面这个地址即可")
        self.reverse_host = QLineEdit("127.0.0.1")
        self.reverse_host.setFixedWidth(130)
        self.reverse_port = QLineEdit("6700")
        self.reverse_port.setFixedWidth(90)
        self.reverse_token = QLineEdit()
        self.reverse_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.reverse_token.setPlaceholderText("反向连接令牌（必填，引擎那边也要填同一个）")
        reverse_port_row = QHBoxLayout()
        reverse_port_row.setSpacing(8)
        reverse_port_row.addWidget(self.reverse_host)
        reverse_port_row.addWidget(QLabel(":"))
        reverse_port_row.addWidget(self.reverse_port)
        reverse_port_row.addStretch(1)
        self.reverse_hint = QLabel("")
        self.reverse_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.reverse_hint.setWordWrap(True)
        qbfl.addWidget(self.reverse_enabled)
        field(qbfl, "监听地址", reverse_port_row)
        field(qbfl, "反向令牌", self.reverse_token)
        qbfl.addWidget(self.reverse_hint)
        self.reverse_enabled.toggled.connect(self._update_reverse_hint)
        self.reverse_host.textChanged.connect(self._update_reverse_hint)
        self.reverse_port.textChanged.connect(self._update_reverse_hint)
        fl1.addWidget(self.qqb_frame)
        self.qqb_frame.setVisible(False)

        # 官方机器人多开：每行「备注|AppID|Secret」
        self.official_bots = RowEditor(
            [("备注", "备注（如：客服）"), ("AppID", "开放平台 AppID"),
             ("Secret", "AppSecret（留空=沿用已保存）")],
            add_text="添加官方机器人")
        bots_hint = QLabel("官方模式可同时登录多个机器人：每个机器人的事件只回复给它自己的会话。")
        bots_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        bots_hint.setWordWrap(True)
        field(fl1, "官方机器人", self.official_bots)
        fl1.addWidget(bots_hint)

        # 管理器（QQ 统一接入控制台）连接：账号以它为准
        self.manager_url = QLineEdit()
        self.manager_url.setPlaceholderText("http://127.0.0.1:18090（留空=自动发现）")
        self.manager_token = QLineEdit()
        self.manager_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.manager_token.setPlaceholderText("管理令牌（留空=读管理器自动写下的连接文件）")
        manager_row = QHBoxLayout()
        manager_row.setSpacing(8)
        manager_row.addWidget(self.manager_url, 1)
        self.manager_open = QPushButton("打开管理器")
        self.manager_open.setProperty("class", "secondary")
        self.manager_open.clicked.connect(self.open_manager)
        manager_row.addWidget(self.manager_open)
        manager_hint = QLabel("账号由管理器负责：这里只填连接信息；管理器启动时会自动写连接文件。")
        manager_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        manager_hint.setWordWrap(True)
        field(fl1, "管理器地址", manager_row)
        field(fl1, "管理器令牌", self.manager_token)
        fl1.addWidget(manager_hint)

        self.groups = RowEditor([("群号", "群号 / 官方 openid")], add_text="添加群")
        field(fl1, "群聊白名单", self.groups)
        self.group_whitelist_strict = QCheckBox("名单为空时也拒绝群聊（严格模式）")
        fl1.addWidget(self.group_whitelist_strict)
        group_hint = QLabel("群名单非空时只回复名单内的群；留空默认不限制。"
                            "官方群的 ID 是 openid，普通 QQ 填群号。")
        group_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        group_hint.setWordWrap(True)
        fl1.addWidget(group_hint)

        self.private_users = RowEditor([("QQ / openid", "允许私聊的 QQ 号 / 官方 openid")],
                                       add_text="添加私聊")
        field(fl1, "私聊白名单", self.private_users)
        self.listen_private = QCheckBox("回复私聊消息（关闭后主人私聊也不回）")
        fl1.addWidget(self.listen_private)
        self.private_whitelist_strict = QCheckBox("名单为空时只允许主人（严格模式）")
        fl1.addWidget(self.private_whitelist_strict)
        private_hint = QLabel("名单非空时只回复名单内的人和主人；严格模式下名单为空只允许主人。"
                              "主人是“机器人管理”里绑定的那个 QQ；未绑定主人前不会锁死私聊。")
        private_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        private_hint.setWordWrap(True)
        fl1.addWidget(private_hint)
        layout.addWidget(sec1)

        # 模型 API
        sec2, fl2 = section("02 · 模型与 API")
        self.api_url = QLineEdit()
        self.api_url.setPlaceholderText("https://你的服务地址/v1")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("API Key（留空保留已保存值）")
        # 模型名必须是上游真正认识的 id（填显示名会 403/404），所以做成可下拉可手填：
        # "获取模型列表"把密钥可用的 id 填进来，用户直接选。
        # 对齐微信端：模型名称是输入框（手填 model id），旁边配一个只读下拉
        # （点「读取可用模型」后才出现，选一个自动填进输入框）。
        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText("模型 id，例如 TM ·Auto")
        self.model_pick = QComboBox()
        self.model_pick.setToolTip("选一个即可填进「模型名称」")
        self.model_pick.hide()
        self.model_pick.currentIndexChanged.connect(self._pick_model)
        self.model_name.setPlaceholderText("模型名称（可点「读取可用模型」下拉选择，也可直接手填）")
        self.api_style = QComboBox()
        for v, t in [("auto", "自动识别"), ("openai", "OpenAI 兼容"), ("anthropic", "Anthropic Messages")]:
            self.api_style.addItem(t, v)
        self.secrets_lbl = QLabel("")
        self.secrets_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        url_row = QHBoxLayout()
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.setSpacing(8)
        url_row.addWidget(self.api_url, 1)
        self.btn_models = decorate_button(QPushButton("获取模型列表"), "btn_save")
        self.btn_models.setToolTip("用当前地址和密钥向上游请求模型列表，再把模型 id 填进下拉框")
        url_row.addWidget(self.btn_models)
        field(fl2, "API 地址", url_row)
        field(fl2, "API Key", self.api_key)
        models_row = QHBoxLayout()
        models_row.setSpacing(8)
        models_row.addWidget(self.model_name, 1)
        models_row.addWidget(self.model_pick)
        field(fl2, "模型名称", models_row)
        field(fl2, "接口格式", self.api_style)
        # 回复超时与超时重试：中转站偶发变慢时，这两项决定"稍等一下就好"还是"直接失败"
        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(8)
        self.sp_reply_timeout = QSpinBox()
        self.sp_reply_timeout.setRange(5, 120)
        self.sp_reply_timeout.setSuffix(" 秒")
        self.sp_reply_timeout.setValue(20)
        self.sp_reply_timeout.setToolTip("单次等待模型返回的秒数；超时就按下面的选项处理")
        self.chk_timeout_retry = QCheckBox("超时后自动重试一次")
        self.chk_timeout_retry.setChecked(True)
        self.chk_timeout_retry.setToolTip("中转站偶发变慢时，第二次往往很快就回来；关掉会让一次抖动直接变成失败")
        timeout_row.addWidget(self.sp_reply_timeout)
        timeout_row.addWidget(self.chk_timeout_retry)
        timeout_row.addStretch(1)
        field(fl2, "回复超时", timeout_row)
        # 计费方式：中转站按次（每次固定扣费）时，省"次数"才有意义；
        # 官方/云服务按 token 时，靠「Token 节流」压缩上下文。
        self.billing_mode = QComboBox()
        for label, value in (("按 token 计费（官方 API / 多数云服务）", "per_token"),
                            ("按次计费（中转站每次固定扣费，例如每次 1 积分）", "per_call")):
            self.billing_mode.addItem(label, value)
        self.billing_mode.setToolTip(
            "按次计费时会自动少调用：跳过可选的第二次调用（防复读重生成）、"
            "放宽多条消息合并窗口，并在控制台显示本次运行调用了几次。")
        field(fl2, "计费方式", self.billing_mode)
        fl2.addWidget(self.secrets_lbl)
        self.probe_lbl = QLabel("")
        self.probe_lbl.setWordWrap(True)
        self.probe_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl2.addWidget(self.probe_lbl)
        layout.addWidget(sec2)

        # Agent 工具环三键（ai.agent_tools / _max_rounds / _timeout）：
        # 后端只认 config.yaml 的 ai 段，故由这个自包含区块直接落盘。
        self.agent_tools = AgentToolsGroup()
        layout.addWidget(self.agent_tools)

        # 做梦与自画像五键（ai.dream_* / ai.portrait_enabled）：同一页、同一份 ai 段，
        # 与上面的工具环区块各自只写自己的键，互不覆盖。
        self.agent_growth = AgentGrowthGroup()
        layout.addWidget(self.agent_growth)

        # 按钮
        btn_row = QHBoxLayout()
        self.btn_save = decorate_button(QPushButton("保存全部配置"), "btn_save")
        self.btn_diagnose = decorate_button(QPushButton("测试连接"), "btn_save")
        self.btn_diagnose.setToolTip("先取模型列表，再用当前模型真发一次最小请求")
        self.btn_speed = decorate_button(QPushButton("模型测速"), "btn_save")
        self.btn_speed.setToolTip("实测每个模型多久回话，用来挑“3 秒内能回”的那个（只读，不写配置）")
        self.btn_fast = decorate_button(QPushButton("快速回复档位"), "btn_apply")
        self.btn_fast.setToolTip("把“等待类”设置一次性调到快档（先列出改动，确认后才生效）")
        self.save_lbl = QLabel("")
        self.save_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_diagnose)
        btn_row.addWidget(self.btn_speed)
        btn_row.addWidget(self.btn_fast)
        btn_row.addWidget(self.save_lbl)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        layout.addStretch()

        scroll.setWidget(inner)
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.addWidget(scroll)

    def _pick_model(self, *_):
        """模型下拉里选了一个 → 填进「模型名称」输入框（微信端同款交互）。

        下拉只负责"挑"，输入框才是保存的值：手填的 model id 不会被下拉列表覆盖。
        """
        try:
            picked = str(self.model_pick.currentText() or "").strip()
        except Exception:
            return
        if picked:
            try:
                self.model_name.setText(picked)
            except Exception:
                pass

    def _launcher_path(self):
        """已安装的统一接入控制台（Inno 装到当前用户目录）。"""
        candidates = [
            Path(os.getenv("LOCALAPPDATA") or "") / "Programs" / "WhaleQQGateway" / "WhaleQQGateway.exe",
            Path(os.getenv("LOCALAPPDATA") or "") / "WhaleQQGateway" / "WhaleQQGateway.exe",
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def open_launcher(self):
        path = self._launcher_path()
        if path is None:
            tell(self, "还没安装统一接入控制台",
                 "请先安装 WhaleQQGateway-Setup（统一接入控制台），装好后再点这里。\n\n"
                 "装好后它会自动完成：启动引擎 → 扫码登录 → 把 OneBot 中继接到本机器人。",
                 ok_text='知道了')
            return
        try:
            subprocess.Popen([str(path)], close_fds=True)
        except Exception as error:
            tell(self, "启动失败", f"{type(error).__name__}: {error}", kind='warning')
            return
        self.launcher_status.setText(
            "已打开统一接入控制台：在里面扫码登录；登录成功后本页选『普通 QQ · 统一接入』即可，"
            "不用再填地址或令牌。")

    def _refresh_launcher_status(self):
        path = self._launcher_path()
        if path is None:
            self.launcher_status.setText("未检测到统一接入控制台（WhaleQQGateway）。")
            return
        mode = str(self.mode.currentData() or "")
        if mode == "qqbridge":
            # SettingsTab 没有 config 对象：用最近一次 populate 的数据（没有就空）。
            data = getattr(self, '_last_data', {}) or {}
            bridge = data.get('qqbridge') if isinstance(data.get('qqbridge'), dict) else {}
            ws = str(bridge.get("ws_url") or "")
            self.launcher_status.setText(
                f"已安装：{path}\n" + (f"当前中继地址：{ws}（由启动器写入）" if ws
                                        else "还没连上统一接入：点上面的按钮打开控制台并扫码登录。"))

    def _toggle_mode(self, idx: int):
        current = self.mode.currentData()
        self.official_frame.setVisible(current == "official")
        self.onebot_frame.setVisible(current == "onebot")
        self.qqb_frame.setVisible(False)          # 手动地址收起来，避免重复输入
        if hasattr(self, "launcher_frame"):
            self.launcher_frame.setVisible(False)   # 管理器连接改在下方「管理器」区
            if current == "qqbridge":
                self._refresh_launcher_status()

    @safe_slot(label="设置")
    def populate(self, data: dict):
        # 后台字段缺失/类型不对时不能整页空白：文本走 str()、列表只收字符串、
        # 数值用安全转换（以前 groups=None、reply_timeout="abc" 都会抛）。
        if not isinstance(data, dict):
            data = {}
        self._last_data = data
        saved_mode = str(data.get("mode") or "official")
        # 三种模式各自显示自己，不再把 onebot 冒充成 qqbridge（那样一保存就丢模式）。
        display_mode = saved_mode if saved_mode in ("official", "qqbridge", "onebot") else "official"
        idx = self.mode.findData(display_mode)
        self.mode.setCurrentIndex(idx)
        self._toggle_mode(idx)
        self.app_id.setText(str(data.get("app_id") or ""))
        self.sandbox.setChecked(bool(data.get("sandbox", False)))
        raw_groups = data.get("groups")
        groups = [str(item) for item in raw_groups if str(item).strip()] \
            if isinstance(raw_groups, list) else []
        self.groups.setPlainText("\n".join(groups))
        raw_private = data.get("private_users")
        private_users = [str(item) for item in raw_private if str(item).strip()] \
            if isinstance(raw_private, list) else []
        self.private_users.setPlainText("\n".join(private_users))
        self.listen_private.setChecked(bool(data.get("listen_private", True)))
        self.group_whitelist_strict.setChecked(bool(data.get("group_whitelist_strict", False)))
        self.private_whitelist_strict.setChecked(bool(data.get("private_whitelist_strict", False)))
        self.api_url.setText(str(data.get("api_url") or ""))
        try:
            index_billing = self.billing_mode.findData(
                str(data.get("billing_mode") or "per_token"))
            self.billing_mode.setCurrentIndex(max(0, index_billing))
        except Exception:
            pass
        self.model_name.setText(str(data.get("model") or ""))
        self.qqb_ws.setText(str(data.get("qqbridge_ws_url") or ""))
        self.qqb_http.setText(str(data.get("qqbridge_http_url") or ""))
        self.qqb_exe.setText(str(data.get("qqbridge_snowluma_exe") or ""))
        self.official_bots.set_text(str(data.get("official_bots_text") or ""))
        self.manager_url.setText(str(data.get("manager_base_url") or ""))
        self.reverse_enabled.setChecked(bool(data.get("reverse_enabled", False)))
        self.reverse_host.setText(str(data.get("reverse_host") or "127.0.0.1"))
        reverse_port = data.get("reverse_port") or 6700
        self.reverse_port.setText(str(reverse_port))
        self._update_reverse_hint()
        idx2 = self.api_style.findData(str(data.get("api_style") or "auto"))
        self.api_style.setCurrentIndex(idx2)
        self.sp_reply_timeout.setValue(int(_safe_float(data.get("reply_timeout"), 20, 5, 120)))
        self.chk_timeout_retry.setChecked(bool(data.get("reply_timeout_retry", True)))
        parts = []
        if data.get("api_key_set"): parts.append("API ✓")
        if data.get("app_secret_set"): parts.append("官方QQ ✓")
        if data.get("onebot_token_set"): parts.append("普通QQ ✓")
        if data.get("qqbridge_token_set"): parts.append("QQ直连 ✓")
        self.secrets_lbl.setText("已保存密钥：" + " · ".join(parts) if parts else "尚未设置密钥")
        self.agent_tools.populate(data)
        self.agent_growth.populate(data)

    def collect(self) -> dict:
        self.agent_tools.collect()       # 三键单独落 config.yaml 的 ai 段，见 src/gui/agent_settings.py
        self.agent_growth.collect()      # 做梦与自画像五键，同样只写自己的键
        return {
            "mode": self.mode.currentData(),
            "app_id": self.app_id.text().strip(),
            "app_secret": self.app_secret.text().strip(),
            "sandbox": self.sandbox.isChecked(),
            "onebot_token": self.onebot_token.text().strip(),
            "qqbridge_ws_url": self.qqb_ws.text().strip(),
            "qqbridge_http_url": self.qqb_http.text().strip(),
            "qqbridge_access_token": self.qqb_token.text().strip(),
            "qqbridge_snowluma_exe": self.qqb_exe.text().strip(),
            "reverse_enabled": self.reverse_enabled.isChecked(),
            "reverse_host": self.reverse_host.text().strip() or "127.0.0.1",
            "reverse_port": int(self.reverse_port.text().strip() or 6700),
            "reverse_token": self.reverse_token.text().strip(),
            "official_bots_text": self.official_bots.to_text().strip(),
            "groups": [g.strip() for g in self.groups.to_text().splitlines() if g.strip()],
            "private_users": [u.strip() for u in self.private_users.to_text().splitlines() if u.strip()],
            "listen_private": self.listen_private.isChecked(),
            "group_whitelist_strict": self.group_whitelist_strict.isChecked(),
            "private_whitelist_strict": self.private_whitelist_strict.isChecked(),
            "api_url": self.api_url.text().strip(),
            "api_key": self.api_key.text().strip(),
            "model": self.model_name.text().strip(),
            "api_style": self.api_style.currentData(),
            "reply_timeout": float(self.sp_reply_timeout.value()),
            "reply_timeout_retry": self.chk_timeout_retry.isChecked(),
            "billing_mode": str(self.billing_mode.currentData() or "per_token"),
        }

    def set_manager_status(self, text: str):
        """显示管理器连接状态（没有独立控件时退回按钮提示）。"""
        self.manager_url.setPlaceholderText(str(text))
        self.manager_url.setToolTip(str(text))

    def open_manager(self):
        """打开统一接入控制台（管理器）。"""
        from src.gateway_link import manager_exe
        exe = manager_exe()
        if not exe:
            tell(self, '没找到管理器', '请先安装 WhaleQQGateway（QQ 统一接入控制台）。',
                 kind='warning')
            return
        try:
            import subprocess
            subprocess.Popen([exe], close_fds=True)
            # 设置页里的按钮也要让主窗口开始等管理器，否则这里点完仍会一直“未连接”。
            window = self.window()
            starter = getattr(window, '_start_manager_retry', None)
            if callable(starter):
                starter()
                QTimer.singleShot(2500, window._retry_manager_connection)
        except Exception as error:
            tell(self, '打开管理器失败', f'{type(error).__name__}: {error}', kind='warning')

    def manager_values(self) -> dict:
        return {'base_url': self.manager_url.text().strip(),
                'token': self.manager_token.text().strip()}

    def _update_reverse_hint(self, *_):
        host = (self.reverse_host.text() or '127.0.0.1').strip()
        port = (self.reverse_port.text() or '6700').strip()
        url = f"ws://{host}:{port}/onebot/v11/ws"
        self.reverse_hint.setText(
            f"引擎里填：{url}?access_token=<反向令牌>" if self.reverse_enabled.isChecked()
            else "未启用：仍按上面的 WebSocket 地址出站连接")

    def set_probe(self, text: str, ok: bool = True):
        """模型列表 / 测试连接的结果就地显示（沿用保存提示的配色）。"""
        self.probe_lbl.setStyleSheet(f"color: {SUCCESS if ok else ERROR}; font-size: 12px;")
        self.probe_lbl.setText(text)

    def fill_models(self, models: list) -> int:
        """把上游模型 id 填进下拉框，保留用户已经填写的模型名。"""
        current = self.model_name.text().strip()
        self.model_name.blockSignals(True)
        self.model_pick.blockSignals(True)
        self.model_pick.clear()
        self.model_pick.addItems([str(m) for m in models])
        if current and current in [str(m) for m in models]:
            self.model_pick.setCurrentIndex([str(m) for m in models].index(current))
        self.model_pick.blockSignals(False)
        self.model_pick.show()
        try:
            self.model_pick.setToolTip(f"已读取 {len(models)} 个模型，选一个填进「模型名称」")
        except Exception:
            pass
        # 已经填过就沿用，不擅自改用户的模型；没填过就把第一个塞进输入框方便直接保存
        if not current and models:
            self.model_name.setText(str(models[0]))
        self.model_name.blockSignals(False)
        return len(models)


# ── 外观标签页 ────────────────────────────────────────────────────────────────
def scrollable_page_layout(page):
    """长表单在页面内滚动，不用最高的表单决定整个窗口的最小高度。"""
    shell = QVBoxLayout(page)
    shell.setContentsMargins(0, 0, 0, 0)
    page.scroll = QScrollArea(page)
    page.scroll.setWidgetResizable(True)
    page.scroll.setFrameShape(QFrame.Shape.NoFrame)
    page.scroll.setStyleSheet("QScrollArea { border: 0; background: transparent; }")
    body = QWidget()
    page.scroll.setWidget(body)
    body.setAutoFillBackground(False)
    page.scroll.viewport().setAutoFillBackground(False)
    shell.addWidget(page.scroll)
    return QVBoxLayout(body)


class AppearanceTab(QWidget):
    appearance_changed = pyqtSignal(dict)  # {transparency, blur, bg_color, background_image}

    def __init__(self):
        super().__init__()
        self._current_image = ""
        # 「模糊度已由用户显式设置」标记：载入配置时的 setValue 不算，老配置里已有的
        # blur 值也不算（否则老用户升级后还会看到白蒙层）。持久化到 blur_explicit。
        self._blur_explicit = False
        self._loading = False
        layout = scrollable_page_layout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        frame = QFrame()
        frame.setProperty("panel", True)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(18, 16, 18, 18)
        fl.setSpacing(14)

        fl.addWidget(icon_row("外观设置", "card_appearance"))

        # 透明度（0% = 完全不透明）
        op_lbl = QLabel(f"窗口透明度（调高可透出桌面；文字和背景一起变淡，最高 {MAX_TRANSPARENCY_PERCENT}%）")
        op_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(op_lbl)
        op_row = QHBoxLayout()
        self.transparency_slider = QSlider(Qt.Orientation.Horizontal)
        self.transparency_slider.setRange(0, MAX_TRANSPARENCY_PERCENT)
        self.transparency_slider.setValue(0)
        self.transparency_slider.setToolTip(
            f"最高 {MAX_TRANSPARENCY_PERCENT}%：再高窗口会淡到看不清、点不到（下限已锁定）")
        self.transparency_val = QLabel("0%")
        self.transparency_val.setStyleSheet(f"color: {ACCENT}; min-width: 40px;")
        self.transparency_slider.valueChanged.connect(lambda v: self.transparency_val.setText(f"{v}%"))
        op_row.addWidget(self.transparency_slider)
        op_row.addWidget(self.transparency_val)
        fl.addLayout(op_row)

        panel_lbl = QLabel("面板透明度（调高可看清背景图；文字和按钮保持清晰）")
        panel_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(panel_lbl)
        panel_row = QHBoxLayout()
        self.panel_transparency_slider = QSlider(Qt.Orientation.Horizontal)
        self.panel_transparency_slider.setRange(0, 100)
        self.panel_transparency_slider.setValue(35)
        self.panel_transparency_val = QLabel("35%")
        self.panel_transparency_val.setStyleSheet(f"color: {ACCENT}; min-width: 40px;")
        self.panel_transparency_slider.valueChanged.connect(lambda v: self.panel_transparency_val.setText(f"{v}%"))
        panel_row.addWidget(self.panel_transparency_slider)
        panel_row.addWidget(self.panel_transparency_val)
        fl.addLayout(panel_row)

        # 云母片模糊度
        blur_lbl = QLabel("云母片模糊度（0% = 关掉云母片，纯银白；越高越糊）")
        blur_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(blur_lbl)
        blur_row = QHBoxLayout()
        self.blur_slider = QSlider(Qt.Orientation.Horizontal)
        self.blur_slider.setRange(0, 100)
        self.blur_slider.setValue(70)
        self.blur_val = QLabel("70%")
        self.blur_val.setStyleSheet(f"color: {ACCENT}; min-width: 40px;")
        self.blur_slider.valueChanged.connect(self._on_blur_changed)
        blur_row.addWidget(self.blur_slider)
        blur_row.addWidget(self.blur_val)
        fl.addLayout(blur_row)

        # 背景色
        bg_lbl = QLabel("背景叠加色（叠加在云母片上，用来调整色调）")
        bg_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(bg_lbl)
        bg_row = QHBoxLayout()
        self.color_preview = QFrame()
        self.color_preview.setFixedSize(32, 32)
        self.color_preview.setStyleSheet(f"background: {BG}; border-radius: 6px; border: 1px solid {BORDER};")
        self._current_color = BG
        btn_pick = QPushButton("选择颜色…")
        btn_pick.setProperty("class", "secondary")
        btn_pick.clicked.connect(self._pick_color)
        bg_row.addWidget(self.color_preview)
        bg_row.addWidget(btn_pick)
        bg_row.addStretch()
        fl.addLayout(bg_row)

        # 自定义背景图
        img_lbl = QLabel("自定义背景图（铺满窗口，叠加色会盖在图片之上）")
        img_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(img_lbl)
        img_row = QHBoxLayout()
        self.image_preview = QLabel("无")
        self.image_preview.setFixedSize(96, 56)
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setStyleSheet(
            f"background: #0c1625; color: {MUTED}; border-radius: 6px; border: 1px solid {BORDER}; font-size: 11px;"
        )
        btn_image = QPushButton("选择其他图片…")
        btn_image.setProperty("class", "secondary")
        btn_image.setToolTip("使用系统文件框；所有文件导入统一使用系统文件框")
        btn_image.clicked.connect(self._pick_image)
        btn_clear = QPushButton("清除背景图")
        btn_clear.setProperty("class", "secondary")
        btn_clear.clicked.connect(self._clear_image)
        img_row.addWidget(self.image_preview)
        img_row.addWidget(btn_image)
        img_row.addWidget(btn_clear)
        img_row.addStretch()
        fl.addLayout(img_row)
        self.image_hint = QLabel("")
        self.image_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.image_hint.setWordWrap(True)
        fl.addWidget(self.image_hint)

        # 独立桌宠：资源包导入与进程启停
        pet_row = QHBoxLayout()
        pet_label = QLabel('桌宠（图片/GIF 资源包，独立进程；Live2D 模型包后续支持）')
        pet_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        btn_pet = QPushButton("管理桌宠…")
        btn_pet.setProperty("class", "secondary")
        btn_pet.clicked.connect(self._open_pets)
        pet_row.addWidget(pet_label, 1)
        pet_row.addWidget(btn_pet)
        fl.addLayout(pet_row)

        # 兼容模式（安全模式）：远程桌面 / 多个虚拟显示适配器的机器上，
        # 无边框 + 半透明 + 云母会让 Qt 在建窗口阶段原生崩溃（实测 0xC0000005，
        # 每次双击必崩、窗口根本出不来）。勾上它就用最朴素的窗口。
        self.safe_mode_box = QCheckBox("兼容模式（远程桌面、虚拟显示或多屏异常时勾选；更稳，但外观简化）")
        self.safe_mode_box.setToolTip(
            "勾选后：使用系统窗口边框、关闭半透明/云母/背景图、强制软件渲染。\n"
            "保存并重启控制台后生效。若程序曾经双击打不开，也可以直接双击\n"
            "安装目录里的「安全模式启动.cmd」。")
        fl.addWidget(self.safe_mode_box)
        self.safe_mode_hint = QLabel("")
        self.safe_mode_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.safe_mode_hint.setWordWrap(True)
        fl.addWidget(self.safe_mode_hint)

        # 应用按钮
        btn_apply = decorate_button(QPushButton("应用外观设置"), "btn_apply")
        btn_apply.clicked.connect(self._apply)
        fl.addWidget(btn_apply)

        note = QLabel("云母片效果仅在 Windows 11 上生效。图片会按窗口比例裁剪铺满，不会变形；面板透明度只影响卡片底色。建议窗口透明度为 0%，面板透明度为 35%–70%。")
        note.setStyleSheet(f"color: {MUTED}; font-size: 11px; margin-top: 8px;")
        note.setWordWrap(True)
        fl.addWidget(note)

        layout.addWidget(frame)
        layout.addStretch()

    def _open_pets(self):
        from src.gui.pet_dialog import PetDialog
        PetDialog(self.window(), self).exec()

    def _pick_color(self):
        # 系统取色框会跟随 Windows 深色主题变成黑底（用户截图），这里固定用浅色自绘版
        color = file_dialogs.choose_color(self, self._current_color, "选择背景叠加色")
        if color.isValid():
            self._current_color = color.name()
            self.color_preview.setStyleSheet(
                f"background: {self._current_color}; border-radius: 6px; border: 1px solid {BORDER};"
            )

    def _pick_image(self):
        # 所有文件导入统一使用系统文件框；起始目录用上次选中的那个。
        pictures = Path.home() / "Pictures"
        start = file_dialog_state.last_directory(
            str(pictures) if pictures.is_dir() else "")
        path, _ = file_dialogs.open_file(
            self, "选择背景图片", start, IMAGE_FILTER, force_light=False)
        if path:
            file_dialog_state.remember(path)
            self._apply_background_file(path)

    def _apply_background_file(self, path):
        try:
            relative = import_background(path, ROOT)
        except (ValueError, OSError) as exc:
            self.image_hint.setText(f"导入失败：{exc}")
            return
        previous = self._current_image
        self._current_image = relative
        if previous and previous != relative:
            try:
                clear_background(previous, ROOT)
            except (ValueError, OSError):
                pass
        self._show_image_hint()
        self._apply()
        self.image_hint.setText(f"已选择背景：{Path(path).name}；已保存。")

    def _clear_image(self):
        previous = self._current_image
        self._current_image = ""
        if previous:
            try:
                clear_background(previous, ROOT)
            except (ValueError, OSError) as exc:
                self.image_hint.setText(f"删除旧图失败（已忽略）：{exc}")
        self._show_image_hint()
        self._apply()

    def _show_image_hint(self):
        resolved = resolve_background(self._current_image, ROOT)
        if resolved is None:
            self._current_image = "" if self._current_image else self._current_image
            self._image_ready = False
            self.image_preview.setText("无")
            self.image_preview.setPixmap(QPixmap())
            self.image_preview.setStyleSheet(
                f"background: #0c1625; color: {MUTED}; border-radius: 6px; border: 1px solid {BORDER}; font-size: 11px;"
            )
            self.image_hint.setText("" if not self._current_image else "背景图文件不存在，已忽略")
            return
        self._image_ready = True
        preview = QPixmap(str(resolved))
        if preview.isNull():
            self._image_ready = False
            self.image_preview.setText("无法读取")
            self.image_preview.setPixmap(QPixmap())
            self.image_hint.setText("图片无法读取，已退回纯色背景")
            return
        self.image_preview.setText("")
        self.image_preview.setPixmap(
            preview.scaled(
                self.image_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.image_hint.setText(f"当前：{self._current_image}")

    def _on_blur_changed(self, value: int):
        self.blur_val.setText(f"{value}%")
        if not self._loading:
            self._blur_explicit = True

    def _apply(self):
        self.appearance_changed.emit({
            "transparency": self.transparency_slider.value() / 100.0,
            "panel_transparency": self.panel_transparency_slider.value() / 100.0,
            "blur": self.blur_slider.value() / 100.0,
            "blur_explicit": self._blur_explicit,
            "bg_color": self._current_color,
            "background_image": self._current_image,
            "safe_mode": self.safe_mode_box.isChecked(),
        })

    @safe_slot(label="外观")
    def load(self, cfg: dict):
        transparency = int(round(float(cfg.get("transparency", 0.0)) * 100))
        # 老配置里可能存着 100（曾经拉满过）：夹回上限，否则窗口一开就是半透明/看不见
        transparency = max(0, min(int(transparency), MAX_TRANSPARENCY_PERCENT))
        self.transparency_slider.setValue(transparency)
        self.transparency_val.setText(f"{transparency}%")
        panel = int(round(float(cfg.get("panel_transparency", 0.35)) * 100))
        self.panel_transparency_slider.setValue(panel)
        self.panel_transparency_val.setText(f"{panel}%")
        blur = int(round(float(cfg.get("blur", 0.70)) * 100))
        self._loading = True
        try:
            self.blur_slider.setValue(blur)  # 这里会触发 valueChanged，不能算用户动过
        finally:
            self._loading = False
        self.blur_val.setText(f"{blur}%")
        # 老配置没有 blur_explicit：视为没动过 → 有背景图时默认不叠蒙层。
        self._blur_explicit = bool(cfg.get("blur_explicit", False))
        color = cfg.get("bg_color", BG)
        self._current_color = color
        self.color_preview.setStyleSheet(
            f"background: {color}; border-radius: 6px; border: 1px solid {BORDER};"
        )
        self._current_image = cfg.get("background_image", "") or ""
        self._show_image_hint()
        # 兼容模式：勾选状态跟着配置走；正在兼容模式下运行时给一句说明
        self.safe_mode_box.setChecked(bool(cfg.get("safe_mode", False)) or SAFE_MODE)
        if SAFE_MODE and not cfg.get("safe_mode"):
            self.safe_mode_hint.setText(
                "本次是因上次启动崩溃而自动进入兼容模式的。想永久开启，请勾选后点「应用外观设置」并重启。")
        elif cfg.get("safe_mode"):
            self.safe_mode_hint.setText("兼容模式已开启（重启控制台后继续生效）。")
        else:
            self.safe_mode_hint.setText("")


# ── 界面小部件辅助 ────────────────────────────────────────────────────────────
ICON_PX = 18          # 卡片/标题图标字号
ICON_PX_SMALL = 14    # 按钮图标（按钮高度有限）


def icon_row(text: str, slot: str, size: int = ICON_PX, color: str = ACCENT) -> QWidget:
    """"图标 + 文字"一行，用来替换标题里的 emoji。"""
    holder = QWidget()
    holder.setStyleSheet("background: transparent;")
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(7)
    mark = QLabel()
    mark.setPixmap(slot_icon(slot).pixmap(size, size))
    mark.setFixedSize(size + 2, size + 2)
    mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label = QLabel(text)
    label.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: bold;")
    row.addWidget(mark)
    row.addWidget(label)
    row.addStretch(1)
    return holder


def decorate_button(button, slot: str, size: int = ICON_PX_SMALL):
    """给按钮加图标（图标取不到时按钮文字保持不变，不会空掉）。"""
    mark = slot_icon(slot)
    if not mark.isNull():
        button.setIcon(mark)
        button.setIconSize(QSize(size, size))
    return button


# ── 人设页 ────────────────────────────────────────────────────────────────────
def _card(title: str, slot: str = "") -> tuple:
    """统一卡片外观，返回 (frame, 内部竖排 layout)。

    ``slot`` 给了就画成"图标 + 标题"，替代以前的 emoji 前缀。
    """
    frame = QFrame()
    frame.setProperty("panel", True)
    box = QVBoxLayout(frame)
    box.setContentsMargins(18, 16, 18, 18)
    box.setSpacing(10)
    if slot:
        box.addWidget(icon_row(title, slot))
    else:
        label = QLabel(title)
        label.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-weight: bold;")
        box.addWidget(label)
    return frame, box


class PersonaTab(QWidget):
    """自定义人设：左边选、右边改，可新建/保存/切换/删除。

    默认人设是鲸鱼娘（tsundere_whale_girl）；内置人设不允许覆盖，
    改完请另存为新标识（界面会提示）。
    """

    def __init__(self):
        super().__init__()
        self._current = ""
        self._original = ""
        self._pending_image = ""      # 当前人设的形象图文件名（非必填）

        root = scrollable_page_layout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：人设列表
        left, lbox = _card("人设列表", "card_persona_list")
        hint = QLabel("默认是鲸鱼娘。选中后可在右侧修改；内置人设要改请用「另存为」。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        lbox.addWidget(hint)
        self.list = QListWidget()
        self.list.setStyleSheet(
            f"QListWidget {{ background: {FIELD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}"
            f"QListWidget::item {{ padding: 6px; }}"
            f"QListWidget::item:selected {{ background: {ACCENT}; color: {ACCENT_FG}; border-radius: 6px; }}"
        )
        self.list.currentRowChanged.connect(self._on_select)
        lbox.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_new = decorate_button(QPushButton("新建"), "btn_new")
        self.btn_reload = decorate_button(QPushButton("刷新"), "btn_reload")
        self.btn_new.clicked.connect(self._new)
        self.btn_reload.clicked.connect(self.refresh)
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_reload)
        lbox.addLayout(row)
        splitter.addWidget(left)

        # 右：编辑区
        right, rbox = _card("人设内容", "card_persona_edit")
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("英文标识，例如 my_persona（只能用字母数字下划线短横线）")
        self.ed_display = QLineEdit()
        self.ed_display.setPlaceholderText("显示名称，例如 傲娇鲸鱼娘")
        self.ed_desc = QLineEdit()
        self.ed_desc.setPlaceholderText("一句话说明适用场景")
        self.ed_keywords = QLineEdit()
        self.ed_keywords.setPlaceholderText("这个人设的关键词，逗号分隔，例如：鲸鱼娘, 小鲸鱼（说这些话时用这个人设回这一条）")

        self.sp_temp = QDoubleSpinBox()
        self.sp_temp.setRange(0.0, 2.0)
        self.sp_temp.setSingleStep(0.1)
        self.sp_temp.setDecimals(2)
        self.sp_tokens = QSpinBox()
        self.sp_tokens.setRange(16, 8000)
        self.sp_tokens.setSingleStep(16)

        self.ed_prompt = QTextEdit()
        self.ed_prompt.setPlaceholderText("人设本体：身份、性格、说话习惯、口头禅、禁止项…")
        self.ed_prompt.setMinimumHeight(140)
        self.ed_examples = QTextEdit()
        self.ed_examples.setPlaceholderText("对话示例，两行一组：\n用户：你好\n你：哼，找我干嘛啦")
        self.ed_examples.setMinimumHeight(110)
        self.ed_banned = QTextEdit()
        self.ed_banned.setPlaceholderText(
            "违禁词：每行一个，或用逗号分隔。\n"
            "人设正文 / 显示名 / 说明 / 示例里出现这些词，保存会被拒绝（配置不动）并暂停自动回复。")
        self.ed_banned.setMinimumHeight(64)

        for widget in (self.ed_name, self.ed_display, self.ed_desc, self.ed_keywords, self.ed_prompt,
                       self.ed_examples, self.ed_banned):
            widget.setStyleSheet(
                f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER};"
                "border-radius: 6px; padding: 6px;"
            )
        for spin in (self.sp_temp, self.sp_tokens):
            spin.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px;")

        form.addRow("标识", self.ed_name)
        form.addRow("显示名", self.ed_display)
        form.addRow("说明", self.ed_desc)
        form.addRow("关键词", self.ed_keywords)
        form.addRow("温度", self.sp_temp)
        form.addRow("最大长度", self.sp_tokens)
        form.addRow("人设本体", self.ed_prompt)
        form.addRow("对话示例", self.ed_examples)
        form.addRow("违禁词", self.ed_banned)
        rbox.addLayout(form)

        btns = QHBoxLayout()
        self.btn_save = decorate_button(QPushButton("保存人设"), "btn_save")
        self.btn_saveas = decorate_button(QPushButton("另存为"), "btn_save")
        self.btn_activate = decorate_button(QPushButton("设为当前"), "btn_apply")
        self.btn_delete = decorate_button(QPushButton("删除"), "btn_delete")
        self.btn_save.clicked.connect(lambda: self._save(rename=False))
        self.btn_saveas.clicked.connect(lambda: self._save(rename=True))
        self.btn_activate.clicked.connect(self._activate)
        self.btn_delete.clicked.connect(self._delete)
        for b in (self.btn_save, self.btn_saveas, self.btn_activate, self.btn_delete):
            btns.addWidget(b)
        # 操作栏放在滚动区外，小窗口和长表单也始终能找到保存入口。
        self.layout().insertLayout(0, btns)

        banned_row = QHBoxLayout()
        self.btn_banned = decorate_button(QPushButton("保存违禁词"), "btn_save")
        self.btn_banned.clicked.connect(self._save_banned)
        banned_row.addWidget(self.btn_banned)
        self.banned_hint = QLabel("违禁词是全局的：命中即拒绝保存该人设、拒绝切换，并暂停自动回复")
        self.banned_hint.setWordWrap(True)
        self.banned_hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        banned_row.addWidget(self.banned_hint, 1)
        rbox.addLayout(banned_row)

        image_box = QHBoxLayout()
        self.image_preview = QLabel("未设置")
        self.image_preview.setFixedSize(96, 96)
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setStyleSheet(
            f"background: {FIELD}; color: {MUTED}; border: 1px dashed {BORDER}; border-radius: 8px;")
        image_box.addWidget(self.image_preview)
        image_btns = QVBoxLayout()
        self.btn_image_upload = decorate_button(QPushButton("更换并保存图片"), "btn_save")
        self.btn_image_upload.clicked.connect(self._upload_persona_image)
        self.btn_image_note = decorate_button(QPushButton("重新识别"), "btn_save")
        self.btn_image_note.clicked.connect(self._describe_persona_image)
        self.btn_image_clear = decorate_button(QPushButton("清除图片"), "btn_delete")
        self.btn_image_clear.clicked.connect(self._clear_persona_image)
        self.btn_image_save = decorate_button(QPushButton('保存图片描述'), 'btn_save')
        self.btn_image_save.clicked.connect(self._save_image_note)
        for button in (self.btn_image_upload, self.btn_image_save, self.btn_image_note, self.btn_image_clear):
            image_btns.addWidget(button)
        image_box.addLayout(image_btns)
        image_text = QVBoxLayout()
        self.ed_image_note = QLineEdit()
        self.ed_image_note.setPlaceholderText(
            "自我描述（非必填）：例如「银白色长发、蓝眼睛、白色连帽衫」。上传图片后会自动识别生成，也可以自己改。")
        self.ed_image_note.setStyleSheet(
            f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER};"
            "border-radius: 6px; padding: 6px;")
        image_text.addWidget(self.ed_image_note)
        self.image_hint = QLabel("选图后立即保存，不等模型。可手写自我描述再点「保存图片描述」，"
                                 "或保存后点「重新识别」。顶部「保存人设」会保存整份表单。")
        self.image_hint.setWordWrap(True)
        self.image_hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        image_text.addWidget(self.image_hint)
        image_box.addLayout(image_text, 1)
        image_container = QWidget()
        image_container.setLayout(image_box)
        rbox.addWidget(image_container)

        file_row = QHBoxLayout()
        self.btn_export = decorate_button(QPushButton("保存人设到文件"), "btn_save")
        self.btn_export.clicked.connect(self._export_persona)
        self.btn_import = decorate_button(QPushButton("导入人设文件"), "btn_save")
        self.btn_import.clicked.connect(self._import_persona)
        file_row.addWidget(self.btn_export)
        file_row.addWidget(self.btn_import)
        self.file_hint = QLabel("导出会把当前表单（含未保存的修改）存成 .yaml；导入直接选文件，"
                                "重名会自动改成 xxx-2，不会覆盖任何人设")
        self.file_hint.setWordWrap(True)
        self.file_hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        file_row.addWidget(self.file_hint, 1)
        rbox.addLayout(file_row)

        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        rbox.addWidget(self.notice)
        splitter.addWidget(right)
        splitter.setSizes([300, 720])
        root.addWidget(splitter, 1)

    # ── 数据 ──────────────────────────────────────────────────────────────
    @safe_slot(label="人设")
    def refresh(self):
        data = api_get("personas")
        personas = data.get("personas") or []
        active = data.get("active") or ""
        banned = api_get("personas/banned-words")
        if isinstance(banned, dict) and "words" in banned and not self.ed_banned.hasFocus():
            # 正在输入时不要覆盖：刷新可能由切页触发。
            self.ed_banned.setPlainText("\n".join(banned.get("words") or []))
            note = str(banned.get("note") or "")
            if note and not self.notice.text():
                self.notice.setText(note)
        self.list.blockSignals(True)
        self.list.clear()
        display_counts = {}
        for item in personas:
            label = str(item.get('display_name') or item.get('name') or '')
            display_counts[label] = display_counts.get(label, 0) + 1
        for item in personas:
            name = item.get("name", "")
            label = f"{item.get('display_name') or name}"
            if display_counts.get(label, 0) > 1:
                label += f' · {name}'
            if name == active:
                label += "   （当前）"
            row = QListWidgetItem(label)
            row.setData(Qt.ItemDataRole.UserRole, name)
            self.list.addItem(row)
        self.list.blockSignals(False)
        if self.list.count() and not self._current:
            self.list.setCurrentRow(0)
        elif self.list.count():
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == self._current:
                    self.list.setCurrentRow(i)
                    break

    @safe_slot(label="人设详情")
    def _load_detail(self, name: str):
        detail = api_get(f"personas/{name}")
        if not detail or "system_prompt" not in detail:
            self.notice.setText(f"读不到人设内容：{name}")
            return
        self._current = name
        self._original = name
        self.ed_name.setText(detail.get("name", ""))
        self.ed_display.setText(detail.get("display_name", ""))
        self.ed_desc.setText(detail.get("description", ""))
        self.ed_keywords.setText(", ".join(str(w) for w in (detail.get("keywords") or [])))
        # 数值字段用安全转换：人设 YAML 是用户手写的（`temperature: 高` 很常见），
        # 以前 float()/int() 直接抛 → 切到人设页就弹"操作未完成"，
        # 而且在 self-test 路径（没装 excepthook）会 abort 整个进程。
        self.sp_temp.setValue(_safe_float(detail.get("temperature"), 0.7, 0.0, 2.0))
        self.sp_tokens.setValue(int(_safe_float(detail.get("max_tokens"), 500, 1, 200000)))
        self.ed_prompt.setPlainText(detail.get("system_prompt", ""))
        examples = detail.get("examples") or []
        lines = []
        for item in examples:
            if isinstance(item, dict):
                lines.append(f"用户：{item.get('user', '')}")
                lines.append(f"你：{item.get('assistant', '')}")
        self.ed_examples.setPlainText("\n".join(lines))
        self._pending_image = str(detail.get("image") or "")
        self.ed_image_note.setText(str(detail.get("image_note") or ""))
        self._refresh_image_preview()
        self.notice.setText("")

    def _on_select(self, row: int):
        if row < 0:
            return
        name = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        if name:
            self._load_detail(name)

    def _new(self):
        self._current = ""
        self._original = ""
        self.ed_name.clear()
        self.ed_display.clear()
        self.ed_desc.clear()
        self.ed_keywords.clear()
        self.sp_temp.setValue(0.7)
        self.sp_tokens.setValue(500)
        self.ed_prompt.clear()
        self.ed_examples.clear()
        self._pending_image = ""
        self.ed_image_note.clear()
        self._refresh_image_preview()
        self.notice.setText("正在新建人设：填好后点「保存」。标识只能用字母、数字、下划线、短横线。")

    def _payload(self, rename: bool) -> dict:
        return {
            "name": self.ed_name.text().strip(),
            "original_name": self._original if not rename else "",
            "display_name": self.ed_display.text().strip(),
            "description": self.ed_desc.text().strip(),
            "keywords": self.ed_keywords.text().strip(),
            "system_prompt": self.ed_prompt.toPlainText().strip(),
            "temperature": self.sp_temp.value(),
            "max_tokens": self.sp_tokens.value(),
            "examples": self.ed_examples.toPlainText(),
            "image": self._pending_image,
            "image_note": self.ed_image_note.text().strip(),
        }

    def _save(self, rename: bool):
        payload = self._payload(rename)
        if not payload["name"]:
            self.notice.setText("请先填「标识」")
            return
        result = api_post("personas", payload)
        ok = bool(result.get("success"))
        message = str(result.get("message") or ("已保存" if ok else "保存失败"))
        self.notice.setText(message)
        if ok:
            self._current = result.get("name") or payload["name"]
            self._original = self._current
            self.refresh()
        elif "违禁词" in message:
            # 命中违禁词后端已经拒绝落盘并暂停了回复，这里把原因说清楚。
            tell(self, "人设被拒绝保存", message, kind="warning", ok_text="知道了")

    def _save_banned(self):
        """保存违禁词表（全局，存在 config.qq.yaml 的 style.banned_words）。"""
        result = api_post("personas/banned-words", {"words": self.ed_banned.toPlainText()})
        words = result.get("words")
        if isinstance(words, list):
            self.ed_banned.setPlainText("\n".join(words))
        message = str(result.get("message") or ("已保存" if result.get("success") else "保存失败"))
        self.notice.setText(message)
        if result.get("active_hit"):
            tell(self, "当前人设命中违禁词", message, kind="warning", ok_text="知道了")

    def _activate(self):
        if not self._current:
            self.notice.setText("先选一个人设")
            return
        result = api_post(f"personas/{self._current}/activate")
        self.notice.setText(result.get("message") or ("已切换" if result.get("success") else "切换失败"))
        self.refresh()

    # ── 人设图片（非必填）────────────────────────────────────────────────────
    def _refresh_image_preview(self):
        """从后台取当前人设的形象图显示成缩略图；没有就显示"未设置"。"""
        pixmap = QPixmap()
        if self._current:
            try:
                response = local_request('get', f"{BASE_URL}/api/personas/{self._current}/image", timeout=5)
                if response.status_code == 200:
                    pixmap.loadFromData(response.content)
            except Exception:
                pixmap = QPixmap()
        if pixmap.isNull():
            self.image_preview.setPixmap(QPixmap())
            self.image_preview.setText("未设置")
        else:
            self.image_preview.setText("")
            self.image_preview.setPixmap(
                pixmap.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation))

    def _upload_persona_image(self):
        if not self._current:
            self.notice.setText("先保存人设，再上传图片")
            return
        path, _ = file_dialogs.open_file(
            self, "选择人设图片", "", "图片 (*.png *.jpg *.jpeg *.gif *.webp);;所有文件 (*)")
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            message = f"读不到这个文件：{exc}"
            self.notice.setText(message)
            tell(self, "上传人设图片失败", message, kind="warning")
            return
        if len(raw) > 2 * 1024 * 1024:
            message = "图片太大（上限 2 MB），先压缩一下再传"
            self.notice.setText(message)
            tell(self, "上传人设图片失败", message, kind="warning")
            return
        payload = {"name": self._current, "filename": Path(path).name,
                   "content_base64": base64.b64encode(raw).decode(), 'describe': False}
        target = self._current
        run_api_async(self, "personas/image", payload, timeout=15,
                      callback=lambda result: self._upload_image_done(result, target), busy="正在保存图片…")

    def _upload_image_done(self, result: dict, target=None):
        if target is not None and target != self._current:
            self.notice.setText(f'「{target}」图片操作已完成，请选回该人设查看')
            return
        if not result.get("success"):
            message = str(result.get("message") or result.get("detail") or "上传失败")
            self.notice.setText(message)
            tell(self, "上传人设图片失败", message, kind="warning")
            return
        self._pending_image = str(result.get("image") or "")
        self.ed_image_note.setText(str(result.get("image_note") or ""))
        self.notice.setText(str(result.get("message") or "已保存人设图片"))
        self._refresh_image_preview()

    def _save_image_note(self):
        if not self._current:
            self.notice.setText('请先选择或保存人设')
            return
        target = self._current
        def done(result):
            if target == self._current:
                self.notice.setText(str(result.get('message') or result.get('detail') or '保存失败'))
        run_api_async(self, 'personas/image/note',
                      {'name': target, 'image_note': self.ed_image_note.text().strip()},
                      timeout=15, callback=done, busy='正在保存图片描述…')

    def _describe_persona_image(self):
        if not self._current or not self._pending_image:
            self.notice.setText("这个人设还没有图片")
            return
        run_api_async(self, "personas/image/describe", {"name": self._current}, timeout=90,
                      callback=self._describe_image_done, busy="正在让模型重新看图…")

    def _describe_image_done(self, result: dict):
        if result.get("success"):
            self.ed_image_note.setText(str(result.get("image_note") or ""))
        self.notice.setText(str(result.get("message") or ("已重新识别" if result.get("success") else "识别失败")))

    def _clear_persona_image(self):
        if not self._current or not self._pending_image:
            self.notice.setText("这个人设没有图片")
            return
        if not ask_confirm(self, "清除人设图片", f"确定清除「{self._current}」的形象图和自我描述吗？",
                           detail="人设的文字内容不受影响。", ok_text="清除", danger=True):
            return
        result = api_post("personas/image/remove", {"name": self._current})
        if result.get("success"):
            self._pending_image = ""
            self.ed_image_note.clear()
            self.notice.setText(str(result.get("message") or "已清除"))
            self._refresh_image_preview()
        else:
            self.notice.setText(str(result.get("message") or result.get("detail") or "清除失败"))

    # ── 导出 / 导入人设文件 ────────────────────────────────────────────────
    def _export_persona(self):
        """把当前表单（含未保存的修改）存成 .yaml 文件。"""
        payload = self._payload(rename=False)
        if not payload["name"]:
            self.notice.setText("先填「标识」再导出")
            return
        result = api_post("personas/export", payload)
        if not result.get("success"):
            message = str(result.get("message") or result.get("detail") or "导出失败")
            self.notice.setText(message)
            tell(self, "导出人设失败", message, kind="warning")
            return
        suggested = str(result.get("filename") or f"persona-{payload['name']}.yaml")
        path, _ = file_dialogs.save_file(self, "保存人设到文件", suggested,
                                              "人设文件 (*.yaml *.yml);;所有文件 (*)")
        if not path:
            return
        try:
            Path(path).write_text(str(result.get("content") or ""), encoding="utf-8")
        except OSError as exc:
            message = f"写入失败：{exc}"
            self.notice.setText(message)
            tell(self, "导出人设失败", message, kind="warning")
            return
        self.notice.setText(f"已导出到 {path}")

    def _import_persona(self):
        """选一个 .yaml/.yml/.json 文件导入成人设。"""
        path, _ = file_dialogs.open_file(self, "导入人设文件", "",
                                              "人设文件 (*.yaml *.yml *.json *.txt);;所有文件 (*)")
        if not path:
            return
        try:
            from src.persona_import import read_persona_file
            content = read_persona_file(path)
        except (OSError, UnicodeError, ValueError) as exc:
            message = f"读不到这个人设文件（支持 UTF-8、UTF-16 文本）：{exc}"
            self.notice.setText(message)
            tell(self, "导入人设失败", message, kind="warning")
            return
        result = api_post("personas/import", {"filename": Path(path).name, "content": content})
        if not result.get("success"):
            message = str(result.get("message") or result.get("detail") or "导入失败")
            self.notice.setText(message)
            tell(self, "导入人设失败", message, kind="warning")
            return
        name = str(result.get("name") or "")
        if name:
            self._current = name
        self.refresh()
        if name:
            self._load_detail(name)
        self.notice.setText(str(result.get("message") or f"已导入 {name}")
                            + '；已保存并选中，点击顶部「设为当前」即可用于后续回复。')

    def _delete(self):
        if not self._current:
            return
        if not ask_confirm(self, "删除人设", f"确定删除「{self._current}」吗？",
                           detail="此操作不可撤销。", ok_text="删除", danger=True):
            return
        result = api_post(f"personas/{self._current}/delete")
        # 后端删除走 DELETE；上面 api_post 走不通时退化为 requests.delete
        if not result.get("success"):
            try:
                result = local_request('delete', f"{BASE_URL}/api/personas/{self._current}", timeout=5).json()
            except Exception as exc:
                result = {"success": False, "message": str(exc)}
        self.notice.setText(result.get("message") or ("已删除" if result.get("success") else "删除失败"))
        if result.get("success"):
            self._current = ""
            self._new()
            self.refresh()


# ── 插件详细配置 ──────────────────────────────────────────────────────────────
class PluginConfigDialog(QDialog):
    """按插件的 config_schema 生成表单：布尔→勾选框、数字→数值框、选项→下拉框、
    多行文本→大文本框。保存时把整份设置写回 /api/plugins/{id}。"""

    def __init__(self, plugin: dict, parent=None):
        super().__init__(parent)
        meta = plugin or {}
        self.plugin_id = meta.get("id", "")
        # /api/plugins 返回的键是 schema / defaults（不是 config_schema），这里做兼容。
        # 第三方插件可能给出 list/None：先判类型，避免构造配置窗时 AttributeError。
        raw_schema = meta.get("schema") or meta.get("config_schema")
        self._schema = raw_schema if isinstance(raw_schema, dict) else {}
        self._defaults = meta.get("defaults") or meta.get("default_settings") or {}
        self._widgets: dict = {}
        self._file_buttons = {}
        self.saved = False

        self.setWindowTitle(f"插件设置 · {meta.get('name') or self.plugin_id}")
        self.setWindowIcon(slot_icon("dialog"))
        self.resize(680, 620)
        if self.plugin_id == 'chat_controls':
            area=self.screen().availableGeometry()
            self.resize(min(740,area.width()-40),min(740,area.height()-60))
        if any(isinstance(spec,dict) and spec.get('editor')=='keyword_rules' for spec in self._schema.values()):
            self.resize(780,660)
        self.setStyleSheet(DIALOG_STYLESHEET)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        head = QLabel(f"{meta.get('name') or self.plugin_id}  ({self.plugin_id})")
        head.setStyleSheet(f"color: {ACCENT}; font-size: 15px; font-weight: bold;")
        outer.addWidget(head)
        desc = QLabel(meta.get("description") or "")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        outer.addWidget(desc)
        if self.plugin_id == 'chat_controls':
            channel = QLabel('QQ 官方接入只接收平台推送的群 @ 消息，填写关键词不会让未 @ 的消息自动送达。普通 QQ / bridge 可对已收到的群消息按昵称触发。人设页“关键词”用于选择回答人设，与这里的唤醒称呼不同。')
            channel.setWordWrap(True)
            channel.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
            outer.addWidget(channel)
        if meta.get('status_note'):
            capability=QLabel(str(meta['status_note']))
            capability.setWordWrap(True);capability.setTextFormat(Qt.TextFormat.PlainText)
            capability.setStyleSheet(f'color: {ACCENT2}; font-size: 12px;')
            outer.addWidget(capability)

        if self.plugin_id in {'sticker_admin','sticker_steal','image_library'}:
            self.manage_stickers=QPushButton('管理表情库…')
            self.manage_stickers.clicked.connect(lambda:StickerLibraryDialog(ROOT,api_get,api_post,api_delete,self).exec())
            outer.addWidget(self.manage_stickers)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet(f"background: {CARD}; border-radius: 10px;")
        form = QFormLayout(body)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        settings = meta.get("settings") or self._defaults
        groups: dict = {}
        for key, spec in self._schema.items():
            if not isinstance(spec, dict):
                continue
            group = spec.get("group") or ""
            if key not in settings and key in self._defaults:
                settings = dict(settings)
                settings[key] = self._defaults[key]
            widget = self._make_widget(spec, settings.get(key))
            self._widgets[key] = (widget, spec)
            field_widget = self._file_field(key,widget,spec) if spec.get('file_picker') else widget
            if self.plugin_id == 'chat_controls' and key in ('enabled','requireAt','aliases'):
                field_widget = self._wakeup_field(key,widget,spec)
            label = QLabel(spec.get("label") or key)
            tip = spec.get("description") or ""
            if tip:
                label.setToolTip(tip)
                widget.setToolTip(tip)
            row_label = QLabel(spec.get("label") or key)
            if tip:
                row_label.setToolTip(tip)
            if group:
                if group not in groups:
                    box = QGroupBox(group)
                    box.setStyleSheet(
                        f"QGroupBox {{ color: {TEXT}; border: 1px solid {BORDER};"
                        "border-radius: 8px; margin-top: 10px; padding: 10px; }}"
                        f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: {ACCENT}; }}"
                    )
                    inner = QFormLayout(box)
                    inner.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
                    if self.plugin_id == 'chat_controls':
                        inner.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
                        inner.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
                    groups[group] = inner
                    form.addRow(box)
                groups[group].addRow(row_label, field_widget)
            else:
                if spec.get('editor')=='keyword_rules':
                    form.addRow(row_label)
                    form.addRow(field_widget)
                else:
                    form.addRow(row_label, field_widget)

        if not self._schema:
            form.addRow(QLabel("这个插件没有可配置项。"))
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        if self.plugin_id == 'chat_controls':
            self.wakeup_summary = QLabel()
            self.wakeup_summary.setWordWrap(True)
            self.wakeup_summary.setTextFormat(Qt.TextFormat.PlainText)
            self.wakeup_summary.setStyleSheet(f'color: {ACCENT}; font-size: 12px;')
            outer.addWidget(self.wakeup_summary)
            for key in ('enabled','requireAt'):
                self._widgets[key][0].toggled.connect(self._update_wakeup_summary)
            self._widgets['aliases'][0].textChanged.connect(self._update_wakeup_summary)
            self._widgets['interjectPercent'][0].valueChanged.connect(self._update_wakeup_summary)
            self._update_wakeup_summary()

        note = QLabel("鼠标悬停在标签上可以看该项的说明。修改后点「保存设置」立即生效。")
        if self.plugin_id == 'chat_controls':
            note.setText('先填写唤醒称呼，其它选项可以保持原值。示例只改当前表单，点击“保存设置”后才生效。')
            note.setWordWrap(True)
        note.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        outer.addWidget(note)

        buttons = QDialogButtonBox()
        if self._schema:
            btn_export = buttons.addButton("导出设置…", QDialogButtonBox.ButtonRole.ActionRole)
            btn_import = buttons.addButton("导入设置…", QDialogButtonBox.ButtonRole.ActionRole)
            btn_export.setToolTip("把当前表单里的设置保存成 JSON 文件（备份或换机器用）")
            btn_import.setToolTip("从导出的 JSON 恢复设置（会按 schema 校验并列出问题）")
            btn_export.clicked.connect(self._export_settings)
            btn_import.clicked.connect(self._import_settings)
        btn_save = buttons.addButton("保存设置", QDialogButtonBox.ButtonRole.AcceptRole)
        btn_reset = buttons.addButton("恢复默认", QDialogButtonBox.ButtonRole.ResetRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        btn_save.clicked.connect(self._save)
        btn_reset.clicked.connect(self._reset)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _wakeup_field(self,key,widget,spec):
        box=QWidget();layout=QVBoxLayout(box);layout.setContentsMargins(0,0,0,0)
        if key=='enabled':widget.setText('使用本页回复控制')
        if key=='requireAt':widget.setText('收到群消息后，需要 @ 或唤醒称呼')
        if key=='aliases':
            row=QHBoxLayout();sample=QLabel('示例：鲸鱼娘、小鲸鱼');sample.setWordWrap(True)
            row.addWidget(sample,1)
            button=QPushButton('添加示例');button.setObjectName('wakeup_examples')
            button.setStyleSheet(f'background: {ACCENT}; color: #FFFFFF; border: none; border-radius: 6px; padding: 6px 10px;')
            button.clicked.connect(self._append_wakeup_examples);row.addWidget(button);layout.addLayout(row)
            widget.setPlaceholderText('填写你希望群友使用的称呼，例如“小鲸鱼”；每行一个。')
            widget.setMinimumHeight(90);widget.setMaximumHeight(115)
        layout.addWidget(widget)
        hint=QLabel(spec.get('description',''));hint.setWordWrap(True)
        hint.setStyleSheet(f'color: {MUTED}; font-size: 12px;');layout.addWidget(hint)
        if key=='aliases':
            # 触发测试：输入一句话立刻告诉你"会不会触发、命中了哪个词"。
            # 用户反馈"设了唤醒词不触发"多半是词写错/被单字规则忽略/被其它闸门拦住，
            # 这里先把"词本身认不认得出"说清楚。
            test_row=QHBoxLayout()
            caption=QLabel('触发测试');caption.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
            test_row.addWidget(caption)
            self._wake_test=QLineEdit()
            self._wake_test.setPlaceholderText('输入一句话，例如“老鲸在吗”，看会不会触发')
            test_row.addWidget(self._wake_test,1)
            layout.addLayout(test_row)
            self._wake_result=QLabel('')
            self._wake_result.setWordWrap(True)
            self._wake_result.setStyleSheet(f'color: {ACCENT}; font-size: 12px;')
            layout.addWidget(self._wake_result)
            self._wake_test.textChanged.connect(self._update_wakeup_test)
        return box

    def _append_wakeup_examples(self):
        import re
        editor=self._widgets['aliases'][0];current=editor.toPlainText()
        words={v.strip() for v in re.split('[,，\n]',current)}
        add=[word for word in ('鲸鱼娘','小鲸鱼') if word not in words]
        if add:editor.setPlainText(current.rstrip()+('\n' if current.strip() else '')+'\n'.join(add))

    def _update_wakeup_summary(self,*_):
        values=self.values()
        # 用与引擎完全一致的拆分规则（src/mention.py）：早期这里自己 split 一遍，
        # 界面认的词和引擎认的词可能不一致（例如顿号分隔、全角、单字被忽略）。
        words,ignored=split_wake_words(values.get('aliases',''))
        if not values['enabled']:text='保存后：使用程序内置触发规则，不代表停止机器人。'
        elif not values['requireAt']:text='保存后：已收到的群消息不要求 @ 或昵称，可能回复任何群消息。'
        elif words:text=f'保存后：收到群消息后，@ 或 {len(words)} 个称呼可触发（'+ '、'.join(w[:24] for w in words[:4])+'）。'
        else:text='保存后：没有有效唤醒称呼，通常需要 @；并不关闭独立的跟聊或固定回复规则。'
        if ignored:
            text+=f' 已忽略（单字或空）：'+ '、'.join(ignored[:4]) + ('…' if len(ignored)>4 else '') + '。'
        if values.get('interjectPercent',0):text+=' 当前还允许未点名时插话。'
        text+=' 仍受平台投递、黑名单、限流及其它插件规则限制；跟聊、固定回复独立生效。'
        self.wakeup_summary.setText(text)

    def _file_field(self,key,editor,spec):
        container=QWidget()
        layout=QVBoxLayout(container)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(6)
        mode=spec.get('file_picker')
        images=bool(spec.get('file_extensions')) and set(spec['file_extensions'])<=IMAGE_EXTENSIONS
        button=QPushButton('选择文件夹…' if mode=='directory' else '上传图片…' if images else '上传程序…' if self.plugin_id=='pc_control' else '上传文件…')
        button.setObjectName('upload_'+key)
        button.setStyleSheet(f'background: {ACCENT}; color: #FFFFFF; border: none; border-radius: 7px; padding: 8px 16px; font-weight: bold;')
        button.clicked.connect(lambda checked=False,k=key:self._choose_plugin_files(k))
        self._file_buttons[key]=button
        controls=QHBoxLayout()
        controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        if spec.get('description'):
            hint=QLabel(spec['description'])
            hint.setWordWrap(True)
            hint.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
            layout.addWidget(hint)
        layout.addWidget(editor,1)
        return container

    def _choose_plugin_files(self,key):
        editor,spec=self._widgets[key]
        try:
            mode,extensions,_=upload_options(spec)
            if mode=='directory':
                path=file_dialogs.choose_dir(self,'选择插件使用的文件夹')
                if path:
                    editor.setText(str(Path(path).resolve()))
                return
            file_filter=('支持的文件 ('+' '.join('*'+v for v in extensions)+')') if extensions else '所有文件 (*)'
            if mode=='file':
                path,_=file_dialogs.open_file(self,'上传插件文件','',file_filter)
                paths=[path] if path else []
            else:
                paths,_=file_dialogs.open_files(self,'上传插件文件（可多选）','',file_filter)
            if not paths:
                return
            items=import_plugin_files(ROOT/'data/plugin_uploads',self.plugin_id,key,paths,spec)
            value=merge_uploaded_files(self._read_widget(editor,spec),items,mode)
            if spec.get('multiline') or spec.get('type')=='text' or mode in {'files','labeled_files'}:
                editor.setPlainText(value)
            else:
                editor.setText(value)
        except (OSError,ValueError) as exc:
            tell(self, '上传失败', str(exc), kind="error")

    def _make_widget(self, spec: dict, value):
        if spec.get('editor')=='pc_owner':
            widget=QLineEdit(str(value or ''));widget.setReadOnly(True);return widget
        if spec.get('editor')=='keyword_rules':
            return KeywordRulesEditor(value)
        kind = str(spec.get("type") or "string")
        if kind == "bool":
            widget = QCheckBox("启用")
            widget.setChecked(bool(value))
            return widget
        if kind == "select":
            widget = QComboBox()
            values = list(spec.get("values") or [])
            options = spec.get("options") or []
            for option in options:
                if isinstance(option, dict):
                    widget.addItem(str(option.get("label", option.get("value", ""))), option.get("value"))
            if not options:
                for item in values:
                    widget.addItem(str(item), item)
            index = widget.findData(value)
            if index < 0:
                index = widget.findText(str(value))
            if index >= 0:
                widget.setCurrentIndex(index)
            widget.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px;")
            return widget
        if kind in ("int", "float"):
            if kind == "int":
                widget = QSpinBox()
                widget.setRange(int(spec.get("min", -10 ** 9) or -10 ** 9), int(spec.get("max", 10 ** 9) or 10 ** 9))
            else:
                widget = QDoubleSpinBox()
                widget.setRange(float(spec.get("min", -10 ** 9) or -10 ** 9), float(spec.get("max", 10 ** 9) or 10 ** 9))
                widget.setDecimals(3)
            if spec.get("step"):
                widget.setSingleStep(spec["step"])
            try:
                widget.setValue(value if value is not None else 0)
            except (TypeError, ValueError):
                pass
            widget.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px;")
            return widget
        if kind == "text" or spec.get("multiline") or spec.get('file_picker') in {'files','labeled_files'}:
            widget = QTextEdit()
            widget.setPlainText("" if value is None else str(value))
            widget.setMinimumHeight(90)
            widget.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px;")
            return widget
        widget = QLineEdit()
        if isinstance(value, (list, tuple)):
            widget.setText(", ".join(str(v) for v in value))
        else:
            widget.setText("" if value is None else str(value))
        widget.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px;")
        return widget

    def _read_widget(self, widget, spec: dict):
        if spec.get('editor')=='keyword_rules':
            return widget.value()
        kind = str(spec.get("type") or "string")
        if kind == "bool":
            return widget.isChecked()
        if kind == "select":
            data = widget.currentData()
            return data if data is not None else widget.currentText()
        if kind == "int":
            return int(widget.value())
        if kind == "float":
            return float(widget.value())
        if kind == "text" or spec.get("multiline") or spec.get('file_picker') in {'files','labeled_files'}:
            return widget.toPlainText()
        return widget.text()

    def values(self) -> dict:
        result = {}
        for key, (widget, spec) in self._widgets.items():
            result[key] = self._read_widget(widget, spec)
        return result

    def _save(self):
        # 不要把 plugin_id 放进 body：后端 update_settings 是**严格 schema 校验**，
        # 任何不在该插件 config_schema 里的键都会抛
        # `unknown setting field 'plugin_id'`，导致保存整体失败。
        # 插件 id 已经在 URL 里了。
        try:
            payload = self.values()
        except ValueError as exc:
            tell(self, '请检查填写的内容', str(exc), ok_text="知道了")
            return
        for key,(_,spec) in self._widgets.items():
            if spec.get('editor')=='pc_owner':payload.pop(key,None)
        result = api_post(f"plugins/{self.plugin_id}", payload)
        if not result.get("success"):
            tell(self, "保存失败", str(result.get("message") or result), kind="error")
            return
        self.saved = True
        # 规则类插件最容易踩的坑：规则填好了，但插件卡片上的开关是"已停用"，
        # 于是"配了关键词却不回复"。保存后直接说清楚，别让人白等。
        rules_key = next((key for key, (_, spec) in self._widgets.items()
                          if isinstance(spec, dict) and spec.get('editor') == 'keyword_rules'), '')
        if rules_key:
            try:
                row_count = len(payload.get(rules_key) or '')
                enabled = bool(payload.get('enabled', True))
            except Exception:
                row_count, enabled = 0, True
            if not enabled:
                tell(self, "规则已保存，但插件是停用的",
                     "这些规则现在不会生效。请在插件中心把「" +
                     f"{self.plugin_id}" + "」卡片上的开关打开（显示“已启用”）后再试。",
                     kind="info")
            elif row_count == 0:
                tell(self, "规则已保存，但内容是空的",
                     "没有规则时这个插件不会回复任何消息，请在「配置」里按“关键词=回复”一行一条填写。",
                     kind="info")
        self.accept()

    def _update_wakeup_test(self, *_):
        """输入一句话，实时回答"会不会触发、命中哪个词"。"""
        label = getattr(self, '_wake_result', None)
        if label is None:
            return
        try:
            probe = self._wake_test.text()
            aliases = self._widgets.get('aliases', (None, None))[0]
            raw = aliases.toPlainText() if aliases is not None else ''
            label.setText(mention_explain(probe, raw) if str(probe).strip()
                          else '上面输入一句话即可测试：它会告诉你有没有命中唤醒词。')
        except Exception as exc:
            label.setText(f'测不出来：{exc}')

    def _export_settings(self):
        """把当前表单里的设置导出成 JSON（一键导出）。"""
        path, _ = file_dialogs.save_file(self, '导出插件设置',
                                        f'{self.plugin_id}-settings.json', 'JSON 文件 (*.json)')
        if not path:
            return
        try:
            plugin_settings_io.dump_settings(path, self.plugin_id, self.values())
            tell(self, '已导出', f'当前设置已写入：\n{path}', kind='info')
        except Exception as exc:
            tell(self, '导出失败', str(exc), kind='error')

    def _import_settings(self):
        """从导出的 JSON 一键导入（按 schema 校验，问题列出来，不静默丢弃）。"""
        path, _ = file_dialogs.open_file(self, '导入插件设置', '', 'JSON 文件 (*.json)')
        if not path:
            return
        values, problems = plugin_settings_io.load_settings(path, self._schema, self._defaults)
        if not values:
            tell(self, '导入失败', '\n'.join(problems) or '这个文件里没有可用设置', kind='error')
            return
        applied = 0
        for key, value in values.items():
            entry = self._widgets.get(key)
            if not entry:
                continue
            widget, spec = entry
            try:
                self._write_widget(widget, spec, value)
                applied += 1
            except Exception as exc:
                problems.append(f'{key} 写入界面失败：{exc}')
        detail = f'已把 {applied} 项填进表单，确认无误后点「保存设置」才会生效。'
        if problems:
            detail += '\n\n需要注意：\n' + '\n'.join(f'· {item}' for item in problems[:12])
        tell(self, '已导入设置', detail, kind='info')
        self._update_wakeup_summary()

    def _write_widget(self, widget, spec: dict, value) -> None:
        """把值写回对应控件（与 _reset 同一套规则，导入时复用）。"""
        kind = str(spec.get('type') or 'string')
        if spec.get('editor') == 'keyword_rules':
            widget.set_rules(value)
        elif kind == 'bool':
            widget.setChecked(bool(value))
        elif kind == 'select':
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
        elif kind in ('int', 'float'):
            widget.setValue(value if value is not None else 0)
        elif kind == 'text' or spec.get('multiline') or spec.get('file_picker') in {'files', 'labeled_files'}:
            widget.setPlainText('' if value is None else str(value))
        else:
            widget.setText('' if value is None else str(value))

    def _reset(self):
        for key, (widget, spec) in self._widgets.items():
            if spec.get('editor')=='pc_owner':continue
            kind = str(spec.get("type") or "string")
            default = self._defaults.get(key, spec.get("default"))
            if spec.get('editor')=='keyword_rules':
                widget.set_rules(default)
            elif kind == "bool":
                widget.setChecked(bool(default))
            elif kind == "select":
                index = widget.findData(default)
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif kind in ("int", "float"):
                try:
                    widget.setValue(default if default is not None else 0)
                except (TypeError, ValueError):
                    pass
            elif kind == "text" or spec.get("multiline") or spec.get('file_picker') in {'files','labeled_files'}:
                widget.setPlainText("" if default is None else str(default))
            else:
                widget.setText("" if default is None else str(default))


class PluginInstallDialog(QDialog):
    """从网址/仓库安装插件。

    ⚠️ 安全设计：远程安装等于**把外部代码装进程序、并且会被 import 执行**。
    所以这里刻意分成两步，中间必须由**用户点确认**：

    1. 「检查」→ 后端只下载并检查（不写盘），把结果、来源、大小、命中的危险用法
       展示在下面；
    2. 「确认安装」→ 才真正落盘并加载。

    检查不通过时"确认安装"按钮保持禁用，用户没法绕过。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加插件")
        self.setWindowIcon(slot_icon("dialog"))
        self.resize(720, 640)
        self.installed = False
        self._inspection = None
        self._batch_items = []
        self.local_paths = []
        self.setAcceptDrops(True)
        self.setStyleSheet(DIALOG_STYLESHEET)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)
        outer.addWidget(icon_row("安装插件", "btn_new"))

        # 用 HTML 加粗，不要写 Markdown 的 ** —— QLabel 不解析 Markdown，
        # 星号会原样显示出来（之前就是这样）。
        warn = QLabel(
            "注意：插件是<b>外部代码</b>，安装后会被本程序加载并执行。"
            "请只安装你信任的来源。安装前请先点「检查」看清它是什么。")
        warn.setWordWrap(True)
        warn.setTextFormat(Qt.TextFormat.RichText)
        warn.setStyleSheet(f"color: {ERROR}; background: rgba(200,60,60,18);"
                           f" padding: 10px; border-radius: 6px; font-size: 12px;")
        outer.addWidget(warn)

        form = QFormLayout()
        form.setSpacing(8)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://…/plugin.py、https://…/plugin.zip 或 GitHub 仓库地址")
        self.url.setAccessibleName("插件地址")
        form.addRow("网址安装", self.url)
        # 本地文件：给"浏览…"按钮，不让用户手打路径（打错的失败最难查）
        local_row = QHBoxLayout()
        local_row.setSpacing(8)
        self.local_path = QLineEdit()
        self.local_path.setPlaceholderText("选择一个或多个本地 .py / .zip 插件文件（也支持插件文件夹里的 .py）")
        self.local_path.setAccessibleName("本地插件文件")
        local_row.addWidget(self.local_path, 1)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setProperty("class", "secondary")
        self.btn_browse.clicked.connect(self._browse)
        local_row.addWidget(self.btn_browse)
        self.btn_folder = QPushButton("文件夹…")
        self.btn_folder.setProperty("class", "secondary")
        self.btn_folder.setToolTip("选择插件文件夹，自动展开里面的 .py")
        self.btn_folder.clicked.connect(self._browse_folder)
        local_row.addWidget(self.btn_folder)
        form.addRow("本地文件", local_row)
        self.overwrite = QCheckBox("覆盖同名插件（更新用）")
        self.overwrite.setToolTip("勾选后，导入的同名插件会直接替换旧文件；不勾选时遇到同名会报告失败。")
        form.addRow("批量导入", self.overwrite)
        self.auto_enable = QCheckBox("安装后立即启用")
        self.auto_enable.setChecked(True)
        self.auto_enable.setToolTip("加载成功后自动打开插件；加载失败会回滚，不会留下坏文件。")
        form.addRow("启用", self.auto_enable)
        self.plugin_id = QLineEdit()
        self.plugin_id.setPlaceholderText("留空自动读取插件声明的标识")
        self.plugin_id.setAccessibleName("插件标识")
        form.addRow("插件标识", self.plugin_id)
        outer.addLayout(form)

        actions = QHBoxLayout()
        self.btn_check = QPushButton("检查")
        self.btn_check.setProperty("class", "secondary")
        self.btn_check.clicked.connect(self._check)
        actions.addWidget(self.btn_check)
        self.btn_install = QPushButton("确认安装")
        self.btn_install.setEnabled(False)
        self.btn_install.clicked.connect(self._install)
        actions.addWidget(self.btn_install)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("class", "secondary")
        self.btn_close.clicked.connect(self.reject)
        actions.addWidget(self.btn_close)
        self.btn_import_any = QPushButton("导入任意包…")
        self.btn_import_any.setProperty("class", "secondary")
        self.btn_import_any.setToolTip(
            "走统一导入窗口，一次装插件 / 技能 / 功能包；装上了会自动刷新插件清单")
        self.btn_import_any.clicked.connect(self._import_any_pack)
        actions.addWidget(self.btn_import_any)
        actions.addStretch(1)
        outer.addLayout(actions)

        self.result = QLabel("还没有检查。先点「检查」看看这个插件是什么。")
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setStyleSheet(f"color: {TEXT}; font-size: 12px;")
        outer.addWidget(self.result)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("这里会显示插件源码前 40 行")
        self.preview.setStyleSheet(
            f"QTextEdit {{ background: {FIELD}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 8px; font-family: Consolas, monospace; }}")
        outer.addWidget(self.preview, 1)

        self._checking = False
        self._source_revision = 0
        self._automatic_plugin_id = ''
        self.url.textChanged.connect(self._source_changed)
        self.local_path.textChanged.connect(self._source_changed)
        self.local_path.textEdited.connect(self._local_edited)
        self.plugin_id.textChanged.connect(self._invalidate_inspection)
        self.plugin_id.textEdited.connect(self._id_edited)

    def _id_edited(self, _text):
        self._automatic_plugin_id = ''

    def _source_changed(self, *_args):
        if self._automatic_plugin_id and self.plugin_id.text() == self._automatic_plugin_id:
            self.plugin_id.blockSignals(True)
            self.plugin_id.clear()
            self.plugin_id.blockSignals(False)
        self._automatic_plugin_id = ''
        self._invalidate_inspection()

    def _invalidate_inspection(self, *_args):
        self._source_revision += 1
        self._inspection = None
        self._batch_items = []
        self.btn_install.setEnabled(False)

    def _local_edited(self, _text):
        self.local_paths = []

    def _source(self) -> tuple:
        """当前要装的是哪个来源？返回 ``("url", str)`` 或 ``("local", [路径…])``。

        本地文件优先（用户刚点了"浏览…"的意图更明确）。
        """
        local = [path for path in self.local_paths if path]
        if not local and self.local_path.text().strip():
            local = [self.local_path.text().strip()]
        if local:
            return "local", local
        return "url", self.url.text().strip()

    def _browse(self):
        """在桌面主线程打开浅色选择窗，不经过后台 Tk/系统深色窗口。"""
        paths, _ = file_dialogs.open_files(
            self, "选择插件文件（可多选）", "", "插件文件 (*.py *.zip)")
        self._browse_done({"success": True, "paths": paths})

    def _browse_done(self, data: dict):
        self.btn_browse.setEnabled(True)
        if not data.get("success"):
            self.result.setText(
                f"打开文件选择框失败：{data.get('message') or data.get('detail') or '未知原因'}")
            return
        paths = [str(path) for path in (data.get("paths") or []) if str(path)]
        if not paths and data.get("path"):
            paths = [str(data["path"])]
        if not paths:
            return                      # 用户取消
        self.local_paths = paths
        self.local_path.setText(paths[0] + (f"（共 {len(paths)} 个）" if len(paths) > 1 else ""))
        self._invalidate_inspection()
        self.result.setText(f"已选择 {len(paths)} 个本地文件：\n" + "\n".join(paths)
                            + "\n现在点「检查」看看它们是什么。")

    def _browse_folder(self):
        path = file_dialogs.choose_dir(self, "选择插件文件夹")
        self._folder_done({"success": True, "paths": [path] if path else []})

    def _folder_done(self, data: dict):
        self.btn_folder.setEnabled(True)
        paths = [str(path) for path in (data.get("paths") or []) if str(path)]
        if not data.get("success") or not paths:
            if not data.get("success"):
                self.result.setText("打开文件夹选择框失败：" + str(data.get("message") or ""))
            return
        self.local_paths = paths
        self.local_path.setText(paths[0] + "（文件夹）")
        self._invalidate_inspection()
        self.result.setText("已选择文件夹：\n" + paths[0] + "\n现在点「检查」展开里面的 .py / .zip。")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile() and url.toLocalFile()]
        if not paths:
            return
        self.local_paths = paths
        self.local_path.setText(paths[0] + (f"（共 {len(paths)} 个）" if len(paths) > 1 else ""))
        self._invalidate_inspection()
        self.result.setText(f"已拖入 {len(paths)} 个路径：\n" + "\n".join(paths)
                            + "\n现在点「检查」看看它们是什么。")
        event.acceptProposedAction()

    def _check(self):
        if self._checking:
            return
        kind, value = self._source()
        if not value:
            self.result.setText("请填写插件网址，或用「浏览…」选择一个本地插件文件。")
            return
        self.btn_check.setEnabled(False)
        self.btn_install.setEnabled(False)
        self.result.setText("正在检查…")
        QApplication.processEvents()

        folder = kind == "local" and len(value) == 1 and Path(value[0]).is_dir()
        if kind == "local" and (len(value) > 1 or folder):
            endpoint = "plugins/inspect-local-batch"
            payload = {"paths": value}
            callback = self._check_batch_done
        elif kind == "local":
            endpoint = "plugins/inspect-local"
            payload = {"path": value[0], "plugin_id": self.plugin_id.text().strip()}
            callback = self._check_done
        else:
            endpoint = "plugins/inspect"
            payload = {"url": value, "plugin_id": self.plugin_id.text().strip()}
            callback = self._check_done

        # 检查要下载并读源码（远端最慢 60 秒），放后台线程，别卡住窗口
        self._checking = True
        revision = self._source_revision
        def checked(data):
            self._checking = False
            self.btn_check.setEnabled(True)
            if revision != self._source_revision:
                self.result.setText("选择的来源已改变，请重新点「检查」。")
                return
            callback(data)
        run_api_async(self, endpoint, payload, timeout=120, callback=checked,
                      busy="正在检查（读取源码并做安全检查）…")

    def _check_done(self, data: dict):
        self.btn_check.setEnabled(True)
        kind, _value = self._source()

        self._inspection = data
        source_label = "本地文件" if kind == "local" else "网址"
        if data.get("problems"):
            # 后端可能把 ok=false 和 problems 一起返回；必须先展示具体原因，
            # 不能只显示一句“检查失败”让用户无处排查。
            self._inspection = None
            self.result.setTextFormat(Qt.TextFormat.RichText)
            self.result.setText(
                f"<b>安全检查未通过</b>（来源：{source_label}），不允许安装：<br>"
                + "<br>".join(f"&nbsp;&nbsp;· {p}" for p in data["problems"]))
            self.btn_install.setEnabled(False)
            self.preview.setPlainText("")
            return
        if not data.get("ok"):
            self._inspection = None
            message = str(data.get("message") or data.get("detail") or "检查失败")
            self.result.setTextFormat(Qt.TextFormat.PlainText)
            self.result.setText(f"检查未通过：{message}")
            self.preview.setPlainText("")
            return
        else:
            # notices 是非阻断提示（例如 plugin.json 缺必填字段）：要显眼，但不能拦安装。
            notices = [str(one) for one in (data.get("notices") or [])]
            self.result.setTextFormat(Qt.TextFormat.PlainText)
            self.result.setText(
                f"检查通过，可以安装。（来源：{source_label}）\n"
                f"  类型：{'压缩包' if data.get('kind') == 'archive' else '单个 .py 文件'}\n"
                f"  实际来源：{data.get('resolved_url')}\n"
                f"  插件标识：{data.get('plugin_id')}\n"
                f"  大小：{data.get('bytes', 0) // 1024} KB，约 {data.get('lines')} 行\n"
                + ("  这是内置插件的同名升级包；安装后会以自定义文件覆盖内置版本。\n" if data.get('builtin') else "")
                + ("  同名自定义插件已存在；要更新请勾选「覆盖同名插件」。\n" if data.get('existing') else "")
                + (("  本机缺少依赖：%s（装好后插件可能加载失败）\n" % "、".join(data.get('missing') or []))
                   if data.get('missing') else "")
                + ("  提示（不影响安装）：\n"
                   + "".join("    · %s\n" % one for one in notices) if notices else "")
                + "  确认无误后再点「确认安装」。")
            self.btn_install.setEnabled(True)
            if not self.plugin_id.text().strip():
                self.plugin_id.blockSignals(True)
                self.plugin_id.setText(str(data.get("plugin_id") or ""))
                self._automatic_plugin_id = self.plugin_id.text()
                self.plugin_id.blockSignals(False)
        if "preview" in data:
            # 后端没给 preview 时不要清空：提示级信息不该把用户正在看的源码抹掉。
            self.preview.setPlainText(str(data.get("preview") or ""))

    def _check_batch_done(self, data: dict):
        self.btn_check.setEnabled(True)
        items = data.get("items") or []
        self._batch_items = items
        if not items:
            self._inspection = None
            self.result.setTextFormat(Qt.TextFormat.PlainText)
            self.result.setText("没有找到可导入的插件文件。")
            self.btn_install.setEnabled(False)
            return
        passed = [item for item in items if item.get("ok")]
        lines = [f"共 {len(items)} 个文件，{len(passed)} 个通过；确认后只安装通过项："]
        for index, item in enumerate(items, 1):
            name = item.get("name") or item.get("plugin_id") or Path(str(item.get("path") or "")).name
            state = "通过" if item.get("ok") else "未通过"
            detail = "" if item.get("ok") else ("：" + str(item.get("error") or "；".join(item.get("problems") or [])))
            notes = []
            if item.get("existing"):
                notes.append("同名已存在")
            if item.get("missing"):
                notes.append("缺少依赖 " + "、".join(item.get("missing") or []))
            # 非阻断提示同样逐条列出：它们不参与 ok 判定，不能混进"未通过"里。
            notes.extend("提示：" + str(one) for one in (item.get("notices") or []))
            lines.append(f"{index}. {name}（{state}）{detail}"
                         + ((" · " + "；".join(notes)) if notes else ""))
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setText("\n".join(lines))
        if passed:
            self._inspection = passed[0]
            self.btn_install.setEnabled(True)
            if not self.plugin_id.text().strip() and len(items) == 1:
                self.plugin_id.blockSignals(True)
                self.plugin_id.setText(str(items[0].get("plugin_id") or ""))
                self._automatic_plugin_id = self.plugin_id.text()
                self.plugin_id.blockSignals(False)
        else:
            self._inspection = None
            self.btn_install.setEnabled(False)
        self.preview.setPlainText(str((items[0] or {}).get("preview") or ""))

    def _install(self):
        if not self._inspection:
            return
        kind, value = self._source()
        batch = (kind == "local" and isinstance(value, list)
                 and (len(value) > 1 or (len(value) == 1 and Path(value[0]).is_dir())))
        if batch:
            # 固定使用本次预检列出的通过项，避免文件夹在检查后新增文件被顺带安装。
            value = [item['path'] for item in self._batch_items if item.get('ok') and item.get('path')]
            if not value:
                return
        title = (f"即将安装 {len(value)} 个插件。" if batch
                 else f"即将安装插件「{self._inspection.get('plugin_id')}」。")
        if not ask_confirm(
                self, "确认安装", title,
                detail="它们来自" + ("本地文件" if kind == "local" else "外部网址")
                       + "，安装后会随程序一起加载执行。"
                       + ("\n勾选了覆盖：同名插件会被替换。" if self.overwrite.isChecked() else "")
                       + "\n确定要继续吗？",
                ok_text="确认安装"):
            return

        self.btn_install.setEnabled(False)
        if batch:
            endpoint = "plugins/install-local-batch"
            fingerprints = {str(item.get('path')): str(item.get('fingerprint') or '')
                            for item in self._batch_items
                            if item.get('ok') and item.get('path') and item.get('fingerprint')}
            payload = {"paths": value, "confirmed": True,
                       "fingerprints": fingerprints,
                       "overwrite": self.overwrite.isChecked(),
                       "enable": self.auto_enable.isChecked()}
        elif kind == "local":
            endpoint = "plugins/install-local"
            payload = {"path": value[0], "plugin_id": self.plugin_id.text().strip(),
                       "fingerprint": str(self._inspection.get('fingerprint') or ''),
                       "confirmed": True, "overwrite": self.overwrite.isChecked(),
                       "enable": self.auto_enable.isChecked()}
        else:
            endpoint = "plugins/install"
            payload = {"url": value, "plugin_id": self.plugin_id.text().strip(),
                       "fingerprint": str(self._inspection.get('fingerprint') or ''),
                       "confirmed": True, "overwrite": self.overwrite.isChecked(),
                       "enable": self.auto_enable.isChecked()}
        # 安装要下载 + 校验 + 落盘（远端最慢 90 秒），放后台线程
        run_api_async(self, endpoint, payload, timeout=90, callback=self._install_done,
                      busy="正在安装插件…")

    def _install_done(self, data: dict):
        if isinstance(data.get("results"), list):
            results = data["results"]
            good = [item for item in results if item.get("success")]
            bad = [item for item in results if not item.get("success")]
            lines = [f"成功 {len(good)} 个，失败 {len(bad)} 个。"]
            for item in results:
                name = item.get("name") or item.get("plugin_id") or Path(str(item.get("path") or "")).name
                lines.append(("✓ " if item.get("success") else "✗ ") + str(name)
                             + ("" if item.get("success") else "：" + str(item.get("message") or "")))
            self.result.setTextFormat(Qt.TextFormat.PlainText)
            self.result.setText("\n".join(lines))
            if good:
                self.installed = True
            if bad:
                self.btn_install.setEnabled(True)
                tell(self, "部分插件未安装", f"成功 {len(good)} 个，失败 {len(bad)} 个，详情见窗口。",
                     ok_text="好")
                return
            tell(self, "安装完成", f"已安装 {len(good)} 个插件", ok_text="好")
            self.accept()
            return
        if not data.get("success"):
            message = str(data.get("message") or data.get("detail") or "安装失败")
            self.result.setText(f"安装失败：{message}")
            self.btn_install.setEnabled(True)
            return
        self.installed = True
        tell(self, "安装完成", str(data.get("message") or "已安装"), ok_text="好")
        self.accept()

    @safe_slot(label="导入任意包")
    def _import_any_pack(self):
        """统一导入窗口：装上了就让上层插件页刷清单（父窗口的 refresh，不另造名字）。"""
        if not open_import_dialog(self):
            return
        refresh = getattr(self.parent(), "refresh", None)
        if callable(refresh):
            refresh()
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setText("导入完成：插件清单已刷新，关掉本窗就能看到新装的东西。")


class FeaturePackDialog(QDialog):
    """导入功能包：一个 zip 带插件 + Agent 技能 + 长期偏好。

    与插件安装相同的安全闸门：先预检展示内容，用户确认后才写入。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导入功能包")
        self.setWindowIcon(slot_icon("dialog"))
        self.resize(680, 560)
        self.installed = False
        self.info = None
        self.path = ""
        self.setStyleSheet(DIALOG_STYLESHEET)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)
        outer.addWidget(icon_row("导入功能包", "btn_new"))
        hint = QLabel("功能包是一个 zip，里面 pack.json 描述插件、Agent 技能与长期偏好。"
                      "导入前会先预检；插件仍会做安全检查，技能只作为说明数据保存，不执行代码。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED};")
        outer.addWidget(hint)
        row = QHBoxLayout()
        self.path_label = QLabel("还没有选择文件")
        self.path_label.setWordWrap(True)
        row.addWidget(self.path_label, 1)
        self.btn_browse = QPushButton("选择功能包…")
        self.btn_browse.setProperty("class", "secondary")
        self.btn_browse.clicked.connect(self._browse)
        row.addWidget(self.btn_browse)
        outer.addLayout(row)
        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("预检结果会显示在这里")
        self.report.setStyleSheet(
            f"QTextEdit {{ background: {FIELD}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 8px; }}")
        outer.addWidget(self.report, 1)
        actions = QHBoxLayout()
        self.btn_install = QPushButton("确认导入")
        self.btn_install.setEnabled(False)
        self.btn_install.clicked.connect(self._install)
        actions.addWidget(self.btn_install)
        close = QPushButton("关闭")
        close.setProperty("class", "secondary")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        actions.addStretch(1)
        outer.addLayout(actions)

    def _browse(self):
        path, _ = file_dialogs.open_file(self, "选择功能包", "", "功能包 (*.zip)")
        self._browse_done({"success": True, "path": path})

    def _browse_done(self, data: dict):
        self.btn_browse.setEnabled(True)
        if not data.get("success") or not data.get("path"):
            if not data.get("success"):
                self.report.setPlainText("打开文件选择框失败：" + str(data.get("message") or ""))
            return
        self.path = str(data["path"])
        self.path_label.setText(self.path)
        self.report.setPlainText("正在预检…")
        run_api_async(self, "feature-packs/inspect", {"path": self.path}, timeout=60,
                      callback=self._inspect_done, busy="正在预检功能包…")

    def _inspect_done(self, data: dict):
        if not data.get("success") or not data.get("ok"):
            self.info = None
            self.btn_install.setEnabled(False)
            self.report.setPlainText("预检未通过："
                                     + str(data.get("message") or data.get("detail") or "包内容不完整"))
            return
        self.info = data
        lines = [f"功能包：{data.get('name')}",
                 f"说明：{data.get('description') or '（无）'}",
                 f"作者：{data.get('author') or '（未标注）'}",
                 f"包含：插件 {data.get('plugins', 0)} 个 · 技能 {data.get('skills', 0)} 个 · "
                 f"偏好 {data.get('preferences', 0)} 条", ""]
        for item in data.get("items") or []:
            kind = {"plugin": "插件", "skill": "技能", "preference": "偏好"}.get(item.get("kind"), "条目")
            name = item.get("name") or item.get("key") or item.get("file")
            lines.append(f"· [{kind}] {name}" + (f"（{item.get('bytes', 0) // 1024} KB）"
                                                if item.get("bytes") else ""))
        self.report.setPlainText("\n".join(lines))
        self.btn_install.setEnabled(True)

    def _install(self):
        if not self.info:
            return
        if not ask_confirm(self, "确认导入",
                           f"即将导入功能包「{self.info.get('name')}」。",
                           detail="插件会被加载执行；技能与偏好只写入本地学习库。\n确定继续吗？",
                           ok_text="确认导入"):
            return
        self.btn_install.setEnabled(False)
        run_api_async(self, "feature-packs/install",
                      {"path": self.path, "confirmed": True, "overwrite": False},
                      timeout=120, callback=self._install_done, busy="正在导入功能包…")

    def _install_done(self, data: dict):
        results = data.get("results") or []
        lines = ["导入完成。" if data.get("success") else "部分内容未导入："]
        for item in results:
            kind = {"plugin": "插件", "skill": "技能", "preference": "偏好"}.get(item.get("kind"), "条目")
            name = item.get("name") or item.get("key") or item.get("file")
            lines.append(("✓ " if item.get("success") else "✗ ") + f"[{kind}] {name}"
                         + ("" if item.get("success") else "：" + str(item.get("message") or "")))
        self.report.setPlainText("\n".join(lines))
        installed = any(item.get("success") and item.get("kind") == "plugin" for item in results)
        if installed:
            self.installed = True
        if data.get("success"):
            tell(self, "导入完成", "功能包已导入。", ok_text="好")
            self.accept()
        else:
            self.btn_install.setEnabled(True)
            tell(self, "部分内容未导入", "详情见窗口列表。", ok_text="好")


class MarketDialog(QDialog):
    """浏览 QQ Agent 市集，并把选中的 Node 扩展安全落盘。

    市集条目不是 Python 插件，不能走 plugin_installer；这里只下载到
    data/qqagent_extensions，交给能力桥/QQ Agent 使用，绝不在 Python 进程里
    import 或执行下载的 JS。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QQ Agent 插件市场")
        self.resize(980, 680)
        self._items = []
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("插件市场")
        title.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {TEXT};")
        head.addWidget(title)
        self.status = QLabel("正在读取市场清单…")
        self.status.setStyleSheet(f"color: {MUTED};")
        head.addWidget(self.status, 1)
        self.btn_refresh = QPushButton("刷新市场")
        self.btn_refresh.clicked.connect(self._load)
        head.addWidget(self.btn_refresh)
        # 官网已无限期停服：不再内嵌链接，改成用户自己贴 ZIP 链接或选本地包。
        self.btn_import_url = QPushButton("导入链接…")
        self.btn_import_url.setToolTip("粘贴插件 ZIP 的 https 下载链接，程序会自己下载并校验")
        self.btn_import_url.clicked.connect(self._import_url)
        head.addWidget(self.btn_import_url)
        self.btn_import_zip = QPushButton("导入本地 ZIP…")
        self.btn_import_zip.setToolTip("选择本机的插件 ZIP 包；不联网也能安装")
        self.btn_import_zip.clicked.connect(self._import_zip)
        head.addWidget(self.btn_import_zip)
        root.addLayout(head)

        hint = QLabel("市场清单仍可下载：选中条目点「安装选中」会装进能力桥宿主并真加载一次；"
                      "某个条目下载失败时，可用「导入链接…」或「导入本地 ZIP…」装同一个包。"
                      "扩展是 QQ Agent/Node 包，不会作为 Python 插件加载；装完会自动写入"
                      "能力桥配置，再点「启用能力桥」才会生效。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(hint)

        self._bridge_enabled = False
        self._bridge_ready = False
        self.bridge_state = QLabel("能力桥：正在读取状态…")
        self.bridge_state.setWordWrap(True)
        self.bridge_state.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.bridge_state)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称、描述、作者或 ID…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        root.addWidget(self.search)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["名称", "分类", "版本", "下载"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(lambda *_: self._install())
        root.addWidget(self.table, 1)

        foot = QHBoxLayout()
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        foot.addWidget(self.notice, 1)
        self.btn_bridge = QPushButton("配置能力桥")
        self.btn_bridge.setProperty("class", "secondary")
        self.btn_bridge.setToolTip("把扩展目录、catalog 和自动发现配置写入 QQ Agent 能力桥；不会自动启用")
        self.btn_bridge.clicked.connect(self._sync_bridge)
        foot.addWidget(self.btn_bridge)
        self.btn_toggle_bridge = QPushButton("启用能力桥")
        self.btn_toggle_bridge.setProperty("class", "secondary")
        self.btn_toggle_bridge.setToolTip("启用后才运行白名单扩展；仅对主人生效")
        self.btn_toggle_bridge.setEnabled(False)
        self.btn_toggle_bridge.clicked.connect(self._toggle_bridge)
        foot.addWidget(self.btn_toggle_bridge)
        # 人是主动点「安装选中」的，意图明确：默认装完就在能力桥里启用。
        self.chk_enable = QCheckBox("安装后立即在能力桥启用")
        self.chk_enable.setChecked(True)
        self.chk_enable.setToolTip("启用后能力桥才会真正调用它；层级过高或缺少文件的扩展装好也不会启用")
        foot.addWidget(self.chk_enable)
        # 默认复用已装目录；勾上才用新包覆盖，避免点第二次安装就把用户改过的扩展冲掉。
        self.chk_force = QCheckBox("覆盖重装")
        self.chk_force.setChecked(False)
        self.chk_force.setToolTip("已安装的扩展默认沿用原目录；勾上后用市场包覆盖并重新探测")
        foot.addWidget(self.chk_force)
        self.btn_install = QPushButton("安装选中")
        self.btn_install.setEnabled(False)
        self.btn_install.clicked.connect(self._install)
        foot.addWidget(self.btn_install)
        root.addLayout(foot)
        self._bridge_done = ""
        self._install_note = ""
        QTimer.singleShot(0, lambda: self._refresh_bridge("能力桥状态已读取"))

    def _reason(self, data: dict, fallback: str) -> str:
        """FastAPI 的失败原因是 detail，不是 message；只读 message 会把真因吞掉。"""
        detail = data.get("detail")
        if isinstance(detail, (list, tuple)):
            detail = "；".join(str(one) for one in detail)
        return str(data.get("message") or detail or fallback)

    def _load(self):
        self.btn_refresh.setEnabled(False)
        self.status.setText("正在读取市场清单…")
        run_api_async(self, "qqagent/market", {"refresh": True}, timeout=45,
                      callback=self._loaded, busy="正在同步插件市场…")

    def _loaded(self, data: dict):
        self.btn_refresh.setEnabled(True)
        if not data.get("success"):
            self.status.setText(self._reason(data, "市场读取失败"))
            return
        self._items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        installable = sum(1 for item in self._items if item.get("downloadUrl"))
        self.status.setText(f"共 {len(self._items)} 个条目，可下载 {installable} 个")
        self._fill(self._items)

    def _fill(self, items):
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(0)
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            name = QTableWidgetItem(str(item.get("name") or item.get("id") or ""))
            name.setData(Qt.ItemDataRole.UserRole, str(item.get("id") or ""))
            name.setToolTip(str(item.get("description") or ""))
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(str(item.get("categoryName") or "")))
            self.table.setItem(row, 2, QTableWidgetItem("v" + str(item.get("version") or "0.0.0")))
            downloads = item.get("downloads") or 0
            self.table.setItem(row, 3, QTableWidgetItem(str(downloads)))
        self.table.setUpdatesEnabled(True)
        self.btn_install.setEnabled(bool(items))

    def _filter(self, text: str):
        needle = text.strip().lower()
        if not needle:
            self._fill(self._items)
            return
        visible = [item for item in self._items
                   if needle in " ".join(str(item.get(key) or "") for key in
                                         ("id", "name", "description", "author", "categoryName")).lower()]
        self._fill(visible)

    def _install(self):
        row = self.table.currentRow()
        if row < 0:
            return
        cell = self.table.item(row, 0)
        ident = str(cell.data(Qt.ItemDataRole.UserRole) or "") if cell else ""
        if not ident:
            return
        self.btn_install.setEnabled(False)
        self.notice.setText(f"正在下载 {ident}…")
        payload = {"id": ident, "enable": self.chk_enable.isChecked()}
        if self.chk_force.isChecked():
            payload["force"] = True
        run_api_async(self, "qqagent/market/install", payload, timeout=90,
                      callback=self._installed, busy="正在下载扩展包…")


    def _import_url(self):
        url, ok = QInputDialog.getText(
            self, "导入插件链接",
            "插件 ZIP 的 https 下载链接（例如 GitHub Release 资产）：")
        if not ok or not str(url).strip():
            return
        self.btn_import_url.setEnabled(False)
        self.notice.setText("正在下载插件包…")
        run_api_async(self, "extensions/install-url",
                      {"url": str(url).strip(), "enable": self.chk_enable.isChecked(),
                       "confirmed": True}, timeout=120,
                      callback=self._imported, busy="正在下载并校验插件包…")

    def _import_zip(self):
        path, _selected = file_dialogs.open_file(
            self, "导入本地插件包", "", "插件包 (*.zip);;所有文件 (*.*)")
        if not path:
            return
        self.btn_import_zip.setEnabled(False)
        self.notice.setText("正在校验本地插件包…")
        run_api_async(self, "extensions/install-local",
                      {"zipPath": str(path), "enable": self.chk_enable.isChecked(),
                       "confirmed": True}, timeout=120,
                      callback=self._imported, busy="正在校验并安装本地插件包…")

    def _imported(self, data: dict):
        self.btn_import_url.setEnabled(True)
        self.btn_import_zip.setEnabled(True)
        if not data.get("success"):
            self.notice.setText(self._reason(data, "导入失败"))
            return
        extension = data.get("extension") or {}
        done = str(data.get("message") or "已导入")
        self._install_note = f"{done}（{extension.get('path', '扩展目录')}）"
        self.notice.setText(f"{self._install_note}；正在写入能力桥配置…")
        self._sync_bridge()

    def _installed(self, data: dict):
        self.btn_install.setEnabled(True)
        if not data.get("success"):
            self.notice.setText(self._reason(data, "安装失败"))
            return
        extension = data.get("extension") or {}
        done = str(data.get("message") or "已安装")
        self._install_note = f"{done}（{extension.get('path', '扩展目录')}）"
        self.notice.setText(f"{self._install_note}；正在写入能力桥配置…")
        self._sync_bridge()

    def _sync_bridge(self):
        self.btn_bridge.setEnabled(False)
        run_api_async(self, "qqagent/bridge/sync", {}, timeout=30,
                      callback=self._bridge_synced, busy="正在配置能力桥…")

    def _bridge_synced(self, data: dict):
        self.btn_bridge.setEnabled(True)
        note, self._install_note = self._install_note, ""
        if not data.get("success"):
            reason = self._reason(data, "能力桥配置失败")
            self.notice.setText(f"{note}；{reason}" if note else reason)
            return
        catalog = data.get("catalog") or {}
        missing = [str(item) for item in (data.get("missing") or []) if str(item).strip()]
        text = f"已写入能力桥配置（{catalog.get('count', 0)} 个扩展）"
        if missing:
            text += "；还缺：" + "、".join(missing)
        else:
            text += "；启用「QQ Agent 能力桥」后生效"
        self._refresh_bridge(f"{note}；{text}" if note else text)

    def _refresh_bridge(self, done_text: str = ""):
        self._bridge_done = str(done_text or "")
        run_api_async(self, "qqagent/bridge/status", {}, timeout=15,
                      callback=self._bridge_status, busy="正在读取能力桥状态…")

    def _bridge_status(self, data: dict):
        done, self._bridge_done = self._bridge_done, ""
        if not data.get("success"):
            self._bridge_ready = False
            self.bridge_state.setText(
                "能力桥：" + str(data.get("message") or data.get("detail") or "状态读取失败"))
            self.btn_toggle_bridge.setEnabled(False)
            if done:
                self.notice.setText(f"{done}；能力桥状态读取失败")
            return
        self._bridge_enabled = bool(data.get("enabled"))
        self._bridge_ready = bool(data.get("ready"))
        profiles = int(data.get("profiles") or 0)
        missing = [str(item) for item in (data.get("missing") or []) if str(item).strip()]
        if self._bridge_enabled and self._bridge_ready:
            state = "已启用"
        elif self._bridge_enabled:
            state = "已启用，但配置不完整"
        elif self._bridge_ready:
            state = "已配置，可启用"
        else:
            state = "未就绪"
        text = f"能力桥：{state} · {profiles} 个可执行扩展"
        if missing:
            text += "；还缺：" + "、".join(missing)
        self.bridge_state.setText(text)
        self.bridge_state.setToolTip("、".join(
            str(item) for item in (data.get("providers") or [])) or "暂无白名单 provider")
        self.btn_toggle_bridge.setText("停用能力桥" if self._bridge_enabled else "启用能力桥")
        self.btn_toggle_bridge.setEnabled(
            bool(data.get("loaded")) and (self._bridge_enabled or self._bridge_ready))
        if done:
            self.notice.setText(done)

    def _toggle_bridge(self):
        target = not self._bridge_enabled
        if target:
            prompt = ("启用后会按白名单在消息处理里运行 Node 扩展，"
                      "只对已绑定主人生效。确认启用？")
        else:
            prompt = "停用后市场扩展将不再参与消息处理。确认停用？"
        if QMessageBox.question(
                self, "QQ Agent 能力桥", prompt,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.btn_toggle_bridge.setEnabled(False)
        run_api_async(
            self, "qqagent/bridge/toggle",
            {"enabled": target, "confirmed": True}, timeout=20,
            callback=self._bridge_toggled, busy="正在切换能力桥…")

    def _bridge_toggled(self, data: dict):
        if not data.get("success"):
            self.notice.setText(str(data.get("message") or "能力桥切换失败"))
            self._refresh_bridge()
            return
        self._refresh_bridge(str(data.get("message") or "能力桥状态已更新"))


def open_import_dialog(parent=None) -> bool:
    """统一「导入包…」：扩展管理页、技能市场页、功能包页、插件安装窗共用同一个窗口（W11-4b）。

    返回是否真的装上了东西 —— 调用方据此决定要不要重新拉清单。
    exec() 保留（生产里要模态）；离屏测试把 import_dialog.ImportDialog 换成替身即可绕过。
    """
    from src.gui.import_dialog import ImportDialog

    dialog = None
    try:
        dialog = ImportDialog(api_post=api_post, parent=parent)
        dialog.exec()
        return bool(dialog.installed)
    except Exception:
        return False
    finally:
        # 新对话框的收尾入口是 shutdown()（内部收线程池），它没有 requests 这种东西。
        if dialog is not None:
            for cleanup in (dialog.shutdown, dialog.deleteLater):
                try:
                    cleanup()
                except Exception:
                    pass


class ExtensionManagerDialog(QDialog):
    """逐项启停 QQ Agent 扩展（启用态的唯一权威在后端 extension_registry）。

    市场里几百个扩展默认全停：一次全开会拉起几百个 Node 进程。这个窗口只负责
    "哪些扩展允许被能力桥调用"，安装仍然走插件市场对话框。
    """

    LAYERS = [("", "全部层级"), ("installed", "已安装"), ("catalog", "市场清单"),
              ("builtin", "内置")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("扩展管理")
        self.resize(1080, 720)
        self._items: list[dict] = []
        self._loading = False
        self._log_path = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("扩展管理")
        title.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {TEXT};")
        head.addWidget(title)
        self.status = QLabel("正在读取扩展清单…")
        self.status.setStyleSheet(f"color: {MUTED};")
        head.addWidget(self.status, 1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._refresh)
        head.addWidget(self.btn_refresh)
        self.btn_log = QPushButton("打开调用日志")
        self.btn_log.setProperty("class", "secondary")
        self.btn_log.setToolTip("查看能力桥的调用账本 data/qqagent_bridge_calls.jsonl")
        self.btn_log.clicked.connect(self._open_log)
        head.addWidget(self.btn_log)
        self.btn_import_local = QPushButton("从本地 zip 安装…")
        self.btn_import_local.setProperty("class", "secondary")
        self.btn_import_local.setToolTip(
            "市场里没有、但你手上已经有 zip 的扩展包：先预检，再装进宿主根")
        self.btn_import_local.clicked.connect(self._import_local_zip)
        head.addWidget(self.btn_import_local)
        self.btn_import_package = QPushButton("导入包…")
        self.btn_import_package.setProperty("class", "secondary")
        self.btn_import_package.setToolTip(
            "四类包（Python 插件 / JS 扩展 / 技能 / 功能包）的统一入口：先预检，再确认安装")
        self.btn_import_package.clicked.connect(self._import_package)
        head.addWidget(self.btn_import_package)
        root.addLayout(head)

        hint = QLabel("扩展默认全部停用：只有勾选了「启用」的扩展才会被 QQ Agent 调用，"
                      "停用后既不枚举工具，也不会拉起 Node 进程。"
                      "安装扩展请到「插件市场」，手上已有 zip 就用「从本地 zip 安装…」。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(hint)

        # 市场清单层看得见但没落地：不写明的话，几百条「启用数 0」看着像坏了。
        self.layer_hint = QLabel(
            "层级「市场清单」的条目只是市场里可安装的东西，还没下载到本机，所以不能启用；"
            "先在「插件市场」点「安装选中」落地，装好后这里就能勾启用。")
        self.layer_hint.setWordWrap(True)
        self.layer_hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.layer_hint)

        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索 ID、名称、层级或阻塞原因…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        tools.addWidget(self.search, 1)
        self.layer = QComboBox()
        for value, label in self.LAYERS:
            self.layer.addItem(label, value)
        self.layer.setMinimumWidth(120)
        self.layer.currentIndexChanged.connect(self._filter)
        tools.addWidget(self.layer)
        self.btn_batch_on = QPushButton("批量启用")
        self.btn_batch_on.setProperty("class", "secondary")
        self.btn_batch_on.clicked.connect(lambda: self._batch(True))
        tools.addWidget(self.btn_batch_on)
        self.btn_batch_off = QPushButton("批量停用")
        self.btn_batch_off.setProperty("class", "secondary")
        self.btn_batch_off.clicked.connect(lambda: self._batch(False))
        tools.addWidget(self.btn_batch_off)
        root.addLayout(tools)

        self.table = QTableWidget(0, 7, self)
        self.table.setHorizontalHeaderLabels(
            ["启用", "ID", "名称", "层级", "工具数", "钩子", "阻塞原因"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in (0, 1, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._changed)
        root.addWidget(self.table, 1)

        self.bridge_state = QLabel("能力桥：正在读取状态…")
        self.bridge_state.setWordWrap(True)
        self.bridge_state.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.bridge_state)

        foot = QHBoxLayout()
        # 与插件市场同一个语义：只对「从本地 zip 安装…」生效；T4/T6 仍然会被拦。
        self.chk_enable = QCheckBox("安装后立即在能力桥启用")
        self.chk_enable.setChecked(True)
        self.chk_enable.setToolTip("T4（要宿主源码）/T6 的扩展即使勾选也不会启用")
        foot.addWidget(self.chk_enable)
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        foot.addWidget(self.notice, 1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("class", "secondary")
        self.btn_close.clicked.connect(self.accept)
        foot.addWidget(self.btn_close)
        root.addLayout(foot)
        QTimer.singleShot(0, self._initial_load)

    # ── 取数 ────────────────────────────────────────────────────────────
    def _initial_load(self):
        self._load_extensions()
        self._load_status()

    def _load_extensions(self, refresh: bool = False):
        self.btn_refresh.setEnabled(False)
        self.status.setText("正在读取扩展清单…")
        run_api_async(self, "extensions", {"refresh": bool(refresh)}, timeout=60,
                      callback=self._loaded, busy="正在读取扩展清单…")

    def _refresh(self):
        self.btn_refresh.setEnabled(False)
        self.status.setText("正在重扫扩展清单…")
        run_api_async(self, "extensions/scan", {"refresh": True}, timeout=120,
                      callback=self._loaded, busy="正在重扫扩展清单…")
        self._load_status()

    def _load_status(self):
        run_api_async(self, "extensions/status", {}, timeout=20,
                      callback=self._status_loaded)

    @staticmethod
    def _error_text(data: dict) -> str:
        return str(data.get("message") or data.get("detail") or "接口没有返回原因")

    def _loaded(self, data: dict):
        self.btn_refresh.setEnabled(True)
        if not data.get("ok") and not data.get("success"):
            self.status.setText("扩展清单读取失败：" + self._error_text(data))
            return
        self._items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        self._filter()
        self._render_summary(data.get("summary") or {})

    def _status_loaded(self, data: dict):
        self._log_path = str(data.get("log_path") or "")
        if not data.get("available"):
            note = str(data.get("message") or "")
            if data.get("loaded"):
                tail = ("（%s）" % note) if note else ""
                self.bridge_state.setText("能力桥：已加载但未启用" + tail)
            else:
                self.bridge_state.setText(
                    "能力桥：未加载 —— 即使启用了扩展，也要先在插件市场里启用能力桥才会被调用")
            return
        parts = ["能力桥：已启用",
                 "生效扩展 %s 个" % len(data.get("enabled_extensions") or []),
                 "调用 %s 次" % int(data.get("calls") or 0),
                 "失败 %s 次" % (int(data.get("provider_failures") or 0)
                               + int(data.get("hook_failures") or 0)
                               + int(data.get("tool_failures") or 0))]
        dead = [cap for cap in (data.get("capabilities") or [])
                if isinstance(cap, dict) and cap.get("deadInHost")]
        if dead:
            parts.append("宿主不支持的能力 %d 项" % len(dead))
        self.bridge_state.setText(" · ".join(parts))

    def _render_summary(self, summary: dict):
        layers = summary.get("layers") or {}
        parts = ["共 %s 个扩展" % summary.get("total", len(self._items)),
                 "已启用 %s 个" % summary.get("enabled", 0),
                 "可调用工具 %s 个" % summary.get("enabledToolCount", 0)]
        detail = "，".join("%s %s" % (label, layers.get(key, 0))
                          for key, label in (("installed", "已安装"),
                                             ("catalog", "市场清单"),
                                             ("builtin", "内置")))
        parts.append("分层 " + detail)
        if summary.get("error"):
            parts.append("扫描告警：" + str(summary.get("error")))
        if summary.get("stale"):
            parts.append("清单来自缓存")
        self.notice.setText(" · ".join(parts))

    # ── 渲染 ────────────────────────────────────────────────────────────
    def _visible(self) -> list:
        needle = self.search.text().strip().lower()
        layer = str(self.layer.currentData() or "")
        rows = []
        for item in self._items:
            if layer and str(item.get("layer") or "") != layer:
                continue
            if needle:
                haystack = " ".join([str(item.get("id") or ""),
                                     str(item.get("name") or ""),
                                     str(item.get("tier") or ""),
                                     " ".join(item.get("blockers") or [])]).lower()
                if needle not in haystack:
                    continue
            rows.append(item)
        return rows

    def _filter(self, *_args):
        rows = self._visible()
        self._loading = True
        try:
            self.table.setUpdatesEnabled(False)
            self.table.setRowCount(0)
            for item in rows:
                self._add_row(item)
        finally:
            self.table.setUpdatesEnabled(True)
            self._loading = False
        self.status.setText("显示 %d / %d 个扩展" % (len(rows), len(self._items)))

    def _add_row(self, item: dict):
        row = self.table.rowCount()
        self.table.insertRow(row)
        check = QTableWidgetItem("")
        check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        check.setCheckState(Qt.CheckState.Checked if item.get("enabled")
                            else Qt.CheckState.Unchecked)
        check.setData(Qt.ItemDataRole.UserRole, str(item.get("id") or ""))
        self.table.setItem(row, 0, check)
        ident = QTableWidgetItem(str(item.get("id") or ""))
        ident.setToolTip(str(item.get("path") or "没有本地文件"))
        name = QTableWidgetItem(str(item.get("name") or ""))
        tier = QTableWidgetItem(str(item.get("tier") or ""))
        tools = QTableWidgetItem(str(int(item.get("toolCount") or 0)))
        tools.setToolTip("工具数（启用后才会被枚举给模型）")
        hooks = QTableWidgetItem(str(int(item.get("hookCount") or 0)))
        blockers = item.get("blockers") or []
        blocker_text = "；".join(str(one) for one in blockers) or "—"
        cell = QTableWidgetItem(blocker_text)
        cell.setToolTip(blocker_text)
        for column, widget in enumerate([ident, name, tier, tools, hooks, cell], start=1):
            self.table.setItem(row, column, widget)

    def _import_package(self):
        """统一导入窗口：预检 → 确认 → 安装；真装上了才刷新清单。"""
        if open_import_dialog(self):
            self._refresh()

    # ── 本地 zip 导入 ───────────────────────────────────────────────────
    # 市场安装只能按 id 走 CDN；用户手上已经有一个 zip 时，这里是唯一入口。
    # 预检（inspect-local）只读，确认框里把层级/阻塞原因摊开，再调 install-local。
    def _import_local_zip(self):
        path, _selected = file_dialogs.open_file(
            self, "选择本地扩展包", "", "扩展包 (*.zip);;所有文件 (*)")
        if not path:
            self.notice.setText("没有选择文件，什么都没装")
            return
        self.notice.setText("正在预检 %s…" % Path(path).name)
        run_api_async(self, "extensions/inspect-local", {"zipPath": str(path)},
                      timeout=60,
                      callback=lambda data: self._local_inspected(str(path), data))

    def _local_inspected(self, path: str, data: dict):
        if not data.get("ok"):
            self.notice.setText("预检没通过：" + self._error_text(data))
            tell(self, "本地扩展包不能安装", self._error_text(data), kind="error")
            return
        blockers = [str(one) for one in (data.get("blockers") or [])]
        problems = [str(one) for one in (data.get("problems") or [])]
        lines = ["ID：%s" % (data.get("id") or "—"),
                 "名称：%s" % (data.get("name") or "—"),
                 "版本：%s" % (data.get("version") or "—"),
                 "层级：%s" % (data.get("tier") or "装后才能判定"),
                 "阻塞原因：%s" % ("、".join(blockers) or "无"),
                 "文件：%s 个，%.1f KB" % (int(data.get("files") or 0),
                                          int(data.get("bytes") or 0) / 1024.0)]
        if problems:
            lines.append("提示：%s" % "；".join(problems))
        if data.get("existing"):
            lines.append("宿主根里已有同 id：这里不覆盖（要覆盖得传 force）")
        if not ask_confirm(self, "确认安装本地扩展",
                           "把「%s」装进宿主根吗？" % (data.get("name") or data.get("id")),
                           detail="\n".join(lines), ok_text="安装"):
            self.notice.setText("已取消，什么都没装")
            return
        payload = {"zipPath": path, "confirmed": True, "force": False,
                   "enable": bool(self.chk_enable.isChecked())}
        self.notice.setText("正在安装 %s…" % (data.get("id") or ""))
        run_api_async(self, "extensions/install-local", payload, timeout=180,
                      callback=self._local_installed)

    def _local_installed(self, data: dict):
        if not data.get("ok") and not data.get("success"):
            self.notice.setText("本地安装失败：" + self._error_text(data))
            return
        self.notice.setText(str(data.get("message") or "本地扩展已安装"))
        self._load_extensions(refresh=True)

    # ── 启停 ────────────────────────────────────────────────────────────
    def _changed(self, item):
        if self._loading or item is None or item.column() != 0:
            return
        ident = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not ident:
            return
        self._set_enabled(ident, item.checkState() == Qt.CheckState.Checked)

    def _set_enabled(self, ident: str, enabled: bool):
        self.notice.setText(("正在启用 %s…" if enabled else "正在停用 %s…") % ident)
        run_api_async(self, "extensions/set-enabled",
                      {"id": ident, "enabled": bool(enabled)}, timeout=20,
                      callback=lambda data: self._saved(ident, bool(enabled), data))

    def _saved(self, ident: str, enabled: bool, data: dict):
        if not data.get("ok") and not data.get("success"):
            self.notice.setText("保存失败：" + self._error_text(data))
            self._filter()
            return
        for record in self._items:
            if str(record.get("id") or "") == ident:
                record["enabled"] = bool(enabled)
        self._filter()
        self.notice.setText(("已启用 %s" if enabled else "已停用 %s") % ident
                            + "，下一次工具调用立即生效")

    def _batch(self, enabled: bool):
        ids = []
        selection = self.table.selectionModel()
        rows = selection.selectedRows() if selection is not None else []
        for index in rows:
            cell = self.table.item(index.row(), 0)
            ident = str(cell.data(Qt.ItemDataRole.UserRole) or "") if cell else ""
            if ident and ident not in ids:
                ids.append(ident)
        scoped = "选中的"
        if not ids:
            ids = [str(item.get("id") or "") for item in self._visible() if item.get("id")]
            scoped = "当前筛选出的"
        if not ids:
            self.notice.setText("没有可操作的扩展")
            return
        action = "启用" if enabled else "停用"
        if not ask_confirm(self, "批量" + action + "扩展",
                           "确定要%s%s %d 个扩展吗？" % (action, scoped, len(ids)),
                           detail="批量操作只改启用状态，不会安装或删除任何文件。",
                           ok_text="继续" + action):
            return
        self.notice.setText("正在批量%s %d 个扩展…" % (action, len(ids)))
        run_api_async(self, "extensions/batch-enabled",
                      {"ids": ids, "enabled": bool(enabled)}, timeout=30,
                      callback=lambda data: self._batch_done(ids, bool(enabled), data))

    def _batch_done(self, ids: list, enabled: bool, data: dict):
        if not data.get("ok") and not data.get("success"):
            self.notice.setText("批量保存失败：" + self._error_text(data))
            return
        wanted = set(ids)
        for record in self._items:
            if str(record.get("id") or "") in wanted:
                record["enabled"] = bool(enabled)
        self._filter()
        action = "启用" if enabled else "停用"
        self.notice.setText("已%s %d 个扩展，下一次工具调用立即生效"
                            % (action, int(data.get("count") or len(ids))))

    def _open_log(self):
        if not self._log_path:
            self.notice.setText("还不知道日志路径：先点「刷新」读取能力桥状态")
            return
        target = Path(self._log_path)
        if not target.is_file():
            self.notice.setText("还没有调用记录（%s 不存在）" % target)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


# ── 技能市场（Agent 技能包 T1..T6）──────────────────────────────────────────────
# 层级只决定"能不能注册到工具环"，不决定装不装得上：门禁在后端 skill_registry，
# GUI 不自己判断，只把后端返回的 tier / note / error 照实显示出来。
SKILL_TIER_COLORS = {
    "T1": "#1E8E3E",   # 绿：可直接注册
    "T2": "#B26A00",   # 黄：可注册，能力受限
    "T3": TEXT,        # 正文色：按门禁注册
    "T4": MUTED,       # 灰：装得下，不注册
    "T5": MUTED,
    "T6": "#C0392B",   # 红：禁止注册
}
SKILL_REGISTERABLE_TIERS = ("T1", "T2", "T3")
# 后端 error.code → 中文；没收录的一律照抄 error.message 原文。
SKILL_ERROR_TEXT = {
    "exists": "已安装，如需覆盖请勾选覆盖安装",
    "unsafe-path": "包内路径不安全，已拒绝",
    "no-manifest": "包内缺少 skill.json/plugin.json",
    "bad-id": "ID 非法",
    "missing": "未安装",
}
_SKILL_FILE_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def skill_tier(value) -> str:
    """把 tier（'t1' / 1 / 'T1'）统一成 'T1'..'T6'；认不出就原样返回。"""
    text = str(value if value is not None else "").strip().upper()
    if not text:
        return ""
    if text.startswith("T") and text[1:].isdigit():
        return text
    if text.isdigit():
        return "T" + text
    return text


def skill_tier_color(tier) -> str:
    """层级 → 颜色；未知层级用次级灰，不猜它能不能注册。"""
    return SKILL_TIER_COLORS.get(skill_tier(tier), MUTED)


def skill_is_registerable(tier) -> bool:
    """T1/T2/T3 才可能注册到工具环（最终仍由后端门禁决定）。"""
    return skill_tier(tier) in SKILL_REGISTERABLE_TIERS


def skill_count(value) -> int:
    """后端的 files/tools/removed/skills 既可能是数量也可能是清单，统一按数量看。"""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def skill_error_text(data) -> str:
    """把 {ok:false, error:{code,message}} 翻成中文；没收录的码照抄原文。"""
    if not isinstance(data, dict):
        return "接口没有返回原因"
    error = data.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
    elif isinstance(error, str):
        code, message = "", error.strip()
    else:
        code, message = "", str(data.get("message") or data.get("detail") or "").strip()
    if code in SKILL_ERROR_TEXT:
        return SKILL_ERROR_TEXT[code]
    return message or "接口没有返回原因"


class SkillMarketDialog(QDialog):
    """技能市场：浏览市场条目，安装/卸载/重建清单，并显示后端门禁结论。

    与「QQ Agent 插件市场」不是一回事：那边下载 Node 扩展，这里落地带
    skill.json / plugin.json 的技能包。能不能注册到工具环由后端 skill_registry
    判定，GUI 只照抄后端返回的 tier / note / error，不自己编话术。
    """

    MARKET_COLUMNS = ["已装", "ID", "名称", "版本", "层级(T1..T6)", "工具数", "说明/原因"]
    INSTALLED_COLUMNS = ["启用", "ID", "名称", "版本", "层级", "工具数", "状态"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("技能市场")
        self.resize(1080, 720)
        self._market: list[dict] = []
        self._installed: list[dict] = []
        self._summary: dict = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("技能市场")
        title.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {TEXT};")
        head.addWidget(title)
        self.status = QLabel("正在读取技能清单…")
        self.status.setStyleSheet(f"color: {MUTED};")
        head.addWidget(self.status, 1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh_all)
        head.addWidget(self.btn_refresh)
        self.btn_import_package = QPushButton("导入包…")
        self.btn_import_package.setProperty("class", "secondary")
        self.btn_import_package.setToolTip(
            "四类包（Python 插件 / JS 扩展 / 技能 / 功能包）的统一入口：先预检，再确认安装")
        self.btn_import_package.clicked.connect(self._import_package)
        head.addWidget(self.btn_import_package)
        root.addLayout(head)

        hint = QLabel("技能包装到本机技能目录后不会立刻被调用：「安装后注册到工具环」只决定要不要注册；"
                      "T4 及以上由后端门禁停用（装得下，但不会出现在工具环里）。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(hint)

        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索 ID、名称或说明…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        tools.addWidget(self.search, 1)
        self.only_registerable = QCheckBox("只看可注册(T1/T2/T3)")
        self.only_registerable.setToolTip("T4 及以上的技能装得下，但后端门禁会把它挡在工具环外")
        self.only_registerable.stateChanged.connect(self._filter)
        tools.addWidget(self.only_registerable)
        root.addLayout(tools)

        self.tabs = QTabWidget()
        self.market_table = self._build_table(self.MARKET_COLUMNS)
        header = self.market_table.horizontalHeader()
        for column in (0, 1, 3, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        # 「层级(T1..T6)」按内容自适应会和「工具数」贴在一起，按字体量出的宽度给足留白。
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.market_table.setColumnWidth(
            4, header.fontMetrics().horizontalAdvance(self.MARKET_COLUMNS[4]) + 22)
        self.market_table.itemSelectionChanged.connect(self._sync_buttons)
        self.market_table.doubleClicked.connect(lambda *_: self._install())
        self.tabs.addTab(self.market_table, "市场清单")

        self.installed_table = self._build_table(self.INSTALLED_COLUMNS)
        header = self.installed_table.horizontalHeader()
        for column in (0, 1, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.installed_table.itemSelectionChanged.connect(self._sync_buttons)
        self.tabs.addTab(self.installed_table, "已安装")
        root.addWidget(self.tabs, 1)

        actions = QHBoxLayout()
        self.chk_enable = QCheckBox("安装后注册到工具环")
        self.chk_enable.setChecked(True)
        self.chk_enable.setToolTip("等同安装请求里的 enable=true；取消勾选只落地文件，不注册")
        actions.addWidget(self.chk_enable)
        self.chk_force = QCheckBox("覆盖安装")
        self.chk_force.setToolTip("后端返回「已安装」时勾上，等同安装请求里的 force=true")
        actions.addWidget(self.chk_force)
        actions.addStretch(1)
        self.btn_install = QPushButton("安装")
        self.btn_install.setEnabled(False)
        self.btn_install.setToolTip("把选中的市场条目装到本机技能目录")
        self.btn_install.clicked.connect(self._install)
        actions.addWidget(self.btn_install)
        self.btn_uninstall = QPushButton("卸载")
        self.btn_uninstall.setProperty("class", "secondary")
        self.btn_uninstall.setEnabled(False)
        self.btn_uninstall.setToolTip("删除本机技能目录里的这个技能包（会二次确认）")
        self.btn_uninstall.clicked.connect(self._uninstall)
        actions.addWidget(self.btn_uninstall)
        self.btn_rescan = QPushButton("重建清单")
        self.btn_rescan.setProperty("class", "secondary")
        self.btn_rescan.setToolTip("让后端按磁盘上的技能包重建运行时清单")
        self.btn_rescan.clicked.connect(self._rescan)
        actions.addWidget(self.btn_rescan)
        root.addLayout(actions)

        self.summary = QLabel("")     # 状态栏：/api/skills/summary 的计数与市场目录
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.summary)

        foot = QHBoxLayout()
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        foot.addWidget(self.notice, 1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("class", "secondary")
        self.btn_close.clicked.connect(self.accept)
        foot.addWidget(self.btn_close)
        root.addLayout(foot)
        QTimer.singleShot(0, self.refresh_all)

    def _build_table(self, columns) -> QTableWidget:
        table = QTableWidget(0, len(columns), self)
        table.setHorizontalHeaderLabels(columns)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        return table

    # ── 取数 ────────────────────────────────────────────────────────────
    @staticmethod
    def _ok(data) -> bool:
        """这些端点用 ok；连接失败时 api_post 回的是 success/message。"""
        if not isinstance(data, dict):
            return False
        if "ok" in data:
            return bool(data.get("ok"))
        return bool(data.get("success"))

    @staticmethod
    def _failure_text(data) -> str:
        return skill_error_text(data)

    def _import_package(self):
        """统一导入窗口：预检 → 确认 → 安装；真装上了才刷新清单。"""
        if open_import_dialog(self):
            self.refresh_all()

    def refresh_all(self, *_args):
        """市场 + 已装 + 汇总一起刷；安装/卸载/重建后都回到这里，不留半新半旧。"""
        self._load_market()
        self._load_installed()
        self._load_summary()

    def _load_market(self):
        self.btn_refresh.setEnabled(False)
        self.status.setText("正在读取技能市场…")
        run_api_async(self, "skills/market", {}, timeout=45,
                      callback=self._market_loaded, busy="正在读取技能市场…")

    def _market_loaded(self, data):
        self.btn_refresh.setEnabled(True)
        if not self._ok(data):
            self._market = []
            self._filter()
            self.notice.clear()          # busy 文案别留在失败状态里
            self.status.setText("技能市场读取失败：" + self._failure_text(data))
            return
        self._market = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        self._filter()
        broken = skill_count(data.get("broken"))
        filled = skill_count(data.get("tierFilled"))
        if self._market and not filled and not self.notice.text():
            self.notice.setText("层级还没算过：点『重建清单』约 10–15 秒，之后才看得出哪些能注册")
            self.notice.setToolTip("清单快照（data/qq-only/skill-tools.json）没建过时，"
                                   "市场列表给不出层级与工具数；重建一次后 24 小时内复用。")
        elif broken:
            self.notice.setText("市场里有 %d 个包读不出清单（缺 skill.json/plugin.json 或已损坏）"
                                % broken)
            self.notice.setToolTip(str(data.get("broken") or ""))

    def _load_installed(self):
        run_api_async(self, "skills/installed", {}, timeout=30,
                      callback=self._installed_loaded)

    def _installed_loaded(self, data):
        if not self._ok(data):
            self._installed = []
            self._fill_installed([])
            self.tabs.setTabText(1, "已安装（读取失败）")
            return
        self._installed = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        self._fill_installed(self._installed)
        self.tabs.setTabText(1, "已安装（%d）" % len(self._installed))

    def _load_summary(self):
        run_api_async(self, "skills/summary", {}, timeout=20, callback=self._summary_loaded)

    def _summary_loaded(self, data):
        if not self._ok(data):
            self.summary.setText("技能汇总读取失败：" + self._failure_text(data))
            return
        self._summary = data

        def _num(value):
            return "—" if value is None else str(value)

        text = ("已装 %s · 市场 %s · 可注册 %s · 被挡 %s · 目录 %s"
                % (_num(data.get("installed")), _num(data.get("marketCount")),
                   _num(data.get("eligible")), _num(data.get("blocked")),
                   str(data.get("marketDir") or "未初始化（后端还没给出市场目录）")))
        error = str(data.get("error") or "").strip()
        if error:
            text += " · 扫描告警：" + error
        self.summary.setText(text)
        self.summary.setToolTip(text)

    # ── 渲染 ────────────────────────────────────────────────────────────
    def _filter(self, *_args):
        needle = self.search.text().strip().lower()
        only = self.only_registerable.isChecked()
        rows = []
        for item in self._market:
            if only and not skill_is_registerable(item.get("tier")):
                continue
            if needle:
                haystack = " ".join([str(item.get("id") or ""),
                                     str(item.get("name") or ""),
                                     str(item.get("description") or ""),
                                     skill_tier(item.get("tier"))]).lower()
                if needle not in haystack:
                    continue
            rows.append(item)
        self._fill_market(rows)
        self.status.setText("显示 %d / %d 个技能包" % (len(rows), len(self._market)))
        self._sync_buttons()

    def _fill_market(self, rows):
        self.market_table.setUpdatesEnabled(False)
        try:
            self.market_table.setRowCount(0)
            for item in rows:
                self._add_market_row(item)
        finally:
            self.market_table.setUpdatesEnabled(True)

    def _add_market_row(self, item: dict):
        row = self.market_table.rowCount()
        self.market_table.insertRow(row)
        tier = skill_tier(item.get("tier"))
        source = str(item.get("file") or item.get("zipPath") or "")
        installed = QTableWidgetItem("已装" if item.get("installed") else "—")
        installed.setData(Qt.ItemDataRole.UserRole, str(item.get("id") or ""))
        installed.setData(_SKILL_FILE_ROLE, source)
        ident = QTableWidgetItem(str(item.get("id") or ""))
        size = item.get("bytes")
        ident.setToolTip((source or "后端没返回包路径")
                         + (("（%s 字节）" % size) if size else ""))
        name = QTableWidgetItem(str(item.get("name") or ""))
        version = QTableWidgetItem(str(item.get("version") or ""))
        tier_item = QTableWidgetItem(tier or "—")
        tier_item.setForeground(QColor(skill_tier_color(tier)))
        tier_item.setToolTip(self._tier_tip(tier))
        tool_names = [str(one) for one in (item.get("tools") or [])] \
            if isinstance(item.get("tools"), (list, tuple)) else []
        tools_item = QTableWidgetItem(str(skill_count(item.get("tools"))))
        tools_item.setToolTip("、".join(tool_names) if tool_names
                              else "工具数（注册到工具环后才会枚举给模型）")
        reason = str(item.get("description") or item.get("reason") or "").strip()
        reason_item = QTableWidgetItem(reason or self._tier_tip(tier))
        reason_item.setToolTip(reason_item.text())
        for column, widget in enumerate(
                [installed, ident, name, version, tier_item, tools_item, reason_item]):
            self.market_table.setItem(row, column, widget)

    def _fill_installed(self, rows):
        self.installed_table.setUpdatesEnabled(False)
        try:
            self.installed_table.setRowCount(0)
            for item in rows:
                self._add_installed_row(item)
        finally:
            self.installed_table.setUpdatesEnabled(True)

    def _add_installed_row(self, item: dict):
        row = self.installed_table.rowCount()
        self.installed_table.insertRow(row)
        tier = skill_tier(item.get("tier"))
        enabled = QTableWidgetItem("是" if item.get("enabled") else "否")
        enabled.setForeground(QColor("#1E8E3E" if item.get("enabled") else MUTED))
        ident = QTableWidgetItem(str(item.get("id") or ""))
        name = QTableWidgetItem(str(item.get("name") or ""))
        version = QTableWidgetItem(str(item.get("version") or ""))
        tier_item = QTableWidgetItem(tier or "—")
        tier_item.setForeground(QColor(skill_tier_color(tier)))
        tier_item.setToolTip(self._tier_tip(tier))
        tools_item = QTableWidgetItem(str(skill_count(item.get("tools"))))
        state = QTableWidgetItem(self._state_text(item))
        state.setToolTip(state.text())
        for column, widget in enumerate(
                [enabled, ident, name, version, tier_item, tools_item, state]):
            self.installed_table.setItem(row, column, widget)

    @staticmethod
    def _tier_tip(tier: str) -> str:
        return {
            "T1": "T1：核心技能包，可直接注册到工具环",
            "T2": "T2：允许注册，工具能力受限",
            "T3": "T3：按门禁注册，需要显式启用",
            "T4": "T4：可以落地，但不会注册到工具环（门禁停用）",
            "T5": "T5：可以落地，但不会注册到工具环（门禁停用）",
            "T6": "T6：禁止注册，只会留在磁盘上",
        }.get(skill_tier(tier), "层级未知：注册与否由后端门禁决定")

    @staticmethod
    def _state_text(item: dict) -> str:
        parts = ["已注册" if item.get("known") else "未注册",
                 "已启用" if item.get("enabled") else "已停用"]
        blocked = item.get("blocked")
        if blocked:
            reason = blocked.strip() if isinstance(blocked, str) else ""
            parts.insert(0, "门禁阻挡（%s）" % reason if reason else "门禁阻挡")
        return " · ".join(parts)

    # ── 选中 ────────────────────────────────────────────────────────────
    @staticmethod
    def _selected_row(table) -> int:
        row = table.currentRow()
        if row >= 0:
            return row
        selection = table.selectionModel()
        rows = selection.selectedRows() if selection is not None else []
        return rows[0].row() if rows else -1

    def _selected_market_item(self):
        """选中行的 id/file 存在第 0 列的 user data 里，过滤后也不用回查原表。"""
        row = self._selected_row(self.market_table)
        if row < 0:
            return None
        cell = self.market_table.item(row, 0)
        if cell is None:
            return None
        ident = str(cell.data(Qt.ItemDataRole.UserRole) or "").strip()
        source = str(cell.data(_SKILL_FILE_ROLE) or "").strip()
        if not ident and not source:
            return None
        tier_cell = self.market_table.item(row, 4)
        return {"id": ident, "file": source,
                "tier": skill_tier(tier_cell.text() if tier_cell is not None else "")}

    def _selected_id(self) -> str:
        """卸载认得两张表：在「市场清单」里选也行，在「已安装」里选也行。"""
        if self.tabs.currentIndex() == 1:
            row = self._selected_row(self.installed_table)
            if row < 0:
                return ""
            cell = self.installed_table.item(row, 1)
            return str(cell.text() if cell is not None else "").strip()
        item = self._selected_market_item()
        return str(item.get("id") or "") if item else ""

    def _sync_buttons(self, *_args):
        self.btn_install.setEnabled(self._selected_market_item() is not None)
        self.btn_uninstall.setEnabled(bool(self._selected_id()))

    # ── 安装 / 卸载 / 重建 ───────────────────────────────────────────────
    def _install(self):
        item = self._selected_market_item()
        if item is None:
            self.notice.setText("先在「市场清单」里选一个技能包")
            return
        if not item.get("file"):
            self.notice.setText("后端没有返回这个技能包的落盘路径，装不了")
            return
        ident = item.get("id") or item.get("file")
        tier = item.get("tier")
        # T4/T6 照样让装：这里只提示，禁用与否由后端门禁决定。
        warn = ""
        if not skill_is_registerable(tier):
            warn = "%s 属于受限层级，装完也不会注册到工具环。" % (tier or "这个包")
        self.btn_install.setEnabled(False)
        self.notice.setText((warn + " 正在安装 %s…" % ident).strip())
        run_api_async(self, "skills/install",
                      {"file": item.get("file"),
                       "force": bool(self.chk_force.isChecked()),
                       "enable": bool(self.chk_enable.isChecked())},
                      timeout=90, callback=self._install_finished, busy="正在安装技能包…")

    @staticmethod
    def _install_summary(data) -> str:
        """安装成功后要显示的话：后端 note 原样优先，缺了才自己拼一句。"""
        note = str(data.get("note") or "").strip()
        if note:
            return note
        text = "已安装 %s（%d 个文件）" % (str(data.get("id") or ""), skill_count(data.get("files")))
        if not data.get("enabled"):
            text += "；当前未注册到工具环"
        return text

    def _install_finished(self, data):
        self.btn_install.setEnabled(True)
        if not self._ok(data):
            self.notice.setText("安装失败：" + self._failure_text(data))
            self._sync_buttons()
            return
        self.notice.setText(self._install_summary(data))
        self.refresh_all()

    def _uninstall(self):
        ident = self._selected_id()
        if not ident:
            self.notice.setText("先选中要卸载的技能（「市场清单」或「已安装」里选都行）")
            return
        if not ask_confirm(self, "卸载技能", "确定要卸载 %s 吗？" % ident,
                           detail="会删除该技能包在本机技能目录里的文件，并重建运行时清单。",
                           ok_text="卸载", danger=True):
            return
        self.btn_uninstall.setEnabled(False)
        self.notice.setText("正在卸载 %s…" % ident)
        run_api_async(self, "skills/uninstall", {"id": ident}, timeout=30,
                      callback=lambda data: self._uninstalled(ident, data))

    def _uninstalled(self, ident: str, data):
        if not self._ok(data):
            self.notice.setText("卸载失败：" + self._failure_text(data))
            self._sync_buttons()
            self.refresh_all()
            return
        self.notice.setText("已卸载 %s（删除 %d 项）"
                            % (str(data.get("id") or ident), skill_count(data.get("removed"))))
        self.refresh_all()

    def _rescan(self):
        self.btn_rescan.setEnabled(False)
        self.notice.setText("正在重建运行时技能清单…")
        run_api_async(self, "skills/rescan", {}, timeout=60,
                      callback=self._rescanned, busy="正在重建技能清单…")

    def _rescanned(self, data):
        self.btn_rescan.setEnabled(True)
        if not self._ok(data):
            self.notice.setText("重建清单失败：" + self._failure_text(data))
            return
        self.notice.setText("已重建技能清单：%d 个技能，耗时 %s ms"
                            % (skill_count(data.get("skills")), data.get("elapsedMs") or 0))
        self.refresh_all()


class PluginPacksDialog(QDialog):
    """本地插件包独立入口：列出 plugin_packs.scan 的结果并安装到本地插件目录。"""

    def __init__(self, parent=None, api_get=None, api_post=None):
        super().__init__(parent)
        self.setWindowTitle("本地插件包")
        self.setObjectName("plugin_packs_dialog")
        self.resize(720, 520)
        self.api_get = api_get or globals().get("api_get")
        self.api_post = api_post or globals().get("api_post")
        self.directory = ""
        self.packs = []
        self.requests = PanelRequests(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        title = QLabel("本地插件包")
        title.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {TEXT};")
        root.addWidget(title)
        hint = QLabel("这里列出插件包目录里的 .zip / 文件夹；安装只复制到本地插件目录，"
                      "不会上传，也不会改动内置插件。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(hint)
        self.pack_list = QListWidget()
        self.pack_list.setObjectName("plugin_packs_list")
        self.pack_list.setMinimumHeight(240)
        self.pack_list.currentItemChanged.connect(self._sync_detail)
        root.addWidget(self.pack_list, 1)
        self.detail = QLabel("选中一个插件包查看说明。")
        self.detail.setObjectName("plugin_packs_detail")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.detail)
        options = QHBoxLayout()
        self.overwrite = QCheckBox("覆盖已安装的同名插件包")
        self.overwrite.setObjectName("plugin_packs_overwrite")
        options.addWidget(self.overwrite)
        self.enable = QCheckBox("安装后自动启用")
        self.enable.setObjectName("plugin_packs_enable")
        self.enable.setChecked(True)
        options.addWidget(self.enable)
        options.addStretch(1)
        root.addLayout(options)
        buttons = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新列表")
        self.btn_refresh.setObjectName("plugin_packs_refresh")
        self.btn_refresh.clicked.connect(self.refresh)
        buttons.addWidget(self.btn_refresh)
        self.btn_install = QPushButton("安装到本地插件目录")
        self.btn_install.setObjectName("plugin_packs_install")
        self.btn_install.clicked.connect(self.install_selected)
        buttons.addWidget(self.btn_install)
        self.btn_open = QPushButton("打开插件包目录")
        self.btn_open.setObjectName("plugin_packs_open")
        self.btn_open.clicked.connect(self.open_directory)
        buttons.addWidget(self.btn_open)
        buttons.addStretch(1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setObjectName("plugin_packs_close")
        self.btn_close.clicked.connect(self.reject)
        buttons.addWidget(self.btn_close)
        root.addLayout(buttons)
        self.notice = QLabel("正在读取本地插件包…")
        self.notice.setObjectName("plugin_packs_notice")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.notice)
        self._sync_buttons()
        QTimer.singleShot(0, self.refresh)

    def _sync_buttons(self) -> None:
        busy = self.requests.busy
        self.btn_refresh.setEnabled(not busy)
        self.btn_open.setEnabled(not busy and bool(self.directory))
        item = self.pack_list.currentItem()
        self.btn_install.setEnabled(not busy and item is not None)

    def refresh(self) -> bool:
        if self.requests.busy or self.api_get is None:
            return False
        self.notice.setText("正在读取本地插件包…")
        self._sync_buttons()
        return self.requests.start(
            lambda: self.api_get("plugins/packs", timeout=20),
            self._loaded, self._failed)

    def _loaded(self, payload) -> None:
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data or data.get("success") is False:
            self.notice.setText("读取插件包失败：%s"
                                % (data.get("message") or data.get("detail") or "后台没有响应"))
            self._sync_buttons()
            return
        self.directory = str(data.get("directory") or "")
        packs = data.get("packs") if isinstance(data.get("packs"), list) else []
        self.packs = [dict(pack) for pack in packs if isinstance(pack, dict)]
        self.pack_list.clear()
        for pack in self.packs:
            name = str(pack.get("name") or pack.get("id") or "未命名插件包")
            version = str(pack.get("version") or "—")
            status = "不可用" if pack.get("ok") is False else (
                "已安装" if pack.get("installed") else "可安装")
            item = QListWidgetItem("%s  v%s  · %s" % (name, version, status))
            item.setData(Qt.ItemDataRole.UserRole, str(pack.get("id") or ""))
            description = str(pack.get("description") or "").strip()
            item.setToolTip(description or "没有说明")
            self.pack_list.addItem(item)
        issues = data.get("issues") or []
        if isinstance(issues, dict):
            issues = list(issues.values())
        self.notice.setText("已找到 %d 个本地插件包%s"
                            % (len(self.packs), ("；有 %d 个问题" % len(issues)) if issues else ""))
        self._sync_buttons()

    def _failed(self, exc) -> None:
        self.notice.setText("读取插件包失败：%s" % (exc or "后台没有响应"))
        self._sync_buttons()

    def _selected_pack(self):
        item = self.pack_list.currentItem()
        if item is None:
            return None
        ident = str(item.data(Qt.ItemDataRole.UserRole) or "")
        for pack in self.packs:
            if str(pack.get("id") or "") == ident:
                return pack
        return None

    def _sync_detail(self, current=None, previous=None) -> None:
        pack = self._selected_pack()
        if pack is None:
            self.detail.setText("选中一个插件包查看说明。")
        else:
            self.detail.setText("ID：%s\n名称：%s\n版本：%s\n说明：%s"
                                % (pack.get("id") or "—", pack.get("name") or "—",
                                   pack.get("version") or "—",
                                   pack.get("description") or "没有说明"))
        self._sync_buttons()

    def install_selected(self) -> bool:
        if self.requests.busy:
            return False
        pack = self._selected_pack()
        if pack is None:
            self.notice.setText("请先在列表里选中一个插件包。")
            return False
        ident = str(pack.get("id") or "").strip()
        if not ident:
            self.notice.setText("这个插件包没有 id，无法安装。")
            return False
        if pack.get("installed") and not self.overwrite.isChecked():
            self.notice.setText("%s 已安装；需要覆盖时请勾选“覆盖已安装的同名插件包”。"
                                % (pack.get("name") or ident))
            return False
        body = {"id": ident, "confirmed": True,
                "overwrite": bool(self.overwrite.isChecked()),
                "enable": bool(self.enable.isChecked())}
        self.notice.setText("正在安装 %s…" % (pack.get("name") or ident))
        self._sync_buttons()
        return self.requests.start(
            lambda: self.api_post("plugins/packs/install", body, timeout=120),
            self._installed, self._failed)

    def _installed(self, payload) -> None:
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data or data.get("success") is False:
            self.notice.setText("安装失败：%s"
                                % (data.get("message") or data.get("detail") or "未知原因"))
            self._sync_buttons()
            return
        enabled = data.get("enabled") if isinstance(data.get("enabled"), list) else []
        if data.get("message"):
            text = str(data.get("message"))
        else:
            text = "已安装到本地插件目录：%s" % (
                data.get("target") or data.get("id") or "完成")
            if enabled:
                text += "；已启用：" + "、".join(str(item) for item in enabled)
        self.notice.setText(text)
        QTimer.singleShot(0, self.refresh)

    def open_directory(self) -> bool:
        path = self.directory.strip()
        if not path:
            self.notice.setText("还没有插件包目录；先刷新列表。")
            return False
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception as exc:           # noqa: BLE001 - 打开目录失败不许抛
            self.notice.setText("打开插件包目录失败：%s" % exc)
            return False
        self.notice.setText("已打开插件包目录：%s" % path)
        return True

    def closeEvent(self, event):
        if hasattr(self, "requests"):
            self.requests.close()
        super().closeEvent(event)


class PluginCoreDialog(QDialog):
    """目录式 plugin_core 插件：查看已加载插件，从本地目录或 zip 安装。"""

    def __init__(self, parent=None, api_get=None, api_post=None):
        super().__init__(parent)
        self.setWindowTitle("目录式插件内核")
        self.setObjectName("plugin_core_dialog")
        self.resize(760, 560)
        self.api_get = api_get or globals().get("api_get")
        self.api_post = api_post or globals().get("api_post")
        self.requests = PanelRequests(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        title = QLabel("目录式插件内核")
        title.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {TEXT};")
        root.addWidget(title)
        hint = QLabel(
            "这里管理 plugin_core 目录式插件（plugins/<id>/plugin.json）。"
            "安装只复制到当前账号的数据目录，不上传、不改内置插件。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(hint)
        self.summary = QLabel("正在读取…")
        self.summary.setObjectName("plugin_core_summary")
        self.summary.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.summary)
        self.plugin_list = QListWidget()
        self.plugin_list.setObjectName("plugin_core_list")
        self.plugin_list.setMinimumHeight(220)
        self.plugin_list.currentItemChanged.connect(lambda *_: self._sync_buttons())
        root.addWidget(self.plugin_list, 1)
        path_row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setObjectName("plugin_core_path")
        self.path.setPlaceholderText("插件目录或 .zip 的完整路径")
        path_row.addWidget(self.path, 1)
        self.btn_dir = QPushButton("选择目录…")
        self.btn_dir.setProperty("class", "secondary")
        self.btn_dir.clicked.connect(self._pick_dir)
        path_row.addWidget(self.btn_dir)
        self.btn_zip = QPushButton("选择 zip…")
        self.btn_zip.setProperty("class", "secondary")
        self.btn_zip.clicked.connect(self._pick_zip)
        path_row.addWidget(self.btn_zip)
        root.addLayout(path_row)
        options = QHBoxLayout()
        self.overwrite = QCheckBox("覆盖已安装的同名插件")
        self.overwrite.setObjectName("plugin_core_overwrite")
        options.addWidget(self.overwrite)
        options.addStretch(1)
        self.btn_install = QPushButton("安装")
        self.btn_install.setObjectName("plugin_core_install")
        self.btn_install.clicked.connect(self.install_plugin)
        options.addWidget(self.btn_install)
        self.btn_uninstall = QPushButton("移除选中")
        self.btn_uninstall.setObjectName("plugin_core_uninstall")
        self.btn_uninstall.setProperty("class", "secondary")
        self.btn_uninstall.clicked.connect(self.uninstall_selected)
        options.addWidget(self.btn_uninstall)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setProperty("class", "secondary")
        self.btn_refresh.clicked.connect(self.refresh)
        options.addWidget(self.btn_refresh)
        root.addLayout(options)
        self.notice = QLabel("")
        self.notice.setObjectName("plugin_core_notice")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.notice)
        self._sync_buttons()
        QTimer.singleShot(0, self.refresh)

    def _sync_buttons(self):
        selected = self.plugin_list.currentItem() is not None
        busy = bool(getattr(self.requests, "busy", False))
        self.btn_install.setEnabled(not busy)
        self.btn_uninstall.setEnabled(selected and not busy)
        self.btn_refresh.setEnabled(not busy)

    def refresh(self) -> None:
        if self.requests.busy:
            return
        self.summary.setText("正在读取…")
        self.requests.start(
            lambda: self.api_get("plugin-core/status", timeout=5),
            self._loaded, self._failed)

    def _loaded(self, payload) -> None:
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data or data.get("success") is False:
            self.summary.setText("读取失败")
            self.notice.setText(str(data.get("message") or data.get("detail") or "后台未响应"))
            self._sync_buttons()
            return
        plugins = data.get("plugins") if isinstance(data.get("plugins"), list) else []
        self.plugin_list.clear()
        for item in plugins:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "")
            name = str(item.get("name") or ident)
            version = str(item.get("version") or "")
            row = QListWidgetItem("%s  (%s)  %s" % (name, ident, version))
            row.setData(Qt.ItemDataRole.UserRole, ident)
            self.plugin_list.addItem(row)
        if data.get("available"):
            self.summary.setText("内核已启动，已加载 %d 个插件。" % len(plugins))
        else:
            self.summary.setText("内核未启动：目录里还没有可加载的插件。")
        self.notice.setText(str(data.get("lastError") or ""))
        self._sync_buttons()

    def _failed(self, exc) -> None:
        self.summary.setText("读取失败")
        self.notice.setText("读取插件内核状态失败：%s" % exc)
        self._sync_buttons()

    def _pick_dir(self) -> None:
        path = file_dialogs.choose_dir(self, "选择插件目录")
        if path:
            self.path.setText(path)

    def _pick_zip(self) -> None:
        path, _ = file_dialogs.open_file(
            self, "选择插件 zip", "", "插件包 (*.zip)")
        if path:
            self.path.setText(path)

    def _selected_id(self) -> str:
        item = self.plugin_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def install_plugin(self) -> bool:
        path = self.path.text().strip()
        if not path:
            self.notice.setText("请先选择插件目录或 zip。")
            return False
        body = {"path": path, "overwrite": self.overwrite.isChecked()}
        self.notice.setText("正在安装…")
        self._sync_buttons()
        return self.requests.start(
            lambda: self.api_post("plugin-core/install", body, timeout=120),
            self._installed, self._failed)

    def _installed(self, payload) -> None:
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data or data.get("success") is False:
            self.notice.setText("安装失败：%s"
                                % (data.get("message") or data.get("detail") or "未知原因"))
            self._sync_buttons()
            return
        plugin = data.get("plugin") if isinstance(data.get("plugin"), dict) else {}
        ident = str(plugin.get("id") or "")
        self.notice.setText("已安装%s。" % ((" " + ident) if ident else "插件"))
        QTimer.singleShot(0, self.refresh)

    def uninstall_selected(self) -> bool:
        ident = self._selected_id()
        if not ident:
            self.notice.setText("请先选中一个插件。")
            return False
        answer = QMessageBox.question(
            self, "移除插件", "确定移除插件 %s？" % ident,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.notice.setText("正在移除…")
        self._sync_buttons()
        return self.requests.start(
            lambda: self.api_post("plugin-core/uninstall", {"id": ident}, timeout=30),
            self._uninstalled, self._failed)

    def _uninstalled(self, payload) -> None:
        data = dict(payload) if isinstance(payload, dict) else {}
        if not data or data.get("success") is False:
            self.notice.setText("移除失败：%s"
                                % (data.get("message") or data.get("detail") or "未知原因"))
            self._sync_buttons()
            return
        self.notice.setText("已移除插件 %s。" % str(data.get("id") or ""))
        QTimer.singleShot(0, self.refresh)

    def closeEvent(self, event):
        if hasattr(self, "requests"):
            self.requests.close()
        super().closeEvent(event)


class PluginTab(QWidget):
    """Searchable plugin catalogue with filters and confirmed toggle state."""
    CATEGORIES = {
        "rhythm": "回复节奏", "message": "消息处理", "social": "互动交流",
        "personality": "个性表达", "intelligence": "智能判断",
        "media": "图片与媒体", "style": "回复样式", "utility": "实用工具",
    }

    def __init__(self):
        super().__init__()
        self._rows = {}
        self._plugins = []
        self._loaded = False
        # 上次成功渲染的数据指纹：一样就不再重建行控件（重建 63 行约 75 毫秒）
        self._signature = None
        # 内核插件（plugin_core）数量文案，由 refresh 读一次，_filter_rows 拼进 summary
        self._core_summary = ""
        # 用户自己装的插件（只有这些允许删除；内置插件永远不在这个集合里）
        self._custom_ids = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        heading = QHBoxLayout()
        title = QLabel("插件中心")
        title.setStyleSheet(f"font-size: 21px; font-weight: bold; color: {TEXT};")
        heading.addWidget(title)
        heading.addStretch()
        self.summary = QLabel("等待加载")
        self.summary.setStyleSheet(f"color: {MUTED};")
        heading.addWidget(self.summary)
        self.btn_stickers=QPushButton('管理表情库')
        self.btn_stickers.clicked.connect(lambda:StickerLibraryDialog(ROOT,api_get,api_post,api_delete,self).exec())
        heading.addWidget(self.btn_stickers)
        self.btn_market = QPushButton("插件市场")
        self.btn_market.setProperty("class", "secondary")
        self.btn_market.setToolTip("浏览 QQ Agent 市集，下载 Node 扩展包到本地扩展目录")
        self.btn_market.clicked.connect(self._open_market)
        heading.addWidget(self.btn_market)
        self.btn_skill_market = QPushButton("技能市场")
        self.btn_skill_market.setObjectName("btn_skill_market")
        self.btn_skill_market.setProperty("class", "secondary")
        self.btn_skill_market.setToolTip("安装/卸载 Agent 技能包；T1/T2/T3 才会注册到工具环")
        self.btn_skill_market.clicked.connect(self._open_skill_market)
        heading.addWidget(self.btn_skill_market)
        self.btn_extensions = QPushButton("扩展管理")
        self.btn_extensions.setProperty("class", "secondary")
        self.btn_extensions.setToolTip("逐项启用/停用 QQ Agent 扩展（默认全停，启用后能力桥才会调用）")
        self.btn_extensions.clicked.connect(self._open_extensions)
        heading.addWidget(self.btn_extensions)
        self.btn_add = decorate_button(QPushButton("添加插件"), "btn_new")
        self.btn_add.setToolTip("从网址、GitHub 仓库或本地文件安装插件")
        self.btn_add.clicked.connect(self._add_plugin)
        heading.addWidget(self.btn_add)
        self.btn_pack = QPushButton("导入功能包")
        self.btn_pack.setProperty("class", "secondary")
        self.btn_pack.setToolTip("导入一个 zip：插件 + Agent 技能 + 长期偏好")
        self.btn_pack.clicked.connect(self._import_pack)
        heading.addWidget(self.btn_pack)
        self.btn_import_any = QPushButton("导入任意包…")
        self.btn_import_any.setProperty("class", "secondary")
        self.btn_import_any.setToolTip(
            "统一导入窗口：Python 插件 / JS 扩展 / Agent 技能 / 功能包，先预检再安装")
        self.btn_import_any.clicked.connect(self._import_any_pack)
        heading.addWidget(self.btn_import_any)
        self.btn_local_packs = QPushButton("本地插件包")
        self.btn_local_packs.setObjectName("btn_plugin_packs")
        self.btn_local_packs.setProperty("class", "secondary")
        self.btn_local_packs.setToolTip(
            "打开本地插件包目录：查看、安装 .zip / 文件夹插件包（不影响上面的添加/导入）")
        self.btn_local_packs.clicked.connect(
            lambda: PluginPacksDialog(self, api_get, api_post).exec())
        heading.addWidget(self.btn_local_packs)
        self.btn_plugin_core = QPushButton("内核插件")
        self.btn_plugin_core.setObjectName("btn_plugin_core")
        self.btn_plugin_core.setProperty("class", "secondary")
        self.btn_plugin_core.setToolTip(
            "目录式 plugin_core 插件：查看已加载插件，从本地目录或 zip 安装")
        self.btn_plugin_core.clicked.connect(
            lambda: PluginCoreDialog(self, api_get, api_post).exec())
        heading.addWidget(self.btn_plugin_core)
        self.btn_reload = QPushButton("刷新列表")
        self.btn_reload.setProperty("class", "secondary")
        self.btn_reload.clicked.connect(self.refresh)
        heading.addWidget(self.btn_reload)
        root.addLayout(heading)

        hint = QLabel("按功能查找插件，开关立即生效；详细参数可在“配置”中调整。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED};")
        root.addWidget(hint)

        wakeup_row=QHBoxLayout()
        self.btn_wakeup=QPushButton('唤醒关键词设置')
        self.btn_wakeup.clicked.connect(self._open_wakeup)
        wakeup_row.addWidget(self.btn_wakeup)
        wakeup_help=QLabel('设置群友用什么称呼叫醒机器人；官方接入的群消息仍需 @。')
        wakeup_help.setWordWrap(True);wakeup_row.addWidget(wakeup_help,1)
        root.addLayout(wakeup_row)

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索插件名称、功能或标识…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索插件")
        filters.addWidget(self.search, 1)
        self.category_filter = QComboBox()
        self.category_filter.addItem("全部分类", "")
        self.category_filter.setAccessibleName("插件分类")
        self.category_filter.setMinimumWidth(125)
        filters.addWidget(self.category_filter)
        self.state_filter = QComboBox()
        for label, value in (("全部状态", "all"), ("已启用", "enabled"), ("已停用", "disabled")):
            self.state_filter.addItem(label, value)
        for label,value in (("接入受限","unavailable"),("需要配置或图片","needs_setup"),("部分可用或外部服务","limited")):
            self.state_filter.addItem(label,value)
        self.state_filter.setAccessibleName("插件启用状态")
        self.state_filter.setMinimumWidth(110)
        filters.addWidget(self.state_filter)
        self.btn_clear = QPushButton("重置筛选")
        self.btn_clear.setProperty("class", "secondary")
        self.btn_clear.clicked.connect(self._clear_filters)
        filters.addWidget(self.btn_clear)
        root.addLayout(filters)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.holder = QWidget()
        self.rows_layout = QVBoxLayout(self.holder)
        self.rows_layout.setContentsMargins(0, 0, 8, 0)
        self.rows_layout.setSpacing(10)
        self.scroll.setWidget(self.holder)
        root.addWidget(self.scroll, 1)
        self.empty = QLabel("点击刷新加载插件列表")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color: {MUTED}; padding: 36px; font-size: 14px;")
        root.addWidget(self.empty)
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.notice)
        self.search.textChanged.connect(self._filter_rows)
        self.category_filter.currentIndexChanged.connect(self._filter_rows)
        self.state_filter.currentIndexChanged.connect(self._filter_rows)

    @safe_slot(label="插件市场")
    def _open_market(self):
        dialog = MarketDialog(self)
        dialog._load()
        dialog.exec()

    @safe_slot(label="技能市场")
    def _open_skill_market(self):
        SkillMarketDialog(self).exec()

    @safe_slot(label="扩展管理")
    def _open_extensions(self):
        ExtensionManagerDialog(self).exec()

    @safe_slot(label="导入功能包")
    def _import_pack(self):
        dialog = FeaturePackDialog(self)
        dialog.exec()
        if dialog.installed:
            self.refresh()

    @safe_slot(label="导入任意包")
    def _import_any_pack(self):
        """统一导入窗口：装上了才刷新插件清单（上面「导入功能包」那条流程一行没动）。"""
        if open_import_dialog(self):
            self.refresh()

    @safe_slot(label="插件")
    def refresh(self):
        plugins = api_get("plugins")
        if isinstance(plugins,dict) and plugins.get('success') is False:
            self.notice.setText(plugins.get('message') or '后台未连接')
            return
        if isinstance(plugins, dict):
            if isinstance(plugins.get("plugins"), list):
                plugins = plugins["plugins"]
            elif plugins and all(isinstance(p, dict) and p.get("id") for p in plugins.values()):
                plugins = list(plugins.values())
            else:
                plugins = None
        if not isinstance(plugins, list):
            self.notice.setText("刷新失败：暂时无法连接后端，请稍后重试。已加载的列表保留。")
            return
        if not plugins:
            # 后台刚起来时偶尔会先返回空：照常显示 0 个，同时自动补一次。
            if not getattr(self, '_empty_retried', False):
                self._empty_retried = True
                self.notice.setText("插件列表为空：后台可能刚启动，正在自动重试…")
                QTimer.singleShot(2000, self.refresh)
            else:
                self._empty_retried = False
                self.notice.setText("插件列表仍为空：请点「刷新列表」重试；"
                                    "若一直为空，检查后台是否连的是另一个程序。")
        if any(not isinstance(p, dict) or not isinstance(p.get("id"), str) for p in plugins):
            self.notice.setText("插件数据不完整，已保留上次列表，请重试。")
            return
        # 自定义插件清单：拿不到就当作"没有自定义插件"（宁可不给删除按钮，也不误删）
        custom = api_get("plugins/custom")
        if isinstance(custom, dict) and isinstance(custom.get("plugins"), list):
            custom_ids = {str(item.get("id")) for item in custom["plugins"]
                          if isinstance(item, dict) and item.get("id")}
        else:
            custom_ids = set()
        # 内核插件是另一条链：只取数量写进 summary，失败也不影响上面的插件列表
        self._core_summary = self._core_summary_text()
        self.notice.setText("列表已更新。开关表示允许执行；“待配置 / 待数据 / 接入受限”会说明未生效的原因。")
        # 数据跟上次一模一样就直接返回：重建 63 行控件约 75 毫秒，每次切回插件页都重建会明显顿一下。
        signature = (json.dumps(plugins, sort_keys=True, ensure_ascii=False, default=str),
                     tuple(sorted(custom_ids)))
        if signature == self._signature and self._rows:
            # 行控件不重建，但 summary 要跟着内核插件数量更新
            self._filter_rows()
            return
        self._signature = signature
        self._plugins = plugins
        self._loaded = True
        self._custom_ids = custom_ids
        selected = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("全部分类", "")
        categories = sorted({str(p.get("category") or "utility") for p in plugins})
        for category in categories:
            self.category_filter.addItem(self.CATEGORIES.get(category, category), category)
        self.category_filter.setCurrentIndex(max(0, self.category_filter.findData(selected)))
        self.category_filter.blockSignals(False)
        while self.rows_layout.count():
            widget = self.rows_layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        self._rows.clear()
        for plugin in plugins:
            self.rows_layout.addWidget(self._make_row(plugin))
        self.rows_layout.addStretch(1)
        self._filter_rows()

    @staticmethod
    def _source_text(plugin: dict) -> str:
        """来源文案：目录包带 mod_id，其它用 source_label；老数据回退「内置」。"""
        label = str(plugin.get("source_label") or "内置")
        if plugin.get("plugin_source") == "mod" and plugin.get("source_detail"):
            return f"目录包 · {plugin['source_detail']}"
        return label

    def _core_summary_text(self) -> str:
        """内核插件数量文案。读不到就报未启动，绝不抛异常、不影响插件列表。"""
        try:
            status = api_get("plugin-core/status", timeout=3)
            plugins = status.get("plugins") if isinstance(status, dict) else None
            if not isinstance(status, dict) or status.get("success") is False or not status.get("available"):
                return " · 内核插件未启动"
            return f" · 内核插件 {len(plugins) if isinstance(plugins, list) else 0} 个（在「内核插件」里管理）"
        except Exception:
            return " · 内核插件未启动"

    def _clear_filters(self):
        self.search.clear()
        self.category_filter.setCurrentIndex(0)
        self.state_filter.setCurrentIndex(0)

    def _filter_rows(self, *_):
        query = self.search.text().strip().casefold()
        category = self.category_filter.currentData()
        state = self.state_filter.currentData()
        visible = 0
        for plugin in self._plugins:
            row = self._rows[plugin.get("id")]["frame"]
            cat = str(plugin.get("category") or "utility")
            searchable = " ".join(str(plugin.get(k) or "") for k in ("name", "description", "id", "status_note", "capability_label", "source_label", "source_detail"))
            searchable += " " + self.CATEGORIES.get(cat, cat)
            enabled = bool(plugin.get("enabled"))
            show = (not query or query in searchable.casefold()) and (not category or category == cat)
            show = show and (state == "all" or (state in {"enabled","disabled"} and enabled == (state == "enabled"))
                or (state == "unavailable" and plugin.get('available') is False)
                or (state == "needs_setup" and plugin.get('capability_status') in {'needs_config','needs_data'})
                or (state == "limited" and plugin.get('capability_status') in {'limited','external','unverified'}))
            row.setVisible(show)
            visible += int(show)
        total = len(self._plugins)
        enabled_count = sum(bool(p.get("enabled")) for p in self._plugins)
        self.summary.setText(f"共 {total} 个  ·  已启用 {enabled_count} 个  ·  显示 {visible} 个"
                             + self._core_summary)
        self.empty.setText("没有符合条件的插件，试试其他关键词或重置筛选。" if total else "暂无插件，请确认后端已加载插件。")
        self.empty.setVisible(visible == 0)
        self.scroll.setVisible(visible > 0)

    def _make_row(self, plugin: dict) -> QWidget:
        frame = QFrame()
        frame.setProperty("panel", True)
        frame.setMinimumHeight(96)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(18)
        text = QVBoxLayout()
        text.setSpacing(6)
        heading = QHBoxLayout()
        name = QLabel(str(plugin.get("name") or plugin.get("id")))
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setStyleSheet(f"color: {TEXT}; font-size: 15px; font-weight: bold;")
        heading.addWidget(name)
        category = str(plugin.get("category") or "utility")
        badge = QLabel(self.CATEGORIES.get(category, category))
        badge.setTextFormat(Qt.TextFormat.PlainText)
        badge.setStyleSheet(f"color: {ACCENT2}; background: rgba(47,168,160,24); padding: 3px 8px; border-radius: 5px; font-size: 11px;")
        heading.addWidget(badge)
        capability_label=plugin.get('capability_label')
        if capability_label:
            state_badge=QLabel(str(capability_label))
            state_badge.setTextFormat(Qt.TextFormat.PlainText)
            state_badge.setStyleSheet(f'color: {ERROR if plugin.get("available") is False else ACCENT2}; font-size: 11px; padding: 3px 8px;')
            heading.addWidget(state_badge)
        # 来源标记：内置 / 目录包（带 mod_id）/ 自定义，用来分清两条插件链
        source_text=self._source_text(plugin)
        source_badge=QLabel(source_text)
        source_badge.setTextFormat(Qt.TextFormat.PlainText)
        source_badge.setStyleSheet(f"color: {MUTED}; background: rgba(95,107,122,18); padding: 3px 8px; border-radius: 5px; font-size: 11px;")
        source_badge.setToolTip("插件来源：内置模块 / 目录包（mod）/ 自定义（自己安装的 .py）")
        heading.addWidget(source_badge)
        heading.addStretch()
        text.addLayout(heading)
        description = QLabel(str(plugin.get("description") or "暂无功能说明"))
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        text.addWidget(description)
        if plugin.get('status_note') or plugin.get('available') is False:
            capability=QLabel(str(plugin.get('status_note') or '当前接入不支持'))
            capability.setTextFormat(Qt.TextFormat.PlainText)
            capability.setWordWrap(True);capability.setStyleSheet(f"color: {ERROR if plugin.get('available') is False else ACCENT2}; font-size: 12px;")
            text.addWidget(capability)
        layout.addLayout(text, 1)
        enabled = QCheckBox(str(plugin.get("enabled_label") or "已启用") if plugin.get("enabled") else "已停用")
        enabled.setMinimumWidth(88)
        enabled.setChecked(bool(plugin.get("enabled")))
        if plugin.get('available') is False:
            enabled.setText(str(plugin.get('availability_label') or '接入受限'))
            enabled.setToolTip(str(plugin.get('status_note') or '当前接入不支持；可关闭已保存的开关。'))
        enabled.setAccessibleName(f"启用 {plugin.get('name') or plugin.get('id')}")
        enabled.toggled.connect(lambda checked, pid=plugin.get("id"): self._toggle(pid, checked))
        layout.addWidget(enabled)
        if plugin.get("schema") or plugin.get("config_schema"):
            button = QPushButton("配置")
            button.setProperty("class", "secondary")
            button.setToolTip(f"调整 {plugin.get('name') or plugin.get('id')} 的参数")
            button.clicked.connect(lambda _=False, p=plugin: self._configure(p))
            layout.addWidget(button)
        else:
            label = QLabel("无可配置项")
            label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            layout.addWidget(label)
        # 只有自定义插件给删除按钮：内置插件删了会让功能缺失
        plugin_id = str(plugin.get("id") or "")
        if plugin_id in self._custom_ids:
            remove = QPushButton("删除")
            remove.setProperty("class", "secondary")
            remove.setToolTip("删除这个自己安装的插件（会真的删掉文件）")
            remove.clicked.connect(lambda _=False, pid=plugin_id, p=plugin: self._delete_plugin(pid, p))
            layout.addWidget(remove)
        frame.setToolTip(f"插件标识：{plugin.get('id', '')}")
        self._rows[plugin.get("id")] = {"frame":frame, "toggle":enabled, "plugin":plugin}
        return frame

    def _add_plugin(self):
        """打开安装对话框，装完自动刷新列表。"""
        dialog = PluginInstallDialog(self)
        dialog.exec()
        if dialog.installed:
            self.refresh()

    def _delete_plugin(self, plugin_id: str, plugin: dict):
        """删除自定义插件（二次确认 + 明确告知会删文件）。"""
        name = plugin.get("name") or plugin_id
        if not ask_confirm(
                self, "删除插件", f"确定要删除插件「{name}」吗？",
                detail="这会删除该插件的文件或插件包目录，并从插件清单移除，"
                       "无法在界面里撤销。\n请确认这不是你还需要的东西。",
                ok_text="删除", danger=True):
            return
        result = api_delete(f"plugins/{plugin_id}", timeout=30)
        if result.get("success"):
            self.notice.setText(str(result.get("message") or f"{plugin_id} 已删除"))
            self.refresh()
        else:
            tell(self, "删除失败",
                 str(result.get("message") or result.get("detail") or "未知原因"), kind="error")

    def _toggle(self, plugin_id: str, checked: bool):
        # 注意：异步版（run_api_async + 回调）曾让全量回归出现原生崩溃 0xC0000005 ——
        # 与 QThread/回调的生命周期有关，要重做必须先设计线程回收并补测试。
        # 这里保持同步实现（请求很短，失败会立即恢复开关状态）。
        entry = self._rows.get(plugin_id)
        if not entry:
            return
        control = entry["toggle"]
        previous = bool(entry["plugin"].get("enabled"))
        control.setEnabled(False)
        try:
            result = api_post(f"plugins/{plugin_id}", {"enabled": bool(checked)})
            if isinstance(result, dict) and result.get("success"):
                entry["plugin"]["enabled"] = checked
                entry["plugin"].setdefault("settings", {})["enabled"] = checked
                control.setText(str(entry["plugin"].get("enabled_label") or "已启用")
                                if checked else "已停用")
                self.notice.setText(f"{entry['plugin'].get('name') or plugin_id} "
                                    f"已{'启用' if checked else '停用'}")
                self._filter_rows()
            else:
                control.blockSignals(True)
                control.setChecked(previous)
                control.blockSignals(False)
                self.notice.setText(f"操作失败，已恢复原状态：{(result or {}).get('message') or result}")
        finally:
            control.setEnabled(True)

    def _open_wakeup(self):
        if not self._plugins:self.refresh()
        plugin=next((p for p in self._plugins if p.get('id')=='chat_controls'),None)
        if plugin is None:
            tell(self,'未读取到设置','请先刷新插件列表，再打开唤醒关键词设置。')
            return
        self._configure(plugin)

    def _configure(self, plugin: dict):
        dialog = PluginConfigDialog(plugin, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved:
            self.refresh()
            self.notice.setText(f"{plugin.get('name') or plugin.get('id')} 设置已保存")


# ── 关于作者 ──────────────────────────────────────────────────────────────────
# 作者与致谢集中在这里一处；要改只改这几个常量（微信版同一套写法，保持一致）。
AUTHOR_NAME = "林夕"
AUTHOR_QQ = "2019117802"
AUTHOR_BLOG = ""          # 有博客地址就填在这里（留空则不显示这一行）
AUTHOR_TAGLINE = "sana世界第一"
GROUP_NAME = "水产养殖交流群"
GROUP_QQ = "948578157"
THANKS_USERS = ["水产养殖群聊的所有群友", "狐栗", "稔穗", "你携秋水揽星河"]
COPYRIGHT_NOTICE = "未经作者允许，禁止贩卖、转卖。"


def author_avatar_path() -> Path:
    """作者头像：随 assets 打进安装包，所以走 BUNDLE 而不是源码目录。"""
    return BUNDLE / "assets" / "author" / "linxi.jpg"


def author_group_qr_path() -> Path:
    """交流群二维码：同样随 assets 打进安装包，走 BUNDLE 而不是源码目录。"""
    return BUNDLE / "assets" / "author" / "group_qr.png"


class AboutTab(QWidget):
    """关于作者：作者、交流群、致谢、声明与使用须知。"""

    def __init__(self):
        super().__init__()
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: 0; }")
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        layout.addWidget(self._author_card())
        layout.addWidget(self._copyright_card())      # 声明显眼：紧贴作者卡片下方
        layout.addWidget(self._group_card())
        layout.addWidget(self._thanks_card())
        layout.addWidget(self._update_card())
        layout.addWidget(self._safety_card())
        layout.addStretch(1)

        scroll.setWidget(inner)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.addWidget(scroll)

    def _card(self, title: str, slot: str = "") -> tuple:
        frame = QFrame()
        frame.setProperty("panel", True)
        box = QVBoxLayout(frame)
        box.setContentsMargins(18, 16, 18, 18)
        box.setSpacing(10)
        if slot:
            box.addWidget(icon_row(title, slot))
        else:
            label = QLabel(title)
            label.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-weight: bold;")
            box.addWidget(label)
        return frame, box

    def _author_card(self) -> QFrame:
        frame, box = self._card("关于作者", "app")
        row = QHBoxLayout()
        row.setSpacing(16)

        self.avatar = QLabel()
        self.avatar.setFixedSize(96, 96)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 背景透明，圆形描边画在 pixmap 里（样式表圆角不会裁掉子 pixmap，会露方底）
        self.avatar.setStyleSheet("QLabel { background: transparent; border: none; }")
        self._load_avatar()
        row.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(6)
        name = QLabel(AUTHOR_NAME)
        name.setStyleSheet(f"color: {ACCENT}; font-size: 20px; font-weight: bold;")
        info.addWidget(name)
        self.qq_label = QLabel(f"QQ：{AUTHOR_QQ}")
        self.qq_label.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        info.addWidget(self.qq_label)
        if AUTHOR_BLOG:
            blog = QLabel(f'博客：<a href="{AUTHOR_BLOG}" style="color:{ACCENT};">{AUTHOR_BLOG}</a>')
            blog.setOpenExternalLinks(True)
            blog.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
            info.addWidget(blog)
        role = QLabel("本程序的设计与开发 · 个人非商业作品")
        role.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        info.addWidget(role)
        tagline = QLabel(AUTHOR_TAGLINE)
        tagline.setStyleSheet(f"color: {ACCENT2}; font-size: 13px; font-weight: bold;")
        info.addWidget(tagline)

        buttons = QHBoxLayout()
        self.btn_copy_qq = decorate_button(QPushButton("复制 QQ 号"), "btn_save")
        self.btn_copy_qq.clicked.connect(self._copy_qq)
        buttons.addWidget(self.btn_copy_qq)
        self.btn_copy_info = decorate_button(QPushButton("复制版本信息"), "btn_reload")
        self.btn_copy_info.setToolTip("把版本号与数据目录复制到剪贴板，反馈问题时一起发来")
        self.btn_copy_info.clicked.connect(self._copy_version)
        buttons.addWidget(self.btn_copy_info)
        buttons.addStretch(1)
        info.addLayout(buttons)

        self.copy_hint = QLabel("")
        self.copy_hint.setStyleSheet(f"color: {SUCCESS}; font-size: 12px;")
        info.addWidget(self.copy_hint)

        row.addLayout(info, 1)
        box.addLayout(row)
        return frame

    def _group_card(self) -> QFrame:
        frame, box = self._card("交流群", "card_events")
        row = QHBoxLayout()
        row.setSpacing(16)

        self.group_qr = QLabel()
        self.group_qr.setFixedSize(220, 210)
        self.group_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.group_qr.setStyleSheet("QLabel { background: transparent; border: none; }")
        path = author_group_qr_path()
        pixmap = QPixmap(str(path)) if path.is_file() else QPixmap()
        if pixmap.isNull():
            self.group_qr.setText("群二维码暂缺")
            self.group_qr.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        else:
            self.group_qr.setPixmap(pixmap.scaled(
                220, 210, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        row.addWidget(self.group_qr, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(6)
        name = QLabel(GROUP_NAME)
        name.setStyleSheet(f"color: {ACCENT}; font-size: 18px; font-weight: bold;")
        info.addWidget(name)
        number = QLabel(f"群号：{GROUP_QQ}")
        number.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        number.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info.addWidget(number)
        hint = QLabel("扫码加入群聊；二维码失效时，可以在群里找作者更新。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        info.addWidget(hint)
        info.addStretch(1)

        row.addLayout(info, 1)
        box.addLayout(row)
        return frame

    def _thanks_card(self) -> QFrame:
        frame, box = self._card("特别感谢", "card_events")
        intro = QLabel("特别感谢（排名不分先后）：")
        intro.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(intro)
        for user in THANKS_USERS:
            line = QLabel(f"· {user}")
            line.setStyleSheet(f"color: {ACCENT2}; font-size: 13px; font-weight: bold;")
            box.addWidget(line)
        more = QLabel("以及所有反馈问题、帮忙测试的朋友。")
        more.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(more)
        return frame


    UPDATE_NOTES_LIMIT = 6000

    @staticmethod
    def format_update_notes(notes, limit: int = UPDATE_NOTES_LIMIT) -> str:
        """更新说明统一截断到 limit 字；空说明给一句可读的占位文案。"""
        text = str(notes or "").strip()
        if not text:
            return "此版本没有附更新说明"
        suffix = "\n…（更新说明过长，已截断）"
        if len(text) > int(limit):
            keep = max(0, int(limit) - len(suffix))
            return text[:keep].rstrip() + suffix
        return text

    @staticmethod
    def format_update_progress(progress) -> str:
        """把 /update/progress 折成“45% · 1.2 MB/s · 正在下载”这一行。"""
        data = dict(progress) if isinstance(progress, dict) else {}
        try:
            percent = max(0.0, min(100.0, float(data.get("percent"))))
        except (TypeError, ValueError):
            percent = None
        parts = []
        if percent is not None:
            parts.append("%d%%" % int(round(percent)))
        try:
            speed = float(data.get("bytesPerSecond"))
        except (TypeError, ValueError):
            speed = None
        if speed is not None and speed >= 0:
            if speed >= 1024 * 1024:
                parts.append("%.1f MB/s" % (speed / (1024 * 1024)))
            elif speed >= 1024:
                parts.append("%.1f KB/s" % (speed / 1024))
            else:
                parts.append("%d B/s" % int(speed))
        message = str(data.get("message") or "").strip()
        if message:
            parts.append(message)
        return " · ".join(parts) or "等待更新进度…"

    def _update_card(self) -> QFrame:
        """GitHub 自动更新卡：更新源可配置，一键更新由安装包在退出后接管。"""
        frame, box = self._card("自动更新")
        hint = QLabel("更新包从 GitHub 仓库的 Release 里取 Setup .exe；"
                      "填 owner/repo 或仓库链接即可。源码运行不能一键覆盖，请用安装版。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("GitHub 仓库")
        label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        row.addWidget(label)
        self.update_repo = QLineEdit()
        self.update_repo.setObjectName("about_update_repo")
        self.update_repo.setPlaceholderText("例如 your-name/WhaleGirlQQBridge，或 https://github.com/…")
        self.update_repo.setClearButtonEnabled(True)
        row.addWidget(self.update_repo, 1)
        box.addLayout(row)

        self.update_auto = QCheckBox("启动时自动检查更新（只检查，不自动安装）")
        self.update_auto.setChecked(True)
        box.addWidget(self.update_auto)

        buttons = QHBoxLayout()
        self.btn_update_save = QPushButton("保存更新设置")
        self.btn_update_save.clicked.connect(self._save_update)
        buttons.addWidget(self.btn_update_save)
        self.btn_update_check = QPushButton("检查更新")
        self.btn_update_check.clicked.connect(self._check_update)
        buttons.addWidget(self.btn_update_check)
        self.btn_update_apply = QPushButton("一键更新")
        self.btn_update_apply.setEnabled(False)
        self.btn_update_apply.setToolTip("下载 GitHub Release 里的安装包，控制台退出后静默安装并重新打开")
        self.btn_update_apply.clicked.connect(self._apply_update)
        buttons.addWidget(self.btn_update_apply)
        buttons.addStretch(1)
        box.addLayout(buttons)

        self.update_notes_label = QLabel("更新说明")
        self.update_notes_label.setStyleSheet(f"color: {MUTED}; font-size: 12px; font-weight: bold;")
        self.update_notes_label.setVisible(False)
        box.addWidget(self.update_notes_label)
        self.update_notes = QTextEdit()
        self.update_notes.setObjectName("about_update_notes")
        self.update_notes.setReadOnly(True)
        self.update_notes.setAcceptRichText(False)
        self.update_notes.setPlaceholderText("检查更新后在这里显示完整更新说明")
        self.update_notes.setFixedHeight(120)
        self.update_notes.setVisible(False)
        box.addWidget(self.update_notes)

        # 发布方没给 sha256 时，一键更新会先落到这里：把后端给的 reason 原样显示，
        # 用户点「仍然继续」才把 allowUnverified 带上去。默认不勾，宁严勿松。
        self.update_unverified = QCheckBox("这个版本没有发布方 sha256 校验值，仍然继续")
        self.update_unverified.setObjectName("about_update_unverified")
        self.update_unverified.setVisible(False)
        box.addWidget(self.update_unverified)

        self.update_progress = QProgressBar()
        self.update_progress.setObjectName("about_update_progress")
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_progress.setTextVisible(True)
        self.update_progress.setVisible(False)
        box.addWidget(self.update_progress)
        self.update_progress_text = QLabel("")
        self.update_progress_text.setObjectName("about_update_progress_text")
        self.update_progress_text.setWordWrap(True)
        self.update_progress_text.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.update_progress_text.setVisible(False)
        box.addWidget(self.update_progress_text)
        self._update_progress_timer = QTimer(self)
        self._update_progress_timer.setInterval(500)
        self._update_progress_timer.timeout.connect(self._poll_update_progress)
        self._update_progress_busy = False

        self.update_state = QLabel(f"当前版本 {APP_VERSION}。填好仓库后可检查更新。")
        self.update_state.setWordWrap(True)
        self.update_state.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(self.update_state)
        return frame

    def auto_check_update(self):
        """启动后静默读一次设置；只有用户勾了自动检查且填了仓库才联网。"""
        run_api_async(self, "update", {}, timeout=15,
                      callback=self._auto_update_loaded, busy="")

    def _auto_update_loaded(self, data: dict):
        if not data.get("success"):
            return
        repo = str(data.get("repo") or "")
        if repo and not self.update_repo.text().strip():
            self.update_repo.setText(repo)
        self.update_auto.setChecked(bool(data.get("auto", True)))
        if not data.get("auto"):
            return
        if not repo:
            self.update_state.setText(f"当前版本 {APP_VERSION}；还没有配置更新仓库。")
            return
        self._check_update()

    def _update_payload(self) -> dict:
        return {"repo": self.update_repo.text().strip(),
                "auto": self.update_auto.isChecked()}

    def _save_update(self):
        self.btn_update_save.setEnabled(False)
        run_api_async(self, "update/config", self._update_payload(), timeout=20,
                      callback=self._update_saved, busy="正在保存更新设置…")

    def _update_saved(self, data: dict):
        self.btn_update_save.setEnabled(True)
        if not data.get("success"):
            self.update_state.setText(str(data.get("message") or "保存失败"))
            return
        self.update_state.setText("更新设置已保存。")

    def _check_update(self):
        self.btn_update_check.setEnabled(False)
        self.btn_update_apply.setEnabled(False)
        self.btn_update_apply.setText("一键更新")
        self.update_unverified.setChecked(False)
        self.update_unverified.setVisible(False)
        self.update_state.setText("正在检查 GitHub Release…")
        run_api_async(self, "update/check", {"repo": self.update_repo.text().strip()},
                      timeout=45, callback=self._update_checked, busy="正在检查更新…")

    def _set_update_notes(self, notes):
        self.update_notes.setPlainText(self.format_update_notes(notes))
        self.update_notes.setVisible(True)
        self.update_notes_label.setVisible(True)

    def _show_update_progress(self, progress):
        data = dict(progress) if isinstance(progress, dict) else {}
        try:
            percent = int(round(max(0.0, min(100.0, float(data.get("percent"))))))
        except (TypeError, ValueError):
            percent = 0
        self.update_progress.setValue(percent)
        self.update_progress.setVisible(True)
        self.update_progress_text.setText(self.format_update_progress(data))
        self.update_progress_text.setVisible(True)

    def _begin_update_progress(self):
        self.update_progress.setValue(0)
        self.update_progress.setVisible(True)
        self.update_progress_text.setText("正在启动更新…")
        self.update_progress_text.setVisible(True)
        self._update_progress_busy = False
        if not self._update_progress_timer.isActive():
            self._update_progress_timer.start()
        QTimer.singleShot(0, self._poll_update_progress)

    def _poll_update_progress(self):
        """每 500ms 读一次进度；单飞标志防止慢请求排队堆叠。"""
        if self._update_progress_busy or not self._update_progress_timer.isActive():
            return
        self._update_progress_busy = True
        run_api_async(self, "GET update/progress", timeout=10,
                      callback=self._update_progress_loaded)

    def _update_progress_loaded(self, data: dict):
        self._update_progress_busy = False
        if not self._update_progress_timer.isActive():
            return
        payload = data if isinstance(data, dict) else {}
        if payload.get("success"):
            progress = payload.get("progress")
            if isinstance(progress, dict):
                self._show_update_progress(progress)
            elif payload.get("message"):
                self.update_progress_text.setText(str(payload.get("message")))
        else:
            message = str(payload.get("message") or "读取更新进度失败")
            self.update_progress_text.setText(message)
        self.update_progress_text.setVisible(True)

    def _stop_update_progress(self, message: str = "", retry: bool = False):
        self._update_progress_timer.stop()
        self._update_progress_busy = False
        if message:
            self.update_progress_text.setText(str(message))
            self.update_progress_text.setVisible(True)
        if retry:
            self.btn_update_apply.setText("重试更新")
            self.btn_update_apply.setEnabled(True)

    def _update_checked(self, data: dict):
        self.btn_update_check.setEnabled(True)
        if not data.get("success"):
            self.update_state.setText(str(data.get("message") or "检查更新失败"))
            return
        latest = str(data.get("latest") or "")
        current = str(data.get("current") or APP_VERSION)
        published = str(data.get("published_at") or data.get("publishedAt") or "")
        repo = str(data.get("repo") or "")
        if repo and not self.update_repo.text().strip():
            self.update_repo.setText(repo)
        self._set_update_notes(data.get("notes"))
        bits = [f"当前版本 {current}"]
        if latest:
            bits.append(f"最新版本 {latest}")
        if published:
            bits.append(f"发布时间 {published}")
        if data.get("hasUpdate"):
            self.btn_update_apply.setEnabled(True)
            # 后端说这个 Release 拿不到发布方哈希：先摆出确认框，把原因说清楚。
            # 用户不勾就照旧提交，后端会以 UNVERIFIED_REASON 400 拒绝，不会静默安装。
            needs_confirm = data.get("needs_confirmation") is True
            reason = str(data.get("confirmation_reason") or "").strip()
            self.update_unverified.setVisible(needs_confirm)
            self.update_unverified.setChecked(False)
            if needs_confirm and reason:
                self.update_unverified.setText(reason)
            self.update_state.setText("发现新版本：" + "，".join(bits) +
                                      ("（无法校验安装包，见下方说明）" if needs_confirm else "") + "。")
        else:
            self.update_state.setText("已经是最新版本：" + "，".join(bits) + "。")

    def _apply_update(self):
        needs_confirm = self.update_unverified.isVisible()
        allow_unverified = needs_confirm and self.update_unverified.isChecked()
        if needs_confirm and not allow_unverified:
            # 没有校验值又没勾选：不去请求后端，直接把选择权摆回用户面前。
            # 让他先勾「仍然继续」，而不是让请求 400 之后再猜哪一步错了。
            QMessageBox.warning(
                self, "缺少校验值",
                "这个 Release 没有发布方 sha256 校验值，无法验证安装包完整性。\n"
                "确认要装的话，请先勾选下方「仍然继续」再点一键更新。")
            return
        answer = QMessageBox.question(
            self, "一键更新",
            "会从 GitHub 下载最新安装包；控制台随后退出，"
            "安装完成后自动重新打开。现在继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.btn_update_apply.setEnabled(False)
        self.btn_update_check.setEnabled(False)
        self.update_state.setText("正在下载更新包…")
        self._begin_update_progress()
        run_api_async(self, "update/apply",
                      {"repo": self.update_repo.text().strip(), "confirmed": True,
                       "allowUnverified": bool(allow_unverified)},
                      timeout=900, callback=self._update_applied,
                      busy="正在下载更新包…")

    def _update_applied(self, data: dict):
        self.btn_update_check.setEnabled(True)
        progress = data.get("progress") if isinstance(data, dict) else None
        if isinstance(progress, dict):
            self._show_update_progress(progress)
        if not data.get("success"):
            self._stop_update_progress(str(data.get("message") or "更新失败"), retry=True)
            self.update_state.setText(str(data.get("message") or "更新失败，可点「重试更新」再试一次。"))
            return
        if not data.get("hasUpdate"):
            self._stop_update_progress(str(data.get("message") or "已经是最新版本"))
            self.update_state.setText(str(data.get("message") or "已经是最新版本"))
            return
        self._stop_update_progress("更新包已下载完成，准备退出安装。")
        self.update_state.setText(
            "安装包已下载，控制台即将退出并安装；安装完成后会自动重新打开。")
        QTimer.singleShot(1500, self._quit_for_update)

    def _quit_for_update(self):
        window = self.window()
        if window is not None:
            window.close()
        QApplication.quit()

    def _safety_card(self) -> QFrame:
        frame, box = self._card("使用须知", "card_persona_list")
        items = [
            "请遵守 QQ 平台规则；因违规使用导致的账号风险由使用者自行承担。",
            "官方 QQ 只推送 @ 机器人的群消息；群内未被点名的消息平台不会发给机器人。",
            "群聊默认只在被 @ 或命中触发词时回复；回复内容由你自己的模型生成，请自行把控。",
            "所有配置与对话记录都保存在本机数据目录，不上传任何服务器。",
        ]
        for text in items:
            label = QLabel(f"· {text}")
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            box.addWidget(label)
        return frame

    def _copyright_card(self) -> QFrame:
        frame, box = self._card("声明", "notice")
        notice = QLabel(COPYRIGHT_NOTICE)
        notice.setWordWrap(True)
        notice.setStyleSheet(f"color: {ERROR}; font-size: 14px; font-weight: bold;")
        box.addWidget(notice)
        extra = QLabel("· 允许个人免费使用与自行修改；请勿删除作者署名后二次分发。\n"
                       "· 本程序按「现状」提供，作者不对使用结果承担任何责任。\n"
                       "· 转载或以本程序牟利前，请先联系作者获得许可。")
        extra.setWordWrap(True)
        extra.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(extra)
        return frame

    def _load_avatar(self) -> None:
        """加载头像并裁成圆形；取不到就退化成文字，不让整页崩掉。"""
        path = author_avatar_path()
        pixmap = QPixmap(str(path)) if path.is_file() else QPixmap()
        if pixmap.isNull():
            self.avatar.setText(AUTHOR_NAME)
            self.avatar.setStyleSheet(
                f"QLabel {{ color: {ACCENT}; font-size: 18px; font-weight: bold;"
                f" border: 2px solid {BORDER}; border-radius: 48px; background: {FIELD}; }}")
            return
        side = 92
        scaled = pixmap.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - side) // 2)
        y = max(0, (scaled.height() - side) // 2)
        circle = QPixmap(side, side)
        circle.fill(Qt.GlobalColor.transparent)
        painter = QPainter(circle)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QPainterPath()
        ring.addEllipse(1, 1, side - 2, side - 2)
        painter.setClipPath(ring)
        painter.drawPixmap(-x, -y, scaled)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(BORDER), 2))
        painter.drawEllipse(1, 1, side - 2, side - 2)
        painter.end()
        self.avatar.setPixmap(circle)

    def _copy_qq(self) -> None:
        QApplication.clipboard().setText(AUTHOR_QQ)
        self.copy_hint.setText(f"已复制 QQ 号：{AUTHOR_QQ}")

    def _copy_version(self) -> None:
        info = (f"鲸鱼娘 · QQ 控制台 {APP_VERSION}\n"
                f"数据目录：%LOCALAPPDATA%\\WhaleGirlQQ\n"
                f"作者：{AUTHOR_NAME}（QQ {AUTHOR_QQ}）")
        QApplication.clipboard().setText(info)
        self.copy_hint.setText("已复制版本信息，反馈问题时可一起发来")


# ── 主窗口 ────────────────────────────────────────────────────────────────────
class ResizableTabs(QTabWidget):
    # QTabWidget 会把换行标签的建议高度传给原生 WM_SIZING，导致拖竖边时
    # 被强制弹回该高度。页面自行排版/滚动，不把建议高度变成窗口限制。
    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, width):
        return -1


class AccountCreateDialog(QDialog):
    """新建账号：备注 + 从哪个账号复制配置 + 创建后是否切换。"""

    def __init__(self, accounts: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle('新建账号')
        self.setMinimumWidth(430)
        # 顶层对话框不一定继承主窗口样式表：这里显式给浅色背景与文字颜色，
        # 否则在深色系统主题下会变成截图里那种黑底。
        self.setStyleSheet(
            "QDialog { background: #F4F6F9; }"
            "QLabel { color: #1F2733; }"
            "QLineEdit, QComboBox { background: #FFFFFF; color: #1F2733;"
            " border: 1px solid #D3DAE3; border-radius: 7px; padding: 7px 9px; }"
            "QCheckBox { color: #1F2733; }"
            "QPushButton { background: #2FA8A0; color: #FFFFFF; border: 0;"
            " border-radius: 7px; padding: 8px 18px; font-weight: bold; }"
            "QPushButton[class=\"secondary\"] { background: #E7ECF3; color: #1F2733; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        title = QLabel('新建一个机器人账号')
        title.setObjectName('Title')
        layout.addWidget(title)
        hint = QLabel('每个账号有独立的配置、聊天记录与插件状态；'
                      '「复制配置」只复制设置，不复制聊天记录。')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #68778C; font-size: 12px;')
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)
        self.label_edit = QLineEdit('新账号')
        form.addRow('账号备注', self.label_edit)
        self.source_combo = QComboBox()
        for item in accounts:
            self.source_combo.addItem(str(item.get('label') or item.get('id')), item.get('id'))
        form.addRow('复制配置自', self.source_combo)
        self.switch_check = QCheckBox('创建后立刻切换到这个账号')
        self.switch_check.setChecked(True)
        form.addRow('', self.switch_check)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('取消')
        cancel.setProperty('class', 'secondary')
        cancel.clicked.connect(self.reject)
        create = QPushButton('创建账号')
        create.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(create)
        layout.addLayout(buttons)

    def values(self) -> dict:
        return {'label': self.label_edit.text().strip() or '新账号',
                'clone_from': self.source_combo.currentData() or None,
                'switch': self.switch_check.isChecked()}


# 面板收敛：早先的做法是把四个页面收进侧栏底部的「更多面板」弹出菜单，用户明确说
# 这种入口方式不对（弹出菜单还是黑底暗字，等于点不到）。现在四个页面直接进侧栏，
# 这里置空；常量名保留，兼容既有引用（测试与外部脚本按名字取）。
HIDDEN_NAV_ENTRIES = ()


class MainWindow(QWidget):
    watchdog_notice = pyqtSignal(str)
    def __init__(self, config_path=None):
        super().__init__()
        # config_path 只给测试用：让界面测试写进临时文件，绝不碰用户/开发机的真实外观配置
        # （之前测试直接读写 data/gui_config.json，把 safe_mode 和测试窗口尺寸留在里面了）
        self._cfg_path = Path(config_path) if config_path else GUI_CFG
        # 由管理器（QQ 统一接入控制台）拉起时，目录里会有 launch.json：
        # 账号、中继地址、端口都由管理器给定，控制台不再自己管账号。
        self._launch_job = {}
        try:
            import json as _json
            launch = Path(ROOT) / 'launch.json'
            if launch.is_file():
                data = _json.loads(launch.read_text(encoding='utf-8'))
                if isinstance(data, dict) and data.get('ws_url'):
                    self._launch_job = data
        except Exception:
            self._launch_job = {}
        self._cfg = load_gui_config(self._cfg_path)
        # 新装/没选过背景的用户默认套用 Mio 风格的浅色背景；自定义背景不受影响。
        ensure_default_background(self._cfg, ROOT, BUNDLE)
        self._backend_proc = None
        self._busy = False
        self._probe_worker = None      # 获取模型列表 / 测试连接的后台线程
        self._quitting=False
        # 界面卡死取证器：下次再"未响应"，logs/hang-*.txt 里就有每个线程的调用栈
        self._hang_notices = queue.SimpleQueue()
        self.hang_watchdog = UiHangWatchdog(
            self, _logs_dir(),
            note=self._hang_notices.put)
        self._hang_notice_timer = QTimer(self)
        self._hang_notice_timer.timeout.connect(self._drain_hang_notices)
        self._hang_notice_timer.start(1000)
        self.hang_watchdog.start()

        # 窗口装饰：安全模式下用**系统边框**（远程桌面 / 多个虚拟显示适配器的机器上，
        # 无边框 + 半透明 + 云母这套平台集成最容易触发 Qt 原生访问违规，
        # 实测那台机器就是每次双击必崩 0xC0000005，窗口根本来不及出现）。
        self.safe_mode = bool(SAFE_MODE or self._cfg.get("safe_mode"))
        self.setWindowTitle(f"鲸鱼娘 · QQ 控制台 {APP_VERSION}")
        if self.safe_mode:
            self.setWindowFlags(Qt.WindowType.Window)
        else:
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # 恢复窗口大小/位置
        geo = self._cfg.get("geometry", {})
        self.resize(geo.get("w", 1200), geo.get("h", 760))
        if "x" in geo:
            self.move(geo["x"], geo["y"])

        # 外层容器（纯色底 + 自定义背景图，自绘）
        outer = QVBoxLayout(self)
        margin = 0 if self.safe_mode else 6
        outer.setContentsMargins(margin, margin, margin, margin)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._container = BackgroundFrame()
        self._container.setObjectName("container")
        # 无边框窗口的边缘缩放：容器铺满整个窗口，事件过滤器装在它身上才收得到边缘鼠标
        self._container.setMouseTracking(True)
        self._container.installEventFilter(self)
        self._apply_bg(self._cfg.get("bg_color", BG), self._cfg.get("background_image", ""))
        outer.addWidget(self._container)

        # 内层布局
        vbox = QVBoxLayout(self._container)
        vbox.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        # 标题栏
        self.title_bar = TitleBar(self)
        vbox.addWidget(self.title_bar)
        # 系统边框已经有标题和窗口按钮；兼容模式不能再叠一套自定义标题栏。
        self.title_bar.setVisible(not self.safe_mode)

        # 分隔线
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {BORDER};")
        vbox.addWidget(line)
        line.setVisible(not self.safe_mode)

        # 多账号：账号切换栏（单进程内切换，见 /api/accounts/select）
        self.account_bar = QWidget()
        account_row = QHBoxLayout(self.account_bar)
        account_row.setContentsMargins(0, 0, 0, 0)
        account_row.setSpacing(8)
        account_label = QLabel("账号")
        account_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(200)
        self.account_combo.activated.connect(self._on_account_chosen)
        self.account_add = QPushButton("新建账号")
        self.account_add.clicked.connect(self._add_account)
        self.account_rename = QPushButton("重命名")
        self.account_rename.clicked.connect(self._rename_account)
        self.account_remove = QPushButton("删除")
        self.account_remove.clicked.connect(self._remove_account)
        for widget in (account_label, self.account_combo, self.account_add,
                       self.account_rename, self.account_remove):
            account_row.addWidget(widget)
        account_row.addStretch(1)
        self.account_hint = QLabel("")
        self.account_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        account_row.addWidget(self.account_hint)
        self.account_bar.setFixedHeight(32)
        # 左侧导航 + 右侧内容（用户要求"选项框在左侧"）
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)
        vbox.addLayout(body_row, 1)

        self.sidebar = QWidget()
        self.sidebar.setObjectName('Sidebar')
        self.sidebar.setFixedWidth(188)
        side_layout = QVBoxLayout(self.sidebar)
        side_layout.setContentsMargins(14, 16, 10, 16)
        side_layout.setSpacing(8)
        brand = QLabel('鲸鱼娘 QQ')
        brand.setObjectName('SidebarTitle')
        side_layout.addWidget(brand)
        subtitle = QLabel('QQ 控制台')
        subtitle.setObjectName('SidebarMuted')
        side_layout.addWidget(subtitle)
        side_layout.addSpacing(6)
        self.nav_list = QListWidget()
        self.nav_list.setObjectName('NavList')
        self.nav_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        side_layout.addWidget(self.nav_list, 1)
        # 账号区已整体移到设置页：侧栏不再显示任何账号控件
        account_title = QLabel('')
        account_title.setVisible(False)
        account_buttons = QHBoxLayout()
        account_buttons.setSpacing(4)
        # 侧栏只有 188px，三个长按钮会被裁成"重界/命合"：换成短标签 + 等高
        shorts = {self.account_add: '新建', self.account_rename: '改名',
                  self.account_remove: '删除'}
        for widget in (self.account_add, self.account_rename, self.account_remove):
            widget.setText(shorts[widget])
            # 主样式表给按钮 18px 左右内边距，侧栏里会把两个字裁掉；这里单独收紧。
            widget.setStyleSheet(
                "QPushButton { padding: 3px 4px; font-size: 12px; min-width: 0; }")
            widget.setMinimumHeight(26)
            widget.setMaximumHeight(30)
            widget.setMinimumWidth(52)
            account_buttons.addWidget(widget)
        side_layout.addLayout(account_buttons)
        # 从顶部账号栏"搬"到侧栏（重新 setParent，原 account_bar 保持隐藏）
        self.account_combo.setParent(self.sidebar)
        side_layout.addWidget(self.account_combo)
        self.account_combo.setPlaceholderText('（未连接管理器）')
        self.account_combo.setVisible(False)
        self.account_combo.setMinimumHeight(30)
        self.account_combo.setMaximumHeight(32)
        # 侧栏里不能用自动换行标签：它会把 hasHeightForWidth 传给根布局，
        # 窗口就会被文字撑高（布局适应性测试抓的就是这个）。
        self.account_hint.setWordWrap(False)
        self.account_hint.setToolTip('账号来自管理器；切换只影响本控制台查看的账号')
        side_layout.addWidget(self.account_hint)
        # 管理器（QQ 统一接入控制台）状态：账号以它为准，这里只做只读入口
        # 账号与「管理器连接」都已挪到设置页；侧栏不再放账号区。
        self.manager_status = QLabel('')
        self.manager_status.setObjectName('SidebarMuted')
        self.manager_status.setWordWrap(False)
        side_layout.addWidget(self.manager_status)
        self.manager_button = QPushButton('打开管理器')
        self.manager_button.setVisible(False)
        self.manager_button.setStyleSheet(
            "QPushButton { padding: 4px 6px; font-size: 12px; }")
        self.manager_button.clicked.connect(self.open_manager)
        side_layout.addWidget(self.manager_button)
        # 账号切换区恢复：管理器模式下它是"一个控制台切多个 QQ"的入口
        for widget in (self.account_hint, self.manager_status):
            widget.setVisible(True)
        for widget in (self.account_add, self.account_rename, self.account_remove,
                       self.manager_button):
            widget.setVisible(False)
        body_row.addWidget(self.sidebar)

        self.content = QWidget()
        self.content.setObjectName('Content')
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.account_bar.setVisible(False)
        body_row.addWidget(self.content, 1)

        # 主内容区（标签页）
        self.tabs = ResizableTabs()
        self.tabs.setDocumentMode(True)
        # 页内滚动承接长表单，窗口高度由用户决定，不能被隐藏标签页的提示尺寸撑大。
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.console_tab = ConsoleTab()
        # 用 bound method 而不是 lambda：窗口销毁后能安全 disconnect，
        # 否则看门狗线程还会去碰已经删掉的控件（实测会把进程直接 abort）。
        self.watchdog_notice.connect(self._on_watchdog_notice)
        self.settings_tab = SettingsTab()
        self.appearance_tab = AppearanceTab()
        self.persona_tab = PersonaTab()
        self.plugin_tab = PluginTab()
        from src.gui.conversation import MemoryTab,PcTab
        self.memory_tab=MemoryTab(api_get,api_post,self.configure_conversation)
        self.pc_tab=PcTab(api_get,api_post,self.configure_conversation)
        self.about_tab = AboutTab()          # 关于作者：作者/致谢/声明与使用须知
        from src.gui.assistant import AssistantTab
        from src.gui.workbench import WorkbenchTab
        self.workbench_tab = WorkbenchTab(api=run_api_async)
        self.assistant_tab = AssistantTab(BASE_URL, ROOT, self)
        self.assistant_tab.navigate.connect(self._assistant_navigate)
        self.assistant_tab.features.connect(self.show_features)
        self.memory_tab.bindingRequested.connect(self.show_features)
        from src.gui.agent_panel import AgentBackendPanel
        # 外部后端页：端点 /api/agent/backends，轮询与动作全部走工作线程。
        self.agent_backend_tab = AgentBackendPanel(api_get, api_post, api_delete)
        from src.gui.logs_page import LogsPage
        # 日志页：端点 /api/logs，只读浏览 + 导出；请求同样走工作线程。
        self.logs_tab = LogsPage(api_get, api_post)
        # 图标：窗口/任务栏用主图标，每个标签页一枚（来自图标库的随机不重复分配）
        self.setWindowIcon(app_icon())
        # Mio 式工作区：打开控制台先进入对话，而不是先看仪表盘。
        self.tabs.addTab(self.assistant_tab, slot_icon("tab_console"), "对话")
        self.tabs.addTab(self.workbench_tab, slot_icon("tab_console"), "智能中心")
        self.tabs.addTab(self.console_tab, slot_icon("tab_console"), "控制台")
        self.tabs.addTab(self.memory_tab,slot_icon('tab_persona'),'对话与记忆')
        self.tabs.addTab(self.pc_tab,slot_icon('tab_console'),'机器人管理')
        self.tabs.addTab(self.persona_tab, slot_icon("tab_persona"), "人设")
        self.tabs.addTab(self.plugin_tab, slot_icon("tab_plugin"), "插件")
        self.tabs.addTab(self.settings_tab, slot_icon("tab_settings"), "设置")
        self.tabs.addTab(self.appearance_tab, slot_icon("tab_appearance"), "外观")
        self.tabs.addTab(self.agent_backend_tab, slot_icon("tab_console"), "外部后端")
        self.tabs.addTab(self.logs_tab, slot_icon("tab_console"), "日志")
        self.tabs.addTab(self.about_tab, slot_icon("app"), "关于作者")
        self.tabs.setCurrentIndex(0)
        # 标签条隐藏，用左侧列表切换（保留 QTabWidget 的页面管理能力）
        try:
            self.tabs.tabBar().hide()
        except Exception:
            pass
        # 分组：工作空间 / 管理（顺序与参考图一致）。
        # 机器人管理、人设、插件、外观四个页面直接进侧栏，不再收进弹出菜单。
        groups = [
            ('工作空间', ['对话', '智能中心', '控制台', '对话与记忆', '机器人管理']),
            ('管理', ['人设', '插件', '设置', '外观', '外部后端', '日志', '关于作者']),
        ]
        titles = {self.tabs.tabText(index): index for index in range(self.tabs.count())}
        self._nav_rows = []          # [(行号, tab 索引或 None)]
        for group, names in groups:
            names = [name for name in names if name not in HIDDEN_NAV_ENTRIES]
            if not names:            # 整组都收起来了就不要再留一个空标题
                continue
            header = QListWidgetItem(group)
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.UserRole, None)
            header.setForeground(QColor(MUTED))
            self.nav_list.addItem(header)
            self._nav_rows.append((self.nav_list.count() - 1, None))
            for name in names:
                index = titles.get(name)
                if index is None or name in HIDDEN_NAV_ENTRIES:
                    continue
                item = QListWidgetItem(self.tabs.tabIcon(index), name)
                item.setData(Qt.ItemDataRole.UserRole, index)
                self.nav_list.addItem(item)
                self._nav_rows.append((self.nav_list.count() - 1, index))
        self.nav_list.currentRowChanged.connect(self._on_nav_row)
        self.tabs.currentChanged.connect(self._sync_nav_row)
        self._sync_nav_row(self.tabs.currentIndex())
        # 切到人设/插件页时自动拉一次最新数据
        self.tabs.currentChanged.connect(self._on_tab_changed)
        content_layout.addWidget(self.tabs)

        # 调整大小手柄
        grip = QSizeGrip(self)
        grip.setFixedSize(16, 16)
        grip_layout = QHBoxLayout()
        grip_layout.setContentsMargins(0, 0, 4, 4)
        grip_layout.addStretch()
        grip_layout.addWidget(grip)
        content_layout.addLayout(grip_layout)

        # 侧栏样式：白底卡片 + 选中项青色高亮
        # 侧栏沿用原 UI 配色（浅色卡片 + 原主题边框/强调色）
        self.sidebar.setStyleSheet(
            "QWidget#Sidebar { background: transparent; border: 0; }"
            "QLabel#SidebarTitle { font-size: 15px; font-weight: bold; }"
            f"QLabel#SidebarMuted {{ color: {MUTED}; font-size: 11px; }}"
            "QListWidget#NavList { background: transparent; border: 0; outline: 0;"
            f" color: {TEXT}; font-size: 13px; }}"
            "QListWidget#NavList::item { height: 32px; border-radius: 7px;"
            " padding-left: 8px; margin: 1px 4px; }"
            f"QListWidget#NavList::item:selected {{ background: #DFF3F0; color: {ACCENT};"
            f" border-left: 3px solid {ACCENT}; font-weight: bold; }}"
            "QListWidget#NavList::item:hover { background: #EEF3F8; }"
            f"QListWidget#NavList::item:disabled {{ color: {MUTED}; height: 24px;"
            " padding-left: 6px; font-size: 11px; }")

        # 样式表
        self._apply_panel_transparency(self._cfg.get("panel_transparency", 0.35))

        # 连接信号
        self.console_tab.btn_start.clicked.connect(lambda: self._action("start"))
        self.console_tab.btn_stop.clicked.connect(lambda: self._action("stop"))
        self.console_tab.btn_test_ai.clicked.connect(lambda: self._action("test-ai"))
        self.console_tab.btn_test_qq.clicked.connect(lambda: self._action("test-qq"))
        self.console_tab.btn_capabilities.clicked.connect(self._check_capabilities)
        self.settings_tab.btn_save.clicked.connect(self._save_settings)
        self.settings_tab.btn_models.clicked.connect(self._fetch_models)
        self.settings_tab.btn_diagnose.clicked.connect(self._diagnose_ai)
        self.settings_tab.btn_speed.clicked.connect(self._speed_test_ai)
        self.settings_tab.btn_fast.clicked.connect(self._apply_fast_profile)
        self.appearance_tab.appearance_changed.connect(self._apply_appearance)
        self.appearance_tab.load(self._cfg)
        # 刷新定时器
        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh)
        self._timer.start(3000)
        from src.gui.tray import RobotTray
        self._tray=RobotTray(self,app_icon(),available=False if any(flag in sys.argv for flag in ('--self-test','--native-self-test')) else None)
        # 首次创建原生窗口之前就给出边界；只在 showEvent 中设会被 Qt 初始布局覆盖。
        self._fit_screen()

        # 启动后端
        if "--no-backend" in sys.argv or not AUTO_BACKEND:
            self.console_tab.set_notice(
                "已禁用后端自动启动（%s）：仅调试界面"
                % ("--no-backend" if "--no-backend" in sys.argv else "离屏测试环境"), ok=True)
        else:
            QTimer.singleShot(500, self._prepare_backend)
            QTimer.singleShot(2000, self._load_settings)
            # 启动 12 秒后再查更新，别和首屏/后端启动抢时间；只检查不自动装。
            QTimer.singleShot(12000, self.about_tab.auto_check_update)

    # ── 后端启动：单实例接管 / 僵尸清理 / 端口冲突说明 ──────────────────────
    def _prepare_backend(self):
        """决定怎么用控制台端口上的后台。

        用户报过"更新后打不开程序"，实测原因是：上一次的控制台**窗口关了、进程没退**
        （或者用户又双击了一次），旧后台一直占着控制台端口；新开的窗口只显示一句
        "检测到旧版或其他后台占用端口"，看起来就是打不开。这里改成：
        是我们的后台就**请它退出后自己接管**（或直接复用它），是别人的进程才提示。

        注意：这个方法由 QTimer 调用（Qt 槽），**任何异常都不能放出去** ——
        PyQt 在槽里遇到未捕获异常会直接 abort 整个进程（实测把测试跑成 0xC0000409）。
        """
        try:
            self._prepare_backend_inner()
        except Exception as exc:
            try:
                self.console_tab.set_notice(f"后台启动检查失败（不影响界面使用）：{exc}", ok=False)
            except Exception:
                pass

    def _prepare_backend_inner(self):
        probe = backend_probe()
        plan = plan_backend_start(probe)
        if plan == 'start':
            self._start_backend()
            return
        version = (probe or {}).get('version') or '未知'
        if plan == 'focus':
            reuse = ask_confirm(
                self, '已经有一个控制台在运行',
                f'检测到另一个控制台（版本 {version}）正在运行，它占着本机 {PORT} 端口。',
                detail='「直接使用它」= 本窗口连上那个后台（配置与聊天记录都在它手里）；\n'
                       '「结束它并重启」= 让旧后台退出，再由本窗口重新启动后台。',
                ok_text='直接使用它', cancel_text='结束它并重启')
            if reuse:
                self.console_tab.set_notice(f'已连上正在运行的后台（版本 {version}）', ok=True)
                return
            self._takeover_backend()
            return
        if plan == 'takeover':
            if not ask_confirm(
                    self, '清理上次留下的后台',
                    f'检测到上次留下的后台进程（版本 {version}，没有窗口）还占着 {PORT} 端口。',
                    detail='它通常是"窗口关了但进程没退"的残留。结束它不会丢配置和聊天记录，'
                           '那些都存在数据目录里。',
                    ok_text='结束并重新启动'):
                self.console_tab.set_notice('已取消：后台仍是旧的那个，控制台可能无法连接', ok=False)
                return
            self._takeover_backend()
            return
        # 是我们自己的后台、但配置目录不是当前安装目录：同样可以接管（请它退出再启动），
        # 不要只丢一句"请关闭旧后台"（用户找不到怎么关）。
        if probe and str(probe.get('app') or '') == APP_ID:
            current_root = str(ROOT.resolve())
            probe_root = str(probe.get('data_root') or '')
            if probe_root and os.path.normcase(probe_root) != os.path.normcase(current_root):
                if ask_confirm(
                        self, '后台用了其他目录的配置',
                        f"端口 {PORT} 上的后台来自另一个目录：\n{probe_root}\n\n"
                        f"当前程序目录：\n{current_root}",
                        detail='「接管并重启」= 让旧后台退出，再由本程序用当前目录重新启动；'
                               '配置与聊天记录都在各自目录里，不会被删除。',
                        ok_text='接管并重启', cancel_text='先不动'):
                    self._takeover_backend()
                    return
                self.console_tab.set_notice('已取消：后台仍是另一个目录的，设置页可能显示的不是当前配置', ok=False)
                return
        # 别人的进程占着端口：说清楚，别静默退出（用户会以为"程序打不开"）
        detail = (f"占用者：{probe.get('app') or '未知程序'}"
                  f"（版本 {version}，PID {probe.get('pid') or '?'}）" if probe else
                  f"占用者：未知程序（{PORT} 端口无响应）")
        tell(self, f'{PORT} 端口被其他程序占用',
             f'控制台需要本机 {PORT} 端口来跑后台服务，但现在被别的程序占着。\n'
             '请关闭占用它的程序（或改端口）后重新打开控制台。',
             detail=detail, kind='warning')
        self.console_tab.set_notice(f'后台未能启动：{PORT} 端口被其他程序占用', ok=False)

    def _takeover_backend(self):
        """请旧后台自己退出，等端口空出来，再启动我们自己的后台。"""
        ok = request_backend_quit()
        freed = wait_port_free(timeout=8.0)
        if not freed:
            self.console_tab.set_notice(
                '旧后台没有在 8 秒内退出；请到任务管理器结束 WhaleGirlQQ.exe 后重开控制台', ok=False)
            return
        self.console_tab.set_notice('旧的 %s后台已退出，正在启动新的后台…' % ('' if ok else '（未响应请求的）'),
                                    ok=True)
        self._start_backend()

    # ── 无边框窗口的边缘拖拽缩放 ────────────────────────────────────────────
    # 用户要求"窗口要可以自动调节大小"。无边框窗口默认只有右下角那个小三角能拖，
    # 而且很多皮肤下几乎看不见；这里让四条边和四个角都能拖（用 Qt 自己的系统级
    # 缩放，DPI / 多屏都正确），右下角的小三角仍旧保留。
    EDGE = 6            # 边缘判定宽度（逻辑像素）

    def _resize_border_width(self):
        # 外层透明留白会让 Windows 鼠标穿透；命中区必须伸进可见内容至少 EDGE 像素。
        margins = self.layout().contentsMargins()
        return self.EDGE + max(margins.left(), margins.top(), margins.right(), margins.bottom())

    def _edges_at(self, pos) -> Qt.Edge | None:
        """给一个窗口内坐标，返回命中的边（角返回两条边的或），没命中返回 None。"""
        rect = self.rect()
        edge = self._resize_border_width()
        left = pos.x() <= edge
        right = pos.x() >= rect.width() - edge
        top = pos.y() <= edge
        bottom = pos.y() >= rect.height() - edge
        if not (left or right or top or bottom):
            return None
        result = None
        if left:
            result = Qt.Edge.LeftEdge
        elif right:
            result = Qt.Edge.RightEdge
        if top:
            result = (result | Qt.Edge.TopEdge) if result else Qt.Edge.TopEdge
        elif bottom:
            result = (result | Qt.Edge.BottomEdge) if result else Qt.Edge.BottomEdge
        return result

    def _cursor_for_edges(self, edges):
        if edges is None:
            return Qt.CursorShape.ArrowCursor
        left = bool(edges & Qt.Edge.LeftEdge)
        right = bool(edges & Qt.Edge.RightEdge)
        top = bool(edges & Qt.Edge.TopEdge)
        bottom = bool(edges & Qt.Edge.BottomEdge)
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _resize_zone(self, obj, event, press: bool = False) -> bool:
        """在窗口边缘按下/移动时的缩放处理；返回 True 表示事件已被消化。"""
        if self.isMaximized():
            return False
        try:
            pos = obj.mapTo(self, event.position().toPoint())
        except Exception:
            return False
        edges = self._edges_at(pos)
        if press:
            if edges is None:
                return False
            handle = self.windowHandle()
            if handle is not None and hasattr(handle, 'startSystemResize'):
                return bool(handle.startSystemResize(edges))
            return False
        self.setCursor(self._cursor_for_edges(edges))
        return False

    def eventFilter(self, obj, event):
        if obj is getattr(self, '_container', None):
            kind = event.type()
            if kind == QEvent.Type.MouseMove:
                self._resize_zone(obj, event)
            elif kind == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton and self._resize_zone(obj, event, press=True):
                    return True
            elif kind == QEvent.Type.Leave:
                self.setCursor(Qt.CursorShape.ArrowCursor)
        return super().eventFilter(obj, event)

    def _fit_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None or self.isMaximized():
            return
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        usable_w = max(1, area.width() - max(0, frame.width() - self.width()))
        usable_h = max(1, area.height() - max(0, frame.height() - self.height()))
        self.setMinimumSize(min(800, usable_w), min(500, usable_h))
        width, height = min(self.width(), usable_w), min(self.height(), usable_h)
        self.resize(width, height)
        self.move(max(area.left(), min(self.x(), area.right() - self.frameGeometry().width() + 1)),
                  max(area.top(), min(self.y(), area.bottom() - self.frameGeometry().height() + 1)))

    def nativeEvent(self, event_type, message):
        if os.name == 'nt' and not getattr(self, 'safe_mode', False) and not self.isMaximized():
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x84:  # WM_NCHITTEST：无边框窗口也支持四边和四角缩放。
                rect = wintypes.RECT()
                # 不在原生消息回调里调用 winId() 创建/重入窗口；HWND 在 64 位下不能截成 int。
                get_rect = ctypes.WinDLL('user32', use_last_error=True).GetWindowRect
                get_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
                get_rect.restype = wintypes.BOOL
                if not get_rect(msg.hWnd, ctypes.byref(rect)):
                    return False, 0
                x, y = ctypes.c_short(msg.lParam & 0xffff).value, ctypes.c_short((msg.lParam >> 16) & 0xffff).value
                edge = max(6, round(self._resize_border_width() * self.devicePixelRatioF()))
                left, right = x < rect.left + edge, x >= rect.right - edge
                top, bottom = y < rect.top + edge, y >= rect.bottom - edge
                hit = (13 if left else 14 if right else 12) if top else ((16 if left else 17 if right else 15) if bottom else (10 if left else 11 if right else 0))
                if hit:
                    return True, hit
        # 未处理的消息直接交还 Qt。实测在 Windows 回调中再调 SIP 包装的基类
        # nativeEvent 会在 window.show() 时访问违规，连系统边框兼容模式也会崩。
        return False, 0

    def configure_conversation(self,pid):
        data=api_get('plugins');plugins=data if isinstance(data,list) else data.get('plugins',[])
        plugin=next((p for p in plugins if p['id']==pid),None)
        if plugin:PluginConfigDialog(plugin,self).exec()

    @safe_slot(label='功能与会话人设')
    def show_features(self):
        from src.gui.features import FeaturesDialog
        FeaturesDialog(api_get,api_post,self.configure_conversation,self).exec()

    def _assistant_navigate(self, page):
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == page:
                self.tabs.setCurrentIndex(index)
                break

    # ── 多账号（切片 1：单进程内切换）──────────────────────────────
    def open_manager(self):
        """打开统一接入控制台（管理器）。"""
        from src.gateway_link import manager_exe
        exe = manager_exe()
        if not exe:
            tell(self, '没找到管理器', '请先安装 WhaleQQGateway（QQ 统一接入控制台）。',
                 kind='warning')
            return
        try:
            import subprocess
            subprocess.Popen([exe], close_fds=True)
            if '已连接' not in self.manager_status.text():
                self._start_manager_retry()
                QTimer.singleShot(2500, self._retry_manager_connection)
        except Exception as error:
            tell(self, '打开管理器失败', f'{type(error).__name__}: {error}', kind='warning')

    def _connect_manager_account(self, account_id: str):
        """切到某个管理器账号：让网关拉起无窗口后端，再把控制台指过去。"""
        from src.gateway_link import read_connection, start_headless_bot
        connection = read_connection()
        if not connection:
            self.account_hint.setText('管理器未连接，无法切换账号。')
            return
        self.account_hint.setText('正在启动该账号的机器人后端…')
        source = str(ROOT)
        import threading

        def worker():
            result = start_headless_bot(connection, account_id, source)
            QTimer.singleShot(0, lambda: self._manager_backend_ready(account_id, result))

        threading.Thread(target=worker, name='manager-bot', daemon=True).start()

    def _manager_backend_ready(self, account_id: str, result: dict):
        if not result.get('ok'):
            self.account_hint.setText('启动该账号后端失败：' + str(result.get('message') or '未知原因'))
            return
        port = int(result.get('port') or 0)
        url = f'http://127.0.0.1:{port}'
        import qq_gui as _self
        _self.BASE_URL = url
        self.base_url = url
        self.account_hint.setText(f'已切换到管理器账号（后端端口 {port}）')
        # 等后端起来再刷新各页
        QTimer.singleShot(800, self._refresh_after_switch)

    def _refresh_after_switch(self):
        try:
            self._refresh()
            self._load_settings()
            self.plugin_tab.refresh()
            self.persona_tab.refresh()
        except Exception:
            pass

    def _bootstrap_accounts(self):
        """首次显示时先问管理器要账号；管理器不在才退回本地账号簿。

        如果是管理器直接拉起的（launch.json），账号已经由管理器指定：
        这里只显示一行说明，不列账号、不建本地账号。
        """
        # 后台刚起来时插件列表可能还没就绪：显示后补刷一次，避免插件页空白。
        QTimer.singleShot(1500, self._refresh_plugins_safely)
        if getattr(self, '_launch_job', None):
            label = str(self._launch_job.get('label') or '当前账号')
            self.manager_status.setText(f'由管理器管理：{label}')
            for widget in (self.account_combo, self.account_add, self.account_rename,
                           self.account_remove, self.manager_button):
                widget.setVisible(False)
            self.account_hint.setText('中继与 QQ 由管理器负责，这里只管机器人回复')
            return
        connected = False
        try:
            self.settings_tab.manager_url.setText(str(self._cfg.get('manager_base_url') or ''))
            connected = self.load_manager_accounts()
            if not connected:
                self._reload_accounts_async()
        except Exception:
            self._reload_accounts_async()
        if connected:
            self._stop_manager_retry()
        else:
            self._start_manager_retry()

    def _refresh_plugins_safely(self):
        try:
            self.plugin_tab.refresh()
        except Exception:
            pass

    def _sync_account_combo_visibility(self):
        """没有账号时不要把空下拉框留在侧栏里（截图里那个空白小方块）。"""
        try:
            self.account_combo.setVisible(self.account_combo.count() > 0)
        except Exception:
            pass

    def _ensure_manager_retry_timer(self):
        """管理器可能比控制台晚启动：用一个轻量定时器在未连接时自动重试。"""
        timer = getattr(self, '_manager_retry_timer', None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(8000)
            timer.timeout.connect(self._retry_manager_connection)
            self._manager_retry_timer = timer
        return timer

    def _start_manager_retry(self):
        if getattr(self, '_launch_job', None):
            return
        timer = self._ensure_manager_retry_timer()
        if not timer.isActive():
            timer.start()
        try:
            text = self.manager_status.text()
            if '自动重试' not in text:
                self.manager_status.setText(text + ' · 正在自动重试…')
        except Exception:
            pass

    def _stop_manager_retry(self):
        timer = getattr(self, '_manager_retry_timer', None)
        if timer is not None:
            timer.stop()

    def _retry_manager_connection(self):
        """定时器回调：只在未连接时跑，连上就停表。"""
        if getattr(self, '_launch_job', None):
            self._stop_manager_retry()
            return
        try:
            if self.load_manager_accounts(timeout=1.0, retrying=True):
                self._stop_manager_retry()
        except Exception:
            pass

    def load_manager_accounts(self, timeout: float = 3.0, retrying: bool = False):
        """从管理器读账号（只读）；失败就退回本地账号簿。

        优先用设置页里手填的地址/令牌；没填就读管理器写下的 connection.json。
        retrying=True 时状态文案会说明“正在自动重试”，避免用户以为卡死。
        """
        from src.gateway_link import fetch_accounts, read_connection
        configured = {'base_url': str(self._cfg.get('manager_base_url') or ''),
                      'token': str(self._cfg.get('manager_token') or '')}
        connection = configured if configured['base_url'] else read_connection()
        suffix = ' · 正在自动重试…' if retrying else ''
        if not connection:
            self.manager_status.setText('未连接管理器（账号以管理器为准）' + suffix)
            try:
                self.settings_tab.set_manager_status(
                    '未连接管理器：点「打开管理器」启动并扫码登录'
                    + ('（会自动重试）' if retrying else ''))
            except Exception:
                pass
            self.manager_button.setVisible(True)
            return False
        result = fetch_accounts(connection, timeout=timeout)
        if not result.get('ok'):
            self.manager_status.setText(
                '管理器不可达：' + str(result.get('message') or '') + suffix)
            self.manager_button.setVisible(True)
            return False
        items = result.get('items') or []
        online = sum(1 for item in items if item.get('send_ready') or item.get('state') == 'online')
        self.manager_status.setText(
            f'管理器已连接 · {len(items)} 个账号 · {online} 个在线')
        try:
            self.settings_tab.set_manager_status(
                f'管理器已连接：{len(items)} 个账号 · {online} 个在线')
        except Exception:
            pass
        self.manager_button.setVisible(True)
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for item in items:
            label = str(item.get('label') or item.get('account_id') or '账号')
            state = '在线' if (item.get('send_ready') or item.get('state') == 'online') else '离线'
            self.account_combo.addItem(f'{label} · {state}', 'manager:' + str(item.get('account_id')))
        self.account_combo.blockSignals(False)
        self._sync_account_combo_visibility()
        self.account_hint.setText('账号来自管理器；切换只影响本控制台查看的账号')
        return True

    def _reload_accounts_async(self):
        """后台线程取账号列表，回到主线程再更新界面（不阻塞 UI）。"""
        import threading

        def worker():
            try:
                data = api_get("accounts") or {}
            except Exception:
                data = {}
            QTimer.singleShot(0, lambda: self._apply_accounts(data))

        threading.Thread(target=worker, name='accounts-load', daemon=True).start()

    def _apply_accounts(self, data: dict):
        # 后台线程可能在窗口已经销毁之后才回来（测试里尤其明显）：
        # 先确认对象还活着，否则会去碰已删除的 C++ 控件。
        try:
            from PyQt6 import sip
            if sip.isdeleted(self) or sip.isdeleted(self.account_combo):
                return
        except Exception:
            pass
        items = list((data or {}).get("items") or [])
        active = str((data or {}).get("active") or "")
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for item in items:
            label = str(item.get("label") or item.get("id"))
            port = int(item.get("port") or 0)
            running = bool(item.get("running"))
            suffix = f" · {port} · {'运行中' if running else '未启动'}" if port else ""
            self.account_combo.addItem(label + suffix, item.get("id"))
        index = self.account_combo.findData(active)
        if index >= 0:
            self.account_combo.setCurrentIndex(index)
        self.account_combo.blockSignals(False)
        self._sync_account_combo_visibility()
        self.account_hint.setText(
            f"{len(items)} 个账号 · 当前请求：{BASE_URL}" if items else "未读取到账号")
        self.account_remove.setEnabled(len(items) > 1)

    def _reload_accounts(self):
        data = api_get("accounts") or {}
        items = data.get("items") or []
        active = str(data.get("active") or "")
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for item in items:
            self.account_combo.addItem(str(item.get("label") or item.get("id")), item.get("id"))
        index = self.account_combo.findData(active)
        if index >= 0:
            self.account_combo.setCurrentIndex(index)
        self.account_combo.blockSignals(False)
        self._sync_account_combo_visibility()
        self.account_hint.setText(f"{len(items)} 个账号 · 当前生效：{self.account_combo.currentText()}")
        self.account_remove.setEnabled(len(items) > 1)

    def _hub_request(self, method: str, path: str, payload: dict | None = None,
                     timeout: float = 60.0) -> dict:
        """固定问控制台自己的后台：账号列表与"启动某账号后台"都由它管。"""
        url = f"http://127.0.0.1:{PORT}/api/{path}"
        try:
            if method == "get":
                response = local_request("get", url, timeout=timeout)
            else:
                response = local_request("post", url, json=payload or {}, timeout=timeout)
            data = response.json() if response.content else {}
            return data if isinstance(data, dict) else {}
        except Exception as error:
            return {"success": False, "message": f"{type(error).__name__}: {error}"}

    def _point_at(self, account_id: str, port: int) -> bool:
        """把控制台请求地址切到该账号的端口，并等它就绪。"""
        global BASE_URL
        url = f"http://127.0.0.1:{port}"
        if url != BASE_URL:
            BASE_URL = url
            try:
                import qq_gui as _self
                _self.BASE_URL = url
            except Exception:
                pass
        for _ in range(120):
            try:
                if not backend_error(timeout=0.5):
                    return True
            except Exception:
                pass
            QTimer.singleShot(0, lambda: None)
            self.spin_events(60)
        return not backend_error(timeout=1)

    def spin_events(self, milliseconds: int):
        from PyQt6.QtCore import QEventLoop
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def _on_account_chosen(self, _index=0):
        account_id = self.account_combo.currentData()
        if not account_id:
            return
        if str(account_id).startswith('manager:'):
            self._connect_manager_account(str(account_id).split(':', 1)[1])
            return
        items = getattr(self, '_accounts_cache', []) or []
        entry = next((item for item in items if item.get('id') == account_id), {})
        port = int(entry.get('port') or (PORT if account_id == 'default' else 0))
        if account_id != 'default' and not entry.get('running'):
            started = self._hub_request('post', 'accounts/start', {'id': account_id})
            if started.get('success') is False:
                tell(self, '启动账号后台失败', str(started.get('message') or '未知原因'),
                 kind='warning')
                return
            port = int(((started.get('server') or {}).get('port')) or port)
        if account_id == 'default':
            port = port or PORT
        if port and not self._point_at(account_id, port):
            tell(self, '账号后台没就绪',
                 f'端口 {port} 上的后台没有在预期时间内响应。', kind='warning')
            return
        result = self._hub_request('post', 'accounts/select', {'id': account_id}) or {}
        if result.get('success') is False:
            tell(self, "切换失败", str(result.get("message") or "未知原因"), kind='warning')
            return
        # 换账号后各页数据都要重取，否则看到的是上一个账号的。
        # 注意顺序：_refresh() 里会重新拉账号列表并把提示覆盖掉，所以提示放在最后。
        self._refresh()
        self._load_settings()
        try:
            self.persona_tab.refresh()
        except Exception:
            pass
        try:
            self.plugin_tab.refresh()
        except Exception:
            pass
        self._reload_accounts()
        self.account_hint.setText(
            f"已切换到：{self.account_combo.currentText()}（独立配置与聊天记录）")

    def _add_account(self):
        accounts = self._hub_request('get', 'accounts').get('items') or []
        dialog = AccountCreateDialog(accounts, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        created = self._hub_request('post', 'accounts',
                                    {'label': values['label'],
                                     'clone_from': values['clone_from']}) or {}
        if created.get("success") is False:
            tell(self, "新建失败", str(created.get("message") or "未知原因"), kind='warning')
            return
        self._reload_accounts()
        account_id = (created.get("item") or {}).get("id")
        if account_id and values.get('switch', True):
            index = self.account_combo.findData(account_id)
            if index >= 0:
                self.account_combo.setCurrentIndex(index)
                self._on_account_chosen()

    def _rename_account(self):
        account_id = self.account_combo.currentData()
        if not account_id:
            return
        label, ok = ask_text(self, '重命名账号', '新的备注：', default=self.account_combo.currentText())
        if not ok:
            return
        api_post("accounts/rename", {"id": account_id, "label": label})
        self._reload_accounts()

    def _remove_account(self):
        account_id = self.account_combo.currentData()
        if not account_id:
            return
        name = self.account_combo.currentText()
        if QMessageBox.question(self, "删除账号",
                                f"确定从列表里删除「{name}」吗？\n"
                                "只删账号条目，聊天记录与配置目录会保留。") != QMessageBox.StandardButton.Yes:
            return
        result = api_post("accounts/remove", {"id": account_id}) or {}
        if result.get("success") is False:
            tell(self, "删除失败", str(result.get("message") or "未知原因"), kind='warning')
            return
        self._reload_accounts()
        self._on_account_chosen()

    def _on_nav_row(self, row: int):
        """左侧导航点击：分组标题不可选，回退到上一项。"""
        target = None
        for nav_row, tab_index in getattr(self, '_nav_rows', []):
            if nav_row == row:
                target = tab_index
                break
        if target is None:
            self._sync_nav_row(self.tabs.currentIndex())
            return
        self.tabs.setCurrentIndex(target)

    def _sync_nav_row(self, tab_index: int):
        """标签页变化时，把左侧选中项同步过去（不触发信号死循环）。"""
        for nav_row, candidate in getattr(self, '_nav_rows', []):
            if candidate == tab_index:
                self.nav_list.blockSignals(True)
                self.nav_list.setCurrentRow(nav_row)
                self.nav_list.blockSignals(False)
                return

    def _show_hidden_pages(self):
        """面板跳转菜单：侧栏现在直出全部页面，这里留一个统一的弹出入口。

        菜单必须显式套浅色样式（style_menu）：QMenu 是独立顶层窗口，
        系统深色模式下没样式就是黑框暗字，用户根本看不清。
        """
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        style_menu(menu)
        titles = [self.tabs.tabText(index) for index in range(self.tabs.count())]
        for name in (HIDDEN_NAV_ENTRIES or tuple(titles)):
            action = menu.addAction(f'{name}…')
            action.triggered.connect(
                lambda _checked=False, page=name: self._assistant_navigate(page))
        menu.exec(self.nav_list.mapToGlobal(self.nav_list.rect().center()))

    def _on_tab_changed(self, index: int):
        """切到人设/插件页时自动拉一次最新数据（后端可能刚启动完成）。"""
        widget = self.tabs.widget(index)
        if widget is self.persona_tab:
            self.persona_tab.refresh()
        elif widget is self.memory_tab or widget is self.pc_tab:
            widget.refresh()
        elif widget is self.plugin_tab:
            self.plugin_tab.refresh()

    def changeEvent(self, event):
        """窗口状态变化（最大化/还原/最小化）后同步按钮符号与边距。

        双击标题栏、Win+↑、拖到屏幕边缘这类"系统发起"的最大化不会走 _toggle_max，
        以前按钮符号和内部标记都不同步，看着像"点不动"。
        """
        try:
            super().changeEvent(event)
        except Exception:
            pass
        if event.type() in (event.Type.WindowStateChange, event.Type.ActivationChange):
            try:
                self.title_bar._sync_max_glyph()
            except Exception:
                pass
            if getattr(self, "_maximized", None) is not None:
                self._maximized = self.isMaximized()

    def showEvent(self, event):
        super().showEvent(event)
        # 原来类里有两个 showEvent，后者覆盖了屏幕适配；窗口显示完成后再调整尺寸。
        QTimer.singleShot(0, self._fit_screen)
        # 账号以管理器为准：只在第一次显示时拉一次，失败自动退回本地账号簿。
        if not getattr(self, '_accounts_bootstrapped', False):
            self._accounts_bootstrapped = True
            QTimer.singleShot(50, self._bootstrap_accounts)
        # 银白主题：DWM 用亮色模式，背板按模糊度挑（0% 模糊 = 不用背板）。
        if not getattr(self, 'safe_mode', False):
            self.setWindowOpacity(window_opacity_from_transparency(self._cfg.get("transparency", 0.0)))
        self._apply_backdrop()
        self._sync_frame_margin()
        try:
            self.title_bar._sync_max_glyph()
        except Exception:
            pass
        if getattr(self, 'safe_mode', False) and not getattr(self, '_safe_notice_shown', False):
            self._safe_notice_shown = True
            try:
                self.console_tab.set_notice(
                    '已用「兼容模式」启动：关闭了无边框/半透明/云母/背景图（远程桌面与虚拟显示环境更稳）。'
                    '想恢复外观可在「外观」页取消勾选后重启控制台。', ok=True)
            except Exception:
                pass

    def _sync_frame_margin(self):
        """按窗口是否真的透明决定外层边距（透明才留 6px 给圆角/阴影）。

        白色边框的来源：无边框窗口外层留了 6px 边距，容器（背景图）只铺在边距以内；
        当「窗口透明度 = 0%」时窗口几乎不透明，这 6px 就露出窗口默认底色 —— 看起来就是白框。
        顺带：容器铺满窗口后，四边/四角的缩放命中区才和窗口边缘对齐。
        """
        if getattr(self, "safe_mode", False):
            return
        try:
            opaque = self.windowOpacity() >= 0.995
        except Exception:
            opaque = True
        margin = 0 if opaque else 6
        layout = self.layout()
        if layout is None:
            return
        current = layout.contentsMargins()
        if current.left() == margin and current.top() == margin:
            return
        layout.setContentsMargins(margin, margin, margin, margin)
        # 边距改了要让容器重新铺满，否则背景图仍停在旧位置
        try:
            self._container.setGeometry(self.rect().adjusted(margin, margin, -margin, -margin))
            self._container.update()
        except Exception:
            pass

    def _apply_backdrop(self):
        if getattr(self, 'safe_mode', False):
            return          # 安全模式不碰 DWM/云母：那正是远程+虚拟显示下崩得最凶的一段
        blur = float(self._cfg.get("blur", 0.70) or 0.0)
        # 有自定义背景图时关掉云母：图已经盖满窗口，云母叠在下面只会发灰并增加合成开销；
        # 纯色底时才用云母/亚克力，玻璃感更干净。
        has_image = bool(str(self._cfg.get("background_image") or "").strip())
        backdrop = BACKDROP_NONE if has_image else backdrop_for_blur(blur)
        dark = False  # 银白主题固定亮色
        def _mica() -> None:
            try:
                handle = int(self.winId())
            except (RuntimeError, AttributeError, TypeError):
                return      # 窗口已销毁：迟到的一次性回调直接丢弃，不再碰 DWM
            apply_mica(handle, dark=dark, backdrop=backdrop)

        QTimer.singleShot(100, _mica)

    def _apply_bg(self, color: str, image: str = "", blur: float = None,
                  transparency: float = None, blur_explicit: bool = None):
        """底色、背景图、模糊度与填充透明度都交给容器自绘（样式表不支持
        background-size，也读不到中文/空格路径的图片）。"""
        if blur is None:
            blur = float(self._cfg.get("blur", 0.70) or 0.0)
        if transparency is None:
            transparency = float(self._cfg.get("transparency", 0.0) or 0.0)
        if blur_explicit is None:
            blur_explicit = bool(self._cfg.get("blur_explicit", False))
        self._container.setStyleSheet("#container { border-radius: 0px; }")
        self._container.set_background_color(color or BG)
        resolved = resolve_background(image, ROOT) if image else None
        pixmap = QPixmap(str(resolved)) if resolved is not None else None
        if pixmap is not None and pixmap.isNull():
            pixmap = None
        self._container.set_background_pixmap(pixmap)
        self._container.set_blur(blur, blur_explicit)
        # 填充透明度 = 1 - 窗口透明度：否则调高透明度只会穿透到桌面被不透明的容器挡住
        self._container.set_fill_alpha(1.0 - min(max(transparency, 0.0), 1.0))

    def _apply_panel_transparency(self, value):
        panel = min(max(float(value), 0.0), 1.0)
        # 有背景图时面板白底要让路给照片（panel_alpha 里的玻璃指数），否则整片
        # 65% 的白色会把图糊成一层纱。是否让路由容器自己那次真正画图决定。
        has_image = self._container.has_background_image()
        alpha_f = panel_alpha(panel, has_image)
        alpha = round(255 * alpha_f)
        self.setStyleSheet(STYLESHEET.replace(f"background: {CARD};", f"background: rgba(255, 255, 255, {alpha});"))
        self.console_tab.apply_panel_alpha(alpha_f)
        # 「对话与记忆」「机器人管理」两页自带样式表，主窗口这条替换规则到不了它们，
        # 必须显式把同一个透明度推下去，否则那两页永远是白纸一张（背景图看不见）。
        for tab in (self.memory_tab, self.pc_tab, self.assistant_tab):
            try:
                tab.apply_panel_alpha(alpha_f, glass=has_image)
            except TypeError:          # 老页面只收一个参数
                try:
                    tab.apply_panel_alpha(alpha_f)
                except Exception:
                    pass
            except Exception:
                pass

    def _apply_appearance(self, values: dict):
        values = values or {}
        transparency = float(values.get("transparency", self._cfg.get("transparency", 0.0)) or 0.0)
        blur = float(values.get("blur", self._cfg.get("blur", 0.70)) or 0.0)
        blur_explicit = bool(values.get("blur_explicit", self._cfg.get("blur_explicit", False)))
        color = values.get("bg_color", self._cfg.get("bg_color", BG))
        image = values.get("background_image", self._cfg.get("background_image", ""))

        self.setWindowOpacity(window_opacity_from_transparency(transparency))
        self._sync_frame_margin()
        self._apply_bg(color, image, blur, transparency, blur_explicit)
        panel = float(values.get("panel_transparency", self._cfg.get("panel_transparency", 0.35)))
        self._apply_panel_transparency(panel)
        self._cfg["panel_transparency"] = panel
        self._cfg["transparency"] = transparency
        self._cfg["blur"] = blur
        self._cfg["blur_explicit"] = blur_explicit
        self._cfg["bg_color"] = color
        self._cfg["background_image"] = image
        if "background_image" in values:
            # 用户在外观页显式换图/清除后，不再自动塞回内置背景。
            self._cfg["default_background"] = False
        wanted_safe = bool(values.get("safe_mode", self._cfg.get("safe_mode", False)))
        if wanted_safe != bool(self._cfg.get("safe_mode", False)):
            self._cfg["safe_mode"] = wanted_safe
            self.console_tab.set_notice(
                "兼容模式已%s：请关闭并重新打开控制台后生效。" % ("开启" if wanted_safe else "关闭"),
                ok=True)
        self._apply_backdrop()
        try:
            save_gui_config(self._cfg_path, self._cfg)
        except OSError as exc:
            self.console_tab.set_notice(f"外观设置保存失败：{exc}", ok=False)

    def _start_backend(self):
        """在后台启动 FastAPI 服务（如果还没运行）。

        两种运行方式差别很大：
        - 直接跑脚本：用 `sys.executable -m uvicorn ...` 起一个子进程；
        - 打包成 exe：`sys.executable` 就是**这个 exe 自己**，再 spawn 一次等于又开一个
          控制台（后端永远不会起来）。所以冻结运行时改为**在进程内**用 uvicorn 起服务。
        """
        if self._backend_ready():
            return
        try:
            local_request('get', f'{BASE_URL}/api/status',timeout=1)
        except Exception:
            pass
        else:
            self.console_tab.set_notice(backend_error(),ok=False)
            return
        if FROZEN:
            self._start_backend_inprocess()
            return
        try:
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            # 把控制台自己的 PID 传给后台：控制台崩溃/被强杀后，后台能自己退出，
            # 不会再留一个"没有窗口却占着控制台端口"的孤儿（下次打开会弹清理提示）。
            child_env = dict(os.environ)
            child_env["WHALEGIRL_PARENT_PID"] = str(os.getpid())
            # 端口也要传给后台：/api/accounts 里"默认账号"报的就是控制台端口。
            child_env[CONSOLE_PORT_ENV] = str(PORT)
            self._backend_proc = subprocess.Popen(
                backend_argv(PORT),
                cwd=str(ROOT),
                creationflags=flags,
                env=child_env,
            )
            self._write_console_endpoint()
        except Exception as e:
            self.console_tab.set_notice(f"后端启动失败：{e}", ok=False)

    @staticmethod
    def _write_console_endpoint():
        """把本次真实监听的端口写进 <data>/qq-only/console_endpoint.json。

        外部 MCP（tools/whale_bot_mcp.py）运行期读这份文件跟端口，用户改端口不用重跑登记。
        数据目录与后台同一个口径（ROOT/"data"）；写失败只记日志，绝不挡住控制台启动。
        """
        if write_console_endpoint(PORT, ROOT / "data") is None:
            _LOG.warning("控制台端点文件没写成（端口 %s），外部工具只能退回默认端口", PORT)

    @staticmethod
    def _backend_ready() -> bool:
        return not backend_error(timeout=1)

    def _start_backend_inprocess(self):
        """在后台线程里跑 uvicorn（打包版专用）。

        路径要分清楚，否则后端会崩在"人设文件不存在"上：
        - **只读资源**（styles/、web/static）在解包目录 BUNDLE 里 → 传给 QQRuntime 的 root；
        - **可写数据**（config.qq.yaml、sqlite）在 exe 目录 ROOT 里 → 显式传 config_path/db_path。
        """
        try:
            import threading

            if str(BUNDLE) not in sys.path:
                sys.path.insert(0, str(BUNDLE))
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            os.chdir(str(ROOT))

            config_path = ROOT / "config.qq.yaml"
            db_path = ROOT / "data" / "qq-only" / "messages.sqlite3"

            def serve():
                try:
                    import uvicorn
                    from src.qq_app import QQRuntime, create_app

                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    runtime = QQRuntime(root=ROOT, config_path=config_path, db_path=db_path)
                    application = create_app(runtime)
                    # 告诉其它控制台实例：这个后台是"带窗口的控制台"在跑（见 /api/runtime）
                    application.state.windowed = True
                    application.state.console_port = PORT
                    uvicorn.run(application, host="127.0.0.1", port=PORT, log_level="warning", log_config=None)
                except Exception as exc:      # 线程里抛错没人接，自己记一笔
                    print(f"[gui] 内嵌后端启动失败：{exc}")

            self._write_console_endpoint()
            self._backend_thread = threading.Thread(target=serve, name="qq-backend", daemon=True)
            self._backend_thread.start()
        except Exception as e:
            self.console_tab.set_notice(f"后端启动失败：{e}", ok=False)

    def _load_settings(self):
        data = api_get("settings")
        if data and data.get('success') is not False:
            self.settings_tab.populate(data)

    def _refresh(self):
        if self._quitting:return
        # 账号列表不在 _refresh() 里加载：窗口显示后由 showEvent 拉一次即可。
        # （之前放在这里会多出一个后台线程抢事件循环，可靠性用例偶发失败。）
        if getattr(self, '_status_pending', False):
            return
        self._status_pending = True
        task = StatusTask('status', 'status')
        task._owner_window = self
        holder = getattr(self, '_api_tasks', None)
        if holder is None:
            holder = self._api_tasks = []
        holder.append(task)
        dispatcher = getattr(self, '_api_dispatcher', None)
        if dispatcher is None:
            dispatcher = self._api_dispatcher = _ApiDispatcher(self)
        dispatcher.watch(task, self._status_done, holder)
        task.start()

    def _status_done(self, data):
        self._status_pending = False
        if not isinstance(data, dict):
            data = {}
        self._tray.update(data)
        if data and data.get('success') is False:
            message = data.get('message') or '后台未连接'
            if getattr(self, 'safe_mode', False) and '兼容模式' not in message:
                message = '已用「兼容模式」启动。' + message
            self.console_tab.set_notice(message, ok=False)
            return
        if data:
            self.console_tab.refresh(data)

    def _action(self, path: str):
        if self._busy or self._quitting:
            return
        self._busy = True
        self._tray.refresh_actions()
        self.console_tab.set_notice("处理中…")
        run_api_async(self, path, timeout=120, callback=self._action_done)

    def _check_capabilities(self):
        """平台能力自检（官方 QQ）：按钮交互没开通时直接说清楚。"""
        if self._busy:
            return
        self._busy = True
        self.console_tab.set_notice("正在检查平台能力（会临时占用一次连接，请稍候）…")
        run_api_async(self, "platform/capabilities", {}, timeout=60,
                      callback=self._capabilities_done)

    def _capabilities_done(self, result: dict):
        self._busy = False
        message = str(result.get("message") or result.get("detail")
                      or result.get("message") or "自检失败")
        self.console_tab.set_notice(message, ok=bool(result.get("success")))
        self._refresh()

    def _action_done(self, result):
        msg = result.get("message", "操作完成")
        ok = result.get("success", True)
        self.console_tab.set_notice(msg, ok=ok)
        self._busy = False
        self._tray.refresh_actions()
        self._refresh()

    def _save_settings(self):
        if self._busy:
            return
        self._busy = True
        data = self.settings_tab.collect()
        # 管理器连接存本机界面配置里（不写机器人配置，避免和账号配置混在一起）
        try:
            values = self.settings_tab.manager_values()
            self._cfg['manager_base_url'] = values['base_url']
            if values['token']:
                self._cfg['manager_token'] = values['token']
            save_gui_config(self._cfg_path, self._cfg)
        except Exception:
            pass
        # 保存会重载配置（有时还要重启机器人，要等平台鉴权），同步做会把窗口卡住
        self.settings_tab.save_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.settings_tab.save_lbl.setText("正在保存…")
        run_api_async(self, "settings", data, timeout=60, callback=self._save_settings_done)

    def _save_settings_done(self, result: dict):
        msg = result.get("message", "保存完成")
        ok = result.get("success", True)
        self.settings_tab.save_lbl.setStyleSheet(
            f"color: {SUCCESS if ok else ERROR}; font-size: 12px;"
        )
        self.settings_tab.save_lbl.setText(msg)
        if ok:
            self._load_settings()
        self._busy = False
        # 管理器地址/令牌可能刚改过：立刻试一次，失败就继续自动轮询。
        try:
            if self.load_manager_accounts(timeout=1.0, retrying=True):
                self._stop_manager_retry()
            else:
                self._start_manager_retry()
        except Exception:
            self._start_manager_retry()

    # ── 获取模型列表 / 测试连接 ────────────────────────────────────────────
    # 两个动作都拿**当前输入框的值**（Key 留空 = 用已保存的密钥），跑在后台线程里，
    # 回来后只更新设置页的提示，不碰保存流程、不改任何配置。
    def _probe_running(self) -> bool:
        return bool(self._probe_worker and self._probe_worker.isRunning())

    def _start_probe(self, path: str, payload: dict, button) -> bool:
        if self._probe_running():
            return False
        button.setEnabled(False)
        self._probe_worker = ApiProbeWorker(path, payload)
        self._probe_worker._owner_window = self
        return True

    def _fetch_models(self):
        if self._probe_running():
            return
        data = self.settings_tab.collect()
        self.settings_tab.set_probe("正在获取模型列表…")
        if not self._start_probe("ai/models", {"api_url": data["api_url"], "api_key": data["api_key"],
                                               "api_style": data["api_style"]},
                                 self.settings_tab.btn_models):
            return
        self._probe_worker.completed.connect(self._models_ready)
        self._probe_worker.start()

    def _models_ready(self, result: dict):
        self.settings_tab.btn_models.setEnabled(True)
        self._probe_worker = None
        models = [str(m) for m in (result.get("models") or []) if str(m).strip()]
        if result.get("ok") and models:
            count = self.settings_tab.fill_models(models)
            self.settings_tab.set_probe(f"已获取 {count} 个模型，请从下拉框选择模型 id")
        else:
            msg = result.get("message") or result.get("detail") or "获取模型列表失败"
            if result.get("detail") and result.get("detail") not in str(msg):
                msg = f"{msg}（{result['detail']}）"
            if result.get("suggested_base_url"):
                msg = f"{msg}\n建议把 API 地址填成：{result['suggested_base_url']}"
            self.settings_tab.set_probe(str(msg), ok=False)

    def _diagnose_ai(self):
        if self._probe_running():
            return
        data = self.settings_tab.collect()
        self.settings_tab.set_probe("正在测试连接（先取模型列表，再发一次最小请求）…")
        if not self._start_probe("ai/diagnose", {"api_url": data["api_url"], "api_key": data["api_key"],
                                                 "model": data["model"], "api_style": data["api_style"]},
                                 self.settings_tab.btn_diagnose):
            return
        self._probe_worker.completed.connect(self._diagnose_ready)
        self._probe_worker.start()

    def _apply_fast_profile(self):
        """「快速回复」档位：先让后台算出改动清单，弹窗逐条列出，确认后才真的改。"""
        if self._probe_running():
            return
        self.settings_tab.set_probe("正在检查等待类设置…")
        if not self._start_probe("pc/speed-profile", {}, self.settings_tab.btn_fast):
            return
        self._probe_worker.completed.connect(self._fast_profile_ready)
        self._probe_worker.start()

    def _fast_profile_ready(self, result: dict):
        self.settings_tab.btn_fast.setEnabled(True)
        self._probe_worker = None
        plan = result.get("plan") or {}
        if not result.get("success"):
            self.settings_tab.set_probe(str(result.get("message") or "检查失败"), ok=False)
            return
        if not plan:
            self.settings_tab.set_probe(str(result.get("message") or "已经是快档了"), ok=True)
            return
        if not ask_confirm(self, "切到「快速回复」档位",
                           "把这些“等待类”设置调快？\n\n" + str(result.get("message", "")),
                           detail="只改等待/延迟相关的插件设置，模型、密钥、人设、图片库、记忆都不会动。",
                           ok_text="调快", cancel_text="先不改"):
            self.settings_tab.set_probe("没有改动。")
            return
        # 后台执行：写设置会连带落盘配置，不能在 Qt 主线程里同步做
        run_api_async(self, "pc/speed-profile", {"apply": True}, timeout=30,
                      callback=self._fast_profile_applied, busy="正在应用「快速回复」档位…")

    def _fast_profile_applied(self, applied: dict):
        self.settings_tab.set_probe(str(applied.get("message") or "已应用"),
                                    ok=bool(applied.get("success")))

    @safe_slot(label="_speed_test_ai")
    def _speed_test_ai(self):
        """模型测速：让后台逐个模型发最小请求，把最快的填进模型框（不自动保存）。"""
        if self._probe_running():
            return
        data = self.settings_tab.collect()
        models = []
        # 注意：model_name 是**输入框**，下拉是 model_pick；以前这里把输入框当组合框用，
        # 抛 AttributeError 后被 PyQt 当成未处理异常直接 abort —— 用户看到的就是"测速闪退"。
        picker = self.settings_tab.model_pick
        for index in range(picker.count()):
            value = str(picker.itemText(index) or "").strip()
            if value and value not in models:
                models.append(value)
        current = str(data.get("model") or "").strip()
        if current and current not in models:
            models.insert(0, current)
        if not models:
            self.settings_tab.set_probe("先点「获取模型列表」，或者手动填一个模型名再测速", ok=False)
            return
        self.settings_tab.set_probe(f"正在给 {min(6, len(models))} 个模型测速（每个发一次最小请求）…")
        # 把界面里当前填的地址/密钥一起带上：否则测速用的是**已保存**的旧地址，
        # 会出现"测试连接 200、测速全 404"的自相矛盾结果（用户实测遇到过）。
        if not self._start_probe("ai/model-speed",
                                 {"models": models[:6], "rounds": 1, "timeout_sec": 8.0, "limit": 6,
                                  "api_url": str(data.get("api_url") or ""),
                                  "api_key": str(data.get("api_key") or ""),
                                  "api_style": str(data.get("api_style") or "")},
                                 self.settings_tab.btn_speed):
            return
        self._probe_worker.completed.connect(self._speed_ready)
        self._probe_worker.start()

    @safe_slot(label="_speed_ready")
    def _speed_ready(self, result: dict):
        self.settings_tab.btn_speed.setEnabled(True)
        self._probe_worker = None
        rows = result.get("results") or []
        if not rows:
            self.settings_tab.set_probe(str(result.get("message") or "测速失败"), ok=False)
            return
        lines = []
        for item in sorted((row for row in rows if isinstance(row, dict)),
                           key=lambda row: (not row.get("ok"), _safe_float(row.get("seconds"), 999, 0, 1e6))):
            # 字段缺失/类型不对时只影响这一行（以前 KeyError/ValueError 会让整块结果不显示）
            model = str(item.get("model") or "未知模型")
            if item.get("ok"):
                lines.append(f"{model}：{_safe_float(item.get('seconds'), 0.0, 0.0, 1e6):.2f}s")
            else:
                lines.append(f"{model}：失败（{str(item.get('error') or '')[:40]}）")
        fastest = str(result.get("fastest") or "")
        if fastest and fastest != result.get("current"):
            combo = self.settings_tab.model_pick
            index = combo.findText(fastest)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(fastest)
            lines.append(f"已把「{fastest}」填进模型框，确认无误后点「保存全部配置」")
        elif fastest:
            lines.append(f"当前模型就是最快的（{fastest}）")
        used_url = str(result.get("api_url") or "")
        if used_url:
            # 这次测速实际用的地址写出来：以前"测速 404 / 测试连接 200"就是
            # 因为两边用的地址不是同一个，用户无从判断。
            lines.append(f"本次测速使用地址：{used_url}")
        self.settings_tab.set_probe("模型测速（越小越快）：\n" + "\n".join(lines), ok=bool(fastest))

    @safe_slot(label="_diagnose_ready")
    def _diagnose_ready(self, result: dict):
        self.settings_tab.btn_diagnose.setEnabled(True)
        self._probe_worker = None
        if not result.get("url") and not result.get("chat_status"):
            self.settings_tab.set_probe(str(result.get("message") or "测试连接失败"), ok=False)
            return
        styles = {"openai": "OpenAI 兼容", "anthropic": "Anthropic Messages"}
        style = styles.get(str(result.get("api_style")), str(result.get("api_style") or "未知"))
        source = "自动识别" if result.get("style_source") == "auto" else "按设置"
        status = result.get("models_status")
        chat_status = result.get("chat_status")
        latency = result.get("latency_ms")
        listed = {True: "是", False: "否", None: "无法确认"}.get(result.get("model_listed"), "无法确认")
        lines = [
            f"接口地址：{result.get('url') or '未填写'}",
            f"接口格式：{style}（{source}）",
            f"模型列表：HTTP {status if status is not None else '未返回'}"
            f" · {result.get('models_count', 0)} 个模型",
            f"对话请求：HTTP {chat_status if chat_status is not None else '未返回'}"
            f" · {latency if latency is not None else '-'} ms",
            f"模型在列表中：{listed}",
        ]
        if result.get("reply"):
            lines.append(f"回复预览：{result['reply']}")
        if result.get("problem"):
            lines.append(f"问题：{result['problem']}")
        if result.get("suggested_models"):
            lines.append(f"建议改用模型：{'、'.join(str(m) for m in result['suggested_models'])}")
        if result.get("suggested_base_url"):
            lines.append(f"建议把 API 地址填成：{result['suggested_base_url']}")
        self.settings_tab.set_probe("\n".join(lines), ok=bool(result.get("ok")))

    def _request_exit(self):
        if self._quitting:return
        pending=any(task.isRunning() for widget in [self]+self.findChildren(QWidget) for task in getattr(widget,'_api_tasks',[]))
        if self._busy or pending or self._probe_running() or (getattr(self.pc_tab,'worker',None) and self.pc_tab.worker.isRunning()):
            self._tray.show_window();self.console_tab.set_notice('后台操作仍在执行，请完成后再选择退出程序。',ok=False);return
        self._quitting=True;self._tray.refresh_actions();self._timer.stop();self.pc_tab.timer.stop()
        run_api_async(self,'stop',timeout=15,callback=self._exit_stopped)

    def _exit_stopped(self,result):
        self._exit_result=result
        if not self._backend_proc and (not isinstance(result,dict) or result.get('success') is not True):
            # 复用的后台不归本窗口管理；停机失败时不能宣称已经彻底退出。
            self._quitting=False;self._timer.start();self.pc_tab.timer.start()
            self._tray.refresh_actions();self._tray.show_window()
            self.console_tab.set_notice('未能停止后台，请检查连接后重新选择退出程序；'
                                        '也可以用托盘里的「强制退出控制台」。',ok=False)
            return
        if self.close():QApplication.instance().quit()
        else:QTimer.singleShot(100,self._retry_exit)

    def force_exit(self):
        "强制退出控制台窗口：不停后台（复用/外部后台时用），后台继续跑。"
        self._quitting=True
        try:
            self._timer.stop();self.pc_tab.timer.stop()
        except Exception:
            pass
        try:
            self._tray.icon.hide()
        except Exception:
            pass
        if self.close():QApplication.instance().quit()

    def _retry_exit(self):
        if self.close():QApplication.instance().quit()
        else:QTimer.singleShot(100,self._retry_exit)

    def closeEvent(self, event):
        tray=getattr(self,'_tray',None)
        if tray is not None and not self._quitting and tray.hide_window():
            try:self._save_geometry()
            except OSError:pass
            event.ignore();return
        if getattr(self.pc_tab,'worker',None) and self.pc_tab.worker.isRunning():
            event.ignore();return
        pending = any(task.isRunning() for widget in [self] + self.findChildren(QWidget)
                      for task in getattr(widget, '_api_tasks', []))
        if pending or self._probe_running() or any(getattr(task, '_owner_window', None) is self and task.isRunning() for task in _PROBE_TASKS | _API_TASKS):
            self.console_tab.set_notice('后台请求仍在执行，请等待完成后关闭窗口。', ok=False)
            event.ignore()
            return
        self.hang_watchdog._stop = True
        self.hang_watchdog._timer.stop()
        try:
            self.watchdog_notice.disconnect(self._on_watchdog_notice)
        except TypeError:
            pass
        self.pc_tab.timer.stop();self._timer.stop()
        self.assistant_tab.shutdown()
        # 关窗时把外部后端真进程停干净（服务端退出钩子同样的收尾）。
        try:self.agent_backend_tab.shutdown_backends()
        except Exception:pass
        try:self.logs_tab.shutdown()
        except Exception:pass
        self._hang_notice_timer.stop()
        if tray is not None:tray.icon.hide()
        if getattr(self,'_instance_server',None):self._instance_server.server.close()
        self._save_geometry()
        if self._backend_proc:
            self._backend_proc.terminate()
        # 系统托盘消失时回退到普通关闭，撤销初始化时的“关窗不退出”。
        QApplication.instance().setQuitOnLastWindowClosed(True)
        super().closeEvent(event)

    def _drain_hang_notices(self):
        # 工作线程只写 Python 队列；窗口销毁后也不会访问已经释放的 Qt 信号。
        while not self._hang_notices.empty():
            self._on_watchdog_notice(self._hang_notices.get())

    def _on_watchdog_notice(self, text: str) -> None:
        """看门狗报告界面卡顿（槽里绝不能抛异常：PyQt 会直接 abort 进程）。"""
        try:
            self.console_tab.set_notice(text, ok=False)
        except RuntimeError:
            pass

    def _save_geometry(self):
        """把当前窗口位置与尺寸记进 gui_config.json（下次打开沿用）。

        抽成独立方法是为了能单独测：用户要求窗口可以自己调大小，
        调完的尺寸必须能记住，否则每次打开都回到旧尺寸。
        """
        geo = self.geometry()
        self._cfg["geometry"] = {"x": geo.x(), "y": geo.y(),
                                 "w": geo.width(), "h": geo.height()}
        try:
            save_gui_config(self._cfg_path, self._cfg)
        except OSError:
            pass
        return dict(self._cfg["geometry"])


# ── 入口 ──────────────────────────────────────────────────────────────────────
def _logs_dir() -> Path:
    """日志目录：与启动日志同处（%LOCALAPPDATA%\\WhaleGirlQQ\\logs）。"""
    folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WhaleGirlQQ" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class UiHangWatchdog:
    """界面"卡死"取证器：主线程一旦停止响应就把**所有线程的调用栈**写到日志。

    用户报过"异常卡死"，而 Windows 只会给一条 `AppHangTransient`，看不出卡在哪。
    这里用一个守护线程盯着主线程的心跳（主线程上的 QTimer 每秒更新一次）：
    超过 ``stall_seconds`` 没跳，就把当前时刻每个线程的栈 dump 到
    ``<数据目录>/logs/hang-<时间>.txt``，并把这件事写进运行记录。下次卡住时，
    用户只要把这个文件发过来，就能直接定位到卡在哪一行。
    """

    def __init__(self, owner, logger_dir, stall_seconds: float = 8.0, tick_seconds: float = 1.0,
                 note=None):
        self.owner = owner
        self.dir = Path(logger_dir)
        self.stall_seconds = float(stall_seconds)
        self.tick_seconds = float(tick_seconds)
        self.note = note
        self.last_tick = time.time()
        self.reported_at = 0.0
        self._stop = False
        owner.destroyed.connect(self._owner_destroyed)
        self._timer = QTimer(owner)
        self._timer.setInterval(int(self.tick_seconds * 1000))
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    def tick(self) -> None:
        """主线程心跳（由 QTimer 调用；卡住时它就不会再被调用）。"""
        self.last_tick = time.time()

    def _owner_destroyed(self):
        self._stop=True

    def stalled_for(self) -> float:
        return time.time() - self.last_tick

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="ui-hang-watchdog", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop:
            time.sleep(max(1.0, self.tick_seconds))
            if self._stop:
                break
            stalled = self.stalled_for()
            if stalled < self.stall_seconds:
                continue
            # 同一次卡顿只写一份（卡住期间心跳不会恢复，避免刷屏）
            if time.time() - self.reported_at < max(30.0, self.stall_seconds * 3):
                continue
            try:
                path = self.dump(stalled)
            except Exception:
                continue
            self.reported_at = time.time()
            if self.note is not None:
                try:
                    self.note(f"界面卡顿 {stalled:.0f} 秒，已把各线程调用栈写到 {path}（发给我就能定位）")
                except RuntimeError:
                    # 窗口已经被销毁（控件也随之删除）：立刻收工。
                    # 以前这里会继续跑，下一次上报就撞上"wrapped C/C++ object has been deleted"，
                    # PyQt 在槽里抛异常会直接把进程 abort —— 实测把全量回归跑成 0xC0000409。
                    self._stop = True
                    break

    def dump(self, stalled: float) -> Path:
        """把每个线程的栈写进文件，返回文件路径。"""
        import traceback
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.dir / f"hang-{stamp}.txt"
        frames = sys._current_frames()
        lines = [f"界面无响应：{stalled:.1f} 秒（阈值 {self.stall_seconds:.0f} 秒）",
                 f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"进程：{os.getpid()}  线程数：{threading.active_count()}", ""]
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            lines.append(f"===== 线程 {thread.name} (id={thread.ident}) =====")
            if frame is None:
                lines.append("（没有可用的栈帧）")
            else:
                lines.extend(line.rstrip() for line in traceback.format_stack(frame))
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def main():
    from src.startup_supervisor import stage, mark_ready
    from src.gui.qt_locale import ensure_chinese, should_use_native_dialogs, light_palette
    stage('application-creating', safe_mode=SAFE_MODE)
    use_native = should_use_native_dialogs(SAFE_MODE)
    if not use_native:
        # 兼容模式下默认也用系统文件对话框（用户要求资源管理器样式）；
        # 配置里显式关掉才退回自绘版本。
        try:
            _cfg = load_gui_config(GUI_CFG)
            if bool(_cfg.get('native_file_dialogs', True)):
                use_native = True
        except Exception:
            use_native = True
    if SAFE_MODE:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL, True)
    # 文件选择用系统原生对话框（用户要求图三那种资源管理器样式）。
    # 兼容模式也不能退自绘：Qt 平台参数里的 dialogs=none 会绕过这个属性，
    # 所以 desktop_runtime.configure 的兼容模式参数里不能再带它。
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, not use_native)
    # Windows 高 DPI 支持。安全模式下不设取整策略：远程桌面的缩放组合也是
    # Qt 平台集成的嫌疑点之一，宁可少一层处理。
    if not SAFE_MODE and hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        # PassThrough：125%/150% 这类非整数缩放按真实比例绘制。
        # 以前用 Round 会把 125% 压成 100% 再由系统放大，整窗（尤其背景图和文字）
        # 都会发虚——这正是用户反馈的"背景图看起来小/糊"。
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    # Windows 任务栏图标：不设 AppUserModelID 时任务栏会显示 python.exe 的图标，
    # 而不是我们自己的图标。
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "whalegirl.qq.console"
            )
        except Exception:
            pass
    app = QApplication(sys.argv)
    from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
    stage('application-created', qt=QT_VERSION_STR, pyqt=PYQT_VERSION_STR)
    app.setApplicationName("鲸鱼娘 QQ 控制台")
    from src.gui.tray import ConsoleInstance
    if ConsoleInstance.forward(ROOT):
        stage('existing-console-restored');mark_ready()
        return
    # Qt 自带的中文翻译：非原生对话框（安全模式）与个别 Qt 文案都靠它变中文
    locale_status = ensure_chinese(app, BUNDLE)
    if not locale_status.get('loaded'):
        logger.warning("Qt 中文翻译未加载：%s", locale_status.get('reason'))
    # 既然统一用自绘对话框，浅色调色板必须始终装上（否则深色系统下还是黑底）。
    # light_palette() 覆盖 Window/WindowText/Base/Text/Button/ButtonText/
    # ToolTipBase/ToolTipText/Highlight/HighlightedText —— 系统深色模式也压得住。
    app.setPalette(light_palette())
    # 弹层全局生效：QMenu / QToolTip / 无父窗口的 QMessageBox / 下拉列表都不是
    # MainWindow 的子控件，窗口级样式继承不到它们，只有应用级样式盖得住。
    # 必须在建窗口之前装，否则先建出来的弹窗拿不到这套规则。
    app.setStyleSheet(POPUP_STYLESHEET)
    from src.gui.diagnostics import install
    install(_logs_dir(), lambda title, message: QMessageBox.warning(None, title, message))
    # 应用级图标：影响任务栏、Alt+Tab，以及所有没单独设图标的子窗口
    try:
        app.setWindowIcon(app_icon())
    except Exception:
        pass
    # 启动阶段任何异常都要**看得见**：以前异常直接冒到 gui_launcher 里，
    # 用户看到的是"双击了没反应/闪一下就没了"，还以为是程序打不开。
    try:
        stage('window-creating')
        window = MainWindow()
        window._instance_server=ConsoleInstance(window,ROOT)
        stage('window-showing')
        window.show()
        stage('window-shown')
        QTimer.singleShot(0, mark_ready)
    except BaseException as exc:
        import traceback
        detail = traceback.format_exc()
        log_path = _logs_dir() / "startup.log"
        try:
            with log_path.open("a", encoding="utf-8") as output:
                output.write(detail)
        except Exception:
            pass
        QMessageBox.critical(
            None, "控制台启动失败",
            f"控制台没能启动：{type(exc).__name__}: {exc}\n\n"
            f"详细信息已写入：\n{log_path}\n\n"
            "把这段信息发给作者即可定位。")
        raise
    sys.exit(app.exec())


def _selftest_main():
    # --self-test 后面必须跟输出位置：以前没带就直接 IndexError（用户日志里出现过两次）。
    index = sys.argv.index("--self-test") + 1
    if index >= len(sys.argv) or not sys.argv[index] or sys.argv[index].startswith("--"):
        message = ("--self-test 需要指定输出位置，例如：--self-test D:/selftest-out"
                   "（这次没带，自检没有开始）")
        try:
            with (_logs_dir() / "selftest-usage.log").open("a", encoding="utf-8") as log:
                log.write(message + "\n")
        except Exception:
            pass
        try:
            print(message, file=sys.stderr)
        except Exception:
            pass
        sys.exit(2)
    # self-test 路径也装诊断：以前槽里异常在这条路径上会让整个进程 abort（0xC0000409），
    # 打包自检就变成跑不起来而不是界面某块显示失败。
    from src.gui.diagnostics import install as _install_diag
    try:
        _install_diag(_logs_dir(), lambda title, message: None)
    except Exception:
        pass
    from src.gui.smoke import run
    output = Path(sys.argv[index])
    try:
        run(sys.modules[__name__], output)
    except BaseException:
        import traceback
        output.with_suffix(".error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        sys.exit(1)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _selftest_main()
    else:
        main()
