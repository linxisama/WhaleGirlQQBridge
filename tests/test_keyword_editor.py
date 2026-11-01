"""Behavioral tests for easy keyword entry and compatible matching."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication
import qq_gui
from src.keyword_rules import read_rule_rows,write_rule_rows
from src.plugins.manager import PluginManager


class CodecTests(unittest.TestCase):
    def test_old_format_and_equals_in_reply_round_trip(self):
        value='你好=你好呀\nHelp=说明：a=b'
        self.assertEqual(write_rule_rows(read_rule_rows(value)),value)

    def test_multiline_and_equals_in_keyword_are_lossless(self):
        rows=[('x=y','第一行\n第二行'),('你好','你好呀')]
        self.assertEqual(read_rule_rows(write_rule_rows(rows)),rows)

    def test_blank_rows_are_ignored_but_incomplete_and_duplicate_rows_fail(self):
        self.assertEqual(write_rule_rows([('','')]),'')
        for rows in [[('你好','')],[('','回复')],[('HELLO','1'),('hello','2')]]:
            with self.assertRaises(ValueError):
                write_rule_rows(rows)

    def test_malformed_legacy_lines_are_visible_for_correction(self):
        self.assertEqual(read_rule_rows('缺少回复\n=缺少关键词'),[('缺少回复',''),('','缺少关键词')])

    def test_bad_structured_data_raises_instead_of_silently_discarding(self):
        for raw in ['{"format":"keyword_reply/v1","rules":','{"format":"keyword_reply/v1","rules":[{"keyword":1,"reply":"x"}]}']:
            with self.assertRaises(ValueError):
                read_rule_rows(raw)


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager=PluginManager(Path(self.tmp.name))

    def dialog(self):
        meta=next(p for p in self.manager.list_plugins() if p['id']=='keyword_reply')
        dialog=qq_gui.PluginConfigDialog(meta)
        self.addCleanup(dialog.close)
        return dialog,dialog._widgets['rules'][0]

    def test_empty_editor_uses_placeholders_without_creating_rules(self):
        dialog,editor=self.dialog()
        self.assertTrue(editor.table.cellWidget(0,0).placeholderText())
        self.assertEqual(dialog.values()['rules'],'')

    def test_add_delete_reorder_and_examples_preserve_current_input(self):
        _,editor=self.dialog()
        editor.set_rows([('早上好','早呀'),('晚安','晚安呀')])
        editor.add_button.click()
        self.assertEqual(editor.table.rowCount(),3)
        editor.remove_row(2)
        editor.move_row(1,-1)
        self.assertEqual(editor.rows()[0],('晚安','晚安呀'))
        editor.example_button.click()
        editor.example_button.click()
        self.assertEqual([key for key,_ in editor.rows()].count('你好'),1)
        self.assertIn(('早上好','早呀'),editor.rows())

    def test_old_rules_expand_and_reset_restores_default(self):
        self.manager.update_settings('keyword_reply',{'rules':'你好=hi\n谢谢=不客气'})
        dialog,editor=self.dialog()
        self.assertEqual(editor.rows(),[('你好','hi'),('谢谢','不客气')])
        dialog._reset()
        self.assertEqual(dialog.values()['rules'],'')

    def test_incomplete_row_blocks_save_and_identifies_row(self):
        dialog,editor=self.dialog()
        editor.set_rows([('你好','hi'),('晚安','')])
        with patch.object(qq_gui,'api_post') as posted,patch.object(qq_gui,'tell') as warned:
            dialog._save()
        posted.assert_not_called()
        self.assertIn('第 2 条',warned.call_args.args[2])
        self.assertFalse(dialog.saved)

    def test_saved_multiline_rule_reloads_and_matches_without_ai(self):
        dialog,editor=self.dialog()
        editor.set_rows([('Hello','第一行\n第二行')])
        dialog._widgets['enabled'][0].setChecked(True)
        def post(path,payload):
            self.assertEqual(set(payload),{'enabled','rules'})
            self.manager.update_settings('keyword_reply',payload)
            return {'success':True}
        with patch.object(qq_gui,'api_post',side_effect=post):
            dialog._save()
        self.assertTrue(dialog.saved)
        reloaded=PluginManager(Path(self.tmp.name))._plugins['keyword_reply']
        self.assertEqual(reloaded.on_ingest({'text':'say HELLO'},None),'第一行\n第二行')

    def test_runtime_preserves_first_match_and_disabled_behavior(self):
        self.manager.update_settings('keyword_reply',{'enabled':True,'rules':write_rule_rows([('你好','第一条'),('好','第二条')])})
        plugin=self.manager._plugins['keyword_reply']
        self.assertEqual(plugin.on_ingest({'text':'你好啊'},None),'第一条')
        self.manager.update_settings('keyword_reply',{'enabled':False})
        self.assertIsNone(plugin.on_ingest({'text':'你好啊'},None))

    def test_backend_rejects_invalid_structured_rules_and_preserves_previous(self):
        old=self.manager.get_settings('keyword_reply')
        raw=json.dumps({'format':'keyword_reply/v1','rules':[{'keyword':'你好','reply':''}]},ensure_ascii=False,separators=(',',':'))
        with self.assertRaises(ValueError):
            self.manager.update_settings('keyword_reply',{'rules':raw})
        self.assertEqual(self.manager.get_settings('keyword_reply'),old)

    def test_corrupt_stored_rules_do_not_crash_editor_or_disappear(self):
        dialog,editor=self.dialog()
        raw='{"format":"keyword_reply/v1","rules":'
        editor.set_rules(raw)
        self.assertEqual(editor.rows()[0][0],raw)
        with self.assertRaises(ValueError):
            dialog.values()


if __name__=='__main__':
    unittest.main()
