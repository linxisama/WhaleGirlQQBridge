"""插件安装链回归：plugin.json 预检、安装后立即启用、批量导入启用。

覆盖两个已实测的缺陷：
1. 第三方插件（config_schema 里没有 enabled）装完就启用会抛
   "unknown setting field 'enabled'"；
2. 预检只读 entry/main/id，结构性错误全放过，等安装时才炸。
"""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src import plugin_installer as pi
from src.plugin_install_service import finish_install, finish_batch
from src.plugins.manager import PluginManager

# 最小第三方插件：config_schema 里**故意没有** enabled。内置 63 个插件都自带 enabled，
# 所以只有这种第三方插件才踩得到"安装后立即启用"的缺陷。
TINY_CODE = '''from src.plugins import PluginBase, PluginMeta


class Plugin(PluginBase):
    META = PluginMeta(id='probe_tiny', name='第三方探针',
                      description='没有 enabled 字段的最小第三方插件')

    async def on_message(self, ctx):
        return None
'''

FULL_MANIFEST = {'id': 'probe_tiny', 'name': '第三方探针',
                 'version': '1.0', 'entry': 'payload.py'}


def make_zip(files) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class ManifestPrecheckTests(unittest.TestCase):
    """预检必须把 plugin.json 的结构性问题报进 problems（界面红字）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def package(self, files, name='pkg.zip') -> Path:
        target = self.dir / name
        target.write_bytes(make_zip(files))
        return target

    def inspect(self, files):
        return pi.inspect_local(self.package(files))

    def test_good_manifest_passes(self):
        info = self.inspect({'plugin.json': json.dumps(FULL_MANIFEST),
                             'payload.py': TINY_CODE})
        self.assertEqual(info['problems'], [])
        self.assertTrue(info['ok'])
        self.assertEqual(info['plugin_id'], 'probe_tiny')

    def test_missing_name_and_version_is_notice_only(self):
        info = self.inspect({'plugin.json': json.dumps({'entry': 'payload.py'}),
                             'payload.py': TINY_CODE})
        # 缺 name/version 只是提醒（界面红字），不挡安装。
        self.assertEqual(info['problems'], [])
        self.assertTrue(info['ok'])
        joined = '；'.join(info['notices'])
        self.assertIn('缺少必填字段 name', joined)
        self.assertIn('缺少必填字段 version', joined)

    def test_missing_entry_is_notice_only(self):
        info = self.inspect({'plugin.json': json.dumps({'name': 'n', 'version': '1.0'}),
                             'payload.py': TINY_CODE})
        self.assertEqual(info['problems'], [])
        self.assertTrue(info['ok'])
        self.assertTrue(any('缺少必填字段 entry' in p for p in info['notices']))

    def test_entry_pointing_to_missing_file_reported(self):
        manifest = dict(FULL_MANIFEST, entry='absent.py')
        info = self.inspect({'plugin.json': json.dumps(manifest), 'payload.py': TINY_CODE})
        self.assertFalse(info['ok'])
        self.assertTrue(any('入口不存在' in p and 'absent.py' in p
                            for p in info['problems']), info['problems'])

    def test_nested_entry_resolved_inside_package(self):
        manifest = dict(FULL_MANIFEST, entry='payload.py')
        info = self.inspect({'pkg/plugin.json': json.dumps(manifest),
                             'pkg/payload.py': TINY_CODE})
        self.assertEqual(info['problems'], [])
        self.assertTrue(info['ok'])

    def test_two_manifests_reported(self):
        info = self.inspect({
            'plugin.json': json.dumps(FULL_MANIFEST),
            'pkg/plugin.json': json.dumps(dict(FULL_MANIFEST, name='另一份')),
            'payload.py': TINY_CODE,
        })
        self.assertFalse(info['ok'])
        self.assertTrue(any('多份 plugin.json' in p for p in info['problems']),
                        info['problems'])

    def test_zero_manifest_with_several_plugins_reported(self):
        info = self.inspect({'alpha.py': TINY_CODE, 'beta.py': TINY_CODE})
        self.assertFalse(info['ok'])
        self.assertTrue(any('没有 plugin.json' in p for p in info['problems']),
                        info['problems'])

    def test_zero_manifest_single_source_still_allowed(self):
        # 单文件包历来能装（readme + 一个 .py 的市场包很常见），不要一刀切。
        info = self.inspect({'readme.md': 'hi', 'my_plugin.py': TINY_CODE})
        self.assertEqual(info['problems'], [])
        self.assertTrue(info['ok'])
        self.assertEqual(info['plugin_id'], 'probe_tiny')

    def test_invalid_id_and_wrong_types_reported(self):
        info = self.inspect({
            'plugin.json': json.dumps({'id': 'Bad-ID', 'name': 5,
                                       'version': '', 'entry': 'payload.py'}),
            'payload.py': TINY_CODE,
        })
        self.assertFalse(info['ok'])
        joined = '；'.join(info['problems'])
        self.assertIn('id 不合法', joined)
        self.assertIn('name 必须是非空字符串', joined)
        self.assertIn('version 必须是非空字符串', joined)

    def test_install_refuses_entry_outside_package(self):
        manifest = dict(FULL_MANIFEST, entry='absent.py')
        target = self.package({'plugin.json': json.dumps(manifest),
                               'payload.py': TINY_CODE})
        plugins_dir = self.dir / 'plugins'
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(target, plugins_dir, confirmed=True)
        self.assertIn('入口不存在', str(caught.exception))
        self.assertEqual(list(plugins_dir.glob('*.py')) if plugins_dir.is_dir() else [], [])

    def test_install_refuses_two_manifests(self):
        target = self.package({
            'plugin.json': json.dumps(FULL_MANIFEST),
            'pkg/plugin.json': json.dumps(FULL_MANIFEST),
            'payload.py': TINY_CODE,
        })
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(target, self.dir / 'plugins', confirmed=True)
        self.assertIn('多份 plugin.json', str(caught.exception))

    def test_minimal_manifest_still_installable(self):
        # 只有 entry 的极简清单（桌面自检 smoke 就是这么造的）必须在预检里标红，
        # 但安装本身不能被这次收紧弄坏，否则老包和新包一起装不上。
        target = self.package({'plugin.json': json.dumps({'entry': 'payload.py'}),
                               'payload.py': TINY_CODE})
        info = pi.inspect_local(target)
        self.assertEqual(info['problems'], [])
        self.assertTrue(info['notices'])
        plugins_dir = self.dir / 'plugins'
        result = pi.install_from_file(target, plugins_dir, confirmed=True)
        self.assertEqual(result['plugin_id'], 'probe_tiny')
        self.assertTrue((plugins_dir / 'probe_tiny.py').is_file())

    def test_blocking_package_keeps_problems_and_ok_false(self):
        manifest = dict(FULL_MANIFEST, entry='absent.py')
        info = self.inspect({'plugin.json': json.dumps(manifest), 'payload.py': TINY_CODE})
        self.assertFalse(info['ok'])
        self.assertTrue(any('入口不存在' in p for p in info['problems']), info['problems'])
        self.assertEqual(info['notices'], [])

    def test_pick_from_archive_without_notices_out_keeps_old_behaviour(self):
        problems = []
        pi._pick_from_archive(make_zip({'plugin.json': json.dumps({'entry': 'payload.py'}),
                                        'payload.py': TINY_CODE}), None, problems)
        self.assertTrue(any('缺少必填字段 name' in p for p in problems), problems)

    def test_pick_from_archive_notices_out_splits_notices(self):
        problems, notices = [], []
        pi._pick_from_archive(make_zip({'plugin.json': json.dumps({'entry': 'payload.py'}),
                                        'payload.py': TINY_CODE}), None, problems, notices)
        self.assertEqual(problems, [])
        self.assertTrue(any('缺少必填字段 name' in p for p in notices), notices)

    def test_inspect_source_exposes_notices(self):
        data = make_zip({'plugin.json': json.dumps({'entry': 'payload.py'}),
                         'payload.py': TINY_CODE})
        with patch.object(pi, 'resolve_source', return_value=('archive', 'https://e/x.zip')), \
                patch.object(pi, '_fetch', return_value=data):
            info = pi.inspect_source('https://e/x.zip')
        self.assertEqual(info['problems'], [])
        self.assertTrue(info['notices'])
        self.assertTrue(info['ok'])

    def test_inspect_local_reports_problems_and_notices(self):
        info = self.inspect({'plugin.json': json.dumps(FULL_MANIFEST), 'payload.py': TINY_CODE})
        self.assertIn('problems', info)
        self.assertIn('notices', info)
        self.assertEqual(info['notices'], [])


class InstallEnableTests(unittest.TestCase):
    """缺陷 1：第三方插件"安装后立即启用"必须真的写进设置并保持住。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        modules = patch('src.plugins.manager.PLUGIN_MODULES', [])
        modules.start()
        self.addCleanup(modules.stop)
        self.manager = PluginManager(self.root / 'runtime')

    def install(self, enable=False):
        target = self.root / 'pkg.zip'
        target.write_bytes(make_zip({'plugin.json': json.dumps(FULL_MANIFEST),
                                     'payload.py': TINY_CODE}))
        result = pi.install_from_file(target, self.manager.custom_plugins_dir,
                                      confirmed=True)
        return finish_install(self.manager, result, enable=enable)

    def test_enable_true_really_writes_enabled(self):
        result = self.install(enable=True)
        self.assertTrue(result['loaded'])
        self.assertTrue(result['enabled'])
        self.assertIs(self.manager.get_settings('probe_tiny')['enabled'], True)
        self.assertTrue(self.manager._plugins['probe_tiny'].enabled)

    def test_enable_true_survives_restart(self):
        self.install(enable=True)
        restarted = PluginManager(self.manager.data_dir)
        self.assertIs(restarted.get_settings('probe_tiny')['enabled'], True)
        self.assertTrue(restarted._plugins['probe_tiny'].enabled)

    def test_install_without_enable_leaves_settings_untouched(self):
        result = self.install(enable=False)
        self.assertTrue(result['loaded'])
        self.assertNotIn('enabled', self.manager.get_settings('probe_tiny'))

    def test_toggle_off_and_on(self):
        self.install(enable=True)
        self.manager.update_settings('probe_tiny', {'enabled': False})
        self.assertFalse(self.manager._plugins['probe_tiny'].enabled)
        self.manager.update_settings('probe_tiny', {'enabled': True})
        self.assertTrue(self.manager._plugins['probe_tiny'].enabled)

    def test_unknown_setting_field_still_rejected(self):
        self.install(enable=True)
        with self.assertRaises(ValueError) as caught:
            self.manager.update_settings('probe_tiny', {'nope': 1})
        self.assertIn("unknown setting field 'nope'", str(caught.exception))

    def test_enabled_must_be_bool(self):
        self.install(enable=True)
        with self.assertRaises(ValueError):
            self.manager.update_settings('probe_tiny', {'enabled': 'yes'})

    def test_batch_install_enable(self):
        target = self.root / 'batch.zip'
        target.write_bytes(make_zip({'plugin.json': json.dumps(FULL_MANIFEST),
                                     'payload.py': TINY_CODE}))
        result = pi.install_from_file(target, self.manager.custom_plugins_dir,
                                      confirmed=True)
        # 批量路径和 /api/plugins/install-local-batch 一样：先标记 success 再统一收尾。
        outcome = finish_batch(self.manager, [{"success": True, **result}], enable=True)
        self.assertTrue(outcome['success'], outcome)
        self.assertEqual(outcome['installed'], ['probe_tiny'])
        self.assertTrue(self.manager._plugins['probe_tiny'].enabled)
        self.assertIs(self.manager.get_settings('probe_tiny')['enabled'], True)


if __name__ == '__main__':
    unittest.main()
