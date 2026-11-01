import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,AsyncMock,patch
from src.meme_service import catalog,find_template,generate
from src.plugins.memes import Memes,parse_command


def png():
    from PyQt6.QtCore import QBuffer,QIODevice
    from PyQt6.QtGui import QImage
    image=QImage(100,100,QImage.Format.Format_RGB32);image.fill(0xff336699)
    out=QBuffer();out.open(QIODevice.OpenModeFlag.WriteOnly);image.save(out,'PNG')
    return bytes(out.data())


class CatalogTests(unittest.TestCase):
    def test_pinned_catalog_has_real_templates_and_input_limits(self):
        self.assertEqual(len(catalog()),300)
        self.assertEqual(find_template('petpet')['min_images'],1)
        self.assertEqual(find_template('5000choyen')['min_texts'],2)

    def test_commands_and_regular_chat_are_distinct(self):
        self.assertIsNone(parse_command('你好今天怎么样'))
        self.assertEqual(parse_command('/表情制作 5000choyen "你好 世界" 你好'),('make',['5000choyen','你好 世界','你好']))
        self.assertEqual(parse_command('@某人 /表情列表 2'),('list',['2']))
        self.assertEqual(parse_command('petpet')[0],'make')
        self.assertIsNone(parse_command('petpet',False))


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        manager=SimpleNamespace(state_dir=Path(self.tmp.name),get_settings=lambda _:dict(Memes.META.default_settings,cooldownSec=0))
        self.plugin=Memes(manager)
        self.runtime=SimpleNamespace(mode='official',note=Mock())

    async def test_list_is_local_and_paginates(self):
        message={'text':'/表情列表 2'}
        with patch('src.plugins.memes.generate',new_callable=AsyncMock) as made:
            await self.plugin.on_prepare(message,self.runtime)
        self.assertIn('第 2/15 页',message['_direct_reply']);made.assert_not_awaited()

    async def test_missing_images_does_not_call_native_engine(self):
        message={'text':'/表情制作 petpet','media':[]}
        with patch('src.plugins.memes.generate',new_callable=AsyncMock) as made:
            await self.plugin.on_prepare(message,self.runtime)
        self.assertIn('需要 1',message['_direct_reply']);self.assertNotIn('_reply_meta',message);made.assert_not_awaited()

    async def test_disabled_template_is_not_generated(self):
        self.plugin._manager.get_settings=lambda _:dict(Memes.META.default_settings,disabledTemplates='petpet')
        message={'text':'/表情制作 petpet'}
        await self.plugin.on_prepare(message,self.runtime)
        self.assertIn('禁用',message['_direct_reply'])

    async def test_success_has_sendable_media_and_counts_only_after_sent(self):
        raw=png();message={'text':'/表情制作 5000choyen 鲸鱼娘 你好','target':'one','sender_id':'u'}
        with patch('src.plugins.memes.generate',new_callable=AsyncMock,return_value=raw):
            await self.plugin.on_prepare(message,self.runtime)
        data=message['_reply_meta']['media'][0]['data']['file']
        self.assertEqual(base64.b64decode(data.removeprefix('base64://')),raw)
        self.assertEqual(self.plugin.load_state('usage.json',{}),{})
        self.plugin.on_sent(message,'做好啦',self.runtime)
        self.assertEqual(self.plugin.load_state('usage.json',{})['5000choyen'],1)

    async def test_engine_failure_never_claims_image_was_generated(self):
        message={'text':'/表情制作 5000choyen 你好 世界'}
        with patch('src.plugins.memes.generate',new_callable=AsyncMock,side_effect=ValueError('引擎退出')):
            await self.plugin.on_prepare(message,self.runtime)
        self.assertNotIn('_reply_meta',message);self.assertNotIn('做好啦',message['_direct_reply'])


class RealEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_text_png_and_image_gif_in_isolated_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw=await generate(Path(tmp),'5000choyen',[],['鲸鱼娘','你好'],download=False)
            self.assertTrue(raw.startswith(b'\x89PNG'))
            raw=await generate(Path(tmp),'petpet',[png()],[])
            self.assertTrue(raw.startswith(b'GIF'))
            self.assertEqual(list(Path(tmp).glob('job-*')),[])

    async def test_timeout_terminates_child_and_cleans_input_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(asyncio.TimeoutError):
                await generate(Path(tmp),'5000choyen',[],['你好','世界'],timeout=.001,download=False)
            self.assertEqual(list(Path(tmp).glob('job-*')),[])

    async def test_native_crash_is_reported_without_stopping_host(self):
        process=SimpleNamespace(returncode=139,wait=AsyncMock(return_value=139))
        with tempfile.TemporaryDirectory() as tmp,patch('src.meme_service.asyncio.create_subprocess_exec',new_callable=AsyncMock,return_value=process):
            with self.assertRaisesRegex(ValueError,'未正常返回'):
                await generate(Path(tmp),'petpet',[],[])
        self.assertTrue(True)
