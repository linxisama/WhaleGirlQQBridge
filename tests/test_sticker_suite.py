"""Offline end-to-end sticker collection, cadence, vision and reaction tests."""
import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import httpx
import yaml
from PyQt6.QtCore import QBuffer,QIODevice
from PyQt6.QtGui import QImage,QColor
from src.sticker_library import StickerLibrary,frames_for_vision,decode_image,public_host,fetch_image
from src.plugins.manager import PluginManager
from src.qq_app import QQRuntime
from src.qq_transports import onebot_notice

def png(color='#30a5a0',size=128):
    image=QImage(size,size,QImage.Format.Format_RGB32);image.fill(QColor(color))
    out=QBuffer();out.open(QIODevice.OpenModeFlag.WriteOnly);assert image.save(out,'PNG')
    return bytes(out.data())

def msg(mid='1',target='group',kind='group',text='鲸鱼娘你好',**kw):
    return dict(id=mid,target=target,kind=kind,text=text,sender='用户',sender_id='100',at=True,media=[],**kw)

class SuiteFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.manager=PluginManager(self.root/'plugins');self.store=StickerLibrary(self.root/'library')
        self.runtime=SimpleNamespace(mode='official',account=lambda:'app',running=True,stickers=self.store,
            note=lambda s:None,reply_meta=None,ai_lock=asyncio.Lock(),
            onebot=SimpleNamespace(self_id='999',call=AsyncMock()),
            ai=SimpleNamespace(api_url='https://example.test/v1',model='vision',describe_images=AsyncMock(return_value='开心的小鲸鱼'),chat=AsyncMock(return_value='["128077"]')))
    async def asyncTearDown(self):self.tmp.cleanup()

class StorageTests(SuiteFixture):
    async def test_content_dedup_and_persistent_metadata(self):
        one,new=self.store.put(png(),'开心');two,new2=self.store.put(png(),'别名')
        self.assertTrue(new);self.assertFalse(new2);self.assertEqual(one['id'],two['id'])
        self.store.update(one['id'],'喜悦','笑得很开心');self.store.used([one['id']])
        reloaded=StickerLibrary(self.store.root)
        self.assertEqual(len(reloaded.entries()),1);self.assertEqual(reloaded.entries()[0]['uses'],1)
        self.assertEqual(reloaded.entries()[0]['label'],'喜悦')

    async def test_only_owned_files_evicted_and_limit_enforced(self):
        outside=self.root/'manual.png';outside.write_bytes(png())
        first,_=self.store.put(png('red'),'红',max_items=1)
        second,_=self.store.put(png('blue'),'蓝',max_items=1)
        self.assertEqual([e['id'] for e in self.store.entries()],[second['id']])
        self.assertTrue(outside.exists());self.assertFalse((self.store.images/(first['id']+'.png')).exists())

    async def test_invalid_contents_traversal_and_duplicate_labels_do_not_change_library(self):
        first,_=self.store.put(png(),'开心');second,_=self.store.put(png('blue'),'蓝')
        before=self.store.index.read_bytes()
        for call in [lambda:self.store.put(b'not image'),lambda:self.store.update(first['id'],'蓝',''),lambda:self.store.update(first['id'],'../[bad]',''),lambda:self.store.remove('../manual')]:
            with self.assertRaises((KeyError,ValueError)):call()
        self.assertEqual(self.store.index.read_bytes(),before)

    async def test_failed_index_save_keeps_old_images_and_index(self):
        first,_=self.store.put(png(),'开心');before=self.store.index.read_bytes()
        with patch.object(self.store,'_save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.store.put(png('red'),'红',max_items=1)
        self.assertTrue(self.store.path(first['id']).exists());self.assertEqual(self.store.index.read_bytes(),before)

    async def test_delete_cannot_affect_manual_upload(self):
        outside=self.root/'manual.png';outside.write_bytes(png())
        entry,_=self.store.put(png(),'开心');self.store.remove(entry['id'])
        self.assertEqual(self.store.entries(),[]);self.assertTrue(outside.is_file())

    async def test_gif_is_decoded_as_multiple_frames(self):
        # Valid single-frame GIF, repeated image block makes a two-frame fixture.
        raw=base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        start=raw.index(b'\x21\xf9');animated=raw[:-1]+raw[start:-1]+b';'
        frames=frames_for_vision(animated,2)
        self.assertEqual(len(frames),2);self.assertTrue(all(f.startswith('data:image/png;') for f in frames))

    async def test_inline_resolve_never_reads_incoming_local_path(self):
        local=self.root/'private.png';local.write_bytes(png())
        self.runtime.mode='onebot';self.runtime.onebot.call.return_value={'file':str(local)}
        with self.assertRaises(ValueError):await self.store.resolve({'file':str(local)},self.runtime)
        self.runtime.onebot.call.assert_awaited_once_with('get_image',{'file':str(local)})

    async def test_public_fetch_rejects_private_dns_and_redirects(self):
        with self.assertRaises(ValueError):await public_host('http://127.0.0.1/file')
        with patch('src.sticker_library.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]):
            with self.assertRaises(ValueError):await public_host('https://example.test/file')

    async def test_streamed_body_limit_without_content_length(self):
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'12345')))
        with patch('src.sticker_library.httpx.AsyncClient',return_value=client),patch('src.sticker_library.public_host',new=AsyncMock(side_effect=lambda s:s)):
            with self.assertRaises(ValueError):await fetch_image('https://example.test/a',maximum=4)

class CollectionTests(SuiteFixture):
    async def test_onebot_sticker_style_parameter_survives_real_send_assembly(self):
        from src.qq_transports import OneBotQQ
        self.runtime.mode='onebot';entry,_=self.store.put(png(),'开心')
        library=self.manager._plugins['image_library'];media=library.media_for('开心',self.runtime)
        transport=OneBotQQ();transport.online=True;transport.call=AsyncMock(return_value={'message_id':123})
        await transport.send(msg(target='100'),'给你',{'media':[media]})
        sent=transport.call.call_args.args[1]['message'][-1]['data']
        self.assertEqual(sent['sub_type'],1);self.assertEqual(sent['summary'],'[动画表情]')
        self.manager.update_settings('image_library',{'asSticker':False})
        self.assertNotIn('sub_type',library.media_for('开心',self.runtime)['data'])
    async def test_received_sticker_enters_library_and_can_really_be_selected(self):
        self.manager.update_settings('sticker_steal',{'enabled':True})
        message=msg();message['media']=[{'type':'image','file':'base64://'+base64.b64encode(png()).decode(),'sub_type':1}]
        await self.manager.observe(message,self.runtime);await self.manager.observe(message,self.runtime)
        self.assertEqual(len(self.store.entries()),1)
        entry=self.store.entries()[0];library=self.manager._plugins['image_library']
        reply,meta=await library.on_post_reply(message,f'给你 [[表情:{entry["label"]}]]',self.runtime)
        self.assertEqual(base64.b64decode(meta['media'][0]['data']['file'][9:]),png())
        self.assertEqual(meta['library_ids'],[entry['id']])

    async def test_large_photo_not_collected_and_disabled_plugin_no_fetch(self):
        message=msg();message['media']=[{'type':'image','file':'base64://'+base64.b64encode(png(size=800)).decode()}]
        with patch.object(self.store,'resolve',new=AsyncMock()) as resolve:
            await self.manager.observe(message,self.runtime);resolve.assert_not_awaited()
        self.manager.update_settings('sticker_steal',{'enabled':True})
        await self.manager.observe(message,self.runtime);self.assertEqual(self.store.entries(),[])

    async def test_blocked_sender_and_private_switch_prevent_download(self):
        self.manager.update_settings('sticker_steal',{'enabled':True,'blockedSenders':'100','allowPrivate':False})
        with patch.object(self.store,'resolve',new=AsyncMock()) as resolve:
            await self.manager.observe(msg(),self.runtime)
            await self.manager.observe(msg(kind='private'),self.runtime);resolve.assert_not_awaited()

    async def test_download_failure_does_not_break_other_observers(self):
        self.manager.update_settings('sticker_steal',{'enabled':True})
        message=msg();message['media']=[{'type':'image','url':'https://example.test/a'}]
        with patch.object(self.store,'resolve',new=AsyncMock(side_effect=OSError())):
            await self.manager.observe(message,self.runtime)
        self.assertEqual(self.store.entries(),[])

    async def test_meaning_attaches_even_if_collection_finishes_after_recognition(self):
        item={'file':'base64://'+base64.b64encode(png()).decode()}
        self.store.record_description(item,'开心')
        entry,_=self.store.put(png());self.store.bind(item,entry['id'])
        self.assertEqual(self.store.entries()[0]['description'],'开心')

    async def test_content_cache_reused_across_different_urls_without_another_model_call(self):
        self.manager.update_settings('sticker_steal',{'enabled':True})
        self.runtime.onebot.call.return_value={}
        image={'data':png(),'ext':'png','width':128,'height':128,'animated':False}
        with patch.object(self.store,'resolve',new=AsyncMock(return_value=image)):
            for index in [1,2]:
                message=msg(str(index));message['media']=[{'type':'image','url':f'https://example.test/{index}.png','sub_type':1}]
                await self.manager.describe_images(message,self.runtime)
                self.assertEqual(message['vision_status'],'ok')
        self.runtime.ai.describe_images.assert_awaited_once()
        self.assertTrue(message['vision_cache_hit'])

class CadenceTests(SuiteFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager.update_settings('sticker_cadence',{'enabled':True})
        self.store.put(png(),'开心')
        self.plugin=self.manager._plugins['sticker_cadence']

    async def test_third_success_attaches_exactly_one_real_image(self):
        for count in range(3):
            self.runtime.reply_meta=None
            await self.plugin.prepare_delivery(msg(), '你好',self.runtime)
            self.assertEqual(bool(self.runtime.reply_meta),count==2)
            self.plugin.on_sent(msg(),'你好',self.runtime)
        self.assertEqual(base64.b64decode(self.runtime.reply_meta['media'][0]['data']['file'][9:]),png())

    async def test_pre_ai_or_failed_send_never_advances_counter(self):
        for _ in range(5):self.plugin.on_pre_ai(msg(),[],self.runtime)
        self.assertEqual(self.plugin._counts,{})
        self.plugin.on_sent(msg(),'hi',self.runtime);self.plugin.on_sent(msg(),'hi',self.runtime)
        await self.plugin.prepare_delivery(msg(),'hi',self.runtime)
        self.runtime.reply_meta=None # failed delivery: no on_sent
        await self.plugin.prepare_delivery(msg(),'hi',self.runtime)
        self.assertTrue(self.runtime.reply_meta)

    async def test_rooms_account_and_private_are_isolated_and_survive_restart(self):
        for _ in range(2):self.plugin.on_sent(msg(),'hi',self.runtime)
        manager=PluginManager(self.root/'plugins');plugin=manager._plugins['sticker_cadence']
        for message in [msg(target='other'),msg(kind='private')]:
            self.runtime.reply_meta=None;await plugin.prepare_delivery(message,'hi',self.runtime);self.assertIsNone(self.runtime.reply_meta)
        self.runtime.reply_meta=None;await plugin.prepare_delivery(msg(),'hi',self.runtime);self.assertTrue(self.runtime.reply_meta)

    async def test_existing_image_is_not_doubled_and_missing_image_stays_text(self):
        for _ in range(2):self.plugin.on_sent(msg(),'hi',self.runtime)
        self.runtime.reply_meta={'media':[{'type':'image','data':{'url':'https://example.test/a'}}]}
        await self.plugin.prepare_delivery(msg(),'hi',self.runtime);self.assertEqual(len(self.runtime.reply_meta['media']),1)
        self.store.remove(self.store.entries()[0]['id']);self.runtime.reply_meta=None
        await self.plugin.prepare_delivery(msg(),'hi',self.runtime);self.assertIsNone(self.runtime.reply_meta)

class ReactionTests(SuiteFixture):
    def notice(self,user=100,sub_type='add',emoji='128077'):
        return onebot_notice({'post_type':'notice','notice_type':'group_msg_emoji_like','group_id':1,'user_id':user,'message_id':42,'time':100,'sub_type':sub_type,'likes':[{'emoji_id':emoji,'count':1}]})

    async def test_echo_uses_same_message_and_emoji_without_model_or_chat(self):
        self.runtime.mode='onebot';self.manager.update_settings('emoji_echo',{'enabled':True,'delayMs':0,'minIntervalMs':0})
        await self.manager.observe(self.notice(),self.runtime);await self.manager.observe(self.notice(),self.runtime)
        self.runtime.onebot.call.assert_awaited_once_with('set_msg_emoji_like',{'message_id':42,'emoji_id':'128077','set':True})
        self.runtime.ai.chat.assert_not_awaited()

    async def test_echo_self_remove_blacklist_and_official_never_trigger(self):
        self.manager.update_settings('emoji_echo',{'enabled':True,'delayMs':0})
        await self.manager.observe(self.notice(),self.runtime)
        self.runtime.mode='onebot'
        await self.manager.observe(self.notice(user=999),self.runtime)
        await self.manager.observe(self.notice(sub_type='remove'),self.runtime)
        self.manager.update_settings('emoji_echo',{'emojiBlacklist':'128077'})
        await self.manager.observe(self.notice(),self.runtime)
        self.runtime.onebot.call.assert_not_awaited()

    async def test_echo_quota_reserved_even_on_failed_call(self):
        self.runtime.mode='onebot';self.manager.update_settings('emoji_echo',{'enabled':True,'delayMs':0,'minIntervalMs':0,'maxPerMinute':1})
        self.runtime.onebot.call.side_effect=ConnectionError()
        await self.manager.observe(self.notice(),self.runtime)
        await self.manager.observe(self.notice(emoji='128514'),self.runtime)
        self.assertEqual(self.runtime.onebot.call.await_count,1)

    async def test_follow_remove_allows_add_again_on_same_message(self):
        self.runtime.mode='onebot'
        self.manager.update_settings('emoji_echo',{'enabled':True,'followRemove':True,'delayMs':0,'minIntervalMs':0})
        for operation in ['add','remove','add']:
            await self.manager.observe(self.notice(sub_type=operation),self.runtime)
        self.assertEqual([c.args[1]['set'] for c in self.runtime.onebot.call.call_args_list],[True,False,True])

    async def test_explicit_reaction_reports_success_only_after_api_success(self):
        self.runtime.mode='onebot';self.manager.update_settings('msg_reaction',{'enabled':True})
        plugin=self.manager._plugins['msg_reaction'];message=msg(mid='42',text='给我点个赞')
        plugin.on_ingest(message,self.runtime)
        self.assertEqual(await plugin.prepare_delivery(message,'placeholder',self.runtime),'贴好啦～')
        self.runtime.onebot.call.side_effect=ConnectionError()
        self.assertIn('没能贴上',await plugin.prepare_delivery(message,'placeholder',self.runtime))
        self.runtime.ai.chat.assert_not_awaited()

    async def test_official_explicit_action_explains_capability(self):
        plugin=self.manager._plugins['msg_reaction']
        self.assertIn('需要普通QQ群聊',plugin.on_ingest(msg(text='给我点个赞'),self.runtime))
        self.runtime.onebot.call.assert_not_awaited()

    async def test_auto_reserves_limits_before_jobs_and_ai_selects_only_pool(self):
        self.runtime.mode='onebot'
        self.manager.update_settings('msg_reaction',{'enabled':True,'probabilityPercent':100,'minGapSec':0,'maxPerHour':1,'delaySec':0,'mode':'ai'})
        plugin=self.manager._plugins['msg_reaction']
        for _ in range(4):plugin.on_sent(msg(mid='42'),'hi',self.runtime)
        self.assertEqual(len(plugin._jobs),1)
        self.runtime.ai.chat.return_value='["unregistered","128077"]'
        await plugin.background(self.runtime)
        self.runtime.onebot.call.assert_awaited_once_with('set_msg_emoji_like',{'message_id':42,'emoji_id':'128077','set':True})

    async def test_bad_notice_does_not_crash_parser(self):
        for likes in [None,{},[None,{'emoji_id':'bad'}]]:
            self.assertIsNone(onebot_notice({'post_type':'notice','notice_type':'group_msg_emoji_like','message_id':42,'group_id':1,'user_id':2,'likes':likes}))

    async def test_disabled_or_stopped_reactions_discard_queued_actions(self):
        self.runtime.mode='onebot'
        self.manager.update_settings('msg_reaction',{'enabled':True,'probabilityPercent':100,'delaySec':0})
        plugin=self.manager._plugins['msg_reaction'];plugin.on_sent(msg(mid='42'),'hi',self.runtime)
        self.assertEqual(len(plugin._jobs),1)
        self.manager.update_settings('msg_reaction',{'enabled':False})
        await self.manager.background(self.runtime);self.assertEqual(plugin._jobs,[])
        self.runtime.onebot.call.assert_not_awaited()

class RuntimeSuiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        config=self.root/'config.qq.yaml';config.write_text(yaml.safe_dump({'ai':{'api_url':'https://example.test','api_key':'fake','model':'m'},'qq':{'app_id':'app','client_secret':'fake'}}),encoding='utf-8')
        self.bot=QQRuntime(root=self.root,config_path=config)
        # 这些用例假定"没点名不进回复队列"；关掉默认开启的随机接话。
        self.bot.plugins.update_settings('ambient_reply',{'enabled':False})
    async def asyncTearDown(self):await self.bot.close();self.tmp.cleanup()

    async def test_unaddressed_group_observation_and_duplicate_event_do_not_add_chat_reply(self):
        self.bot.running=True;message=msg();message.update(at=False,text='[图片]')
        self.bot.ingest(message,'official');self.bot.ingest(dict(message),'official')
        self.assertEqual(self.bot.observations.qsize(),1)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)

    async def test_reaction_notice_never_becomes_ai_reply(self):
        self.bot.config.set('qq_only.mode','onebot');self.bot.onebot.self_id='999';self.bot.running=True
        message=onebot_notice({'post_type':'notice','notice_type':'group_msg_emoji_like','group_id':1,'user_id':2,'message_id':42,'likes':[{'emoji_id':'128077'}]})
        self.bot.ingest(message,'onebot')
        self.assertEqual(self.bot.observations.qsize(),1);self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        removed=dict(message,notice_data=dict(message['notice_data'],set=False))
        self.bot.ingest(removed,'onebot');self.bot.ingest(message,'onebot')
        self.assertEqual(self.bot.observations.qsize(),3)

    async def test_full_worker_failed_first_reply_then_third_success_has_image(self):
        for pid,settings in self.bot.plugins._settings.items():settings['enabled']=pid in {'keyword_reply','image_library','sticker_cadence'}
        self.bot.plugins.update_settings('keyword_reply',{'rules':'hello=你好'})
        self.bot.stickers.put(png(),'开心')
        self.bot.official.authenticated=True;self.bot.running=True;attempts=[]
        async def send(message,reply,meta=None):
            attempts.append(meta)
            if len(attempts)==1:raise httpx.ReadTimeout('uncertain')
            return {'id':'sent'}
        self.bot.official.send=send;self.bot.ai.chat=AsyncMock()
        for i in range(4):self.bot.ingest(msg(str(i),text='hello'),'official')
        task=asyncio.create_task(self.bot._work())
        try:
            for _ in range(100):
                if self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='sent'").fetchone()[0]==3:break
                await asyncio.sleep(.05)
            self.assertEqual(len(attempts),5)
            self.assertIsNone(attempts[2]);self.assertNotIn('media',attempts[3]);self.assertTrue(attempts[4]['media'])
            self.bot.ai.chat.assert_not_awaited()
            self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='uncertain'").fetchone()[0],1)
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)

if __name__=='__main__':unittest.main()
