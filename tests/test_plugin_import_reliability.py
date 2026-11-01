"""真实导入、更新、回滚、重启与桌面选择流程的隔离回归。"""
import asyncio
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
from src.plugin_install_service import finish_install, finish_batch, PluginLoadError
from src.plugins.manager import PluginManager

CODE = '''from src.plugins import PluginBase, PluginMeta, FieldSchema
class ImportProbe(PluginBase):
    META = PluginMeta(id='import_probe', name='导入探针', description='隔离验证',
        config_schema={'enabled': FieldSchema('bool','启用'), 'suffix': FieldSchema('string','后缀')},
        default_settings={'enabled': False, 'suffix': '甲'})
    async def on_post_reply(self, message, reply, runtime):
        return reply + self.get('suffix', '')
'''


class ImportReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.modules = patch('src.plugins.manager.PLUGIN_MODULES', [])
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.manager = PluginManager(self.root / 'runtime')

    def source(self, code=CODE, name='中文插件（新版）.py'):
        p = self.root / name
        p.write_text(code, encoding='utf-8-sig')
        return p

    def install(self, source, **kwargs):
        result = pi.install_from_file(source, self.manager.custom_plugins_dir,
                                      confirmed=True, **kwargs)
        return finish_install(self.manager, result, enable=True)

    def test_chinese_bom_import_enable_configure_call_and_restart(self):
        source = self.source()
        info = pi.inspect_local(source)
        self.assertEqual(info['plugin_id'], 'import_probe')
        self.assertTrue(info.get('fingerprint'))
        result = self.install(source)
        self.assertTrue(result['loaded'] and result['enabled'])
        self.manager.update_settings('import_probe', {'suffix': '已正常调用'})
        reply = asyncio.run(self.manager.run_on_post_reply({}, '回复', None))
        self.assertEqual(reply, '回复已正常调用')
        source.unlink()
        manager = PluginManager(self.manager.data_dir)
        self.assertEqual(manager.get_settings('import_probe')['suffix'], '已正常调用')
        self.assertTrue(manager._plugins['import_probe'].enabled)

    def test_zip_manifest_selects_entry_and_uses_declared_id(self):
        source = self.root / '压缩插件.zip'
        with zipfile.ZipFile(source, 'w') as z:
            z.writestr('pkg/plugin.json', json.dumps({'id': 'import_probe', 'entry': 'main.py'}))
            z.writestr('pkg/helper.py', 'unused = 1')
            z.writestr('pkg/main.py', CODE.encode('utf-8-sig'))
        self.assertEqual(pi.inspect_local(source)['plugin_id'], 'import_probe')
        self.assertTrue(self.install(source)['loaded'])

    def test_missing_manifest_entry_and_node_extension_explain_problem(self):
        for files, message in [({'plugin.json': '{"entry":"absent.py"}', 'x.py': CODE}, '入口不存在'),
                               ({'index.js': 'export default {}'}, 'QQ Agent')]:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                for name, text in files.items(): z.writestr(name, text)
            with self.assertRaisesRegex(pi.InstallError, message):
                pi._pick_from_archive(buf.getvalue(), None)

    def test_folder_expands_zip_and_deduplicates_without_silent_truncation(self):
        incoming = self.root / 'incoming'
        incoming.mkdir()
        (incoming / 'one.py').write_text(CODE, encoding='utf-8')
        (incoming / 'two.zip').write_bytes(b'zip')
        self.assertEqual(len(pi.expand_local_paths([incoming, incoming / 'one.py'])), 2)
        for n in range(49): (incoming / f'file{n}.py').write_text('')
        with self.assertRaisesRegex(pi.InstallError, '51'):
            pi.expand_local_paths([incoming])

    def test_failed_new_install_does_not_leave_registration_or_settings(self):
        source = self.source(CODE + "\nraise RuntimeError('测试加载失败')\n")
        with self.assertRaisesRegex(PluginLoadError, '测试加载失败'):
            self.install(source)
        self.assertEqual(self.manager.custom_plugin_ids(), [])
        self.assertNotIn('import_probe', self.manager._settings)
        self.assertFalse(self.manager.custom_plugin_path('import_probe').exists())

    def test_failed_overwrite_restores_code_settings_and_existing_registration(self):
        source = self.source()
        self.install(source)
        self.manager.update_settings('import_probe', {'suffix': '我的配置'})
        previous = self.manager.custom_plugin_path('import_probe').read_bytes()
        source.write_text(CODE + "\nraise RuntimeError('更新失败')\n", encoding='utf-8')
        with self.assertRaisesRegex(PluginLoadError, '回滚'):
            self.install(source, overwrite=True)
        self.assertEqual(self.manager.custom_plugin_path('import_probe').read_bytes(), previous)
        self.assertEqual(self.manager.get_settings('import_probe')['suffix'], '我的配置')
        self.assertIn('import_probe', self.manager.custom_plugin_ids())

    def test_activation_validation_failure_is_not_reported_as_success(self):
        source = self.source(CODE + '''
    def validate_settings(self, settings):
        if settings.get('enabled'): raise ValueError('请先配置凭据')
''')
        with self.assertRaisesRegex(PluginLoadError, '请先配置凭据'):
            self.install(source)
        self.assertEqual(self.manager.custom_plugin_ids(), [])

    def test_source_changed_after_inspection_must_be_rechecked(self):
        source = self.source()
        info = pi.inspect_local(source)
        source.write_text(CODE.replace("reply + self.get", "'changed' + self.get"),
                          encoding='utf-8')
        with self.assertRaisesRegex(pi.InstallError, '预检后已变化'):
            pi.install_from_file(source, self.manager.custom_plugins_dir,
                                 confirmed=True, fingerprint=info['fingerprint'])
        self.assertFalse(self.manager.custom_plugin_path('import_probe').exists())

    def test_batch_reload_failure_rolls_back_files_and_registration(self):
        source = self.source()
        result = pi.install_from_file(source, self.manager.custom_plugins_dir, confirmed=True)
        result['success'] = True
        with patch.object(self.manager, 'reload', side_effect=RuntimeError('reload boom')):
            outcome = finish_batch(self.manager, [result])
        self.assertFalse(outcome['success'])
        self.assertFalse(outcome['results'][0]['success'])
        self.assertIn('回滚', outcome['results'][0]['message'])
        self.assertNotIn('import_probe', self.manager.custom_plugin_ids())
        self.assertFalse(self.manager.custom_plugin_path('import_probe').exists())

    def test_equal_size_same_timestamp_update_runs_latest_source(self):
        source = self.source()
        self.install(source)
        path = self.manager.custom_plugin_path('import_probe')
        timestamp = path.stat().st_mtime
        source.write_text(CODE.replace("reply + self.get", "'新' + self.get"), encoding='utf-8')
        # 直接构造相同大小/时间的已安装源码，模拟旧 SourceFileLoader 的缓存命中条件。
        old = path.read_text(encoding='utf-8')
        new = old.replace("suffix': '甲'", "suffix': '乙'")
        path.write_text(new, encoding='utf-8', newline='\n')
        os.utime(path, (timestamp, timestamp))
        self.manager.reload()
        self.assertEqual(self.manager._plugins['import_probe'].META.default_settings['suffix'], '乙')


class DesktopImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_plugin_and_pack_browse_use_light_desktop_picker(self):
        import qq_gui as gui
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.close)
        with patch.object(gui.file_dialogs, 'open_files', return_value=(['C:/中文.zip'], '')) as picker, \
                patch.object(gui, 'run_api_async') as api:
            dialog._browse()
        picker.assert_called_once()
        api.assert_not_called()
        self.assertEqual(dialog._source(), ('local', ['C:/中文.zip']))
        with patch.object(gui.file_dialogs, 'choose_dir', return_value='C:/插件包'):
            dialog._browse_folder()
        self.assertEqual(dialog._source(), ('local', ['C:/插件包']))
        pack = gui.FeaturePackDialog()
        self.addCleanup(pack.close)
        with patch.object(gui.file_dialogs, 'open_file', return_value=('C:/功能包.zip', '')), \
                patch.object(gui, 'run_api_async') as api:
            pack._browse()
        self.assertEqual(api.call_args.args[1], 'feature-packs/inspect')

    def test_change_after_check_invalidates_install_and_stale_async_result(self):
        import qq_gui as gui
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.close)
        dialog.url.setText('https://example.com/first.py')
        callbacks = []
        with patch.object(gui, 'run_api_async', side_effect=lambda *a, **kw: callbacks.append(kw['callback'])):
            dialog._check()
        dialog.url.setText('https://example.com/second.py')
        callbacks[0]({'ok': True, 'plugin_id': 'first'})
        self.assertFalse(dialog.btn_install.isEnabled())
        self.assertIsNone(dialog._inspection)
        self.assertIn('重新', dialog.result.text())

    def test_plugin_id_edit_during_check_invalidates_stale_async_result(self):
        import qq_gui as gui
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.close)
        dialog.url.setText('https://example.com/first.py')
        callbacks = []
        with patch.object(gui, 'run_api_async',
                          side_effect=lambda *a, **kw: callbacks.append(kw['callback'])):
            dialog._check()
        # 预检尚未返回时用户改了插件标识；该结果已不再对应当前选择。
        dialog.plugin_id.setText('changed_id')
        callbacks[0]({'ok': True, 'plugin_id': 'first'})
        self.assertFalse(dialog.btn_install.isEnabled())
        self.assertIsNone(dialog._inspection)
        self.assertIn('重新', dialog.result.text())

    def test_batch_install_passes_prechecked_fingerprints(self):
        import qq_gui as gui
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.close)
        dialog.local_paths = ['C:/a.py', 'C:/bad.py']
        dialog._check_batch_done({'items': [
            {'ok': True, 'path': 'C:/a.py', 'plugin_id': 'aa', 'fingerprint': 'abc'},
            {'ok': False, 'path': 'C:/bad.py', 'error': '不支持'}]})
        with patch.object(gui, 'ask_confirm', return_value=True), \
                patch.object(gui, 'run_api_async') as api:
            dialog._install()
        payload = api.call_args.args[2]
        self.assertEqual(payload['paths'], ['C:/a.py'])
        self.assertEqual(payload['fingerprints'], {'C:/a.py': 'abc'})

    def test_batch_install_only_sends_inspected_good_files(self):
        import qq_gui as gui
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.close)
        dialog.local_paths = ['C:/a.py', 'C:/bad.py']
        dialog._check_batch_done({'items': [{'ok': True, 'path': 'C:/a.py', 'plugin_id': 'aa'},
                                           {'ok': False, 'path': 'C:/bad.py', 'error': '不支持'}]})
        self.assertTrue(dialog.btn_install.isEnabled())
        with patch.object(gui, 'ask_confirm', return_value=True), patch.object(gui, 'run_api_async') as api:
            dialog._install()
        self.assertEqual(api.call_args.args[2]['paths'], ['C:/a.py'])

    def test_new_source_clears_the_previous_automatic_plugin_id(self):
        import qq_gui as gui
        dialog = gui.PluginInstallDialog()
        self.addCleanup(dialog.close)
        dialog.url.setText('https://example.com/first.py')
        dialog._check_done({'ok': True, 'plugin_id': 'first'})
        self.assertEqual(dialog.plugin_id.text(), 'first')
        dialog._browse_done({'success': True, 'paths': ['C:/second.zip']})
        self.assertEqual(dialog.plugin_id.text(), '')
        self.assertIsNone(dialog._inspection)

    def test_explicit_light_picker_stays_light_under_dark_transparent_parent(self):
        from PyQt6.QtCore import QTimer, Qt
        from PyQt6.QtWidgets import QWidget, QFileDialog
        from src.gui import file_dialogs
        parent = QWidget()
        parent.setStyleSheet('QWidget { background: transparent; color: white; }')
        parent.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.addCleanup(parent.close)
        seen = []
        def cancel():
            dialog = next(w for w in self.app.topLevelWidgets() if isinstance(w, QFileDialog) and w.isVisible())
            color = dialog.grab().toImage().pixelColor(8, 8)
            seen.append(dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
                        and not dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                        and color.alpha() == 255 and color.lightness() > 220)
            dialog.reject()
        QTimer.singleShot(30, cancel)
        self.assertEqual(file_dialogs.open_file(parent, '导入插件', force_light=True), ('', ''))
        self.assertEqual(seen, [True])


if __name__ == '__main__':
    unittest.main()
