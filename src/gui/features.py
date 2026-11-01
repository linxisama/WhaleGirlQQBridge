"""原生功能管理：会话人设、官方指令对接和群聊诊断。"""
import json
import time
from PyQt6.QtWidgets import (QApplication,QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QTabWidget,QWidget,QTableWidget,QTableWidgetItem,QComboBox,QLineEdit,QHeaderView,QTextEdit)
from PyQt6.QtCore import Qt


class FeaturesDialog(QDialog):
    def __init__(self,get,post,configure,parent=None):
        super().__init__(parent);self.get=get;self.post=post;self.configure=configure;self.snapshot={}
        self.setWindowTitle('功能与会话人设');self.resize(960,670);self.setMinimumSize(650,450)
        self.setStyleSheet('QDialog,QWidget {background:#f4f7fb;color:#26364b;} QTableWidget,QLineEdit,QComboBox,QTextEdit {background:white;color:#26364b;} QPushButton {padding:8px;}')
        layout=QVBoxLayout(self);tabs=QTabWidget();layout.addWidget(tabs,1)
        page=QWidget();p=QVBoxLayout(page);tabs.addTab(page,'按群 / 用户分配人设')
        text=QLabel('群聊：填群ID，用户留空=全群默认；填写用户ID=仅这个人在本群使用。私聊：填对方ID。\n优先级：群内个人 > 群默认 > 人设关键词 > 当前人设。保存后下一条消息生效，各人设历史与记忆独立。官方ID是openid，请从最近会话选取。')
        text.setWordWrap(True);p.addWidget(text)
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['场景','群 / 私聊对象ID','仅指定用户（可留空）','使用人设'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);p.addWidget(self.table,1)
        self.notice=QLabel();self.notice.setWordWrap(True);self.notice.setTextFormat(Qt.TextFormat.PlainText);p.addWidget(self.notice)
        row=QHBoxLayout();p.addLayout(row)
        for name,callback in [('添加绑定',self.add),('删除选中绑定',self.remove),('刷新',self.reload),('保存绑定',self.save)]:
            b=QPushButton(name);b.clicked.connect(callback);row.addWidget(b)
        commands=QWidget();c=QVBoxLayout(commands);tabs.addTab(commands,'QQ菜单与官方指令')
        c.addWidget(QLabel('QQ 中发送 /菜单 可查看功能。图文按钮需官方开通权限；普通菜单直接可用。'))
        self.catalog=QTableWidget(0,2);self.catalog.setHorizontalHeaderLabels(['官方管理端填写的指令','功能说明'])
        self.catalog.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);c.addWidget(self.catalog,1)
        self.catalog_note=QLabel();self.catalog_note.setWordWrap(True);c.addWidget(self.catalog_note)
        row=QHBoxLayout();c.addLayout(row)
        for name,callback in [('复制选中指令',self.copy_command),('复制全部对接配置',self.copy_catalog),('菜单封面与按钮设置',lambda:self.configure('quick_menu'))]:
            b=QPushButton(name);b.clicked.connect(callback);row.addWidget(b)
        diag=QWidget();d=QVBoxLayout(diag);tabs.addTab(diag,'群聊诊断与语音 / Token')
        self.diagnostic=QTextEdit();self.diagnostic.setReadOnly(True);d.addWidget(self.diagnostic,1)
        row=QHBoxLayout();d.addLayout(row)
        for name,callback in [('刷新群聊诊断',self.diagnose),('语音转写设置',lambda:self.configure('speech_to_text_qq')),('节省Token设置',lambda:self.configure('token_saver')),('应用更省Token档',self.economy)]:
            b=QPushButton(name);b.clicked.connect(callback);row.addWidget(b)
        self.reload();self.load_commands()
    def error(self,exc):self.notice.setText('操作未完成：'+str(exc)[:300])
    def reload(self):
        try:
            self.snapshot=self.get('persona-bindings');self.table.setRowCount(0)
            for row in self.snapshot.get('rows',[]):self.add_row(row)
            self.notice.setText('当前接入：'+str(self.snapshot.get('mode',''))+'。修改后点击“保存绑定”。刷新会丢弃尚未保存的表格修改。')
        except Exception as exc:self.error(exc)
    def add_row(self,value):
        index=self.table.rowCount();self.table.insertRow(index)
        kind=QComboBox();kind.addItem('群聊','group');kind.addItem('私聊','private');kind.setCurrentIndex(1 if value.get('kind')=='private' else 0)
        target=QComboBox();target.setEditable(True)
        for row in self.snapshot.get('recent',[]):
            if target.findText(row['target'])<0:target.addItem(row['target'])
        target.setEditText(value.get('target',''))
        user=QLineEdit(value.get('user',''));user.setPlaceholderText('全群默认请留空')
        persona=QComboBox()
        for item in self.snapshot.get('personas',[]):persona.addItem(item['display_name']+' ['+item['name']+']',item['name'])
        chosen=persona.findData(value.get('persona'))
        if chosen<0 and value.get('persona'):
            persona.addItem('人设不存在 ['+str(value['persona'])+']',value['persona']);chosen=persona.count()-1
        persona.setCurrentIndex(max(0,chosen))
        for col,widget in enumerate((kind,target,user,persona)):self.table.setCellWidget(index,col,widget)
    def add(self):self.add_row({})
    def remove(self):
        index=self.table.currentRow()
        if index>=0:self.table.removeRow(index)
    def save(self):
        try:
            rows=[]
            for index in range(self.table.rowCount()):
                widgets=[self.table.cellWidget(index,c) for c in range(4)]
                rows.append(dict(kind=widgets[0].currentData(),target=widgets[1].currentText().strip(),user=widgets[2].text().strip(),persona=widgets[3].currentData()))
            result=self.post('persona-bindings',{'rows':rows,'revision':self.snapshot.get('revision')})
            self.reload();self.notice.setText(result.get('message','绑定已保存'))
        except Exception as exc:self.error(exc)
    def load_commands(self):
        try:
            self.command_data=self.get('command-catalog');items=self.command_data.get('commands',[]);self.catalog.setRowCount(len(items))
            for n,item in enumerate(items):
                self.catalog.setItem(n,0,QTableWidgetItem(item['command'].split()[0]));self.catalog.setItem(n,1,QTableWidgetItem(item['description']))
            self.catalog_note.setText(self.command_data.get('note',''))
        except Exception as exc:self.catalog_note.setText(str(exc)[:300])
    def copy_command(self):
        item=self.catalog.item(self.catalog.currentRow(),0)
        if item:QApplication.clipboard().setText(item.text())
    def copy_catalog(self):
        rows=[dict(command=self.catalog.item(n,0).text(),description=self.catalog.item(n,1).text(),trigger='发送指令') for n in range(self.catalog.rowCount())]
        QApplication.clipboard().setText(json.dumps(rows,ensure_ascii=False,indent=2))
        self.catalog_note.setText('对接配置已复制。在QQ官方管理端逐项添加；好友和群聊场景分别配置。')
    def diagnose(self):
        try:
            data=self.get('group-diagnostics');lines=['接入：'+str(data.get('mode')),'服务运行：'+str(data.get('running'))+'；回复任务存活：'+str(data.get('worker_alive'))+'；暂停：'+str(data.get('paused')),data.get('hint','')]
            last=data.get('last_group_delivery')
            if last:lines+=['最近群回复：'+str(last['state'])+' '+str(last.get('error',''))]
            if not data.get('groups'):lines+=['尚未收到群消息事件。请在QQ中真正 @机器人后发 /菜单，再刷新。']
            for row in data.get('groups',[]):lines.append(time.strftime('%m-%d %H:%M:%S',time.localtime(row['at']))+' '+row['outcome']+' '+row['reason'])
            self.diagnostic.setPlainText('\n\n'.join(lines))
        except Exception as exc:self.diagnostic.setPlainText('诊断未完成：'+str(exc)[:300])
    def economy(self):
        try:
            result=self.post('plugins/token_saver',dict(enabled=True,keepRecentMessages=6,maxDigestChars=200,maxContentChars=600,maxPromptChars=3000,historyMediaChars=240))
            self.diagnostic.setPlainText(result.get('message','已保存')+'\n仅减少历史与旧媒体描述的字数；不改人设正文、模型、当前问题或磁盘对话记录。实际token依模型计费统计。')
        except Exception as exc:self.diagnostic.setPlainText(str(exc)[:300])
