import asyncio,json,sqlite3,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import yaml,httpx
from fastapi.testclient import TestClient
from src.qq_app import QQRuntime,create_app
from src.chat_state import scope_of,ChatState,build_history
from src.pc_service import PcService
from src.plugins.pc_control import parse_command
from src.plugins.long_image_reader import plan_tiles,tile_data
from src.plugins.search_query_hygiene import clean_query,parse_search
from src.plugins.hot_trend_tracker import parse_topics

def event(mid='1',text='你好',kind='private',subject='owner',target=None,at=True,**extra):
    return dict(id=mid,text=text,kind=kind,target=target or subject,sender=subject,sender_id=subject,sender_name=subject,at=at,**extra)

class Fixture:
    def setup(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        cfg=root/'config.qq.yaml';cfg.write_text(yaml.safe_dump({'ai':{'api_url':'https://example.test/v1','api_key':'fake','model':'mock'},'qq':{'app_id':'app','client_secret':'fake'}}),'utf-8')
        self.bot=QQRuntime(root=root)
        for pid in self.bot.plugins._plugins:self.bot.plugins.update_settings(pid,{'enabled':False})
        for pid in ('chat_controls','short_followup','archive_isolation','conversation_memory'):self.bot.plugins.update_settings(pid,{'enabled':True})
    async def cleanup(self):await self.bot.close();self.tmp.cleanup()

class ConversationTests(Fixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.setup()
    async def asyncTearDown(self):await self.cleanup()
    def test_followup_requires_success_same_person_scope_and_no_other_at(self):
        # 此测试验证跟聊身份/时间边界，抽签行为由1667专项测试单独覆盖。
        self.bot.plugins.update_settings('chat_controls',{'ordinaryPercent':100})
        p=self.bot.plugins._plugins['short_followup'];m=event(kind='group',target='g',at=False)
        self.assertFalse(p.eligible(m,self.bot));p.on_pre_ai(m,[],self.bot);self.assertFalse(p.eligible(m,self.bot))
        p.on_sent(m,'已发',self.bot);self.assertTrue(p.eligible(m,self.bot));self.assertTrue(self.bot.plugins._plugins['chat_controls'].should_reply(m,self.bot))
        for changed in (dict(subject='other'),dict(target='g2'),dict(at_ids=['somebody'])):
            values=dict(kind='group',target='g',at=False);values.update(changed);self.assertFalse(p.eligible(event(**values),self.bot))
        with patch('src.plugins.short_followup.time.time',return_value=time.time()+301):self.assertFalse(p.eligible(m,self.bot))
    def test_blacklist_prevents_immediate_plugin_and_memory(self):
        self.bot.running=True;self.bot.plugins.update_settings('chat_controls',{'blockedUsers':'owner'})
        self.bot.ingest(event(text='/记住 喜欢蓝色'),'official')
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM chat_memories').fetchone()[0],0)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.assertIn('黑名单',self.bot.decisions[-1]['reason'])
    def test_memory_independent_sources_not_last_only(self):
        state=self.bot.chat_state;s=scope_of(event(),self.bot)
        for mid in ('a','b','a'):state.remember(s,'owner','喜欢蓝色',mid)
        self.assertEqual(state.recall(s,'owner')[0]['evidence'],2)
    def test_archive_memory_is_restored_and_private_does_not_enter_group(self):
        rt=self.bot;old=rt.style.active_name;m=event();scope=scope_of(m,rt)
        rt.chat_state.remember(scope,'owner','喜欢蓝色','a',True)
        rt.style._active='新角色';other=scope_of(m,rt);self.assertNotEqual(scope,other);self.assertFalse(rt.chat_state.recall(other,'owner'))
        rt.style._active=old;self.assertEqual(scope_of(m,rt),scope)
        cross=rt.plugins._plugins['cross_chat_link'];rt.plugins.update_settings('cross_chat_link',{'enabled':True})
        c=[];cross.on_pre_ai(event(kind='group',target='g'),c,rt);self.assertEqual(c,[])
        cross.on_pre_ai(event(target='other-room'),c,rt);self.assertIn('喜欢蓝色',str(c))
    def test_history_archive_key_keeps_role_history(self):
        rows=[(json.dumps(dict(event(),_archive_key='鲸鱼娘',_persona_key='old')),'回复')]
        self.assertEqual(len(build_history(rows,'new',6000,'鲸鱼娘')),2)
        self.assertFalse(build_history(rows,'new',6000,'其他人设'))
    async def run_worker_until(self,state,timeout=3):
        task=asyncio.create_task(self.bot._work())
        try:
            for _ in range(int(timeout/.05)):
                rows=self.bot.db.execute('SELECT state FROM inbox').fetchall()
                if rows and rows[0][0]==state:return
                await asyncio.sleep(.05)
            self.fail(str(rows))
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    async def test_model_failure_feedback_once_not_history_or_cadence(self):
        self.bot.running=True;self.bot.official.authenticated=True;self.bot.ai.chat=AsyncMock(return_value='');self.bot.official.send=AsyncMock()
        self.bot.ingest(event(),'official');await self.run_worker_until('error_replied')
        self.bot.official.send.assert_awaited_once();self.assertEqual(self.bot.db.execute('SELECT count(*) FROM bot_messages').fetchone()[0],0)
        self.assertFalse(self.bot.plugins._plugins['short_followup']._last_replied)
    async def test_empty_postprocessed_reply_with_only_bookkeeping_does_not_fake_sent(self):
        self.bot.running=True;self.bot.official.authenticated=True;self.bot.ai.chat=AsyncMock(return_value='生成过的内容');self.bot.official.send=AsyncMock()
        async def empty(message,reply,runtime):runtime.reply_meta={'library_ids':['fixture']};return ''
        with patch.object(self.bot.plugins,'prepare_delivery',side_effect=empty):
            self.bot.ingest(event(),'official');await self.run_worker_until('error_replied')
        self.bot.official.send.assert_awaited_once();self.assertFalse(self.bot.plugins._plugins['short_followup']._last_replied)

    async def test_send_timeout_no_second_error_message(self):
        self.bot.running=True;self.bot.official.authenticated=True;self.bot.ai.chat=AsyncMock(return_value='你好');self.bot.official.send=AsyncMock(side_effect=TimeoutError())
        self.bot.ingest(event(),'official');await self.run_worker_until('uncertain');self.bot.official.send.assert_awaited_once()
    async def test_pre_ai_block_is_not_sent(self):
        self.bot.running=True;self.bot.official.authenticated=True;self.bot.official.send=AsyncMock()
        with patch.object(self.bot.plugins,'run_on_pre_ai_async',new=AsyncMock(return_value={'block':True})):
            self.bot.ingest(event(),'official');await self.run_worker_until('blocked')
        self.bot.official.send.assert_not_awaited()
    async def test_forward_images_and_nested_nodes_have_budget(self):
        p=self.bot.plugins._plugins['forward_summary'];m=event(media=[{'type':'forward','nodes':[{'content':[{'type':'image','data':{'url':'https://example.test/1.png'}}]}]*5}])
        await p.on_prepare(m,self.bot);self.assertEqual(len([v for v in m['media'] if v['type']=='image']),3);self.assertIn('5张图',m['forward_status'])

class PcTests(Fixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup();self.bot.running=True;self.bot.plugins.update_settings('pc_control',{'enabled':True,'ownerIds':'owner'})
        self.backend=SimpleNamespace(execute=lambda *args:('测试完成',None));self.bot.pc=PcService(self.bot,self.backend)
    async def asyncTearDown(self):await self.cleanup()
    def create_job(self):
        m=event(text='机器人 暂停');reply=self.bot.plugins._plugins['pc_control'].on_ingest(m,self.bot);self.assertIn('审核',reply)
        self.bot.pc.receipt_sent(m['_pc_job']);return m['_pc_job']
    def test_parser_rejects_shell_and_unknown_actions(self):
        for value in ('电脑 打开 powershell','电脑 打开 cmd & del','电脑 删除 C:\\重要文件','电脑 音量 101','电脑 打开 记事本;calc'):
            self.assertEqual(parse_command(value),('help',''))
        self.assertEqual(parse_command('电脑 音量 30'),('help',''));self.assertEqual(parse_command('机器人 暂停'),('pause',''))
        self.assertEqual(parse_command('普通聊天'),None)
    def test_owner_and_group_denied_without_creating_job(self):
        p=self.bot.plugins._plugins['pc_control']
        for m in (event(text='电脑 截图',subject='stranger'),event(text='电脑 状态',kind='group')):
            self.assertIn('接受',p.on_ingest(m,self.bot))
        self.assertFalse(self.bot.pc.list())
    async def test_execution_once_queues_same_passive_id_seq_two(self):
        job=self.create_job();spy=unittest.mock.Mock(return_value=('结果',{'media':[]}));self.backend.execute=spy
        self.assertTrue((await self.bot.pc.review(job,True))['success'])
        with self.assertRaises(ValueError):await self.bot.pc.review(job,True)
        spy.assert_called_once();m=json.loads(self.bot.db.execute('SELECT payload FROM inbox').fetchone()[0]);self.assertEqual(m['_passive_seq'],2);self.assertEqual(m['id'],'1')
    async def test_local_rejection_expiry_mode_owner_stop_and_rate_limit(self):
        job=self.create_job();spy=unittest.mock.Mock();self.backend.execute=spy
        await self.bot.pc.review(job,False);spy.assert_not_called()
        for i,mutation in enumerate(('expiry','owner','stop','rate')):
            self.bot.running=True;self.bot.plugins.update_settings('pc_control',{'ownerIds':'owner','maxPerHour':1})
            m=event(mid=str(i+2));self.bot.pc.request(m,'pause','');key=m['_pc_job'];self.bot.pc.receipt_sent(key)
            if mutation=='expiry':self.bot.db.execute('UPDATE pc_jobs SET created=? WHERE id=?',(time.time()-121,key))
            if mutation=='owner':self.bot.plugins.update_settings('pc_control',{'ownerIds':'different'})
            if mutation=='stop':self.bot.running=False
            if mutation=='rate':self.bot.db.execute("UPDATE pc_jobs SET state='failed',created=? WHERE id=?",(time.time(),job))
            with self.assertRaises(ValueError):await self.bot.pc.review(key,True)
        spy.assert_not_called()
    async def test_restart_cancels_pending_and_failure_does_not_reexecute(self):
        key=self.create_job();PcService(self.bot,self.backend)
        with self.assertRaises(ValueError):await self.bot.pc.review(key,True)
        m=event(mid='new');self.bot.pc.request(m,'pause','');self.bot.pc.receipt_sent(m['_pc_job'])
        self.backend.execute=unittest.mock.Mock(side_effect=RuntimeError('fixture failure'))
        self.assertFalse((await self.bot.pc.review(m['_pc_job'],True))['success'])
        with self.assertRaises(ValueError):await self.bot.pc.review(m['_pc_job'],True)
        self.backend.execute.assert_called_once()

class PureTests(unittest.TestCase):
    def test_overlapping_tiles_cover_long_image_and_explicitly_truncate(self):
        plan,partial=plan_tiles(1000,9000,12);self.assertFalse(partial);self.assertEqual(plan[-1][0]+plan[-1][1],9000)
        for previous,current in zip(plan,plan[1:]):self.assertLess(current[0],previous[0]+previous[1])
        self.assertTrue(plan_tiles(1000,30000,2)[1])
        with self.assertRaises(ValueError):plan_tiles(10000,10000)
    def test_search_cleaning_preserves_meaning_and_urls(self):
        self.assertEqual(clean_query('/搜索 帮我查一下 夏亚 是什么梗？'),'夏亚')
        self.assertEqual(clean_query('什么值得买'),'什么值得买')
        results=parse_search('<li class="b_algo"><h2><a href="https://example.com">标题</a></h2><p>摘要</p></li>')
        self.assertEqual(results[0]['url'],'https://example.com');self.assertEqual(results[0]['snippet'],'摘要')
    def test_hot_sources_parsing_and_failure(self):
        data=json.dumps({'code':0,'data':{'list':[{'title':'测试','bvid':'BVfixture123'}]}})
        self.assertEqual(len(parse_topics('bilibili',data)),1)
        with self.assertRaises(ValueError):parse_topics('bilibili','{"code":-1}')

class ApiTests(Fixture,unittest.TestCase):
    def setUp(self):self.setup();self.client=TestClient(create_app(self.bot));self.client.__enter__()
    def tearDown(self):self.client.__exit__(None,None,None);self.tmp.cleanup()
    def test_memory_ui_roundtrip_and_untrusted_origin(self):
        self.assertTrue(self.client.post('/api/memory',json={'action':'add','content':'喜欢蓝色'}).json()['success'])
        m=self.client.get('/api/memory').json()['manual'][0]
        self.client.post('/api/memory',json={'action':'enable','id':m['id'],'enabled':False})
        self.assertFalse(self.client.get('/api/memory').json()['manual'][0]['enabled'])
        self.assertEqual(self.client.post('/api/pc/review',headers={'Origin':'https://evil.test'},json={'approve':True,'id':'a'}).status_code,403)
        self.client.post('/api/memory',json={'action':'delete','id':m['id']});self.assertEqual(self.client.get('/api/memory').json()['manual'],[])
