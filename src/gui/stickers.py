"""Native local sticker library: preview, upload, edit, recognize and delete."""
import base64
from pathlib import Path
from urllib.parse import quote
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,QTextEdit,
    QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QFileDialog,QMessageBox,QFormLayout)
from ..plugin_files import import_plugin_files
from . import file_dialogs
from .diagnostics import safe_slot
from .dialogs import ask_choice, ask_confirm

class BatchRecognizeWorker(QThread):
    """后台逐张识别：不阻塞界面，可中途停止。

    识别一张就是一次视觉模型调用 —— 按次计费的中转站每张扣一次钱，
    所以这里把"成功/失败"都数清楚，结束时如实汇报。
    """

    progress = pyqtSignal(int, int, str)      # 已完成, 总数, 当前标签
    completed = pyqtSignal(int, int, bool)    # 成功, 失败, 是否被停止

    def __init__(self, post, entries):
        super().__init__()
        self._post = post
        self._entries = list(entries or [])
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self._entries)
        ok = failed = 0
        for index, entry in enumerate(self._entries, 1):
            if self._stop or self.isInterruptionRequested():
                self.completed.emit(ok, failed, True)
                return
            sticker_id = str((entry or {}).get('id') or '')
            label = str((entry or {}).get('label') or sticker_id[:8])
            self.progress.emit(index - 1, total, label)
            if not sticker_id:
                failed += 1
                continue
            try:
                result = self._post('stickers/' + sticker_id + '/recognize', {}, timeout=90)
                if isinstance(result, dict) and result.get('success'):
                    ok += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        self.progress.emit(total, total, '')
        self.completed.emit(ok, failed, False)


class StickerLibraryDialog(QDialog):
    def __init__(self,root,get,post,delete,parent=None):
        super().__init__(parent)
        self.root=Path(root);self.get=get;self.post=post;self.delete=delete
        self.page=1;self.pages=1;self._items=[]
        self.setWindowTitle('表情包管理 · 机器人本地图库');self.resize(930,740)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        self.setStyleSheet('''QDialog {background:#f4f6fa;color:#26364b;}
            QLabel {color:#34506d;}
            QTableWidget {background:#ffffff;color:#26364b;border:1px solid #d1dce8;gridline-color:#e2e8f0;}
            QHeaderView::section {background:#e8eef5;color:#34506d;padding:8px;border:0;}
            QLineEdit,QTextEdit {background:#ffffff;color:#26364b;border:1px solid #cbd8e8;border-radius:6px;padding:8px;}
            QPushButton {background:#2da6a0;color:white;border:0;border-radius:6px;padding:9px 13px;}
            QPushButton:hover {background:#218e88;}
            QPushButton:disabled {background:#e1e8f0;color:#68778c;}
            QTableWidget::item:selected {background:#d7eeec;color:#20364a;}
        ''')
        layout=QVBoxLayout(self)
        hint=QLabel('管理自动收集与在这里上传的图片；删除只影响机器人本地库。原有手动图库仍在“表情包与自定义图库”中。')
        hint.setWordWrap(True);layout.addWidget(hint)
        row=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('搜索标签或图片含义，按回车查找')
        self.search.returnPressed.connect(self.search_now);row.addWidget(self.search,1)
        self.upload_button=QPushButton('上传图片…');self.upload_button.clicked.connect(self.upload);row.addWidget(self.upload_button)
        refresh=QPushButton('刷新');refresh.clicked.connect(self.refresh);row.addWidget(refresh)
        # 一键识别全部：库里有几十张时，一张一张点太费事（也容易漏）
        self.batch_button=QPushButton('一键识别全部…');self.batch_button.clicked.connect(self.batch_recognize)
        self.batch_button.setToolTip('把库里所有表情交给视觉模型识别一遍（每张一次调用）')
        row.addWidget(self.batch_button)
        self.stop_button=QPushButton('停止识别');self.stop_button.clicked.connect(self.stop_batch)
        self.stop_button.setVisible(False);row.addWidget(self.stop_button)
        layout.addLayout(row)
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['预览','标签','图片含义','使用次数'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0,110);self.table.setColumnWidth(1,200)
        self.table.itemSelectionChanged.connect(self.select);layout.addWidget(self.table,1)
        pager=QHBoxLayout();previous=QPushButton('上一页');previous.clicked.connect(lambda:self.move(-1));pager.addWidget(previous)
        self.page_label=QLabel();pager.addWidget(self.page_label);next_button=QPushButton('下一页');next_button.clicked.connect(lambda:self.move(1));pager.addWidget(next_button);pager.addStretch();layout.addLayout(pager)
        form=QFormLayout();self.label=QLineEdit();self.label.setPlaceholderText('例如：开心、委屈、疑惑（最多40字）');self.label.setMaxLength(40)
        self.description=QTextEdit();self.description.setPlaceholderText('描述图中动作、表情及适用语境，供模型选图使用');self.description.setMaximumHeight(100)
        form.addRow('标签',self.label);form.addRow('图片含义',self.description);layout.addLayout(form)
        actions=QHBoxLayout()
        self.save_button=QPushButton('保存标签与说明');self.save_button.clicked.connect(self.save);actions.addWidget(self.save_button)
        self.recognize_button=QPushButton('AI识别这张图');self.recognize_button.clicked.connect(self.recognize);actions.addWidget(self.recognize_button)
        self.delete_button=QPushButton('删除选中图片');self.delete_button.clicked.connect(self.remove);actions.addWidget(self.delete_button)
        actions.addStretch();close=QPushButton('关闭');close.clicked.connect(self.accept);actions.addWidget(close);layout.addLayout(actions)
        self.notice=QLabel();self.notice.setWordWrap(True);layout.addWidget(self.notice)
        self.refresh()

    # ── 一键识别全部 ────────────────────────────────────────────────────
    def _all_entries(self, limit=500):
        """把整个图库的条目收全（接口是分页的）。

        只读接口，出错就返回已收集的部分：识别少几张不会被卡住。
        """
        entries = []
        page = 1
        pages = 1
        while page <= pages and len(entries) < limit:
            try:
                result = self.get('stickers?q=&page=' + str(page))
            except Exception:
                break
            if not isinstance(result, dict):
                break
            # 注意：`/api/stickers` 返回 {items,total,page,pages}，**没有 success 字段**。
            # 以前这里要求 result['success'] 为真 → 第一页就被当成失败、返回空列表，
            # 于是表格里有 16 张、点「一键识别全部」却说"图库里还没有表情"（真机 bug）。
            if result.get('success') is False or 'items' not in result:
                break
            items = [item for item in (result.get('items') or []) if isinstance(item, dict)]
            if not items:
                break
            entries.extend(items)
            try:
                pages = max(1, int(result.get('pages') or 1))
            except (TypeError, ValueError):
                pages = 1
            page += 1
        return entries[:limit]

    def batch_recognize(self):
        """一键把库里的表情交给视觉模型识别（可只补空缺，也可全部重来）。"""
        if getattr(self, '_batch_worker', None) is not None:
            self.notice.setText('正在识别中，等这一轮结束或点「停止识别」')
            return
        entries = self._all_entries()
        if not entries:
            self.notice.setText('图库里还没有表情，先上传或等它自动收集')
            return
        missing = [item for item in entries if not str(item.get('description') or '').strip()]
        total = len(entries)
        # 识别一张 = 一次视觉模型调用：按次计费的接口每张都会扣钱，所以先说清楚再动手
        cost = '每张都会调用一次视觉模型（按次计费时约等于张数积分）'
        if missing:
            answer = ask_choice(
                self, '一键识别全部',
                f'图库里共 {total} 张，其中 {len(missing)} 张还没有图片说明。',
                detail=f'「只识别未识别的」= 处理 {len(missing)} 张；'
                       f'「全部重新识别」= 处理 {total} 张（已有的说明会被覆盖）。\n{cost}',
                options=[('只识别未识别的 %d 张' % len(missing), 'missing'),
                         ('全部重新识别 %d 张' % total, 'all'),
                         ('取消', '')])
            if not answer:
                return
            targets = missing if answer == 'missing' else entries
        else:
            if not ask_confirm(self, '一键识别全部',
                               f'图库里 {total} 张都已有说明，要全部重新识别吗？',
                               detail=f'已有的说明会被覆盖。{cost}', ok_text='全部重新识别'):
                return
            targets = entries
        self._start_batch(targets)

    def _start_batch(self, targets):
        worker = BatchRecognizeWorker(self.post, targets)
        self._batch_worker = worker
        self._batch_total = len(targets)
        self.batch_button.setEnabled(False)
        self.stop_button.setVisible(True)
        self.notice.setText(f'开始识别，共 {self._batch_total} 张…')
        worker.progress.connect(self._batch_progress)
        worker.completed.connect(self._batch_finished)
        worker.start()

    def _batch_progress(self, done, total, label):
        if label:
            self.notice.setText(f'正在识别第 {done + 1}/{total} 张：{label}')
        else:
            self.notice.setText(f'识别中 {done}/{total}…')

    def _batch_finished(self, ok, failed, stopped):
        worker = getattr(self, '_batch_worker', None)
        if worker is not None:
            try:
                worker.wait(3000)
            except Exception:
                pass
        self._batch_worker = None
        self.batch_button.setEnabled(True)
        self.stop_button.setVisible(False)
        summary = f'识别结束：成功 {ok} 张' + (f'，失败 {failed} 张（可重试）' if failed else '')
        if stopped:
            summary = f'已停止：完成 {ok + failed} 张（成功 {ok}，失败 {failed}），其余未处理'
        self.notice.setText(summary)
        self.refresh()

    def stop_batch(self):
        worker = getattr(self, '_batch_worker', None)
        if worker is not None:
            worker.stop()
            self.notice.setText('已请求停止，正在收尾…')

    def closeEvent(self, event):
        """关窗时先让后台线程停下，避免线程还在跑而控件已经销毁。"""
        worker = getattr(self, '_batch_worker', None)
        if worker is not None:
            try:
                worker.stop()
                worker.wait(5000)
            except Exception:
                pass
            self._batch_worker = None
        super().closeEvent(event)

    def search_now(self):self.page=1;self.refresh()
    def move(self,amount):self.page=max(1,min(self.pages,self.page+amount));self.refresh()
    def current(self):
        row=self.table.currentRow()
        return self._items[row] if 0<=row<len(self._items) else None
    def select(self):
        entry=self.current()
        self.label.setText(str(entry.get('label') or '') if entry else '')
        self.description.setPlainText(entry.get('description','') if entry else '')
        for button in [self.save_button,self.recognize_button,self.delete_button]:button.setEnabled(bool(entry))
    @safe_slot(label="表情库")
    def refresh(self):
        # 注意：以前这里是"判定不是 dict 之后又对 result 调 .get" —— 判空那一行自己会炸
        # （和"记忆页 time 未导入"同型的自相矛盾 bug）。现在所有字段走安全取值：
        # 缺 label/pages/total 只是显示占位符，不会让整个弹窗打不开。
        result = self.get('stickers?q=' + quote(self.search.text()) + '&page=' + str(self.page))
        if not isinstance(result, dict):
            self.notice.setText('图库读取失败（后台返回了非预期格式）')
            return
        items = [entry for entry in (result.get('items') or []) if isinstance(entry, dict)]
        if 'items' not in result:
            self.notice.setText(str(result.get('message') or '图库读取失败'))
            return
        self._items = items
        try:
            self.pages = max(1, int(result.get('pages') or 1))
        except (TypeError, ValueError):
            self.pages = 1
        self.table.setRowCount(len(self._items))
        for row, entry in enumerate(self._items):
            preview = QLabel()
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = QPixmap()
            try:
                payload = entry.get('thumbnail')
                if payload:
                    pixmap.loadFromData(base64.b64decode(payload))
            except Exception:
                pixmap = QPixmap()          # 缩略图坏了只影响这一格
            preview.setPixmap(pixmap.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
            self.table.setCellWidget(row, 0, preview)
            self.table.setRowHeight(row, 104)
            self.table.setItem(row, 1, QTableWidgetItem(str(entry.get('label') or '')))
            self.table.setItem(row, 2, QTableWidgetItem(str(entry.get('description') or '')))
            self.table.setItem(row, 3, QTableWidgetItem(str(entry.get('uses') or 0)))
        self.page_label.setText(f'{self.page}/{self.pages} 页 · 共 {result.get("total") or 0} 张')
        try:
            self.select()
        except Exception:
            pass
    def show_result(self,result):
        self.notice.setText(result.get('message') or '操作未完成')
        if result.get('success'):self.refresh()
    def upload(self):
        paths,_=file_dialogs.open_files(self,'上传表情图片（可多选）','','图片 (*.png *.jpg *.jpeg *.gif *.webp)')
        if not paths:return
        try:
            items=import_plugin_files(self.root/'data/plugin_uploads','sticker_admin','imports',paths,
                {'file_picker':'files','file_extensions':['.png','.jpg','.jpeg','.gif','.webp'],'file_max_mb':6})
            self.show_result(self.post('stickers/import',{'items':items},timeout=30))
        except (OSError,ValueError) as exc:self.notice.setText(str(exc))
    def save(self):
        entry=self.current()
        if entry:self.show_result(self.post('stickers/'+entry['id'],{'label':self.label.text(),'description':self.description.toPlainText()}))
    def recognize(self):
        entry=self.current()
        if not entry:return
        self.notice.setText('正在识别，完成后会更新图片说明…')
        self.recognize_button.setEnabled(False)
        try:self.show_result(self.post('stickers/'+entry['id']+'/recognize',{},timeout=90))
        finally:self.recognize_button.setEnabled(bool(self.current()))
    def remove(self):
        entry=self.current()
        if entry and ask_confirm(self,'删除本地表情',f'删除“{entry["label"]}”？',
                                 detail='只删除机器人自己的本地收藏。',ok_text='删除',danger=True):
            self.show_result(self.delete('stickers/'+entry['id']))
