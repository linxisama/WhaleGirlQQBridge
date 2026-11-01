"""T14 音频素材配置插件测试：上传入口类型、默认关闭、云端出网回退。"""
import unittest

from src.plugins.audio_assets_config import AudioAssetsConfig
from src.plugins.voice_reply import VoiceReply
from src.plugins.local_agent_control import LocalAgentControl


class AudioAssetsConfigTests(unittest.TestCase):
    def setUp(self):
        self.meta = AudioAssetsConfig.META

    def test_plugin_is_off_by_default(self):
        self.assertFalse(self.meta.enabled_by_default)
        self.assertEqual(self.meta.category, 'voice')
        for key in ('refAudio', 'voiceSamples', 'songFiles', 'voiceTable', 'assetDir'):
            self.assertEqual(self.meta.default_settings[key], '')
        self.assertFalse(self.meta.default_settings['allowOutbound'])
        self.assertEqual(self.meta.default_settings['routePolicy'], 'local_only')

    def test_upload_fields_declare_the_right_picker(self):
        schema = self.meta.config_schema
        self.assertEqual(schema['refAudio'].file_picker, 'file')
        self.assertEqual(schema['voiceSamples'].file_picker, 'files')
        self.assertEqual(schema['songFiles'].file_picker, 'files')
        self.assertEqual(schema['voiceTable'].file_picker, 'labeled_files')
        self.assertEqual(schema['assetDir'].file_picker, 'directory')
        self.assertIn('.wav', schema['refAudio'].file_extensions)
        self.assertGreater(schema['refAudio'].file_max_mb, 0)
        self.assertGreater(schema['songFiles'].file_max_mb, 0)

    def test_schema_dict_carries_picker_metadata(self):
        rendered = self.meta.schema_dict()
        self.assertEqual(rendered['refAudio']['file_picker'], 'file')
        self.assertEqual(rendered['assetDir']['file_picker'], 'directory')
        self.assertIn('.wav', rendered['refAudio']['file_extensions'])

    def test_cloud_route_without_permission_falls_back_to_local(self):
        settings = {'routePolicy': 'cloud_only', 'allowOutbound': False}
        AudioAssetsConfig.validate_settings(self, settings)
        self.assertEqual(settings['routePolicy'], 'local_only')
        allowed = {'routePolicy': 'cloud_only', 'allowOutbound': True}
        AudioAssetsConfig.validate_settings(self, allowed)
        self.assertEqual(allowed['routePolicy'], 'cloud_only')

    def test_no_credentials_in_schema(self):
        for key in self.meta.config_schema:
            self.assertNotIn('key', key.lower())
            self.assertNotIn('secret', key.lower())


class VoicePluginDefaultsTests(unittest.TestCase):
    def test_voice_and_agent_plugins_are_off_by_default(self):
        for plugin in (VoiceReply, LocalAgentControl):
            self.assertFalse(plugin.META.enabled_by_default)
            self.assertTrue(plugin.META.default_settings.get('enabled', False) is False
                            or 'enabled' not in plugin.META.default_settings)


if __name__ == '__main__':
    unittest.main()
