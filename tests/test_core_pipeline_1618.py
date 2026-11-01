"""复现用户的默认插件组合，经传输解析、完整工作循环、出站边界验证。"""
import asyncio,base64,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock
from src.qq_app import QQRuntime
from src.qq_transports import official_event,onebot_message
from tests.test_persona_image import png_bytes

ACTIVE=('archive_isolation','cross_chat_link','long_image_reader','hot_trend_tracker',
        'search_query_hygiene','sticker_admin','conversation_memory','chat_controls',
        'emotion_state','image_library','memes','burst_merge','anti_repeat','short_followup',
        'mention_nudge_guard','addressee_guard','sticker_cadence','quote_reply','sticker_memory',
        'face_decode','forward_summary','image_understanding')


class MixedPluginPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        (root/'config.qq.yaml').write_text('ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: vision\nqq:\n  app_id: fake\n  client_secret: fake\n',encoding='utf-8')
        self.bot=QQRuntime(root=root)
        for pid in self.bot.plugins._settings:self.bot.plugins._settings[pid]['enabled']=pid in ACTIVE
        self.bot.ai.describe_images=AsyncMock(return_value='图片中有红色矩形和蓝色圆形，文字TEST42。')
        self.conversations=[];self.sent=[]
        async def generate(messages,**kwargs):
            self.conversations.append(json.loads(json.dumps(messages)))
            return '这是完整回答'+str(len(self.conversations))+'。'
        async def send(message,text,meta=None):
            self.sent.append((dict(message),text,meta));return {'id':'out'+str(len(self.sent))}
        self.bot.ai.chat=AsyncMock(side_effect=generate)
        self.bot.official.send=AsyncMock(side_effect=send);self.bot.official.authenticated=True
        self.bot.onebot.send=AsyncMock(side_effect=send);self.bot.onebot.online=True;self.bot.onebot.self_id='10001'
        self.worker=None

    async def asyncTearDown(self):
        self.bot.running=False
        if self.worker:self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
        await self.bot.close();self.temp.cleanup()

    def message(self,mode,kind,mid,*,image=False):
        encoded=base64.b64encode(png_bytes(48,32,(255,0,0))).decode()
        source='data:image/png;base64,'+encoded
        if mode=='official':
            return official_event('GROUP_AT_MESSAGE_CREATE' if kind=='group' else 'C2C_MESSAGE_CREATE',{
                'id':mid,'group_openid':'g1','author':{'member_openid':'30003','user_openid':'30003'},
                'content':('<@!BOT> ' if kind=='group' else '')+('帮我看这张图'+mid if image else '这是问题'+mid),
                'attachments':[{'content_type':'image/png','url':source}] if image else []})
        segments=([{'type':'at','data':{'qq':'10001'}}] if kind=='group' else [])+[
            {'type':'text','data':{'text':'帮我看这张图'+mid if image else '这是问题'+mid}}]
        if image:segments.append({'type':'image','data':{'file':'base64://'+encoded}})
        return onebot_message({'post_type':'message','message_type':kind,'message_id':mid,
            'group_id':20002,'user_id':30003,'message':segments},'10001')

    async def run_message(self,message):
        count=len(self.sent);self.bot.ingest(message,self.bot.mode)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if len(self.sent)>count:return self.sent[-1]
            await asyncio.sleep(.01)
        rows=self.bot.db.execute('SELECT state,error FROM inbox').fetchall()
        self.fail('完整插件组合未发送回复：'+repr(rows))

    async def exercise(self,mode):
        self.bot.config.set('qq_only.mode',mode);self.bot.running=True
        self.worker=asyncio.create_task(self.bot._work())
        for index,(kind,image) in enumerate([('group',False),('private',False),('group',True),('private',True)]):
            message=self.message(mode,kind,str(index),image=image)
            sent,text,meta=await self.run_message(message)
            self.assertIn('完整回答',text)
            if image:
                self.assertEqual(sent['vision_status'],'ok')
                self.assertTrue(any('TEST42' in str(turn.get('content')) for turn in self.conversations[-1]))
                self.assertTrue(self.bot.ai.describe_images.await_args.args[0][0].startswith('data:image/'))
        self.assertEqual(len(self.sent),4);self.assertEqual(len(self.conversations),4)
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='sent'").fetchone()[0],4)

    async def test_official_private_group_text_and_images_with_22_plugins(self):
        await self.exercise('official')

    async def test_onebot_private_group_text_and_images_with_22_plugins(self):
        await self.exercise('onebot')

    async def test_bare_at_is_replied_to_in_official_and_bridge_modes(self):
        self.bot.running=True;self.worker=asyncio.create_task(self.bot._work())
        official=official_event('GROUP_AT_MESSAGE_CREATE',{'id':'bare-official','group_openid':'g1',
            'author':{'member_openid':'30003'},'content':''})
        _,text,_=await self.run_message(official);self.assertIn('完整回答',text)
        self.bot.config.set('qq_only.mode','onebot')
        bridge=onebot_message({'post_type':'message','message_type':'group','message_id':'bare-bridge',
            'group_id':20002,'user_id':30003,'message':'[CQ:at,qq=10001]'},'10001')
        _,text,_=await self.run_message(bridge);self.assertIn('完整回答',text)

    async def test_group_prefix_commands_with_22_plugins_skip_model_and_send(self):
        self.bot.running=True;self.worker=asyncio.create_task(self.bot._work())
        message=self.message('official','group','cmd')
        message=official_event('GROUP_AT_MESSAGE_CREATE',{'id':'cmd','group_openid':'g1',
            'author':{'member_openid':'30003'},'content':'<@!BOT> /记住 我喜欢蓝色'})
        sent,text,meta=await self.run_message(message)
        self.assertIn('记住了',text);self.bot.ai.chat.assert_not_awaited()

    async def test_no_mention_blacklist_and_pause_do_not_leak_replies(self):
        self.bot.config.set('qq_only.mode','onebot');self.bot.running=True
        message=self.message('onebot','group','blocked');message['at']=False
        self.bot.ingest(message,'onebot')
        self.bot.plugins._settings['chat_controls']['blockedUsers']='30003'
        message=self.message('onebot','private','blacklisted');self.bot.ingest(message,'onebot')
        self.bot.plugins._settings['chat_controls']['blockedUsers']=''
        self.bot.reply_paused=True
        message=self.message('onebot','private','paused');self.bot.ingest(message,'onebot')
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.bot.official.send.assert_not_awaited();self.bot.onebot.send.assert_not_awaited()
