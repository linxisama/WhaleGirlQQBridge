"""线上回归：按真实传输格式测试，不把单个模型连接成功视为全链路成功。"""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from src.qq_app import QQRuntime
from src.qq_transports import official_event,onebot_message
from src.plugins.manager import PluginManager
from src.ai_engine import AIEngine
from tests.test_persona_image import png_bytes


class OfficialCommandRegressionTests(unittest.TestCase):
    def test_bare_group_at_is_a_real_message_while_empty_private_message_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'config.qq.yaml').write_text('ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: fake\n',encoding='utf-8')
            bot=QQRuntime(root=root)
            try:
                for pid in bot.plugins._settings:bot.plugins._settings[pid]['enabled']=False
                bot.running=True
                group=official_event('GROUP_AT_MESSAGE_CREATE',{'id':'only-at','group_openid':'g1','author':{'member_openid':'u1'},'content':''})
                bot.ingest(group,'official')
                self.assertEqual(bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
                private=official_event('C2C_MESSAGE_CREATE',{'id':'empty','author':{'user_openid':'u1'},'content':''})
                bot.ingest(private,'official')
                self.assertEqual(bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
            finally:bot.running=False;asyncio.run(bot.close())

    def test_group_addressed_plugin_commands_reach_their_handlers(self):
        for pid,command,expected in [('conversation_memory','/记住 我喜欢蓝色','记住了'),
                                     ('image_library','/表情','图库'),('hot_trend_tracker','/热点','热点')]:
            with self.subTest(plugin=pid),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);(root/'config.qq.yaml').write_text('ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: fake\n',encoding='utf-8')
                bot=QQRuntime(root=root)
                try:
                    for key in bot.plugins._plugins:bot.plugins._settings[key]['enabled']=key==pid
                    if pid=='hot_trend_tracker':
                        # 此用例覆盖已有缓存的同步命令；首次刷新由异步准备阶段专项覆盖。
                        import time
                        bot.plugins._plugins[pid].save_state('topics.json',{'attempt':time.time(),'topics':[]})
                    bot.running=True
                    message=official_event('GROUP_AT_MESSAGE_CREATE',{'id':'m1','group_openid':'g1',
                        'author':{'member_openid':'u1'},'content':' <@!BOT> '+command})
                    bot.ingest(message,'official')
                    row=bot.db.execute('SELECT reply FROM inbox').fetchone()
                    self.assertTrue(row and row[0] and expected in row[0],(pid,row))
                finally:bot.running=False;asyncio.run(bot.close())

    def test_normal_mentions_still_render_and_private_mentions_are_not_stripped(self):
        payload={'id':'m1','group_openid':'g1','author':{'member_openid':'u1'},'content':'<@!BOT> 你好 <@!OTHER>'}
        message=official_event('GROUP_AT_MESSAGE_CREATE',payload)
        self.assertTrue(message['at']);self.assertEqual(message['at_ids'],['BOT','OTHER'])
        self.assertIn('你好',message['text']);self.assertIn('@某人',message['text'])
        private=official_event('C2C_MESSAGE_CREATE',dict(payload,author={'user_openid':'u1'},content='<@!OTHER> /表情'))
        self.assertTrue(private['text'].startswith('@某人'))


class BridgeMessageRegressionTests(unittest.TestCase):
    def test_invalid_shapes_still_rejected_after_accepting_valid_strings(self):
        for value in (None, {'invalid':True}, 12):
            with self.subTest(value=value),self.assertRaises(ValueError):
                onebot_message({'post_type':'message','message_type':'private','message_id':1,
                    'user_id':3,'message':value},'10001')

    def test_cq_string_at_and_image_are_equivalent_to_array(self):
        event={'post_type':'message','message_type':'group','message_id':1,'group_id':2,'user_id':3}
        source='[CQ:at,qq=10001] 看看[CQ:image,file=opaque.png,url=https://example.org/a.png?x=1&#44;y=2]'
        got=onebot_message(dict(event,message=source),'10001')
        wanted=onebot_message(dict(event,message=[{'type':'at','data':{'qq':'10001'}},
            {'type':'text','data':{'text':' 看看'}},{'type':'image','data':{'file':'opaque.png','url':'https://example.org/a.png?x=1,y=2'}}]),'10001')
        self.assertEqual(got,wanted)

    def test_cq_escaped_text_is_not_executed_as_a_mention(self):
        got=onebot_message({'post_type':'message','message_type':'group','message_id':1,'group_id':2,
            'user_id':3,'message':'&#91;CQ:at,qq=10001&#93; &amp;#91;普通文字'},'10001')
        self.assertFalse(got['at']);self.assertEqual(got['text'],'[CQ:at,qq=10001] &#91;普通文字')


class ImageRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_transfer_does_not_depend_on_collection_or_long_image_plugins(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=PluginManager(Path(temp));plugin=manager._plugins['image_understanding']
            for pid in manager._settings:manager._settings[pid]['enabled']=pid=='image_understanding'
            ai=SimpleNamespace(api_url='https://example.test/v1',model='vision',last_error='',describe_images=AsyncMock(return_value='红色图片'))
            store=SimpleNamespace(resolve=AsyncMock(return_value={'data':png_bytes(24,24,(255,0,0)),'width':24,'height':24}),record_description=lambda *a:None)
            runtime=SimpleNamespace(ai=ai,mode='official',account=lambda:'bot',stickers=store,note=lambda *a:None)
            await plugin.describe({'media':[{'type':'image','url':'https://example.org/temporary.png'}]},runtime)
            store.resolve.assert_awaited_once()
            self.assertTrue(ai.describe_images.await_args.args[0][0].startswith('data:image/'))

    async def test_vision_does_not_start_with_an_overly_small_reasoning_budget(self):
        engine=AIEngine('https://example.test/v1','fake',model='reasoning',max_retries=2)
        requests=[]
        async def respond(request):
            payload=json.loads(request.content);requests.append(payload)
            text='红色图片' if payload['max_tokens']>=1024 else ''
            return httpx.Response(200,json={'choices':[{'message':{'content':text,'reasoning_content':'分析' if not text else ''},'finish_reason':'stop' if text else 'length'}]})
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        engine._get_client=AsyncMock(return_value=client)
        try:
            text=await engine.describe_images(['base64://'+__import__('base64').b64encode(png_bytes(24,24)).decode()])
            self.assertEqual(text,'红色图片');self.assertGreaterEqual(requests[0]['max_tokens'],1024)
            self.assertEqual(len(requests),1)
        finally:await client.aclose();await engine.close()

    async def test_global_timeout_preserves_explicitly_recognized_long_image_parts(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=PluginManager(Path(temp));plugin=manager._plugins['image_understanding']
            manager._settings['image_understanding']['timeoutSec']=1
            async def partial(message,runtime):
                message['vision_description']='第一段明确识别到 TEST42';message['vision_status']='部分缺失：后续尚未读取'
                await asyncio.sleep(30)
            plugin.describe=partial;message={'media':[{'type':'image','url':'https://example.org/long.png'}]}
            await manager.describe_images(message,SimpleNamespace(note=lambda *a:None))
            self.assertIn('TEST42',message.get('vision_description',''))
            self.assertTrue(message['vision_status'].startswith('部分缺失'))


class PluginIsolationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_notes_do_not_claim_working_official_commands_are_unavailable(self):
        from src.plugin_capabilities import apply_capabilities
        runtime=SimpleNamespace(mode='official',plugins=SimpleNamespace(_plugins={}),running=True)
        rows=apply_capabilities([{'id':'poke_counter','settings':{},'enabled':True},
                                {'id':'send_pacing','settings':{},'enabled':True}],runtime)
        self.assertIn('/戳一戳',rows[0]['status_note'])
        self.assertNotIn('只在普通',rows[0]['status_note'])
        self.assertIn('5',rows[1]['status_note']);self.assertNotIn('合为一条',rows[1]['status_note'])

    async def test_bad_enabled_property_cannot_break_other_post_reply_plugins(self):
        class Broken:
            META=SimpleNamespace(id='broken')
            @property
            def enabled(self):raise RuntimeError('broken setting')
        class Good:
            META=SimpleNamespace(id='good');enabled=True
            async def on_post_reply(self,message,reply,runtime):return reply+'正常后处理'
        manager=PluginManager.__new__(PluginManager);manager._plugins={'broken':Broken(),'good':Good()}
        got=await manager.run_on_post_reply({},'正文',SimpleNamespace(note=lambda *a:None))
        self.assertEqual(got,'正文正常后处理')
