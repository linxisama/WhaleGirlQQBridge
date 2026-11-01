"""Behavioral coverage for local gallery pictures on both QQ transports."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from src.plugins.manager import PluginManager
from src.qq_transports import OfficialQQ

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')

class GalleryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.image=self.root/'happy.png'
        self.image.write_bytes(PNG)
        self.manager=PluginManager(self.root/'plugins')
        self.plugin=self.manager._plugins['image_library']
        self.runtime=SimpleNamespace(mode='official')
        self.manager.update_settings('image_library',{'library':f'开心 | {self.image}'})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_official_command_attaches_registered_local_image(self):
        message={'text':'/表情 开心'}
        self.assertEqual(self.plugin.on_ingest(message,self.runtime),'给你～')
        file=message['_reply_meta']['media'][0]['data']['file']
        self.assertTrue(file.startswith('base64://'))
        self.assertEqual(base64.b64decode(file[9:]),PNG)

    async def test_official_model_selection_attaches_same_local_image(self):
        hint=self.plugin.on_pre_ai({},[],self.runtime)['extra_system']
        self.assertIn('开心',hint)
        self.assertIn('[[表情:标签]]',hint)
        text,meta=await self.plugin.on_post_reply({},'给你一张～ [[表情:开心]]',self.runtime)
        self.assertEqual(text,'给你一张～')
        self.assertEqual(base64.b64decode(meta['media'][0]['data']['file'][9:]),PNG)

    async def test_onebot_local_image_remains_supported(self):
        self.runtime.mode='onebot'
        self.assertEqual(self.plugin.media_for('开心',self.runtime)['data']['file'],'base64://'+base64.b64encode(PNG).decode())

    async def test_deleted_local_image_not_advertised_and_cannot_fake_sent(self):
        self.image.unlink()
        hint=self.plugin.on_pre_ai({},[],self.runtime)['extra_system']
        self.assertIn('没有可发送',hint)
        reply=await self.plugin.on_post_reply({},'送你这张表情包 [[表情:开心]]',self.runtime)
        self.assertIn('本次没有发出图片',reply)
        self.assertNotIn('送你这张',reply)

    async def test_empty_gallery_explains_text_only(self):
        self.manager.update_settings('image_library',{'library':''})
        self.assertIn('不要承诺',self.plugin.on_pre_ai({},[],self.runtime)['extra_system'])

    async def test_auto_off_preserves_explicit_image_command(self):
        self.manager.update_settings('image_library',{'autoSelect':False})
        self.assertIn('自动配图已关闭',self.plugin.on_pre_ai({},[],self.runtime)['extra_system'])
        msg={'text':'/表情 开心'}
        self.assertEqual(self.plugin.on_ingest(msg,self.runtime),'给你～')
        self.assertTrue(msg['_reply_meta']['media'])

    async def test_unknown_label_does_not_read_arbitrary_path(self):
        with self.assertRaises(ValueError):
            self.plugin.media_for(str(self.image),self.runtime)
        self.assertIn('没有发出图片',await self.plugin.on_post_reply({},'[[表情:unknown]]',self.runtime))

    async def test_invalid_local_contents_rejected(self):
        self.image.write_bytes(b'not an image')
        self.assertIn('不是受支持',self.plugin.on_ingest({'text':'/表情 开心'},self.runtime))

class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_and_private_upload_binary_without_local_path(self):
        for kind,part in [('group','groups'),('private','users')]:
            with self.subTest(kind=kind):
                await self.check_upload(kind,part)

    async def check_upload(self,kind,part):
        bridge=OfficialQQ('app','secret')
        await bridge.http.aclose()
        bridge.authorize=AsyncMock(return_value='token')
        calls=[]
        def respond(request):
            calls.append((request.url.path,json.loads(request.content)))
            return httpx.Response(200,json={'file_info':'uploaded'} if request.url.path.endswith('/files') else {'id':'sent'})
        bridge.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            await bridge.send({'kind':kind,'target':'target','id':'incoming'},'给你～',{'media':[{'type':'image','data':{'file':'base64://'+base64.b64encode(PNG).decode()}}]})
            self.assertEqual(calls[0],(f'/v2/{part}/target/files',{'file_type':1,'srv_send_msg':False,'file_data':base64.b64encode(PNG).decode()}))
            self.assertEqual(calls[1][1]['media'],{'file_info':'uploaded'})
            self.assertEqual(calls[1][1]['msg_type'],7)
            self.assertEqual(calls[1][1]['msg_seq'],1)
            self.assertEqual(calls[1][1]['msg_id'],'incoming')
        finally:
            await bridge.http.aclose()

    async def test_upload_token_refresh_keeps_same_binary_payload(self):
        bridge=OfficialQQ('app','secret')
        await bridge.http.aclose()
        bridge.authorize=AsyncMock(return_value='token')
        payloads=[]
        def respond(request):
            payloads.append(json.loads(request.content))
            if len(payloads)==1:
                return httpx.Response(401,json={})
            return httpx.Response(200,json={'file_info':'uploaded'})
        bridge.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            await bridge.send({'kind':'group','target':'target','id':'incoming'},'hi',{'media':[{'type':'image','data':{'file':'base64://'+base64.b64encode(PNG).decode()}}]})
            self.assertEqual(payloads[0],payloads[1])
            self.assertEqual(len(payloads),3)
        finally:
            await bridge.http.aclose()

    async def test_invalid_inline_image_makes_no_api_call(self):
        bridge=OfficialQQ('app','secret')
        await bridge.http.aclose()
        bridge.http=SimpleNamespace(post=AsyncMock())
        try:
            with self.assertRaises(ValueError):
                await bridge.send({'kind':'group','target':'target','id':'incoming'},'hi',{'media':[{'type':'image','data':{'file':'base64://bm90IGltYWdl'}}]})
            bridge.http.post.assert_not_awaited()
        finally:
            bridge.http=SimpleNamespace()
