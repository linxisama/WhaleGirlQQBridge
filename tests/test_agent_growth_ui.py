"""W7c3：定时任务类型唯一真源 action_choices() 与界面/提示词同步。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src.agent_scheduler import ACTIONS, action_choices

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ['connection_check', 'daily_digest', 'daily_diary', 'memory_maintain',
            'memory_daily', 'learn_chat', 'learn_web_queue', 'bili_daily',
            'dream_consolidate', 'self_portrait']


def source(rel):
    return (ROOT / 'src' / rel).read_text(encoding='utf-8')


class ActionChoicesTests(unittest.TestCase):
    def test_returns_all_choices(self):
        self.assertEqual(len(action_choices()), len(EXPECTED))

    def test_first_is_connection_check(self):
        label, key = action_choices()[0]
        self.assertEqual(key, 'connection_check')
        self.assertEqual(label, ACTIONS['connection_check'])

    def test_growth_actions_present(self):
        choices = action_choices()
        self.assertIn(('夜间固化记忆', 'dream_consolidate'), choices)
        self.assertIn(('重写自画像', 'self_portrait'), choices)

    def test_keys_match_actions_in_order(self):
        choices = action_choices()
        self.assertEqual([key for _, key in choices], list(ACTIONS))
        self.assertEqual({key for _, key in choices}, set(ACTIONS))
        self.assertEqual([key for _, key in choices], EXPECTED)


class SourceSyncTests(unittest.TestCase):
    def test_assistant_has_no_hardcoded_action_literals(self):
        text = source(os.path.join('gui', 'assistant.py'))
        self.assertNotIn("'bili_daily'", text)
        self.assertNotIn("'connection_check'", text)
        self.assertIn('action_choices', text)

    def test_assistant_hint_mentions_growth_switches(self):
        text = source(os.path.join('gui', 'assistant.py'))
        self.assertIn('后台每 30 秒检查一次，不会发消息。夜间固化记忆与重写自画像按设置页开关生效。', text)

    def test_console_prompt_line_lists_all_actions(self):
        """不绑行号，只绑内容：提示词里必须有恰好一行列出全部定时动作。"""
        lines = [line for line in source('console_agent.py').splitlines()
                 if '"type":"schedule"' in line]
        self.assertEqual(len(lines), 1,
                         '提示词里应该恰好有一行描述 schedule，实际命中 %d 行' % len(lines))
        line = lines[0]
        for key in EXPECTED:
            self.assertIn(key, line, '%s 没出现在 schedule 提示行：%r' % (key, line))


class SchedulesDialogTests(unittest.TestCase):
    """offscreen 下真实建出定时任务对话框，确认下拉项来自 action_choices()。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_schedule_action_combo_lists_all_actions(self):
        from src.gui.assistant import AssistantTab
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tab = AssistantTab('http://127.0.0.1:9', tmp.name)
        self.addCleanup(tab.deleteLater)
        dialog = tab._schedules_dialog()
        self.addCleanup(dialog.accept)
        combo = tab.schedule_action
        self.assertEqual(combo.count(), len(EXPECTED))
        self.assertEqual([combo.itemData(i) for i in range(combo.count())], EXPECTED)
        self.assertEqual([combo.itemText(i) for i in range(combo.count())],
                         [label for label, _ in action_choices()])


if __name__ == '__main__':
    unittest.main()
