"""T14/T17 面板测试：数据助手 + offscreen UI 刷新/取消/审批。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src.gui.audio_panel import AudioPanel, TaskRow
from src.gui.local_agent_panel import LocalAgentPanel, PendingAction


class PanelDataTests(unittest.TestCase):
    def test_audio_rows_map_gateway_jobs(self):
        rows = AudioPanel._rows_from_payload({'jobs': [
            {'job_id': 'j1', 'mode': 'tts', 'state': 'ready',
             'provider': 'audio_cpp', 'cost_actual': 0.0123},
            {'job_id': 'j2', 'mode': 'tts', 'state': 'running'},
        ]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].capability, 'tts')
        self.assertEqual(rows[0].cost_note, '0.0123')
        self.assertEqual(rows[1].state, 'running')
        self.assertTrue(isinstance(rows[0], TaskRow))

    def test_audio_rows_tolerate_missing_payload(self):
        self.assertEqual(AudioPanel._rows_from_payload({}), [])
        self.assertEqual(AudioPanel._rows_from_payload(None), [])

    def test_budget_text(self):
        text = AudioPanel._budget_from_payload({'capabilities': {'budget': {
            'daily_limit': 5, 'spent_today': 1.5, 'reserved_today': 0.2,
            'unknown_today': 0.1}}})
        self.assertIn('1.5', text)
        self.assertIn('待核实', text)
        self.assertEqual(AudioPanel._budget_from_payload({}), '')

    def test_pending_action_reapproval(self):
        pending = PendingAction('j1', 'dev-1', 'app.open', 'app_id=notepad')
        self.assertFalse(pending.needs_reapproval('app_id=notepad'))
        self.assertTrue(pending.needs_reapproval('app_id=calc'))
        parsed = LocalAgentPanel._pending_from({'job_id': 'j1', 'device_id': 'dev-1',
                                                'action': 'app.open', 'arg_summary': 'x',
                                                'policy_version': 2})
        self.assertEqual(parsed.policy_version, 2)


try:
    from PyQt6.QtWidgets import QApplication
    _QT = True
except Exception:
    _QT = False


@unittest.skipUnless(_QT, '需要 PyQt6')
class PanelUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def get(self, path):
        if path.startswith('/api/v2/audio/status'):
            return {'jobs': [{'job_id': 'j1', 'mode': 'tts', 'state': 'ready',
                              'provider': 'audio_cpp'}],
                    'capabilities': {'budget': {'daily_limit': 5, 'spent_today': 0}}}
        if path.startswith('/api/v2/agent/pending'):
            return {'pending': [{'job_id': 'a1', 'device_id': 'dev-1',
                                 'action': 'app.open', 'arg_summary': 'app_id=notepad'}]}
        if path.startswith('/api/v2/agent/status'):
            return {'devices': [{'device_id': 'dev-1', 'paired': True}], 'stopped': False}
        if path.startswith('/api/v2/agent/audit'):
            return {'events': [{'event': 'submitted app.open'}]}
        if path.startswith('/api/v2/audio/voices'):
            return {'voices': [{'profile_id': 'alice', 'name': 'Alice',
                                'provider_map': {'gpt_sovits_api_v2': {}}}]}
        return {}

    def test_audio_panel_refresh_and_cancel(self):
        posts = []
        panel = AudioPanel(self.get, lambda path, data=None: posts.append(path) or {})
        rows = panel.refresh_tasks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(panel.table.rowCount(), 1)
        panel.cancel('j1')
        self.assertTrue(any('cancel' in path for path in posts))
        panel.cleanup()

    def test_audio_panel_voices_import_and_generate(self):
        posts = []

        def post(path, data=None):
            posts.append((path, data))
            if path.endswith('/voices/import'):
                return {'success': True, 'count': 1}
            if path.endswith('/jobs'):
                return {'success': True, 'job_id': 'j-new'}
            return {}

        panel = AudioPanel(self.get, post)
        voices = panel.refresh_voices()
        self.assertEqual(len(voices), 1)
        self.assertEqual(panel.voice_combo.count(), 2)
        panel.voice_combo.setCurrentIndex(1)
        self.assertEqual(panel.selected_voice(), 'alice')
        self.assertEqual(panel.import_voices(['/tmp/a.wav']), 1)
        self.assertTrue(panel.set_default_voice())
        self.assertTrue(any(path.endswith('/default') for path, _ in posts))
        panel.sample_text.setText('试听一句')
        self.assertEqual(panel.generate_sample(), 'j-new')
        job_calls = [data for path, data in posts if path.endswith('/jobs')]
        self.assertEqual(job_calls[-1]['voice_profile_id'], 'alice')
        self.assertEqual(job_calls[-1]['text'], '试听一句')
        panel.cleanup()

    def test_agent_panel_refresh_approve_and_stop(self):
        posts = []

        def post(path, data=None):
            posts.append((path, data))
            return {'state': 'succeeded', 'cleared_approvals': 2}

        panel = LocalAgentPanel(self.get, post)
        pending = panel.refresh()
        self.assertEqual(len(pending), 1)
        self.assertIn('已配对', panel.status.text())
        panel.approve(pending[0])
        panel.emergency_stop()
        self.assertTrue(any(path.endswith('/approve') for path, _ in posts))
        self.assertTrue(any(path.endswith('/emergency-stop') for path, _ in posts))
        panel.cleanup()


if __name__ == '__main__':
    unittest.main()
