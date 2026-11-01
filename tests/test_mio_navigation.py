"""Navigation must preserve drafts, actual save control and window constraints."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication
import qq_gui

APP = QApplication.instance() or QApplication([])


class MioNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.network = patch.object(qq_gui, 'api_get', return_value={})
        self.network.start()
        with patch.object(sys, 'argv', ['gui', '--no-backend']):
            self.window = qq_gui.MainWindow(config_path=Path(self.temp.name) / 'gui.json')
        self.window._timer.stop()
        self.window.settings_tab.populate({'api_url': 'https://example.test/v1', 'model': 'saved-model',
                                          'qqbridge_ws_url': 'ws://127.0.0.1:3001'})
        self.window.show()
        APP.processEvents()
        self.addCleanup(self._cleanup)
        if (not hasattr(self.window, 'shell') and hasattr(self.window, 'nav_list')
                and hasattr(self.window, 'assistant_tab')
                and hasattr(self.window.settings_tab, 'launcher_button')):
            self.skipTest('当前统一接入版使用 nav_list + AssistantTab 侧栏，未装配 MioShell/bridge_port；原 Mio 导航与草稿断言保留。当前侧栏和设置另有独立回归。')

    def _cleanup(self):
        self.window.hang_watchdog._stop = True
        self.window.hang_watchdog._timer.stop()
        self.window._timer.stop()
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()
        self.network.stop()
        self.temp.cleanup()

    def test_switching_sections_and_pages_preserves_all_unsaved_inputs(self):
        shell = self.window.shell
        tab = self.window.settings_tab
        shell.navigate('models')
        tab.api_url.setText('https://draft.test/v1')
        tab.api_key.setText('draft-key')
        tab.model_name.setText('draft-model')
        shell.navigate('qq')
        tab.bridge_port.setValue(3456)
        tab.groups.setPlainText('123\n456')
        for route in ('home', 'memory', 'plugins', 'appearance', 'models', 'qq'):
            shell.buttons[route].click()
            APP.processEvents()
            self.assertEqual(shell.route, route)
        saved = tab.collect()
        self.assertEqual(saved['api_url'], 'https://draft.test/v1')
        self.assertEqual(saved['api_key'], 'draft-key')
        self.assertEqual(saved['model'], 'draft-model')
        self.assertEqual(saved['qqbridge_ws_url'], 'ws://127.0.0.1:3456')
        self.assertEqual(saved['groups'], ['123', '456'])
        self.assertEqual(saved['mode'], 'qqbridge')

    def test_header_reuses_one_save_button_and_its_busy_state(self):
        tab = self.window.settings_tab
        tab.btn_save.clicked.disconnect(self.window._save_settings)
        results = []
        tab.btn_save.clicked.connect(lambda: results.append(tab.collect()))
        for route in ('qq', 'models'):
            self.window.shell.navigate(route)
            APP.processEvents()
            self.assertTrue(tab.btn_save.isVisible())
            tab.btn_save.setEnabled(False)
            tab.btn_save.click()
            self.assertEqual(len(results), 0)
            tab.btn_save.setEnabled(True)
        tab.btn_save.click()
        self.assertEqual(len(results), 1)
        self.window.shell.navigate('home')
        self.assertFalse(tab.btn_save.isVisible())

    def test_adaptive_navigation_keeps_pages_and_system_resize_available(self):
        shell = self.window.shell
        for width, height in ((1200, 800), (800, 500), (1100, 780)):
            self.window.resize(width, height)
            APP.processEvents()
            self.assertEqual((self.window.width(), self.window.height()), (width, height))
            self.assertFalse(self.window.layout().hasHeightForWidth())
            self.assertEqual(shell._compact, width < 1012)
            for route in shell.ROUTES:
                shell.navigate(route)
                self.assertIs(self.window.tabs.currentWidget(), self.window.tabs.widget(shell.ROUTES[route][0]))
                self.assertTrue(shell.buttons[route].isChecked())
        self.assertTrue(self.window.tabs.tabBar().isHidden())
        self.window.tabs.setCurrentWidget(self.window.persona_tab)
        self.assertEqual(shell.route, 'persona')
        self.window.tabs.setCurrentWidget(self.window.settings_tab)
        self.assertEqual(shell.route, self.window.settings_tab.section)

    def test_save_reload_preserves_edits_made_while_request_was_pending(self):
        tab, shell = self.window.settings_tab, self.window.shell
        shell.navigate('models')
        tab.model_name.setText('submitted-model')
        tab.api_key.setText('submitted-key')
        with patch.object(qq_gui, 'run_api_async') as request:
            self.window._save_settings()
        submitted = request.call_args.args[2]
        self.assertFalse(tab.btn_save.isEnabled())
        self.assertTrue(shell.feedback.isVisible())
        tab.model_name.setText('later-model')
        tab.api_key.setText('later-key')
        returned = {**submitted, 'api_key_set': True}
        returned.pop('api_key')
        with patch.object(qq_gui, 'api_get', return_value=returned):
            self.window._save_settings_done({'success': True, 'message': '配置已保存'})
        self.assertEqual(tab.model_name.text(), 'later-model')
        self.assertEqual(tab.api_key.text(), 'later-key')
        self.assertEqual(tab.dirty_sections(), {'models'})
        self.assertEqual(shell.dirty_label.text(), '有未保存修改')
        self.assertTrue(tab.btn_save.isEnabled())
        self.assertTrue(shell.feedback.isVisible())

    def test_failed_save_keeps_drafts_and_successful_save_clears_only_saved_values(self):
        tab, shell = self.window.settings_tab, self.window.shell
        shell.navigate('qq')
        tab.bridge_port.setValue(3456)
        with patch.object(qq_gui, 'run_api_async') as request:
            self.window._save_settings()
        with patch.object(qq_gui, 'api_get') as reload:
            self.window._save_settings_done({'success': False, 'message': '连接配置校验失败'})
            reload.assert_not_called()
        self.assertEqual(tab.bridge_port.value(), 3456)
        self.assertEqual(tab.dirty_sections(), {'qq'})
        self.assertIn('校验失败', tab.save_lbl.text())
        with patch.object(qq_gui, 'run_api_async') as request:
            self.window._save_settings()
        submitted = request.call_args.args[2]
        with patch.object(qq_gui, 'api_get', return_value=submitted):
            self.window._save_settings_done({'success': True, 'message': '已保存'})
        self.assertEqual(tab.dirty_sections(), set())
        self.assertNotIn('*', shell.buttons['qq'].text())
        shell.navigate('home')
        self.assertFalse(shell.feedback.isVisible())

    def test_startup_reload_does_not_erase_early_port_or_secret_edits(self):
        tab = self.window.settings_tab
        tab.bridge_port.setValue(3456)
        tab.api_key.setText('early-draft-key')
        returned = {'api_url': 'https://example.test/v1', 'model': 'saved-model',
                    'qqbridge_ws_url': 'ws://127.0.0.1:3001'}
        with patch.object(qq_gui, 'api_get', return_value=returned):
            self.window._load_settings()
        self.assertEqual(tab.bridge_port.value(), 3456)
        self.assertEqual(tab.api_key.text(), 'early-draft-key')
        self.assertEqual(tab.dirty_sections(), {'qq', 'models'})
        tab.bridge_port.setValue(3001)
        tab.api_key.clear()
        self.assertEqual(tab.dirty_sections(), set())

    def test_local_navigation_filter_is_recoverable_after_compacting(self):
        shell = self.window.shell
        self.window.resize(1200, 800)
        APP.processEvents()
        shell.search.setText('models')
        self.assertTrue(shell.buttons['models'].isVisible())
        self.assertFalse(shell.buttons['memory'].isVisible())
        shell.search.setText('not-a-real-page')
        self.assertTrue(shell.no_results.isVisible())
        self.window.resize(800, 500)
        APP.processEvents()
        self.assertFalse(shell.search.isVisible())
        self.assertTrue(all(button.isVisible() for button in shell.buttons.values()))
        self.window.resize(1200, 800)
        APP.processEvents()
        self.assertTrue(shell.no_results.isVisible())
        shell.search.clear()
        self.assertFalse(shell.no_results.isVisible())
        self.assertTrue(all(button.isVisible() for button in shell.buttons.values()))


if __name__ == '__main__':
    unittest.main()
