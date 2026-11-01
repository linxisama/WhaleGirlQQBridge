"""功能包 HTTP 路由：预检、确认闸门、插件加载与学习库落盘。"""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from fastapi.testclient import TestClient

from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root
from tests.test_feature_pack import MANIFEST, PLUGIN, SKILL


class FeaturePackRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = build_root(cls.tmp.name)
        cls.bot = QQRuntime(root=root, db_path=root / 'test.sqlite')
        cls.client = TestClient(create_app(cls.bot))
        cls.client.__enter__()
        cls.pack = Path(cls.tmp.name) / 'pack.zip'
        with zipfile.ZipFile(cls.pack, 'w') as archive:
            archive.writestr('pack.json', json.dumps(MANIFEST, ensure_ascii=False))
            archive.writestr('plugins/pack_plugin.py', PLUGIN)
            archive.writestr('skills/pack_skill.json', json.dumps(SKILL, ensure_ascii=False))

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.tmp.cleanup()

    def test_inspect_reports_components(self):
        response = self.client.post('/api/feature-packs/inspect', json={'path': str(self.pack)})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['plugins'], 1)

    def test_install_requires_confirmation(self):
        response = self.client.post('/api/feature-packs/install', json={'path': str(self.pack)})
        self.assertEqual(response.status_code, 400)

    def test_install_loads_plugin_and_imports_skill(self):
        response = self.client.post('/api/feature-packs/install',
                                    json={'path': str(self.pack), 'confirmed': True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'], body)
        self.assertIn('pack_plugin', self.bot.plugins._plugins)
        self.assertTrue(self.bot.plugins.custom_plugin_path('pack_plugin').is_file())
        learning = getattr(self.client.app.state, 'agent_learning', None) or self.client.app.state.console_agent.learning
        overview = learning.overview()
        self.assertEqual(overview['skills'][0]['id'], 'pack_skill')
        self.assertEqual(overview['preferences'][0]['value'], '老鲸')


if __name__ == '__main__':
    unittest.main()
