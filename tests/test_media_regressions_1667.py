"""图片落盘、原生ASR、表情发送及搜索的可重复回归测试，无真实账号外发。"""
import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.received_images import ReceivedImages
from src.sticker_library import StickerLibrary
from src.plugins.manager import PluginManager
from src.qq_transports import OneBotQQ, QqBridge, onebot_message
from tests.test_sticker_suite import png


class ImageCacheTests(unittest.TestCase):
    def test_51_images_remove_oldest_half_and_keep_manual_files(self):
        with tempfile.TemporaryDirectory() as temp:
            cache=ReceivedImages(temp)
            manual=Path(temp)/'manual.png';manual.write_bytes(b'user file')
            paths=[cache.put(png(f'#{i:06x}',16),'png') for i in range(51)]
            self.assertEqual(len(cache.entries),26)
            self.assertTrue(all(not p.exists() for p in paths[:25]))
            self.assertTrue(all(p.exists() for p in paths[25:]))
            self.assertEqual(manual.read_bytes(),b'user file')
            self.assertEqual(len(ReceivedImages(temp).entries),26)

    def test_dedup_restart_and_index_failure_preserve_old_images(self):
        with tempfile.TemporaryDirectory() as temp:
            cache=ReceivedImages(temp);first=cache.put(png('red'),'png')
            self.assertEqual(cache.put(png('red'),'png'),first)
            cache=ReceivedImages(temp)
            new=png('blue');newpath=Path(temp)/(hashlib.sha256(new).hexdigest()+'.png')
            with patch.object(Path,'replace',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):cache.put(new,'png')
            self.assertTrue(first.exists());self.assertFalse(newpath.exists())

    def test_untrusted_index_cannot_delete_outside_or_manual_files(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'cache';folder.mkdir()
            outside=Path(temp)/'keep.png';outside.write_bytes(b'keep')
            (folder/'index.json').write_text(json.dumps({'../keep':{'ext':'png'},'a'*64:{'ext':'../png'}}))
            cache=ReceivedImages(folder);cache.put(png(),'png')
            self.assertEqual(outside.read_bytes(),b'keep')


class MediaFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.manager=PluginManager(self.root/'plugins');self.store=StickerLibrary(self.root/'stickers')
        self.peer=SimpleNamespace(call=AsyncMock(return_value={'url':'https://example.test/a'}),self_id='123')
        self.runtime=SimpleNamespace(root=self.root,mode='qqbridge',account=lambda:'123',bridge=self.peer,
            onebot=self.peer,stickers=self.store,note=lambda text:None,
            ai=SimpleNamespace(api_url='https://example.test/v1',model='vision',
                               describe_images=AsyncMock(return_value='蓝色鲸鱼')))

    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_bridge_file_id_download_is_saved_before_vision(self):
        raw=png();message={'text':'看图','media':[{'type':'image','file':'opaque-id'}]}
        async def vision(*args,**kwargs):
            files=list((self.root/'data/received_images').glob('*.png'))
            self.assertEqual(len(files),1);self.assertEqual(files[0].read_bytes(),raw)
            self.assertTrue(args[0][0].startswith('data:image/'))
            return '蓝色鲸鱼'
        self.runtime.ai.describe_images.side_effect=vision
        with patch('src.sticker_library.fetch_image',new=AsyncMock(return_value=raw)) as fetch:
            await self.manager.describe_images(message,self.runtime)
            await self.store.resolve(message['media'][0],self.runtime)
        self.peer.call.assert_awaited_once_with('get_image',{'file':'opaque-id'})
        fetch.assert_awaited_once();self.assertEqual(message['vision_status'],'ok')

    async def test_download_failure_never_sends_unstored_url_to_model(self):
        message={'media':[{'type':'image','url':'https://example.test/a'}]}
        with patch('src.sticker_library.fetch_image',new=AsyncMock(side_effect=OSError('private details'))):
            await self.manager.describe_images(message,self.runtime)
        self.runtime.ai.describe_images.assert_not_awaited()
        self.assertIn('失败',message['vision_status']);self.assertNotIn('private',str(message))

    async def test_same_file_id_across_accounts_does_not_share_download(self):
        raw=png();item={'file':'file1'}
        with patch('src.sticker_library.fetch_image',new=AsyncMock(return_value=raw)) as fetch:
            await self.store.resolve(item,self.runtime)
            self.runtime.account=lambda:'456'
            await self.store.resolve(item,self.runtime)
        self.assertEqual(fetch.await_count,2)

    async def test_supplied_transcript_survives_disabled_plugin_and_repeated_prepare(self):
        message=onebot_message({'post_type':'message','message_type':'private','message_id':'native',
            'user_id':456,'message':[{'type':'record','data':{'file':'a.silk','asr_refer_text':'明天带伞'}}]},'123')
        for pid in self.manager._settings:self.manager._settings[pid]['enabled']=False
        await self.manager.run_on_prepare(message,self.runtime)
        await self.manager.run_on_prepare(message,self.runtime)
        self.assertEqual(message['text'].count('明天带伞'),1)
        self.assertEqual(message['speech_status'],'已转写');self.peer.call.assert_not_awaited()

    async def test_native_asr_envelope_message_is_not_speech(self):
        plugin=self.manager._plugins['speech_to_text_qq']
        self.peer.call.return_value={'status':'ok','retcode':'0','message':'not ready','data':{}}
        self.assertIsNone(await plugin.transcribe(self.runtime,{'message_id':'opaque'}))
        self.peer.call.return_value={'status':'ok','retcode':'0','data':{'text':'你好'}}
        self.assertEqual(await plugin.transcribe(self.runtime,{'message_id':'opaque'}),'你好')
        self.assertEqual(self.peer.call.call_args.args,('fetch_ptt_text',{'message_id':'opaque'}))

    async def test_two_successful_records_are_not_marked_partial(self):
        plugin=self.manager._plugins['speech_to_text_qq']
        self.peer.call.return_value={'text':'你好'}
        message={'id':'x','media':[{'type':'record','message_id':'a'},{'type':'record','message_id':'b'}]}
        await plugin.on_prepare(message,self.runtime)
        self.assertEqual(message['speech_status'],'已转写')

    async def test_sticker_binary_reaches_both_transports_and_string_success_is_accepted(self):
        self.store.put(png(),'开心')
        for mode,cls in (('onebot',OneBotQQ),('qqbridge',QqBridge)):
            self.runtime.mode=mode
            media=self.manager._plugins['image_library'].media_for('开心',self.runtime)
            peer=cls();peer.online=True;sent=[]
            async def send_json(payload):
                sent.append(payload)
                peer.receive({'echo':payload['echo'],'status':'ok','retcode':'0','data':{'message_id':'m'}})
            async def send(payload):await send_json(json.loads(payload))
            peer.ws=SimpleNamespace(send_json=send_json,send=send)
            for kind in ('private','group'):
                await peer.send({'kind':kind,'target':'456','id':'x'},'给你',{'media':[media]})
                segments=sent[-1]['params']['message'];data=segments[-1]['data']
                self.assertEqual(base64.b64decode(data['file'][9:]),png())
                self.assertEqual(data['sub_type'],1)
            async def failed(payload):
                peer.receive({'echo':payload['echo'],'status':'failed','retcode':'0','message':'rejected'})
            async def failed_send(payload):await failed(json.loads(payload))
            peer.ws=SimpleNamespace(send_json=failed,send=failed_send)
            with self.assertRaises(RuntimeError):await peer.call('send_private_msg')

    async def test_search_uses_second_endpoint_and_returns_without_model(self):
        plugin=self.manager._plugins['search_query_hygiene']
        rss='\ufeff<rss><channel><item><title>真实标题</title><link>https://example.test/news</link><description>摘要</description></item></channel></rss>'
        message={'text':'/资讯 科技'}
        with patch('src.plugins.search_query_hygiene.read_public',new=AsyncMock(side_effect=[OSError(),rss])) as read:
            await plugin.on_prepare(message,self.runtime)
        self.assertEqual(read.await_count,2);self.assertIn('www.bing.com',read.call_args.args[0])
        self.assertIn('真实标题',message['_direct_reply']);self.assertIn('https://example.test/news',message['_direct_reply'])
        self.assertIn('未读取全文',message['_direct_reply'])

    async def test_all_search_endpoints_fail_without_inventing_sources(self):
        message={'text':'/热梗 咕咕'}
        with patch('src.plugins.search_query_hygiene.read_public',new=AsyncMock(side_effect=TimeoutError())):
            await self.manager._plugins['search_query_hygiene'].on_prepare(message,self.runtime)
        self.assertIn('失败',message['_direct_reply']);self.assertNotIn('http',message['_direct_reply'])

    async def test_generation_budget_stops_late_fallbacks_and_cancels_request(self):
        for pid in self.manager._settings:self.manager._settings[pid]['enabled']=False
        cancelled=[]
        async def stall(*args,**kwargs):
            try:await asyncio.sleep(30)
            finally:cancelled.append(True)
        engine=SimpleNamespace(chat=stall,last_error='')
        original=asyncio.wait_for
        async def shortened(request,budget):
            self.assertEqual(budget,5) # timeout=2，禁用重试，额外收尾3秒
            return await original(request,.02)
        with patch('src.plugins.manager.asyncio.wait_for',side_effect=shortened):
            reply=await self.manager.generate_reply(engine,[],timeout=2,retry_on_timeout=False)
        self.assertEqual(reply,'');self.assertTrue(cancelled);self.assertIn('总等待超时',engine.last_error)


class GroupProbabilityTests(unittest.TestCase):
    def test_probabilities_and_explicit_wake_exemptions(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=PluginManager(Path(temp));plugin=manager._plugins['chat_controls']
            manager._settings['short_followup']['enabled']=False
            manager.update_settings('chat_controls',{'requireAt':False,'ordinaryPercent':10})
            runtime=SimpleNamespace(mode='onebot',account=lambda:'123',
                db=SimpleNamespace(execute=lambda *a:SimpleNamespace(fetchone=lambda:(0,None))))
            message={'kind':'group','target':'456','text':'闲聊','at':False}
            with patch('src.plugins.chat_controls.random.random',return_value=.09):
                self.assertTrue(plugin.should_reply(message,runtime))
            with patch('src.plugins.chat_controls.random.random',return_value=.10):
                self.assertFalse(plugin.should_reply(message,runtime))
            manager.update_settings('chat_controls',{'ordinaryPercent':0})
            for change in ({'at':True},{'text':'鲸鱼娘在吗'},{'text':'/搜索 今日新闻'}):
                self.assertTrue(plugin.should_reply(dict(message,**change),runtime))
            self.assertTrue(plugin.should_reply(dict(message,kind='private'),runtime))


class KimiParametersTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_model_spelling_and_locked_parameters_without_mutation(self):
        from src.ai_engine import AIEngine
        engine=AIEngine(api_url='https://api.moonshot.cn/v1',api_key='fixture',model='Kimi K3')
        payload=engine._build_payload([{'role':'user','content':'你好'}],'',.1)
        self.assertEqual(payload['model'],'kimi-k3');self.assertNotIn('temperature',payload)
        self.assertEqual(payload['reasoning_effort'],'low')
        self.assertEqual(engine.model,'Kimi K3')
        await engine.close()

    async def test_third_party_aliases_and_other_models_are_untouched(self):
        from src.ai_engine import AIEngine
        for host,model in (('api.example.test','Kimi K3'),('api.moonshot.cn','moonshot-v1-8k')):
            engine=AIEngine(api_url='https://'+host+'/v1',api_key='fixture',model=model)
            payload=engine._build_payload([{'role':'user','content':'你好'}],'',.1)
            self.assertEqual(payload['model'],model);self.assertEqual(payload['temperature'],.1)
            self.assertNotIn('reasoning_effort',payload);await engine.close()

    async def test_k26_vision_disables_thinking_but_k3_is_never_disabled(self):
        from src.ai_engine import AIEngine
        for model in ('kimi-k2.6','Kimi K3'):
            engine=AIEngine(api_url='https://api.moonshot.cn/v1',api_key='fixture',model=model)
            engine.chat=AsyncMock(return_value='红色')
            await engine.describe_images(['data:image/png;base64,'+base64.b64encode(png('red')).decode()])
            options=engine.chat.call_args.kwargs
            self.assertEqual(options.get('thinking'),'disabled' if model=='kimi-k2.6' else None)
            await engine.close()
