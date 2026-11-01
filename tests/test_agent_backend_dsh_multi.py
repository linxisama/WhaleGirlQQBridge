"""DSH 多 profile 自动接入：批量、单点隔离、幂等、并发写、连接体检。"""
import contextlib
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from src import agent_backend_api as api
from src import dsh_memory_bridge as bridge

REPO = 'D:/repo'
PYTHON = 'D:/repo/.venv-qq/Scripts/python.exe'
SCRIPTS = {'memory': 'D:/repo/tools/whale_memory_mcp.py',
           'bot': 'D:/repo/tools/whale_bot_mcp.py',
           'tools': 'D:/repo/tools/whale_tools_mcp.py'}
ENTRY_IDS = [bridge.ENTRY_ID, bridge.CONTROL_ENTRY_ID, bridge.TOOLS_ENTRY_ID]
BRIDGE_FILE = str(Path(__file__).resolve().parents[1] / 'src' / 'dsh_memory_bridge.py')


class FakeApp:
    def __init__(self, root):
        self.state = SimpleNamespace(bot=SimpleNamespace(root=str(root)))


class DshMultiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.app = FakeApp(self.data)
        self.home = self.root / '.dsh'
        (self.home / 'profiles').mkdir(parents=True)

    def enter(self, home=None, frozen=True):
        """把 DSH 根指到临时目录、假装安装版：真实 ~/.dsh 一次都不碰。"""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        target = self.home if home is None else home
        for patcher in (mock.patch.object(api, 'dsh_home',
                                          lambda override=None: str(target)),
                        mock.patch.object(api, 'frozen_mode', lambda: frozen),
                        mock.patch.object(api, 'dsh_repo_root', return_value=REPO),
                        mock.patch.object(api, 'dsh_scripts', return_value=dict(SCRIPTS)),
                        mock.patch.object(api, 'dsh_python', return_value=PYTHON),
                        mock.patch.object(api, 'dsh_bundled_exe', return_value=''),
                        mock.patch.object(api, 'dsh_python_check',
                                          return_value={'ok': True})):
            stack.enter_context(patcher)
        return stack

    def make_profile(self, name, text='[]\n'):
        target = self.home / 'profiles' / name
        target.mkdir(parents=True, exist_ok=True)
        (target / 'cordis.patch.yml').write_text(text, encoding='utf-8')
        return target

    def install(self, name):
        """按 api 的写法真落一次三条登记（绕开 dsh_link_state，用来造「已有」态）。"""
        return bridge.install_all(str(self.home), PYTHON, SCRIPTS['memory'],
                                  str(self.data), SCRIPTS['bot'], '',
                                  profile=name, tools_script=SCRIPTS['tools'])

    def state(self, name):
        return bridge.status(str(self.home), profile=name)

    def backups(self, name):
        return sorted(path.name for path in (self.home / 'profiles' / name)
                      .glob('cordis.patch.yml.bak-whale-memory-*'))

    def yaml_text(self, name):
        return (self.home / 'profiles' / name / 'cordis.patch.yml').read_text(encoding='utf-8')


class DshAutoConnectAllTests(DshMultiBase):
    def test_mixed_profiles_link_each_independently(self):
        self.make_profile('desktop')
        self.install('desktop')
        self.make_profile('web')
        self.make_profile('bad', 'not: [a, list]\n')
        self.enter()
        payload = api.dsh_auto_connect_all(self.app)
        rows = {row['profile']: row for row in payload['results']}
        self.assertEqual(['bad', 'desktop', 'web'], sorted(rows))
        self.assertTrue(rows['desktop']['skipped'])
        self.assertEqual('already', rows['desktop']['reason'])
        self.assertFalse(rows['web']['skipped'])
        self.assertTrue(rows['web']['ok'])
        self.assertTrue(rows['web']['changed'])
        self.assertFalse(rows['bad']['ok'])
        self.assertEqual('bad_dsh_profile', rows['bad']['reason'])
        # 好 profile 照样接上，坏 profile 只算自己那一条。
        self.assertEqual(2, payload['attempted'])
        self.assertEqual(1, payload['linked'])
        self.assertEqual(1, payload['skipped'])
        self.assertEqual(1, payload['failed'])
        self.assertFalse(payload['ok'])
        web = self.state('web')
        self.assertTrue(web['installed'])
        self.assertTrue(web['control_installed'])
        self.assertTrue(web['tools_installed'])
        self.assertTrue(web['reconnect_ok'])
        # 坏 profile 原样不动，没被写成半份。
        self.assertEqual('not: [a, list]\n', self.yaml_text('bad'))

    def test_exception_in_one_profile_is_isolated(self):
        self.make_profile('desktop')
        self.install('desktop')
        self.make_profile('web')
        self.enter()
        real = api.dsh_auto_connect

        def fake(app, profile='desktop'):
            if profile == 'web':
                raise RuntimeError('boom')
            return real(app, profile)

        with mock.patch.object(api, 'dsh_auto_connect', fake):
            payload = api.dsh_auto_connect_all(self.app)
        rows = {row['profile']: row for row in payload['results']}
        self.assertTrue(rows['desktop']['ok'])
        self.assertEqual('already', rows['desktop']['reason'])
        self.assertFalse(rows['web']['ok'])
        self.assertEqual('error', rows['web']['reason'])
        self.assertIn('boom', rows['web']['message'])
        self.assertFalse(payload['ok'])
        self.assertIn('web', payload['message'])

    def test_source_mode_touches_no_dsh_home(self):
        self.make_profile('desktop')
        before = self.yaml_text('desktop')
        self.enter(frozen=False)
        payload = api.dsh_auto_connect_all(self.app)
        self.assertTrue(payload['ok'])
        self.assertEqual('dev_mode', payload['reason'])
        self.assertEqual(0, payload['attempted'])
        self.assertEqual(0, payload['linked'])
        self.assertTrue(all(row['skipped'] for row in payload['results']))
        self.assertEqual(before, self.yaml_text('desktop'))
        self.assertEqual([], self.backups('desktop'))

    def test_no_profiles_is_quiet(self):
        self.enter()
        payload = api.dsh_auto_connect_all(self.app)
        self.assertTrue(payload['ok'])
        self.assertEqual('no_dsh', payload['reason'])
        self.assertEqual([], payload['results'])
        self.assertEqual(0, payload['attempted'])

    def test_disabled_auto_connect_skips_every_profile(self):
        self.make_profile('desktop')
        self.assertTrue(api.dsh_set_auto(self.app, False))
        self.enter()
        payload = api.dsh_auto_connect_all(self.app)
        self.assertTrue(payload['ok'])
        self.assertEqual('disabled', payload['reason'])
        self.assertEqual(0, payload['attempted'])
        self.assertEqual(['desktop'],
                         [row['profile'] for row in payload['results']])
        self.assertTrue(payload['results'][0]['skipped'])
        self.assertFalse(self.state('desktop')['installed'])

    def test_second_run_rewrites_nothing(self):
        self.make_profile('desktop')
        self.enter()
        first = api.dsh_auto_connect_all(self.app)
        self.assertEqual(1, first['linked'])
        after_first = self.yaml_text('desktop')
        first_backups = self.backups('desktop')
        self.assertEqual(1, len(first_backups))
        second = api.dsh_auto_connect_all(self.app)
        self.assertEqual(0, second['linked'])
        self.assertEqual(1, second['skipped'])
        self.assertEqual('already', second['results'][0]['reason'])
        self.assertEqual(after_first, self.yaml_text('desktop'))
        self.assertEqual(first_backups, self.backups('desktop'))

    def test_concurrent_runs_write_yaml_once(self):
        self.make_profile('desktop')
        self.make_profile('web')
        self.enter()
        real_status = bridge.status

        def locked_status(dsh_home, profile='desktop'):
            # 读也排在锁里：模拟两个控制台进程，读不会撞上别人的原子替换。
            with bridge.profile_lock(bridge.profile_path(dsh_home, profile)):
                return real_status(dsh_home, profile=profile)

        payloads = []

        def run():
            payloads.append(api.dsh_auto_connect_all(self.app))

        with mock.patch.object(bridge, 'status', locked_status):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(2, len(payloads))
        for name in ('desktop', 'web'):
            state = self.state(name)
            self.assertTrue(state['installed'])
            self.assertTrue(state['control_installed'])
            self.assertTrue(state['tools_installed'])
            self.assertTrue(state['reconnect_ok'])
            self.assertIsInstance(yaml.safe_load(self.yaml_text(name)), list)
            # 并发两轮只真写一次：每次落盘都会留一个备份。
            self.assertEqual(1, len(self.backups(name)))
        changed = sum(1 for payload in payloads for row in payload['results']
                      if row.get('changed'))
        self.assertEqual(2, changed)


class DshConnectionReportTests(DshMultiBase):
    def test_report_after_real_link(self):
        self.make_profile('desktop')
        self.enter()
        linked = api.dsh_link_state(self.app, 'link', 'desktop')
        self.assertTrue(linked['ok'])
        report = api.dsh_connection_report(self.app)
        self.assertTrue(report['ok'])
        self.assertEqual('desktop', report['profile'])
        self.assertEqual(str(self.home), report['host'])
        self.assertTrue(report['profile_exists'])
        self.assertEqual(str(self.home / 'profiles' / 'desktop' / 'cordis.patch.yml'),
                         report['path'])
        self.assertTrue(report['installed'])
        self.assertEqual([], report['missing'])
        self.assertEqual(ENTRY_IDS, [row['id'] for row in report['entries']])
        self.assertTrue(all(row['installed'] for row in report['entries']))
        self.assertEqual({'mcp__whale_memory__*', 'mcp__whale_bot__*', 'mcp__whale_tools__*'},
                         set(report['tools'].values()))
        entry_tools = {row['id']: row['tools'] for row in report['entries']}
        self.assertEqual('mcp__whale_memory__*', entry_tools[bridge.ENTRY_ID])
        self.assertEqual('mcp__whale_bot__*', entry_tools[bridge.CONTROL_ENTRY_ID])
        self.assertEqual('mcp__whale_tools__*', entry_tools[bridge.TOOLS_ENTRY_ID])
        self.assertTrue(report['reconnect_ok'])
        self.assertTrue(report['commands_current'])
        # 本次运行刚写过 yaml：DSH 里已经开着的会话还是旧配置。
        self.assertTrue(report['restart_required'])
        self.assertTrue(report['config_changed_at'])
        self.assertTrue(report['new_session_ready'])
        self.assertEqual(['restart_required'], report['issues'])
        self.assertIn('重启 DSH', report['hint'])
        self.assertIn('新建会话', report['hint'])

    def test_old_config_change_does_not_ask_restart(self):
        self.make_profile('desktop')
        self.enter()
        api.dsh_link_state(self.app, 'link', 'desktop')
        data = api.dsh_config_data(self.app)
        data['config_changed_at'] = {'desktop': 1.0}
        self.assertTrue(api.dsh_write_config(self.app, data))
        report = api.dsh_connection_report(self.app)
        self.assertFalse(report['restart_required'])
        self.assertTrue(report['new_session_ready'])
        self.assertIn('继承', report['hint'])
        self.assertNotIn('重启 DSH 后', report['hint'])

    def test_report_lists_missing_entries(self):
        self.make_profile('web')
        self.enter()
        report = api.dsh_connection_report(self.app, 'web')
        self.assertTrue(report['ok'])
        self.assertEqual('web', report['profile'])
        self.assertFalse(report['installed'])
        self.assertEqual(ENTRY_IDS, report['missing'])
        self.assertFalse(report['new_session_ready'])
        self.assertIn('missing_entries', report['issues'])
        self.assertIn('还缺', report['hint'])

    def test_report_on_broken_yaml(self):
        self.make_profile('bad', 'not: [a, list]\n')
        self.enter()
        report = api.dsh_connection_report(self.app, 'bad')
        self.assertTrue(report['ok'])
        self.assertFalse(report['new_session_ready'])
        self.assertIn('bad_yaml', report['issues'])
        self.assertIn('读不出来', report['hint'])

    def test_report_never_writes_or_runs_external(self):
        self.make_profile('desktop')
        self.enter()
        self.install('desktop')
        profile_dir = self.home / 'profiles' / 'desktop'
        before = sorted(path.name for path in profile_dir.iterdir())
        config_before = api.dsh_config_data(self.app)
        with mock.patch.object(bridge, 'install_all',
                              side_effect=AssertionError('体检不许写 yaml')), \
                mock.patch.object(bridge, 'install',
                                  side_effect=AssertionError('体检不许写 yaml')), \
                mock.patch.object(bridge, 'remove',
                                  side_effect=AssertionError('体检不许写 yaml')), \
                mock.patch.object(api, 'dsh_python_check',
                                  side_effect=AssertionError('体检不许跑外部命令')):
            report = api.dsh_connection_report(self.app)
        self.assertTrue(report['ok'])
        self.assertTrue(report['new_session_ready'])
        self.assertTrue(report['reconnect_ok'])
        self.assertEqual(before, sorted(path.name for path in profile_dir.iterdir()))
        self.assertEqual(config_before, api.dsh_config_data(self.app))

    def test_bad_profile_name_is_rejected(self):
        report = api.dsh_connection_report(self.app, '../evil')
        self.assertFalse(report['ok'])
        self.assertEqual('bad_profile', report['code'])


class ProfileLockTests(DshMultiBase):
    CHILD = '''\
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('bridge', sys.argv[1])
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
try:
    with bridge.profile_lock(bridge.profile_path(sys.argv[2], 'desktop'),
                             timeout=float(sys.argv[3])):
        print('ACQUIRED')
except OSError:
    print('TIMEOUT')
'''

    def child(self, timeout):
        done = subprocess.run([sys.executable, '-c', self.CHILD, BRIDGE_FILE,
                               str(self.home), str(timeout)],
                              capture_output=True, text=True, timeout=60)
        return done.stdout.strip()

    def test_lock_blocks_another_process(self):
        self.make_profile('desktop')
        target = bridge.profile_path(str(self.home), 'desktop')
        with bridge.profile_lock(target):
            self.assertIn('TIMEOUT', self.child(0.4))
        self.assertIn('ACQUIRED', self.child(5))


if __name__ == '__main__':
    unittest.main()
