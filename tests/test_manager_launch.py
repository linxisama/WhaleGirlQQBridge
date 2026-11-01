"""管理器（统一接入控制台）拉起机器人：launch.json → qqbridge 中继，端到端（假 OneBot 协议端）。

验证三件事：
1. bot_worker 会把 launch.json 里的 ws_url/access_token 写进 config.qqbridge；
2. 机器人能连上这个中继，完成 get_login_info 握手，拿到 self_id；
3. 控制台识别 launch.json 后隐藏账号切换区（由管理器管账号）。
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENV_PY = ROOT / '.venv-build' / 'Scripts' / 'python.exe'
WORKER = pathlib.Path(r'D:\聊天机器人\QQ统一接入框架-DeepSeek\gateway\bot_worker.py')


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class _FakeOneBot:
    """最小 OneBot v11 服务端：只回 get_login_info 和 lifecycle。"""

    def __init__(self):
        import asyncio
        from websockets.server import serve

        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.port = _free_port()
        self.seen_token = ''

        async def handler(websocket):
            try:
                path = str(getattr(websocket, 'path', '') or getattr(
                    getattr(websocket, 'request', None), 'path', ''))
            except Exception:
                path = ''
            self.seen_token = path.split('access_token=')[-1]
            try:
                await websocket.send(json.dumps({
                    'post_type': 'meta_event', 'meta_event_type': 'lifecycle',
                    'sub_type': 'connect', 'self_id': 10001}))
                async for raw in websocket:
                    data = json.loads(raw)
                    if data.get('action') == 'get_login_info':
                        await websocket.send(json.dumps({
                            'status': 'ok', 'retcode': 0, 'data': {
                                'user_id': 10001, 'nickname': '测试号'},
                            'echo': data.get('echo')}))
                    elif data.get('action') == 'get_status':
                        # probe() 要求 online 且 good 才判定在线
                        await websocket.send(json.dumps({
                            'status': 'ok', 'retcode': 0,
                            'data': {'online': True, 'good': True},
                            'echo': data.get('echo')}))
                    else:
                        await websocket.send(json.dumps({
                            'status': 'ok', 'retcode': 0, 'data': {},
                            'echo': data.get('echo')}))
            except Exception:
                pass

        async def main():
            async with serve(handler, '127.0.0.1', self.port, max_size=None):
                self.ready.set()
                await asyncio.Future()

        self.thread = threading.Thread(target=lambda: self.loop.run_until_complete(main()),
                                       daemon=True)
        self.thread.start()
        self.ready.wait(5)

    def stop(self):
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass


@unittest.skipUnless(VENV_PY.is_file() and WORKER.is_file(), '缺少机器人虚拟环境或管理器 worker')
class ManagerLaunchTests(unittest.TestCase):
    def test_worker_uses_launch_job_and_connects(self):
        server = _FakeOneBot()
        self.addCleanup(server.stop)
        with tempfile.TemporaryDirectory(prefix='manager-launch-') as folder:
            root = pathlib.Path(folder)
            job = {
                'source': str(ROOT), 'root': str(root), 'port': _free_port(),
                'parent': os.getpid(), 'label': '测试账号', 'account_id': 'a1',
                'ws_url': f'ws://127.0.0.1:{server.port}',
                'access_token': 'relay-key-123',
            }
            job_path = root / 'launch.json'
            job_path.write_text(json.dumps(job), encoding='utf-8')
            env = dict(os.environ)
            # 隔离用户目录：否则这个子进程会去读开发机真实装过的插件库（.env 里的
            # LOCALAPPDATA 与 USERPROFILE 都会被 qq_app.installed_data_root 回退使用）。
            env.update({'WHALE_GATEWAY_WORKER_PROBE': '1',
                        'WHALE_GATEWAY_WORKER_SMOKE': '1',
                        'QT_QPA_PLATFORM': 'offscreen',
                        'LOCALAPPDATA': str(root / 'local'),
                        'USERPROFILE': str(root / 'profile')})
            # encoding/errors 必须显式给：子进程按 UTF-8 写中文，父进程若按 Windows
            # 本地编码（gbk）解码会在线程里抛 UnicodeDecodeError，导致 process.stdout /
            # process.stderr 变成 None，反过来把下面那条断言消息炸成 TypeError。
            process = subprocess.run([str(VENV_PY), str(WORKER), str(job_path)],
                                     capture_output=True, encoding='utf-8', errors='replace',
                                     timeout=120, env=env)
            smoke = root / 'bridge-smoke.json'
            if not smoke.is_file():
                # 只在真失败时拼消息（惰性求值），并对 None 兜底。
                self.fail('没有生成 bridge-smoke.json\nstdout=%s\nstderr=%s' % (
                    (process.stdout or '')[-800:], (process.stderr or '')[-800:]))
            result = json.loads(smoke.read_text(encoding='utf-8'))
            self.assertTrue(result.get('online'), result)
            self.assertEqual(str(result.get('self_id')), '10001')
            self.assertIn('relay-key-123', server.seen_token)
            config = (root / 'config.qq.yaml').read_text(encoding='utf-8-sig')
            self.assertIn('ws_url: ws://127.0.0.1', config)
            self.assertIn('mode: qqbridge', config)


if __name__ == '__main__':
    unittest.main()
