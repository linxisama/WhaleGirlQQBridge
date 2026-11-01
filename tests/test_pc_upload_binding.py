import json,os,tempfile,unittest,asyncio
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication,QMessageBox
import qq_gui
from src.plugins.manager import PluginManager
from src.plugin_files import upload_options
from src.pc_backend import allowed_apps
from src.gui.conversation import PcTab
from src.qq_app import QQRuntime,create_app
from fastapi.testclient import TestClient

class UploadGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.addCleanup(self.tmp.cleanup)
        self.manager=PluginManager(self.root/'data/qq-only');self.plugin=next(p for p in self.manager.list_plugins() if p['id']=='pc_control')
        self.root_patch=patch.object(qq_gui,'ROOT',self.root);self.root_patch.start();self.addCleanup(self.root_patch.stop)
    def dialog(self):
        d=qq_gui.PluginConfigDialog(self.plugin);self.addCleanup(d.close);return d
    def test_every_builtin_upload_schema_is_accepted_by_shared_importer(self):
        for plugin in self.manager.list_plugins():
            for key,spec in plugin['schema'].items():
                if spec.get('file_picker'):upload_options(spec)
    def test_robot_management_has_no_exe_upload_or_host_alias_field(self):
        d=self.dialog();self.assertNotIn('programAliases',self.plugin['schema'])
        self.assertFalse(d._file_buttons);self.assertNotIn('programAliases',self.manager.get_settings('pc_control'))
        self.assertEqual(allowed_apps(SimpleNamespace(root=self.root),'任意 | app.exe'),{})
    def test_readonly_owner_is_not_overwritten_by_an_old_dialog(self):
        self.manager.update_settings('pc_control',{'ownerIds':'original'})
        self.plugin=next(p for p in self.manager.list_plugins() if p['id']=='pc_control');d=self.dialog();self.assertTrue(d._widgets['ownerIds'][0].isReadOnly())
        self.manager.update_settings('pc_control',{'ownerIds':'new-owner'})
        with patch.object(qq_gui,'api_post',side_effect=lambda _,payload:(self.manager.update_settings('pc_control',payload) or {'success':True})):d._save()
        self.assertEqual(self.manager.get_settings('pc_control')['ownerIds'],'new-owner')

class BindingGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.data={'success':True,'enabled':True,'owners':'long-private-owner-id','jobs':[],'candidates':[{'id':'long-private-owner-id','name':'long-private-owner-id'},{'id':'other-id','name':'小鱼'}]}
        self.post=Mock(return_value={'success':True,'enabled':True})
        self.tab=PcTab(lambda _:self.data,self.post,Mock());self.addCleanup(self.tab.close);self.tab.refresh()
    def test_identifiers_are_not_duplicated_and_enabled_state_is_truthful(self):
        """候选标签要能认出"这是哪个号"。

        以前只显示"私聊用户 N · 后 6 位"，用户在机器人管理页根本认不出自己的账号
        （用户反馈过）。现在显示完整 QQ 号 + 昵称 + 来源，但**不重复**：
        昵称和号相同时只显示一次。
        """
        label=self.tab.owner.itemText(0)
        self.assertIn('long-private-owner-id',label)
        self.assertEqual(label.count('long-private-owner-id'),1,'不要重复显示同一个标识')
        other=self.tab.owner.itemText(1)
        self.assertIn('小鱼',other);self.assertIn('other-id',other)
        self.assertIn('已启用',self.tab.bound.text());self.assertFalse(self.tab.bind_button.isEnabled());self.assertTrue(self.tab.unbind_button.isEnabled())
    def test_refresh_does_not_rebuild_popup_or_lose_selection(self):
        self.tab.owner.setCurrentIndex(1)
        with patch.object(self.tab.owner,'clear',wraps=self.tab.owner.clear) as clear:
            self.tab.refresh();clear.assert_not_called()
        self.assertEqual(self.tab.owner.currentData(),'other-id')
        self.data['candidates'].append({'id':'third','name':'第三位'})
        with patch.object(self.tab.owner.view(),'isVisible',return_value=True),patch.object(self.tab.owner,'clear') as clear:self.tab.refresh();clear.assert_not_called()
        self.assertEqual(self.tab.owner.currentData(),'other-id')
    def test_binding_enabled_plugin_does_not_claim_disabled_and_unbind_has_action(self):
        self.tab.owner.setCurrentIndex(1);self.tab.bind();self.post.assert_called_with('pc/bind',{'owner':'other-id'})
        self.assertNotIn('请点击',self.tab.notice.text());self.tab.unbind();self.post.assert_called_with('pc/bind',{'owner':''})
    def test_panel_transparency_reaches_this_tab(self):
        """机器人管理页必须跟随外观页的「面板透明度」。

        它自带样式表，主窗口那条 CARD→rgba 的替换规则到不了它；以前写死 #ffffff，
        结果背景图透不出来（用户反馈"背景看不到、面板透明没用在这个界面"）。
        """
        self.tab.apply_panel_alpha(0.40)
        sheet=self.tab.styleSheet().lower()
        self.assertIn('rgba(255,255,255,0.400',sheet)
        self.assertNotIn('#ffffff',sheet,'不能再用不透明纯白')
        self.assertIn('rgba(238,246,250,0.400',self.tab.summary.styleSheet())

class BindingApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.bot=QQRuntime(root=self.root)
        self.client=TestClient(create_app(self.bot));self.client.__enter__()
        message={'id':'private','kind':'private','target':'verified','sender_id':'verified','sender_name':'','text':'你好'}
        self.bot.db.execute("INSERT INTO inbox(key,mode,account,room,payload,state,created) VALUES(?,?,?,?,?,'sent',1)",('fixture',self.bot.mode,self.bot.account(),'private:verified',json.dumps(message)))
        self.bot.db.execute("INSERT INTO pc_jobs VALUES('job','session','{}','status','','ready',1,'')");self.bot.db.commit()
    def tearDown(self):self.client.__exit__(None,None,None);self.tmp.cleanup()
    def test_binding_only_verified_private_sender_preserves_enabled_and_other_settings(self):
        self.bot.plugins.update_settings('pc_control',{'enabled':True,'maxPerHour':37})
        for owner in ('unknown','group-member'):self.assertEqual(self.client.post('/api/pc/bind',json={'owner':owner}).status_code,400)
        result=self.client.post('/api/pc/bind',json={'owner':'verified'}).json();self.assertTrue(result['enabled'])
        self.assertEqual(self.bot.plugins.get_settings('pc_control')['maxPerHour'],37)
        self.assertEqual(self.bot.db.execute("SELECT state FROM pc_jobs WHERE id='job'").fetchone()[0],'cancelled')
        self.assertTrue(self.client.get('/api/pc').json()['enabled'])
        self.client.post('/api/pc/bind',json={'owner':''});self.assertEqual(self.bot.plugins.get_settings('pc_control')['ownerIds'],'');self.assertTrue(self.bot.plugins.get_settings('pc_control')['enabled'])
    def test_cross_origin_and_wrong_format_are_rejected(self):
        self.assertEqual(self.client.post('/api/pc/bind',json={'owner':'verified'},headers={'Origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.client.post('/api/pc/bind',json={'owner':True}).status_code,400)
