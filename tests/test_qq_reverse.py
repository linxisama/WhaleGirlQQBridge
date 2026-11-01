"""反向 WebSocket（OneBot v11）测试：我们监听，假引擎连进来。"""
import asyncio
import json
import socket
import unittest

from src.qq_reverse import ReverseOneBot


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class _FakeEngine:
    """假协议端：连上后响应 get_login_info/get_status，并可主动推事件。"""

    def __init__(self, url, token='tok'):
        self.url = url
        self.token = token
        self.actions = []
        self.ws = None
        self.task = None
        self.events = []

    async def start(self):
        import websockets
        self.ws = await websockets.connect(self.url + '?access_token=' + self.token)
        self.task = asyncio.create_task(self._reader())

    async def _reader(self):
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                action = data.get('action')
                if not action:
                    continue
                self.actions.append((action, data.get('params') or {}))
                if action == 'get_login_info':
                    payload = {'user_id': 10001, 'nickname': '测试号'}
                elif action == 'get_status':
                    payload = {'online': True, 'good': True}
                else:
                    payload = {'message_id': 1}
                await self.ws.send(json.dumps({'status': 'ok', 'retcode': 0,
                                               'data': payload, 'echo': data.get('echo')}))
        except Exception:
            pass

    async def push(self, event):
        await self.ws.send(json.dumps(event))

    async def stop(self):
        if self.task:
            self.task.cancel()
        if self.ws:
            await self.ws.close()


class ReverseOneBotTests(unittest.TestCase):
    def test_handshake_event_and_send(self):
        port = _free_port()

        async def scenario():
            bridge = ReverseOneBot(port=port, token='tok')
            await bridge.connect()
            self.assertTrue(bridge.status()['enabled'], bridge.last_error)
            received = []
            bridge.on_event = lambda message: _collect(received, message)
            engine = _FakeEngine(f'ws://127.0.0.1:{port}/onebot/v11/ws')
            await engine.start()
            for _ in range(50):
                await asyncio.sleep(0.05)
                if bridge.online:
                    break
            self.assertTrue(bridge.online, bridge.last_error)
            self.assertEqual(bridge.self_id, '10001')
            await engine.push({'post_type': 'message', 'message_type': 'group',
                               'group_id': 999, 'user_id': 2, 'self_id': 10001, 'message_id': 12345,
                               'raw_message': '你好', 'message': [{'type': 'text',
                                                                  'data': {'text': '你好'}}],
                               'sender': {'user_id': 2, 'nickname': '甲'}})
            for _ in range(40):
                await asyncio.sleep(0.05)
                if received:
                    break
            self.assertTrue(received, '没有把引擎推的事件转出来')
            await bridge.send_group_msg(999, '测试回复')
            self.assertIn('send_group_msg', [action for action, _ in engine.actions])
            sent = [params for action, params in engine.actions if action == 'send_group_msg'][0]
            self.assertEqual(sent['group_id'], 999)
            await engine.stop()
            await bridge.close()

        asyncio.run(scenario())

    def test_wrong_token_is_rejected(self):
        port = _free_port()

        async def scenario():
            bridge = ReverseOneBot(port=port, token='right')
            await bridge.connect()
            import websockets
            with self.assertRaises(Exception):
                ws = await websockets.connect(f'ws://127.0.0.1:{port}/onebot/v11/ws'
                                              '?access_token=wrong', open_timeout=3)
                await ws.recv()
            await bridge.close()

        asyncio.run(scenario())

    def test_status_before_any_connection(self):
        bridge = ReverseOneBot(port=0, token='')
        status = bridge.status()
        self.assertFalse(status['enabled'])
        self.assertEqual(status['port'], 6700, '默认端口是 6700')


async def _collect(bucket, message):
    bucket.append(message)


if __name__ == '__main__':
    unittest.main()
