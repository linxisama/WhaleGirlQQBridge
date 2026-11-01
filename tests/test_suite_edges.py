import asyncio,json,tempfile,time,unittest,sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock,Mock
from src.plugins.manager import PluginManager
from src.plugins.long_image_reader import tile_data
from src.chat_state import ChatState
from src.pc_backend import allowed_apps
from src.qq_transports import OfficialQQ
from src.gui.conversation import MemoryTab,PcTab

class SuiteEdges(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.manager=PluginManager(self.root/'data/qq-only')
    async def asyncTearDown(self):self.tmp.cleanup()
    async def test_long_image_real_encoding_batches_and_failed_region(self):
        from PyQt6.QtGui import QImage
        from PyQt6.QtCore import QBuffer,QIODevice
        image=QImage(500,9000,QImage.Format.Format_RGB32);image.fill(0xffeeeeee);out=QBuffer();out.open(QIODevice.OpenModeFlag.WriteOnly);image.save(out,'PNG')
        data=bytes(out.data());tiles,partial=tile_data(data,12);self.assertFalse(partial);self.assertGreater(len(tiles),3)
        ai=SimpleNamespace(describe_images=AsyncMock(side_effect=['第一组内容','']))
        msg={};await self.manager._plugins['long_image_reader'].describe_parts(msg,SimpleNamespace(ai=ai),(tiles,False),'vision')
        self.assertIn('部分缺失',msg['vision_status']);self.assertIn('第一组',msg['vision_description']);self.assertEqual(ai.describe_images.await_count,2)
        self.assertTrue(all(len(c.args[0])<=3 for c in ai.describe_images.await_args_list))
    async def test_hot_refresh_preserves_cache_on_source_failure_and_bounds(self):
        p=self.manager._plugins['hot_trend_tracker'];now=time.time();p.save_state('topics.json',{'topics':[dict(key='k',title='旧',time=now,url='https://example.test',source='baidu',description='')],'updated':now})
        with patch('src.plugins.hot_trend_tracker.read_public',new=AsyncMock(side_effect=RuntimeError('offline'))):
            s=await p.refresh()
        self.assertEqual(s['updated'],now);self.assertEqual(len(s['topics']),1);self.assertEqual(len(s['errors']),3)
    async def test_search_failure_is_quoted_not_a_system_instruction(self):
        p=self.manager._plugins['search_query_hygiene'];message={'text':'/搜索 测试'}
        with patch('src.plugins.search_query_hygiene.read_public',new=AsyncMock(side_effect=RuntimeError('offline'))):await p.on_prepare(message,None)
        conversation=[];p.on_pre_ai(message,conversation,None);self.assertEqual(conversation[0]['role'],'user');self.assertIn('搜索失败',str(conversation))
    async def test_proactive_official_skips_model_and_jitter_never_sleeps(self):
        p=self.manager._plugins['silence_ping'];self.manager.update_settings('silence_ping',{'enabled':True,'targets':'123','quietRange':'00:00-00:00','targetIdleMinMinutes':1})
        rt=SimpleNamespace(mode='official',ai=SimpleNamespace(chat=AsyncMock()))
        await p.tick(rt);rt.ai.chat.assert_not_awaited()
        rt.mode='onebot';rt.note=lambda *_:None
        with patch.object(p,'_last_dm_time',return_value=time.time()-7200),patch.object(p,'_last_bot_dm_time',return_value=None),patch('src.plugins.silence_ping.asyncio.sleep',new=AsyncMock()) as sleep:
            await p.tick(rt);sleep.assert_not_awaited();self.assertIn('due',p._tstate['123'])
        recreated=type(p)(self.manager);self.assertEqual(recreated._tstate['123']['due'],p._tstate['123']['due'])
    def test_custom_apps_only_owned_uploaded_exe_no_shell_or_external_file(self):
        folder=self.root/'data/plugin_uploads/pc_control/apps';folder.mkdir(parents=True);exe=folder/'demo.exe';exe.write_bytes(b'fixture')
        outside=self.root/'outside.exe';outside.write_bytes(b'fixture')
        apps=allowed_apps(SimpleNamespace(root=self.root),f'测试 | {exe}\n越界 | {outside}\n命令 | cmd /c calc')
        self.assertEqual(apps,{})
    async def test_official_passive_result_seq_is_stable_across_auth_retry(self):
        bot=OfficialQQ('x','y');bot.authorize=AsyncMock();sent=[]
        import httpx
        async def handler(request):
            sent.append(json.loads(request.content));return httpx.Response(401 if len(sent)==1 else 200,json={})
        await bot.http.aclose();bot.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:await bot.send({'kind':'private','target':'owner','id':'incoming','_passive_seq':2},'结果')
        finally:await bot.http.aclose()
        self.assertEqual([v['msg_seq'] for v in sent],[2,2]);self.assertEqual(sent[0],sent[1])

class NativeSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os;os.environ['QT_QPA_PLATFORM']='offscreen'
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def test_memory_rows_and_diagnostics_bind_actions(self):
        get=lambda p:{'success':True,'persona':'鲸鱼娘','autoRemember':True,'manual':[{'id':'x','content':'蓝色','enabled':True}],'structured':[]} if p.startswith('memory') else {'decisions':[dict(kind='group',target='g',sender='u',reason='未被点名')]}
        post=Mock(return_value={'success':True});tab=MemoryTab(get,post,Mock());tab.refresh();self.assertEqual(tab.manual_table.rowCount(),1);self.assertEqual(tab.decisions.rowCount(),1)
        tab.manual_table.cellWidget(0,0).click();post.assert_called_with('memory',{'action':'enable','id':'x','enabled':False});tab.close()
    def test_owner_binding_does_not_enable_until_explicit_choice(self):
        get=lambda p:{'success':True,'owners':'','jobs':[],'candidates':[{'id':'opaque-owner','name':'主人'}]}
        post=Mock(return_value={'success':True});tab=PcTab(get,post,Mock());tab.refresh();tab.bind();post.assert_called_once_with('pc/bind',{'owner':'opaque-owner'});tab.timer.stop();tab.close()
