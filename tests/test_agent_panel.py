
"""外部后端面板：offscreen 构造、空态、行渲染、按钮状态、错误码回显、非阻塞轮询。"""
import os
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from src.gui.agent_panel import (
    EMPTY_TEXT,
    POLL_INTERVAL_MS,
    STATE_LABELS,
    AgentBackendPanel,
    split_args,
    error_of,
    state_of,
)

APP = QApplication.instance() or QApplication([])

ROWS = [
    {'id': 'alpha', 'kind': 'cli', 'state': 'idle', 'running': False, 'pid': None,
     'command': 'python.exe', 'args': ['-c', 'print(1)'], 'workdir': 'C:/tmp',
     'timeout': 120.0, 'workspace': 'C:/tmp/ws/alpha', 'data_dir_ready': True},
    {'id': 'beta', 'kind': 'cli', 'state': 'running', 'running': True, 'pid': 4242,
     'command': 'python.exe', 'args': ['-c', 'time.sleep(30)'], 'workdir': 'C:/tmp',
     'timeout': 60.0, 'workspace': 'C:/tmp/ws/beta', 'data_dir_ready': True},
    {'id': 'gamma', 'kind': 'cli', 'state': 'failed', 'running': False, 'pid': None,
     'command': 'python.exe', 'args': [], 'workdir': 'C:/tmp',
     'timeout': 120.0, 'workspace': 'C:/tmp/ws/gamma', 'data_dir_ready': True},
]


def list_payload(rows=None):
    return {'ok': True, 'backends': list(ROWS if rows is None else rows),
            'data_dir': 'C:/tmp/data', 'data_dir_ready': True,
            'profile_file': 'C:/tmp/data/agent_backends.json'}


class FakeApi:
    """端点替身：记录调用，可注入延迟与错误载荷。"""

    def __init__(self, list_result=None, post_result=None, delete_result=None, delay=0.0):
        self.list_result = list_payload() if list_result is None else list_result
        self.post_result = {'ok': True, 'id': 'alpha'} if post_result is None else post_result
        self.delete_result = {'ok': True, 'id': 'alpha'} if delete_result is None else delete_result
        self.delay = delay
        self.calls = []

    def get(self, path, timeout=None):
        self.calls.append(('GET', path, None))
        if self.delay:
            time.sleep(self.delay)
        return dict(self.list_result) if isinstance(self.list_result, dict) else self.list_result

    def post(self, path, data=None, timeout=None):
        self.calls.append(('POST', path, data))
        if self.delay:
            time.sleep(self.delay)
        return dict(self.post_result) if isinstance(self.post_result, dict) else self.post_result

    def delete(self, path, timeout=None):
        self.calls.append(('DELETE', path, None))
        if self.delay:
            time.sleep(self.delay)
        return dict(self.delete_result) if isinstance(self.delete_result, dict) else self.delete_result


def settle(panel, api, timeout=3.0):
    """等首个刷新真正落到控件上：worker 起来 -> 结果回到主线程 apply。"""
    pump(until=lambda: bool(api.calls), timeout=timeout)
    pump(until=lambda: not panel.requests.busy, timeout=timeout)
    pump(timeout=0.05)
    return panel


def pump(until=None, timeout=3.0):
    """转事件循环，直到 until() 为真或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        APP.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    APP.processEvents()
    return until is None or bool(until())


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            try:
                panel.stop_polling()
            except Exception:
                pass
        pump(timeout=0.05)

    def make(self, list_result=None, post_result=None, delete_result=None, delay=0.0):
        api = FakeApi(list_result, post_result, delete_result, delay)
        panel = AgentBackendPanel(api.get, api.post, api.delete)
        self.panels.append(panel)
        settle(panel, api)
        return api, panel


class TestConstructAndEmpty(PanelCase):
    def test_constructs_offscreen_without_touching_network(self):
        api = FakeApi()
        started = time.time()
        panel = AgentBackendPanel(api.get, api.post, api.delete)
        elapsed = time.time() - started
        self.panels.append(panel)
        self.assertLess(elapsed, 0.8, '构造必须是纯建控件，不能同步打网络')
        self.assertEqual([], api.calls, '构造期不许发请求（首刷放到事件循环里）')
        self.assertEqual([], panel.rows())
        self.assertEqual('agent_backend_empty', panel.empty.objectName())
        settle(panel, api)
        self.assertEqual('GET', api.calls[0][0])
        self.assertEqual('agent/backends', api.calls[0][1])

    def test_empty_state_shows_text_not_blank_page(self):
        api, panel = self.make(list_result=list_payload(rows=[]))
        self.assertEqual([], panel.rows())
        self.assertEqual({}, panel.states())
        self.assertEqual(EMPTY_TEXT, panel.empty_text())
        self.assertTrue(panel.empty_visible())
        self.assertEqual(0, panel.table.rowCount())
        self.assertFalse(panel.table.isVisibleTo(panel))
        self.assertTrue(panel.empty.isVisibleTo(panel))
        self.assertEqual('', panel.error_text())

    def test_rows_render_when_backends_exist(self):
        api, panel = self.make()
        self.assertEqual(['alpha', 'beta', 'gamma'], [row['id'] for row in panel.rows()])
        self.assertEqual(3, panel.table.rowCount())
        self.assertEqual({'alpha': 'idle', 'beta': 'running', 'gamma': 'failed'}, panel.states())
        self.assertFalse(panel.empty.isVisibleTo(panel))
        self.assertTrue(panel.table.isVisibleTo(panel))
        self.assertEqual('运行中', panel.table.item(1, 1).text())
        self.assertEqual('4242', panel.table.item(1, 2).text())
        self.assertEqual('-', panel.table.item(0, 2).text())

    def test_bad_profile_file_keeps_empty_state_and_echoes_code(self):
        api, panel = self.make(list_result={'ok': False, 'code': 'bad_profile_file',
                                            'message': '第 3 行不是合法 JSON'})
        self.assertEqual([], panel.rows())
        self.assertTrue(panel.empty_visible())
        self.assertIn('[bad_profile_file]', panel.error_text())
        self.assertIn('第 3 行不是合法 JSON', panel.error_text())


class TestButtons(PanelCase):
    def test_buttons_follow_selected_state(self):
        api, panel = self.make()
        panel.select('alpha')
        self.assertEqual('alpha', panel.current_id())
        idle = panel.button_state()
        self.assertTrue(idle['start'], 'idle 能启动')
        self.assertFalse(idle['stop'])
        self.assertTrue(idle['delete'])
        self.assertTrue(idle['probe'])
        panel.select('beta')
        running = panel.button_state()
        self.assertEqual('beta', panel.current_id())
        self.assertFalse(running['start'], 'running 不许再启动')
        self.assertTrue(running['stop'])
        self.assertFalse(running['delete'], '运行中不许删除')
        panel.select('gamma')
        failed = panel.button_state()
        self.assertTrue(failed['start'], 'failed 允许重启')
        self.assertFalse(failed['stop'])

    def test_buttons_disabled_without_rows(self):
        api, panel = self.make(list_result=list_payload(rows=[]))
        state = panel.button_state()
        self.assertEqual({'probe': False, 'start': False, 'stop': False,
                          'open': False, 'delete': False}, state)

    def test_status_apply_flips_state_and_buttons(self):
        api, panel = self.make()
        panel.select('alpha')
        self.assertTrue(panel.button_state()['start'])
        panel.apply_status('alpha', {'ok': True, 'state': 'running', 'pid': 999, 'running': True})
        self.assertEqual('running', panel.state_of('alpha'))
        self.assertEqual(999, panel.rows()[0]['pid'])
        self.assertFalse(panel.button_state()['start'])
        self.assertTrue(panel.button_state()['stop'])
        panel.apply_status('alpha', {'ok': True, 'state': 'stopped', 'pid': None, 'running': False})
        self.assertEqual('stopped', panel.state_of('alpha'))
        self.assertEqual(STATE_LABELS['stopped'], panel.table.item(0, 1).text())

    def test_error_payload_on_status_is_echoed(self):
        api, panel = self.make()
        panel.apply_status('alpha', {'ok': False, 'code': 'not_running', 'message': '后端没有在跑'})
        self.assertIn('[not_running]', panel.error_text())


class TestAsync(PanelCase):
    def test_refresh_does_not_block_the_gui_thread(self):
        panel = self.make(delay=0.6)[1]
        panel.apply_list(list_payload(rows=[]))
        self.assertEqual([], panel.rows())
        started = time.time()
        accepted = panel.refresh(force=True)
        elapsed = time.time() - started
        self.assertTrue(accepted)
        self.assertLess(elapsed, 0.2, 'refresh 必须立刻返回，慢活在 worker 线程')
        self.assertEqual([], panel.rows(), '慢请求还没回来时控件不能被阻塞等待')
        self.assertTrue(pump(until=lambda: len(panel.rows()) == 3, timeout=4.0),
                        '结果最终要回到主线程')
        self.assertGreaterEqual(time.time() - started, 0.5, '证明这活确实是异步跑完的')

    def test_poll_timer_interval_and_start_stop(self):
        api, panel = self.make()
        self.assertEqual(POLL_INTERVAL_MS, panel.poll_timer.interval())
        self.assertFalse(panel.poll_timer.isActive(), '没显示时不许轮询')
        panel.start_polling()
        self.assertTrue(panel.poll_timer.isActive())
        panel.stop_polling()
        self.assertFalse(panel.poll_timer.isActive())

    def test_save_profile_posts_and_echoes_error_code(self):
        api, panel = self.make(post_result={'ok': False, 'code': 'bad_profile',
                                           'message': 'command 不能为空'})
        panel.input_id.setText('alpha')
        panel.input_command.setText('')
        panel.input_args.setText('')
        panel.input_workdir.setText('C:/tmp')
        panel.input_timeout.setValue(120.0)
        self.assertTrue(panel.save_profile())
        self.assertTrue(pump(until=lambda: any(call[0] == 'POST' for call in api.calls), timeout=3.0))
        post = [call for call in api.calls if call[0] == 'POST'][0]
        self.assertEqual('agent/backends', post[1])
        self.assertEqual('alpha', post[2]['id'])
        self.assertEqual(120.0, post[2]['profile']['timeout'])
        self.assertTrue(pump(until=lambda: 'bad_profile' in panel.error_text(), timeout=3.0))
        self.assertIn('[bad_profile]', panel.error_text())

    def test_start_action_posts_start_path(self):
        api, panel = self.make()
        panel.select('alpha')
        self.assertTrue(panel.action_start())
        self.assertTrue(pump(until=lambda: any(call[1].endswith('/start') for call in api.calls),
                             timeout=3.0))
        self.assertIn(('POST', 'agent/backends/alpha/start', {}),
                      [(call[0], call[1], call[2] or {}) for call in api.calls])

    def test_shutdown_stops_every_backend_through_the_endpoint(self):
        api, panel = self.make()
        panel.shutdown_backends()
        self.assertTrue(pump(until=lambda: any(call[1].endswith('/shutdown') for call in api.calls),
                             timeout=3.0))
        self.assertIn(('POST', 'agent/backends/shutdown'),
                      [(call[0], call[1]) for call in api.calls])


class TestHelpers(unittest.TestCase):
    def test_split_args_keeps_quoted_argument_together(self):
        self.assertEqual([], split_args(''))
        self.assertEqual([], split_args('   '))
        self.assertEqual(['-c', 'print(1)'], split_args('-c "print(1)"'))
        self.assertEqual(['--workdir', 'C:\\tmp\\ws'], split_args('--workdir C:\\tmp\\ws'))
        self.assertEqual(['-c', 'print(1)', 'x'], split_args('-c "print(1)"   x'))

    def test_error_of_classifies_payloads(self):
        self.assertEqual(('offline', '控制台后台没有响应'), error_of({}))
        self.assertEqual(('offline', '控制台后台没有响应'), error_of(None))
        self.assertIsNone(error_of({'ok': True, 'backends': []}))
        self.assertEqual(('bad_profile', '空命令'),
                         error_of({'ok': False, 'code': 'bad_profile', 'message': '空命令'}))
        self.assertEqual(('internal', '网络断了'), error_of({'success': False, 'message': '网络断了'}))

    def test_state_of_prefers_explicit_state(self):
        self.assertEqual('running', state_of({'state': 'running', 'running': True}))
        self.assertEqual('running', state_of({'running': True}))
        self.assertEqual('idle', state_of({}))
        self.assertEqual('stopped', state_of({'state': 'stopped'}))


if __name__ == '__main__':
    unittest.main()

