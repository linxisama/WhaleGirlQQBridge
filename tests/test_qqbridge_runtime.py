"""Bridge identity, protocol recovery, plugin routing and complete replies."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import websockets
import yaml
from fastapi.testclient import TestClient

from src.qq_transports import QqBridge as RuntimeQqBridge
from src.qqbridge_transport import QqBridge
from src.qq_app import QQRuntime, Settings, create_app
from src.account_transport import onebot_transport
from src.plugin_capabilities import apply_capabilities


class BridgeServer:
    def __init__(self):
        self.account = '999'
        self.ws = None
        self.authorization = None
        self.query = None
        self.actions = []

    async def handler(self, ws):
        self.ws = ws
        self.authorization = ws.request.headers.get('Authorization')
        self.query = ws.request.path
        async for raw in ws:
            data = json.loads(raw)
            action = data['action']
            self.actions.append(data)
            if action == 'never_reply':
                continue
            value = ({'user_id': self.account} if action == 'get_login_info' else
                     {'online': True, 'good': True} if action == 'get_status' else
                     {'text': '测试语音'} if action == 'fetch_ptt_text' else {'message_id': 'out1'})
            await ws.send(json.dumps({'retcode': 0, 'status': 'ok', 'echo': data['echo'], 'data': value}))


class TransportReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = BridgeServer()
        self.server = await websockets.serve(self.fake.handler, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.bridge = QqBridge(ws_url=f'ws://127.0.0.1:{port}?access_token=a%2Bb%26c', account_id='999')
        await self.bridge.connect()
        self.assertTrue(self.bridge.online, self.bridge.last_error)

    async def asyncTearDown(self):
        await self.bridge.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_credentials_use_header_and_query_is_clean(self):
        self.assertEqual(self.fake.authorization, 'Bearer a+b&c')
        self.assertNotIn('access_token', self.fake.query)

    async def test_relay_endpoint_gets_query_token(self):
        # 统一接入网关的中继只认 ?access_token=：同一份令牌必须同时出现在头里和查询串里。
        port = self.server.sockets[0].getsockname()[1]
        relay = QqBridge(ws_url=f'ws://127.0.0.1:{port}/v1/accounts/acc1/onebot'
                                f'?access_token=a%2Bb%26c', account_id='999')
        try:
            await relay.connect()
            self.assertTrue(relay.online, relay.last_error)
            self.assertEqual(self.fake.authorization, 'Bearer a+b&c')
            self.assertIn('access_token=a%2Bb%26c', self.fake.query)
        finally:
            await relay.close()

    async def test_malformed_events_do_not_drop_connection(self):
        messages = []
        self.bridge.on_event = messages.append  # Both sync and async callbacks are supported.
        for raw in ('[]', 'null', '3', '"text"', '{', '{"meta_event_type":"heartbeat","status":"bad"}'):
            await self.fake.ws.send(raw)
        await self.fake.ws.send(json.dumps({'post_type': 'message', 'message_type': 'private',
                                           'self_id': 999, 'user_id': 123, 'message_id': 5, 'message': '你好'}))
        for _ in range(50):
            if messages:
                break
            await asyncio.sleep(.01)
        self.assertEqual(len(messages), 1)
        await self.bridge.probe()
        self.assertTrue(self.bridge.online)

    async def test_account_change_cannot_be_reenabled_by_heartbeat(self):
        self.fake.account = '888'
        with self.assertRaisesRegex(ConnectionError, '账号已变化'):
            await self.bridge.probe()
        self.bridge.receive({'meta_event_type': 'heartbeat', 'status': {'online': True, 'good': True}})
        self.assertFalse(self.bridge.online)
        with self.assertRaises(ConnectionError):
            await self.bridge.send({'kind': 'private', 'target': '123'}, '禁止误发')
        self.assertFalse(any(a['action'].startswith('send_') for a in self.fake.actions))

    async def test_disconnect_fails_pending_without_replaying(self):
        pending = asyncio.create_task(self.bridge.call('never_reply'))
        for _ in range(50):
            if any(a['action'] == 'never_reply' for a in self.fake.actions):
                break
            await asyncio.sleep(.01)
        await self.fake.ws.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(pending, 1)
        self.assertEqual(self.bridge.pending, {})
        for _ in range(200):
            if self.bridge.online:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.bridge.online)
        self.assertEqual(sum(a['action'] == 'never_reply' for a in self.fake.actions), 1)

    async def test_close_joins_background_tasks(self):
        task = self.bridge._task
        await self.bridge.close()
        self.assertTrue(task.done())
        self.assertIsNone(self.bridge.ws)
        self.assertFalse(self.bridge.online)
        self.assertEqual(self.bridge.pending, {})


class DiscoveryTests(unittest.TestCase):
    def test_discovery_uses_ws_credentials_and_selected_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'config'
            folder.mkdir()
            for account, token in [('999', 'ws-999'), ('888', 'ws-888')]:
                (folder / f'onebot_{account}.json').write_text(json.dumps({'networks': {
                    'httpServers': [{'host': '127.0.0.1', 'port': 3000, 'accessToken': 'http-token'}],
                    'wsServers': [{'host': '0.0.0.0', 'port': 3001, 'accessToken': token}]}}), encoding='utf-8')
            self.assertEqual(QqBridge(home_dir=tmp, account_id='999').endpoint(), ('ws://127.0.0.1:3001', 'ws-999'))
            with self.assertRaisesRegex(ValueError, '唯一'):
                QqBridge(home_dir=tmp).endpoint()
            self.assertEqual(QqBridge(ws_url='ws://localhost:3456', home_dir=tmp, access_token='explicit').endpoint(),
                             ('ws://localhost:3456', 'explicit'))

    def test_invalid_endpoint_is_rejected_without_starting_task(self):
        for url in ('https://localhost:3001', 'ws://user:pass@localhost:3001', 'ws://localhost:0', 'ws://localhost:99999'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                QqBridge(ws_url=url).endpoint()

    def test_discovery_does_not_silently_switch_bound_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'config'
            folder.mkdir()
            original = folder / 'onebot_999.json'
            original.write_text(json.dumps({'networks': {'wsServers': [{'port': 3001}]}}), encoding='utf-8')
            bridge = QqBridge(home_dir=tmp)
            bridge.endpoint()
            original.rename(folder / 'onebot_888.json')
            with self.assertRaisesRegex(ValueError, '账号配置已变化'):
                bridge.endpoint()


class RuntimeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = {'ai': {'api_url': 'https://example.test/v1', 'api_key': 'fake', 'model': 'test'},
                  'qq_only': {'mode': 'qqbridge', 'qqbridge': {'ws_url': 'ws://localhost:3001'}}}
        (root / 'config.qq.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
        self.bot = QQRuntime(root=root)
        self.bot.bridge.self_id = '999'
        self.bot.bridge.online = True
        self.bot.bridge.call = AsyncMock(return_value={'text': '测试语音'})
        self.bot.onebot.call = AsyncMock(side_effect=AssertionError('不得调用旧连接'))
        self.sent = []
        async def send(message, text, meta=None):
            self.sent.append((message, text, meta))
            return {'id': 'out1'}
        self.bot.bridge.send = AsyncMock(side_effect=send)
        self.bot.ai.chat = AsyncMock(return_value='收到，这是回复。')
        self.worker = None

    async def asyncTearDown(self):
        self.bot.running = False
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.bot.close()
        self.temp.cleanup()

    async def test_group_and_private_complete_reply_and_deduplication(self):
        active = {'chat_controls', 'conversation_memory', 'archive_isolation'}
        for pid in self.bot.plugins._settings:
            self.bot.plugins._settings[pid]['enabled'] = pid in active
        self.bot.running = True
        self.worker = asyncio.create_task(self.bot._work())
        for number, kind in enumerate(('group', 'private')):
            message = {'id': str(number), 'kind': kind, 'target': '123', 'sender_id': '456',
                       'sender_name': '测试用户', 'sender': '测试用户', 'text': '你好', 'at': True, 'media': []}
            self.bot.ingest(message, 'qqbridge')
            self.bot.ingest(message, 'qqbridge')
            for _ in range(200):
                if len(self.sent) > number:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(len(self.sent), number + 1)
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='sent' AND mode='qqbridge' AND account='999'").fetchone()[0], 2)

    async def test_speech_uses_bridge_and_preserves_old_onebot_route(self):
        plugin = self.bot.plugins._plugins['speech_to_text_qq']
        self.bot.plugins._settings['speech_to_text_qq']['enabled'] = True
        text = await plugin.transcribe(self.bot, {'message_id': 'voice-long-id'})
        self.assertEqual(text, '测试语音')
        self.bot.bridge.call.assert_awaited_once()
        self.bot.onebot.call.assert_not_awaited()
        old = SimpleNamespace(mode='onebot', onebot=object(), bridge=object())
        self.assertIs(onebot_transport(old), old.onebot)

    async def test_real_websocket_through_runtime_worker_and_reply(self):
        fake = BridgeServer()
        server = await websockets.serve(fake.handler, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        # The current unified-gateway runtime owns this adapter; the standalone
        # account-bound adapter above is exercised by its own protocol tests.
        self.bot.bridge = RuntimeQqBridge(ws_url=f'ws://127.0.0.1:{port}')
        for pid in self.bot.plugins._settings:
            self.bot.plugins._settings[pid]['enabled'] = pid in {'chat_controls', 'conversation_memory'}
        try:
            await self.bot.start()
            for _ in range(100):
                if self.bot.bridge.online:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.bot.bridge.online)
            for kind, number in [('group', 1), ('private', 2)]:
                event = {'post_type': 'message', 'message_type': kind, 'self_id': 999,
                         'user_id': 456, 'group_id': 123, 'message_id': number,
                         'sender': {'nickname': '测试用户'}, 'message': [
                             {'type': 'at', 'data': {'qq': '999'}},
                             {'type': 'text', 'data': {'text': '你好'}}]}
                await fake.ws.send(json.dumps(event))
                await fake.ws.send(json.dumps(event))
                for _ in range(300):
                    if sum(a['action'].startswith('send_') for a in fake.actions) >= number:
                        break
                    await asyncio.sleep(.01)
            sent = [a['action'] for a in fake.actions if a['action'].startswith('send_')]
            self.assertEqual(sent, ['send_group_msg', 'send_private_msg'])
            self.assertEqual(self.bot.ai.chat.await_count, 2)
        finally:
            await self.bot.stop()

    async def test_catalog_matches_existing_account_capabilities(self):
        bridge_items = self.bot.plugins.list_plugins()
        apply_capabilities(bridge_items, self.bot)
        self.bot.config.set('qq_only.mode', 'onebot')
        old_items = self.bot.plugins.list_plugins()
        apply_capabilities(old_items, self.bot)
        # Displayed capability fields must not misclassify the bridge as official QQ.
        self.assertEqual([item['id'] for item in bridge_items], [item['id'] for item in old_items])
        for new, old in zip(bridge_items, old_items):
            if new['id'] == 'speech_to_text_qq':
                # The bridge has a specific fetch_ptt_text prerequisite. Keep
                # that accurate note while requiring the same capability fields.
                self.assertIn('fetch_ptt_text', new['status_note'])
                self.assertEqual({k: v for k, v in new.items() if k != 'status_note'},
                                 {k: v for k, v in old.items() if k != 'status_note'}, new['id'])
            else:
                self.assertEqual(new, old, new['id'])

    async def test_capability_panel_recognizes_account_bridge(self):
        items = [{'id': 'speech_to_text_qq', 'settings': {'enabled': True}, 'schema': {}}]
        apply_capabilities(items, self.bot)
        self.assertNotEqual(items[0].get('capability'), 'unavailable')
        self.assertNotIn('当前官方', str(items[0]))

    async def test_invalid_bridge_settings_do_not_stop_running_bot(self):
        self.bot.running = True
        before = self.bot.config.config_path.read_bytes()
        with self.assertRaises(ValueError):
            await self.bot.save(Settings(mode='qqbridge', qqbridge_ws_url='http://bad',
                                         api_url='https://example.test/v1', model='test'))
        self.assertTrue(self.bot.running)
        self.assertEqual(before, self.bot.config.config_path.read_bytes())


class ProbeEndpointTests(unittest.TestCase):
    def test_native_test_qq_endpoint_selects_bridge(self):
        bridge = SimpleNamespace(ws=object(), self_id='999', probe=AsyncMock())
        bot = SimpleNamespace(mode='qqbridge', lock=asyncio.Lock(), bridge=bridge,
                              onebot=SimpleNamespace(probe=AsyncMock(side_effect=AssertionError('wrong route'))),
                              close=AsyncMock())
        # Endpoint routing does not require the application scheduler/registry
        # lifespan. Its real runtime dependencies have separate integration tests.
        app = create_app(bot)
        app.state.bot = bot
        client = TestClient(app, raise_server_exceptions=True)
        self.addCleanup(client.close)
        result = client.post('/api/test-qq').json()
        self.assertTrue(result['success'], result)
        self.assertIn('999', result['message'])
        bridge.probe.assert_awaited_once()
        bot.onebot.probe.assert_not_awaited()
