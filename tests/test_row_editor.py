"""官方机器人/群列表的按行编辑：增删与文本往返。"""
import os
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
import qq_gui  # noqa: E402


class RowEditorTests(unittest.TestCase):
    def test_roundtrip_and_blank_lines(self):
        editor = qq_gui.RowEditor([("备注", "备注"), ("AppID", "id"), ("Secret", "key")])
        editor.set_text('客服|2001|s1\n\n助手|2002|')
        self.assertEqual(editor.to_text(), '客服|2001|s1\n助手|2002|')
        self.assertEqual(len(editor._rows), 2)

    def test_delete_keeps_one_row(self):
        editor = qq_gui.RowEditor([("群号", "群号")])
        editor.set_text('123456')
        editor.remove_row(editor._rows[0])
        self.assertEqual(len(editor._rows), 1, '删空后要留一行可输入')
        self.assertEqual(editor.to_text(), '')

    def test_secret_fields_are_masked(self):
        editor = qq_gui.RowEditor([("备注", "备注"), ("AppID", "id"), ("Secret", "Secret（留空）")])
        modes = [edit.echoMode() for edit in editor._rows[0]['edits']]
        self.assertEqual(modes[-1].name, 'Password')

    def test_groups_collect(self):
        tab = qq_gui.SettingsTab()
        tab.groups.set_text('123456\n789012')
        self.assertEqual(tab.collect()['groups'], ['123456', '789012'])
        tab.official_bots.set_text('客服|2001|sec')
        self.assertEqual(tab.collect()['official_bots_text'], '客服|2001|sec')


if __name__ == '__main__':
    unittest.main()
