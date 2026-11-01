import copy,os,unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication,QPushButton
import qq_gui as gui
from src.plugins.chat_controls import ChatControls
APP=QApplication.instance() or QApplication([])

class WakeupSettingsTests(unittest.TestCase):
    def make(self,**values):
        meta=ChatControls.META
        data={'id':meta.id,'name':meta.name,'description':meta.description,'schema':meta.schema_dict(),
              'defaults':meta.default_settings,'settings':dict(meta.default_settings,**values)}
        before=copy.deepcopy(data);dialog=gui.PluginConfigDialog(data);self.addCleanup(dialog.deleteLater)
        self.assertEqual(data,before);return dialog
    def test_defaults_and_old_aliases_preserved(self):
        for aliases in ('','鲸鱼娘,小鲸鱼','专用称呼\n第二称呼'):
            d=self.make(aliases=aliases);self.assertEqual(d.values(),dict(ChatControls.META.default_settings,aliases=aliases))
    def test_examples_are_draft_only_and_keep_custom_names(self):
        d=self.make(aliases='自定义,小鲸鱼')
        with patch.object(gui,'api_post') as save:
            b=d.findChild(QPushButton,'wakeup_examples');b.click();b.click();save.assert_not_called()
        self.assertEqual(d.values()['aliases'].count('小鲸鱼'),1);self.assertIn('自定义',d.values()['aliases'])
    def test_summary_explains_fallback_and_followup(self):
        d=self.make(aliases='鲸');self.assertIn('没有有效',d.wakeup_summary.text());self.assertIn('跟聊',d.wakeup_summary.text())
        d._widgets['requireAt'][0].setChecked(False);self.assertIn('任何群消息',d.wakeup_summary.text())
        d._widgets['enabled'][0].setChecked(False);self.assertIn('内置触发',d.wakeup_summary.text())
    def test_save_payload_remains_complete_and_canonical(self):
        d=self.make(aliases='专用称呼')
        with patch.object(gui,'api_post',return_value={'success':True}) as save:
            d._save();self.assertEqual(save.call_args.args,('plugins/chat_controls',d.values()))
        self.assertTrue(d.saved)
    def test_reset_does_not_save_and_small_window_has_save_button(self):
        d=self.make(aliases='专用称呼')
        with patch.object(gui,'api_post') as save:d._reset();save.assert_not_called()
        self.assertEqual(d.values(),ChatControls.META.default_settings)
        d.resize(600,440);d.show();APP.processEvents()
        b=next(b for b in d.findChildren(QPushButton) if b.text()=='保存设置')
        self.assertTrue(d.rect().contains(b.mapTo(d,b.rect().bottomRight())));d.close()
    def test_shortcut_opens_real_existing_plugin(self):
        tab=gui.PluginTab();self.addCleanup(tab.deleteLater);tab._plugins=[{'id':'chat_controls'}]
        with patch.object(tab,'_configure') as configure:
            tab.btn_wakeup.click();configure.assert_called_once_with({'id':'chat_controls'})
