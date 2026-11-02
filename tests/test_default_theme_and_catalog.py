"""默认 Mio 风格背景与插件分层清单的回归。"""
import tempfile
import unittest
from pathlib import Path

from src.gui.appearance import ensure_default_background, DEFAULT_BACKGROUND
from src.plugin_catalog import BASIC_PLUGINS, HELPERS, market_plugins


class DefaultBackgroundTests(unittest.TestCase):
    def test_empty_config_gets_bundled_background_copied(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = {}
            ensure_default_background(cfg, root, Path(__file__).resolve().parents[1])
            self.assertTrue(cfg.get('background_image'))
            copied = Path(root) / cfg['background_image']
            self.assertTrue(copied.is_file())
            self.assertGreater(copied.stat().st_size, 1000)

    def test_custom_or_cleared_background_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            custom = {'background_image': 'data/backgrounds/my.png'}
            self.assertEqual(ensure_default_background(dict(custom), root,
                                                      Path(__file__).resolve().parents[1]),
                             custom)
            cleared = {'background_image': '', 'default_background': False}
            self.assertEqual(ensure_default_background(dict(cleared), root,
                                                      Path(__file__).resolve().parents[1]),
                             cleared)
            self.assertFalse((Path(root) / 'data' / 'backgrounds' / Path(*DEFAULT_BACKGROUND).name).exists())


class PluginCatalogTests(unittest.TestCase):
    def test_basic_and_market_are_disjoint_and_cover_all(self):
        market = set(market_plugins())
        basic = set(BASIC_PLUGINS)
        self.assertFalse(basic & market)
        self.assertNotIn('face_names', market)
        self.assertNotIn('face_names', basic)
        for plugin_id in basic | market:
            self.assertTrue((Path(__file__).resolve().parents[1] / 'src' / 'plugins'
                             / f'{plugin_id}.py').is_file(), plugin_id)
        self.assertEqual(len(basic), 22)
        self.assertEqual(len(market), 41)

    def test_basic_keeps_core_chat_and_memory(self):
        for required in ('conversation_memory', 'image_understanding', 'token_saver',
                         'chat_controls', 'ambient_reply'):
            self.assertIn(required, BASIC_PLUGINS)


if __name__ == '__main__':
    unittest.main()
