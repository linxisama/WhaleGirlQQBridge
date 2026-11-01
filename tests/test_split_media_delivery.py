"""Verify two separate QQ bubbles and persist confirmed partial delivery."""
import asyncio,base64,json,unittest
from unittest.mock import AsyncMock,patch
import httpx
from test_conversation_suite import Fixture,event
from src.qq_app import _send_with_pacing,SplitReplyError,QQRuntime

GIF=base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
IMAGE={'type':'image','data':{'file':'base64://'+base64.b64encode(GIF).decode()}}

class DeliveryTests(Fixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup();self.bot.running=True;self.bot.official.authenticated=True
        self.bot.ai.chat=AsyncMock(return_value='哼，给你啦～')
        self.bot.official.authorize=AsyncMock(return_value='fake-token')
        self.requests=[]
        await self.bot.official.http.aclose()
        self.bot.official.http=httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
    def respond(self,request):
        data=json.loads(request.content);self.requests.append((request.url.path,data))
        return httpx.Response(200,json={'file_info':'uploaded'} if request.url.path.endswith('/files') else {'id':'out'})
    async def asyncTearDown(self):await self.cleanup()
    async def settle(self,state='sent'):
        task=asyncio.create_task(self.bot._work())
        try:
            for _ in range(70):
                row=self.bot.db.execute('SELECT state FROM inbox ORDER BY created DESC LIMIT 1').fetchone()
                if row and row[0]==state:return
                await asyncio.sleep(.05)
            self.fail(str(row))
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    def sent(self):return [data for path,data in self.requests if path.endswith('/messages')]
    def library(self,**settings):
        self.bot.stickers.put(GIF,label='开心')
        self.bot.plugins.update_settings('image_library',dict(enabled=True,**settings))
    async def test_official_group_text_then_image_different_sequences(self):
        m=event(kind='group',target='g')
        await _send_with_pacing(self.bot.official,m,'正文',{'media':[IMAGE],'separate_images':True})
        sends=self.sent();self.assertEqual(len(sends),2)
        self.assertEqual([(v['msg_type'],v['msg_seq'],v['content']) for v in sends],[(0,1,'正文'),(7,2,' ')])
        self.assertEqual(sends[0]['msg_id'],sends[1]['msg_id']);self.assertNotIn('media',sends[0]);self.assertIn('media',sends[1])
        self.assertEqual(m['_delivery_parts']['image'],'sent')
    async def test_private_uses_users_endpoint_and_empty_image_text_space(self):
        await _send_with_pacing(self.bot.official,event(),'正文',{'media':[IMAGE],'separate_images':True})
        self.assertTrue(all(path.startswith('/v2/users/') for path,_ in self.requests))
        self.assertEqual(self.sent()[1]['content'],' ')
    async def test_401_retry_keeps_each_stage_sequence_stable(self):
        count={1:0,2:0}
        def auth(request):
            response=self.respond(request)
            if request.url.path.endswith('/messages'):
                seq=json.loads(request.content)['msg_seq'];count[seq]+=1
                if count[seq]==1:return httpx.Response(401,json={})
            return response
        await self.bot.official.http.aclose();self.bot.official.http=httpx.AsyncClient(transport=httpx.MockTransport(auth))
        await _send_with_pacing(self.bot.official,event(),'正文',{'media':[IMAGE],'separate_images':True})
        sends=self.sent();self.assertEqual([v['msg_seq'] for v in sends],[1,1,2,2])
        self.assertEqual(sends[0],sends[1]);self.assertEqual(sends[2],sends[3])
    async def test_explicit_start_sequence_has_noncolliding_second_part(self):
        await _send_with_pacing(self.bot.official,dict(event(),_passive_seq=2),'正文',{'media':[IMAGE],'separate_images':True})
        self.assertEqual([v['msg_seq'] for v in self.sent()],[2,3])
    async def test_no_text_sends_only_one_image(self):
        await _send_with_pacing(self.bot.official,event(),' ',{'media':[IMAGE],'separate_images':True})
        self.assertEqual(len(self.sent()),1);self.assertEqual(self.sent()[0]['msg_type'],7)
    async def test_plain_text_and_disabled_split_remain_one_message(self):
        await _send_with_pacing(self.bot.official,event(),'正文')
        self.assertEqual(len(self.sent()),1);self.requests.clear()
        await _send_with_pacing(self.bot.official,event(),'正文',{'media':[IMAGE],'separate_images':False})
        self.assertEqual(len(self.sent()),1);self.assertEqual(self.sent()[0]['content'],'正文')
    async def test_cadence_remains_one_logical_reply_despite_two_bubbles(self):
        self.library();self.bot.plugins.update_settings('sticker_cadence',{'enabled':True,'sendEveryN':1})
        self.bot.ingest(event(),'official');await self.settle()
        self.assertEqual(len(self.sent()),2);self.bot.ai.chat.assert_awaited_once()
        self.assertEqual(list(self.bot.plugins._plugins['sticker_cadence']._counts.values()),[1])
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
    async def test_explicit_sticker_command_splits_without_model_call(self):
        self.library();self.bot.ingest(event(text='/表情 开心'),'official');await self.settle()
        self.assertEqual(len(self.sent()),2);self.bot.ai.chat.assert_not_awaited()
    async def test_new_setting_can_restore_combined_bubble(self):
        self.library(separateMessages=False);self.bot.ingest(event(text='/表情 开心'),'official');await self.settle()
        self.assertEqual(len(self.sent()),1)
    async def test_image_upload_failure_records_text_sent_without_resending(self):
        self.library()
        def fail(request):
            if request.url.path.endswith('/files'):return httpx.Response(400,json={})
            return self.respond(request)
        await self.bot.official.http.aclose();self.bot.official.http=httpx.AsyncClient(transport=httpx.MockTransport(fail))
        self.bot.ingest(event(text='/表情 开心'),'official');await self.settle('partial')
        self.assertEqual(len(self.sent()),1)
        state,raw,error=self.bot.db.execute('SELECT state,payload,error FROM inbox').fetchone()
        self.assertEqual(json.loads(raw)['_delivery_parts']['text'],'sent');self.assertIn('文字已发送',error)
        self.bot.ingest(event(text='/表情 开心'),'official');self.assertEqual(len(self.sent()),1)
        await self.settle('partial');self.assertEqual(len(self.sent()),1)
    async def test_image_timeout_is_unknown_and_never_repeats_text(self):
        self.library()
        def timeout(request):
            if request.url.path.endswith('/messages') and json.loads(request.content)['msg_seq']==2:
                raise httpx.ReadTimeout('mock timeout',request=request)
            return self.respond(request)
        await self.bot.official.http.aclose();self.bot.official.http=httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        self.bot.ingest(event(text='/表情 开心'),'official');await self.settle('partial')
        self.assertEqual(len(self.sent()),1)
        raw,error=self.bot.db.execute('SELECT payload,error FROM inbox').fetchone()
        self.assertEqual(json.loads(raw)['_delivery_parts']['image'],'uncertain');self.assertIn('待核实',error)
    async def test_pause_after_text_skips_image_and_reports_partial(self):
        def paused(request):
            value=self.respond(request)
            if request.url.path.endswith('/messages'):self.bot.reply_paused=True
            return value
        self.library();await self.bot.official.http.aclose();self.bot.official.http=httpx.AsyncClient(transport=httpx.MockTransport(paused))
        self.bot.ingest(event(text='/表情 开心'),'official');await self.settle('partial')
        self.assertEqual(len(self.sent()),1);self.assertFalse(any(path.endswith('/files') for path,_ in self.requests))
    async def test_invalid_image_and_insufficient_sequences_do_not_send_text(self):
        for message,media in ((dict(event(),_passive_seq=5),IMAGE),(event(),{'type':'image','data':{'file':'invalid'}})):
            with self.assertRaises(ValueError):
                await _send_with_pacing(self.bot.official,message,'正文',{'media':[media],'separate_images':True})
        self.assertFalse(self.requests)
    async def test_recovery_keeps_partial_confirmed_text_without_automatic_retry(self):
        m=dict(event(),_delivery_parts={'text':'sent','image':'pending'})
        self.bot.db.execute("INSERT INTO inbox(key,mode,account,room,payload,state,reply,created) VALUES('recovery','official','app','private:owner',?,'sending','正文',0)",(json.dumps(m),));self.bot.db.commit()
        self.bot._mark_interrupted_delivery('模拟中断')
        self.assertEqual(self.bot.db.execute('SELECT state FROM inbox').fetchone()[0],'partial')
        self.assertEqual(self.bot.db.execute('SELECT text FROM bot_messages').fetchone()[0],'正文')
