"""普通端能力入口的 GUI 测试：撤回 / 群资料 / 成员列表都走白名单动作接口。"""
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import qq_gui  # noqa: E402


class OneBotActionUiTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.tab = qq_gui.ConsoleTab()
        self.addCleanup(self.tab.close)

    def _post(self, result):
        def fake_post(path, payload=None, *args, **kwargs):
            self.calls.append((path, payload))
            return result
        return fake_post

    def test_recall_sends_delete_msg(self):
        with patch.object(qq_gui, 'api_post', self._post({'success': True, 'data': {}})), \
             patch.object(qq_gui, 'ask_text', return_value=('12345', True)):
            self.tab.recall_message()
        self.assertEqual(self.calls[0][0], 'onebot/action')
        self.assertEqual(self.calls[0][1]['action'], 'delete_msg')
        self.assertEqual(self.calls[0][1]['params'], {'message_id': '12345'})

    def test_group_info_uses_int_group_id(self):
        with patch.object(qq_gui, 'api_post', self._post({'success': True, 'data': {'group_name': '测试群'}})), \
             patch.object(qq_gui, 'ask_text', return_value=('555', True)), \
             patch.object(qq_gui, 'tell') as info:
            self.tab.show_group_info()
        self.assertEqual(self.calls[0][1]['action'], 'get_group_info')
        self.assertEqual(self.calls[0][1]['params']['group_id'], 555)
        info.assert_called_once()

    def test_failure_shows_reason(self):
        with patch.object(qq_gui, 'api_post', self._post({'success': False, 'message': '不在白名单'})), \
             patch.object(qq_gui, 'ask_text', return_value=('1', True)), \
             patch.object(qq_gui, 'tell') as warn:
            self.tab.recall_message()
        warn.assert_called_once()
        self.assertIn('不在白名单', str(warn.call_args))

    def test_cancel_does_nothing(self):
        with patch.object(qq_gui, 'api_post') as post, \
             patch.object(qq_gui, 'ask_text', return_value=('', False)):
            self.tab.recall_message()
        post.assert_not_called()




class MediaUiTests(unittest.TestCase):
    def setUp(self):
        self.tab = qq_gui.ConsoleTab()
        self.addCleanup(self.tab.close)


    def test_send_text_to_group_uses_group_action(self):
        calls = []
        def fake_post(path, payload=None, *a, **k):
            calls.append((path, payload))
            return {'success': True, 'data': {'message_id': 1}}
        answers = iter([('987654', True), ('你好', True)])
        with patch.object(qq_gui, 'api_post', fake_post), \
             patch.object(qq_gui, 'ask_text', side_effect=lambda *a, **k: next(answers)):
            self.tab.send_text_message()
        self.assertEqual(calls[0][1]['action'], 'send_group_msg')
        self.assertEqual(calls[0][1]['params'], {'group_id': 987654, 'message': '你好'})

    def test_send_image_posts_media(self):
        calls = []
        def fake_post(path, payload=None, *args, **kwargs):
            calls.append((path, payload))
            return {'success': True}
        with patch.object(qq_gui, 'api_post', fake_post), \
             patch.object(qq_gui.file_dialogs, 'open_file', return_value=('C:/x/a.png', '')), \
             patch.object(qq_gui, 'ask_text', return_value=('123456', True)):
            self.tab.send_media('image')
        self.assertEqual(calls[0][0], 'media/send')
        self.assertEqual(calls[0][1]['kind'], 'image')
        self.assertEqual(calls[0][1]['target_type'], 'group')
        self.assertEqual(calls[0][1]['target_id'], '123456')

    def test_failure_shows_message(self):
        with patch.object(qq_gui, 'api_post', lambda *a, **k: {'success': False, 'message': '文件太大'}), \
             patch.object(qq_gui.file_dialogs, 'open_file', return_value=('C:/x/a.png', '')), \
             patch.object(qq_gui, 'ask_text', return_value=('123456', True)), \
             patch.object(qq_gui, 'tell') as warn:
            self.tab.send_media('file')
        warn.assert_called_once()
        self.assertIn('文件太大', str(warn.call_args))


if __name__ == '__main__':
    unittest.main()
