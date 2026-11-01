"""W9a：外部后端面板的「自动识别」——按钮、异步探测、候选框、存为 profile。

全部离屏跑：构造不发请求、点按钮不走主线程网络、候选框非阻塞 show()、
选中候选后 profile 文件里真的出现该 id（写盘用真实的 save_profiles）。
"""
import json
import os
import tempfile
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from src.agent_backends import load_profiles, save_profiles, validate_profile
from src.gui.agent_panel import (AgentBackendPanel, DetectDialog, args_text,
                                detect_path)

APP = QApplication.instance() or QApplication([])

EMPTY_LIST = {'ok': True, 'backends': [], 'data_dir': 'C:/tmp/data',
              'data_dir_ready': True, 'profile_file': 'C:/tmp/data/agent_backends.json'}


def candidate(profile, command='C:/Users/x/AppData/Roaming/npm/dsh.CMD', **extra):
    item = {'id': 'dsh-%s' % profile, 'kind': 'dsh', 'label': 'DSH %s' % profile,
            'command': command, 'args': ['--profile', profile, '{prompt}'],
            'workdir': '', 'source': 'which', 'profile': profile,
            'version': '0.1.5-rc.3', 'ok': True, 'reason': ''}
    item.update(extra)
    return item


def detect_payload(items):
    return {'ok': True, 'items': list(items), 'probe': False,
            'scanned': ['C:/Users/x/AppData/Roaming/npm/dsh.CMD',
                        'C:/Users/x/.dsh/profiles'],
            'elapsed_ms': 7,
            'reason': '' if items else
            '已扫描：C:/Users/x/AppData/Roaming/npm/dsh.CMD；没有找到可用的 DSH 入口'}


class FakeApi:
    """端点替身：GET 按路径分发，POST 把 profile 真的写进临时目录。"""

    def __init__(self, detect_result=None, delay=0.0, profile_file=''):
        self.detect_result = detect_payload([candidate('headless')]) \
            if detect_result is None else detect_result
        self.delay = delay
        self.profile_file = profile_file
        self.calls = []

    def get(self, path, timeout=None):
        self.calls.append(('GET', path, None))
        if self.delay:
            time.sleep(self.delay)
        if 'detect' in path:
            return dict(self.detect_result)
        return dict(EMPTY_LIST)

    def post(self, path, data=None, timeout=None):
        self.calls.append(('POST', path, dict(data or {})))
        if self.delay:
            time.sleep(self.delay)
        checked = validate_profile((data or {}).get('kind', 'cli'),
                                   (data or {}).get('profile'))
        if not checked.get('ok'):
            return dict(checked)
        if self.profile_file:
            current = load_profiles(self.profile_file)
            profiles = dict(current.get('profiles') or {})
            profiles[data['id']] = {'kind': checked['kind'],
                                   'profile': checked['profile']}
            saved = save_profiles(self.profile_file, profiles)
            if not saved.get('ok'):
                return dict(saved)
        return {'ok': True, 'id': (data or {}).get('id'), 'kind': checked['kind']}

    def delete(self, path, timeout=None):
        self.calls.append(('DELETE', path, None))
        return {'ok': True}

    def detect_calls(self):
        return [call for call in self.calls if call[1].startswith('agent/backends/detect')]


def pump(until=None, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        APP.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    APP.processEvents()
    return until is None or bool(until())


class PanelDetectCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.profile_file = os.path.join(self.temp.name, 'agent_backends.json')
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            try:
                panel.stop_polling()
            except Exception:
                pass
        pump(timeout=0.05)
        for panel in self.panels:
            try:
                panel.close()
            except Exception:
                pass
        self.temp.cleanup()

    def make(self, detect_result=None, delay=0.0):
        api = FakeApi(detect_result, delay=delay, profile_file=self.profile_file)
        panel = AgentBackendPanel(api.get, api.post, api.delete)
        self.panels.append(panel)
        pump(until=lambda: bool(api.calls))
        pump(until=lambda: not panel.requests.busy)
        return api, panel

    def detect(self, api, panel):
        self.assertTrue(panel.action_detect())
        pump(until=lambda: bool(api.detect_calls()))
        pump(until=lambda: not panel.requests.busy)
        return panel


class TestDetectButton(PanelDetectCase):
    def button_row(self, panel):
        layout = panel.layout()
        for index in range(layout.count()):
            sub = layout.itemAt(index).layout()
            if sub is None:
                continue
            labels = [sub.itemAt(i).widget().text() for i in range(sub.count())
                      if sub.itemAt(i).widget() is not None
                      and hasattr(sub.itemAt(i).widget(), 'text')]
            if '刷新' in labels:
                return labels
        return []

    def test_auto_detect_button_follows_refresh(self):
        _api, panel = self.make()
        labels = self.button_row(panel)
        self.assertIn('自动识别', labels)
        self.assertEqual(labels.index('刷新') + 1, labels.index('自动识别'))

    def test_constructor_does_not_call_detect(self):
        api, panel = self.make()
        self.assertEqual([], api.detect_calls())
        self.assertEqual('', panel.detect_hint())

    def test_action_detect_hits_detect_path_without_probe_flag(self):
        api, panel = self.make()
        self.detect(api, panel)
        self.assertEqual('GET', api.detect_calls()[0][0])
        self.assertEqual('agent/backends/detect?probe=0', api.detect_calls()[0][1])
        self.assertEqual(detect_path(), 'agent/backends/detect?probe=0')
        self.assertEqual(detect_path(True), 'agent/backends/detect?probe=1')

    def test_probe_flag_is_forwarded(self):
        api, panel = self.make()
        self.assertTrue(panel.action_detect(True))
        pump(until=lambda: bool(api.detect_calls()))
        self.assertEqual('agent/backends/detect?probe=1', api.detect_calls()[0][1])

    def test_action_detect_returns_immediately(self):
        api, panel = self.make(delay=0.4)
        pump(until=lambda: not panel.requests.busy)
        started = time.time()
        self.assertTrue(panel.action_detect())
        elapsed = time.time() - started
        self.assertLess(elapsed, 0.2, '按钮不许在主线程等网络')
        pump(until=lambda: not panel.requests.busy, timeout=5.0)


class TestDetectApply(PanelDetectCase):
    def test_single_candidate_prefills_form_without_dialog(self):
        api, panel = self.make(detect_result=detect_payload([candidate('headless')]))
        self.detect(api, panel)
        self.assertEqual('dsh-headless', panel.input_id.text())
        self.assertEqual('C:/Users/x/AppData/Roaming/npm/dsh.CMD',
                         panel.input_command.text())
        self.assertEqual('--profile headless {prompt}', panel.input_args.text())
        self.assertEqual('', panel.input_workdir.text())
        self.assertEqual(120.0, panel.input_timeout.value())
        self.assertIsNone(panel.dialog)
        self.assertIn('已识别 1 个候选', panel.detect_hint())
        self.assertEqual(1, len(panel.candidates()))

    def test_multiple_candidates_open_dialog_with_one_row_each(self):
        items = [candidate('headless'), candidate('web', command='C:/npm/dsh.cmd')]
        api, panel = self.make(detect_result=detect_payload(items))
        self.detect(api, panel)
        dialog = panel.dialog
        self.assertIsInstance(dialog, DetectDialog)
        self.assertEqual(2, dialog.row_count())
        self.assertEqual(2, len(dialog.buttons))
        self.assertEqual('dsh-headless', dialog.table.item(0, 0).text())
        self.assertEqual('dsh', dialog.table.item(1, 1).text())
        self.assertEqual('headless', dialog.table.item(0, 2).text())
        self.assertEqual('0.1.5-rc.3', dialog.table.item(0, 4).text())
        self.assertEqual('which', dialog.table.item(0, 5).text())
        self.assertEqual('存为 profile', dialog.buttons[0].text())
        self.assertFalse(dialog.isModal(), '候选框不许阻塞主线程')
        self.assertIn('已识别 2 个候选', panel.detect_hint())

    def test_zero_candidates_hint_carries_scanned_and_reason(self):
        api, panel = self.make(detect_result=detect_payload([]))
        self.detect(api, panel)
        hint = panel.detect_hint()
        self.assertIn('C:/Users/x/AppData/Roaming/npm/dsh.CMD', hint)
        self.assertIn('C:/Users/x/.dsh/profiles', hint)
        self.assertIn('没有找到可用的 DSH 入口', hint)
        self.assertEqual(hint, panel.error_text())
        self.assertIsNone(panel.dialog)

    def test_error_payload_shows_code_and_hint(self):
        api, panel = self.make(detect_result={'ok': False, 'code': 'io',
                                             'message': '探测端点挂了'})
        self.detect(api, panel)
        self.assertIn('[io]', panel.error_text())
        self.assertIn('探测端点挂了', panel.detect_hint())
        self.assertEqual([], panel.candidates())


class TestSaveCandidate(PanelDetectCase):
    def test_dialog_button_saves_profile_file_with_candidate_id(self):
        items = [candidate('headless'), candidate('web')]
        api, panel = self.make(detect_result=detect_payload(items))
        self.detect(api, panel)
        dialog = panel.dialog
        dialog.buttons[1].click()       # 真按钮 → 面板 → PanelRequests
        pump(until=lambda: any(call[0] == 'POST' for call in api.calls))
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        posts = [call for call in api.calls if call[0] == 'POST']
        self.assertEqual(1, len(posts))
        body = posts[0][2]
        self.assertEqual('dsh-web', body['id'])
        self.assertEqual('dsh', body['kind'])
        self.assertEqual(['--profile', 'web', '{prompt}'], body['profile']['args'])
        self.assertEqual(120.0, body['profile']['timeout'])
        saved = load_profiles(self.profile_file)
        self.assertTrue(saved['ok'], saved)
        self.assertIn('dsh-web', saved['profiles'])
        self.assertEqual('dsh', saved['profiles']['dsh-web']['kind'])
        with open(self.profile_file, encoding='utf-8') as handle:
            self.assertIn('dsh-web', handle.read())

    def test_args_text_roundtrips_through_split_args(self):
        from src.gui.agent_panel import split_args
        args = ['--profile', 'headless', '{prompt}', 'two words']
        self.assertEqual(args, split_args(args_text(args)))

    def test_save_candidate_without_id_is_rejected(self):
        _api, panel = self.make()
        self.assertFalse(panel.save_candidate({'kind': 'dsh', 'command': 'dsh.cmd'}))
        self.assertIn('bad_profile', panel.error_text())

    def test_close_closes_dialog(self):
        items = [candidate('headless'), candidate('web')]
        api, panel = self.make(detect_result=detect_payload(items))
        self.detect(api, panel)
        dialog = panel.dialog
        panel.close()
        pump(timeout=0.05)
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(panel.dialog)


if __name__ == '__main__':
    unittest.main()
