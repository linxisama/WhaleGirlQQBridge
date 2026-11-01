"""Native memory/trigger controls and local PC request review."""
import time
from pathlib import Path
from urllib.parse import quote
from PyQt6.QtCore import Qt,QTimer,QThread,pyqtSignal,QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,
    QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QCheckBox,QComboBox,QInputDialog,QMessageBox,QTabWidget,QTextEdit,QDialog)
from .diagnostics import safe_slot
from .dialogs import ask_confirm

_PANEL_ALPHA = 1.0


def _num(value, default: float, low: float, high: float) -> float:
    """界面用的安全数值转换：后台返回的 "abc"/None/列表一律回退默认值并夹在区间内。

    Qt 槽里抛异常会弹"操作未完成"（装了 excepthook 时）或直接 abort 进程，
    所以界面读取外部数据时不能裸 int()/float()。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:            # NaN
        return float(default)
    return max(low, min(high, number))


def panel_stylesheet(alpha: float = None) -> str:
    """这个页的样式表：卡片用**半透明**底色，才能透出背景图和「面板透明度」。

    以前这里写死 ``#ffffff``，等于把整页盖成一张不透明白纸 —— 背景图看不到，
    外观页的「面板透明度」也管不到这个页（它自带样式表，不跟随主窗口那条替换规则）。
    """
    value = _PANEL_ALPHA if alpha is None else max(0.0, min(1.0, float(alpha)))
    card = f"rgba(255,255,255,{value:.3f})"
    return (
        'QWidget {color:#26364b;background:transparent;}'
        'QLabel,QCheckBox {color:#34506d;background:transparent;}'
        f'QTableWidget {{background:{card};color:#26364b;border:1px solid rgba(209,220,232,{value:.3f});'
        'gridline-color:rgba(226,232,240,0.85);}'
        'QHeaderView::section {background:rgba(232,238,245,0.92);color:#34506d;padding:7px;border:0;}'
        f'QLineEdit,QComboBox {{background:{card};color:#26364b;border:1px solid #cbd8e8;'
        'border-radius:6px;padding:8px;}'
        'QTableWidget::item:selected {background:#d7eeec;color:#20364a;}'
        'QPushButton {background:#2da6a0;color:white;border:0;border-radius:6px;padding:9px 13px;}'
        'QPushButton:hover {background:#218e88;} QPushButton:disabled {background:#e1e8f0;color:#68778c;}'
    )


def appearance(widget, alpha: float = None):
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(panel_stylesheet(alpha))


def info_stylesheet(alpha: float = None) -> str:
    """摘要框（浅蓝卡片）也要跟着面板透明度走。"""
    value = _PANEL_ALPHA if alpha is None else max(0.0, min(1.0, float(alpha)))
    return f'background:rgba(238,246,250,{value:.3f});color:#26364b;padding:12px;border-radius:8px;'

def table(headers):
    t=QTableWidget(0,len(headers));t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return t

class MemoryTab(QWidget):
    bindingRequested=pyqtSignal()
    def __init__(self,get,post,configure):
        super().__init__();appearance(self);self.get=get;self.post=post;self.configure=configure;self.manual=[];self.structured=[]
        l=QVBoxLayout(self);self.title=QLabel('对话与记忆');l.addWidget(self.title)
        row=QHBoxLayout()
        for label,pid in [('回复触发与黑名单','chat_controls'),('连续跟聊','short_followup'),('人设隔离','archive_isolation'),('跨会话偏好','cross_chat_link')]:
            b=QPushButton(label);b.clicked.connect(lambda _,v=pid:self.configure(v));row.addWidget(b)
        l.addLayout(row)
        bindings=QPushButton('按群 / 用户分配人设');bindings.clicked.connect(lambda checked=False:self.bindingRequested.emit());l.addWidget(bindings)
        self.auto=QCheckBox('自动记录明确偏好（两条独立消息后确认）');self.auto.clicked.connect(lambda value:self.change({'action':'auto','enabled':value}));l.addWidget(self.auto)
        sub=QTabWidget();l.addWidget(sub,1)
        manual=QWidget();m=QVBoxLayout(manual);sub.addTab(manual,'自我记忆')
        m.addWidget(QLabel('手动记忆按人设保存，开关开启后会进入下一轮提示。'))
        self.manual_table=table(['启用','内容']);m.addWidget(self.manual_table)
        row=QHBoxLayout();self.input=QLineEdit();self.input.setPlaceholderText('例如：谈到加班时，先关心对方的状态');row.addWidget(self.input,1)
        add=QPushButton('添加');add.clicked.connect(self.add);row.addWidget(add);m.addLayout(row)
        remove=QPushButton('删除选中的手动记忆');remove.clicked.connect(self.remove);m.addWidget(remove)
        structured=QWidget();s=QVBoxLayout(structured);sub.addTab(structured,'群友与个人档案')
        self.search=QLineEdit();self.search.setPlaceholderText('搜索记忆内容，按回车查找');self.search.returnPressed.connect(self.refresh);s.addWidget(self.search)
        self.facts=table(['内容','确认状态','证据条数','会话 / 用户']);s.addWidget(self.facts)
        delete=QPushButton('删除选中档案');delete.clicked.connect(self.forget);s.addWidget(delete)
        diagnostic=QWidget();d=QVBoxLayout(diagnostic);sub.addTab(diagnostic,'未回复诊断')
        d.addWidget(QLabel('上半张表是**平台推过来的每一条事件**（含群聊）：如果这里没有你要问的那条群消息，'
                           '说明 QQ 平台没有把它推给机器人（官方 QQ 只推送 @ 机器人的群消息），不是本程序丢的。'))
        self.inbound=table(['收到时间','事件','会话','发送者','内容','结果与原因']);d.addWidget(self.inbound,1)
        d.addWidget(QLabel('下半张表是**本程序内部的处置记录**（进队列 / 被哪条规则拦下）。'))
        self.decisions=table(['会话','发送者','处理原因']);d.addWidget(self.decisions)
        unified=QWidget(); u=QVBoxLayout(unified)
        u.addWidget(QLabel('分层记忆：关于我 / 关于你 / 项目 / 事实 / 教训 / 话题 / 相处原则；'
                            'QQ、桌面 Agent 共用，群聊不会读取“关于你/关于我”。'))
        self.unified_layers=QLabel('记忆层统计：—')
        u.addWidget(self.unified_layers)
        row=QHBoxLayout(); self.unified_search=QLineEdit()
        self.unified_search.setPlaceholderText('搜索统一记忆，回车查找')
        self.unified_search.returnPressed.connect(self.refresh_unified)
        row.addWidget(self.unified_search,1)
        add=QPushButton('添加');add.clicked.connect(self.add_unified);row.addWidget(add)
        edit=QPushButton('纠正内容');edit.clicked.connect(self.edit_unified);row.addWidget(edit)
        delete=QPushButton('删除选中');delete.clicked.connect(self.remove_unified);row.addWidget(delete)
        dream=QPushButton('做梦整理');dream.clicked.connect(self.dream_unified);row.addWidget(dream)
        maintain=QPushButton('维护 / 遗忘');maintain.clicked.connect(self.maintain_unified);row.addWidget(maintain)
        u.addLayout(row)
        self.unified_table=table(['层','项目','来源','内容','置信','使用']);u.addWidget(self.unified_table,1)
        u.addWidget(QLabel('最近的日记'))
        self.diary_view=QTextEdit();self.diary_view.setReadOnly(True);self.diary_view.setMaximumHeight(150)
        u.addWidget(self.diary_view)
        diary_row=QHBoxLayout(); diary_row.addStretch()
        self.open_diary_button=QPushButton('打开日记本')
        self.open_diary_button.clicked.connect(self.open_diary)
        diary_row.addWidget(self.open_diary_button); u.addLayout(diary_row)
        sub.addTab(unified,'统一记忆 / 日记')
        bottom_row=QHBoxLayout()
        refresh=QPushButton('刷新');refresh.clicked.connect(self.refresh);bottom_row.addWidget(refresh)
        self.maintain_button=QPushButton('整理记忆（合并重复 / 清理过期）')
        self.maintain_button.clicked.connect(self.maintain_memory)
        bottom_row.addWidget(self.maintain_button)
        self.check_button=QPushButton('记忆体检（只看不改）')
        self.check_button.clicked.connect(self.check_memory)
        bottom_row.addWidget(self.check_button)
        self.report_button=QPushButton('导出体检报告')
        self.report_button.clicked.connect(self.export_report)
        bottom_row.addWidget(self.report_button)
        self.view_report_button=QPushButton('查看报告')
        self.view_report_button.clicked.connect(self.view_report)
        bottom_row.addWidget(self.view_report_button)
        self.backups_button=QPushButton('备份列表')
        self.backups_button.clicked.connect(self.show_backups)
        bottom_row.addWidget(self.backups_button)
        self.ingest_button=QPushButton('从最近私聊提取记忆（零 token）')
        self.ingest_button.clicked.connect(self.ingest_memory)
        bottom_row.addWidget(self.ingest_button)
        bottom_row.addStretch()
        l.addLayout(bottom_row)
        self.notice=QLabel();self.notice.setWordWrap(True);l.addWidget(self.notice)
    def apply_panel_alpha(self,alpha,glass=False):
        """跟随外观页的「面板透明度」：本页自带样式表，主窗口的替换规则到不了这里。

        glass=True 表示背后有背景图：alpha 已经在 qq_gui.panel_alpha 里压薄过，
        这里直接用即可（表格直接铺在透明页面上，没有叠加层）。
        """
        self.setStyleSheet(panel_stylesheet(alpha))
    @safe_slot(label="整理记忆")
    def maintain_memory(self):
        self._run_maintain(True)

    @safe_slot(label="记忆体检")
    def check_memory(self):
        self._run_maintain(False)

    @safe_slot(label="提取记忆")
    def ingest_memory(self):
        """零 token 提取：先预览候选，确认后才写 fact 层（不写群聊、不写密钥）。"""
        preview = self.get('memory/ingest?days=7&limit=20') or {}
        if not preview.get('success'):
            self.notice.setText(preview.get('message') or '提取没开始：后台尚未就绪。')
            return
        candidates = preview.get('candidates') or []
        self.notice.setText(str(preview.get('message') or ''))
        if not candidates:
            return
        lines = '\n'.join('· ' + str(item.get('text') or '')[:60] for item in candidates[:5])
        more = '' if len(candidates) <= 5 else f'\n…另有 {len(candidates) - 5} 条'
        if not ask_confirm(self, '从最近私聊提取记忆',
                           f'找到 {len(candidates)} 条候选（规则提取，不花 token）：\n{lines}{more}\n\n'
                           '写入后会出现在「统一记忆 / 日记」的事实层，确认写入吗？'):
            return
        result = self.post('memory/ingest', {'apply': True, 'days': 7, 'limit': 20}) or {}
        if not result.get('success'):
            self.notice.setText(result.get('message') or '写入没完成，请稍后再试。')
            return
        self.notice.setText(str(result.get('message') or ('已写入 ' + str(result.get('added', 0)) + ' 条记忆')))
        self.refresh()

    @safe_slot(label="导出体检报告")
    def export_report(self):
        """把只读体检写成 Markdown 文件；报告路径回显在下方提示里。"""
        result = self.post('memory/report', {}) or {}
        if not result.get('success'):
            self.notice.setText(result.get('message') or '报告导出失败，请稍后再试。')
            return
        path = str(result.get('path') or '')
        self.notice.setText(('体检报告已导出：' + path) if path else '体检报告已导出。')

    @safe_slot(label="查看体检报告")
    def view_report(self):
        """生成最新报告后在本机弹窗里显示；写文件只是附带，不改任何记忆行。"""
        result = self.post('memory/report', {}) or {}
        if not result.get('success'):
            self.notice.setText(result.get('message') or '报告生成失败，请稍后再试。')
            return
        path = str(result.get('path') or '')
        try:
            text = Path(path).read_text(encoding='utf-8') if path else ''
        except OSError:
            text = ''
        dialog = QDialog(self)
        dialog.setWindowTitle('记忆体检报告')
        dialog.resize(760, 620)
        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text or '报告还没生成，请先点「导出体检报告」。')
        layout.addWidget(view)
        close = QPushButton('关闭')
        close.clicked.connect(dialog.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        dialog.exec()
        self.notice.setText(('体检报告：' + path) if path else '体检报告已生成。')

    @safe_slot(label="备份列表")
    def show_backups(self):
        """列出整理前自动生成的记忆库备份，并提供打开备份文件夹。"""
        result = self.get('memory/backups?limit=20') or {}
        if not result.get('success'):
            self.notice.setText(result.get('message') or '读取备份列表失败，请稍后再试。')
            return
        items = result.get('items') or []
        folder = str(result.get('dir') or '')
        dialog = QDialog(self)
        dialog.setWindowTitle('记忆备份')
        dialog.resize(720, 420)
        layout = QVBoxLayout(dialog)
        if items:
            table = QTableWidget(0, 3)
            table.setHorizontalHeaderLabels(['时间', '文件名', '大小'])
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            for item in items:
                row = table.rowCount()
                table.insertRow(row)
                try:
                    when = time.strftime('%Y-%m-%d %H:%M', time.localtime(float(item.get('mtime') or 0)))
                except (TypeError, ValueError, OSError):
                    when = ''
                size = int(item.get('bytes') or 0)
                table.setItem(row, 0, QTableWidgetItem(when))
                table.setItem(row, 1, QTableWidgetItem(str(item.get('name') or '')))
                table.setItem(row, 2, QTableWidgetItem(f'{size / 1024:.0f} KB' if size else ''))
            layout.addWidget(table, 1)
        else:
            hint = QLabel('还没有备份。整理记忆或记忆每日维护后，会在这里出现。')
            hint.setWordWrap(True)
            layout.addWidget(hint, 1)
        row = QHBoxLayout()
        if folder:
            open_button = QPushButton('打开备份文件夹')
            open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
            row.addWidget(open_button)
        row.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(dialog.accept)
        row.addWidget(close)
        layout.addLayout(row)
        dialog.exec()
        self.notice.setText(('备份 ' + str(len(items)) + ' 份：' + folder) if folder else '还没有备份。')

    def _run_maintain(self, apply):
        """整理/体检共用一个槽：apply=True 才动数据。结果只报数量，不回显记忆正文。"""
        result = self.post('memory/maintain', {'apply': bool(apply)}) or {}
        if not result.get('success'):
            self.notice.setText(result.get('message') or '整理未完成，请稍后再试。')
            return
        text = str(result.get('message') or '')
        if not text:
            text = (f"合并重复 {result.get('merged', 0)} 条、清理被覆盖 "
                    f"{result.get('superseded_removed', 0)} 条、清理过期候选 "
                    f"{result.get('candidates_removed', 0)} 条")
        head = '已整理' if apply else '体检完成（未改动）'
        self.notice.setText(f"{head}：{text}；剩余 {result.get('remaining', 0)} 条。")
        if apply:
            self.refresh()

    @safe_slot(label="对话与记忆")
    def refresh(self):
        """刷新整页（自我记忆 / 群友档案 / 未回复诊断）。

        这一页以前会因为**一条数据形状不对就整页崩**，用户看到的是"操作未完成，错误已记录"
        弹窗、表格全空。实测两个真问题：
        1. 用了 ``time.strftime`` 却没 ``import time`` —— 只要"未回复诊断"里有一条入站记录
           就抛 NameError（用户在那台机器上就是这么崩的）；
        2. ``v['scope']+' / '+v['subject']``、``v['kind']``、``v['time']`` 等直接下标，
           后端字段一缺就 KeyError/TypeError。
        现在：缺字段一律给默认值，每张表各自兜异常 —— 一块坏数据不该让整页不可用，
        而且 Qt 槽里抛异常会让 PyQt 直接 abort 整个进程。
        """
        result = self.get('memory?q=' + quote(self.search.text())) or {}
        if not result.get('success'):
            self.notice.setText(result.get('message') or '后台尚未就绪')
            return
        problems = []
        self.title.setText('对话与记忆 · ' + str(result.get('persona') or ''))
        self.auto.setChecked(bool(result.get('autoRemember')))

        # ① 自我记忆（手动记忆）
        try:
            self.manual = list(result.get('manual') or [])
            self.manual_table.setRowCount(len(self.manual))
            for i, v in enumerate(self.manual):
                check = QCheckBox()
                check.setChecked(bool(v.get('enabled')))
                check.clicked.connect(lambda value, mid=str(v.get('id') or ''):
                                      self.change({'action': 'enable', 'id': mid, 'enabled': value}))
                self.manual_table.setCellWidget(i, 0, check)
                self.manual_table.setItem(i, 1, QTableWidgetItem(str(v.get('content') or '')))
        except Exception as exc:
            problems.append(f'自我记忆：{exc}')

        # ② 群友与个人档案
        try:
            self.structured = list(result.get('structured') or [])
            self.facts.setRowCount(len(self.structured))
            for i, v in enumerate(self.structured):
                scope = str(v.get('scope') or '-')
                subject = str(v.get('subject') or '-')
                cells = [str(v.get('content') or ''),
                         '已确认' if v.get('status') == 'confirmed' else '待复证',
                         str(v.get('evidence') or 0),
                         f'{scope} / {subject}']
                for col, text in enumerate(cells):
                    self.facts.setItem(i, col, QTableWidgetItem(text))
        except Exception as exc:
            problems.append(f'档案：{exc}')

        status = self.get('status') or {}

        # ③ 未回复诊断：内部处置记录
        try:
            decisions = list(reversed(status.get('decisions') or []))
            self.decisions.setRowCount(len(decisions))
            for i, v in enumerate(decisions):
                kind = str(v.get('kind') or '')
                target = str(v.get('target') or '')
                cells = [f'{kind}:{target}'.strip(':'), str(v.get('sender') or ''),
                         str(v.get('reason') or '')]
                for col, text in enumerate(cells):
                    self.decisions.setItem(i, col, QTableWidgetItem(text))
        except Exception as exc:
            problems.append(f'处置记录：{exc}')

        # ④ 入站审计：平台推过来的每一条事件都在这（含被拦下的原因）
        if hasattr(self, 'inbound'):
            try:
                inbound = list(status.get('inbound') or [])
                self.inbound.setRowCount(len(inbound))
                outcome_names = {'queued': '已进入回复队列', 'handled': '已处理', 'dropped': '被拦下',
                                 'received': '收到', 'unparsed': '无法解析', 'ignored': '非聊天事件'}
                for i, v in enumerate(inbound):
                    kind = ('群' if v.get('kind') == 'group'
                            else '私聊' if v.get('kind') == 'private' else (v.get('kind') or '-'))
                    outcome = outcome_names.get(v.get('outcome'), v.get('outcome') or '')
                    stamp = v.get('time')
                    try:
                        when = time.strftime('%H:%M:%S', time.localtime(float(stamp)))
                    except (TypeError, ValueError, OSError):
                        when = '-'          # 缺时间戳或格式不对：显示占位符，不要让整页崩
                    cells = [when, str(v.get('event') or ''),
                             kind + ' ' + str(v.get('room') or '').split(':')[-1][:10],
                             str(v.get('sender') or '')[:10],
                             str(v.get('text') or '')[:40],
                             (outcome + '：' + str(v.get('reason') or '')).strip('：')]
                    for col, text in enumerate(cells):
                        self.inbound.setItem(i, col, QTableWidgetItem(text))
            except Exception as exc:
                problems.append(f'未回复诊断：{exc}')

        try:
            self.refresh_unified()
        except Exception as exc:
            problems.append(f'统一记忆：{exc}')

        if problems:
            text = '部分内容显示失败（其余照常可用）：' + '；'.join(problems)
            self._page_notice = text
            self.notice.setText(text)
        elif getattr(self, '_page_notice', '') and self.notice.text() == self._page_notice:
            # 只清掉**本方法自己**写的那条提示；保存成功/失败之类的提示不能被刷新抹掉
            self._page_notice = ''
            self.notice.setText('')
    def open_diary(self):
        path = str(getattr(self, 'diary_path', '') or '')
        if not path or not __import__('pathlib').Path(path).is_file():
            self.notice.setText('日记文件还没生成；先点「写今天的日记」或等定时任务运行。')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def refresh_unified(self):
        data = self.get('memory/unified?limit=50&q=' + quote(self.unified_search.text().strip())) or {}
        self.unified = list(data.get('items') or [])
        labels = {'soul': '关于我', 'user': '关于你', 'project': '项目',
                  'fact': '事实', 'lesson': '教训', 'topic': '话题', 'rules': '相处原则'}
        layers = data.get('layers') or {}
        self.unified_layers.setText('记忆层统计：' + ' / '.join(
            f'{labels.get(key, key)} {int(value or 0)}' for key, value in layers.items()) if layers else '记忆层统计：—')
        self.unified_table.setRowCount(len(self.unified))
        for i, item in enumerate(self.unified):
            cells = [labels.get(str(item.get('layer') or ''), str(item.get('layer') or '')),
                     str(item.get('project') or '全局'), str(item.get('source') or ''),
                     str(item.get('content') or ''), str(item.get('confidence') or 0),
                     str(item.get('uses') or 0)]
            for col, text in enumerate(cells):
                self.unified_table.setItem(i, col, QTableWidgetItem(text))
        self.diary_path = str(data.get('diaryPath') or '')
        diary = (data.get('diaries') or [{}])[0]
        self.diary_view.setPlainText(str(diary.get('content') or '还没有日记。'))

    def add_unified(self):
        text, ok = QInputDialog.getText(self, '添加统一记忆', '内容：')
        if ok and text.strip():
            result = self.post('memory/unified', {'action': 'add', 'content': text.strip(),
                                                  'source': 'desktop', 'importance': 60})
            self.notice.setText('已写入统一记忆。' if result.get('success') else str(result.get('detail') or '写入失败'))
            self.refresh_unified()

    def edit_unified(self):
        i = self.unified_table.currentRow()
        if not (0 <= i < len(self.unified)):
            return
        item = self.unified[i]
        text, ok = QInputDialog.getText(self, '纠正记忆', '内容：', text=str(item.get('content') or ''))
        if ok and text.strip():
            result = self.post('memory/unified', {'action': 'update', 'id': item['id'],
                                                  'content': text.strip()})
            self.notice.setText('已纠正。' if result.get('success') else str(result.get('detail') or '纠正失败'))
            self.refresh_unified()

    def remove_unified(self):
        i = self.unified_table.currentRow()
        if 0 <= i < len(self.unified):
            result = self.post('memory/unified', {'action': 'forget', 'id': self.unified[i]['id']})
            self.notice.setText('已删除。' if result.get('success') else '删除失败')
            self.refresh_unified()

    def dream_unified(self):
        result = self.post('memory/unified', {'action': 'dream'})
        self.notice.setText('做梦整理完成：合并 %s 条、归档 %s 个旧话题。' % (
            result.get('merged', 0), result.get('archived_topics', 0)))
        self.refresh_unified()

    def maintain_unified(self):
        result = self.post('memory/unified', {'action': 'maintain'})
        self.notice.setText('记忆维护完成：清理过期 %s、长期未用 %s。' % (
            result.get('expired', 0), result.get('stale', 0)))
        self.refresh_unified()

    def change(self,payload):
        r=self.post('memory',payload);self.notice.setText('已保存并生效' if r.get('success') else str(r.get('detail') or r.get('message') or '保存失败'));self.refresh()
    def add(self):
        text=self.input.text().strip()
        if text:self.change({'action':'add','content':text});self.input.clear()
    def remove(self):
        i=self.manual_table.currentRow()
        if 0<=i<len(self.manual):
            mid=(self.manual[i] or {}).get('id') if isinstance(self.manual[i],dict) else None
            if mid:self.change({'action':'delete','id':mid})
    def forget(self):
        i=self.facts.currentRow()
        if 0<=i<len(self.structured):
            mid=(self.structured[i] or {}).get('id') if isinstance(self.structured[i],dict) else None
            if mid:self.change({'action':'forget','id':mid})

class ReviewWorker(QThread):
    completed=pyqtSignal(dict)
    def __init__(self,post,payload,endpoint='pc/review'):super().__init__();self.post=post;self.payload=payload;self.endpoint=endpoint
    def run(self):
        try:r=self.post(self.endpoint,self.payload,timeout=30)
        except Exception:r={'success':False,'message':'操作结果未知，请查看记录，勿重复执行'}
        self.completed.emit(r)


class PcTab(QWidget):
    def __init__(self,get,post,configure):
        super().__init__();appearance(self);self.get=get;self.post=post;self.configure=configure;self.jobs=[];self.worker=None;self._candidates=None;self._owners='';self._enabled=False;self._password_set=False
        l=QVBoxLayout(self);l.addWidget(QLabel('机器人控制台管理'))
        hint=QLabel('绑定管理员后，可私聊“机器人 状态/连接/队列/日志”直接查询；远程变更默认需要在这里审核。'
                    '设置管理员密码后，管理员可在 QQ 私聊发“进入管理员模式”再发密码，直接执行暂停、换人设、开关插件等操作（每次都记录在下面）。');hint.setWordWrap(True);l.addWidget(hint)
        row=QHBoxLayout();self.owner=QComboBox();self.owner.setMinimumWidth(250);self.owner.currentIndexChanged.connect(self.selection_changed);row.addWidget(self.owner,1);self.bind_button=QPushButton('绑定选中的管理员');self.bind_button.clicked.connect(self.bind);row.addWidget(self.bind_button)
        self.unbind_button=QPushButton('解除绑定');self.unbind_button.clicked.connect(self.unbind);row.addWidget(self.unbind_button)
        self.config_button=QPushButton('启用与设置');self.config_button.clicked.connect(self.open_settings);row.addWidget(self.config_button);l.addLayout(row)
        admin_row=QHBoxLayout();admin_row.addWidget(QLabel('管理员密码'))
        self.admin_password=QLineEdit();self.admin_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.admin_password.setPlaceholderText('至少 6 位；设置后在 QQ 私聊发“进入管理员模式”再发密码')
        admin_row.addWidget(self.admin_password,1)
        self.admin_save=QPushButton('设置密码');self.admin_save.clicked.connect(self.save_admin_password);admin_row.addWidget(self.admin_save)
        self.admin_clear=QPushButton('清除密码');self.admin_clear.clicked.connect(self.clear_admin_password);admin_row.addWidget(self.admin_clear)
        l.addLayout(admin_row)
        self.admin_state=QLabel();self.admin_state.setWordWrap(True);l.addWidget(self.admin_state)
        self.bound=QLabel();l.addWidget(self.bound)
        self.summary=QLabel();self.summary.setWordWrap(True);self.summary.setStyleSheet(info_stylesheet());l.addWidget(self.summary)
        local=QHBoxLayout()
        for text,action in [('暂停自动回复','pause'),('恢复自动回复','resume'),('重新连接 QQ','reconnect')]:
            button=QPushButton(text);button.clicked.connect(lambda _,a=action:self.local_action(a));local.addWidget(button)
        l.addLayout(local)
        self.table=table(['操作','参数','状态','结果']);l.addWidget(self.table,1)
        row=QHBoxLayout();self.approve=QPushButton('审核并执行选中操作');self.approve.clicked.connect(lambda:self.review(True));row.addWidget(self.approve)
        self.reject=QPushButton('拒绝选中操作');self.reject.clicked.connect(lambda:self.review(False));row.addWidget(self.reject)
        refresh=QPushButton('刷新');refresh.clicked.connect(self.refresh);row.addWidget(refresh);l.addLayout(row)
        self.notice=QLabel();self.notice.setWordWrap(True);l.addWidget(self.notice)
        self.timer=QTimer(self);self.timer.setInterval(3000);self.timer.timeout.connect(self.poll)

    def apply_panel_alpha(self,alpha,glass=False):
        """跟随外观页的「面板透明度」：摘要框和表格一起变透（alpha 已含玻璃压薄）。"""
        self.setStyleSheet(panel_stylesheet(alpha))
        self.summary.setStyleSheet(info_stylesheet(alpha))

    def showEvent(self,event):
        super().showEvent(event);self.timer.start()

    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)

    def closeEvent(self,event):
        self.timer.stop();super().closeEvent(event)

    def poll(self):
        # 注意：曾经把它改成后台线程取数据（避免 3 秒一次的同步请求堵 UI），
        # 但 QThread 在"控件销毁/进程退出"时的生命周期会引发**原生崩溃**
        # （实测全量回归出现 0xC0000005）。要再做这件事必须连同线程回收一起设计并补测试，
        # 所以这里回到同步实现；填充逻辑本身已经加固（缺字段只影响那一格）。
        if self.isVisible() and not self.worker:
            self.refresh()

    def refresh(self):
        self._apply(self.get('pc') or {})

    @safe_slot(label="机器人管理")
    def _apply(self, r):
        # 这一页每 3 秒刷一次：字段缺失/类型不对时以前会整块空白。
        # 现在能显示的照常显示，缺字段用占位符（后台半启动/升级中也还能用）。
        if not isinstance(r, dict) or not r.get('success'):
            self.notice.setText((r.get('message') if isinstance(r, dict) else '') or '后台尚未就绪')
            return
        robot = r.get('robot') if isinstance(r.get('robot'), dict) else {}
        queue = robot.get('queue') if isinstance(robot.get('queue'), dict) else {}
        plugins = [v for v in (robot.get('plugins') or []) if isinstance(v, dict)]
        self.summary.setText(
            f"机器人：{'运行中' if robot.get('running') else '未启动'} · "
            f"回复：{'已暂停' if robot.get('reply_paused') else '正常'} · "
            f"QQ：{'已连接' if robot.get('qq_connected') else '未连接'}\n"
            f"模型：{robot.get('model') or '未知'} · 人设：{robot.get('persona') or '未知'} · "
            f"插件启用：{sum(1 for v in plugins if v.get('enabled'))}/{len(plugins)}\n"
            f"等待处理：{queue.get('pending', 0)} · 已暂停：{queue.get('paused', 0)} · "
            f"失败：{queue.get('failed', 0)} · 发送待核实：{queue.get('uncertain', 0)}")
        admin = r.get('admin') if isinstance(r.get('admin'), dict) else {}
        self._password_set = bool(admin.get('password_set'))
        minutes = int(_num(admin.get('minutes'), 30, 1, 100000))
        active = int(_num(admin.get('active'), 0, 0, 100000))
        self.admin_save.setText('修改密码' if self._password_set else '设置密码')
        self.admin_clear.setEnabled(self._password_set)
        self.admin_state.setText(
            f'管理员密码：已设置 · 会话有效期 {minutes} 分钟 · 当前已进入的管理员 {active} 个'
            if self._password_set else
            '管理员密码：未设置（远程变更仍需在这里审核）。设置后可在 QQ 私聊发“进入管理员模式”，再发密码，直接执行管理操作。')
        self._owners = str(r.get('owners') or '')
        self._enabled = bool(r.get('enabled', False))
        candidates = [v for v in (r.get('candidates') or []) if isinstance(v, dict)]
        # Preserve the user's selection and open popup across background refreshes.
        if candidates != self._candidates and not self.owner.view().isVisible():
            selected = self.owner.currentData()
            self.owner.blockSignals(True)
            self.owner.clear()
            for index, v in enumerate(candidates, 1):
                label = self.owner_label(v, index)
                self.owner.addItem(label, v.get('id'))
                self.owner.setItemData(self.owner.count() - 1, str(v.get('id') or ''),
                                       Qt.ItemDataRole.ToolTipRole)
            selected_index = self.owner.findData(selected)
            if selected_index < 0 and self._owners:
                selected_index = self.owner.findData(self._owners.split(',')[0])
            if selected_index >= 0:
                self.owner.setCurrentIndex(selected_index)
            self.owner.blockSignals(False)
            self._candidates = candidates
        names = {str(v.get('id')): self.owner_label(v, i) for i, v in enumerate(candidates, 1)}
        bound = [names.get(v.strip(), '私聊用户 · ' + v.strip()[-6:])
                 for v in self._owners.split(',') if v.strip()]
        self.bound.setText(('已绑定：' + '、'.join(bound) if bound else '尚未绑定管理员')
                           + '  ·  ' + ('插件已启用' if self._enabled else '插件未启用'))
        self.bound.setToolTip(self._owners)
        self.config_button.setText('已启用 · 设置' if self._enabled else '启用与设置')
        self.unbind_button.setEnabled(bool(bound))
        self.selection_changed()
        jobs = [v for v in (r.get('jobs') or []) if isinstance(v, dict)]
        if not self.jobs and not jobs and not self.notice.text():
            self.notice.setText('暂无待审核请求。管理员可私聊“机器人 状态”直接查询；'
                                '暂停、恢复等远程管理请求会显示在这里。')
        current = self.table.currentRow()
        selected_job = self.jobs[current].get('id') if 0 <= current < len(self.jobs) else None
        self.jobs = jobs
        self.table.setRowCount(len(self.jobs))
        labels = {'pending': '等待回执', 'ready': '待审核', 'expired': '已过期', 'done': '已执行',
                  'failed': '未完成', 'rejected': '已拒绝', 'executing': '执行中', 'cancelled': '已取消'}
        names = {'pause': '暂停回复', 'resume': '恢复回复', 'reconnect': '重连QQ', 'status': '状态查询',
                 'connections': '连接查询', 'queue': '队列查询', 'logs': '日志查询', 'usage': '用量',
                 'replies': '回复记录', 'persona': '人设', 'plugin': '插件',
                 'retry_failed': '重试失败', 'help': '帮助'}
        for i, v in enumerate(self.jobs):
            action = str(v.get('action') or '')
            state = str(v.get('state') or '')
            cells = [names.get(action, action), str(v.get('arg') or ''),
                     labels.get(state, state), str(v.get('result') or '')]
            for col, text in enumerate(cells):
                self.table.setItem(i, col, QTableWidgetItem(text))
        selected_row = next((i for i, v in enumerate(self.jobs) if v.get('id') == selected_job), -1)
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        else:
            self.table.setCurrentCell(-1, -1)

    @staticmethod
    def owner_label(candidate,index):
        """候选标签带上完整 QQ 号，用户才认得出哪个是自己（以前只留后 6 位）。

        昵称和号相同时只显示一次：后端已经过滤，这里再兜一次（旧数据/测试可能
        直接把 id 当名字塞进来）。
        """
        identity=str(candidate['id']);name=str(candidate.get('name') or '').strip()
        if name==identity:name=''
        source=str(candidate.get('chat') or '')
        label=f'{name}（{identity}）' if name else f'QQ {identity}'
        return label+(f' · {source}' if source else '')
    def selection_changed(self,*_):
        selected=str(self.owner.currentData() or '')
        same=bool(selected and selected==self._owners)
        self.bind_button.setEnabled(bool(selected) and not same)
        self.bind_button.setText('当前已绑定' if same else '更换为选中管理员' if self._owners else '绑定选中的管理员')
    def open_settings(self):self.configure('pc_control');self.refresh()
    def bind(self):
        owner=self.owner.currentData()
        if not owner:self.notice.setText('请先在 QQ 私聊机器人一句，再刷新选择自己的标识。');return
        r=self.post('pc/bind',{'owner':str(owner)})
        if r.get('success'):
            self.notice.setText('绑定成功，可以发送机器人管理指令。' if r.get('enabled') else '绑定成功，请点击“启用与设置”开启插件。')
        else:self.notice.setText(str(r.get('message') or r.get('detail') or '绑定失败'))
        self.refresh()
    def unbind(self):
        r=self.post('pc/bind',{'owner':''});self.notice.setText('已解除绑定，待审核请求已取消。' if r.get('success') else str(r.get('message') or r.get('detail') or '解除失败'));self.refresh()
    def save_admin_password(self):
        password=self.admin_password.text()
        if not password:
            self.notice.setText('请先输入新的管理员密码（至少 6 位）');return
        r=self.post('pc/admin-password',{'new':password})
        self.admin_password.clear()          # 明文密码不留在界面上
        self.notice.setText(str(r.get('message') or r.get('detail') or ('已保存' if r.get('success') else '保存失败')))
        self.refresh()
    def clear_admin_password(self):
        if not self._password_set:
            return
        if not ask_confirm(self,'清除管理员密码',
                           '清除后，远程变更会回到“本地审核”流程，已进入的管理员会话立即失效。',
                           ok_text='清除',danger=True):
            return
        r=self.post('pc/admin-password',{'new':''})
        self.notice.setText(str(r.get('message') or r.get('detail') or ('已清除' if r.get('success') else '清除失败')))
        self.refresh()
    def review(self,approve):
        index=self.table.currentRow()
        if self.worker or not 0<=index<len(self.jobs):return
        job=self.jobs[index]
        if job['state']!='ready':self.notice.setText('仅能审核仍有效的待审核操作');return
        if approve:
            if QMessageBox.question(self,'审核机器人管理',f"执行 {self.table.item(index,0).text()}？")!=QMessageBox.StandardButton.Yes:return
        self.approve.setEnabled(False);self.reject.setEnabled(False)
        self.worker=ReviewWorker(self.post,{'id':job['id'],'approve':approve});self.worker.completed.connect(self.completed);self.worker.finished.connect(self.finished);self.worker.start()
    def local_action(self,action):
        if self.worker:return
        self.approve.setEnabled(False);self.reject.setEnabled(False)
        self.worker=ReviewWorker(self.post,{'action':action},'pc/action');self.worker.completed.connect(self.completed);self.worker.finished.connect(self.finished);self.worker.start()
    def completed(self,r):self.notice.setText(str(r.get('message') or r.get('detail') or '已处理'));self.refresh()
    def finished(self):self.worker.deleteLater();self.worker=None;self.approve.setEnabled(True);self.reject.setEnabled(True)
