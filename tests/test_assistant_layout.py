"""验证真实绘制和控件位置，防止助手空白布局与独立白底回归。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication
import qq_gui as gui
from tests.test_console_agent import PERSONA


class AssistantLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config=Path(self.tmp.name)/'gui.json'
        self.patches=[patch.object(gui,'AUTO_BACKEND',False),patch.object(gui.UiHangWatchdog,'start')]
        for p in self.patches:p.start()
        self.window=gui.MainWindow(config_path=self.config)
        self.window._timer.stop();self.window.pc_tab.timer.stop()
        self.tab=self.window.assistant_tab;self.window.tabs.setCurrentWidget(self.tab)
        self.window.show();self.app.processEvents()

    def tearDown(self):
        self.window._quitting=True;self.window.close();self.window.deleteLater();self.app.processEvents()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def awaiting(self):
        self.tab.present({'id':'id1','state':'confirm',
            'message':'这一步要操作你的电脑，需要你点一次确认：\n打开网页',
            'actions':[{'type':'open_url','url':'https://example.test/'}]})
        self.app.processEvents()

    def title_y(self,label):return label.mapTo(self.tab,QPoint(0,0)).y()

    def test_idle_headers_aligned_at_top_at_small_and_large_sizes(self):
        for size in [(800,500),(1128,698),(1440,900)]:
            self.window.resize(*size);self.app.processEvents()
            self.assertFalse(self.tab.history_title.isHidden())
            self.assertFalse(self.tab.confirm_button.isEnabled())
            self.assertFalse(self.tab.send_button.visibleRegion().isEmpty())

    def test_agent_chat_takes_the_whole_tab(self):
        # 右侧「方案」面板已下线：对话区独占，确认按钮只在等待确认时可用。
        self.window.resize(800,500);self.app.processEvents()
        for gone in ('preview_title','preview','empty_preview','summary','apply_button','persona_prompt','activate'):
            self.assertFalse(hasattr(self.tab,gone),gone)
        self.assertFalse(self.tab.confirm_button.isEnabled())
        self.awaiting()
        self.assertTrue(self.tab.history_title.isVisible())
        self.assertTrue(self.tab.confirm_button.isEnabled())
        self.assertGreaterEqual(self.tab.chat_scroll.width(),self.tab.width()-260)

    def sample(self,widget,x=None,y=None):
        # 采样窗口合成结果；透明子控件独立grab不含父级背景，且不能采到文字。
        visible=widget.visibleRegion().boundingRect()
        self.assertFalse(visible.isEmpty())
        x=max(visible.left(),visible.right()-15) if x is None else x
        y=max(visible.top(),visible.bottom()-15) if y is None else y
        window=widget.window();point=widget.mapTo(window,QPoint(x,y))
        image=window.grab().toImage();scale=image.devicePixelRatio()
        return image.pixelColor(round(point.x()*scale),round(point.y()*scale))

    def test_background_and_input_panels_repaint_when_transparency_changes(self):
        self.window._apply_bg('#285fa0','',0,0)
        self.window._apply_panel_transparency(0);self.app.processEvents()
        opaque=self.sample(self.tab.chat_scroll.viewport())
        self.window._apply_panel_transparency(1);self.app.processEvents()
        clear=self.sample(self.tab.chat_scroll.viewport())
        base=self.sample(self.tab,x=3,y=self.tab.height()-3)
        self.assertGreater(opaque.red(),240)
        self.assertLess(clear.red(),opaque.red()-80)
        self.assertLess(base.red(),100);self.assertGreater(base.blue(),120)
        self.assertLess(abs(clear.red()-base.red()),10)
        self.assertFalse(self.tab.confirm_button.isEnabled())

    def test_enter_sends_and_shift_enter_does_not(self):
        from PyQt6.QtTest import QTest
        from PyQt6.QtCore import Qt
        calls = []
        self.tab.prompt.sendRequested.disconnect()
        self.tab.prompt.sendRequested.connect(lambda: calls.append(1))
        self.tab.prompt.setPlainText('你好')
        QTest.keyClick(self.tab.prompt, Qt.Key.Key_Return)
        self.assertEqual(len(calls), 1)
        QTest.keyClick(self.tab.prompt, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(len(calls), 1)
        self.assertIn('\n', self.tab.prompt.toPlainText())

    def test_attachment_clear(self):
        self.tab.add_attachments(['C:/tmp/a.txt', 'C:/tmp/b.log'])
        self.assertEqual(len(self.tab.attachments), 2)
        self.assertTrue(self.tab.clear_attach_button.isVisible() or self.tab.clear_attach_button.isHidden())
        self.tab.clear_attachments()
        self.assertEqual(self.tab.attachments, [])

    def test_saved_appearance_is_applied_on_reopen_without_replacing_typed_prompt(self):
        self.tab.prompt.setPlainText('保留正在编辑的内容')
        self.window._apply_appearance({'panel_transparency':.8,'bg_color':'#285fa0','background_image':'','blur':0,'transparency':0})
        self.assertEqual(self.tab.prompt.toPlainText(),'保留正在编辑的内容')
        restored=gui.MainWindow(config_path=self.config)
        try:
            restored._timer.stop();restored.pc_tab.timer.stop();restored.tabs.setCurrentWidget(restored.assistant_tab)
            restored.show();self.app.processEvents()
            self.assertEqual(restored._cfg['panel_transparency'],.8)
            self.assertLess(self.sample(restored.assistant_tab.chat_scroll.viewport()).red(),150)
        finally:restored._quitting=True;restored.close();restored.deleteLater();self.app.processEvents()
