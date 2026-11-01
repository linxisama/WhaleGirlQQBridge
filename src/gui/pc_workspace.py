"""电脑控制页的两块新面板：本地工作空间 + 常用网址收藏夹。

工作空间的边界不在这里判断：所有路径都先过 agent_workspace.resolve()
（越界、'..' 穿越、UNC、控制字符、符号链接一律抛 WorkspaceError(code='denied')），
本模块只把结构化拒绝翻成用户能看懂的中文，绝不自己拼一个"看起来在工作区里"的路径。

agent_workspace.root() 只算路径、不建目录（见该模块 root() 文档），
所以根目录由 workspace_root() 自己 mkdir。

收藏夹落盘位置：<data_dir>/qq-only/pc_bookmarks.json。选它而不选 configure()/bot 配置
的理由：WorkbenchTab 由 qq_gui.py 无参构造，手边没有 bot 也没有配置对象，拿不到
configure(...)；而这份列表是纯界面偏好，塞进 bot 配置会被配置迁移/校验一起带着走，
还要为它加一个键。读失败（文件缺失/坏 JSON/项不合法）一律降级成默认三项。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from src import agent_workspace
from . import dialogs

AGENT_ID = "console"             # 与 src/console_agent.py:177 NOTE_AGENT 同一个工作区
MAX_ENTRIES = 500                # 一次最多列这么多项，超出在界面写明
BOOKMARK_FILE = "pc_bookmarks.json"
DEFAULT_BOOKMARKS = (
    {"name": "B站", "url": "https://www.bilibili.com"},
    {"name": "微博", "url": "https://weibo.com"},
    {"name": "QQ邮箱", "url": "https://mail.qq.com"},
)
ILLEGAL_NAME_CHARS = '\\/:*?"<>|'


# ── 数据根 / 工作区根 ───────────────────────────────────────────────────

def default_data_dir() -> Path:
    """<data_dir>：显式 WHALE_QQ_DATA_DIR / WHALE_QQ_HOME 优先，其余交给 src.data_root。

    最后一级回退以前是 __file__ 往上退两级（源码下正好是仓库根，冻结下却是解包目录
    _internal，写进去等于写安装目录）。现在与 src/qq_app.py 的 DATA_ROOT 同一个函数：
    源码=仓库根/data，冻结=%LOCALAPPDATA%/WhaleGirlQQ/data。
    src/bot_control_api.data_dir_candidates 只是探活用的候选列表（含旧位置），不是这里的口径。
    """
    explicit = str(os.environ.get("WHALE_QQ_DATA_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    home = str(os.environ.get("WHALE_QQ_HOME") or "").strip()
    if home:
        return Path(home) / "data"
    from ..data_root import resolve_data_root
    return resolve_data_root() / "data"


def workspace_root(data_dir=None, agent_id: str = AGENT_ID) -> Path:
    """工作区根目录；不存在就建（agent_workspace.root() 本身不建目录）。"""
    base = Path(str(data_dir)) if data_dir else default_data_dir()
    path = agent_workspace.root(base, agent_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _base(data_dir) -> Path:
    return Path(str(data_dir)) if data_dir else default_data_dir()


def _fail(code: str, message: str, **details) -> dict:
    error = {"code": str(code), "message": str(message)}
    if details:
        error["details"] = {str(k): str(v) for k, v in details.items()}
    return {"ok": False, "error": error}


def _join(relpath: str, name: str) -> str:
    head = str(relpath or "").strip().strip("/")
    return "/".join(part for part in (head, str(name)) if part)


def _parent(relpath: str) -> str:
    parts = [part for part in str(relpath or "").split("/") if part]
    return "/".join(parts[:-1])


def name_problem(name) -> str:
    """文件夹名的可接受性；返回空串表示通过，否则返回一句中文原因。"""
    text = str(name or "")
    if not text.strip():
        return "文件夹名称不能为空"
    if text in (".", ".."):
        return "不能用「" + text + "」做文件夹名"
    for char in ILLEGAL_NAME_CHARS:
        if char in text:
            return '名称里不能有 \\ / : * ? " < > | 这些字符'
    for char in text:
        if ord(char) < 32 or ord(char) == 127:
            return "名称里不能有控制字符"
    if text != text.strip():
        return "名称首尾不能有空格"
    if text.endswith("."):
        return "名称不能以句点结尾"
    return ""


def human_size(size) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return ""


# ── 列目录 / 新建 / 删除（纯逻辑，界面与测试共用）────────────────────────

def list_entries(relpath: str = "", *, data_dir=None, agent_id: str = AGENT_ID) -> dict:
    """列工作区里一个目录；越界抛出的 WorkspaceError 在这里翻成中文结构化拒绝。"""
    try:
        path = agent_workspace.resolve(_base(data_dir), agent_id, relpath)
        if not path.exists():
            return _fail("not_found", "这个文件夹不存在了，点「刷新」重新读一次")
        if not path.is_dir():
            return _fail("invalid", "选中的不是文件夹")
        entries = []
        truncated = False
        try:
            with os.scandir(str(path)) as handle:
                for item in handle:
                    if len(entries) >= MAX_ENTRIES:
                        truncated = True
                        break
                    try:
                        info = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    is_dir = item.is_dir(follow_symlinks=False)
                    entries.append({
                        "name": item.name,
                        "type": "dir" if is_dir else "file",
                        "size": 0 if is_dir else int(getattr(info, "st_size", 0)),
                        "mtime": float(getattr(info, "st_mtime", 0.0)),
                        "relpath": _join(relpath, item.name),
                    })
        except OSError as exc:
            return _fail("io", "读这个文件夹失败：" + type(exc).__name__)
    except agent_workspace.WorkspaceError as exc:
        return exc.to_dict()
    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    return {"ok": True, "path": str(path), "relpath": str(relpath or ""),
            "entries": entries, "truncated": truncated, "limit": MAX_ENTRIES}


def make_folder(name, relpath: str = "", *, data_dir=None, agent_id: str = AGENT_ID) -> dict:
    """在工作区里新建一个文件夹；名字不合法或已存在都直接拒绝。"""
    problem = name_problem(name)
    if problem:
        return _fail("invalid", problem)
    try:
        base = _base(data_dir)
        agent_workspace.resolve(base, agent_id, relpath)
        target = agent_workspace.resolve(base, agent_id, _join(relpath, str(name)))
        if target.exists():
            return _fail("exists", "「" + str(name) + "」已经存在")
        target.mkdir()
    except agent_workspace.WorkspaceError as exc:
        return exc.to_dict()
    except OSError as exc:
        return _fail("io", "建文件夹失败：" + type(exc).__name__)
    return {"ok": True, "path": str(target), "relpath": _join(relpath, str(name))}


def delete_entry(relpath: str, *, recursive: bool = False, data_dir=None,
                 agent_id: str = AGENT_ID) -> dict:
    """删工作区里的一个文件/文件夹；非空文件夹要 recursive=True 才动。"""
    try:
        base = _base(data_dir)
        target = agent_workspace.resolve(base, agent_id, relpath)
        root = agent_workspace.root(base, agent_id)
        if os.path.normcase(os.path.abspath(str(target))) == \
                os.path.normcase(os.path.abspath(str(root))):
            return _fail("denied", "工作空间根目录不能删除")
        if not target.exists():
            return _fail("not_found", "这个文件已经不在了，点「刷新」重新读一次")
        if target.is_dir():
            try:
                empty = not any(target.iterdir())
            except OSError as exc:
                return _fail("io", "读文件夹失败：" + type(exc).__name__)
            if not empty and not recursive:
                return _fail("not_empty", "文件夹里还有内容，删掉会把里面的东西一起删")
            shutil.rmtree(str(target))
        else:
            target.unlink()
    except agent_workspace.WorkspaceError as exc:
        return exc.to_dict()
    except OSError as exc:
        return _fail("io", "删除失败：" + type(exc).__name__)
    return {"ok": True, "relpath": str(relpath or ""), "path": str(target)}


# ── 收藏夹 ──────────────────────────────────────────────────────────────

def bookmarks_path(data_dir=None) -> Path:
    return _base(data_dir) / "qq-only" / BOOKMARK_FILE


def default_bookmarks() -> list:
    return [dict(item) for item in DEFAULT_BOOKMARKS]


def _clean_bookmark(item):
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    url = str(item.get("url") or "").strip()
    if not name or not url.startswith(("http://", "https://")):
        return None
    return {"name": name, "url": url}


def load_bookmarks(path=None) -> list:
    """读收藏夹；文件缺失/JSON 坏了/项全不合法都降级成默认三项，不抛异常。

    文件里存的是空数组时返回空列表（用户把收藏全删了，不该自己长回来）。
    """
    target = Path(str(path)) if path else bookmarks_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_bookmarks()
    if not isinstance(raw, list):
        return default_bookmarks()
    items = [clean for clean in (_clean_bookmark(item) for item in raw) if clean]
    if raw and not items:
        # 文件里有内容但一条都不合法：当作坏了，退回默认三项。
        return default_bookmarks()
    return items


def save_bookmarks(items, path=None) -> bool:
    target = Path(str(path)) if path else bookmarks_path()
    cleaned = [clean for clean in (_clean_bookmark(item) for item in (items or [])) if clean]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    except OSError:
        return False
    return True


# ── 界面：工作空间面板 ──────────────────────────────────────────────────

def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color:#5b7188;font-size:12px;")
    return label


class WorkspacePanel(QWidget):
    """工作空间文件表：双击进目录/开文件，可新建文件夹、删除选中。"""

    COLUMNS = ("名称", "大小", "修改时间")

    def __init__(self, data_dir=None, agent_id: str = AGENT_ID, parent=None):
        super().__init__(parent)
        self.agent_id = agent_id
        self.data_dir = _base(data_dir)
        self.relpath = ""
        self.entries: list = []
        self.root_error = ""
        self.root = self.data_dir
        try:
            self.root = workspace_root(self.data_dir, agent_id)
        except (OSError, agent_workspace.WorkspaceError) as exc:
            self.root_error = "工作空间根目录打不开：" + str(exc)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.root_label = QLabel("工作空间：" + str(self.root))
        self.root_label.setWordWrap(True)
        self.root_label.setStyleSheet("color:#17324d;font-size:12px;")
        layout.addWidget(self.root_label)

        row = QHBoxLayout()
        self.open_dir_btn = QPushButton("打开目录")
        self.open_dir_btn.clicked.connect(self.open_root)
        self.new_folder_btn = QPushButton("新建文件夹")
        self.new_folder_btn.clicked.connect(self.create_folder)
        self.delete_btn = QPushButton("删除选中")
        self.delete_btn.clicked.connect(self.delete_selected)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.reload)
        self.up_btn = QPushButton("上一级")
        self.up_btn.clicked.connect(self.go_up)
        for button in (self.open_dir_btn, self.new_folder_btn, self.delete_btn,
                       self.refresh_btn, self.up_btn):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(180)
        self.table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#5b7188;font-size:12px;")
        layout.addWidget(self.status)

        self.reload()

    # 读盘 ─────────────────────────────────────────────────────────────
    def current_path_text(self) -> str:
        if not self.relpath:
            return str(self.root)
        return str(self.root / self.relpath.replace("/", os.sep))

    def reload(self):
        return self._load(self.relpath)

    def _load(self, relpath: str):
        result = list_entries(relpath, data_dir=self.data_dir, agent_id=self.agent_id)
        if not result.get("ok"):
            self.status.setText(str((result.get("error") or {}).get("message") or "读目录失败"))
            return False
        self.relpath = str(result.get("relpath") or "")
        self.entries = list(result.get("entries") or [])
        self._fill_table()
        notes = ["当前位置：" + self.current_path_text()]
        if self.relpath:
            notes.append("双击文件用默认程序打开，双击文件夹进去。")
        if result.get("truncated"):
            notes.append("仅显示前 %d 项" % MAX_ENTRIES)
        self.status.setText(" · ".join(notes))
        return True

    def _fill_table(self):
        self.table.setRowCount(len(self.entries))
        for index, entry in enumerate(self.entries):
            name = entry["name"] + ("/" if entry["type"] == "dir" else "")
            self.table.setItem(index, 0, QTableWidgetItem(name))
            self.table.setItem(index, 1, QTableWidgetItem(human_size(entry["size"])))
            moment = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["mtime"]))
            self.table.setItem(index, 2, QTableWidgetItem(moment))
        if self.entries:
            self.table.selectRow(0)

    def selected_entry(self):
        model = self.table.selectionModel()
        rows = model.selectedRows() if model is not None else []
        if not rows:
            return None
        index = int(rows[0].row())
        if 0 <= index < len(self.entries):
            return self.entries[index]
        return None

    # 动作 ─────────────────────────────────────────────────────────────
    @staticmethod
    def _open_local(path) -> bool:
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))

    def open_root(self):
        if not self._open_local(self.root):
            dialogs.tell(self, "打不开目录", str(self.root), kind="warning")

    def open_selected(self):
        entry = self.selected_entry()
        if entry is None:
            self.status.setText("先选中一个文件或文件夹")
            return
        if entry["type"] == "dir":
            self._load(entry["relpath"])
            return
        target = self.root / entry["relpath"].replace("/", os.sep)
        if not self._open_local(target):
            dialogs.tell(self, "打不开文件", str(target), kind="warning")

    def _on_double_click(self, index):
        try:
            row = int(index.row())
        except (AttributeError, TypeError):
            return
        if 0 <= row < len(self.entries):
            self.table.selectRow(row)
            self.open_selected()

    def go_up(self):
        if not self.relpath:
            self.status.setText("已经在工作空间根目录")
            return
        self._load(_parent(self.relpath))

    def create_folder(self):
        name, ok = dialogs.ask_text(self, "新建文件夹", "文件夹名称（建在当前这一层）")
        if not ok:
            return None
        result = make_folder(name, self.relpath, data_dir=self.data_dir, agent_id=self.agent_id)
        if not result.get("ok"):
            dialogs.tell(self, "没能新建",
                         str((result.get("error") or {}).get("message") or ""), kind="warning")
            return result
        self.reload()
        return result

    def delete_selected(self):
        entry = self.selected_entry()
        if entry is None:
            self.status.setText("先选中要删除的文件或文件夹")
            return None
        if not dialogs.ask_confirm(self, "删除确认", "要删除「" + entry["name"] + "」吗？",
                                   detail="删除后不进回收站，没法撤销。",
                                   ok_text="删除", danger=True):
            return None
        result = delete_entry(entry["relpath"], data_dir=self.data_dir, agent_id=self.agent_id)
        if not result.get("ok"):
            error = result.get("error") or {}
            if str(error.get("code") or "") != "not_empty":
                dialogs.tell(self, "删除被拒绝", str(error.get("message") or ""), kind="warning")
                return result
            if not dialogs.ask_confirm(
                    self, "这个文件夹不是空的",
                    "「" + entry["name"] + "」里还有东西，连着里面全部删掉吗？",
                    detail="里面的文件会一起消失，没法撤销。",
                    ok_text="全部删掉", danger=True):
                return result
            result = delete_entry(entry["relpath"], recursive=True,
                                  data_dir=self.data_dir, agent_id=self.agent_id)
            if not result.get("ok"):
                error = result.get("error") or {}
                dialogs.tell(self, "删除被拒绝", str(error.get("message") or ""), kind="warning")
                return result
        self.reload()
        return result


# ── 界面：网址收藏夹 ───────────────────────────────────────────────────

class BookmarkBar(QWidget):
    """一排常用网站按钮，点一下用默认浏览器打开；右键删除，末尾「＋ 添加」。"""

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self.path = Path(str(path)) if path else bookmarks_path()
        self.items = load_bookmarks(self.path)
        self.buttons: list = []
        self.add_button = QPushButton("＋ 添加")
        self.add_button.clicked.connect(self.add_bookmark)
        self.empty_label = QLabel("还没有收藏的网址，点「＋ 添加」加一个")
        self.empty_label.setStyleSheet("color:#5b7188;font-size:12px;")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._rebuild()

    def _rebuild(self):
        while self._row.count():
            self._row.takeAt(0)
        for button in self.buttons:
            button.setParent(None)
            button.deleteLater()
        self.buttons = []
        if not self.items:
            self._row.addWidget(self.empty_label)
        for index, item in enumerate(self.items):
            button = QPushButton(item["name"])
            button.setToolTip(item["url"] + "（右键可以删除）")
            button.clicked.connect(lambda _checked=False, url=item["url"]: self._open(url))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, index=index, button=button: self._menu(index, button, pos))
            self._row.addWidget(button)
            self.buttons.append(button)
        self._row.addWidget(self.add_button)
        self._row.addStretch()

    def _open(self, url: str):
        if not QDesktopServices.openUrl(QUrl(url)):
            dialogs.tell(self, "打不开网址", url, kind="warning")

    def _menu(self, index: int, button, pos):
        menu = QMenu(button)
        remove = menu.addAction("删除这个网址")
        chosen = menu.exec(button.mapToGlobal(pos))
        if chosen is remove:
            self.remove_bookmark(index)

    def add_bookmark(self):
        name, ok = dialogs.ask_text(self, "添加网址", "名称，例如 B站")
        if not ok or not str(name).strip():
            return None
        url, ok = dialogs.ask_text(self, "添加网址", "网址，要以 http:// 或 https:// 开头")
        if not ok:
            return None
        problem = _bookmark_problem(name, url)
        if problem:
            dialogs.tell(self, "没能添加", problem, kind="warning")
            return None
        self.items.append({"name": str(name).strip(), "url": str(url).strip()})
        self._persist()
        self._rebuild()
        return self.items[-1]

    def remove_bookmark(self, index: int):
        if 0 <= index < len(self.items):
            del self.items[index]
            self._persist()
            self._rebuild()
            return True
        return False

    def _persist(self):
        if not save_bookmarks(self.items, self.path):
            dialogs.tell(self, "没能保存", "写不进收藏夹文件：" + str(self.path), kind="warning")


def _bookmark_problem(name, url) -> str:
    if not str(name or "").strip():
        return "名称不能为空"
    text = str(url or "").strip()
    if not text.startswith(("http://", "https://")):
        return "网址要以 http:// 或 https:// 开头"
    return ""
