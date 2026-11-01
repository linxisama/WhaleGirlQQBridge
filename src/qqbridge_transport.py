"""QQ account connection over an independently installed OneBot endpoint."""
import asyncio
import contextlib
import inspect
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


class QqBridge:
    def __init__(self, ws_url='', http_url='', access_token='', snowluma_exe='',
                 home_dir='', account_id=''):
        self.ws_url = str(ws_url or '').strip()
        self.http_url = str(http_url or '').strip()
        self.access_token = str(access_token or '')
        try:
            parsed = urlsplit(self.ws_url)
            query = parse_qsl(parsed.query, keep_blank_values=True)
            self.access_token = self.access_token or next((v for k, v in query if k == 'access_token'), '')
            self.ws_url = urlunsplit(parsed._replace(query=urlencode([(k, v) for k, v in query if k != 'access_token'])))
        except ValueError:
            pass  # Validation supplies the user-facing error at connect/save time.
        # Old settings remain readable. The bridge never owns the QQ client.
        self.snowluma_exe = str(snowluma_exe or '')
        self.home_dir = str(home_dir or '').strip()
        self.account_id = str(account_id or '').strip()
        self.ws = None
        self.self_id = ''
        self.online = False
        self.last_error = ''
        self.last_seen = 0
        self.pending = {}
        self.on_event = None
        self.on_error = None
        self._task = None
        self._closing = False
        self._bound_account = self.account_id
        self._identity_verified = False
        self._ready = asyncio.Event()
        self._probe_lock = asyncio.Lock()
        self._errors = {}

    def _note(self, text):
        text = str(text).replace(self.access_token, '[已隐藏]') if self.access_token else str(text)
        self.last_error = text
        now = time.monotonic()
        if now - self._errors.get(text, -60) >= 30:
            self._errors[text] = now
            if callable(self.on_error):
                self.on_error(text)

    @staticmethod
    def validate_url(url, schemes):
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError('连接端口无效') from None
        if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('请填写有效的 OneBot 连接地址；凭据请填写在令牌栏')
        if port == 0:
            raise ValueError('连接端口不能为 0')
        return parsed

    @staticmethod
    def is_relay(url):
        """统一接入网关的中继端点（/v1/accounts/<账号>/onebot）只认查询串令牌。"""
        return '/v1/accounts/' in str(url or '')

    @staticmethod
    def authorized_url(url, token):
        """把令牌同时放进查询串（网关中继用），调用方另外再带 Authorization 头。"""
        if not token:
            return url
        parsed = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != 'access_token']
        query.append(('access_token', token))
        return urlunsplit(parsed._replace(query=urlencode(query)))

    def endpoint(self):
        """Explicit WS settings win; discovery is restricted to the selected account."""
        url, token = self.ws_url, self.access_token
        home = self.home_dir or (str(Path(self.snowluma_exe).parent) if self.snowluma_exe else '')
        if not url and home:
            folder = Path(home) / 'config'
            candidates = []
            for path in sorted(folder.glob('onebot_*.json')):
                account = path.stem.removeprefix('onebot_')
                if not account.isdigit() or (self.account_id and account != self.account_id):
                    continue
                try:
                    data = json.loads(path.read_text(encoding='utf-8-sig'))
                    servers = data.get('networks', {}).get('wsServers', [])
                    for server in servers:
                        if not isinstance(server, dict) or server.get('enable') is False:
                            continue
                        host = str(server.get('host') or '127.0.0.1')
                        if host in ('0.0.0.0', '::', '[::]'):
                            host = '127.0.0.1'
                        if ':' in host and not host.startswith('['):
                            host = '[' + host + ']'
                        port = int(server.get('port') or 3001)
                        candidates.append((f'ws://{host}:{port}', str(server.get('accessToken') or ''), account))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            if len(candidates) != 1:
                raise ValueError('未找到唯一的 OneBot WebSocket 配置，请填写连接地址和目标 QQ 号')
            url, discovered, account = candidates[0]
            token = token or discovered
            if self._bound_account and account != self._bound_account:
                raise ValueError('QQ 账号配置已变化，请确认目标 QQ 号并保存设置后重连')
            self._bound_account = self.account_id or account
        if not url:
            raise ValueError('请填写 OneBot WebSocket 地址，或选择 SnowLuma 目录')
        parsed = self.validate_url(url, {'ws', 'wss'})
        # Accept old query credentials, but move them into the Authorization header.
        query = parse_qsl(parsed.query, keep_blank_values=True)
        token = token or next((v for k, v in query if k == 'access_token'), '')
        parsed = parsed._replace(query=urlencode([(k, v) for k, v in query if k != 'access_token']))
        return urlunsplit(parsed), token

    async def connect(self):
        if self._task is not None and not self._task.done():
            return
        self.endpoint()  # Fail configuration validation before reporting startup.
        self._closing = False
        self._ready.clear()
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), 1)
        except asyncio.TimeoutError:
            pass  # Login can require a human scan.

    async def _run(self):
        import websockets
        delay = 1
        while not self._closing:
            try:
                url, token = self.endpoint()
                headers = {'Authorization': 'Bearer ' + token} if token else {}
                # 普通协议端认 Authorization 头；统一接入网关的中继只认 ?access_token=。
                # 只给中继端点补查询串，别把令牌撒进普通协议端的地址和日志里。
                if self.is_relay(url):
                    url = self.authorized_url(url, token)
                async with websockets.connect(url, additional_headers=headers,
                                              open_timeout=10, close_timeout=3) as ws:
                    delay = 1
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note('QQ 账号连接未就绪（' + type(exc).__name__ + '），请检查登录状态、地址和令牌')
            finally:
                self.online = False
                self.ws = None
                self._fail_pending('QQ 连接已断开，发送结果需核实；不会自动重发')
                self._ready.set()
            if not self._closing:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _session(self, ws):
        self.ws = ws
        self.self_id = ''
        self.online = False
        self._identity_verified = False
        probe_task = asyncio.create_task(self._probe_loop())
        try:
            async for raw in ws:
                self.last_seen = time.time()
                data = self._decode(raw)
                if data is None:
                    self._note('忽略格式无效的 QQ 事件；连接继续')
                    continue
                if not self.receive(data):
                    await self._dispatch(data)
        finally:
            self.online = False
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)
            if self.ws is ws:
                self.ws = None
            self._fail_pending('QQ 连接已断开，发送结果需核实；不会自动重发')

    async def _probe_loop(self):
        while not self._closing:
            try:
                await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.online = False
                self._note(str(exc))
            finally:
                self._ready.set()
            await asyncio.sleep(5 if not self.online else 30)

    @staticmethod
    def _decode(raw):
        try:
            value = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError, UnicodeError):
            return None

    async def _dispatch(self, data):
        if not callable(self.on_event) or not self.online:
            return
        from .qq_transports import onebot_message, onebot_notice
        event_account = str(data.get('self_id') or '')
        if event_account and event_account != self.self_id:
            self.online = False
            self._identity_verified = False
            self._note('忽略来自其他 QQ 账号的事件')
            return
        try:
            message = onebot_message(data, self.self_id) or onebot_notice(data, self.self_id)
            if message:
                result = self.on_event(message)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note('QQ 事件处理失败（' + type(exc).__name__ + '）；接收继续')

    def receive(self, data):
        echo = data.get('echo')
        if isinstance(echo, str) and echo in self.pending:
            future = self.pending[echo]
            if not future.done():
                future.set_result(data)
            return True
        if data.get('meta_event_type') == 'heartbeat':
            status = data.get('status')
            if isinstance(status, dict):
                # A heartbeat alone never establishes an unverified account identity.
                self.online = bool(self._identity_verified and self.self_id and self.self_id == self._bound_account
                                   and status.get('online', False) and status.get('good', True))
            return True
        return False

    async def call(self, action, params=None):
        ws = self.ws
        if ws is None:
            raise ConnectionError('QQ 直连尚未连接')
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await ws.send(json.dumps({'action': action, 'params': params or {}, 'echo': echo}))
            result = await asyncio.wait_for(future, 15)
            if result.get('status') == 'failed' or result.get('retcode') not in (None, 0):
                raise RuntimeError(f'QQ 直连调用 {action} 失败，错误码 {result.get("retcode")}')
            return result.get('data') or {}
        finally:
            self.pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Retrieve disconnect exceptions even if send() failed.

    async def probe(self):
        async with self._probe_lock:
            try:
                info = await self.call('get_login_info')
                status = await self.call('get_status')
                if not isinstance(info, dict) or not isinstance(status, dict):
                    raise ConnectionError('QQ 登录信息或状态格式无效')
                account = str(info.get('user_id') or '')
                if not account.isdigit() or not status.get('online') or not status.get('good', True):
                    raise ConnectionError('请在 SnowLuma 完成 QQ 扫码登录')
                if self._bound_account and account != self._bound_account:
                    raise ConnectionError('QQ 账号已变化；已暂停收发，请确认目标 QQ 号并保存设置后重连')
                self._bound_account = account
                self.self_id = account
                self._identity_verified = True
                self.online = True
                self.last_error = ''
            except BaseException:
                self.online = False
                self._identity_verified = False
                raise

    async def send(self, message, text, meta=None):
        if not self.online:
            raise ConnectionError('QQ 账号尚未在线')
        from .plugin_segments import build_reply_segments
        group = message.get('kind') == 'group'
        segments = build_reply_segments(text, meta)
        if not segments:
            return
        return await self.call('send_group_msg' if group else 'send_private_msg', {
            'group_id' if group else 'user_id': int(message['target']), 'message': segments})

    async def send_active(self, kind, target, text, meta=None):
        return await self.send({'kind': kind, 'target': target}, text, meta)

    async def send_group_msg(self, group_id, text):
        return await self.send_active('group', group_id, text)

    async def send_private_msg(self, user_id, text):
        return await self.send_active('private', user_id, text)

    async def send_voice(self, kind, target, audio):
        """Explicit OneBot record output, separate from text/plugin media handling."""
        if not self.online:
            raise ConnectionError('QQ 账号尚未在线')
        if kind not in {'group', 'private'} or not str(target).isdigit():
            raise ValueError('语音发送目标无效')
        from .voice_service import validate_wav
        import base64
        validate_wav(audio)
        record = {'type': 'record', 'data': {'file': 'base64://' + base64.b64encode(audio).decode('ascii')}}
        return await self.call('send_group_msg' if kind == 'group' else 'send_private_msg', {
            'group_id' if kind == 'group' else 'user_id': int(target), 'message': [record]})

    def _fail_pending(self, reason):
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError(reason))

    async def close(self):
        self._closing = True
        self.online = False
        self._identity_verified = False
        self._fail_pending('QQ 直连已关闭，发送结果需核实')
        ws, self.ws = self.ws, None
        if ws:
            with contextlib.suppress(Exception):
                await ws.close()
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
