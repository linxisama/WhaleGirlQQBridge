"""插件来源标记（内置 / 目录包 / 自定义）与内核插件数量的回归测试。

两条链要分清：控制台「插件」页列的是 plugins/manager.py 那套（内置 + 裸 .py +
目录包 mod），「内核插件」按钮后面才是 plugin_core 的目录式插件。这里只验证前者
的来源标记，以及后者数量只进 summary、失败不影响插件列表。
"""
import os
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src.plugin_capabilities import apply_capabilities

try:
    from PyQt6.QtWidgets import QApplication
    import qq_gui as gui

    APP = QApplication.instance() or QApplication([])
    HAS_QT = True
except Exception:
    HAS_QT = False


class _Plugins:
    """只实现来源判定要用的两个只读方法。

    _plugins 是 apply_capabilities 第三方分支要读的实例表（src/plugin_capabilities.py:91），
    空字典表示当前没有加载实例。
    """

    _plugins = {}

    def __init__(self, mods=None, customs=None):
        self._mods = dict(mods or {})
        self._customs = list(customs or [])

    def mod_id_for_plugin(self, plugin_id):
        return self._mods.get(plugin_id, '')

    def custom_plugin_ids(self):
        return list(self._customs)


class _Runtime:
    mode = 'onebot'
    stickers = None
    onebot = None
    running = False

    def __init__(self, plugins=None):
        self.settings = {}
        if plugins is not None:
            self.plugins = plugins


class MinimalFakeRuntime:
    """与 tests/test_plugin_audit_fixes.py 里那个 FakeRuntime 一样：没有 plugins。"""

    mode = 'onebot'
    stickers = None
    onebot = None
    running = False

    def __init__(self):
        self.settings = {}


class _BrokenPlugins:
    """两个方法都抛异常：来源判定必须吞掉并退回内置。"""

    _plugins = {}

    def mod_id_for_plugin(self, plugin_id):
        raise RuntimeError('mod index corrupted')

    def custom_plugin_ids(self):
        raise RuntimeError('custom list unreadable')


def _item(pid):
    return {'id': pid, 'schema': {}, 'settings': {}, 'enabled': True}


class PluginSourceTests(unittest.TestCase):
    def test_mod_plugin_reports_directory_pack_and_mod_id(self):
        runtime = _Runtime(_Plugins(mods={'bank_tool': 'xiaoyou-bank'}))
        item = _item('bank_tool')
        apply_capabilities([item], runtime)
        self.assertEqual(item['plugin_source'], 'mod')
        self.assertEqual(item['source_label'], '目录包')
        self.assertEqual(item['source_detail'], 'xiaoyou-bank')

    def test_custom_plugin_reports_custom_without_detail(self):
        runtime = _Runtime(_Plugins(customs=['my_plugin']))
        item = _item('my_plugin')
        apply_capabilities([item], runtime)
        self.assertEqual(item['plugin_source'], 'custom')
        self.assertEqual(item['source_label'], '自定义')
        self.assertEqual(item['source_detail'], '')

    def test_builtin_plugin_is_default(self):
        runtime = _Runtime(_Plugins())
        item = _item('image_library')
        apply_capabilities([item], runtime)
        self.assertEqual(item['plugin_source'], 'builtin')
        self.assertEqual(item['source_label'], '内置')
        self.assertEqual(item['source_detail'], '')

    def test_mod_wins_over_custom_listing(self):
        # 目录包和自定义清单同时登记时，目录包优先（mod_id 非空即判 mod）
        runtime = _Runtime(_Plugins(mods={'both': 'pack-a'}, customs=['both']))
        item = _item('both')
        apply_capabilities([item], runtime)
        self.assertEqual(item['plugin_source'], 'mod')
        self.assertEqual(item['source_detail'], 'pack-a')

    def test_minimal_fake_runtime_without_plugins_does_not_raise(self):
        item = _item('sticker_cap')
        apply_capabilities([item], MinimalFakeRuntime())
        self.assertEqual(item['plugin_source'], 'builtin')
        self.assertEqual(item['source_label'], '内置')
        self.assertEqual(item['source_detail'], '')

    def test_manager_errors_fall_back_to_builtin(self):
        item = _item('image_library')
        apply_capabilities([item], _Runtime(_BrokenPlugins()))
        self.assertEqual(item['plugin_source'], 'builtin')
        self.assertEqual(item['source_label'], '内置')


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class PluginSourceGuiTests(unittest.TestCase):
    def tearDown(self):
        APP.processEvents()

    @staticmethod
    def _labels(row):
        return [widget.text() for widget in row.findChildren(gui.QLabel)]

    def test_row_badge_shows_mod_id_for_directory_pack(self):
        tab = gui.PluginTab()
        try:
            row = tab._make_row({'id': 'bank_tool', 'name': '银行工具',
                                 'plugin_source': 'mod', 'source_label': '目录包',
                                 'source_detail': 'xiaoyou-bank'})
            self.assertIn('目录包 · xiaoyou-bank', self._labels(row))
        finally:
            tab.close()
            tab.deleteLater()

    def test_row_badge_falls_back_to_builtin_without_label(self):
        tab = gui.PluginTab()
        try:
            row = tab._make_row({'id': 'p1', 'name': '插件一'})
            self.assertIn('内置', self._labels(row))
        finally:
            tab.close()
            tab.deleteLater()

    @staticmethod
    def _api(plugins, core=None):
        def fake_get(path, *args, **kwargs):
            if path == 'plugins':
                return {'success': True, 'plugins': plugins}
            if path == 'plugins/custom':
                return {'success': True, 'plugins': []}
            if path == 'plugin-core/status':
                if isinstance(core, Exception):
                    raise core
                return core if core is not None else {'success': True, 'available': False}
            raise AssertionError('插件页刷到了意外的接口：' + str(path))
        return fake_get

    def _visible(self, tab):
        return {pid for pid, row in tab._rows.items() if not row['frame'].isHidden()}

    def test_search_matches_source_label(self):
        plugins = [
            {'id': 'a', 'name': '甲', 'category': 'utility', 'enabled': True,
             'plugin_source': 'mod', 'source_label': '目录包', 'source_detail': 'xiaoyou-bank'},
            {'id': 'b', 'name': '乙', 'category': 'utility', 'enabled': True,
             'plugin_source': 'custom', 'source_label': '自定义', 'source_detail': ''},
            {'id': 'c', 'name': '丙', 'category': 'utility', 'enabled': True,
             'plugin_source': 'builtin', 'source_label': '内置', 'source_detail': ''},
        ]
        tab = gui.PluginTab()
        try:
            with mock.patch.object(gui, 'api_get', self._api(plugins)):
                tab.refresh()
            self.assertEqual(self._visible(tab), {'a', 'b', 'c'})
            tab.search.setText('目录包')
            self.assertEqual(self._visible(tab), {'a'})
            tab.search.setText('自定义')
            self.assertEqual(self._visible(tab), {'b'})
        finally:
            tab.close()
            tab.deleteLater()

    def test_summary_shows_core_plugin_count(self):
        plugins = [{'id': 'a', 'name': '甲', 'category': 'utility', 'enabled': True}]
        core = {'success': True, 'available': True, 'started': True,
                'plugins': [{'id': 'x'}, {'id': 'y'}, {'id': 'z'}]}
        tab = gui.PluginTab()
        try:
            with mock.patch.object(gui, 'api_get', self._api(plugins, core)):
                tab.refresh()
            self.assertIn('内核插件 3 个（在「内核插件」里管理）', tab.summary.text())
        finally:
            tab.close()
            tab.deleteLater()

    def test_summary_reports_core_down_without_breaking_list(self):
        plugins = [{'id': 'a', 'name': '甲', 'category': 'utility', 'enabled': True}]
        tab = gui.PluginTab()
        try:
            with mock.patch.object(
                    gui, 'api_get', self._api(plugins, RuntimeError('backend down'))):
                tab.refresh()
            self.assertIn('内核插件未启动', tab.summary.text())
            self.assertEqual(set(tab._rows), {'a'})
            self.assertIn('列表已更新', tab.notice.text())
        finally:
            tab.close()
            tab.deleteLater()


if __name__ == '__main__':
    unittest.main()
