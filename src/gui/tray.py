"""原生托盘保留控制台进程；单实例通道只接收显示窗口请求。"""
import hashlib
from PyQt6.QtCore import QObject,Qt,QEventLoop,QTimer
from PyQt6.QtWidgets import QApplication,QMenu,QSystemTrayIcon
from PyQt6.QtNetwork import QLocalServer,QLocalSocket

from src.gui.menu_style import style_menu


class RobotTray(QObject):
    def __init__(self,window,icon,available=None):
        super().__init__(window);self.window=window
        self.enabled=(QSystemTrayIcon.isSystemTrayAvailable() and QApplication.platformName()!='offscreen') if available is None else available
        self.running=False;self.online=False
        self.icon=QSystemTrayIcon(icon,self);self.menu=QMenu(window)
        # 菜单样式统一走 src/gui/menu_style.py（托盘菜单曾在系统深色模式下黑底暗字）
        style_menu(self.menu)
        self.show_action=self.menu.addAction('显示控制台');self.menu.addSeparator()
        self.start_action=self.menu.addAction('开启机器人');self.stop_action=self.menu.addAction('停止机器人');self.menu.addSeparator()
        self.force_exit_action=self.menu.addAction('强制退出控制台（后台继续运行）')
        self.exit_action=self.menu.addAction('退出程序')
        self.show_action.triggered.connect(self.show_window)
        self.start_action.triggered.connect(lambda checked=False:self.action('start'))
        self.stop_action.triggered.connect(lambda checked=False:self.action('stop'))
        self.exit_action.triggered.connect(window._request_exit)
        self.force_exit_action.triggered.connect(getattr(window,'force_exit',window._request_exit))
        self.icon.setContextMenu(self.menu);self.icon.activated.connect(self._activated)
        self.menu.aboutToShow.connect(self.refresh_actions);self.update({})
        if self.enabled:
            self.icon.show();QApplication.instance().setQuitOnLastWindowClosed(False)
    def _activated(self,reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick):self.show_window()
    def show_window(self):
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.show();self.window.raise_();self.window.activateWindow()
    def action(self,path):
        if self.online and not getattr(self.window,'_quitting',False):self.window._action(path)
        self.refresh_actions()
    def update(self,status):
        status=status if isinstance(status,dict) else {}
        self.online=bool(status) and status.get('success') is not False
        self.running=bool(status.get('running')) if self.online else False
        state=('运行中' if self.running else '已停止') if self.online else '后台未连接'
        if status.get('reply_paused'):state+=' · 回复已暂停'
        self.icon.setToolTip('鲸鱼娘 QQ · '+state+'\n单击显示控制台，右键开启、停止或退出')
        self.refresh_actions()
    def refresh_actions(self):
        quitting=bool(getattr(self.window,'_quitting',False));busy=bool(getattr(self.window,'_busy',False) or quitting)
        self.start_action.setEnabled(self.online and not self.running and not busy)
        self.stop_action.setEnabled(self.online and self.running and not busy)
        self.exit_action.setEnabled(not quitting)
    def hide_window(self):
        if not self.enabled or not QSystemTrayIcon.isSystemTrayAvailable():return False
        self.window.hide();return True


class ConsoleInstance(QObject):
    @staticmethod
    def name(root):
        return 'WhaleGirlQQ.Console.'+hashlib.sha256(str(root).lower().encode()).hexdigest()[:20]
    @staticmethod
    def forward(root):
        socket=QLocalSocket();socket.connectToServer(ConsoleInstance.name(root))
        if not socket.waitForConnected(180):return False
        socket.write(b'show');socket.flush()
        # 收到窗口端确认后再退出新进程；过早销毁本地管道会丢掉“显示”请求。
        loop=QEventLoop();timer=QTimer();timer.setSingleShot(True);timer.timeout.connect(loop.quit)
        socket.readyRead.connect(loop.quit);socket.disconnected.connect(loop.quit)
        timer.start(400)
        if not socket.bytesAvailable():loop.exec()
        timer.stop();socket.readAll();socket.abort()
        # 管道已连接就已有控制台；对方暂忙或确认丢失也不能再开第二个。
        return True
    def __init__(self,window,root):
        super().__init__(window);self.window=window;self.server=QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.accept)
        self.listening=self.server.listen(self.name(root))
    def accept(self):
        while self.server.hasPendingConnections():
            socket=self.server.nextPendingConnection()
            socket.readyRead.connect(lambda peer=socket:self.read(peer))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():self.read(socket)
            # 系统繁忙时 readyRead 可能晚到；补一次兜底读取，避免“显示控制台”请求丢失。
            QTimer.singleShot(120, lambda peer=socket: self._drain(peer))
    def _drain(self,socket):
        try:
            if socket.state()==QLocalSocket.LocalSocketState.ConnectedState and socket.bytesAvailable():
                self.read(socket)
        except RuntimeError:
            pass

    def read(self,socket):
        data=getattr(socket,'_console_request',b'')+bytes(socket.read(128))
        socket._console_request=data
        if len(data)<4 and b'show'.startswith(data):return
        if data==b'show' and not getattr(self.window,'_quitting',False):
            self.window._tray.show_window();socket.write(b'ok');socket.flush()
        socket.disconnectFromServer()
