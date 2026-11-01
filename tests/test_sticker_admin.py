"""Native UI actions go through the real local API and owned file store."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import yaml
from fastapi.testclient import TestClient
from PyQt6.QtWidgets import QApplication,QFileDialog,QMessageBox
from src.qq_app import QQRuntime,create_app
import src.gui.stickers as stickers_gui
from src.gui.stickers import StickerLibraryDialog
from src.plugin_files import import_plugin_files
from tests.test_sticker_suite import png

class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        config=self.root/'config.qq.yaml';config.write_text(yaml.safe_dump({'ai':{'api_url':'https://example.test','api_key':'fake','model':'m'},'qq':{'app_id':'app','client_secret':'fake'}}),encoding='utf-8')
        self.bot=QQRuntime(root=self.root,config_path=config)
        self.bot.ai.describe_images=AsyncMock(return_value='开心地笑，适合表示喜悦')
        self.client=TestClient(create_app(self.bot));self.client.__enter__()
        self.source=self.root/'开心.png';self.source.write_bytes(png())
    def tearDown(self):self.client.__exit__(None,None,None);self.tmp.cleanup()
    def get(self,path,timeout=3):return self.client.get('/api/'+path).json()
    def post(self,path,data,timeout=10):return self.client.post('/api/'+path,json=data).json()
    def delete(self,path,timeout=15):return self.client.delete('/api/'+path).json()
    def imports(self):
        return import_plugin_files(self.root/'data/plugin_uploads','sticker_admin','imports',[self.source],{'file_picker':'files','file_extensions':['.png'],'file_max_mb':6})

    def test_upload_list_thumbnail_edit_search_recognize_and_delete_native_flow(self):
        dialog=StickerLibraryDialog(self.root,self.get,self.post,self.delete)
        with patch.object(stickers_gui.file_dialogs,'open_files',return_value=([str(self.source)],'')):
            dialog.upload_button.click()
        self.assertEqual(dialog.table.rowCount(),1)
        self.assertFalse(dialog.table.cellWidget(0,0).pixmap().isNull())
        dialog.table.selectRow(0);self.qt.processEvents()
        dialog.label.setText('喜悦');dialog.description.setPlainText('开心大笑')
        dialog.save_button.click();self.assertEqual(self.bot.stickers.entries()[0]['label'],'喜悦')
        dialog.table.selectRow(0);dialog.recognize_button.click()
        self.assertIn('喜悦',self.bot.stickers.entries()[0]['description'])
        self.bot.ai.describe_images.assert_awaited_once()
        dialog.search.setText('喜悦');dialog.search_now();self.assertEqual(dialog.table.rowCount(),1)
        dialog.table.selectRow(0)
        with patch.object(stickers_gui,'ask_confirm',return_value=True):dialog.delete_button.click()
        self.assertEqual(dialog.table.rowCount(),0);self.assertTrue(self.source.is_file())
        dialog.close()

    def test_invalid_update_does_not_change_saved_label(self):
        self.assertTrue(self.post('stickers/import',{'items':self.imports()})['success'])
        entry=self.bot.stickers.entries()[0]
        response=self.client.post('/api/stickers/'+entry['id'],json={'label':'[invalid]','description':'x'})
        self.assertEqual(response.status_code,400);self.assertEqual(self.bot.stickers.entries()[0]['label'],'开心')

    def test_arbitrary_local_path_cannot_be_imported_via_api(self):
        response=self.client.post('/api/stickers/import',json={'items':[{'path':str(self.source),'label':'x'}]})
        self.assertEqual(response.status_code,400);self.assertEqual(self.bot.stickers.entries(),[])

    def test_invalid_second_file_validates_whole_batch_before_adding(self):
        items=self.imports();bad=Path(items[0]['path']).with_name('bad.png');bad.write_bytes(b'not a picture')
        response=self.client.post('/api/stickers/import',json={'items':items+[{'path':str(bad),'label':'坏图'}]})
        self.assertEqual(response.status_code,400);self.assertEqual(self.bot.stickers.entries(),[])

    def test_management_disabled_and_cross_origin_guard(self):
        self.assertEqual(self.client.get('/api/stickers',headers={'Origin':'https://outside.example'}).status_code,403)
        self.assertEqual(self.client.post('/api/stickers/import',json={'items':self.imports()},headers={'Origin':'https://outside.example'}).status_code,403)
        self.bot.plugins.update_settings('sticker_admin',{'enabled':False})
        self.assertEqual(self.client.get('/api/stickers').status_code,403)

    def test_failed_vision_does_not_erase_manual_description(self):
        entry,_=self.bot.stickers.put(png(),'开心','原有含义')
        self.bot.ai.describe_images.return_value=''
        self.assertEqual(self.client.post('/api/stickers/'+entry['id']+'/recognize').status_code,400)
        self.assertEqual(self.bot.stickers.entries()[0]['description'],'原有含义')

    def test_reaction_capability_changes_with_qq_mode(self):
        for mode,available in [('official',False),('onebot',True)]:
            self.bot.config.set('qq_only.mode',mode)
            plugins=self.client.get('/api/plugins').json()
            self.assertEqual(next(p for p in plugins if p['id']=='msg_reaction')['available'],available)

if __name__=='__main__':unittest.main()
