"""Native navigation inspired by Mio's MIT-licensed desktop layout and palette.

Original page widgets remain mounted: navigation never rebuilds a form or reloads
its drafts. See third_party/mio for the pinned reference and license.
"""
from PyQt6.QtCore import QByteArray, QSize, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

BG = '#FAF7F2'
CARD = '#FDFBF7'
BORDER = '#E0D8CD'
TEXT = '#2E2A26'
MUTED = '#6F665D'
ACCENT = '#4A6B74'
ACCENT_HOVER = '#3A565E'
SOFT = '#E3EBEC'
# 以下四项此前是 _apply_style 里的颜色字面量，集中成常量：改主题只动这一处。
SIDEBAR_BG = 'rgba(250,247,242,235)'
FIELD_BG = CARD
FEEDBACK_BG = '#F4EFE8'
NAV_HOVER = '#F0EBE4'
DIRTY = '#996B34'

# Simple original vector marks; no Mio character art or external icon package.
MARKS = {
    'home': '<path d="m3 10 9-7 9 7v10H3z"/><path d="M9 20v-7h6v7"/>',
    'memory': '<path d="M5 3h11a3 3 0 0 1 3 3v15H7a3 3 0 0 1-3-3V4z"/><path d="M7 17h12M8 7h7M8 11h5"/>',
    'heart': '<path d="M12 20 4 12C-2 5 7 0 12 7c5-7 14-2 8 5z"/>',
    'plug': '<path d="M8 3v5m8-5v5M6 8h12v3a6 6 0 0 1-6 6v4M6 11h12"/>',
    'shield': '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
    'link': '<path d="m10 7 2-2a5 5 0 0 1 7 7l-2 2M14 17l-2 2a5 5 0 0 1-7-7l2-2M8 16l8-8"/>',
    'sliders': '<path d="M3 6h18M3 12h18M3 18h18M8 3v6m8 0v6M8 15v6"/>',
    'palette': '<path d="M12 3a9 9 0 1 0 0 18h2a2 2 0 0 0 1-4c-1-1 0-3 2-3h2c4 0 2-11-7-11z"/><path d="M8 7h.1M5 12h.1M8 17h.1M14 7h.1"/>',
    'info': '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    'menu': '<path d="M4 6h16M4 12h16M4 18h16"/>',
    'save': '<path d="M5 3h12l4 4v14H3V3zM7 3v6h10V3M7 21v-7h10v7"/>',
    'plus': '<path d="M12 4v16M4 12h16"/>',
    'refresh': '<path d="M20 7V3m0 4h-4M4 17v4m0-4h4M20 7A9 9 0 0 0 4 8m0 9a9 9 0 0 0 16-1"/>',
    'trash': '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
    'play': '<path d="m7 4 14 8-14 8z"/>',
    'stop': '<path d="M5 5h14v14H5z"/>',
}


def line_icon(name, color=MUTED):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
           f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" '
           'stroke-linecap="round" stroke-linejoin="round">' + MARKS[name] + '</svg>')
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


class MioShell(QWidget):
    """Sidebar adapter for the existing eight native pages and their signals."""
    ROUTES = {
        'home': (0, '首页', '每一次对话，都在延续同一个角色的陪伴。', 'home'),
        'memory': (4, '对话与记忆', '回看消息、管理记忆，了解每一次回应。', 'memory'),
        'persona': (2, '人设与表达', '身份、性格、称呼与说话方式。', 'heart'),
        'plugins': (3, '能力与插件', '按需开启能力，让每一项功能都有清楚的边界。', 'plug'),
        'audio': (7, '音频与语音', '试听、排队、取消与预算，数据去向一目了然。', 'play'),
        'agent': (8, '本地电脑', '本机配对、动作审批、审计摘要与急停。', 'shield'),
        'manage': (5, '机器人管理', '管理会话、权限与电脑操作请求。', 'shield'),
        'qq': (1, 'QQ 连接', '填写 Bridge 端口，连接已经登录的 QQ 账号。', 'link'),
        'models': (1, '模型与 API', '使用你自己的模型与服务，保持原有回复习惯。', 'sliders'),
        'appearance': (6, '外观主题', '背景、透明度和窗口显示，按你的习惯调整。', 'palette'),
        'about': (9, '关于与许可', '版本信息、作者与开源项目致谢。', 'info'),
    }

    def __init__(self, tabs, settings, icon, version):
        super().__init__()
        self.tabs, self.settings = tabs, settings
        self.route = 'home'
        self._manual_compact = None
        self._compact = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName('mioSidebar')
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(12, 22, 12, 14)
        side.setSpacing(8)
        brand = QHBoxLayout()
        avatar = QLabel()
        avatar.setPixmap(icon.pixmap(34, 34))
        avatar.setFixedSize(36, 36)
        brand.addWidget(avatar)
        self.brand_text = QLabel('鲸鱼娘\n私人空间')
        self.brand_text.setObjectName('mioBrand')
        brand.addWidget(self.brand_text, 1)
        side.addLayout(brand)
        side.addSpacing(20)
        self.search = QLineEdit()
        self.search.setObjectName('mioNavSearch')
        self.search.setPlaceholderText('搜索页面')
        self.search.setAccessibleName('搜索页面')
        self.search.setClearButtonEnabled(True)
        side.addWidget(self.search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        navigation = QWidget()
        nav = QVBoxLayout(navigation)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(5)
        self.buttons, self.group_labels, self.group_routes = {}, [], []
        self.button_group = QButtonGroup(self)
        self.button_group.setExclusive(True)
        for label, routes in (
            ('工作空间', ('home', 'memory')),
            ('角色与能力', ('persona', 'plugins', 'audio', 'agent', 'manage')),
            ('设置', ('qq', 'models', 'appearance')),
        ):
            group = QLabel(label)
            group.setObjectName('mioNavGroup')
            self.group_labels.append(group)
            self.group_routes.append((group, routes))
            nav.addWidget(group)
            for key in routes:
                nav.addWidget(self._button(key))
        self.no_results = QLabel('没有匹配的页面，请清空搜索重试。')
        self.no_results.setWordWrap(True)
        self.no_results.setObjectName('mioNavGroup')
        self.no_results.hide()
        nav.addWidget(self.no_results)
        nav.addStretch(1)
        scroll.setWidget(navigation)
        side.addWidget(scroll, 1)
        side.addWidget(self._button('about'))
        self.version_label = QLabel('QQ 账号版 · ' + version)
        self.version_label.setObjectName('mioVersion')
        side.addWidget(self.version_label)
        root.addWidget(self.sidebar)

        self.content = QWidget()
        self.content.setMinimumWidth(0)
        self.content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        header = QFrame()
        header.setObjectName('mioPageHeader')
        h = QHBoxLayout(header)
        h.setContentsMargins(22, 18, 24, 16)
        h.setSpacing(16)
        menu = QPushButton()
        menu.setObjectName('mioMenu')
        menu.setIcon(line_icon('menu'))
        menu.setFixedSize(30, 30)
        menu.setToolTip('展开或收起导航')
        menu.setAccessibleName('展开或收起导航')
        menu.clicked.connect(self.toggle_sidebar)
        h.addWidget(menu, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(5)
        self.heading = QLabel()
        self.heading.setObjectName('mioPageTitle')
        self.description = QLabel()
        self.description.setObjectName('mioPageDescription')
        self.description.setWordWrap(True)
        titles.addWidget(self.heading)
        titles.addWidget(self.description)
        h.addLayout(titles, 1)
        # Reparent the actual save control: its existing busy state/connections
        # remain authoritative, and there is only one clickable save operation.
        settings.btn_save.setText('保存设置')
        actions = QVBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(settings.btn_save, 0, Qt.AlignmentFlag.AlignRight)
        self.dirty_label = QLabel()
        self.dirty_label.setObjectName('mioDirtyState')
        actions.addWidget(self.dirty_label, 0, Qt.AlignmentFlag.AlignRight)
        h.addLayout(actions)
        content.addWidget(header)
        self.feedback = QFrame()
        self.feedback.setObjectName('mioFeedback')
        feedback_layout = QHBoxLayout(self.feedback)
        feedback_layout.setContentsMargins(24, 10, 24, 10)
        settings.save_lbl.setWordWrap(True)
        settings.save_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        feedback_layout.addWidget(settings.save_lbl)
        content.addWidget(self.feedback)
        tabs.tabBar().hide()
        tabs.setObjectName('mioPages')
        content.addWidget(tabs, 1)
        root.addWidget(self.content, 1)
        tabs.currentChanged.connect(self._sync_page)
        settings.state_changed.connect(self._sync_settings_state)
        self.search.textChanged.connect(self._filter_navigation)
        self._apply_style()
        self._sync_page(tabs.currentIndex())

    def _button(self, key):
        _, title, _, mark = self.ROUTES[key]
        button = QPushButton(title)
        button.setObjectName('mioNav_' + key)
        button.setProperty('mioNav', True)
        button.setCheckable(True)
        button.setIcon(line_icon(mark))
        button.setIconSize(QSize(18, 18))
        button.setToolTip(title)
        button.setAccessibleName(title)
        button.setMinimumHeight(39)
        button.clicked.connect(lambda checked=False, route=key: self.navigate(route))
        self.button_group.addButton(button)
        self.buttons[key] = button
        return button

    def navigate(self, key):
        if key not in self.ROUTES:
            return
        if key in ('qq', 'models'):
            self.settings.show_section(key)
        self.tabs.setCurrentIndex(self.ROUTES[key][0])
        self._sync_page(self.tabs.currentIndex())

    def _sync_page(self, index):
        if index == 1:
            key = self.settings.section
        else:
            key = next((key for key, value in self.ROUTES.items() if value[0] == index), 'home')
        self.route = key
        _, title, description, _ = self.ROUTES[key]
        self.buttons[key].setChecked(True)
        self.heading.setText(title)
        self.description.setText(description)
        self.settings.btn_save.setVisible(index == 1)
        self.dirty_label.setVisible(index == 1)
        self._sync_settings_state()

    def _sync_settings_state(self):
        dirty = self.settings.dirty_sections()
        self.dirty_label.setText('有未保存修改' if dirty else '无未保存修改')
        self.dirty_label.setStyleSheet(f'color:{DIRTY};font-size:11px;' if dirty
                                       else f'color:{MUTED};font-size:11px;')
        for key, button in self.buttons.items():
            title = self.ROUTES[key][1]
            button.setText('' if self._compact else title + (' *' if key in dirty else ''))
            button.setToolTip(title + ('（有未保存修改）' if key in dirty else ''))
        self.feedback.setVisible(self.tabs.currentIndex() == 1 and bool(self.settings.save_lbl.text()))

    def _filter_navigation(self, *_):
        query = '' if self._compact else self.search.text().strip().casefold()
        found = False
        for label, routes in self.group_routes:
            matched = []
            for key in routes:
                _, title, description, _ = self.ROUTES[key]
                visible = not query or query in (key + ' ' + title + ' ' + description).casefold()
                self.buttons[key].setVisible(visible)
                matched.append(visible)
            label.setVisible(not self._compact and any(matched))
            found = found or any(matched)
        self.no_results.setVisible(bool(query) and not found)

    def toggle_sidebar(self):
        self._manual_compact = not bool(self._compact)
        self._adapt()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adapt()

    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, width):
        return -1

    def _adapt(self):
        compact = self.width() < 1000 if self._manual_compact is None else self._manual_compact
        if compact == self._compact:
            return
        self._compact = compact
        self.sidebar.setFixedWidth(72 if compact else 206)
        self.brand_text.setVisible(not compact)
        self.version_label.setVisible(not compact)
        self.search.setVisible(not compact)
        self._filter_navigation()
        self._sync_settings_state()
        self.sidebar.setProperty('compact', compact)
        self.sidebar.style().unpolish(self.sidebar)
        self.sidebar.style().polish(self.sidebar)

    def _apply_style(self):
        self.setStyleSheet(f'''
            QFrame#mioSidebar {{ background:{SIDEBAR_BG}; border:0;
                border-right:1px solid {BORDER}; }}
            QLabel#mioBrand {{ font-size:14px; font-weight:600; color:{TEXT}; }}
            QLabel#mioNavGroup {{ font-size:11px; color:{MUTED}; padding:16px 9px 5px; }}
            QLabel#mioVersion {{ font-size:10px; color:{MUTED}; padding:8px 8px 0; }}
            QLineEdit#mioNavSearch {{ background:{FIELD_BG}; color:{TEXT}; border:1px solid {BORDER};
                border-radius:6px; padding:7px; font-size:12px; }}
            QFrame#mioFeedback {{ background:{FEEDBACK_BG}; border:0; border-bottom:1px solid {BORDER}; }}
            QPushButton[mioNav="true"] {{ background:transparent; color:{TEXT}; border:0;
                border-radius:7px; padding:9px 11px; text-align:left; font-weight:normal; }}
            QPushButton[mioNav="true"]:hover {{ background:{NAV_HOVER}; }}
            QPushButton[mioNav="true"]:checked {{ background:{SOFT}; color:{ACCENT}; font-weight:600; }}
            QFrame#mioSidebar[compact="true"] QPushButton[mioNav="true"] {{ padding:9px 12px; }}
            QFrame#mioPageHeader {{ border:0; border-bottom:1px solid {BORDER}; }}
            QLabel#mioPageTitle {{ font-size:23px; font-weight:600; color:{TEXT}; }}
            QLabel#mioPageDescription {{ font-size:12px; color:{MUTED}; }}
            QPushButton#mioMenu {{ background:transparent; border:0; border-radius:5px; padding:5px; }}
            QPushButton#mioMenu:hover {{ background:{SOFT}; }}
            QTabWidget#mioPages::pane {{ border:0; background:transparent; }}
            QScrollArea {{ border:0; background:transparent; }}
        ''')
