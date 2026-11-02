"""Plugin catalog regression tests: shipped defaults must stay explicit."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from src.plugin_catalog import BASIC_PLUGINS, HELPERS, market_plugins
from src.plugins.archive_isolation import ArchiveIsolation


class _Style:
    def __init__(self, name):
        self.active_name = name


class _Runtime:
    def __init__(self, name):
        self.style = _Style(name)


class _Manager:
    def __init__(self, root):
        self.state_dir = Path(root)
        self.settings = {'enabled': True, 'protectKeys': ''}

    def get_settings(self, plugin_id):
        return dict(self.settings)


class PluginCatalogBasicsTests(unittest.TestCase):
    def test_archive_isolation_is_shipped_as_a_basic_plugin(self):
        # The plugin only participates in history/memory scoping when frozen
        # builds actually load it; keeping it out of BASIC_PLUGINS made the
        # persona-isolation switch silently disappear from installed builds.
        self.assertIn('archive_isolation', BASIC_PLUGINS)
        self.assertNotIn('archive_isolation', HELPERS)

    def test_basic_plugins_are_not_also_offered_in_the_market(self):
        market = set(market_plugins())
        self.assertFalse(set(BASIC_PLUGINS) & market)

    def test_archive_isolation_is_enabled_by_default(self):
        self.assertIs(ArchiveIsolation.META.default_settings.get('enabled'), True)


class ArchiveIsolationScopeTests(unittest.TestCase):
    def test_switching_persona_gets_a_new_scope_and_switching_back_restores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _Manager(tmp)
            runtime = _Runtime('甲')
            plugin = ArchiveIsolation(manager)
            base = 'conversation|group:1'
            message = {'kind': 'group', 'target': '1', '_effective_persona': '甲'}

            self.assertEqual(plugin.scope(base, message, runtime), base)

            message['_effective_persona'] = '乙'
            other = plugin.scope(base, message, runtime)
            expected = base + '|persona:' + hashlib.sha256('乙'.encode()).hexdigest()[:16]
            self.assertEqual(other, expected)

            message['_effective_persona'] = '甲'
            self.assertEqual(plugin.scope(base, message, runtime), base)

    def test_protected_chat_stays_shared_across_personas(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _Manager(tmp)
            manager.settings['protectKeys'] = 'group:2，private:3'
            runtime = _Runtime('甲')
            plugin = ArchiveIsolation(manager)
            base = 'conversation|group:1'

            plugin.scope(base, {'kind': 'group', 'target': '1'}, runtime)
            runtime.style.active_name = '乙'
            self.assertEqual(plugin.scope('chat|group:2',
                                         {'kind': 'group', 'target': '2'}, runtime),
                             'chat|group:2')
            self.assertEqual(plugin.scope('chat|private:3',
                                         {'kind': 'private', 'target': '3'}, runtime),
                             'chat|private:3')
            self.assertTrue(plugin.scope(base, {'kind': 'group', 'target': '1'}, runtime)
                            .startswith(base + '|persona:'))


if __name__ == '__main__':
    unittest.main()
