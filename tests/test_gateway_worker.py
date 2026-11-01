"""安装版控制台被网关拉起：账号配置、无控制台环境、自检入口参数。"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from src import gateway_worker

REPO = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def _closed_stdio():
    """模拟冻结窗口模式（PyInstaller windowed）：sys.stdout / sys.stderr 是 None。"""
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = None
    try:
        yield
    finally:
        sys.stdout, sys.stderr = saved


class GatewayWorkerPrepareTests(unittest.TestCase):
    def test_prepare_writes_qqbridge_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'bot'
            job = {'root': str(root), 'ws_url': 'ws://127.0.0.1:1/v1/accounts/a/onebot',
                   'access_token': 'relay-token', 'port': 1, 'parent': 0}
            gateway_worker.prepare(job)
            config = yaml.safe_load((root / 'config.qq.yaml').read_text(encoding='utf-8'))
            self.assertEqual(config['qq_only']['mode'], 'qqbridge')
            self.assertEqual(config['qq_only']['qqbridge']['ws_url'], job['ws_url'])
            self.assertEqual(config['qq_only']['qqbridge']['access_token'], 'relay-token')
            self.assertFalse(config['qq_only']['auto_start'])
            self.assertNotIn('access_token_encrypted', config['qq_only']['qqbridge'])

    def test_prepare_keeps_existing_account_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'bot'
            root.mkdir()
            (root / 'config.qq.yaml').write_text(
                'qq_only:\n  mode: onebot\n  groups: [123]\n', encoding='utf-8')
            gateway_worker.prepare({'root': str(root), 'ws_url': 'ws://x/y',
                                    'access_token': 't', 'port': 1, 'parent': 0})
            config = yaml.safe_load((root / 'config.qq.yaml').read_text(encoding='utf-8'))
            self.assertEqual(config['qq_only']['mode'], 'qqbridge')
            self.assertEqual(config['qq_only']['groups'], [123])

    def test_gui_launcher_dispatches_gateway_job(self):
        source = (REPO / 'gui_launcher.py').read_text(encoding='utf-8')
        self.assertIn('--gateway-job', source)
        self.assertIn('src.gateway_worker', source)


class GatewayWorkerStdioTests(unittest.TestCase):
    def test_log_dir_follows_account_log_convention(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = gateway_worker.log_dir(Path(tmp) / 'bot')
            self.assertEqual(folder, Path(tmp) / 'bot' / 'data' / 'qq-only' / 'logs')
            self.assertTrue(folder.is_dir())

    def test_ensure_stdio_keeps_working_streams_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(gateway_worker.ensure_stdio(Path(tmp)))
            self.assertFalse((Path(tmp) / 'data' / 'qq-only' / 'logs').exists())

    def test_ensure_stdio_redirects_closed_streams_to_account_log(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / 'bot'
            with _closed_stdio():
                stream = gateway_worker.ensure_stdio(root)
                self.assertIsNotNone(stream)
                self.assertIs(sys.stdout, stream)
                self.assertIs(sys.stderr, stream)
                print('gateway-stdio-probe')
                stream.close()
            logs = sorted((root / 'data' / 'qq-only' / 'logs').glob('gateway-console-*.log'))
            self.assertEqual(len(logs), 1)
            self.assertIn('gateway-stdio-probe', logs[0].read_text(encoding='utf-8'))


class GatewayWorkerEnvFlagTests(unittest.TestCase):
    def test_autostart_defaults_on(self):
        self.assertTrue(gateway_worker.autostart_enabled({}))
        self.assertTrue(gateway_worker.autostart_enabled({'WHALE_GATEWAY_AUTOSTART': '1'}))

    def test_autostart_off_only_on_zero(self):
        self.assertFalse(gateway_worker.autostart_enabled({'WHALE_GATEWAY_AUTOSTART': '0'}))
        self.assertFalse(gateway_worker.autostart_enabled({'WHALE_GATEWAY_AUTOSTART': ' 0 '}))
        self.assertTrue(gateway_worker.autostart_enabled({'WHALE_GATEWAY_AUTOSTART': 'off'}))

    def test_smoke_flag_matches_gateway_bot_worker(self):
        self.assertFalse(gateway_worker.smoke_enabled({}))
        self.assertFalse(gateway_worker.smoke_enabled({'WHALE_GATEWAY_WORKER_SMOKE': ''}))
        self.assertTrue(gateway_worker.smoke_enabled({'WHALE_GATEWAY_WORKER_SMOKE': '1'}))


class UvicornLoggingTests(unittest.TestCase):
    """缺陷A 的机制本身：默认日志配置在无控制台时会崩，log_config=None 不会。"""

    def test_default_log_config_breaks_without_stdio(self):
        import uvicorn
        with _closed_stdio():
            with self.assertRaises(ValueError):
                uvicorn.Config(object(), log_level='warning', access_log=False)
            config = uvicorn.Config(object(), log_level='warning', access_log=False,
                                     log_config=None)
        self.assertIsNone(config.log_config)

    def test_gateway_worker_disables_uvicorn_log_config(self):
        source = (REPO / 'src' / 'gateway_worker.py').read_text(encoding='utf-8')
        self.assertIn('log_config=None', source)
        self.assertIn("console-smoke.png", source)
        self.assertIn('QTimer.singleShot(2500', source)
        self.assertIn('QTimer.singleShot(3000', source)


class SelfTestArgumentTests(unittest.TestCase):
    """缺陷B：--self-test 不带输出位置不能 IndexError。"""

    def test_selftest_without_path_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env['LOCALAPPDATA'] = tmp
            env['QT_QPA_PLATFORM'] = 'offscreen'
            done = subprocess.run([sys.executable, 'qq_gui.py', '--self-test'],
                                  cwd=str(REPO), env=env, timeout=300,
                                  capture_output=True, text=True, encoding='utf-8',
                                  errors='replace')
            self.assertEqual(done.returncode, 2, done.stderr[-2000:])
            self.assertNotIn('IndexError', done.stderr)
            self.assertNotIn('Traceback', done.stderr)
            self.assertIn('--self-test', done.stderr)
            usage = Path(tmp) / 'WhaleGirlQQ' / 'logs' / 'selftest-usage.log'
            self.assertTrue(usage.is_file())
            self.assertIn('--self-test', usage.read_text(encoding='utf-8'))


class HeadlessAutostartTests(unittest.TestCase):
    """无窗口模式下自动启动失败：留证据、留后台，不能当场退出。"""

    def test_report_writes_log_and_notes(self):
        seen = []

        class FakeRuntime:
            def note(self, text):
                seen.append(text)

        with tempfile.TemporaryDirectory() as tmp:
            try:
                raise ValueError('请先配置 API 地址和密钥')
            except ValueError as error:
                path = gateway_worker.report_autostart_failure(Path(tmp), FakeRuntime(), error)
            self.assertIsNotNone(path)
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("自动启动机器人失败", text)
            self.assertIn("请先配置 API 地址和密钥", text)
            self.assertIn("Traceback", text)
            self.assertTrue(seen)
            self.assertIn("机器人没起来", seen[0])

    def test_main_survives_headless_start_failure(self):
        import asyncio
        import threading
        from unittest import mock

        loop = asyncio.new_event_loop()
        worker = threading.Thread(target=loop.run_forever, daemon=True)
        worker.start()
        joiner = threading.Thread(target=lambda: None)
        joiner.start()
        notes = []

        class FakeRuntime:
            def note(self, text):
                notes.append(text)

            async def start(self):
                raise ValueError("请先配置 API 地址和密钥")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bot"
            root.mkdir()
            job_path = Path(tmp) / "launch.json"
            job_path.write_text('{"root": "%s", "port": 1}' % str(root).replace("\\", "\\\\"),
                                encoding="utf-8")
            runtime = FakeRuntime()
            with mock.patch.object(gateway_worker, "prepare", lambda job: root), \
                    mock.patch.object(gateway_worker, "ensure_stdio", lambda r: None), \
                    mock.patch.object(gateway_worker, "start_backend",
                                      lambda job, r: (runtime, None, None, joiner, {"loop": loop})), \
                    mock.patch.dict(os.environ, {"WHALE_GATEWAY_HEADLESS": "1"}):
                code = gateway_worker.main(job_path)
            self.assertEqual(code, 0)
            self.assertTrue(notes)
            self.assertIn("机器人没起来", notes[0])
            self.assertTrue((root / "data" / "qq-only" / "logs" / "gateway-boot.log").is_file())
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=5)

class GatewayPluginStoreTests(unittest.TestCase):
    """网关账号必须有自己完整的插件库，不能永远回退到共享目录。"""

    def _shared(self, base: Path) -> Path:
        shared = base / 'local' / 'WhaleGirlQQ' / 'data' / 'qq-only'
        (shared / 'qqagent_host' / 'plugins' / 'ext-a').mkdir(parents=True)
        (shared / 'qqagent_host' / 'host-one.mjs').write_text('// host', encoding='utf-8')
        (shared / 'qqagent_host' / 'plugins' / 'ext-a' / 'plugin.json').write_text(
            '{"id":"ext-a","name":"甲"}', encoding='utf-8')
        (shared / 'qqagent_extensions').mkdir()
        (shared / 'qqagent_extensions.json').write_text(
            '{"version":1,"extensions":{"ext-a":{"enabled":true}}}', encoding='utf-8')
        (shared / 'qqagent_bridge_catalog.json').write_text('[]', encoding='utf-8')
        return shared

    def _job(self, root: Path) -> dict:
        return {'root': str(root), 'ws_url': 'ws://x/y', 'access_token': 't',
                'port': 1, 'parent': 0}

    def test_prepare_migrates_shared_store_and_rewrites_bridge_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            shared = self._shared(base)
            settings = {'qqagent_bridge': {
                'enabled': True,
                'hostRoot': str(shared / 'qqagent_host'),
                'catalogPath': str(shared / 'qqagent_bridge_catalog.json'),
                'extensionsDir': str(shared / 'qqagent_host' / 'plugins'),
                'sourceDir': str(shared / 'source'),
                # 手写配置可能用正斜杠，迁移时也必须改到账号目录。
                'extensionPath': str(shared / 'qqagent_host' / 'plugins' / 'ext-a').replace('\\', '/'),
            }}
            (shared / 'plugin_settings.json').write_text(
                json.dumps(settings, ensure_ascii=False), encoding='utf-8')
            root = base / 'bot'
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(base / 'local')}):
                gateway_worker.prepare(self._job(root))
            own = root / 'data' / 'qq-only'
            self.assertTrue((own / 'qqagent_host' / 'host-one.mjs').is_file())
            self.assertTrue((own / 'qqagent_extensions.json').is_file())
            self.assertTrue((own / 'qqagent_bridge_catalog.json').is_file())
            saved = json.loads(
                (own / 'plugin_settings.json').read_text(encoding='utf-8'))
            bridge = saved['qqagent_bridge']
            self.assertEqual(bridge['hostRoot'], str(own / 'qqagent_host'))
            self.assertEqual(bridge['extensionsDir'], str(own / 'qqagent_host' / 'plugins'))
            self.assertEqual(bridge['catalogPath'], str(own / 'qqagent_bridge_catalog.json'))
            self.assertEqual(bridge['sourceDir'], str(own / 'source'))

            from src.qq_app import QQRuntime
            runtime = object.__new__(QQRuntime)
            runtime.root = root
            self.assertEqual(runtime._plugin_dir(), own)

    def test_prepare_merges_settings_without_overwriting_account_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            shared = self._shared(base)
            shared_settings = {'qqagent_bridge': {
                'enabled': True, 'ownerIds': '2019117802',
                'hostRoot': str(shared / 'qqagent_host')},
                'sharedOnly': {'added': True}}
            (shared / 'plugin_settings.json').write_text(
                json.dumps(shared_settings, ensure_ascii=False), encoding='utf-8')
            root = base / 'bot'
            own = root / 'data' / 'qq-only'
            own.mkdir(parents=True)
            own_settings = {'qqagent_bridge': {'enabled': False, 'custom': 'keep'},
                            'ownOnly': {'keep': True}}
            (own / 'plugin_settings.json').write_text(
                json.dumps(own_settings, ensure_ascii=False), encoding='utf-8')
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(base / 'local')}):
                gateway_worker.prepare(self._job(root))
            saved = json.loads(
                (own / 'plugin_settings.json').read_text(encoding='utf-8'))
            self.assertFalse(saved['qqagent_bridge']['enabled'])
            self.assertEqual(saved['qqagent_bridge']['custom'], 'keep')
            self.assertEqual(saved['qqagent_bridge']['ownerIds'], '2019117802')
            self.assertEqual(saved['qqagent_bridge']['hostRoot'], str(own / 'qqagent_host'))
            self.assertTrue(saved['ownOnly']['keep'])
            self.assertTrue(saved['sharedOnly']['added'])

    def test_prepare_repairs_partial_own_host_without_overwriting_user_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            shared = self._shared(base)
            root = base / 'bot'
            own_host = root / 'data' / 'qq-only' / 'qqagent_host'
            own_host.mkdir(parents=True)
            own_ext = own_host / 'plugins' / 'ext-b'
            own_ext.mkdir(parents=True)
            (own_ext / 'user.txt').write_text('keep', encoding='utf-8')
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(base / 'local')}):
                gateway_worker.prepare(self._job(root))
            self.assertTrue((own_host / 'src' / 'config.js').is_file())
            self.assertTrue((own_host / 'src' / 'plugin-loader.js').is_file())
            self.assertTrue((own_host / 'host-one.mjs').is_file())
            self.assertEqual((own_ext / 'user.txt').read_text(encoding='utf-8'), 'keep')
            self.assertTrue((own_host / 'plugins' / 'ext-a' / 'plugin.json').is_file())

    def test_prepare_bootstraps_host_without_shared_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'bot'
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(base / 'empty-local')}):
                gateway_worker.prepare(self._job(root))
            host = root / 'data' / 'qq-only' / 'qqagent_host'
            self.assertTrue((host / 'src' / 'config.js').is_file())
            self.assertTrue((host / 'src' / 'plugin-loader.js').is_file())
            self.assertTrue((host / 'host-one.mjs').is_file())


if __name__ == '__main__':
    unittest.main()
