"""DSH 自动接入：配置开关、免手动 link、以及不碰真实 ~/.dsh。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import agent_backend_api as api


class FakeApp:
    def __init__(self, root):
        self.state = SimpleNamespace(bot=SimpleNamespace(root=str(root)))


class DshAutoConnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.app = FakeApp(self.data)
        self.home = self.root / '.dsh'
        self.profile_dir = self.home / 'profiles' / 'desktop'
        self.profile_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_roundtrip_keeps_both_keys(self):
        self.assertTrue(api.dsh_save_repo(self.app, 'D:/qq机器人5.0'))
        self.assertTrue(api.dsh_set_auto(self.app, False))
        saved = json.loads((self.data / api.DSH_REPO_CONFIG).read_text(encoding='utf-8'))
        self.assertEqual('D:/qq机器人5.0', saved['repo_root'])
        self.assertFalse(saved['auto_connect'])
        self.assertFalse(api.dsh_auto_enabled(self.app))
        # 再存一次仓库根，不能把自动接入开关冲掉。
        self.assertTrue(api.dsh_save_repo(self.app, 'D:/other'))
        saved = json.loads((self.data / api.DSH_REPO_CONFIG).read_text(encoding='utf-8'))
        self.assertFalse(saved['auto_connect'])
        self.assertEqual('D:/other', api.dsh_saved_repo(self.app))

    def test_missing_dsh_profile_is_skipped(self):
        self.home.joinpath('profiles', 'desktop').rmdir()
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state') as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result['skipped'])
        self.assertEqual('no_dsh', result['reason'])
        link.assert_not_called()

    def test_disabled_auto_connect_is_skipped(self):
        api.dsh_set_auto(self.app, False)
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state') as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result['skipped'])
        self.assertEqual('disabled', result['reason'])
        link.assert_not_called()

    def test_source_mode_is_skipped(self):
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: False), \
                mock.patch.object(api, 'dsh_link_state') as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result['skipped'])
        self.assertEqual('dev_mode', result['reason'])
        link.assert_not_called()

    def test_ready_profile_is_left_alone(self):
        status = {'ok': True, 'tools_installed': True, 'reconnect_ok': True,
                  'python': 'D:/repo/.venv-qq/Scripts/python.exe',
                  'scripts': {'bot': 'D:/repo/tools/whale_bot_mcp.py',
                              'tools': 'D:/repo/tools/whale_tools_mcp.py'},
                  'control_command': 'D:/repo/.venv-qq/Scripts/python.exe',
                  'control_script': 'D:/repo/tools/whale_bot_mcp.py',
                  'tools_command': 'D:/repo/.venv-qq/Scripts/python.exe',
                  'tools_script': 'D:/repo/tools/whale_tools_mcp.py'}
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state', return_value=status) as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result['skipped'])
        self.assertEqual('already', result['reason'])
        self.assertEqual(1, link.call_count)
        self.assertEqual(('status', 'desktop'), link.call_args.args[1:])

    def test_stale_commands_are_relinked(self):
        # 条目齐全、重连够，但命令还指着搬家前的解释器：必须重写。
        status = {'ok': True, 'tools_installed': True, 'reconnect_ok': True,
                  'python': 'D:/new/.venv-qq/Scripts/python.exe',
                  'scripts': {'bot': 'D:/new/tools/whale_bot_mcp.py',
                              'tools': 'D:/new/tools/whale_tools_mcp.py'},
                  'control_command': 'D:/old/.venv-qq/Scripts/python.exe',
                  'control_script': 'D:/old/tools/whale_bot_mcp.py',
                  'tools_command': 'D:/old/.venv-qq/Scripts/python.exe',
                  'tools_script': 'D:/old/tools/whale_tools_mcp.py'}
        linked = {'ok': True, 'changed': True, 'profile': 'desktop'}
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state', side_effect=[status, linked]) as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result.get('auto'))
        self.assertEqual(['status', 'link'], [c.args[1] for c in link.call_args_list])

    def test_missing_command_fields_are_not_stale(self):
        # 老版本 status 不带 python/scripts：只能当未知，不能每次启动都重写 yaml。
        status = {'ok': True, 'tools_installed': True, 'reconnect_ok': True}
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state', return_value=status) as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result['skipped'])
        self.assertEqual('already', result['reason'])
        self.assertEqual(1, link.call_count)

    def test_incomplete_profile_is_linked(self):
        status = {'ok': True, 'tools_installed': False, 'reconnect_ok': False}
        linked = {'ok': True, 'changed': True, 'profile': 'desktop'}
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state', side_effect=[status, linked]) as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result['ok'])
        self.assertTrue(result['auto'])
        self.assertEqual(['status', 'link'], [c.args[1] for c in link.call_args_list])

    def test_link_failure_is_surfaced(self):
        status = {'ok': True, 'tools_installed': False, 'reconnect_ok': False}
        failed = {'ok': False, 'code': 'io', 'message': '写不进'}
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state', side_effect=[status, failed]):
            result = api.dsh_auto_connect(self.app)
        self.assertFalse(result['ok'])
        self.assertEqual('io', result['code'])

    def test_no_source_repo_is_skipped_quietly(self):
        status = {'ok': False, 'code': 'dsh_needs_source', 'message': '缺仓库'}
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state', return_value=status) as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result['skipped'])
        self.assertEqual('no_source', result['reason'])
        self.assertEqual(1, link.call_count)

    def test_failed_link_self_check_is_retried(self):
        api.dsh_set_link_ok(self.app, False)
        status = {'ok': True, 'tools_installed': True, 'reconnect_ok': True,
                  'python': 'D:/repo/.venv-qq/Scripts/python.exe',
                  'scripts': {'bot': 'D:/repo/tools/whale_bot_mcp.py',
                              'tools': 'D:/repo/tools/whale_tools_mcp.py'},
                  'control_command': 'D:/repo/.venv-qq/Scripts/python.exe',
                  'control_script': 'D:/repo/tools/whale_bot_mcp.py',
                  'tools_command': 'D:/repo/.venv-qq/Scripts/python.exe',
                  'tools_script': 'D:/repo/tools/whale_tools_mcp.py'}
        linked = {'ok': True, 'changed': True, 'profile': 'desktop'}
        with mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)), \
                mock.patch.object(api, 'frozen_mode', lambda: True), \
                mock.patch.object(api, 'dsh_link_state', side_effect=[status, linked]) as link:
            result = api.dsh_auto_connect(self.app)
        self.assertTrue(result.get('auto'))
        self.assertEqual(['status', 'link'], [c.args[1] for c in link.call_args_list])

    def test_real_link_disables_and_enables_auto(self):
        # 用真实配置函数验证 link/unlink 会写开关；这里只替换桥的落盘动作。
        self.assertTrue(api.dsh_set_auto(self.app, False))
        with mock.patch.object(api.dsh_memory_bridge, 'install_all',
                              return_value={'changed': True}), \
                mock.patch.object(api.dsh_memory_bridge, 'status',
                                  return_value={'installed': True, 'control_installed': True,
                                                'tools_installed': True, 'reconnect_ok': True}), \
                mock.patch.object(api, 'dsh_repo_root', return_value='D:/repo'), \
                mock.patch.object(api, 'dsh_scripts', return_value={'memory': 'm.py', 'bot': 'b.py', 'tools': 't.py'}), \
                mock.patch.object(api, 'dsh_python', return_value='python.exe'), \
                mock.patch.object(api, 'dsh_home', lambda override=None: str(self.home)):
            linked = api.dsh_link_state(self.app, 'link', 'desktop')
            self.assertTrue(api.dsh_auto_enabled(self.app))
            self.assertTrue(linked['reconnect_ok'])
            self.assertTrue(api.dsh_link_ok(self.app))
            with mock.patch.object(api.dsh_memory_bridge, 'remove',
                                   return_value={'removed': True}):
                api.dsh_link_state(self.app, 'unlink', 'desktop')
            self.assertFalse(api.dsh_auto_enabled(self.app))
            self.assertFalse(api.dsh_link_ok(self.app))


if __name__ == '__main__':
    unittest.main()
