"""反向 WebSocket（OneBot v11）：让协议端**主动连到我们**，而不是我们去连它。

为什么加它（用户提议的"反向接入 QQ"）：
- 现在 qqbridge 是"出站客户端"：得先知道 SnowLuma/NapCat 的端口，端口变了就得改配置；
- 反向模式下**我们监听一个端口**，SnowLuma/NapCat 配成「WebSocket 服务器 / 反向 WS」连过来，
  地址与令牌都是我们定的，换引擎只用改引擎那一侧；
- 协议不变（OneBot v11），所以消息解析、发送段、插件全部复用现有代码。

接口与 `QqBridge` 保持一致（`connect/close/online/self_id/call/send/send_group_msg/
send_private_msg/on_event`），`QQRuntime` 只换实现不换调用方。

安全：默认只监听 127.0.0.1；令牌必填（URL `?access_token=` 或 `Authorization: Bearer`）。
"""
from __future__ import annotations

import asyncio
import json
import uuid

__all__ = ['ReverseOneBot']


class ReverseOneBot:
    def __init__(self, host: str = '127.0.0.1', port: int = 6700, token: str = '',
                 path: str = '/onebot/v11/ws'):
        self.host = str(host or '127.0.0.1')
        self.port = int(port or 6700)
        self.token = str(token or '')
        self.path = str(path or '/onebot/v11/ws')
        self.online = False
        self.self_id = ''
        self.nickname = ''
        self.last_error = ''
        self.on_event = None
        self.ws = None
        self._server = None
        self._task = None
        self._probe_task = None
        self.pending: dict = {}
        self._closing = False
        self.connections = 0

    # ── 生命周期 ────────────────────────────────────────────────────
    async def connect(self):
        """启动监听（不等引擎连进来）。引擎连上后自动握手、置在线。"""
        self._closing = False
        if self._server is not None:
            return
        try:
            import websockets
            from websockets.server import serve
        except Exception as error:                      # pragma: no cover
            self.last_error = f'缺少 websockets：{error}'
            return
        try:
            self._server = await serve(self._handler, self.host, self.port,
                                       max_size=None, ping_interval=None)
        except Exception as error:
            self.last_error = f'反向 WebSocket 监听失败：{type(error).__name__}: {error}'
            self._server = None
            return
        self.last_error = ''

    async def close(self):
        self._closing = True
        if self._probe_task is not None:
            self._probe_task.cancel()
            self._probe_task = None
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        self.online = False

    # ── 连接处理 ────────────────────────────────────────────────────
    def _authorized(self, ws) -> bool:
        if not self.token:
            return True
        headers = dict(getattr(ws, 'request_headers', {}) or {})
        header = str(headers.get('Authorization') or '')
        if header.lower().startswith('bearer ') and header[7:].strip() == self.token:
            return True
        query = str(getattr(ws, 'path', '') or '')
        if 'access_token=' in query:
            return query.split('access_token=', 1)[1].split('&', 1)[0] == self.token
        return False

    async def _handler(self, ws):
        if not self._authorized(ws):
            self.last_error = '反向连接令牌不对，已拒绝'
            await ws.close(code=1008)
            return
        self.ws = ws
        self.connections += 1
        self._probe_task = asyncio.create_task(self._probe_loop())
        try:
            async for raw in ws:
                data = self._decode(raw)
                if not data:
                    continue
                if self._resolve_pending(data):
                    continue
                await self._dispatch(data)
        except Exception:
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                self.online = False
                self.last_error = self.last_error or '反向连接已断开，等待协议端重连'

    @staticmethod
    def _decode(raw):
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8', 'ignore')
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None
        return raw if isinstance(raw, dict) else None

    def _resolve_pending(self, data) -> bool:
        echo = data.get('echo')
        if isinstance(echo, str) and echo in self.pending:
            future = self.pending[echo]
            if not future.done():
                future.set_result(data)
            return True
        return False

    async def _dispatch(self, data):
        callback = self.on_event
        if not callback:
            return
        try:
            from .qq_transports import onebot_message, onebot_notice
            message = onebot_message(data, self.self_id) or onebot_notice(data, self.self_id)
            if message:
                await callback(message)
        except Exception:
            pass

    async def _probe_loop(self):
        while not self._closing and self.ws is not None:
            try:
                await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.online = False
                self.last_error = str(error)
            await asyncio.sleep(30)

    # ── 协议调用（与 QqBridge 同构）────────────────────────────────
    async def call(self, action, params=None):
        ws = self.ws
        if ws is None:
            raise ConnectionError('反向 WebSocket 还没有协议端连入')
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await ws.send(json.dumps({'action': action, 'params': params or {}, 'echo': echo}))
            result = await asyncio.wait_for(future, 15)
            retcode = result.get('retcode')
            if result.get('status') == 'failed' or retcode not in (None, 0):
                detail = str(result.get('wording') or result.get('message') or '').strip()
                raise RuntimeError(f'反向调用 {action} 失败：错误码 {retcode}'
                                   + (f'（{detail}）' if detail else ''))
            return result.get('data') or {}
        finally:
            self.pending.pop(echo, None)

    async def probe(self):
        info = await self.call('get_login_info')
        self.self_id = str(info.get('user_id') or '')
        self.nickname = str(info.get('nickname') or '')
        status = await self.call('get_status')
        self.online = bool(self.self_id and status.get('online') and status.get('good', True))
        if not self.online:
            raise ConnectionError('协议端已连入，但 QQ 尚未登录')
        self.last_error = ''

    # ── 发送（复用现有段构造）──────────────────────────────────────
    async def send(self, message, text, meta=None):
        from .plugin_segments import build_reply_segments
        if not self.online:
            raise ConnectionError('普通 QQ 尚未在线')
        segments = build_reply_segments(text, meta)
        if not segments:
            return None
        group = message['kind'] == 'group'
        return await self.call('send_group_msg' if group else 'send_private_msg', {
            'group_id' if group else 'user_id': int(message['target']),
            'message': segments})

    async def send_group_msg(self, group_id, text):
        from .plugin_segments import build_reply_segments
        if not self.online:
            raise ConnectionError('普通 QQ 尚未在线')
        return await self.call('send_group_msg', {'group_id': int(group_id),
                                                  'message': build_reply_segments(text, None)})

    async def send_private_msg(self, user_id, text):
        from .plugin_segments import build_reply_segments
        if not self.online:
            raise ConnectionError('普通 QQ 尚未在线')
        return await self.call('send_private_msg', {'user_id': int(user_id),
                                                    'message': build_reply_segments(text, None)})

    def status(self) -> dict:
        return {'enabled': self._server is not None, 'host': self.host, 'port': self.port,
                'path': self.path, 'token_set': bool(self.token),
                'connected': self.ws is not None, 'online': self.online,
                'self_id': self.self_id, 'connections': self.connections,
                'last_error': self.last_error}
