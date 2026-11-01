
"""W12：外部后端面板的「好用」验收 —— 按钮态说话、点了必有回音、三步空状态、
候选框可读（列宽/tooltip/版本异步补/推荐列/双击保存/全部保存）、保存后自动选中、
状态列着色、表单归属文案。全部 offscreen 跑。
"""
import os
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from src.gui.agent_panel import (
    COL_COMMAND,
    COL_PROFILE,
    COL_RECOMMEND,
    COL_VERSION,
    EMPTY_TEXT,
    RECOMMEND_COLOR,
    STATE_COLORS,
    VERSION_PROBING,
    AgentBackendPanel,
)

APP = QApplication.instance() or QApplication([])

ROWS = [
    {'id': 'alpha', 'kind': 'cli', 'state': 'idle', 'running': False, 'pid': None,
     'command': 'python.exe', 'args': ['-c', 'print(1)'], 'workdir': 'C:/tmp',
     'timeout': 120.0, 'workspace': 'C:/tmp/ws/alpha'},
    {'id': 'beta', 'kind': 'cli', 'state': 'running', 'running': True, 'pid': 4242,
     'command': 'python.exe', 'args': ['-c', 'time.sleep(30)'], 'workdir': 'C:/tmp',
     'timeout': 60.0, 'workspace': 'C:/tmp/ws/beta'},
    {'id': 'gamma', 'kind': 'cli', 'state': 'failed', 'running': False, 'pid': None,
     'command': 'python.exe', 'args': [], 'workdir': 'C:/tmp',
     'timeout': 120.0, 'workspace': 'C:/tmp/ws/gamma'},
]

LONG_COMMAND = 'C:/Users/16522/AppData/Roaming/npm/dsh.CMD'

CANDIDATES = [
    {'id': 'dsh-headless', 'kind': 'dsh', 'profile': 'headless',
     'command': LONG_COMMAND, 'args': ['--profile', 'headless', '{prompt}'],
     'version': '', 'source': 'which', 'timeout': 120.0},
    {'id': 'dsh-desktop', 'kind': 'dsh', 'profile': 'desktop',
     'command': LONG_COMMAND, 'args': ['--profile', 'desktop', '{prompt}'],
     'version': '', 'source': 'which', 'timeout': 120.0},
    {'id': 'dsh-web', 'kind': 'dsh', 'profile': 'web',
     'command': LONG_COMMAND, 'args': ['--profile', 'web', '{prompt}'],
     'version': '', 'source': 'which', 'timeout': 120.0},
    {'id': 'dsh-web-desktop', 'kind': 'dsh', 'profile': 'web-desktop',
     'command': LONG_COMMAND, 'args': ['--profile', 'web-desktop', '{prompt}'],
     'version': '', 'source': 'which', 'timeout': 120.0},
]

VERSIONS = {'dsh-headless': '0.1.5-rc.3', 'dsh-desktop': '0.1.5-rc.3',
            'dsh-web': '0.1.5-rc.3', 'dsh-web-desktop': '0.1.5-rc.3'}


def pump(until=None, timeout=4.0):
    """转事件循环，直到 until() 为真或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        APP.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    APP.processEvents()
    return until is None or bool(until())


class FakeApi:
    """端点替身：列表可增、可拒某个 id、探测可返回版本号。"""

    def __init__(self, rows=None, reject_ids=(), detect=None):
        self.rows = [dict(row) for row in (ROWS if rows is None else rows)]
        self.reject_ids = set(reject_ids)
        self.detect = detect
        self.calls = []

    def get(self, path, timeout=None):
        self.calls.append(('GET', path, None))
        if path.startswith('agent/backends/detect'):
            probe = path.endswith('probe=1')
            items = [dict(item) for item in CANDIDATES]
            if self.detect is not None:
                return dict(self.detect)
            if probe:
                for item in items:
                    item['version'] = VERSIONS.get(item['id'], '')
            return {'ok': True, 'items': items, 'scanned': ['C:/Users/16522'],
                    'probe': probe, 'elapsed_ms': 5, 'reason': ''}
        if path.endswith('/workspace'):
            return {'ok': True, 'root': 'C:/tmp/ws/alpha'}
        return {'ok': True, 'backends': [dict(row) for row in self.rows],
                'data_dir': 'C:/tmp/data', 'data_dir_ready': True,
                'profile_file': 'C:/tmp/data/agent_backends.json'}

    def post(self, path, data=None, timeout=None):
        self.calls.append(('POST', path, data))
        if path.endswith('/probe'):
            return {'ok': True, 'resolved': 'C:/Python312/python.exe', 'runnable': True,
                    'version_ok': True, 'returncode': 0, 'stdout': '', 'version': ''}
        if path.endswith('/start'):
            return {'ok': True, 'id': 'alpha', 'pid': 9911, 'running': True}
        if path.endswith('/stop'):
            return {'ok': True, 'id': 'beta', 'pid': 4242}
        if path.endswith('/shutdown'):
            return {'ok': True}
        if isinstance(data, dict) and isinstance(data.get('profile'), dict):
            backend_id = str(data.get('id') or '')
            if backend_id in self.reject_ids:
                return {'ok': False, 'code': 'bad_profile', 'message': '命令不可执行'}
            if not any(row.get('id') == backend_id for row in self.rows):
                self.rows.append({'id': backend_id, 'kind': data.get('kind') or 'cli',
                                  'state': 'stopped', 'running': False, 'pid': None,
                                  'command': data['profile'].get('command') or '',
                                  'args': data['profile'].get('args') or [],
                                  'workdir': data['profile'].get('workdir') or '',
                                  'timeout': data['profile'].get('timeout') or 120.0,
                                  'workspace': 'C:/tmp/ws/' + backend_id})
            return {'ok': True, 'id': backend_id}
        return {'ok': True}

    def delete(self, path, timeout=None):
        self.calls.append(('DELETE', path, None))
        backend_id = path.rsplit('/', 1)[-1]
        self.rows = [row for row in self.rows if row.get('id') != backend_id]
        return {'ok': True, 'id': backend_id}

    def posted(self, suffix):
        return [call for call in self.calls
                if call[0] == 'POST' and str(call[1]).endswith(suffix)]

    def saved(self):
        """保存 profile 的 POST（路径就是 PANEL_PATH 本身）。"""
        return [call for call in self.calls
                if call[0] == 'POST' and call[1] == 'agent/backends']

    def get_paths(self):
        return [call[1] for call in self.calls if call[0] == 'GET']


class UxCase(unittest.TestCase):
    def setUp(self):
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            try:
                panel.stop_polling()
            except Exception:
                pass
        pump(timeout=0.05)

    def make(self, rows=None, reject_ids=(), detect=None):
        api = FakeApi(rows, reject_ids, detect)
        panel = AgentBackendPanel(api.get, api.post, api.delete)
        self.panels.append(panel)
        pump(until=lambda: bool(api.calls))
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.05)
        return api, panel


# ── ③ 空状态三步引导 ───────────────────────────────────────────────────────

class TestEmptyGuide(UxCase):
    def test_empty_state_lists_three_steps(self):
        api, panel = self.make(rows=[])
        self.assertTrue(panel.empty_visible())
        text = panel.empty_text()
        for step in ('①', '②', '③'):
            self.assertIn(step, text)
        self.assertIn('自动识别', text)
        self.assertIn('保存 profile', text)
        self.assertIn('启动', text)
        self.assertIn('真实进程', text)
        self.assertIn('进程树', text)
        self.assertEqual(text, EMPTY_TEXT)

    def test_rows_hide_empty_state(self):
        api, panel = self.make()
        self.assertFalse(panel.empty_visible())
        self.assertTrue(panel.table.isVisibleTo(panel))


# ── ① 按钮态说话 ───────────────────────────────────────────────────────────

class TestButtonMatrix(UxCase):
    def test_no_selection_disables_actions_with_reason(self):
        api, panel = self.make()
        panel.table.clearSelection()
        panel.table.setCurrentCell(-1, -1)
        self.assertEqual(panel.button_state(),
                         {'probe': False, 'start': False, 'stop': False,
                          'open': False, 'delete': False})
        tips = panel.button_tips()
        for name in ('probe', 'start', 'stop', 'open', 'delete'):
            self.assertIn('选中一个后端', tips[name])

    def test_selected_idle_enables_probe_start_delete_but_not_stop(self):
        api, panel = self.make()
        panel.select('alpha')
        self.assertEqual(panel.button_state(),
                         {'probe': True, 'start': True, 'stop': False,
                          'open': True, 'delete': True})
        self.assertIn('alpha', panel.button_tips()['start'])
        self.assertIn('没在运行', panel.button_tips()['stop'])

    def test_selected_running_enables_stop_and_blocks_start_delete(self):
        api, panel = self.make()
        panel.select('beta')
        self.assertEqual(panel.button_state(),
                         {'probe': True, 'start': False, 'stop': True,
                          'open': True, 'delete': False})
        tips = panel.button_tips()
        self.assertIn('4242', tips['stop'])
        self.assertIn('先点「停止」', tips['start'])
        self.assertIn('先点「停止」', tips['delete'])

    def test_failed_backend_can_be_started_again(self):
        api, panel = self.make()
        panel.select('gamma')
        self.assertTrue(panel.btn_start.isEnabled())
        self.assertFalse(panel.btn_stop.isEnabled())

    def test_refresh_and_detect_always_enabled(self):
        api, panel = self.make()
        panel.table.clearSelection()
        panel.table.setCurrentCell(-1, -1)
        self.assertTrue(panel.btn_refresh.isEnabled())
        self.assertTrue(panel.btn_detect.isEnabled())
        self.assertIn('任何状态都能点', panel.button_tips()['detect'])


# ── ② 点了必有回音 ─────────────────────────────────────────────────────────

class TestImmediateFeedback(UxCase):
    def test_refresh_says_refreshing_immediately(self):
        api, panel = self.make()
        panel.feedback.setText('')
        self.assertTrue(panel.refresh())
        self.assertIn('正在刷新', panel.feedback_text())
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.05)
        self.assertEqual(panel.feedback_text(), '')      # 成功后擦掉进行中文案

    def test_probe_writes_pending_text_then_result(self):
        api, panel = self.make()
        panel.select('alpha')
        panel.feedback.setText('')
        self.assertTrue(panel.action_probe())
        self.assertIn('正在探测 alpha', panel.feedback_text())
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        text = panel.feedback_text()
        self.assertIn('已探测 alpha', text)
        self.assertIn('resolved=', text)
        self.assertIn('runnable=是', text)

    def test_start_writes_pending_text_then_pid(self):
        api, panel = self.make()
        panel.select('alpha')
        panel.feedback.setText('')
        self.assertTrue(panel.action_start())
        self.assertIn('正在启动 alpha', panel.feedback_text())
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        self.assertIn('已启动 alpha', panel.feedback_text())
        self.assertIn('pid=9911', panel.feedback_text())

    def test_stop_writes_pending_text_then_result(self):
        api, panel = self.make()
        panel.select('beta')
        panel.feedback.setText('')
        self.assertTrue(panel.action_stop())
        self.assertIn('正在停止 beta', panel.feedback_text())
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        self.assertIn('已停止 beta', panel.feedback_text())

    def test_delete_writes_pending_text_then_result(self):
        api, panel = self.make()
        panel.select('alpha')
        panel.feedback.setText('')
        self.assertTrue(panel.action_delete())
        self.assertIn('正在删除 alpha', panel.feedback_text())
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        self.assertIn('已删除 alpha', panel.feedback_text())

    def test_open_workspace_writes_pending_text_then_path(self):
        seen = []
        api = FakeApi()
        panel = AgentBackendPanel(api.get, api.post, api.delete, opener=seen.append)
        self.panels.append(panel)
        pump(until=lambda: bool(api.calls))
        pump(until=lambda: not panel.requests.busy)
        panel.select('alpha')
        panel.feedback.setText('')
        self.assertTrue(panel.action_open_workspace())
        self.assertIn('正在获取 alpha 的工作区', panel.feedback_text())
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        self.assertIn('C:/tmp/ws/alpha', panel.feedback_text())
        self.assertEqual(seen, ['C:/tmp/ws/alpha'])

    def test_action_without_selection_speaks_instead_of_silence(self):
        api, panel = self.make()
        panel.table.clearSelection()
        panel.table.setCurrentCell(-1, -1)
        panel.feedback.setText('')
        self.assertFalse(panel.action_probe())
        self.assertIn('[no_selection]', panel.feedback_text())
        self.assertFalse(panel.action_start())
        self.assertIn('[no_selection]', panel.feedback_text())
        self.assertEqual(api.posted('/probe'), [])

    def test_failure_keeps_raw_error_code(self):
        api = FakeApi(detect={'ok': False, 'code': 'io', 'message': '扫描失败'})
        panel = AgentBackendPanel(api.get, api.post, api.delete)
        self.panels.append(panel)
        pump(until=lambda: bool(api.calls))
        pump(until=lambda: not panel.requests.busy)
        panel.action_detect()
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.05)
        self.assertIn('[io]', panel.feedback_text())
        self.assertIn('扫描失败', panel.feedback_text())

    def test_detect_says_scanning_immediately(self):
        api, panel = self.make()
        panel.feedback.setText('')
        self.assertTrue(panel.action_detect())
        self.assertIn('正在识别本机 DSH', panel.feedback_text())


# ── ⑦ 表单归属 ─────────────────────────────────────────────────────────────

class TestFormOwnership(UxCase):
    def test_edit_title_follows_selection(self):
        api, panel = self.make()
        panel.select('alpha')
        self.assertEqual(panel.edit_title(), '正在编辑：alpha')
        panel.select('beta')
        self.assertEqual(panel.edit_title(), '正在编辑：beta')
        panel.clear_form()
        self.assertEqual(panel.edit_title(), '正在编辑：新建 profile')
        self.assertEqual(panel.input_id.text(), '')
        self.assertIn('表单已清空', panel.feedback_text())


# ── ⑥ 状态列着色 ───────────────────────────────────────────────────────────

class TestStateColors(UxCase):
    def test_state_column_uses_foreground_colors(self):
        api, panel = self.make()
        colors = {}
        for row in range(3):
            item = panel.table.item(row, 1)
            self.assertIsNotNone(item)
            color = item.foreground().color().name()
            self.assertTrue(color)
            colors[panel.table.item(row, 0).text()] = color
        self.assertEqual(colors['beta'], STATE_COLORS['running'].lower())
        self.assertEqual(colors['gamma'], STATE_COLORS['failed'].lower())
        self.assertEqual(colors['alpha'], STATE_COLORS['idle'].lower())
        self.assertNotEqual(colors['beta'], colors['gamma'])
        # 状态列写中文标签，pid 列仍然保留
        self.assertEqual(panel.table.item(1, 1).text(), '运行中')
        self.assertEqual(panel.table.item(1, 2).text(), '4242')
        self.assertEqual(panel.table.item(0, 2).text(), '-')


# ── ⑤ 保存后自动定位 ───────────────────────────────────────────────────────

class TestSaveSelectsNewRow(UxCase):
    def test_save_profile_selects_new_row_and_hints_start(self):
        api, panel = self.make()
        panel.select('beta')
        panel.input_id.setText('delta')
        panel.input_command.setText('python.exe')
        panel.input_args.setText('-c "print(1)"')
        self.assertTrue(panel.save_profile())
        self.assertIn('正在保存 profile：delta', panel.feedback_text())
        pump(until=lambda: not panel.requests.busy)
        pump(until=lambda: panel.current_id() == 'delta')
        pump(timeout=0.1)
        self.assertIn('已保存 profile：delta', panel.feedback_text())
        self.assertIn('点「启动」开始', panel.feedback_text())
        self.assertEqual(panel.current_id(), 'delta')
        self.assertIn('delta', [row['id'] for row in api.rows])
        body = api.saved()[0][2]
        self.assertEqual(body['id'], 'delta')
        self.assertEqual(body['profile']['args'], ['-c', 'print(1)'])

    def test_save_profile_failure_keeps_error_code(self):
        api, panel = self.make(reject_ids={'delta'})
        panel.input_id.setText('delta')
        panel.input_command.setText('nope.exe')
        self.assertTrue(panel.save_profile())
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        self.assertIn('[bad_profile]', panel.feedback_text())
        self.assertIn('命令不可执行', panel.feedback_text())
        self.assertNotEqual(panel.current_id(), '')


# ── ④ 候选对话框可读 ───────────────────────────────────────────────────────

class TestCandidateDialog(UxCase):
    def open_dialog(self, panel):
        dialog = panel.open_candidates(CANDIDATES)
        self.assertIsNotNone(dialog)
        pump(timeout=0.05)
        return dialog

    def test_dialog_command_column_shows_full_text(self):
        api, panel = self.make()
        dialog = self.open_dialog(panel)
        self.assertFalse(dialog.isModal())
        self.assertEqual(dialog.table.textElideMode(), Qt.TextElideMode.ElideNone)
        width = dialog.table.columnWidth(COL_COMMAND)
        need = dialog.table.fontMetrics().horizontalAdvance(LONG_COMMAND)
        self.assertGreaterEqual(width, 260)
        self.assertGreaterEqual(width, need)
        cell = dialog.table.item(0, COL_COMMAND)
        self.assertEqual(cell.text(), LONG_COMMAND)          # 不截断、不换行
        self.assertEqual(cell.toolTip(), LONG_COMMAND)       # 完整 tooltip
        for column in range(dialog.table.columnCount()):
            item = dialog.table.item(0, column)
            self.assertTrue(item.toolTip() or item.text() == '')

    def test_dialog_version_column_probes_async(self):
        api, panel = self.make()
        dialog = panel.open_candidates(CANDIDATES)
        self.assertEqual(dialog.table.horizontalHeaderItem(COL_VERSION).text(),
                         VERSION_PROBING)
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        self.assertEqual(dialog.table.horizontalHeaderItem(COL_VERSION).text(), '版本')
        self.assertEqual(dialog.table.item(0, COL_VERSION).text(), '0.1.5-rc.3')
        self.assertTrue(any(path.endswith('probe=1') for path in api.get_paths()))
        self.assertIn('版本', dialog.hint.text())

    def test_dialog_marks_headless_recommended(self):
        api, panel = self.make()
        dialog = self.open_dialog(panel)
        self.assertEqual(dialog.table.item(0, COL_PROFILE).text(), 'headless')
        self.assertEqual(dialog.table.item(0, COL_RECOMMEND).text(), '推荐')
        self.assertEqual(dialog.table.item(0, COL_RECOMMEND)
                         .foreground().color().name(), RECOMMEND_COLOR.lower())
        self.assertEqual(dialog.table.item(1, COL_RECOMMEND).text(), '')

    def test_dialog_double_click_saves_profile(self):
        api, panel = self.make()
        dialog = self.open_dialog(panel)
        dialog.table.itemDoubleClicked.emit(dialog.table.item(1, COL_COMMAND))
        pump(until=lambda: bool(api.saved()))
        pump(until=lambda: not panel.requests.busy)
        body = api.saved()[0][2]
        self.assertEqual(body['id'], 'dsh-desktop')
        self.assertEqual(body['kind'], 'dsh')
        self.assertEqual(body['profile']['args'],
                         ['--profile', 'desktop', '{prompt}'])
        self.assertFalse(dialog.isVisible())

    def test_dialog_save_all_reports_each_row(self):
        api, panel = self.make(reject_ids={'dsh-web'})
        dialog = self.open_dialog(panel)
        dialog.save_all()
        pump(until=lambda: len(dialog.save_all_results()) >= 4)
        pump(until=lambda: not panel.requests.busy)
        pump(timeout=0.1)
        results = dialog.save_all_results()
        self.assertEqual(len(results), 4)
        self.assertEqual([row['id'] for row in results],
                         ['dsh-headless', 'dsh-desktop', 'dsh-web', 'dsh-web-desktop'])
        self.assertEqual([row['ok'] for row in results], [True, True, False, True])
        failed = [row for row in results if not row['ok']]
        self.assertEqual(failed[0]['code'], 'bad_profile')
        text = dialog.hint.text()
        self.assertIn('成功 3/4', text)
        self.assertIn('dsh-web', text)
        self.assertIn('bad_profile', text)
        # 失败的那条没有打断后面那条
        self.assertIn('dsh-web-desktop', [row['id'] for row in api.rows])

    def test_dialog_has_three_bottom_buttons(self):
        api, panel = self.make()
        dialog = self.open_dialog(panel)
        self.assertEqual(dialog.btn_save_selected.text(), '存选中')
        self.assertEqual(dialog.btn_save_all.text(), '全部保存')
        self.assertEqual(dialog.btn_close_dialog.text(), '关闭')
        self.assertEqual(len(dialog.buttons), len(CANDIDATES))
        self.assertEqual(dialog.buttons[0].text(), '存为 profile')

    def test_save_selected_uses_current_row(self):
        api, panel = self.make()
        dialog = self.open_dialog(panel)
        dialog.table.selectRow(3)
        self.assertTrue(dialog.save_selected())
        pump(until=lambda: bool(api.saved()))
        pump(until=lambda: not panel.requests.busy)
        self.assertEqual(api.saved()[0][2]['id'], 'dsh-web-desktop')


if __name__ == '__main__':
    unittest.main()
