"""功能包导入：预检、拒绝不安全路径、插件+技能+偏好一起装。"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from src import feature_pack
from src.agent_learning import AgentLearning

PLUGIN = '''from src.plugins import FieldSchema, PluginBase, PluginMeta


class PackPlugin(PluginBase):
    META = PluginMeta(id="pack_plugin", name="功能包插件", description="功能包验证",
                      category="utility", enabled_by_default=False,
                      config_schema={"enabled": FieldSchema(type="bool", label="启用")},
                      default_settings={"enabled": False})
'''

SKILL = {'format': 'whale-agent-skill', 'version': 1, 'id': 'pack_skill',
         'name': '功能包技能', 'instructions': '遇到相关问题时按此说明处理。', 'triggers': ['功能包']}

MANIFEST = {'format': 'whale-feature-pack', 'version': 1, 'name': '测试功能包',
            'description': '插件 + 技能 + 偏好', 'author': '测试',
            'plugins': [{'file': 'plugins/pack_plugin.py'}],
            'skills': [{'file': 'skills/pack_skill.json'}],
            'preferences': [{'key': '称呼', 'value': '老鲸'}]}


class FeaturePackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pack = self.root / 'demo.zip'
        with zipfile.ZipFile(self.pack, 'w') as archive:
            archive.writestr('pack.json', json.dumps(MANIFEST, ensure_ascii=False))
            archive.writestr('plugins/pack_plugin.py', PLUGIN)
            archive.writestr('skills/pack_skill.json', json.dumps(SKILL, ensure_ascii=False))
        self.learning = AgentLearning(self.root / 'learning.json')
        self.plugins = self.root / 'custom_plugins'

    def tearDown(self):
        self.tmp.cleanup()

    def test_inspect_lists_components_without_writing(self):
        info = feature_pack.inspect(self.pack)
        self.assertTrue(info['ok'])
        self.assertEqual(info['name'], '测试功能包')
        self.assertEqual((info['plugins'], info['skills'], info['preferences']), (1, 1, 1))
        self.assertFalse(self.plugins.exists())

    def test_install_requires_confirmation(self):
        with self.assertRaises(feature_pack.FeaturePackError):
            feature_pack.install(self.pack, self.plugins, self.learning)

    def test_install_plugin_skill_and_preference(self):
        report = feature_pack.install(self.pack, self.plugins, self.learning, confirmed=True)
        self.assertTrue(report['success'], report)
        self.assertTrue((self.plugins / 'pack_plugin.py').is_file())
        self.assertEqual(self.learning.overview()['preferences'][0]['value'], '老鲸')
        self.assertEqual(self.learning.overview()['skills'][0]['id'], 'pack_skill')
        self.assertIn('pack_plugin', report['plugins'])

    def test_rejects_path_traversal(self):
        bad = self.root / 'bad.zip'
        manifest = dict(MANIFEST, plugins=[{'file': '../evil.py'}])
        with zipfile.ZipFile(bad, 'w') as archive:
            archive.writestr('pack.json', json.dumps(manifest, ensure_ascii=False))
            archive.writestr('../evil.py', PLUGIN)
        with self.assertRaises(feature_pack.FeaturePackError):
            feature_pack.inspect(bad)

    def test_rejects_wrong_format_and_missing_files(self):
        bad = self.root / 'bad2.zip'
        with zipfile.ZipFile(bad, 'w') as archive:
            archive.writestr('pack.json', json.dumps({'format': 'other', 'version': 1, 'name': 'x'}))
        with self.assertRaises(feature_pack.FeaturePackError):
            feature_pack.inspect(bad)
        missing = self.root / 'bad3.zip'
        with zipfile.ZipFile(missing, 'w') as archive:
            archive.writestr('pack.json', json.dumps(MANIFEST, ensure_ascii=False))
        info = feature_pack.inspect(missing)
        self.assertFalse(info['ok'])
        self.assertFalse(info['items'][0]['ok'])


if __name__ == '__main__':
    unittest.main()
