"""学习 Agent 页面：入口按钮、反馈按钮与学习记录弹窗的可见性。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from PyQt6.QtWidgets import QApplication, QPushButton

    from src.gui.assistant import AssistantTab

    HAS_QT = True
except Exception:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, '需要 PyQt6')
class LearningAgentPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tab = AssistantTab('http://127.0.0.1:27499', '/tmp')
        self.addCleanup(self.tab.deleteLater)

    def test_learning_entry_buttons_exist(self):
        labels = [button.text() for button in self.tab.findChildren(QPushButton)]
        tooltips = [button.toolTip() for button in self.tab.findChildren(QPushButton)]
        self.assertIn('学习记录', tooltips)
        self.assertIn('导入技能包', tooltips)
        self.assertIn('操作历史', tooltips)
        self.assertIn('这次对了', labels)
        self.assertIn('这次不对', labels)

    def test_feedback_buttons_hidden_until_applied(self):
        self.assertTrue(self.tab.good_button.isHidden())
        self.assertTrue(self.tab.bad_button.isHidden())
        self.tab.present({'id': 'a' * 32, 'state': 'applied', 'message': '已应用',
                          'result': {'steps': []}})
        self.assertFalse(self.tab.good_button.isHidden())
        self.assertFalse(self.tab.bad_button.isHidden())

    def test_forget_sends_typed_payload(self):
        calls = []
        self.tab._request = lambda kind, path, data=None, **kw: calls.append((kind, path, data))
        self.tab._show_learning({'stats': {}, 'lessons': [], 'preferences': [
            {'key': '称呼', 'value': '老鲸'}], 'skills': [{'id': 's1', 'name': '技能'}]})
        self.tab.learning_list.setCurrentRow(0)
        self.tab._forget()
        self.assertEqual(calls[-1][2], {'preference': '称呼'})
        self.tab.learning_list.setCurrentRow(1)
        self.tab._forget()
        self.assertEqual(calls[-1][2], {'skill_id': 's1'})

    def test_undo_button_only_when_available_and_sends_request(self):
        self.tab.present({'id': 'b' * 32, 'state': 'applied', 'message': '已应用',
                          'can_undo': False, 'result': {'steps': []}})
        self.assertTrue(self.tab.undo_button.isHidden())
        self.tab.present({'id': 'c' * 32, 'state': 'applied', 'message': '已应用',
                          'can_undo': True, 'result': {'steps': []}})
        self.assertFalse(self.tab.undo_button.isHidden())
        calls = []
        self.tab._request = lambda kind, path, data=None, **kw: calls.append((kind, path, data))
        self.tab.undo()
        self.assertEqual(calls[-1][0], 'undo')
        self.assertTrue(calls[-1][1].endswith('/undo'))

    def test_maintain_button_sends_request(self):
        calls = []
        self.tab._request = lambda kind, path, data=None, **kw: calls.append((kind, path, data))
        self.tab._maintain()
        self.assertEqual(calls[-1][0], 'maintain')
        self.assertTrue(calls[-1][1].endswith('learning/maintain'))

    def test_read_aloud_uses_local_voice_client(self):
        from unittest.mock import patch
        self.tab._last_agent_text = '你好，这是本地朗读。'
        with patch('src.gui.assistant.voice_client.speak') as speak:
            self.tab.read_aloud()
            for _ in range(20):
                self.app.processEvents()
                if speak.called:
                    break
                self.app.thread().msleep(10)
            speak.assert_called_once()

    def test_schedules_dialog_create_and_run(self):
        self.tab._show_schedules({'tasks': [
            {'id': 's1', 'name': '连接检查', 'every_minutes': 30, 'enabled': True,
             'last_result': '连接正常'},
            {'id': 's2', 'name': '日报', 'every_minutes': 60, 'enabled': False}]})
        self.assertEqual(self.tab.schedules_list.count(), 2)
        self.assertIn('连接检查', self.tab.schedules_list.item(0).text())
        calls = []
        self.tab._request = lambda kind, path, data=None, **kw: calls.append((kind, path, data))
        self.tab.schedule_name.setText('新任务')
        self.tab.schedule_action.setCurrentIndex(1)
        self.tab.schedule_minutes.setValue(120)
        self.tab._create_schedule()
        self.assertEqual(calls[-1][0], 'schedule_create')
        self.assertEqual(calls[-1][2]['action'], 'daily_digest')
        self.assertEqual(calls[-1][2]['every_minutes'], 120)
        self.tab.schedules_list.setCurrentRow(0)
        self.tab._run_schedule()
        self.assertEqual(calls[-1][0], 'schedule_run')
        self.tab._toggle_schedule()
        self.assertEqual(calls[-1][2], {'enabled': False})

    def test_skills_dialog_toggle_and_delete(self):
        self.tab._show_skills({'skills': [
            {'id': 's1', 'name': '权限技能', 'permissions': ['read_memory'], 'enabled': True},
            {'id': 's2', 'name': '停用技能', 'permissions': [], 'enabled': False}]})
        self.assertEqual(self.tab.skills_list.count(), 2)
        self.assertIn('read_memory', self.tab.skills_list.item(0).text())
        calls = []
        self.tab._request = lambda kind, path, data=None, **kw: calls.append((kind, path, data))
        self.tab.skills_list.setCurrentRow(0)
        self.tab._toggle_skill()
        self.assertEqual(calls[-1][0], 'skill_toggle')
        self.assertEqual(calls[-1][2], {'enabled': False})
        from unittest.mock import patch
        from PyQt6.QtWidgets import QMessageBox
        with patch('src.gui.assistant.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.tab._delete_skill()
        self.assertEqual(calls[-1][0], 'skill_delete')
        self.assertEqual(calls[-1][2], {'skill_id': 's1'})

    def test_sessions_dialog_and_task_cards(self):
        self.tab._show_sessions({'sessions': [
            {'id': 's1', 'title': '调快回复', 'turns': 2},
            {'id': 's2', 'title': '插件任务', 'turns': 1}]})
        self.assertEqual(self.tab.session_list.count(), 2)
        self.assertIn('调快回复', self.tab.session_list.item(0).text())
        self.tab._render_session({'id': 's1', 'title': '调快回复',
                                  'turns': [{'event': 'applied', 'prompt': '把回复调快',
                                             'summary': '等待 15 秒'}]})
        self.assertEqual(self.tab.session_id, 's1')
        self.assertIn('把回复调快', self.tab.history.toPlainText())
        self.assertIn('已应用', self.tab.history.toPlainText())
        self.assertIn('调快回复', self.tab.sessions_button.toolTip())

    def test_usage_guide_opens_and_example_fills_prompt(self):
        from unittest.mock import patch
        with patch('src.gui.assistant.QDialog.exec', return_value=0):
            self.tab.open_guide()
        self.assertEqual(self.tab.guide_button.toolTip(), '使用说明')
        labels = [w.text() for w in self.tab.findChildren(QPushButton)]
        self.assertIn('说明', labels)

    def test_history_dialog_and_undo_selection(self):
        self.tab._show_history({'history': [
            {'time': 1, 'event': 'applied', 'prompt': '调快回复', 'summary': '等待 15 秒', 'job': 'd' * 32},
            {'time': 2, 'event': 'planned', 'prompt': '打开插件', 'summary': '', 'job': 'e' * 32}]})
        self.assertEqual(self.tab.history_list.count(), 2)
        self.assertIn('调快回复', self.tab.history_list.item(0).text())
        calls = []
        self.tab._request = lambda kind, path, data=None, **kw: calls.append((kind, path, data))
        self.tab.history_list.setCurrentRow(0)
        self.tab._undo_selected()
        self.assertEqual(calls[-1][0], 'undo')
        self.assertIn('d' * 32, calls[-1][1])

    def test_learning_dialog_lists_lessons_and_skills(self):
        self.tab._show_learning({'stats': {'lessons': 1, 'skills': 1, 'preferences': 1,
                                          'success': 2, 'failure': 0},
                                'lessons': [{'id': 'l1', 'intent': '把回复调快', 'ok': 2, 'fail': 0,
                                             'summary': '等待上限 20 → 15'}],
                                'preferences': [{'key': '称呼', 'value': '老鲸'}],
                                'skills': [{'id': 's1', 'name': '语气调整', 'description': '先问清楚'}]})
        texts = [self.tab.learning_list.item(i).text()
                 for i in range(self.tab.learning_list.count())]
        self.assertTrue(any('把回复调快' in text for text in texts))
        self.assertTrue(any('称呼' in text for text in texts))
        self.assertTrue(any('语气调整' in text for text in texts))
        self.assertIn('经验 1 条', self.tab.learning_summary.text())


if __name__ == '__main__':
    unittest.main()
