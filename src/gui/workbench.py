"""智能中心：把记忆核心、日记、电脑控制、桥接状态做成软件界面。"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from src import local_control
from . import dialogs, pc_workspace

MEMORY_CORE = Path(r"D:\工作\记忆核心.md")
BRIDGE = Path(r"D:\聊天机器人\qq-bridge")


STATUS_PORTS = (
    ("onebot", "OneBot", 3072),
    ("http", "OneBot HTTP", 3000),
    ("dsh", "DSH", 10529),
    ("bridge", "qqbridge", 3100),
)

_PROBES = set()          # 运行中的探测线程，留住引用防止被 GC 提前回收


def _read_json(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _url_port(value) -> int:
    """从 URL、端口字符串或整数里取端口；取不到就返回 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    text = str(value or "").strip()
    if not text:
        return 0
    if "://" not in text:
        text = "//" + text
    try:
        port = urlsplit(text).port
    except (TypeError, ValueError):
        return 0
    try:
        return int(port) if port and int(port) > 0 else 0
    except (TypeError, ValueError):
        return 0


def _gateway_db_candidates():
    roots = []
    env = os.environ.get("WHALEQQ_GATEWAY_HOME") or os.environ.get("WHALEQQ_GATEWAY_DATA")
    if env:
        base = Path(env)
        roots.append(base / "core" / "gateway.sqlite3")
        roots.append(base / "gateway.sqlite3")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "WhaleQQGateway" / "core" / "gateway.sqlite3")
    profile = os.environ.get("USERPROFILE")
    if profile:
        roots.append(Path(profile) / "AppData" / "Local" / "WhaleQQGateway"
                     / "core" / "gateway.sqlite3")
    return roots


def _process_alive(pid) -> bool:
    """只查询进程是否存在，绝不向它发信号（Windows 上 os.kill(pid, 0) 会杀进程）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    kernel32 = windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _onebot_net_port(data, key: str) -> int:
    networks = data.get("networks") if isinstance(data, dict) else None
    servers = networks.get(key) if isinstance(networks, dict) else None
    if not isinstance(servers, list):
        return 0
    for server in servers:
        if not isinstance(server, dict):
            continue
        if server.get("enable") is False or server.get("enabled") is False:
            continue
        port = _url_port(server.get("port"))
        if port:
            return port
    return 0


def _manager_accounts(db_path=None):
    """读 WhaleQQGateway 的账号库；读不到就返回空列表，界面显示未配置。"""
    candidates = [Path(db_path)] if db_path else _gateway_db_candidates()
    db = next((path for path in candidates if path.is_file()), None)
    if db is None:
        return []
    try:
        uri = db.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            rows = con.execute("select id, config from accounts").fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error, ValueError):
        return []
    accounts = []
    for account_id, raw in rows:
        try:
            cfg = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        uin = str(cfg.get("expected_uin") or "").strip()
        engine = db.parent / "accounts" / str(account_id) / "engine"
        connection = _read_json(engine / "connection.json")
        config_dir = engine / "runtime" / "config"
        base_onebot = _read_json(config_dir / "onebot.json")
        overlay = _read_json(config_dir / ("onebot_%s.json" % uin)) if uin else {}
        http_port = _onebot_net_port(base_onebot, "httpServers")
        overlay_networks = overlay.get("networks") if isinstance(overlay, dict) else None
        if isinstance(overlay_networks, dict) and "httpServers" in overlay_networks:
            http_port = _onebot_net_port(overlay, "httpServers")
        ws_port = _url_port(connection.get("ws_port"))
        if not ws_port:
            ws_port = _onebot_net_port(base_onebot, "wsServers")
        injected = cfg.get("injected_pid")
        accounts.append({
            "id": str(account_id),
            "label": str(cfg.get("label") or uin or account_id),
            "uin": uin,
            "engine": str(cfg.get("engine") or ""),
            "ws_port": ws_port,
            "http_port": http_port,
            "online": _process_alive(injected),
        })
    return accounts


def _pick_account(accounts, owner_qq):
    online = [item for item in (accounts or []) if item.get("online")]
    if not online:
        return None
    owner = str(owner_qq or "").strip()
    if owner:
        for item in online:
            if str(item.get("uin") or "") == owner:
                return item
    return online[0]


def _status_targets(config, accounts):
    """端口来源：账号在线时用 WhaleQQGateway 账号端口；DSH/qqbridge 用 qq-bridge 配置。"""
    cfg = config if isinstance(config, dict) else {}
    account = _pick_account(accounts, cfg.get("ownerQQ"))
    ws_port = _url_port(account.get("ws_port")) if account else 0
    http_port = _url_port(account.get("http_port")) if account else 0
    dsh = cfg.get("dsh") if isinstance(cfg.get("dsh"), dict) else {}
    dsh_port = _url_port(dsh.get("baseUrl"))
    bridge_port = _url_port(cfg.get("consolePort"))
    return [
        {"key": "onebot", "label": "OneBot %s" % ws_port if ws_port else "OneBot",
         "port": ws_port},
        {"key": "http", "label": "OneBot HTTP %s" % http_port if http_port else "OneBot HTTP",
         "port": http_port},
        {"key": "dsh", "label": "DSH %s" % dsh_port if dsh_port else "DSH",
         "port": dsh_port},
        {"key": "bridge", "label": "qqbridge %s" % bridge_port if bridge_port else "qqbridge",
         "port": bridge_port},
    ]


def _build_status_targets():
    return _status_targets(_read_json(BRIDGE / "config.json"), _manager_accounts())


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.8):
            return True
    except OSError:
        return False


class _PortProbe(QThread):
    """在后台线程读状态来源并探端口，结果用信号送回 GUI 线程。

    create_connection 打一个没在监听的本地端口不是立刻失败：被防火墙或安全
    软件拦掉 SYN 的机器上它会一直挂到 timeout 才返回。实测四个端口串起来能把
    窗口冻 3.2 秒，所以这一步不能留在 GUI 线程上；读配置和账号库也一起放到
    这个线程里，避免 GUI 线程被磁盘/数据库拖住。
    """

    done = pyqtSignal(object)

    def run(self):
        try:
            results = []
            for target in _build_status_targets():
                port = _url_port(target.get("port"))
                results.append(dict(target, ok=_port_open(port) if port else False))
        except Exception:      # 线程里漏出异常会让 Qt 直接 abort 进程
            results = []
        self.done.emit(results)


class WorkbenchTab(QWidget):
    def __init__(self, api=None, diary_dir=None):
        super().__init__()
        self.api = api
        self.diary_dir = Path(diary_dir) if diary_dir else (
            pc_workspace.default_data_dir() / "qq-only" / "日记")
        self._probe_running = False
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel("智能中心")
        title.setStyleSheet("font-size:20px;font-weight:700;color:#17324d;")
        root.addWidget(title)
        hint = QLabel("记忆、日记、电脑控制和 QQ 桥接都在这里操作；不需要记命令。")
        hint.setStyleSheet("color:#5b7188;")
        root.addWidget(hint)

        status_box = QGroupBox("运行状态")
        status_row = QHBoxLayout(status_box)
        self.labels = {}
        for key, text, _port in STATUS_PORTS:
            lab = QLabel(f"{text}：检查中")
            lab.setStyleSheet("padding:4px 8px;border:1px solid #dce6ef;border-radius:6px;")
            self.labels[key] = lab
            status_row.addWidget(lab)
        status_row.addStretch()
        self.refresh_btn = QPushButton("刷新状态")
        self.refresh_btn.clicked.connect(self.refresh_status)
        status_row.addWidget(self.refresh_btn)
        self.start_btn = QPushButton("启动 qqbridge")
        self.start_btn.clicked.connect(self.start_bridge)
        status_row.addWidget(self.start_btn)
        root.addWidget(status_box)

        inner = QTabWidget()
        inner.addTab(self._memory_page(), "记忆核心")
        inner.addTab(self._diary_page(), "日记")
        inner.addTab(self._pc_page(), "电脑控制")
        inner.addTab(self._bridge_page(), "QQ 桥接")
        root.addWidget(inner, 1)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(10000)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start()
        self.refresh_status()

    def _memory_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.memory_info = QLabel()
        layout.addWidget(self.memory_info)
        self.memory_edit = QTextEdit()
        self.memory_edit.setAcceptRichText(False)
        layout.addWidget(self.memory_edit, 1)
        row = QHBoxLayout()
        for text, slot in (("重新读取", self.load_memory), ("保存", self.save_memory),
                           ("备份后保存", self.backup_memory), ("打开所在文件夹", self.open_memory_dir)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch()
        layout.addLayout(row)
        self.load_memory()
        return page

    def _diary_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        picker = QHBoxLayout()
        picker.addWidget(QLabel("日期"))
        self.diary_day = QComboBox()
        self.diary_day.setMinimumWidth(150)
        self.diary_day.currentIndexChanged.connect(self.load_diary)
        picker.addWidget(self.diary_day)
        picker.addStretch()
        layout.addLayout(picker)

        self.diary_info = QLabel()
        self.diary_info.setWordWrap(True)
        layout.addWidget(self.diary_info)
        self.diary_edit = QTextEdit()
        self.diary_edit.setAcceptRichText(False)
        self.diary_edit.setReadOnly(True)
        self.diary_edit.setPlaceholderText("AI 写完当天日记后会显示在这里。日记由 AI 自己写，管理员不能在这里改。")
        layout.addWidget(self.diary_edit, 1)

        row = QHBoxLayout()
        for text, slot in (("刷新", self.refresh_diaries),
                           ("让 AI 写今天", self.ask_ai_diary),
                           ("打开日记文件夹", self.open_diary_dir)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch()
        layout.addLayout(row)
        self.refresh_diaries()
        return page

    def _pc_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(10)
        scroller.setWidget(body)
        outer.addWidget(scroller, 1)

        # 旧控件全部留成隐藏字段：别处（含测试）按这些名字取值，删掉就是 AttributeError。
        self.path_edit = QLineEdit()
        self.url_edit = QLineEdit()
        for hidden in (self.path_edit, self.url_edit):
            hidden.setVisible(False)

        ws_box = QGroupBox("本地工作空间")
        ws_layout = QVBoxLayout(ws_box)
        ws_layout.addWidget(pc_workspace.hint(
            "这里是 agent 自己的文件夹：双击文件夹进去，双击文件用默认程序打开；"
            "新建文件夹和删除都只在这个文件夹里生效，出不了这个范围。"))
        self.pc_workspace = pc_workspace.WorkspacePanel(parent=ws_box)
        ws_layout.addWidget(self.pc_workspace)
        layout.addWidget(ws_box)

        url_box = QGroupBox("常用网址")
        url_layout = QVBoxLayout(url_box)
        url_layout.addWidget(pc_workspace.hint(
            "点一下就用系统默认浏览器打开；点「＋ 添加」自己加一个，右键按钮能删掉。"))
        self.bookmark_bar = pc_workspace.BookmarkBar(parent=url_box)
        url_layout.addWidget(self.bookmark_bar)
        url_layout.addWidget(self.url_edit)
        layout.addWidget(url_box)

        type_box = QGroupBox("输入文字")
        type_layout = QVBoxLayout(type_box)
        type_layout.addWidget(pc_workspace.hint(
            "把这段文字直接打进当前最前面的窗口（比如微信/浏览器输入框）。"
            "执行前请先点一下目标窗口。"))
        self.type_edit = QLineEdit()
        self.type_edit.setPlaceholderText("要打进去的文字")
        tb = QPushButton("输入到当前窗口")
        tb.clicked.connect(self.type_text)
        type_row = QHBoxLayout()
        type_row.addWidget(self.type_edit, 1)
        type_row.addWidget(tb)
        type_layout.addLayout(type_row)
        layout.addWidget(type_box)

        hotkey_box = QGroupBox("快捷键")
        hotkey_layout = QVBoxLayout(hotkey_box)
        hotkey_layout.addWidget(pc_workspace.hint(
            "给当前最前面的窗口发一串按键，例如 ctrl+s 保存、alt+tab 切窗口、enter 回车。"
            "多个键用 + 连接。"))
        self.hotkey_edit = QLineEdit()
        self.hotkey_edit.setPlaceholderText("例如 ctrl+s、alt+tab、enter")
        hb = QPushButton("发送快捷键")
        hb.clicked.connect(self.send_hotkey)
        hk_row = QHBoxLayout()
        hk_row.addWidget(self.hotkey_edit, 1)
        hk_row.addWidget(hb)
        hotkey_layout.addLayout(hk_row)
        layout.addWidget(hotkey_box)

        click_box = QGroupBox("鼠标")
        click_layout = QVBoxLayout(click_box)
        click_layout.addWidget(pc_workspace.hint(
            "点屏幕上的某个坐标（左上角是 0,0）。可以先点下面「截图并预览」看清位置，再把坐标填进来。"))
        self.x_edit = QLineEdit()
        self.x_edit.setPlaceholderText("横坐标 x")
        self.y_edit = QLineEdit()
        self.y_edit.setPlaceholderText("纵坐标 y")
        cb = QPushButton("点击坐标")
        cb.clicked.connect(self.click)
        click_row = QHBoxLayout()
        click_row.addWidget(QLabel("x")); click_row.addWidget(self.x_edit)
        click_row.addWidget(QLabel("y")); click_row.addWidget(self.y_edit)
        click_row.addWidget(cb)
        click_row.addStretch()
        click_layout.addLayout(click_row)
        layout.addWidget(click_box)

        action_box = QGroupBox("动作")
        action_layout = QVBoxLayout(action_box)
        action_layout.addWidget(pc_workspace.hint(
            "截图并预览：截一张全屏图放在下面。复制截图路径：把这张图的文件路径放进剪贴板。"
            "读剪贴板：把剪贴板内容显示在下面。写剪贴板：把上面「输入文字」框里的内容放进剪贴板。"
            "打开记事本：开一个记事本窗口。"))
        actions = QHBoxLayout()
        for text, slot in (("截图并预览", self.screenshot), ("复制截图路径", self.copy_screenshot_path),
                           ("读剪贴板", self.read_clipboard), ("写剪贴板", self.write_clipboard),
                           ("打开记事本", self.open_notepad)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            actions.addWidget(b)
        actions.addStretch()
        action_layout.addLayout(actions)
        layout.addWidget(action_box)

        self.pc_info = QLabel("截图会显示在这里")
        self.pc_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pc_info.setStyleSheet("border:1px solid #dce6ef;border-radius:8px;background:#f8fbfd;")
        self.pc_info.setMinimumHeight(260)
        layout.addWidget(self.pc_info, 1)
        self._last_shot = ""
        return page

    def _bridge_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        info = QLabel("桥接目录：" + str(BRIDGE))
        info.setWordWrap(True)
        layout.addWidget(info)
        row = QHBoxLayout()
        for text, slot in (("打开控制台", self.open_console), ("打开目录", self.open_bridge_dir),
                           ("查看日志", self.load_bridge_log), ("启动 / 重启", self.start_bridge)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch()
        layout.addLayout(row)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)
        self.load_bridge_log()
        return page

    def refresh_status(self, callback=None):
        """读状态来源并在后台探端口，结果回来后更新标签。

        同一时刻只跑一个探测：定时器每 10 秒一次，按钮也能连点。
        端口不是写死的：OneBot 取 WhaleQQGateway 里当前在线账号的实际端口，
        DSH / qqbridge 取 qq-bridge 的 config.json；没有配置的来源显示「未配置」。
        """
        if self._probe_running:
            return
        self._probe_running = True
        probe = _PortProbe()          # 不挂父对象：窗口先关掉也不会连带销毁线程
        _PROBES.add(probe)
        probe.done.connect(lambda results: self._apply_status(results, callback))
        probe.finished.connect(self._probe_finished)
        # 引用必须留到线程真正结束：以前在 run() 里 discard(self)，线程还没收工
        # Python 引用就可能归零 → 销毁运行中的 QThread → Qt qFatal
        # "QThread: Destroyed while thread is still running" → 进程 0xC0000409。
        # 捕获 probe、不捕获 self：窗口关掉了也不该被这条连接拖住。
        probe.finished.connect(lambda probe=probe: _PROBES.discard(probe))
        probe.start()

    def _probe_finished(self):
        self._probe_running = False

    def _apply_status(self, results, callback=None):
        try:
            for item in results or []:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                lab = self.labels.get(key)
                if lab is None:
                    continue
                port = _url_port(item.get("port"))
                label = str(item.get("label") or lab.text().split("：")[0])
                if not port:
                    lab.setText(label + "：未配置")
                    lab.setStyleSheet(
                        "padding:4px 8px;border-radius:6px;border:1px solid #dce6ef;"
                        "background:#f7fafc;color:#5b7188;")
                    continue
                ok = bool(item.get("ok"))
                lab.setText(label + "：" + ("在线" if ok else "离线"))
                lab.setStyleSheet(
                    "padding:4px 8px;border-radius:6px;border:1px solid "
                    + ("#9ddfc8" if ok else "#f0c6c6")
                    + ";background:" + ("#effbf6" if ok else "#fff1f1") + ";")
        except RuntimeError:
            # 探测期间窗口被关掉：Qt 包装对象已失效，这次结果直接丢弃。
            # 吞不掉的话，槽里漏出的异常会让 Qt 直接终止进程（实测 0xC0000409）。
            return
        if callable(callback):
            try:
                callback()
            except RuntimeError:
                pass

    # ── 记忆 / 日记 ─────────────────────────────────────────────────────
    def _read(self, path, edit, info):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        edit.setPlainText(text)
        info.setText(f"{path} · {len(text)} 字 · 最后修改 {time.strftime('%Y-%m-%d %H:%M', time.localtime(path.stat().st_mtime)) if path.exists() else '不存在'}")

    def load_memory(self):
        self._read(MEMORY_CORE, self.memory_edit, self.memory_info)

    def _diary_files(self):
        try:
            files = sorted(self.diary_dir.glob("????-??-??.txt"), reverse=True)
        except OSError:
            files = []
        return [path for path in files if path.stem[:4].isdigit()]

    def refresh_diaries(self):
        """按 txt 文件名刷新日期列表；默认停在今天，没有今天就选最新一篇。"""
        files = self._diary_files()
        current = self.diary_day.currentText().strip()
        self.diary_day.blockSignals(True)
        self.diary_day.clear()
        self.diary_day.addItems([path.stem for path in files])
        today = time.strftime("%Y-%m-%d")
        wanted = today if today in [path.stem for path in files] else (
            current if current in [path.stem for path in files] else
            (files[0].stem if files else ""))
        if wanted:
            self.diary_day.setCurrentText(wanted)
        self.diary_day.blockSignals(False)
        self.load_diary()

    def load_diary(self):
        day = self.diary_day.currentText().strip()
        if not day:
            self.diary_edit.setPlainText("")
            self.diary_info.setText("还没有 AI 日记 · 目录 " + str(self.diary_dir))
            return
        path = self.diary_dir / (day + ".txt")
        try:
            text = path.read_text(encoding="utf-8")
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        except OSError:
            text = ""
            stamp = "不存在"
        self.diary_edit.setPlainText(text)
        self.diary_info.setText(f"{path} · {len(text)} 字 · 最后修改 {stamp}")

    def ask_ai_diary(self):
        """日记只让 AI 写；这里只负责确认，然后把请求交给控制台后端。"""
        if not callable(self.api):
            dialogs.tell(self, "只能在控制台里写", "AI 写日记需要通过控制台后端调用模型。")
            return
        day = self.diary_day.currentText().strip() or time.strftime("%Y-%m-%d")
        path = self.diary_dir / (day + ".txt")
        exists = path.exists()
        tip = ("会覆盖已有的 " + day + ".txt，确定让 AI 重写吗？" if exists
               else "让 AI 根据当天的聊天和记忆写 " + day + " 的日记？")
        if not dialogs.ask_confirm(self, "让 AI 写日记", tip, kind="question",
                                   ok_text="开始写"):
            return
        self.api(self, "memory/diary/auto",
                 {"day": day, "confirmed": True, "force": True},
                 timeout=180, callback=self._after_ai_diary, busy="AI 正在写日记…")

    def _after_ai_diary(self, result):
        result = result if isinstance(result, dict) else {}
        if result.get("success"):
            self.refresh_diaries()
            if result.get("day"):
                self.diary_day.setCurrentText(str(result["day"]))
                self.load_diary()
            dialogs.tell(self, "日记已写好", str(result.get("message") or "AI 已写完。"),
                         kind="info")
            return
        detail = result.get("error") or result.get("detail") or result.get("message") or "未知错误"
        dialogs.tell(self, "本次没有写成", str(detail), kind="warning")

    def _save(self, path, edit, reload):
        text = edit.toPlainText()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        reload()
        dialogs.tell(self, "已保存", str(path), kind="info")

    def save_memory(self):
        self._save(MEMORY_CORE, self.memory_edit, self.load_memory)

    def backup_memory(self):
        if MEMORY_CORE.exists():
            target = MEMORY_CORE.with_name(MEMORY_CORE.stem + ".bak-" + time.strftime("%Y%m%d-%H%M%S") + ".md")
            target.write_text(MEMORY_CORE.read_text(encoding="utf-8"), encoding="utf-8")
        self.save_memory()

    def open_memory_dir(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(MEMORY_CORE.parent)))

    def open_diary_dir(self):
        try:
            self.diary_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.diary_dir)))

    # ── 电脑控制 ────────────────────────────────────────────────────────
    def pick_path(self):
        from .file_dialogs import open_file
        path, _ = open_file(self, "选择要打开的文件")
        if path:
            self.path_edit.setText(path)

    def open_path(self):
        try:
            local_control.open_path(self.path_edit.text().strip())
        except Exception as exc:
            dialogs.tell(self, "打开失败", str(exc), kind="warning")

    def open_url(self):
        try:
            local_control.open_url(self.url_edit.text().strip())
        except Exception as exc:
            dialogs.tell(self, "打开失败", str(exc), kind="warning")

    def type_text(self):
        try:
            local_control.type_text(self.type_edit.text())
        except Exception as exc:
            dialogs.tell(self, "输入失败", str(exc), kind="warning")

    def send_hotkey(self):
        try:
            local_control.hotkey(self.hotkey_edit.text())
        except Exception as exc:
            dialogs.tell(self, "快捷键失败", str(exc), kind="warning")

    def click(self):
        try:
            import ctypes
            x, y = int(self.x_edit.text()), int(self.y_edit.text())
            ctypes.windll.user32.SetCursorPos(x, y)
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        except Exception as exc:
            dialogs.tell(self, "点击失败", str(exc), kind="warning")

    def screenshot(self):
        try:
            self._last_shot = local_control.screenshot()
            pix = QPixmap(self._last_shot)
            self.pc_info.setPixmap(pix.scaled(880, 430, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))
        except Exception as exc:
            dialogs.tell(self, "截图失败", str(exc), kind="warning")

    def copy_screenshot_path(self):
        if self._last_shot:
            self.clipboard().setText(self._last_shot)

    def clipboard(self):
        from PyQt6.QtWidgets import QApplication
        return QApplication.clipboard()

    def read_clipboard(self):
        self.pc_info.setText(self.clipboard().text() or "剪贴板为空")

    def write_clipboard(self):
        self.clipboard().setText(self.type_edit.text())

    def open_notepad(self):
        try:
            local_control.open_path(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "notepad.exe"))
        except Exception as exc:
            dialogs.tell(self, "打开记事本失败", str(exc), kind="warning")

    # ── 桥接 ────────────────────────────────────────────────────────────
    def start_bridge(self):
        py = Path(r"D:\聊天机器人\qq机器人\.venv-build\Scripts\python.exe")
        entry = BRIDGE / "stack_entry.py"
        if not entry.exists():
            dialogs.tell(self, "缺少桥接", str(entry), kind="warning")
            return
        try:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen([str(py) if py.exists() else "python", str(entry)], cwd=str(BRIDGE),
                             creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.refresh_status()
            self.start_btn.setEnabled(False)
            self.start_btn.setText("启动中…")
            QTimer.singleShot(3500, self._after_start)
        except Exception as exc:
            self.start_btn.setEnabled(True)
            self.start_btn.setText("启动 qqbridge")
            dialogs.tell(self, "启动失败", str(exc), kind="warning")

    def _after_start(self):
        self.start_btn.setEnabled(True)
        self.start_btn.setText("启动 qqbridge")
        self.refresh_status(self._report_bridge_started)

    def _report_bridge_started(self):
        """探测结果回到标签之后再判断，口径与以前一样（看标签文案）。"""
        onebot_text = self.labels["onebot"].text()
        http_text = self.labels["http"].text()
        if onebot_text.endswith("在线") or http_text.endswith("在线"):
            dialogs.tell(self, "已启动", "qqbridge 已启动，OneBot 连接在线。", kind="info")
        elif "未配置" in onebot_text or "未配置" in http_text:
            dialogs.tell(
                self, "桥接进程已启动",
                "qqbridge 进程已启动，但 OneBot 还没有可用账号。\n"
                "请先在 WhaleQQGateway 里启动并登录 QQ，再回来看状态。\n"
                "状态每 10 秒自动刷新一次。", kind="warning")
        else:
            onebot_name = onebot_text.split("：")[0]
            http_name = http_text.split("：")[0]
            dialogs.tell(
                self, "桥接进程已启动",
                "qqbridge 进程已启动，但 %s / %s 仍离线。\n"
                "请检查 WhaleQQGateway 是否运行、QQ 是否已登录。\n"
                "状态每 10 秒自动刷新一次。" % (onebot_name, http_name),
                kind="warning")

    def open_console(self):
        QDesktopServices.openUrl(QUrl("http://127.0.0.1:3100"))

    def open_bridge_dir(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(BRIDGE)))

    def load_bridge_log(self):
        path = BRIDGE / "state" / "bridge.out.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[-30000:]
        except OSError:
            text = "还没有桥接日志。"
        self.log_view.setPlainText(text)
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
