"""Offline local-import and generated configuration UI regressions."""
import base64
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication,QTextEdit
import qq_gui
from src.plugin_files import import_plugin_files,merge_uploaded_files
from src.plugins import FieldSchema,PluginMeta
from src.plugins.manager import PluginManager

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')
SPEC={'file_picker':'labeled_files','file_extensions':['.png','.jpg','.gif','.webp'],'file_max_mb':6}


class FileImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.file=self.root/'开心.png'
        self.file.write_bytes(PNG)
        self.managed=self.root/'data/plugin_uploads'

    def upload(self,paths=None,spec=SPEC):
        return import_plugin_files(self.managed,'image_library','library',paths or [self.file],spec)

    def test_copy_survives_original_removal_and_onebot_can_send_it(self):
        result=self.upload()
        copied=Path(result[0]['path'])
        self.assertTrue(copied.is_relative_to(self.managed))
        self.file.unlink()
        self.assertEqual(copied.read_bytes(),PNG)
        manager=PluginManager(self.root/'plugins')
        manager.update_settings('image_library',{'library':merge_uploaded_files('',result,'labeled_files')})
        media=manager._plugins['image_library'].media_for('开心',SimpleNamespace(mode='onebot'))
        self.assertEqual(base64.b64decode(media['data']['file'][9:]),PNG)

    def test_same_content_is_not_copied_or_registered_twice(self):
        value=merge_uploaded_files('',self.upload(),'labeled_files')
        self.assertEqual(merge_uploaded_files(value,self.upload(),'labeled_files'),value)
        self.assertEqual(len(list(self.managed.rglob('*.png'))),1)

    def test_duplicate_labels_and_existing_entries_are_preserved(self):
        value='开心 | https://example.test/old.png\n# 我的图库'
        result=merge_uploaded_files(value,[{'label':'开心','path':'C:/new.png'},
            {'label':'[特别]|图片','path':'C:/other.png'}],'labeled_files')
        self.assertTrue(result.startswith(value))
        self.assertIn('开心_2 | C:/new.png',result)
        self.assertIn('_特别__图片 | C:/other.png',result)

    def test_bad_picture_in_batch_prevents_all_copies(self):
        bad=self.root/'坏图.png'
        bad.write_text('not a picture')
        with self.assertRaises(ValueError):
            self.upload([self.file,bad])
        self.assertFalse(self.managed.exists())

    def test_wrong_type_oversize_and_scope_escape_are_rejected(self):
        text=self.root/'test.txt'
        text.write_text('hello')
        with self.assertRaises(ValueError):
            self.upload([text])
        large=self.root/'big.png'
        large.write_bytes(PNG+b'0'*(1024*1024))
        with self.assertRaises(ValueError):
            self.upload([large],dict(SPEC,file_max_mb=1))
        for pid,field in [('../escape','f'),('p','../../escape')]:
            with self.assertRaises(ValueError):
                import_plugin_files(self.managed,pid,field,[self.file],SPEC)
        self.assertFalse(self.managed.exists())

    def test_generic_single_and_multiple_file_fields(self):
        text=self.root/'words.txt'
        text.write_text('hello',encoding='utf-8')
        spec={'file_picker':'file','file_extensions':['.txt'],'file_max_mb':1}
        result=self.upload([text],spec)
        self.assertEqual(merge_uploaded_files('old',result,'file'),result[0]['path'])
        self.assertEqual(merge_uploaded_files('',result,'files'),result[0]['path'])
        with self.assertRaises(ValueError):
            self.upload([text,text],spec)

    def test_schema_serializes_capability_without_sharing_mutable_extensions(self):
        meta=PluginMeta(id='p',name='P',description='',config_schema={
            'path':FieldSchema(type='string',label='文件',file_picker='file',file_extensions=['.txt']),
            'normal':FieldSchema(type='string',label='普通文本')})
        schema=meta.schema_dict()
        self.assertEqual(schema['path']['file_picker'],'file')
        schema['path']['file_extensions'].append('.exe')
        self.assertEqual(meta.config_schema['path'].file_extensions,['.txt'])
        self.assertNotIn('file_picker',schema['normal'])


class FileConfigGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.file=self.root/'微笑.png'
        self.file.write_bytes(PNG)
        self.manager=PluginManager(self.root/'plugins')
        root_patch=patch.object(qq_gui,'ROOT',self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def dialog(self,pid='image_library'):
        dialog=qq_gui.PluginConfigDialog(next(p for p in self.manager.list_plugins() if p['id']==pid))
        self.addCleanup(dialog.close)
        return dialog

    def select(self,dialog):
        with patch.object(qq_gui.file_dialogs,'open_files',return_value=([str(self.file)],'')):
            dialog._file_buttons['library'].click()

    def test_button_imports_and_save_posts_only_settings(self):
        dialog=self.dialog()
        self.assertEqual(dialog._file_buttons['library'].text(),'上传图片…')
        self.select(dialog)
        value=dialog.values()['library']
        self.assertTrue(value.startswith('微笑 | '))
        self.assertTrue(Path(value.partition('|')[2].strip()).is_file())
        with patch.object(qq_gui,'api_post',return_value={'success':True}) as posted:
            dialog._save()
        payload=posted.call_args.args[1]
        self.assertEqual(set(payload),set(self.manager.get_settings('image_library')))
        self.assertEqual(payload['library'],value)
        self.assertTrue(dialog.saved)

    def test_picker_cancel_and_form_cancel_leave_configuration_unchanged(self):
        dialog=self.dialog()
        initial=dialog.values()
        with patch.object(qq_gui.file_dialogs,'open_files',return_value=([],'')):
            dialog._file_buttons['library'].click()
        self.assertEqual(dialog.values(),initial)
        with patch.object(qq_gui,'api_post') as posted:
            self.select(dialog)
            dialog.reject()
        posted.assert_not_called()
        self.assertEqual(self.manager.get_settings('image_library')['library'],'')

    def test_bad_file_warns_without_changing_editor(self):
        self.file.write_text('bad')
        dialog=self.dialog()
        with patch.object(qq_gui,'tell') as warned:
            self.select(dialog)
        warned.assert_called_once()
        self.assertEqual(dialog.values()['library'],'')

    def test_directory_uses_folder_picker(self):
        dialog=self.dialog('sticker_cap')
        self.assertEqual(dialog._file_buttons['stickerDir'].text(),'选择文件夹…')
        with patch.object(qq_gui.file_dialogs,'choose_dir',return_value=str(self.root)):
            dialog._file_buttons['stickerDir'].click()
        self.assertEqual(dialog.values()['stickerDir'],str(self.root.resolve()))

    def test_future_single_multiple_and_labeled_fields_get_upload_buttons(self):
        for mode in ['file','files','labeled_files']:
            meta={'id':'future_plugin','schema':{'asset':dict(type='string',label='本地文件',**dict(SPEC,file_picker=mode))},
                  'settings':{'asset':''},'defaults':{'asset':''}}
            dialog=qq_gui.PluginConfigDialog(meta)
            self.addCleanup(dialog.close)
            picker=patch.object(qq_gui.file_dialogs,'open_file',return_value=(str(self.file),'')) if mode=='file' else patch.object(qq_gui.file_dialogs,'open_files',return_value=([str(self.file)],''))
            with picker:
                dialog._file_buttons['asset'].click()
            self.assertTrue(dialog.values()['asset'])
            if mode!='file':
                self.assertIsInstance(dialog._widgets['asset'][0],QTextEdit)
            dialog._reset()
            self.assertEqual(dialog.values()['asset'],'')


if __name__=='__main__':
    unittest.main()
