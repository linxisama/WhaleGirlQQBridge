"""1.6.39 新能力跨消息/存储/传输回归；所有QQ外发均为本地模拟。"""
import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import httpx
from src.qq_app import QQRuntime,create_app
from src.qq_transports import official_event,OfficialQQ,QQSendError
from src.persona_bindings import resolve_persona,bindings,revision,validate_rows
from src.chat_state import scope_of,build_history,persona_key
from src.plugins.forward_summary import nodes_of,render_forward
from src.plugins.token_saver import truncate_history_content
from src.token_saver_util import dedupe_conversation
from src.bilibili_share import bili_source,read_video,approved_video
from src.plugin_segments import build_reply_segments
from src.menu_catalog import official_menu_payload
from tests.test_conversation_suite import Fixture,event


class FeatureTests(Fixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup();self.bot.ai.chat=AsyncMock(return_value='正常回答');self.bot.official.send=AsyncMock(return_value={'id':'sent'})
        self.bot.style.save_persona(dict(name='gentle',display_name='温柔',system_prompt='你是温柔的人设，简短回复。',temperature=.6,max_tokens=500))
    async def asyncTearDown(self):await self.cleanup()
    def bind(self,rows):
        p=self.bot.plugins._plugins['archive_isolation'];p.save_state('bindings.json',{'rows':validate_rows(rows,self.bot)})
    async def drain(self,message):
        self.bot.running=True;self.bot.official.authenticated=True;self.bot.ingest(message,'official')
        worker=asyncio.create_task(self.bot._work())
        try:
            for _ in range(150):
                row=self.bot.db.execute('SELECT state FROM inbox ORDER BY created DESC LIMIT 1').fetchone()
                if row and row[0] in ('sent','failed','uncertain','error_replied','blocked'):return row[0]
                await asyncio.sleep(.02)
            self.fail('消息未完成')
        finally:
            self.bot.running=False;worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    def test_group_and_member_binding_priority_without_global_switch(self):
        original=self.bot.style.active_name
        self.bind([dict(kind='group',target='G1',user='',persona=original),dict(kind='group',target='G1',user='A',persona='gentle')])
        a=event(kind='group',target='G1',subject='A');b=event(kind='group',target='G1',subject='B')
        self.assertEqual(resolve_persona(self.bot,a).name,'gentle');self.assertEqual(resolve_persona(self.bot,b).name,original)
        self.assertNotEqual(scope_of(a,self.bot),scope_of(b,self.bot));self.assertEqual(self.bot.style.active_name,original)
        c=event(kind='group',target='G2',subject='A');self.assertEqual(resolve_persona(self.bot,c).name,original)
    async def test_private_binding_persists_restart_and_account_separation(self):
        self.bind([dict(kind='private',target='owner',user='',persona='gentle')])
        self.assertEqual(resolve_persona(self.bot,event()).name,'gentle')
        other=QQRuntime(root=self.bot.root)
        try:
            self.assertEqual(resolve_persona(other,event()).name,'gentle')
            other.official.app_id='another-account';self.assertNotEqual(resolve_persona(other,event()).name,'gentle')
        finally:await other.close()
    async def test_effective_persona_reaches_group_worker_and_manual_memory(self):
        self.bind([dict(kind='group',target='G1',user='A',persona='gentle')])
        self.bot.db.execute('INSERT INTO self_memories VALUES(?,?,?,?,?)',('m','gentle','只在温柔角色里出现的记忆',1,time.time()));self.bot.db.commit()
        state=await self.drain(event(kind='group',target='G1',subject='A',text='你好'))
        self.assertEqual(state,'sent');args=self.bot.ai.chat.call_args
        self.assertIn('温柔的人设',args.kwargs['system_prompt']);self.assertIn('只在温柔角色里',str(args))
        saved=json.loads(self.bot.db.execute('SELECT payload FROM inbox').fetchone()[0]);self.assertEqual(saved['_archive_key'],'gentle')
    async def test_binding_routes_revision_validation_and_catalog(self):
        app=create_app(self.bot);app.state.bot=self.bot
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            data=(await client.get('/api/persona-bindings')).json()
            body=dict(rows=[dict(kind='group',target='G1',user='',persona='gentle')],revision=data['revision'])
            self.assertEqual((await client.post('/api/persona-bindings',json=body)).status_code,200)
            self.assertEqual((await client.post('/api/persona-bindings',json=body)).status_code,400)
            self.assertEqual((await client.get('/api/group-diagnostics')).status_code,200)
            cat=(await client.get('/api/command-catalog')).json();self.assertTrue(any(x['command']=='/菜单' for x in cat['commands']))
            bad=dict(body,revision=(await client.get('/api/persona-bindings')).json()['revision']);bad['rows'][0]['persona']='missing'
            self.assertEqual((await client.post('/api/persona-bindings',json=bad)).status_code,400)
    async def test_menu_private_and_group_skip_model_and_send_once(self):
        self.bot.plugins.update_settings('quick_menu',{'enabled':True})
        for kind in ('group','private'):
            self.bot.official.send.reset_mock()
            m=event(mid='menu-'+kind,text='/菜单',kind=kind)
            self.assertEqual(await self.drain(m),'sent');self.bot.official.send.assert_awaited_once()
            self.assertIn('/热点',self.bot.official.send.call_args.args[1])
        self.bot.ai.chat.assert_not_awaited()
    async def test_bili_video_command_reaches_existing_worker_delivery(self):
        self.bot.plugins.update_settings('quick_menu',{'enabled':True})
        result=dict(title='测试视频',description='内容简介',uploader='UP',url='https://www.bilibili.com/video/BV1xx411c7mD',video={'url':'https://cdn.bilivideo.com/video.mp4','size':100})
        with patch('src.plugins.quick_menu.read_video',new=AsyncMock(return_value=result)):
            self.assertEqual(await self.drain(event(text='/视频 BV1xx411c7mD',kind='group')),'sent')
        self.bot.ai.chat.assert_not_awaited();meta=self.bot.official.send.call_args.args[2]
        self.assertEqual(meta['video']['url'],result['video']['url'])
    async def test_bridge_nested_forward_standard_singular_response_and_cycle(self):
        self.bot.config.set('qq_only.mode','qqbridge')
        p=self.bot.plugins._plugins['forward_summary']
        outer={'message':[{'sender':{'nickname':'甲'},'message':[{'type':'text','data':{'text':'外层'}},{'type':'forward','data':{'id':'inner'}}]}]}
        inner={'messages':[{'nickname':'乙','content':[{'type':'text','data':{'text':'内层内容'}},{'type':'image','data':{'url':'https://example.test/1.png'}},{'type':'forward','data':{'id':'root'}}]}]}
        self.bot.bridge.call=AsyncMock(side_effect=[outer,inner])
        m=event(media=[{'type':'forward','id':'root'}]);await p.on_prepare(m,self.bot)
        rendered=render_forward(m['media'][0]['nodes'],200,8000)
        self.assertIn('内层内容',rendered['text']);self.assertIn('乙',rendered['text']);self.assertEqual(self.bot.bridge.call.await_count,2)
        self.assertEqual(len([x for x in m['media'] if x['type']=='image']),1);self.assertIn('重复',m['forward_status'])
    async def test_forward_failure_preserves_attached_nodes_and_status(self):
        self.bot.onebot.call=AsyncMock(side_effect=TimeoutError());self.bot.config.set('qq_only.mode','onebot')
        p=self.bot.plugins._plugins['forward_summary'];m=event(media=[{'type':'forward','id':'bad'},{'type':'forward','nodes':[{'content':'已读内容'}]}])
        await p.on_prepare(m,self.bot);self.assertIn('失败',m['forward_status']);self.assertEqual(m['media'][1]['nodes'][0]['content'],'已读内容')
    async def test_hot_first_request_refreshes_in_parallel_and_keeps_sources(self):
        p=self.bot.plugins._plugins['hot_trend_tracker'];m=event(text='/热点')
        data={'topics':[dict(title='话题',source='bilibili',url='https://www.bilibili.com/video/BV1xx411c7mD',time=time.time(),key='b:话题')], 'attempt':time.time(),'updated':time.time(),'errors':[]}
        self.assertIsNone(p.on_ingest(m,self.bot))
        with patch.object(p,'refresh',new=AsyncMock(return_value=data)) as refresh:await p.on_prepare(m,self.bot)
        refresh.assert_awaited_once();self.assertIn('bilibili',m['_direct_reply']);self.assertIn('https://',m['_direct_reply'])
    async def test_bridge_voice_supplied_text_and_protocol_transcript(self):
        p=self.bot.plugins._plugins['speech_to_text_qq'];self.bot.config.set('qq_only.mode','qqbridge')
        self.bot.bridge.call=AsyncMock(return_value={'data':{'text':'你好鲸鱼'}})
        m=event(text='[语音]',media=[{'type':'record','file':'voice.silk'}]);await p.on_prepare(m,self.bot)
        self.assertEqual(m['speech_status'],'已转写');self.assertIn('你好鲸鱼',m['text']);self.bot.bridge.call.assert_awaited_once()
        self.bot.bridge.call.reset_mock();m=event(media=[{'type':'record','text':'平台已经转写'}]);await p.on_prepare(m,self.bot)
        self.bot.bridge.call.assert_not_awaited();self.assertIn('平台已经转写',m['text'])
    async def test_voice_total_budget_and_partial_result(self):
        p=self.bot.plugins._plugins['speech_to_text_qq'];self.bot.config.set('qq_only.mode','qqbridge')
        self.bot.plugins.update_settings('speech_to_text_qq',{'budgetMs':1000})
        async def slow(action,params):await asyncio.sleep(2);return '不应等到'
        self.bot.bridge.call=slow;m=event(media=[{'type':'record','file':'a','message_id':'1'},{'type':'record','file':'b','message_id':'2'}])
        start=time.monotonic();await p.on_prepare(m,self.bot);self.assertLess(time.monotonic()-start,1.3);self.assertIn('失败',m['speech_status'])
    async def test_empty_meme_search_gives_usage_without_network(self):
        p=self.bot.plugins._plugins['search_query_hygiene'];m=event(text='/热梗')
        with patch('src.plugins.search_query_hygiene.read_public',new=AsyncMock()) as read:await p.on_prepare(m,self.bot)
        read.assert_not_awaited();self.assertIn('用法',m['_direct_reply'])
    async def test_forward_honors_configured_node_budget(self):
        p=self.bot.plugins._plugins['forward_summary'];self.bot.plugins.update_settings('forward_summary',{'maxNodes':1})
        m=event(media=[{'type':'forward','nodes':[{'content':'一'},{'content':'二'}]}]);await p.on_prepare(m,self.bot)
        self.assertEqual(len(m['media'][0]['nodes']),1);self.assertIn('预算',m['forward_status'])
    async def test_known_group_event_with_missing_fields_is_unparsed_not_notification(self):
        self.bot.running=True
        async def receive(callback,should_stop):
            await callback('READY',{})
            await callback('GROUP_AT_MESSAGE_CREATE',{'group_openid':'g','content':'缺ID'})
            await callback('GROUP_AT_MESSAGE_CREATE',{'id':'group-valid','group_openid':'g','content':'测试','author':{'member_openid':'u'}})
        self.bot.official.run_websocket=receive;await self.bot._official_receiver()
        rows=self.bot.db.execute("SELECT event,outcome,reason FROM inbound_audit").fetchall()
        self.assertTrue(any(e=='READY' and o=='ignored' for e,o,r in rows))
        self.assertTrue(any(e=='GROUP_AT_MESSAGE_CREATE' and o=='unparsed' and '缺少' in r for e,o,r in rows))
        self.assertFalse(any(e=='GROUP_AT_MESSAGE_CREATE' and o=='ignored' for e,o,r in rows))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
    async def test_signed_webhook_incomplete_group_event_is_visible_in_diagnostics(self):
        from src.qq_transports import signing_key
        app=create_app(self.bot);app.state.bot=self.bot;self.bot.running=True
        raw=json.dumps({'op':0,'t':'GROUP_AT_MESSAGE_CREATE','d':{'group_openid':'G','content':'missing id'}}).encode()
        stamp=str(int(time.time()));signature=signing_key(self.bot.official.secret).sign(stamp.encode()+raw).hex()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            response=await client.post('/qq/callback',content=raw,headers={'x-signature-timestamp':stamp,'x-signature-ed25519':signature})
            self.assertEqual(response.status_code,200)
            result=(await client.get('/api/group-diagnostics')).json()
            self.assertTrue(any(r['outcome']=='unparsed' for r in result['groups']))


class PublicReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_bili_metadata_and_progressive_video(self):
        data={'code':0,'data':{'bvid':'BV1xx411c7mD','title':'标题','desc':'简介','owner':{'name':'作者'},'cid':123,'duration':90}}
        stream={'code':0,'data':{'format':'mp4','durl':[{'url':'http://cdn.bilivideo.com/a.mp4','size':1000}]}}
        with patch('src.bilibili_share.read_public',new=AsyncMock(side_effect=[json.dumps(data),json.dumps(stream)])):
            result=await read_video('https://www.bilibili.com/video/BV1xx411c7mD',True)
        self.assertTrue(result['video']['url'].startswith('https://'));self.assertEqual(result['title'],'标题')
    async def test_short_url_resolves_only_bilibili_and_no_download_on_bad_host(self):
        with patch('src.bilibili_share.read_public',new=AsyncMock(return_value=('', 'https://evil.test/video/BV1xx411c7mD'))):
            with self.assertRaises(ValueError):await read_video('https://b23.tv/short')
    async def test_video_unavailable_returns_link_metadata(self):
        data={'code':0,'data':{'bvid':'BV1xx411c7mD','title':'标题','cid':1,'duration':999}}
        with patch('src.bilibili_share.read_public',new=AsyncMock(return_value=json.dumps(data))):result=await read_video('https://www.bilibili.com/video/BV1xx411c7mD',True)
        self.assertIn('video_error',result);self.assertNotIn('video',result)
    async def test_official_video_upload_and_reject_fallback(self):
        transport=OfficialQQ('app','fake');transport._api=AsyncMock(side_effect=[{'file_info':'vid'},{'id':'sent'}])
        try:
            m=event();await transport.send(m,'原视频链接',{'video':{'url':'https://cdn.bilivideo.com/a.mp4'}})
            self.assertEqual(transport._api.call_args_list[0].args[2]['file_type'],2)
            self.assertEqual(transport._api.call_args_list[1].args[2]['msg_type'],7)
            transport._api=AsyncMock(side_effect=[ValueError('upload fail'),{'id':'link'}]);await transport.send(m,'原链接',{'video':{'url':'https://cdn.bilivideo.com/a.mp4'}})
            self.assertEqual(transport._api.call_args.args[2]['msg_type'],0)
        finally:await transport.close()
    async def test_rich_menu_permission_failure_falls_back_only_on_known_rejection(self):
        t=OfficialQQ('a','s');t._api=AsyncMock(side_effect=[QQSendError('permission',code=40034090),{}])
        try:
            await t.send(event(),'/菜单',{'menu':{'commands':[('/热点','看热点')]}})
            self.assertEqual(t._api.await_count,2);self.assertEqual(t._api.call_args.args[2]['msg_type'],0)
        finally:await t.close()
    async def test_heartbeat_missing_ack_closes_stale_connection_ack_recovers(self):
        t=OfficialQQ('a','s');ws=SimpleNamespace(send=AsyncMock(),close=AsyncMock())
        try:
            with patch('src.qq_transports.asyncio.sleep',new=AsyncMock()):await t._ws_heartbeat(ws,5)
            ws.send.assert_awaited_once();ws.close.assert_awaited_once();self.assertFalse(t.ws_connected)
            await t._ws_handle({'op':11},AsyncMock());self.assertFalse(t._heartbeat_pending);self.assertGreater(t.last_heartbeat_ack,0)
        finally:await t.close()


class PureFeatureTests(unittest.TestCase):
    def test_official_chat_record_images_author_and_empty_record_marker(self):
        data=dict(id='m',group_openid='g',author={'member_openid':'u'},message_type=102,msg_elements=[{'author':{'username':'甲'},'content':'原话','attachments':[{'content_type':'image/png','url':'https://example.test/1.png'}]}])
        m=official_event('GROUP_AT_MESSAGE_CREATE',data);self.assertIn('甲',m['text']);self.assertTrue(m['media'][0]['_from_chat_record'])
        data.pop('msg_elements');self.assertTrue(official_event('GROUP_AT_MESSAGE_CREATE',data)['_is_chat_record'])
    def test_full_text_dedupe_does_not_delete_similar_long_material_or_system(self):
        a='共同开头'*100
        rows=[{'role':'system','content':'规则'},{'role':'system','content':'规则'}, {'role':'user','content':a+'内容一'},{'role':'user','content':a+'内容二'}]
        self.assertEqual(dedupe_conversation(rows),0);self.assertEqual(truncate_history_content(rows,80)>0,True);self.assertEqual(rows[0]['content'],'规则')
    def test_media_history_smaller_keeps_original_and_current_text(self):
        m=event(media=[{'type':'image','url':'https://example.test/1.png'}]);m.update(_persona_key='p',vision_description='识图内容'*500,vision_status='success')
        raw=json.dumps(m,ensure_ascii=False);stats={}
        full=build_history([(raw,'回答')],'p');short=build_history([(raw,'回答')],'p',media_chars=100,stats=stats)
        self.assertLess(len(str(short)),len(str(full)));self.assertGreater(stats['_context_saved_chars'],0);self.assertEqual(json.loads(raw)['vision_description'],m['vision_description'])
    def test_video_source_and_segments_reject_lookalikes(self):
        self.assertFalse(approved_video('https://bilivideo.com.evil.test/a.mp4'));self.assertFalse(approved_video('file:///C:/private.mp4'))
        self.assertEqual(bili_source({'text':'https://bilibili.com.evil.test/x'}),'')
        self.assertEqual(build_reply_segments('x',{'video':{'url':'https://evil.test/a.mp4'}}),[{'type':'text','data':{'text':'x'}}])
        self.assertEqual(build_reply_segments('',{'video':{'url':'https://cdn.bilivideo.com/a.mp4'}})[0]['type'],'video')
    def test_standard_onebot_json_card_keeps_bilibili_link(self):
        from src.qq_transports import onebot_message
        raw=json.dumps({'app':'com.tencent.structmsg','meta':{'detail_1':{'title':'视频','qqdocurl':'https://b23.tv/abcdef'}}})
        m=onebot_message(dict(post_type='message',message_type='group',message_id=1,group_id=123,user_id=456,message=[{'type':'json','data':{'data':raw}}]),'999')
        self.assertEqual(bili_source(m),'https://b23.tv/abcdef')
    def test_menu_buttons_are_normal_commands_with_no_quote(self):
        body=official_menu_payload({'commands':[('/热点','热点'),('/搜索','搜索')]},'菜单')
        action=body['keyboard']['content']['rows'][0]['buttons'][0]['action'];self.assertEqual(action['type'],2);self.assertFalse(action['reply']);self.assertFalse(action['enter'])
    def test_rss_search_keeps_source_publication_time_and_link(self):
        from src.plugins.search_query_hygiene import parse_search
        rss='<rss><channel><item><title>新闻</title><link>https://example.test/news</link><description>摘要</description><pubDate>Mon, 19 Oct 2026</pubDate></item></channel></rss>'
        result=parse_search(rss);self.assertIn('2026',result[0]['published']);self.assertEqual(result[0]['url'],'https://example.test/news')


class FeaturesGuiTests(unittest.TestCase):
    def test_binding_edit_save_conflict_and_menu_copy(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PyQt6.QtWidgets import QApplication
        from src.gui.features import FeaturesDialog
        app=QApplication.instance() or QApplication([]);calls=[]
        def get(path):
            if path=='persona-bindings':return dict(rows=[],revision='v1',mode='official',recent=[dict(kind='group',target='G',user='U')],personas=[dict(name='p',display_name='鲸鱼')])
            if path=='command-catalog':return dict(commands=[dict(command='/搜索 关键词',description='搜索')],note='配置说明')
            return dict(mode='official',groups=[],running=True,worker_alive=True)
        def post(path,body):calls.append((path,body));return {'message':'已保存'}
        dialog=FeaturesDialog(get,post,lambda pid:None)
        try:
            dialog.add();dialog.table.cellWidget(0,1).setEditText('G');dialog.table.cellWidget(0,2).setText('U');dialog.save()
            self.assertEqual(calls[0][1]['rows'][0]['persona'],'p');self.assertEqual(calls[0][1]['rows'][0]['user'],'U')
            dialog.catalog.selectRow(0);dialog.copy_command();self.assertEqual(app.clipboard().text(),'/搜索')
            dialog.diagnose();self.assertIn('尚未收到',dialog.diagnostic.toPlainText());dialog.economy();self.assertEqual(calls[-1][1]['historyMediaChars'],240)
        finally:dialog.close();dialog.deleteLater();app.processEvents()
