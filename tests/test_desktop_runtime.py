"""桌面启动约束和真实 Windows 窗口回归，不启动 QQ 或浏览器。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gui_launcher
from src import desktop_runtime as desktop
from src.startup_supervisor import GuiResult


class DesktopEnvironmentTests(unittest.TestCase):
    def test_safe_mode_uses_gdi_fusion_and_forces_software(self):
        env = {'QT_OPENGL': 'desktop', 'QT_QPA_PLATFORM': 'windows'}
        desktop.configure(True, environment=env, windows=True)
        self.assertIn('fontengine=gdi', env['QT_QPA_PLATFORM'])
        self.assertIn('dpiawareness=0', env['QT_QPA_PLATFORM'])
        # 兼容模式也必须给系统原生文件框留路：dialogs=none 会让用户点开的
        # 背景图选择框退回 Qt 自绘版（列表点不动、标签英文）。
        self.assertNotIn('dialogs=none', env['QT_QPA_PLATFORM'])
        self.assertEqual(env['QT_OPENGL'], 'software')
        self.assertEqual(env['QT_STYLE_OVERRIDE'], 'Fusion')

    def test_ordinary_offscreen_tests_stay_offscreen(self):
        env = {'QT_QPA_PLATFORM': 'offscreen'}
        desktop.configure(True, environment=env, windows=True)
        self.assertEqual(env['QT_QPA_PLATFORM'], 'offscreen')

    def test_native_test_really_uses_windows_even_if_parent_is_offscreen(self):
        env = {'QT_QPA_PLATFORM': 'offscreen'}
        desktop.configure(False, environment=env, windows=True, native_test=True)
        self.assertEqual(env['QT_QPA_PLATFORM'], 'windows')

    def test_packaged_plugin_paths_cannot_point_to_another_qt_install(self):
        with tempfile.TemporaryDirectory() as folder:
            plugins = Path(folder) / 'PyQt6/Qt6/plugins'
            plugins.mkdir(parents=True)
            env = {'QT_PLUGIN_PATH': 'other-app', 'QT_QPA_PLATFORM_PLUGIN_PATH': 'other-app'}
            desktop.configure(False, environment=env, windows=True, bundle=folder)
            self.assertEqual(env['QT_PLUGIN_PATH'], str(plugins))
            self.assertEqual(env['QT_QPA_PLATFORM_PLUGIN_PATH'], str(plugins / 'platforms'))

    @unittest.skipUnless(os.name == 'nt', 'Windows 会话标识')
    def test_rdp_session_selects_compatibility(self):
        with patch.dict(os.environ, SESSIONNAME='RDP-Tcp#9'):
            self.assertTrue(desktop.remote_session())


class DesktopOnlyLaunchTests(unittest.TestCase):
    def test_failed_desktop_attempts_only_show_native_error_never_start_web(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(gui_launcher, '_logs_dir', return_value=Path(folder)), \
             patch.object(sys, 'argv', ['app']), \
             patch.object(gui_launcher, 'config_safe_mode', return_value=False), \
             patch.object(gui_launcher, 'previous_launch_crashed', return_value=False), \
             patch.object(desktop, 'remote_session', return_value=False), \
             patch.object(desktop, 'failed', return_value=1) as failed, \
             patch('src.startup_supervisor.stage'), \
             patch('src.startup_supervisor.watch_gui', side_effect=[GuiResult(0xC0000005, False)] * 2) as attempts, \
             patch('src.compat_console.main', side_effect=AssertionError('不得启动网页后台')) as web:
            self.assertEqual(gui_launcher.main(), 1)
            self.assertEqual(attempts.call_count, 2)
            failed.assert_called_once()
            web.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', '需要 Windows 原生平台')
    def test_real_windows_normal_and_compat_windows_open_and_all_pages_survive(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            for safe in (False, True):
                output = base / f'native-{safe}.json'
                command = [sys.executable, str(repo / 'gui_launcher.py')]
                if safe:
                    command.append('--safe-mode')
                command += ['--native-self-test', str(output)]
                # 真实窗口 + 整套页面在这台机器上正常 13 秒；全量跑到这里时机器很忙，
                # 偶发一次子进程退 1（同样命令单独跑 2/2 通过）。给足超时并重试一次，
                # 让偶发不算回归——真坏了两次都会失败。
                last = None
                for _attempt in range(2):
                    result = subprocess.run(command, cwd=repo, capture_output=True, timeout=180,
                        env=dict(os.environ, LOCALAPPDATA=str(base / f'profile-{safe}')),
                        creationflags=0x08000000)
                    if result.returncode == 0:
                        break
                    last = result
                    time.sleep(1.0)
                self.assertEqual(result.returncode, 0,
                                 (last or result).stderr.decode(errors='replace')[-1200:])
                data = json.loads(output.read_text(encoding='utf-8'))
                self.assertTrue(data['ok'] and data['native_window_test'])
                self.assertEqual(data['platform'], 'windows')
                self.assertEqual(data['safe_mode'], safe)
                self.assertEqual(data['plugins'], 63)  # 5.2.45 新增 QQ Agent 能力桥


if __name__ == '__main__':
    unittest.main()
