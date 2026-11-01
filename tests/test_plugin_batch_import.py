"""本地插件批量导入：多选、文件夹展开、逐个结果与统一重载。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from fastapi.testclient import TestClient

from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root

GOOD = '''from src.plugins import FieldSchema, PluginBase, PluginMeta


class BatchGood(PluginBase):
    META = PluginMeta(id="batch_good", name="批量测试插件", description="批量导入验证",
                      category="utility", enabled_by_default=False,
                      config_schema={"enabled": FieldSchema(type="bool", label="启用")},
                      default_settings={"enabled": False})
'''

SECOND = GOOD.replace('BatchGood', 'BatchSecond').replace('batch_good', 'batch_second').replace('批量测试插件', '第二个插件')


class BatchImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = build_root(cls.tmp.name)
        (root / 'config.yaml').write_text('', encoding='utf-8')
        cls.bot = QQRuntime(root=root, db_path=root / 'test.sqlite')
        cls.client = TestClient(create_app(cls.bot))
        cls.client.__enter__()
        cls.manager = cls.bot.plugins
        cls.dir = Path(cls.tmp.name) / 'incoming'
        cls.dir.mkdir()
        cls.good = cls.dir / 'batch_good.py'
        cls.good.write_text(GOOD, encoding='utf-8')
        cls.second = cls.dir / 'batch_second.py'
        cls.second.write_text(SECOND, encoding='utf-8')
        cls.evil = cls.dir / 'evil.py'
        cls.evil.write_text('import os\nfrom src.plugins import PluginBase, PluginMeta\n\n\n'
                            'class Evil(PluginBase):\n    META = PluginMeta(id="evil_batch", name="坏")\n'
                            '    def post_load(self):\n        os.system("calc")\n', encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.tmp.cleanup()

    def test_batch_inspect_reports_each_file(self):
        response = self.client.post('/api/plugins/inspect-local-batch',
                                    json={'paths': [str(self.good), str(self.evil)]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body['items']), 2)
        self.assertFalse(body['ok'])
        good = next(item for item in body['items'] if item['path'].endswith('batch_good.py'))
        evil = next(item for item in body['items'] if item['path'].endswith('evil.py'))
        self.assertTrue(good['ok'])
        self.assertEqual(good['name'], '批量测试插件')
        self.assertFalse(evil['ok'])

    def test_batch_inspect_expands_folder(self):
        response = self.client.post('/api/plugins/inspect-local-batch',
                                    json={'paths': [str(self.dir)]})
        self.assertEqual(response.status_code, 200, response.text)
        paths = {Path(item['path']).name for item in response.json()['items']}
        self.assertEqual(paths, {'batch_good.py', 'batch_second.py', 'evil.py'})

    def test_batch_install_requires_confirmation(self):
        response = self.client.post('/api/plugins/install-local-batch',
                                    json={'paths': [str(self.good)], 'confirmed': False})
        self.assertEqual(response.status_code, 400)

    def test_batch_install_installs_good_and_skips_bad(self):
        response = self.client.post('/api/plugins/install-local-batch',
                                    json={'paths': [str(self.good), str(self.evil)],
                                          'confirmed': True, 'overwrite': True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['total'], 2)
        self.assertEqual(body['failed'], 1)
        self.assertIn('batch_good', body['installed'])
        self.assertTrue(self.manager.custom_plugin_path('batch_good').is_file())
        self.assertIn('batch_good', self.manager._plugins)
        self.assertFalse(self.manager.custom_plugin_path('evil_batch').is_file())

    def test_batch_install_rejects_file_changed_after_inspection(self):
        source = self.dir / 'batch_fingerprint.py'
        source.write_text(
            GOOD.replace('BatchGood', 'BatchFingerprint')
                .replace('batch_good', 'batch_fingerprint')
                .replace('批量测试插件', '指纹测试插件'),
            encoding='utf-8')
        self.addCleanup(lambda: self.manager.custom_plugin_path('batch_fingerprint').unlink(missing_ok=True))
        inspected = self.client.post('/api/plugins/inspect-local',
                                     json={'path': str(source)}).json()
        self.assertTrue(inspected.get('fingerprint'))
        source.write_text(source.read_text(encoding='utf-8') + '\n# changed\n',
                          encoding='utf-8')
        response = self.client.post('/api/plugins/install-local-batch', json={
            'paths': [str(source)], 'confirmed': True,
            'fingerprints': {str(source): inspected['fingerprint']}})
        body = response.json()
        self.assertEqual(body['failed'], 1)
        self.assertIn('预检后已变化', body['results'][0]['message'])
        self.assertFalse(self.manager.custom_plugin_path('batch_fingerprint').is_file())

    def test_batch_install_rejects_missing_fingerprint_when_one_was_supplied(self):
        source = self.dir / 'batch_missing_fp.py'
        source.write_text(
            GOOD.replace('BatchGood', 'BatchMissingFp')
                .replace('batch_good', 'batch_missing_fp')
                .replace('批量测试插件', '缺指纹测试插件'),
            encoding='utf-8')
        self.addCleanup(lambda: self.manager.custom_plugin_path('batch_missing_fp').unlink(missing_ok=True))
        response = self.client.post('/api/plugins/install-local-batch', json={
            'paths': [str(source)], 'confirmed': True,
            'fingerprints': {str(source) + '.other': 'abc'}})
        body = response.json()
        self.assertEqual(body['failed'], 1)
        self.assertIn('缺少预检指纹', body['results'][0]['message'])
        self.assertFalse(self.manager.custom_plugin_path('batch_missing_fp').is_file())

    def test_batch_install_is_idempotent_with_overwrite(self):
        response = self.client.post('/api/plugins/install-local-batch',
                                    json={'paths': [str(self.good)], 'confirmed': True,
                                          'overwrite': True})
        self.assertTrue(response.json()['success'], response.text)
        item = response.json()['results'][0]
        self.assertTrue(item['overwritten'])

    def test_duplicate_declared_ids_are_not_written_twice(self):
        duplicate = self.dir / '中文别名.py'
        duplicate.write_text(GOOD, encoding='utf-8-sig')
        self.addCleanup(duplicate.unlink)
        response = self.client.post('/api/plugins/install-local-batch',
                                    json={'paths': [str(self.good), str(duplicate)],
                                          'confirmed': True, 'overwrite': True})
        body = response.json()
        self.assertEqual(body['installed'], ['batch_good'])
        self.assertEqual(body['failed'], 1)
        self.assertIn('重复插件标识', body['results'][1]['message'])
        self.assertIn('batch_good', self.manager._plugins)


if __name__ == '__main__':
    unittest.main()
