"""安装版内置 MCP 宿主：没有源码仓库时也能把三条桥登记进 DSH。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import httpx

from src import agent_backend_api as api
from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root


class DshBundledTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api._reset_for_tests()
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = os.path.join(self.temp.name, 'dsh')
        self.keys = ('DSH_HOME', 'WHALE_QQ_HOME', api.DSH_REPO_ENV)
        self.saved = {key: os.environ.get(key) for key in self.keys}
        os.environ['DSH_HOME'] = self.home
        for key in ('WHALE_QQ_HOME', api.DSH_REPO_ENV):
            os.environ.pop(key, None)
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')
        self.exe = os.path.join(self.temp.name, 'WhaleGirlQQBridge.exe')
        with open(self.exe, 'wb') as handle:
            handle.write(b'MZ fake')

    async def asyncTearDown(self):
        await self.client.aclose()
        api._reset_for_tests()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        await self.bot.close()
        self.temp.cleanup()

    def bundled(self):
        return mock.patch.multiple(sys, frozen=True, _MEIPASS=self.temp.name,
                                   executable=self.exe, create=True)

    async def status(self, **params):
        return await self.client.get('/api/agent/backends/dsh/status', params=params)

    async def test_status_uses_bundled_host_without_repo(self):
        with self.bundled():
            response = await self.status(profile='desktop')
        body = response.json()
        self.assertTrue(body['ok'], body)
        self.assertEqual('bundled', body['mcp_mode'])
        self.assertEqual('bundled', body['repo_root_source'])
        self.assertEqual('', body['repo_root'])
        self.assertEqual(self.exe, body['mcp_command'])
        self.assertEqual(['--mcp', 'memory'], body['mcp_args']['memory'])
        self.assertEqual([], body['control_args'])
        self.assertEqual('', body['command'])

    async def test_link_registers_exe_for_all_three_entries(self):
        with self.bundled(), mock.patch.object(api, 'dsh_bundled_check',
                                                return_value={'ok': True, 'error': ''}):
            linked = (await self.client.post('/api/agent/backends/dsh/link',
                                             json={'profile': 'desktop'})).json()
        self.assertTrue(linked['ok'], linked)
        self.assertEqual('bundled', linked['mcp_mode'])
        self.assertEqual(self.exe, linked['mcp_command'])
        self.assertTrue(linked['python_check']['ok'])
        with self.bundled():
            body = (await self.status(profile='desktop')).json()
        self.assertTrue(body['tools_installed'], body)
        self.assertEqual(3, body['entry_count'])
        for key in ('memory_command', 'control_command', 'tools_command'):
            self.assertEqual(self.exe, body[key])
        self.assertEqual(['--mcp', 'memory'], body['memory_args'])
        self.assertEqual(['--mcp', 'bot'], body['control_args'])
        self.assertEqual(['--mcp', 'tools'], body['tools_args'])
        self.assertNotIn('_internal', json.dumps(body))

    async def test_explicit_bad_repo_still_reports_the_typo(self):
        with self.bundled(), mock.patch.object(api, 'dsh_bundled_check',
                                                return_value={'ok': True, 'error': ''}):
            response = await self.status(profile='desktop', repo_root='Z:/missing')
        self.assertEqual(400, response.status_code)
        self.assertEqual('dsh_needs_source', response.json()['code'])

    async def test_bundled_exe_is_not_a_python_interpreter(self):
        with mock.patch.multiple(sys, frozen=True, _MEIPASS=self.temp.name,
                                 executable=sys.executable, create=True):
            self.assertEqual('', api.dsh_bundled_exe())


if __name__ == '__main__':
    unittest.main()
