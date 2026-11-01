"""Row editor for keyword rules; preserves order and validates before saving."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,QTextEdit,QTableWidget,QHeaderView,QAbstractItemView
from ..keyword_rules import read_rule_rows,write_rule_rows


class KeywordRulesEditor(QWidget):
    def __init__(self,value='',parent=None):
        super().__init__(parent)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        toolbar=QHBoxLayout()
        self.add_button=QPushButton('＋ 新增规则')
        self.example_button=QPushButton('填入示例')
        for button in (self.add_button,self.example_button):
            button.setStyleSheet('background:#2FA8A0;color:white;border:none;border-radius:7px;padding:8px 14px;font-weight:bold;')
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.hint=QLabel('消息包含关键词就会触发；多条命中时优先回复上面的规则。回复可换行。')
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet('color:#5F6B7A;font-size:12px;')
        layout.addWidget(self.hint)
        self.table=QTableWidget(0,3)
        self.table.setHorizontalHeaderLabels(['关键词','回复内容','操作'])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(280)
        self.table.setStyleSheet('QTableWidget {background:white;border:1px solid #D3DAE3;border-radius:6px;gridline-color:#E8EDF3;} QHeaderView::section {background:#E8EDF3;color:#1F2733;border:none;padding:8px;font-weight:bold;}')
        header=self.table.horizontalHeader()
        header.setSectionResizeMode(0,QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2,QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0,150)
        self.table.setColumnWidth(2,118)
        layout.addWidget(self.table,1)
        self.add_button.clicked.connect(lambda:self.add_row())
        self.example_button.clicked.connect(self.add_examples)
        self.set_rules(value)

    def rows(self):
        return [(self.table.cellWidget(row,0).text(),self.table.cellWidget(row,1).toPlainText())
                for row in range(self.table.rowCount())]

    def set_rules(self,value):
        try:
            rows=read_rule_rows(value)
        except ValueError as exc:
            rows=[(str(value),'')]
            self.hint.setText(str(exc)+'；原始内容已保留在关键词栏，修正或删除后再保存。')
        self.set_rows(rows)

    def set_rows(self,rows):
        self.table.setRowCount(0)
        for keyword,reply in rows or [('', '')]:
            self.add_row(keyword,reply,focus=False)

    def add_row(self,keyword='',reply='',focus=True):
        row=self.table.rowCount()
        self.table.insertRow(row)
        key=QLineEdit(keyword)
        key.setMaximumHeight(42)
        key.setPlaceholderText('例如：你好')
        key.setStyleSheet('background:white;color:#1F2733;border:1px solid #D3DAE3;border-radius:5px;padding:7px;')
        response=QTextEdit()
        response.setAcceptRichText(False)
        response.setPlainText(reply)
        response.setPlaceholderText('例如：你好呀～今天过得怎么样？')
        response.setStyleSheet('background:white;color:#1F2733;border:1px solid #D3DAE3;border-radius:5px;padding:7px;')
        self.table.setCellWidget(row,0,key)
        self.table.setCellWidget(row,1,response)
        actions=QWidget()
        buttons=QHBoxLayout(actions)
        buttons.setContentsMargins(3,3,3,3)
        buttons.setSpacing(3)
        for label,tip,handler in [('↑','提高优先级',lambda checked=False,r=row:self.move_row(r,-1)),
                                  ('↓','降低优先级',lambda checked=False,r=row:self.move_row(r,1)),
                                  ('删除','删除这条规则',lambda checked=False,r=row:self.remove_row(r))]:
            button=QPushButton(label)
            button.setToolTip(tip)
            button.setStyleSheet('background:#E8EDF3;color:#1F2733;border:none;border-radius:5px;padding:6px 4px;')
            button.clicked.connect(handler)
            buttons.addWidget(button)
        self.table.setCellWidget(row,2,actions)
        self.table.setRowHeight(row,100)
        if focus:
            self.table.scrollToBottom()
            key.setFocus()

    def remove_row(self,row):
        rows=self.rows()
        rows.pop(row)
        self.set_rows(rows)

    def move_row(self,row,direction):
        target=row+direction
        rows=self.rows()
        if not 0<=target<len(rows):
            return
        rows[row],rows[target]=rows[target],rows[row]
        self.set_rows(rows)
        self.table.cellWidget(target,0).setFocus()

    def add_examples(self):
        rows=[row for row in self.rows() if row[0].strip() or row[1].strip()]
        seen={key.strip().lower() for key,_ in rows}
        rows.extend((key,reply) for key,reply in [('你好','你好呀～今天过得怎么样？'),('晚安','晚安，早点休息～')]
                    if key.lower() not in seen)
        self.set_rows(rows)

    def value(self):
        return write_rule_rows(self.rows())
