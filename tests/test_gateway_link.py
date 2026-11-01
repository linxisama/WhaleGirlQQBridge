"""管理器（WhaleQQGateway）连接信息读取测试。"""
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import src.gateway_link as gateway_link
from src.gateway_link import connection_paths, fetch_accounts, manager_exe, read_connection


class _Handler(BaseHTTPRequestHandler):
    token = 'tok'

    def do_GET(self):  # noqa: N802
        if self.path != '/v1/accounts':
            self.send_response(404); self.end_headers(); return
        if self.headers.get('Authorization') != 'Bearer ' + self.token:
            self.send_response(401); self.end_headers(); return
        body = json.dumps({'items': [{'account_id': 'a1', 'label': '我的 QQ',
                                      'state': 'online'}], 'active': 'a1'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class GatewayLinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = pathlib.Path(self._tmp.name) / 'connection.json'

    def test_reads_live_connection(self):
        self.path.write_text(json.dumps({'base_url': 'http://127.0.0.1:1',
                                         'token': 't', 'pid': os.getpid()}), encoding='utf-8')
        data = read_connection(self.path)
        self.assertEqual(data['base_url'], 'http://127.0.0.1:1')

    def test_dead_pid_is_rejected(self):
        self.path.write_text(json.dumps({'base_url': 'http://127.0.0.1:1',
                                         'token': 't', 'pid': 999999}), encoding='utf-8')
        self.assertEqual(read_connection(self.path), {})

    def test_missing_or_broken_is_empty(self):
        self.assertEqual(read_connection(self.path), {})
        self.path.write_text('{not json', encoding='utf-8')
        self.assertEqual(read_connection(self.path), {})

    def test_connection_paths_cover_appdata_and_install_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = pathlib.Path(tmp) / 'local'
            roaming = pathlib.Path(tmp) / 'roaming'
            prog = pathlib.Path(tmp) / 'prog'
            env = {'LOCALAPPDATA': str(local), 'APPDATA': str(roaming),
                   'ProgramData': str(pathlib.Path(tmp) / 'programdata'),
                   'ProgramFiles': str(prog), 'ProgramFiles(x86)': str(prog),
                   'ProgramW6432': str(prog)}
            with mock.patch.dict(os.environ, env, clear=False):
                paths = [str(item).lower() for item in connection_paths()]
        self.assertIn(str(local / 'WhaleQQGateway' / 'core' / 'connection.json').lower(), paths)
        self.assertIn(str(roaming / 'WhaleQQGateway' / 'core' / 'connection.json').lower(), paths)
        self.assertIn(str(prog / 'WhaleQQGateway' / 'core' / 'connection.json').lower(), paths)

    def test_read_connection_uses_next_candidate_when_first_is_dead(self):
        dead = pathlib.Path(self._tmp.name) / 'dead.json'
        live = pathlib.Path(self._tmp.name) / 'live.json'
        dead.write_text(json.dumps({'base_url': 'http://127.0.0.1:1',
                                    'token': 'old', 'pid': 999999}), encoding='utf-8')
        live.write_text(json.dumps({'base_url': 'http://127.0.0.1:2',
                                    'token': 'new', 'pid': os.getpid()}), encoding='utf-8')
        with mock.patch.object(gateway_link, 'connection_paths', return_value=[dead, live]):
            data = read_connection()
        self.assertEqual(data['base_url'], 'http://127.0.0.1:2')

    def test_manager_exe_prefers_running_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / 'Portable' / 'WhaleQQGateway.exe'
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b'MZ')
            with mock.patch.object(gateway_link, '_running_manager_exe', return_value=str(exe)):
                self.assertEqual(manager_exe(), str(exe))

    @unittest.skipUnless(os.name == 'nt', 'Windows only')
    def test_running_manager_exe_reads_get_process_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / 'WhaleQQGateway.exe'
            exe.write_bytes(b'MZ')
            completed = mock.Mock(stdout=str(exe) + '\n', returncode=0)
            with mock.patch('subprocess.run', return_value=completed):
                self.assertEqual(gateway_link._running_manager_exe(), str(exe))

    def test_fetch_accounts(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        result = fetch_accounts({'base_url': f'http://127.0.0.1:{port}', 'token': 'tok'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['label'], '我的 QQ')
        bad = fetch_accounts({'base_url': f'http://127.0.0.1:{port}', 'token': 'wrong'})
        self.assertFalse(bad['ok'])

    def test_no_connection_info(self):
        self.assertFalse(fetch_accounts({})['ok'])

    def test_denied_pid_is_accepted(self):
        self.path.write_text(json.dumps({'base_url': 'http://127.0.0.1:1',
                                         'token': 't', 'pid': 999999}), encoding='utf-8')
        with mock.patch.object(gateway_link, '_pid_state', return_value='denied'):
            data = read_connection(self.path)
        self.assertEqual(data['base_url'], 'http://127.0.0.1:1')

    @unittest.skipUnless(os.name == 'nt', 'Windows only')
    def test_pid_state_alive_and_dead(self):
        self.assertEqual(gateway_link._pid_state(os.getpid()), 'alive')
        self.assertEqual(gateway_link._pid_state(999999), 'dead')

    def test_manager_exe_finds_local_programs(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = pathlib.Path(tmp) / 'local'
            prog = pathlib.Path(tmp) / 'prog'
            (local / 'Programs' / 'WhaleQQGateway').mkdir(parents=True)
            (local / 'Programs' / 'WhaleQQGateway' / 'WhaleQQGateway.exe').write_bytes(b'MZ')
            (prog / 'WhaleQQGateway').mkdir(parents=True)
            (prog / 'WhaleQQGateway' / 'WhaleQQGateway.exe').write_bytes(b'MZ')
            env = {'LOCALAPPDATA': str(local), 'ProgramFiles': str(prog),
                   'ProgramFiles(x86)': str(prog), 'ProgramW6432': str(prog)}
            with mock.patch.object(gateway_link, '_running_manager_exe', return_value=''):
                with mock.patch.dict(os.environ, env, clear=False):
                    self.assertEqual(pathlib.Path(manager_exe()),
                                     local / 'Programs' / 'WhaleQQGateway' / 'WhaleQQGateway.exe')

    def test_manager_exe_finds_program_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = pathlib.Path(tmp) / 'local'
            prog = pathlib.Path(tmp) / 'prog'
            (prog / 'WhaleQQGateway').mkdir(parents=True)
            (prog / 'WhaleQQGateway' / 'WhaleQQGateway.exe').write_bytes(b'MZ')
            env = {'LOCALAPPDATA': str(local), 'ProgramFiles': str(prog),
                   'ProgramFiles(x86)': str(prog), 'ProgramW6432': str(prog)}
            with mock.patch.object(gateway_link, '_running_manager_exe', return_value=''):
                with mock.patch.dict(os.environ, env, clear=False):
                    self.assertEqual(pathlib.Path(manager_exe()),
                                     prog / 'WhaleQQGateway' / 'WhaleQQGateway.exe')

    def test_manager_exe_missing_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = pathlib.Path(tmp) / 'local'
            prog = pathlib.Path(tmp) / 'prog'
            env = {'LOCALAPPDATA': str(local), 'ProgramFiles': str(prog),
                   'ProgramFiles(x86)': str(prog), 'ProgramW6432': str(prog)}
            with mock.patch.object(gateway_link, '_running_manager_exe', return_value=''):
                with mock.patch.dict(os.environ, env, clear=False):
                    self.assertEqual(manager_exe(), '')


if __name__ == '__main__':
    unittest.main()