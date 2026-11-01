"""按5.2.3故障现场验证：市场插件缺席、完整回复、桌面弹窗与本机诊断。"""
import asyncio,json,os,shutil,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication,QWidget
from PyQt6.QtNetwork import QNetworkRequest
from src.qq_app import QQRuntime,create_app,safe_error
from src.gui.smoke import check_packaged_reply
from src.gui.assistant import AssistantTab
from src.plugin_catalog import market_plugins


class PackagedReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[1]/'styles',self.root/'styles')
        self.bot=QQRuntime(root=self.root)

    async def asyncTearDown(self):
        await self.bot.close();self.temp.cleanup()

    async def test_all_market_modules_absent_private_group_text_and_asr_reply(self):
        absent={'src.plugins.'+name:None for name in market_plugins()}
        for index in range(2):
            # 第二轮用新数据库，验证重启时同样不依赖市场模块。
            if index:
                await self.bot.close()
                root=self.root/'restart';root.mkdir()
                shutil.copytree(self.root/'styles',root/'styles')
                self.bot=QQRuntime(root=root)
            with patch.dict(sys.modules,absent):
                result=await check_packaged_reply(self.bot)
            self.assertTrue(result['group_at_text']);self.assertTrue(result['private_supplied_asr'])
            self.assertEqual(result['real_messages_sent'],0)

    async def test_missing_menu_plugin_keeps_catalog_route_available(self):
        app=create_app(self.bot)
        endpoint=next(route.endpoint for route in app.routes if route.path=='/api/command-catalog')
        with patch.dict(sys.modules,{'src.plugins.quick_menu':None}):
            data=await endpoint()
        self.assertTrue(data['commands']);self.assertIn('/帮助',str(data))

    async def test_missing_module_name_and_stack_are_local_without_exception_secrets(self):
        try:raise ModuleNotFoundError('private token=do-not-record',name='src.plugins.missing_probe')
        except ModuleNotFoundError as exc:
            self.assertIn('src.plugins.missing_probe',safe_error(exc))
            self.bot._log_module_error(exc)
        log=(self.root/'logs/qq-direct.log').read_text('utf-8')
        self.assertIn('src.plugins.missing_probe',log)
        self.assertIn('test_fault_regressions_524.py',log)
        self.assertNotIn('do-not-record',log)
        # 源码行可能出现测试字面量，实际异常参数不应落盘；用动态值模拟凭据。
        secret='fixture-'+'unprintable-123'
        try:raise ModuleNotFoundError(secret,name='src.absent')
        except ModuleNotFoundError as exc:self.bot._log_module_error(exc)
        self.assertNotIn(secret,(self.root/'logs/qq-direct.log').read_text('utf-8'))


class AssistantDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.parent=QWidget();self.parent.setStyleSheet('QWidget{background:transparent;color:#18314d;}')
        self.tab=AssistantTab('http://127.0.0.1:27499','/fixture',self.parent)
    def tearDown(self):
        self.parent.close();self.parent.deleteLater();self.app.processEvents()
    def test_auxiliary_dialogs_have_opaque_readable_surfaces(self):
        for factory in (self.tab._skills_dialog,self.tab._learning_dialog,self.tab._schedules_dialog,
                        self.tab._history_dialog,self.tab._session_dialog):
            dialog=factory();dialog.show();self.app.processEvents()
            color=dialog.grab().toImage().pixelColor(8,8)
            self.assertGreater(color.red(),180);self.assertGreater(color.green(),180)
            self.assertEqual(dialog.objectName(),'agentAuxDialog');dialog.hide()
    def test_skills_empty_state_and_loaded_list(self):
        self.tab._show_skills({'skills':[]});self.app.processEvents()
        self.assertFalse(self.tab.skills_empty.isHidden())
        self.tab._show_skills({'skills':[{'id':'s','name':'示例技能'}]})
        self.assertTrue(self.tab.skills_empty.isHidden());self.assertEqual(self.tab.skills_list.count(),1)
    def test_failed_backend_reports_actual_account_port_not_zero_records(self):
        class Reply:
            def readAll(self):return b''
            def deleteLater(self):pass
            def attribute(self,key):return 401
        for kind in ('learning','identity'):
            reply=Reply();self.tab.pending=reply;self.tab.request_kind=kind
            self.tab.queued_request=('learning','assistant/learning',None)
            self.tab._response(reply)
            self.assertIn('27499',self.tab.notice.text());self.assertIn('401',self.tab.notice.text())
            self.assertIn('不等于没有记录',self.tab.notice.text())
