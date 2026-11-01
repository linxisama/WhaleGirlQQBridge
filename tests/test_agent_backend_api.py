"""W5b：外部 agent 后端 HTTP 端点（/api/agent/backends）。"""
import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest

import httpx
from fastapi import FastAPI

from src import agent_backend_api as api
from src import agent_workspace
from src.agent_backends import load_profiles
from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root

LONG_ARGS = ['-c', 'import time; time.sleep(30)']


class _NoRoot:
    """没有 root 的假 bot：验证 data_dir 未就绪时的行为。"""


class BackendApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api._reset_for_tests()
        # 子进程 cwd 落在 temp 里，taskkill 之后句柄要一点时间才释放：宽容清理避免抖动。
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        # 真进程一定要收干净：stop 全部登记后端后再清表。
        await asyncio.to_thread(api.shutdown_all)
        api._reset_for_tests()
        await self.bot.close()
        await asyncio.sleep(.1)
        self.temp.cleanup()

    # ── 小工具 ──────────────────────────────────────────────────────────────

    def profile_file(self):
        return self.root / 'agent_backends.json'

    async def put(self, backend_id, profile, *, kind='cli', create_only=False):
        return await self.client.post('/api/agent/backends', json={
            'id': backend_id, 'kind': kind, 'profile': profile,
            'create_only': create_only})

    async def long_profile(self, backend_id, timeout=30):
        return await self.put(backend_id, {'command': sys.executable,
                                           'args': list(LONG_ARGS),
                                           'workdir': str(self.root),
                                           'timeout': timeout})

    async def wait_state(self, backend_id, states, timeout=15.0):
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            response = await self.client.get('/api/agent/backends/%s/status' % backend_id)
            last = response.json()
            if last.get('state') in states:
                return last
            await asyncio.sleep(.05)
        self.fail('状态没进入 %s：%s' % (states, last))

    @staticmethod
    def pid_listed(pid):
        out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % int(pid)],
                             capture_output=True, text=True)
        return str(int(pid)) in (out.stdout or '')

    # ── 列表 ────────────────────────────────────────────────────────────────

    async def test_list_empty_state(self):
        response = await self.client.get('/api/agent/backends')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['backends'], [])
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['live'], [])
        self.assertEqual(body['kinds'], ['cli', 'dsh'])

    async def test_list_reports_data_dir_and_profile_file(self):
        body = (await self.client.get('/api/agent/backends')).json()
        self.assertTrue(body['data_dir_ready'])
        self.assertEqual(body['profile_file'],
                         str(self.profile_file()))
        self.assertFalse(self.profile_file().exists())

    async def test_list_corrupt_profile_file(self):
        self.profile_file().write_text('{not json', encoding='utf-8')
        response = await self.client.get('/api/agent/backends')
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['code'], 'bad_profile_file')
        self.assertEqual(body['backends'], [])

    # ── 新增 / 更新 ─────────────────────────────────────────────────────────

    async def test_create_valid_profile_persists(self):
        response = await self.put('cli-a', {'command': sys.executable,
                                            'args': ['-c', 'print(1)'],
                                            'workdir': str(self.root), 'timeout': 5})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['created'])
        self.assertFalse(body['updated'])
        saved = load_profiles(str(self.profile_file()))
        self.assertTrue(saved['ok'])
        self.assertIn('cli-a', saved['profiles'])
        self.assertEqual(saved['profiles']['cli-a']['kind'], 'cli')

    async def test_create_invalid_profile_returns_bad_profile(self):
        response = await self.put('cli-b', {'command': ''})
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertEqual(body['code'], 'bad_profile')
        self.assertFalse(self.profile_file().exists())

    async def test_create_unknown_key_still_bad_profile(self):
        response = await self.put('cli-c', {'command': sys.executable, 'nope': 1})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['code'], 'bad_profile')

    async def test_create_bad_id_rejected(self):
        response = await self.put('有 空格', {'command': sys.executable})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['code'], 'bad_profile')

    async def test_create_duplicate_id(self):
        await self.put('cli-d', {'command': sys.executable})
        again = await self.put('cli-d', {'command': sys.executable}, create_only=True)
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(again.json()['code'], 'duplicate_id')

    async def test_update_existing_profile(self):
        await self.put('cli-e', {'command': sys.executable, 'timeout': 5})
        updated = await self.put('cli-e', {'command': sys.executable, 'timeout': 9})
        self.assertEqual(updated.status_code, 200, updated.text)
        body = updated.json()
        self.assertTrue(body['updated'])
        self.assertFalse(body['created'])
        self.assertEqual(body['profile']['timeout'], 9.0)

    async def test_update_running_profile_rejected(self):
        await self.long_profile('cli-f')
        (await self.client.post('/api/agent/backends/cli-f/start')).raise_for_status()
        await self.wait_state('cli-f', ('running',))
        response = await self.put('cli-f', {'command': sys.executable, 'timeout': 5})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['code'], 'already_running')
        await self.client.post('/api/agent/backends/cli-f/stop')

    # ── 探测 ────────────────────────────────────────────────────────────────

    async def test_probe_ok(self):
        await self.put('cli-g', {'command': sys.executable, 'workdir': str(self.root)})
        response = await self.client.post('/api/agent/backends/cli-g/probe')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['runnable'])
        self.assertEqual(body['returncode'], 0)

    async def test_probe_command_not_found(self):
        await self.put('cli-h', {'command': 'w5b-no-such-command-xyz'})
        response = await self.client.post('/api/agent/backends/cli-h/probe')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['code'], 'command_not_found')

    # ── 启动 / 停止 ─────────────────────────────────────────────────────────

    async def test_start_returns_immediately(self):
        await self.long_profile('cli-i')
        began = time.monotonic()
        response = await self.client.post('/api/agent/backends/cli-i/start')
        elapsed = time.monotonic() - began
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertFalse(body['blocking'])
        self.assertIn(body['state'], ('starting', 'running'))
        self.assertLess(elapsed, 0.5, 'start 阻塞了事件循环：%.3fs' % elapsed)

    async def test_start_twice_already_running(self):
        await self.long_profile('cli-j')
        first = await self.client.post('/api/agent/backends/cli-j/start')
        self.assertEqual(first.status_code, 202, first.text)
        second = await self.client.post('/api/agent/backends/cli-j/start')
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(second.json()['code'], 'already_running')
        await self.client.post('/api/agent/backends/cli-j/stop')

    async def test_stop_kills_real_process(self):
        await self.long_profile('cli-k')
        await self.client.post('/api/agent/backends/cli-k/start')
        running = await self.wait_state('cli-k', ('running',))
        pid = running['pid']
        self.assertTrue(pid and self.pid_listed(pid), '未能在 tasklist 里找到 pid %s' % pid)
        stopped = await self.client.post('/api/agent/backends/cli-k/stop')
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertTrue(stopped.json()['stopped'])
        await self.wait_state('cli-k', ('stopped',))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.pid_listed(pid):
            await asyncio.sleep(.05)
        self.assertFalse(self.pid_listed(pid), 'pid %s 还活着' % pid)

    # ── 删除 ────────────────────────────────────────────────────────────────

    async def test_delete_running_rejected(self):
        await self.long_profile('cli-l')
        await self.client.post('/api/agent/backends/cli-l/start')
        await self.wait_state('cli-l', ('running',))
        response = await self.client.delete('/api/agent/backends/cli-l')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['code'], 'already_running')
        await self.client.post('/api/agent/backends/cli-l/stop')
        self.assertIn('cli-l', load_profiles(str(self.profile_file()))['profiles'])

    async def test_delete_after_stop(self):
        await self.put('cli-m', {'command': sys.executable})
        response = await self.client.delete('/api/agent/backends/cli-m')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['removed'], 'cli-m')
        self.assertNotIn('cli-m', load_profiles(str(self.profile_file()))['profiles'])

    # ── 状态 ────────────────────────────────────────────────────────────────

    async def test_status_unknown_id(self):
        response = await self.client.get('/api/agent/backends/nope/status')
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()['code'], 'not_found')

    async def test_status_idle_after_create(self):
        await self.put('cli-n', {'command': sys.executable})
        body = (await self.client.get('/api/agent/backends/cli-n/status')).json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['state'], 'idle')
        self.assertFalse(body['running'])
        self.assertFalse(body['running_thread'])

    # ── 工作区 ──────────────────────────────────────────────────────────────

    async def test_workspace_lists_entries(self):
        await self.put('cli-o', {'command': sys.executable})
        root = agent_workspace.root(str(self.root), 'cli-o')
        root.mkdir(parents=True, exist_ok=True)
        (root / 'notes.txt').write_text('hi', encoding='utf-8')
        response = await self.client.get('/api/agent/backends/cli-o/workspace')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        names = [row['name'] for row in body['entries']]
        self.assertIn('notes.txt', names)
        self.assertEqual(body['count'], len(names))

    async def test_workspace_unknown_id(self):
        response = await self.client.get('/api/agent/backends/nope/workspace')
        self.assertEqual(response.status_code, 404, response.text)

    # ── 鉴权 ────────────────────────────────────────────────────────────────

    def bare_app(self):
        app = FastAPI()
        app.state.bot = self.bot
        api.install_agent_backend_routes(app)
        return app

    async def test_auth_foreign_client_rejected(self):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.bare_app(),
                                          client=('10.1.2.3', 50000)),
            base_url='http://127.0.0.1')
        try:
            response = await client.get('/api/agent/backends')
        finally:
            await client.aclose()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()['code'], 'auth_denied')

    async def test_auth_origin_mismatch_rejected(self):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.bare_app()),
            base_url='http://127.0.0.1')
        try:
            response = await client.get('/api/agent/backends',
                                        headers={'origin': 'http://evil.example'})
        finally:
            await client.aclose()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()['code'], 'auth_denied')

    async def test_auth_forwarded_header_rejected(self):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.bare_app()),
            base_url='http://127.0.0.1')
        try:
            response = await client.get('/api/agent/backends',
                                        headers={'x-forwarded-for': '8.8.8.8'})
        finally:
            await client.aclose()
        self.assertEqual(response.status_code, 403, response.text)

    async def test_auth_hosted_app_also_blocks_foreign_client(self):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=('192.168.1.9', 4444)),
            base_url='http://127.0.0.1')
        try:
            response = await client.get('/api/agent/backends')
        finally:
            await client.aclose()
        self.assertEqual(response.status_code, 403, response.text)

    # ── data_dir 未就绪 ─────────────────────────────────────────────────────

    async def test_no_data_dir_list_and_post(self):
        self.app.state.bot = _NoRoot()
        listing = await self.client.get('/api/agent/backends')
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertFalse(listing.json()['data_dir_ready'])
        self.assertEqual(listing.json()['profile_file'], '')
        created = await self.put('cli-p', {'command': sys.executable})
        self.assertEqual(created.status_code, 503, created.text)
        self.assertEqual(created.json()['code'], 'no_data_dir')

    # ── 退出收尾 ────────────────────────────────────────────────────────────

    async def test_shutdown_all_stops_registered(self):
        await self.long_profile('cli-q')
        await self.client.post('/api/agent/backends/cli-q/start')
        running = await self.wait_state('cli-q', ('running',))
        result = await asyncio.to_thread(api.shutdown_all)
        self.assertTrue(result['ok'])
        self.assertEqual(result['count'], 1)
        self.assertTrue(result['stopped'][0]['stopped'])
        pid = running['pid']
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.pid_listed(pid):
            await asyncio.sleep(.05)
        self.assertFalse(self.pid_listed(pid), 'shutdown_all 之后 pid %s 还活着' % pid)

    async def test_config_yaml_untouched(self):
        await self.put('cli-r', {'command': sys.executable, 'timeout': 7})
        config = self.root / 'config.qq.yaml'
        self.assertFalse(config.exists(), 'profile 不该写进 config.yaml')
        dumped = json.dumps(load_profiles(str(self.profile_file()))['profiles'])
        self.assertIn('cli-r', dumped)


if __name__ == '__main__':
    unittest.main()
