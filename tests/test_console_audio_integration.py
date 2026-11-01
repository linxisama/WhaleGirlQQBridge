"""Exercise the real GUI callback convention against the real v2 FastAPI routes."""
import hashlib
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from src.gui.console_client import ConsoleApiClient, ConsoleApiError
from src.gui.audio_panel import AudioPanel
from src.gui.audio_setup import AudioSetup
from src.gui.local_agent_panel import LocalAgentPanel
from src.gui.panel_requests import PanelRequests
import tests.test_audio_api as fixtures


APP = QApplication.instance() or QApplication([])


def drain(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ConsoleClientTests(unittest.TestCase):
    def test_one_prefix_and_query_survives(self):
        paths = []
        client = ConsoleApiClient(lambda path: paths.append('/api/' + path) or {'events': []},
                                  lambda path, data: paths.append('/api/' + path) or {'success': True})
        client.get('/api/v2/agent/audit?limit=10')
        client.post('/api/v2/audio/settings', {})
        self.assertEqual(paths, ['/api/v2/agent/audit?limit=10', '/api/v2/audio/settings'])

    def test_backend_failures_are_not_success(self):
        for result in ({}, {'success': False, 'message': '后台版本不匹配'},
                       {'detail': {'message': '模型未准备'}}):
            with self.subTest(result=result), self.assertRaises(ConsoleApiError):
                ConsoleApiClient(lambda _: result, None).get('/api/v2/audio/status')

    def test_foreign_or_relative_paths_rejected(self):
        for path in ('https://example.invalid', 'v2/audio/status', '/settings'):
            with self.assertRaises(ConsoleApiError):
                ConsoleApiClient.relative(path)


class ConsoleApiIntegrationTests(unittest.TestCase):
    setUp = fixtures.AudioApiTests.setUp
    tearDown = fixtures.AudioApiTests.tearDown
    wait_ready = fixtures.AudioApiTests.wait_ready

    def bridge(self):
        # The prefix below is exactly the convention qq_gui.api_get/post use.
        return ConsoleApiClient(lambda path: self.client.get('/api/' + path).json(),
                                lambda path, data: self.client.post('/api/' + path, json=data).json())

    def test_console_callback_reaches_settings_jobs_and_asset(self):
        api = self.bridge()
        self.assertEqual(api.get('/api/v2/audio/settings')['provider'], 'audio_cpp')
        job = api.post('/api/v2/audio/jobs', {'client_request_id': 'console-real-route',
                                           'mode': 'tts', 'text': '控制台接线测试'})
        row = self.wait_ready(job['job_id'])
        self.assertEqual(row['state'], 'ready')
        asset = api.get('/api/v2/audio/assets/' + row['audio']['audio_id'])
        self.assertEqual(asset['audio_id'], row['audio']['audio_id'])
        status = api.get('/api/v2/audio/status')
        self.assertEqual(Path(status['data_root']), self.root.resolve())
        self.assertEqual(self.adapter.calls, 1)

    def test_settings_only_change_selected_local_engine(self):
        config = self.bot.config
        config.values.update({'ai.api_key': 'fixture-preserve',
                              'plugins.voice_reply.enabled': False,
                              'qq_only.voice.gpt_sovits.prompt_text': '原台词'})
        response = self.client.post('/api/v2/audio/settings', json={
            'provider': 'audio_cpp', 'params': {'model': 'breeze-tts-2', 'voice': 'alice'},
            'route_policy': 'local_only'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(config.get('qq_only.voice.audio_cpp.voice'), 'alice')
        self.assertEqual(config.get('ai.api_key'), 'fixture-preserve')
        self.assertFalse(config.get('plugins.voice_reply.enabled'))
        self.assertEqual(config.get('qq_only.voice.gpt_sovits.prompt_text'), '原台词')

    def test_save_failure_rolls_memory_back(self):
        before = dict(self.bot.config.values)
        def fail():
            raise OSError('test disk failure')
        self.bot.config.save_atomic = fail
        response = self.client.post('/api/v2/audio/settings', json={
            'provider': 'audio_cpp', 'params': {'voice': 'alice'}})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.bot.config.values, before)

    def test_unknown_fields_and_external_endpoints_rejected(self):
        for params in ({'endpoint': 'https://example.invalid/v1/audio/speech'},
                       {'api_key': 'not-allowed'}, {'endpoint': 'http://127.0.0.1:18100/wrong'}):
            before = dict(self.bot.config.values)
            response = self.client.post('/api/v2/audio/settings', json={
                'provider': 'audio_cpp', 'params': params})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.bot.config.values, before)

    def test_active_job_blocks_config_change(self):
        result = self.client.post('/api/v2/audio/jobs', json={
            'client_request_id': 'running-config-guard', 'mode': 'tts', 'text': '测试'}).json()
        self.wait_ready(result['job_id'])
        self.db.execute("UPDATE audio_jobs SET state='running' WHERE job_id=?", (result['job_id'],))
        self.db.commit()
        response = self.client.post('/api/v2/audio/settings', json={
            'provider': 'audio_cpp', 'params': {'voice': 'alice'}})
        self.assertEqual(response.status_code, 409)

    def test_probe_is_bounded_get_without_synthesis_or_config_change(self):
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json={'status': 'ok'})
        gateway = self.app.state.audio_gateway
        gateway.http_transport = httpx.MockTransport(handler)
        before = dict(self.bot.config.values)
        result = self.client.post('/api/v2/audio/probe', json={
            'provider': 'audio_cpp', 'params': {}}).json()
        self.assertTrue(result['reachable'])
        self.assertFalse(result['synthesis_verified'])
        self.assertEqual(calls, [('GET', '/health')])
        self.assertEqual(self.bot.config.values, before)
        self.assertEqual(self.adapter.calls, 0)

    def test_qwen_discovery_does_not_claim_loaded_model(self):
        self.app.state.audio_gateway.http_transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={'models': [{'name': 'prepared-model'}]}))
        result = self.client.post('/api/v2/audio/probe', json={
            'provider': 'qwen_webui', 'params': {'model': 'prepared-model'}}).json()
        self.assertTrue(result['reachable'])
        self.assertEqual(result['models'], ['prepared-model'])
        self.assertFalse(result['synthesis_verified'])

    def test_capability_credentials_do_not_prove_synthesis(self):
        data = self.client.get('/api/v2/audio/capabilities').json()
        self.assertFalse(data['engines']['audio_cpp']['verified'])


class PanelIntegrationTests(unittest.TestCase):
    def test_preview_never_falls_back_to_unrelated_latest_audio(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'data/voice/assets'
            directory.mkdir(parents=True)
            audio = fixtures.make_wav(0.2)
            (directory / (hashlib.sha256(audio).hexdigest() + '.wav')).write_bytes(audio)
            panel = AudioPanel(lambda _: {'state': 'failed'}, lambda *_: {}, Path(tmp))
            with patch('src.gui.audio_panel.QDesktopServices.openUrl') as opened:
                self.assertFalse(panel.preview('failed-job'))
                opened.assert_not_called()
            panel.cleanup()

    def test_preview_matches_ready_job_and_checks_hash(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'data/voice/assets'
            directory.mkdir(parents=True)
            audio = fixtures.make_wav(0.2)
            digest = hashlib.sha256(audio).hexdigest()
            path = directory / (digest + '.wav')
            path.write_bytes(audio)
            panel = AudioPanel(lambda _: {'state': 'ready', 'audio': {'audio_id': digest}},
                               lambda *_: {}, Path(tmp))
            with patch('src.gui.audio_panel.QDesktopServices.openUrl', return_value=True) as opened:
                self.assertTrue(panel.preview('ready-job'))
                opened.assert_called_once()
                path.write_bytes(b'changed')
                self.assertFalse(panel.preview('ready-job'))
                self.assertEqual(opened.call_count, 1)
            panel.cleanup()

    def test_panel_io_stays_off_gui_and_close_waits_for_result(self):
        owner = AudioPanel(None, None)
        runner = owner.requests
        gate = threading.Event()
        observed = []
        def work():
            observed.append(('worker', QThread.currentThread() is APP.thread()))
            gate.wait(2)
            return 'done'
        runner.start(work, lambda value: observed.append((value, QThread.currentThread() is APP.thread())),
                     lambda exc: self.fail(str(exc)))
        self.assertFalse(owner.cleanup())
        self.assertFalse(owner.close())
        gate.set()
        self.assertTrue(drain(lambda: not runner.busy))
        self.assertEqual(observed, [('worker', False), ('done', True)])
        self.assertTrue(owner.cleanup())

    def test_callback_error_is_reported_on_gui_thread(self):
        panel = AudioPanel(None, None)
        errors = []
        def broken(_):
            raise ValueError('invalid response')
        panel.requests.start(lambda: {}, broken, lambda exc: errors.append(str(exc)))
        self.assertTrue(drain(lambda: not panel.requests.busy))
        self.assertEqual(errors, ['invalid response'])
        panel.cleanup()

    def test_agent_unpaired_state_is_honest_and_resume_reaches_api(self):
        posts = []
        def get(path):
            return {'devices': [], 'stopped': True} if path.endswith('/status') else {'pending': [], 'events': []}
        panel = LocalAgentPanel(get, lambda path, data: posts.append(path) or {'stopped': False})
        panel.request_refresh()
        self.assertTrue(drain(lambda: not panel.busy))
        self.assertIn('尚未配对', panel.status.text())
        panel.btn_resume.click()
        self.assertTrue(drain(lambda: not panel.busy))
        self.assertIn('/api/v2/agent/resume', posts)
        panel.cleanup()


if __name__ == '__main__':
    unittest.main()
