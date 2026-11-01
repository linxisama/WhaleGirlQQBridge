"""网关账号目录要沿用用户默认那套插件库与主人绑定，不然新账号等于空商店。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import qq_app
from src.qq_app import QQRuntime


class PluginDirInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.userdata = base / 'userdata'
        self.shared = self.userdata / 'data' / 'qq-only'
        self.shared.mkdir(parents=True)
        (self.shared / 'plugin_settings.json').write_text('{}', encoding='utf-8')
        patcher = mock.patch.object(qq_app, 'DATA_ROOT', self.userdata)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 默认这台机器没装控制台；需要时用 _with_installed() 单独打开。
        self.installed = base / 'localappdata' / 'WhaleGirlQQ'
        self._real_installed = qq_app.installed_data_root
        install_patch = mock.patch.object(qq_app, 'installed_data_root',
                                          lambda: base / 'no-install')
        install_patch.start()
        self.addCleanup(install_patch.stop)
        self.account = base / 'accounts' / 'qq-10001' / 'bot'
        self.account.mkdir(parents=True)

    @staticmethod
    def _close(runtime):
        """Runtime.close() 是 async；测试里只要关掉 sqlite 句柄，否则临时目录删不掉。"""
        try:
            runtime.db.close()
        except Exception:
            pass

    def _gateway(self):
        """摆一份网关任务单：bot_launcher 写的 launch.json 一定带 root/source。"""
        (self.account / 'launch.json').write_text(
            json.dumps({'source': str(self.tmp.name), 'root': str(self.account), 'port': 1}),
            encoding='utf-8')

    def _runtime(self, **kwargs):
        runtime = QQRuntime(root=self.account, db_path=self.account / 'test.sqlite', **kwargs)
        self.addCleanup(self._close, runtime)
        return runtime

    def test_fresh_gateway_account_inherits_user_plugin_store(self):
        self._gateway()
        runtime = self._runtime()
        self.assertEqual(runtime.plugins.data_dir.resolve(), self.shared.resolve())

    def test_account_with_own_store_keeps_it(self):
        self._gateway()
        own = self.account / 'data' / 'qq-only'
        own.mkdir(parents=True)
        (own / 'qqagent_host').mkdir()
        runtime = self._runtime()
        self.assertEqual(runtime.plugins.data_dir.resolve(), own.resolve())

    def test_runtime_files_alone_still_inherit(self):
        # prepare 拷的 plugin_settings.json、加上 logs/backups/messages，都不算装过插件。
        self._gateway()
        own = self.account / 'data' / 'qq-only'
        (own / 'logs').mkdir(parents=True)
        (own / 'plugin_settings.json').write_text('{}', encoding='utf-8')
        (own / 'plugin_state').mkdir()
        (own / 'messages.sqlite3').write_text('', encoding='utf-8')
        runtime = self._runtime()
        self.assertEqual(runtime.plugins.data_dir.resolve(), self.shared.resolve())

    def test_random_launch_json_is_not_a_gateway_account(self):
        # 别处随手放的 launch.json（没有 root/source）不该把插件库指到用户目录去。
        (self.account / 'launch.json').write_text('{}', encoding='utf-8')
        self._with_installed()
        runtime = self._runtime()
        self.assertEqual(runtime.plugins.data_dir.resolve(),
                         (self.account / 'data' / 'qq-only').resolve())

    def test_plain_root_without_launch_json_is_untouched(self):
        runtime = self._runtime()
        self.assertEqual(runtime.plugins.data_dir.resolve(),
                         (self.account / 'data' / 'qq-only').resolve())

    def test_explicit_data_root_wins(self):
        self._gateway()
        explicit = Path(self.tmp.name) / 'explicit'
        runtime = self._runtime(data_root=explicit)
        self.assertEqual(runtime.plugins.data_dir.resolve(), explicit.resolve())

    def test_owner_binding_falls_back_to_user_data_dir(self):
        (self.userdata / 'agent_admin.json').write_text(
            json.dumps({'admin': '2019117802', 'bound_at': 1}), encoding='utf-8')
        runtime = self._runtime()
        self.assertEqual(runtime.owner_ids(), {'2019117802'})

    def _with_installed(self):
        """模拟「机器上装过控制台」，插件库在 %LOCALAPPDATA%\\WhaleGirlQQ 那套。"""
        installed_store = self.installed / 'data' / 'qq-only'
        installed_store.mkdir(parents=True, exist_ok=True)
        patch = mock.patch.object(qq_app, 'installed_data_root', lambda: self.installed)
        patch.start()
        self.addCleanup(patch.stop)
        return installed_store

    def test_source_mode_account_inherits_installed_store(self):
        # 网关用源码目录跑后端时，DATA_ROOT 是仓库的空插件库，插件其实装在安装版目录里。
        self._gateway()
        installed_store = self._with_installed()
        (installed_store / 'qqagent_extensions').mkdir(parents=True)
        runtime = self._runtime()
        self.assertEqual(runtime.plugins.data_dir.resolve(), installed_store.resolve())

    def test_user_store_wins_over_installed_store(self):
        # 用户默认那套（DATA_ROOT）自己装过插件时，不要越位去找安装版。
        self._gateway()
        (self.shared / 'qqagent_host').mkdir()
        installed_store = self._with_installed()
        (installed_store / 'qqagent_host').mkdir(parents=True)
        runtime = self._runtime()
        self.assertEqual(runtime.plugins.data_dir.resolve(), self.shared.resolve())

    def test_installed_store_without_plugins_is_ignored(self):
        self._gateway()
        installed_store = self._with_installed()
        (installed_store / 'plugin_settings.json').write_text('{}', encoding='utf-8')
        runtime = self._runtime()
        self.assertEqual(runtime.plugins.data_dir.resolve(), self.shared.resolve())

    def test_installed_root_survives_redirected_localappdata(self):
        # 网关的 bot_worker 把 LOCALAPPDATA 改指到账号文件夹里；这时要从 USERPROFILE 拼回来。
        profile = Path(self.tmp.name) / 'profile'
        real = profile / 'AppData' / 'Local' / 'WhaleGirlQQ'
        real.mkdir(parents=True)
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(Path(self.tmp.name) / 'nowhere'),
                                         'USERPROFILE': str(profile)}):
            with mock.patch.object(qq_app, 'installed_data_root', self._real_installed):
                self.assertEqual(qq_app.installed_data_root(), real)

    def test_owner_binding_falls_back_to_installed_dir(self):
        # 网关账号目录、仓库根都没有绑定，但安装版目录里绑过主人。
        self.installed.mkdir(parents=True, exist_ok=True)
        (self.installed / 'agent_admin.json').write_text(
            json.dumps({'admin': '2019117802', 'bound_at': 1}), encoding='utf-8')
        self._with_installed()
        runtime = self._runtime()
        self.assertEqual(runtime.owner_ids(), {'2019117802'})

    def test_account_owner_binding_still_wins(self):
        (self.userdata / 'agent_admin.json').write_text(
            json.dumps({'admin': '2019117802'}), encoding='utf-8')
        (self.account / 'agent_admin.json').write_text(
            json.dumps({'admin': '123456'}), encoding='utf-8')
        runtime = self._runtime()
        self.assertIn('123456', runtime.owner_ids())
        self.assertNotIn('2019117802', runtime.owner_ids())


if __name__ == '__main__':
    unittest.main()
