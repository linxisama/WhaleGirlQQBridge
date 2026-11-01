"""QQ-only transports: official signed callbacks and OneBot 11 universal WS."""
import asyncio
import contextlib
import json
import re
import time
import uuid

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .plugin_segments import build_reply_segments


def signing_key(secret):
    seed = secret.encode()
    if not seed:
        raise ValueError("请先填写 AppSecret")
    return Ed25519PrivateKey.from_private_bytes((seed * (32 // len(seed) + 1))[:32])


# 官方接口返回的错误码 → 人话。以前只 raise_for_status()，用户看到的是
# "HTTP 500"／"HTTP 400"，而 QQ 其实在 body 里写清了原因（额度、风控、过期…）。
QQ_ERROR_HINTS = {
    11244: "主动消息额度不足：QQ 对主动推送有配额（每个用户/群每天有限），改被动回复或明天再试",
    11245: "暂时无法发送（可能被风控或对方关闭了机器人消息）",
    11253: "消息发送过于频繁，稍后再试",
    11254: "请求过于频繁，已被限流，稍后再试",
    11255: "机器人已被禁言或不在该群",
    11256: "机器人没有该群的发言权限",
    11265: "请求参数不合法（检查 content / msg_type / media）",
    11278: "被动回复已过期：QQ 要求在本条消息后 5 分钟内回复，且最多 5 条",
    11279: "被动回复次数已用完（每条来信最多 5 次回复）",
    11281: "主动消息超出配额",
    # 11292：真机上出现过（2026-10-16 用户截图）—— 群消息生成了回复却发不出去，
    # 原因是**当前走的是沙箱环境**（sandbox.api.sgroup.qq.com），沙箱接口只能发到
    # 开放平台里配置的沙箱群/测试用户。以前这条没有中文提示，用户只看到 QQ 的原文。
    11292: ("当前用的是沙箱环境：沙箱接口只能发到 QQ 开放平台里配置的沙箱群/测试用户，"
            "真实群会被拒绝。请在「设置 → QQ 接入」里取消勾选「使用沙箱环境」，"
            "或把该群加进开放平台的沙箱白名单"),
    11298: "审核不通过：内容可能触发风控，换一种说法再试",
    30400: "请求参数错误（appid / 路径 / 消息体）",
    30401: "不支持的消息类型",
    30403: "主动消息超出发送时间窗",
    30404: "回复序号 msg_seq 重复或非法",
    40034: "机器人未获得该接口权限，请到 QQ 开放平台检查权限/沙箱设置",
    50054: "请求被拒绝（权限或沙箱环境不符）",
}

# 沙箱环境专属的拒绝码：出现时自动改用正式环境重发一次（见 OfficialQQ.send）
SANDBOX_REJECT_CODES = {11292}


def describe_qq_error(status, body) -> str:
    """把 QQ 返回的 HTTP 状态 + JSON body 翻成一句可操作的中文。"""
    code = None
    message = ""
    if isinstance(body, dict):
        code = body.get("code") or body.get("errcode") or (body.get("err_code"))
        message = str(body.get("message") or body.get("err_msg") or body.get("msg") or "")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    hint = QQ_ERROR_HINTS.get(code) if code is not None else None
    if hint:
        return f"QQ 拒绝了这次发送（{code}）：{hint}"
    if code is not None:
        return f"QQ 拒绝了这次发送（{code}）：{message or '未提供原因'}"
    if status == 401:
        return "QQ 鉴权失败（401）：AppSecret 或 AppID 不对，令牌可能已失效"
    if status == 403:
        return f"QQ 拒绝访问（403）：{message or '机器人没有该权限，或该消息不在可回复的时间窗内'}"
    if status == 404:
        return f"QQ 接口不存在（404）：{message or '检查 AppID 与沙箱设置'}"
    if status == 429:
        return "QQ 限流（429）：发送太快，稍后再试"
    if status and status >= 500:
        return f"QQ 服务端错误（{status}）：{message or '稍后再试'}"
    return f"QQ 返回了 {status}：{message or '未提供原因'}"


class QQSendError(RuntimeError, ValueError):
    """官方发送失败，带可读原因与原始错误码（供控制台事件日志与失败分类使用）。

    同时继承 ``ValueError``：历史上"图片不可用/参数不合法"这类是 ValueError，
    插件与旧测试里有 ``except ValueError`` 的写法，多继承可以两边都不破。
    """

    def __init__(self, message, status=None, code=None, body=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


async def _gate_acquire(transport):
    """发送前过闸：transport 挂有 send_gate 时等最小间隔/冷却结束。"""
    gate = getattr(transport, "send_gate", None)
    if gate is not None:
        await gate.acquire()


async def _gate_report(transport, status_or_code):
    """把 429/11253/11254 报给闸门触发冷却；不改变原有错误文案。"""
    gate = getattr(transport, "send_gate", None)
    if gate is not None:
        await gate.report_code(status_or_code)


class OfficialQQ:
    def __init__(self, app_id, secret, sandbox=False, send_gate=None):
        self.app_id, self.secret = app_id, secret
        self.sandbox = bool(sandbox)
        self.send_gate = send_gate
        self.base = "https://sandbox.api.sgroup.qq.com" if sandbox else "https://api.sgroup.qq.com"
        self.http = httpx.AsyncClient(timeout=20)
        self.token = ""
        self.expires = 0
        self.lock = asyncio.Lock()
        self.authenticated = False
        self.last_callback = 0
        self.last_error = ""
        # 官方 WebSocket 接收用
        self.ws_connected = False
        self.seq = None
        # intents 可降级：见 downgrade_intents 与 run_websocket 的说明
        self.intents = self.INTENTS
        self._identified = False
        self.intents_note = ""
        # 一次性提示（例如"沙箱拒绝后自动改用正式环境发送"），由 qq_app 的看门狗取走写进运行记录
        self._notes: list[str] = []

    def drain_notes(self) -> list[str]:
        """取走并清空待提示的消息（线程内使用，不含锁也够：只有发送链会写）。"""
        notes, self._notes = list(self._notes), []
        return notes

    def downgrade_intents(self, reason: str = "") -> int:
        """退到最小可用 intents（群@ + 单聊），并记下原因。返回新的 intents。"""
        self.intents = self.MIN_INTENTS
        self.intents_note = (f"{reason}；已改用最小 intents（群@ + 单聊）"
                             if reason else "已改用最小 intents（群@ + 单聊）")
        return self.intents

    async def authorize(self, force=False):
        async with self.lock:
            if not force and self.token and time.time() < self.expires:
                return self.token
            if not self.app_id or not self.secret:
                raise ValueError("请填写 QQ AppID 和 AppSecret")
            response = await self.http.post("https://bots.qq.com/app/getAppAccessToken", json={
                "appId": self.app_id, "clientSecret": self.secret})
            response.raise_for_status()
            data = response.json()
            if not data.get("access_token"):
                raise ValueError("QQ 未返回访问令牌，请核对 AppID / AppSecret")
            self.token = data["access_token"]
            ttl = float(data.get("expires_in", 7200))
            self.expires = time.time() + max(1, ttl - min(300, ttl / 5))
            self.authenticated = True
            self.last_error = ""
            return self.token

    async def _api(self, method, path, payload=None, *, attempts=2, timeout=20):
        """带鉴权与一次 401 重试的官方接口调用；失败抛 QQSendError（含可读原因）。

        以前这里是 ``response.raise_for_status()``：QQ 在 body 里写明的
        "主动消息额度不足/被动回复已过期/频率限制" 全被丢掉，用户只看到 HTTP 500。
        """
        last = None
        for attempt in range(max(1, attempts)):
            token = await self.authorize()
            # 用 post/get（而不是 request(method,…)）：测试替身与旧代码都只实现了这两个名字
            call = self.http.post if str(method).upper() == 'POST' else self.http.get
            kwargs = {"headers": {"Authorization": f"QQBot {token}", "X-Union-Appid": self.app_id},
                      "timeout": timeout}
            if str(method).upper() == 'POST':
                kwargs["json"] = payload
            response = await call(self.base + path, **kwargs)
            if response.status_code == 401 and attempt < attempts - 1:
                await self.authorize(force=True)
                last = response
                continue
            if response.status_code >= 400:
                try:
                    body = response.json()
                except Exception:
                    body = {"message": (response.text or "")[:200]}
                text = describe_qq_error(response.status_code, body)
                code = body.get("code") if isinstance(body, dict) else None
                await _gate_report(self, code if code is not None else response.status_code)
                raise QQSendError(text, status=response.status_code, code=code, body=body)
            try:
                return response.json()
            except Exception:
                return {}
        detail = describe_qq_error(getattr(last, "status_code", 401), None)
        raise QQSendError("QQ 鉴权失败：" + detail, status=getattr(last, "status_code", 401))

    async def _upload_media(self, kind, target, payload):
        """上传富媒体，返回 file_info（图片/语音/视频/文件共用）。"""
        where = 'groups' if kind == 'group' else 'users'
        data = await self._api('POST', f'/v2/{where}/{target}/files', payload, timeout=60)
        file_info = (data or {}).get('file_info')
        if not file_info:
            raise QQSendError('QQ 媒体上传未返回 file_info：检查文件格式、大小与链接是否可公开访问')
        return file_info

    def _build_media_payload(self, kind, target, items):
        """把插件的媒体段翻成官方上传参数（图片 file_type=1）。"""
        item = items[0]
        if item.get('type')=='video':
            from .bilibili_share import approved_video
            data=item.get('data') or {}
            if not approved_video(data.get('url')):raise QQSendError('视频地址无效')
            return {'file_type':2,'srv_send_msg':False,'url':data['url']}
        from .image_input import image_source
        data = item.get('data') or {}
        source = image_source(data.get('url') or data.get('file'))
        if not source:
            raise QQSendError('图库图片不可用，请检查图片文件或公开链接')
        payload = {'file_type': 1, 'srv_send_msg': False}
        if source.startswith('data:'):
            # QQ 的富媒体接口接受 base64（不带 data: 头），不要本地路径
            payload['file_data'] = source.split(',', 1)[1]
        else:
            payload['url'] = source
        return payload

    async def _send_with_sandbox_fallback(self, action):
        """先按当前环境发送；**沙箱拒绝(11292)** 时自动改用正式环境重发一次。

        为什么需要：沙箱接口（sandbox.api.sgroup.qq.com）只能发到开放平台里配置的
        沙箱群/测试用户。真机上出现过的现象就是"回复生成了、列表里却是待核实"——
        QQ 回 11292，用户完全不知道是沙箱限制。
        令牌由 bots.qq.com 统一签发，正式环境与沙箱通用，所以可以带着同一份令牌重发。
        重发成功会置 ``sandbox_fallback_at``，由上层写进运行记录（用户需要知道走了降级）。
        """
        try:
            return await action(self)
        except QQSendError as exc:
            code = getattr(exc, 'code', None)
            if code not in SANDBOX_REJECT_CODES or not self.sandbox:
                raise
            self.last_error = str(exc)
            production = OfficialQQ(self.app_id, self.secret, sandbox=False, send_gate=self.send_gate)
            production.token, production.expires = self.token, self.expires
            try:
                result = await action(production)
            except QQSendError as retry_exc:
                # 正式环境也不行：把两边的原因都给出来，别让用户以为只是"抖动"
                raise QQSendError(
                    f"沙箱环境拒绝了发送（{code}），正式环境重试也失败：{retry_exc}",
                    status=getattr(retry_exc, 'status', None),
                    code=getattr(retry_exc, 'code', None)) from retry_exc
            finally:
                try:
                    await production.close()
                except Exception:
                    pass
            self.sandbox_fallback_at = time.time()
            self.last_error = ""
            self._notes.append(
                "这次发送原本被沙箱环境拒绝（11292），已自动改用正式环境发出去；"
                "建议在「设置 → QQ 接入」里取消勾选「使用沙箱环境」，避免每次都走这条兜底")
            return result

    async def send(self, message, text, meta=None):
        """发送**被动**回复（引用本条来信）。

        - 文本：``msg_type=0``；
        - 图片：先上传拿 ``file_info``，再以 ``msg_type=7`` 发送（单次一张）；
        - 文字与图片分开发（``separate_images``）：用递增的 ``msg_seq``，
          QQ 一条来信最多接受 5 条被动回复，这里会检查并给出可读错误；
        - ``msg_seq`` 由发送链通过 ``message['_passive_seq']`` 指定；
        - 沙箱环境被拒（11292）时自动改用正式环境重发一次（见 ``_send_with_sandbox_fallback``）。
        """
        await _gate_acquire(self)
        kind, target = message["kind"], message["target"]
        if kind not in {"group", "private"}:
            raise ValueError("暂不支持该会话类型")
        path = f"/v2/{'groups' if kind == 'group' else 'users'}/{target}/messages"
        sequence = int(message.get("_passive_seq") or 1)
        if not 1 <= sequence <= 5:
            raise QQSendError(f"回复序号 {sequence} 超出范围：QQ 一条来信最多 5 条被动回复")
        payload = {"content": text or " ", "msg_type": 0,
                   "msg_id": message["id"], "msg_seq": sequence}
        # 引用回复：官方要求填 message_reference，值是被引用消息的 **REFIDX**
        # （来自入站事件的 message_scene.ext 里 msg_idx，不是消息 id）。
        # 只有内部明确请求原生引用时才附带；入站引用内容仍作为模型上下文保留。
        # 官方会为普通来信也附带索引；存在索引不等于用户要求每条回复都带引用框。
        reference = message.get("_quote_ref") if message.get('_quote_reply') else ''
        if message.get("_no_quote"):
            reference = ""
        if isinstance(reference, str) and reference.strip():
            payload["message_reference"] = {"message_id": reference.strip()}
        images = [item for item in (meta or {}).get('media', [])
                  if isinstance(item, dict) and item.get('type') == 'image']
        video=(meta or {}).get('video')
        if isinstance(video,dict):
            from .bilibili_share import approved_video
            if approved_video(video.get('url')):images=[{'type':'video','data':video}]
        if images:
            if len(images) > 1:
                raise QQSendError('官方QQ单次回复最多附带一张图库图片')
            if not str(text or '').strip():
                payload['content'] = ' '          # 官方富媒体空正文需要一个空格
            payload['msg_type'] = 7

        menu=(meta or {}).get('menu')
        if isinstance(menu,dict) and not images:
            from .menu_catalog import official_menu_payload
            payload.update(official_menu_payload(menu,text))

        async def action(transport: "OfficialQQ"):
            if images:
                try:
                    file_info = await transport._upload_media(
                        kind, target, transport._build_media_payload(kind, target, images))
                except Exception:
                    if not isinstance(video,dict):raise
                    # 上传阶段 srv_send_msg=false，不会发给群友；失败可安全发送正文中的原链接。
                    payload.pop('media',None);payload['msg_type']=0
                    payload['content']=(text or '')+'\n视频上传未完成，请打开上面的原链接。'
                    return await transport._api('POST',path,payload)
                payload['media'] = {'file_info': file_info}
            try:return await transport._api('POST', path, payload)
            except QQSendError as exc:
                if isinstance(menu,dict) and exc.code==40034090:
                    plain={k:v for k,v in payload.items() if k not in ('markdown','keyboard')}
                    plain.update(msg_type=0,content=text or '请发送 /菜单 查看功能')
                    return await transport._api('POST',path,plain)
                raise

        await self._send_with_sandbox_fallback(action)
        return {'id': str(sequence)}

    async def send_active(self, kind, target, text, meta=None):
        """发送**主动**消息（不带 msg_id/msg_seq）。

        官方 QQ 允许机器人在没有来信的情况下主动说话，但受配额限制
        （超出时返回 11244/11281，这里会翻成中文提示）。插件里的
        "主动开话题/定时播报/主动私信"在官方模式下走这条路径。
        """
        await _gate_acquire(self)
        if kind not in {"group", "private"}:
            raise ValueError("暂不支持该会话类型")
        path = f"/v2/{'groups' if kind == 'group' else 'users'}/{target}/messages"
        payload = {"content": text or " ", "msg_type": 0}
        images = [item for item in (meta or {}).get('media', [])
                  if isinstance(item, dict) and item.get('type') == 'image']
        if images:
            if len(images) > 1:
                raise QQSendError('官方QQ单次消息最多附带一张图库图片')
            if not str(text or '').strip():
                payload['content'] = ' '
            payload['msg_type'] = 7

        async def action(transport: "OfficialQQ"):
            if images:
                payload['media'] = {'file_info': await transport._upload_media(
                    kind, target, transport._build_media_payload(kind, target, images))}
            return await transport._api('POST', path, payload)

        # 主动消息也走同一套"沙箱被拒 → 正式环境重试"
        await self._send_with_sandbox_fallback(action)
        return {'id': 'active'}

    # ── 官方 WebSocket 接收（免公网 HTTPS 回调）──────────────────────────────
    # QQ 开放平台的"事件订阅"可选 Webhook（要公网 HTTPS）或 WebSocket。
    # 本地/内网部署只有 WebSocket 现实，所以这里实现官方 WS 客户端：
    # 连上 → 收 Hello 拿心跳间隔 → Identify 鉴权 → 收事件（op=0）→ 定时心跳。
    WS_URL = "wss://api.sgroup.qq.com/websocket/"
    OP_HELLO, OP_IDENTIFY, OP_HEARTBEAT = 10, 2, 1
    OP_RECONNECT, OP_INVALID_SESSION, OP_HEARTBEAT_ACK = 7, 9, 11
    INTENTS = (1 << 25) | (1 << 30)      # 群 @ 消息 + 单聊消息
    MIN_INTENTS = 1 << 25                # 最小可用集合（群@ + 单聊都在这位上）
    INTERACTION_INTENT = 1 << 26         # 按钮/指令交互（需要账号开通，未开通时平台拒绝整个会话）

    async def probe_intents(self, extra: int = 0, timeout: float = 12.0) -> dict:
        """只读探测：这套 intents 平台收不收（连一次、看 READY，然后断开）。

        2026-10-15 实测：给用户账号加上 ``1<<26``（按钮交互）后，平台对 Identify
        回 op=9 INVALID_SESSION —— 即**该账号没有开通交互能力**。这个探测让控制台
        能直接告诉用户"点按钮没反应"是平台没开通，而不是程序坏了。
        """
        import websockets

        masks = [("当前（群@ + 单聊）", self.INTENTS)]
        if extra:
            masks.append(("加上按钮交互", self.INTENTS | extra))
        results = []
        token = await self.authorize()
        for label, mask in masks:
            entry = {"label": label, "intents": mask, "ok": False, "detail": ""}
            try:
                headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": self.app_id}
                async with websockets.connect(self.WS_URL, additional_headers=headers,
                                              open_timeout=timeout) as ws:
                    await asyncio.wait_for(ws.recv(), timeout=timeout)      # Hello
                    await ws.send(json.dumps({
                        "op": self.OP_IDENTIFY,
                        "d": {"token": f"QQBot {token}", "intents": mask, "shard": [0, 1]}}))
                    for _ in range(4):
                        payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                        if payload.get("op") == 0:
                            entry["ok"] = True
                            break
                        if payload.get("op") == self.OP_INVALID_SESSION:
                            entry["detail"] = "平台拒绝了这个 intents（会话无效）"
                            break
                        entry["detail"] = f"op={payload.get('op')}"
            except Exception as exc:
                entry["detail"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            results.append(entry)
        return {"ok": results[0]["ok"], "results": results,
                "interaction": next((item["ok"] for item in results[1:]), None)}


    async def run_websocket(self, on_event, should_stop) -> None:
        """持续接收官方事件，直到 ``should_stop()`` 为真。

        ``on_event(event_type, data)`` 收到 op=0 的事件。断线自动重连（退避封顶 60 秒）；
        异常记进 ``last_error``，不让接收循环拖垮整个机器人。

        **intents 降级**：平台对"账号没开通的能力"会直接回 op=9 拒绝整个会话（实测：
        加上按钮交互位就是这样）。所以识别阶段被拒时，会自动退到最小可用集合
        （群@ + 单聊）再试一次 —— 任何一次 intents 调整都不会让机器人连不上。
        """
        import websockets

        backoff = 1
        while not should_stop():
            try:
                await self.authorize()
                headers = {"Authorization": f"QQBot {self.token}", "X-Union-Appid": self.app_id}
                async with websockets.connect(self.WS_URL, additional_headers=headers) as ws:
                    self.ws_connected = False
                    self.last_error = ""
                    backoff = 1
                    self._identified = False
                    self._heartbeat_pending = False
                    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    interval = float((hello.get("d") or {}).get("heartbeat_interval", 40000)) / 1000.0
                    await ws.send(json.dumps({
                        "op": self.OP_IDENTIFY,
                        "d": {"token": f"QQBot {self.token}", "intents": self.intents,
                              "shard": [0, 1]},
                    }))
                    heartbeat = asyncio.create_task(self._ws_heartbeat(ws, interval))
                    try:
                        async for raw in ws:
                            await self._ws_handle(json.loads(raw), on_event)
                            if should_stop():
                                break
                    finally:
                        heartbeat.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 不能用 qq_app.safe_error：那个模块反过来 import 本模块，会成环。
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.ws_connected = False
            if should_stop():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _ws_heartbeat(self, ws, interval: float) -> None:
        while True:
            await asyncio.sleep(max(5.0, interval))
            if getattr(self,'_heartbeat_pending',False):
                self.last_error='QQ心跳未确认，正在重新连接消息通道'
                self.ws_connected=False
                await ws.close(code=1011,reason='heartbeat acknowledgement timeout')
                return
            self._heartbeat_pending=True
            await ws.send(json.dumps({"op": self.OP_HEARTBEAT, "d": self.seq or None}))

    async def _ws_handle(self, payload: dict, on_event) -> None:
        op = payload.get("op")
        if payload.get("s") is not None:
            self.seq = payload["s"]
        if op == self.OP_HEARTBEAT_ACK:
            self._heartbeat_pending=False
            self.last_heartbeat_ack=time.time()
            return
        if op == self.OP_HELLO:
            return
        if op == self.OP_RECONNECT:
            raise ConnectionError("平台要求重连")
        if op == self.OP_INVALID_SESSION:
            self.seq = None
            # 还没 READY 就被拒 = 这套 intents 平台不收（账号没开通对应能力）。
            # 立刻退到最小集合，并记下原因，避免"改一次 intents 就再也连不上"。
            if not self._identified:
                self.downgrade_intents("平台拒绝了当前的 intents（账号可能没开通对应能力）")
            raise ConnectionError("会话失效，需要重新鉴权")
        if op == 0:
            self._identified = True
            self.ws_connected = True
            if payload.get("t"):
                self.last_callback = time.time()
                await on_event(payload["t"], payload.get("d") or {})

    async def close(self):
        await self.http.aclose()


class OneBotQQ:
    def __init__(self, send_gate=None):
        self.send_gate = send_gate
        self.ws = None
        self.self_id = ""
        self.nickname = ""
        self.online = False
        self.last_seen = 0
        self.pending = {}
        self.last_error = ""

    async def call(self, action, params=None):
        ws = self.ws
        if ws is None:
            raise ConnectionError("普通 QQ 客户端尚未连接")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await ws.send_json({"action": action, "params": params or {}, "echo": echo})
            result = await asyncio.wait_for(future, 15)
            # 回包容错：部分实现不带 status 字段，只给 retcode。以前要求
            # status=="ok" 且 retcode==0，于是 {"retcode":0} 这种成功回包被当成失败
            # → 回复发不出去。现在只在"明确失败"时抛错。
            retcode = result.get("retcode")
            status = result.get("status")
            if status == "failed" or retcode not in (None, 0, '0'):
                wording = str(result.get("wording") or result.get("message") or "").strip()
                detail = f"（{wording}）" if wording else ""
                await _gate_report(self, retcode)
                raise RuntimeError(
                    f"普通 QQ 调用 {action} 失败：错误码 {retcode}{detail}")
            return result.get("data") or {}
        finally:
            self.pending.pop(echo, None)

    def receive(self, data):
        self.last_seen = time.time()
        echo = data.get("echo")
        if isinstance(echo, str) and echo in self.pending:
            future = self.pending[echo]
            if not future.done():
                future.set_result(data)
            return True
        if data.get("meta_event_type") == "heartbeat":
            # status 必须是字典：实测客户端偶尔上报字符串，以前这里 AttributeError
            # 会一路冒到接收循环把连接关掉（"机器人突然掉线"）。
            status = data.get("status")
            if isinstance(status, dict):
                self.online = bool(status.get("online", False) and status.get("good", True))
        return False

    async def probe(self):
        """连上后确认客户端身份与登录状态。

        未登录时客户端会对 ``get_login_info`` 回失败码，以前直接抛
        "OneBot 调用失败，错误码 100"，用户看不懂；现在翻译成中文说明。
        """
        try:
            info = await self.call("get_login_info")
        except RuntimeError as exc:
            raise ConnectionError(f"QQ 尚未登录或客户端拒绝了登录信息查询：{exc}") from exc
        previous=self.self_id
        self.self_id = str(info.get("user_id") or "")
        if previous!=self.self_id:self.online=False
        self.nickname = str(info.get('nickname') or '')
        status = await self.call("get_status")
        self.online = bool(self.self_id and status.get("online") and status.get("good", True))
        if not self.online:
            raise ConnectionError("客户端已连接，但 QQ 尚未登录或状态异常")
        self.last_error = ""

    async def send(self, message, text, meta=None):
        """发送文本，并把插件给的 @ 与媒体段组装进同一条消息。

        meta 见 src/plugin_segments.py：mentions 会产生真正的 at 段（写在正文里的
        @名字 对方收不到提醒），media 追加 image/face 段。
        """
        if not self.online:
            raise ConnectionError("普通 QQ 尚未在线")
        await _gate_acquire(self)
        group = message["kind"] == "group"
        segments = build_reply_segments(text, meta)
        if not segments:
            return
        return await self.call("send_group_msg" if group else "send_private_msg", {
            "group_id" if group else "user_id": int(message["target"]),
            "message": segments})

    async def send_group_msg(self, group_id, text):
        """按群号直接发一条文本。

        插件（主动发言、沉默探测等）拿到的是群号而不是消息对象，所以需要这个入口。
        没有它时插件只能去调不存在的方法，异常被钩子吞掉 → 表现为"插件开着却没反应"。
        """
        if not self.online:
            raise ConnectionError("普通 QQ 尚未在线")
        await _gate_acquire(self)
        segments = build_reply_segments(text, None)
        if not segments:
            return
        await self.call("send_group_msg", {"group_id": int(group_id), "message": segments})

    async def send_private_msg(self, user_id, text):
        """按 QQ 号直接发一条私聊文本。"""
        if not self.online:
            raise ConnectionError("普通 QQ 尚未在线")
        await _gate_acquire(self)
        segments = build_reply_segments(text, None)
        if not segments:
            return
        await self.call("send_private_msg", {"user_id": int(user_id), "message": segments})

    async def close(self):
        ws, self.ws = self.ws, None
        self.online = False
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("QQ 客户端连接已断开"))
        if ws:
            try:
                await ws.close()
            except Exception:
                pass


class QqBridge:
    """QQ 账号直连（内置 SnowLuma 协议端，出站 OneBot v11 客户端）。

    与 OneBotQQ（入站、11 客户端连进本程序）方向相反：本程序**主动连出去**接
    SnowLuma 的 OneBot 端点。SnowLuma 注入本机 PC QQ、负责扫码登录；本程序负责
    收发消息，并可在启动时自动拉起 SnowLuma 子进程。消息解析复用
    ``onebot_message`` / ``onebot_notice``，出站段复用 ``build_reply_segments``，
    与 OneBot 路径完全同构 —— 因此本模块不新写任何协议解析逻辑。
    """

    def __init__(self, ws_url="", http_url="", access_token="", snowluma_exe="", send_gate=None):
        self.send_gate = send_gate
        self.ws_url = str(ws_url or "")
        self.http_url = str(http_url or "")
        self.access_token = str(access_token or "")
        self.snowluma_exe = str(snowluma_exe or "")
        self.ws = None
        self.self_id = ""
        self.nickname = ""
        self.online = False
        self.last_error = ""
        self.pending = {}
        self.on_event = None              # 外部注入：async def on_event(message)
        self._proc = None
        self._task = None
        self._closing = False

    # ── SnowLuma 子进程（可选自动拉起；缺省则假设用户已手动启动）──
    def _spawn_snowluma(self):
        import os
        import subprocess
        if not self.snowluma_exe or not os.path.exists(self.snowluma_exe):
            return
        try:
            self._proc = subprocess.Popen([self.snowluma_exe],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self._proc = None
            self.last_error = f"拉起 SnowLuma 失败：{exc}"

    def _stop_snowluma(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    # ── 出站连接与接收（带自动重连，退避封顶 60s）──
    async def connect(self):
        """拉起 SnowLuma（如配置）并建立出站 OneBot 连接 + 后台维护任务。

        首次需人工扫码，因此**不阻塞等待登录**：连接/探测在后台任务里进行，
        ``online`` 在扫码登录成功后自动置 True，回复循环按它自动开闸。
        只等前 5 秒，好把"连接地址根本连不上"这类硬错误尽早落到 last_error。
        """
        self._closing = False
        self._spawn_snowluma()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        for _ in range(5):
            await asyncio.sleep(1)
            if self.online or self.last_error:
                break

    async def _run(self):
        import websockets
        delay = 1
        while not self._closing:
            url = self.ws_url
            if not url:
                self.last_error = "未配置 SnowLuma WebSocket 地址（设置 → QQ 接入 → QQ 直连）"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if self.access_token and "access_token=" not in url:
                url += ("&" if "?" in url else "?") + "access_token=" + self.access_token
            try:
                await self._session(websockets, url)
                self.online = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.online = False
                if not self._closing:
                    self.last_error = f"QQ 直连连接异常：{exc}"
            if self._closing:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _session(self, wsmod, url):
        # 先起 WS，再**并发**跑探测与接收：probe 的 call() 需要接收循环来解包
        # 应答，若串行（先 probe 再收）会死锁。二者同在事件循环里跑，互不阻塞。
        probe_task = None
        async with wsmod.connect(url, open_timeout=15, close_timeout=5) as ws:
            self.ws = ws
            probe_task = asyncio.create_task(self._probe_loop())
            try:
                while not self._closing:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=90)
                    except asyncio.TimeoutError:
                        continue
                    data = self._decode(raw)
                    if data is None:
                        continue
                    if self.receive(data):
                        continue
                    await self._dispatch(data)
            finally:
                # 探测任务取消即可；不 await，避免取消期间 CancelledError 冒出去打断 close()
                probe_task.cancel()
                if self.ws is ws:
                    self.ws = None

    async def _probe_loop(self):
        """后台反复探测登录/在线状态；未登录（扫码未完成）时只记提示、不致命。"""
        while not self._closing:
            try:
                await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                self.online = False
            await asyncio.sleep(30)

    @staticmethod
    def _decode(raw):
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None
        return raw if isinstance(raw, dict) else None

    async def _dispatch(self, data):
        callback = self.on_event
        if not callback:
            return
        try:
            message = onebot_message(data, self.self_id)
            if not message:
                message = onebot_notice(data, self.self_id)
            if message:
                await callback(message)
        except Exception:
            # 单条事件处理失败不打断整条连接（与 OneBot 入站循环同款语义）
            pass

    # ── 与 OneBotQQ 同构的调用 / 收包 / 探测 / 发送 ──
    async def call(self, action, params=None):
        ws = self.ws
        if ws is None:
            raise ConnectionError("QQ 直连尚未连接")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            result = await asyncio.wait_for(future, 15)
            retcode = result.get("retcode")
            status = result.get("status")
            if status == "failed" or retcode not in (None, 0, '0'):
                wording = str(result.get("wording") or result.get("message") or "").strip()
                detail = f"（{wording}）" if wording else ""
                await _gate_report(self, retcode)
                raise RuntimeError(f"QQ 直连调用 {action} 失败：错误码 {retcode}{detail}")
            return result.get("data") or {}
        finally:
            self.pending.pop(echo, None)

    def receive(self, data):
        echo = data.get("echo")
        if isinstance(echo, str) and echo in self.pending:
            future = self.pending[echo]
            if not future.done():
                future.set_result(data)
            return True
        if data.get("meta_event_type") == "heartbeat":
            status = data.get("status")
            if isinstance(status, dict):
                self.online = bool(status.get("online", False) and status.get("good", True))
        return False

    async def probe(self):
        try:
            info = await self.call("get_login_info")
        except RuntimeError as exc:
            raise ConnectionError(f"QQ 尚未登录或协议端拒绝了登录信息查询：{exc}") from exc
        previous=self.self_id
        self.self_id = str(info.get("user_id") or "")
        if previous!=self.self_id:self.online=False
        self.nickname = str(info.get('nickname') or '')
        status = await self.call("get_status")
        self.online = bool(self.self_id and status.get("online") and status.get("good", True))
        if not self.online:
            raise ConnectionError("QQ 直连已连上协议端，但 QQ 尚未扫码登录或状态异常")
        self.last_error = ""

    async def send(self, message, text, meta=None):
        if not self.online:
            raise ConnectionError("QQ 账号尚未在线")
        await _gate_acquire(self)
        group = message.get("kind") == "group"
        segments = build_reply_segments(text, meta)
        if not segments:
            return
        return await self.call("send_group_msg" if group else "send_private_msg", {
            "group_id" if group else "user_id": int(message["target"]),
            "message": segments})

    async def send_active(self, kind, target, text, meta=None):
        if not self.online:
            raise ConnectionError("QQ 账号尚未在线")
        await _gate_acquire(self)
        segments = build_reply_segments(text, meta)
        if not segments:
            return
        await self.call("send_group_msg" if kind == "group" else "send_private_msg", {
            "group_id" if kind == "group" else "user_id": int(target),
            "message": segments})

    async def send_group_msg(self, group_id, text):
        await self.send_active("group", group_id, text)

    async def send_private_msg(self, user_id, text):
        await self.send_active("private", user_id, text)

    async def close(self):
        self._closing = True
        self.online = False
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        ws, self.ws = self.ws, None
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("QQ 直连已关闭"))
        self._stop_snowluma()


def face_name(face_id) -> str:
    """QQ 内置表情 id → 官方名；查不到返回空串（调用方保持原样，宁可不译不许编）。"""
    try:
        from .plugins.face_names import FACE_NAMES
    except Exception:
        return ""
    try:
        return FACE_NAMES.get(int(face_id), "")
    except (TypeError, ValueError):
        return ""


OFFICIAL_EVENTS = {"GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}

# 官方/协议端可能把表情以这种标记形式塞进纯文本：
#   <faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">
# 实测 faceId 与标准 QQ 小黄脸序号一致（0=惊讶 6=害羞 14=微笑），
# 所以能直接用 face_names 表翻译；faceType 只是分类，不参与取名。
# ext 是 base64（如 {"text":""}），普通表情里是空的。
FACE_MARKUP_RE = re.compile(r'<faceType=(\d+),faceId="(\d+)"(?:,ext="([^"]*)")?>')
# 官方纯文本里的表情标记：<emoji:4>
EMOJI_TAG_RE = re.compile(r"<emoji:(\d+)>")
# 官方图片段带 ! 后缀（形如 xxxx.jpg!100）做缩略图标记，取原始地址时去掉
IMAGE_SUFFIX_RE = re.compile(r"![^!]*$")


def _decode_ext(raw):
    """解出 ext 里的 text（普通表情为空）；失败返回空串，绝不抛异常。"""
    if not raw:
        return ""
    import base64
    import json as _json

    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = _json.loads(base64.b64decode(padded).decode("utf-8", "replace"))
    except Exception:
        return ""
    return str((data or {}).get("text") or "").strip() if isinstance(data, dict) else ""


def _face_text(face_id, ext_text=""):
    """表情 → 可读文本。表里没有的 id 保留原始数字（宁可不译不许编）。"""
    name = face_name(face_id)
    if ext_text:
        return f"[表情:{name or face_id}:{ext_text}]"
    return f"[表情:{name}]" if name else f"[表情{face_id}]"


def translate_face_markup(text):
    """把混在纯文本里的 ``<faceType=..,faceId=".."..>`` 翻成 ``[表情:名称]``。

    与 face-decode 插件是同一目的，但这里处理的是**协议端直接塞进文本**的形态
    （不是 OneBot 的 face 段），插件层够不到。

    顺带处理官方另一种标记 ``<emoji:4>``：以前原样漏进提示词，模型看到的是
    "<emoji:4>" 这种天书（真机 2026-10-17：群里只发一个表情时出现）。
    """
    source = "" if text is None else str(text)
    if "emoji:" in source:
        source = EMOJI_TAG_RE.sub(lambda match: f"[表情:{match.group(1)}]", source)
    if "faceId" not in source:
        return source

    def replace(match):
        return _face_text(match.group(2), _decode_ext(match.group(3)))

    return FACE_MARKUP_RE.sub(replace, source)


def _image_url(value):
    # QQ 缩略图标记属于路径，不能把签名查询参数里的感叹号一起裁掉。
    path, separator, query = str(value or '').partition('?')
    path = IMAGE_SUFFIX_RE.sub('', path)
    if path.startswith('//'):
        path = 'https:' + path
    return path + separator + query


def _image_attachment(item):
    from urllib.parse import urlsplit
    content_type = str(item.get('content_type') or '').strip().lower()
    if content_type.startswith('image/') or content_type == 'image':
        return True
    if content_type not in ('', 'application/octet-stream'):
        return False
    if item.get('type') == 'image':
        return True
    # 有些事件只提供文件名或链接；已声明为 PDF/音视频的附件不能误判。
    name = str(item.get('filename') or item.get('file_name') or '').lower()
    try:
        path = urlsplit(_image_url(item.get('url'))).path.lower()
    except ValueError:
        path = ''
    return any(value.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')) for value in (name, path))


def official_content(content):
    """解析官方消息的 content：既支持段数组，也支持纯文本。

    QQ 官方机器人的 ``content`` 既可能是纯字符串，也可能是**消息段数组**
    （text / image / face / voice / file 等）。原来这里直接 ``str(content)``，
    段数组会被 Python 的 repr 变成 ``<faceType=6,faceId="0",ext="...">`` 这种
    天书塞给模型 —— 模型只会回"我看到一串看不懂的符号"。

    返回 ``(text, media)``：
    - text：可读文本（表情译成 ``[表情:名称]``，媒体补占位）
    - media：原始段信息，供插件使用（结构与 OneBot 路径的 media 对齐）
    """
    if isinstance(content, str):
        return translate_face_markup(content).strip(), []

    if not isinstance(content, list):
        return ("" if content is None else str(content)).strip(), []

    parts = []
    media = []
    for segment in content:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        elif seg_type == "face":
            face_id = data.get("id")
            item = {"type": "face", "id": face_id}
            if data.get("text"):
                item["text"] = str(data["text"])
            media.append(item)
            parts.append(_face_text(face_id, str(data.get("text") or "")))
        elif seg_type in ("image", "voice", "video", "file"):
            kind = {"voice": "record"}.get(seg_type, seg_type)
            item = {"type": kind}
            for key in ("url", "file", "file_uuid", "file_name", "size", "width", "height", "asr_refer_text"):
                if data.get(key) is not None:
                    item[key] = data[key]
            if item.get("url"):
                item["url"] = _image_url(item["url"])
            media.append(item)
            parts.append({"image": "[图片]", "record": "[语音]",
                          "video": "[视频]"}.get(kind, f"[{seg_type}]"))
        elif seg_type == "at":
            parts.append(f"[@{data.get('qq') or data.get('user_id') or '某人'}]")
        else:
            # 未知段：保留类型名，别把内容吞了也别编
            parts.append(f"[{seg_type or '未知段'}]")

    return "".join(parts).strip(), media


def official_event(event, data):
    """把官方事件（事件名 + d 字段）转成本项目的消息结构。

    HTTPS 回调与官方 WebSocket 用的是**同一套事件格式**，所以两条路径共用这里，
    避免两边解析口径不一致（一处改了另一处忘改）。
    """
    if event not in OFFICIAL_EVENTS or not isinstance(data, dict):
        return None
    author = data.get("author") or {}
    group = event == "GROUP_AT_MESSAGE_CREATE"
    target = data.get("group_openid") if group else author.get("user_openid")
    if not target or not data.get("id"):
        return None
    text, media = official_content(data.get("content"))
    # 引用消息(103)/聊天记录(102)/结构化卡片(3) 的正文都在 msg_elements / ark_data 里。
    # 以前只读顶层 content，于是"引用一条消息再 @ 机器人"变成了一条空正文 ——
    # 机器人看不到被引用的内容，只能瞎答（真机 2026-10-17）。
    from .qq_elements import render_elements, scene_refs
    elements_text, elements_media, element_marks = render_elements(data.get("msg_elements"), default_kind=data.get("message_type"))
    if isinstance(data.get("ark_data"), dict):
        from .qq_elements import _ark_text
        ark_text = _ark_text(data["ark_data"])
        if ark_text:
            element_marks["card"] = True
            elements_text = ark_text + (("\n" + elements_text) if elements_text else "")
    if elements_text:
        text = (text + "\n" + elements_text).strip() if text.strip() else elements_text
    # 被引用/聊天记录里的图片只作文字说明，不并入本条消息的媒体（否则会被当成本人刚发的图去识图）
    for item in elements_media:
        url = _image_url(item.get("url"))
        if not url or (item.get('quoted') and not (element_marks.get('record') and not element_marks.get('quoted'))):
            continue
        if not any(m.get("url") == url for m in media):
            media.append({**item, "url": url, '_from_chat_record':bool(element_marks.get('record'))})
    refs = scene_refs(data)
    # Official QQ sends images in attachments even when content is an empty string.
    attachments = data.get("attachments")
    for item in attachments if isinstance(attachments, list) else []:
        if not isinstance(item, dict):
            continue
        content_type = str(item.get('content_type') or '').strip().lower()
        kind = 'record' if content_type.startswith('audio/') else 'image' if _image_attachment(item) else ''
        if not kind:
            continue
        url = _image_url(item.get("url"))
        existing = next((m for m in media if url and m.get('type') == kind and m.get('url') == url), None)
        if existing is not None:
            if kind == 'record' and isinstance(item.get('asr_refer_text'), str):
                existing.setdefault('asr_refer_text', item['asr_refer_text'])
            continue
        media.append({"type": kind, "url": url, "name": str(item.get("filename") or ""),
                      "width": item.get("width"), "height": item.get("height"),
                      "size": item.get("size")})
        if kind == 'record' and isinstance(item.get('asr_refer_text'), str):
            media[-1]['asr_refer_text'] = item['asr_refer_text']
        text += "[语音]" if kind == 'record' else "[图片]"
    # 官方正文里的 @ 是 ``<@!openid>`` 标记。以前原样丢给模型和插件 ——
    # 模型看到的是 ``<@!238548A1FA8AF51CC5C1D0D81B67FD05> 群聊还是有些bug``，
    # 插件侧 ``at_ids`` 又永远是空的（想按"是否 @ 到机器人"判断的插件全都失灵）。
    # 这里翻成可读的 ``@某人``（发言人自己则用昵称）并填好 at_ids。
    mentioned = []
    sender_id = str(author.get("member_openid") or author.get("user_openid") or "")
    sender_name = str(author.get("username") or "")

    def render_mention(match):
        openid = match.group(1) or match.group(2) or ""
        if not openid:
            return "@某人"
        mentioned.append(openid)
        if openid == sender_id and sender_name:
            return "@" + sender_name
        # 对方的名字官方没给（只有 openid），用末四位方便人肉对上
        return f"@某人({openid[-4:]})"

    # 群命令的寻址前缀不能成为命令正文；其他 @ 仍保留在 at_ids。
    prefix = re.match(r'^(?:\s*<@!?[^>]{1,64}>)+\s*', text) if group else None
    command = text[prefix.end():].lstrip() if prefix else ''
    text = re.sub(r"<@!([^>]{1,64})>|<@([^>]{1,64})>", render_mention, text).strip()
    if command.startswith('/'):
        text = re.sub(r"<@!([^>]{1,64})>|<@([^>]{1,64})>", render_mention, command).strip()
    # at 表示"这条消息点了机器人的名"。官方只有两种事件：群里被 @ 到（GROUP_AT_MESSAGE_CREATE）
    # 和私聊消息（C2C_MESSAGE_CREATE）——**私聊不是被 @**。
    # 以前无条件写 True，导致按 at 判定的插件在官方私聊里行为跟群里一样：
    # sleep_mode 的"允许 @ 唤醒"对私聊永远生效、affinity 私聊每条都拿 @ 加分。
    message = {"id": str(data["id"]), "kind": "group" if group else "private",
            "target": str(target), "text": text,
            "sender": sender_id or "QQ 用户",
            "sender_id": sender_id,
            "sender_name": sender_name,
            "at_ids": list(dict.fromkeys(mentioned)), "media": media,
            "at": bool(group)}
    # 引用/聊天记录/卡片：把标记与"出站引用要用的 REFIDX"一起带给上层。
    # `_quote_ref` 是**本消息**的引用索引（官方叫 msg_idx），回复时填进 message_reference。
    if refs.get("msg_idx"):
        message["_quote_ref"] = refs["msg_idx"]
    if refs.get("is_quote") or element_marks.get("quoted"):
        message["_is_quote"] = True
    if element_marks.get("record"):
        message["_is_chat_record"] = True
    if element_marks.get("card"):
        message["_is_card"] = True
    if element_marks.get("nested_texts"):
        message["_quoted_texts"] = element_marks["nested_texts"][:6]
    # 官方附件可能已经带有腾讯转写结果；它是用户内容，无需再走 bridge。
    records = [m for m in media if m.get('type') == 'record']
    transcripts = [m['asr_refer_text'].strip()[:2000] for m in records
                   if isinstance(m.get('asr_refer_text'), str) and m['asr_refer_text'].strip()]
    if transcripts:
        message['text'] += '\n[语音转写，仅为用户消息内容]\n' + '\n'.join(transcripts)
        message['speech_status'] = '已转写' if len(transcripts) == len(records) else '部分语音已转写，其余未转写'
    return message


def official_message(payload):
    """HTTPS 回调路径：整包 payload → 消息结构。"""
    return official_event(payload.get("t"), payload.get("d") or {})


# 入站消息里会被保留的媒体段类型（原来这些段被整个丢弃）
MEDIA_SEGMENTS = frozenset({"face", "image", "record", "video", "file", "forward", "json", "xml"})


def cq_segments(text):
    """OneBot 11 先分段再反转义，避免把转义的正文误当作 CQ 指令。"""
    def unescape(value, parameter=False):
        if parameter:
            value = value.replace('&#44;', ',')
        return value.replace('&#91;', '[').replace('&#93;', ']').replace('&amp;', '&')
    parts = []
    cursor = 0
    for match in re.finditer(r'\[CQ:([A-Za-z0-9_]+)((?:,[^\[\]]*)?)\]', text):
        if match.start() > cursor:
            parts.append({'type':'text','data':{'text':unescape(text[cursor:match.start()])}})
        params = {}
        for item in match.group(2).lstrip(',').split(','):
            key, sep, value = item.partition('=')
            if sep and key:
                params[key] = unescape(value, True)
        parts.append({'type':match.group(1),'data':params})
        cursor = match.end()
    if cursor < len(text):
        parts.append({'type':'text','data':{'text':unescape(text[cursor:])}})
    return parts


def onebot_message(data, self_id):
    if data.get("post_type") != "message" or data.get("message_type") not in {"group", "private"}:
        return None
    if str(data.get("user_id")) == str(self_id):
        return None
    segments = data.get("message")
    if isinstance(segments, str):
        segments = cq_segments(segments)
    if not isinstance(segments, list):
        # 合法字符串已转成数组；缺失、对象或数字才属于畸形上报。
        raise ValueError("上报里没有 message 字段或内容为空（不是格式设置问题）")
    group = data["message_type"] == "group"
    target = data.get("group_id") if group else data.get("user_id")
    message_id = data.get("message_id")
    if target is None or message_id is None:
        return None
    sender = data.get("sender")
    if not isinstance(sender, dict):
        sender = {"user_id": data.get("user_id")}
    text_parts = []
    media = []
    at_self = False
    at_ids = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        seg_type = segment.get("type")
        seg_data = segment.get("data")
        if seg_type == "text" and isinstance(seg_data, dict) and isinstance(seg_data.get("text"), str):
            text_parts.append(seg_data["text"])
        elif group and seg_type == "at" and isinstance(seg_data, dict):
            target_qq = seg_data.get("qq")
            # @ 段缺 qq 时以前会把字符串 "None" 塞进 at_ids（下游按名字/号码比对全部失真）
            if target_qq not in (None, ""):
                target_qq = str(target_qq)
                # @ 的判定必须按 QQ 号：同名的人不能算被点名（JS 宿主同款语义）。
                at_ids.append(target_qq)
                at_self = at_self or target_qq == str(self_id)
        elif seg_type in MEDIA_SEGMENTS and isinstance(seg_data, dict):
            # 媒体段原来被直接丢掉，导致依赖它们的插件（表情记忆、表情上限、
            # 表情转译、转发总结、语音转写）拿不到任何输入 —— 插件"装了也没用"。
            # 这里保留原始段信息，交给插件按需消费。
            item = {"type": seg_type}
            for key in ("id", "file", "url", "path", "name", "summary", "title", "desc",
                        "sub_type", "text", "width", "height", "size", "asr_refer_text", "message_id", "file_uuid"):
                if key in seg_data:
                    item[key] = seg_data[key]
            if isinstance(seg_data.get("data"), dict):
                item["card"] = seg_data["data"]
            elif seg_type in ('json','xml') and isinstance(seg_data.get('data'),str):
                item['data']=seg_data['data'][:32000]
            if isinstance(seg_data.get('nodes'), list):
                item['nodes'] = seg_data['nodes'][:200]
            media.append(item)
    # 表情（face）在文本里补一个可读名字：模型看到 "[表情:微笑]" 比 "[表情14]" 有用得多。
    # 查不到的 id 保持数字形式（宁可不译不许编）。
    if media:
        rendered = []
        for item in media:
            if item["type"] == "face":
                label = item.get("id")
                name = face_name(label)
                # 缺 id 时以前渲染成 "[表情None]"（又难看又会被模型当真）
                if name:
                    rendered.append(f"[表情:{name}]")
                elif label not in (None, ""):
                    rendered.append(f"[表情{label}]")
                else:
                    rendered.append("[表情]")
            elif item["type"] == "image":
                rendered.append("[图片]")
            elif item["type"] == "record":
                rendered.append("[语音]")
            elif item["type"] == "video":
                rendered.append("[视频]")
            elif item["type"] == "file":
                rendered.append(f"[文件:{item.get('name') or '未知'}]")
            elif item["type"] == "forward":
                rendered.append("[合并转发]")
            elif item["type"] in ("json", "xml"):
                title = item.get("title") or (item.get("card") or {}).get("title") or ""
                rendered.append(f"[卡片消息:{title}]" if title else "[卡片消息]")
            else:
                rendered.append(f"[{item['type']}]")
        joined = "".join(text_parts).strip()
        suffix = "".join(rendered)
        text_parts = [(joined + suffix).strip()] if (joined or suffix) else []
    return {"id": str(data["message_id"]), "kind": "group" if group else "private",
            "target": str(target), "text": "".join(text_parts).strip(),
            # 媒体段原始列表（图片/表情/语音/转发/卡片…），插件按需读取
            "media": media,
            "sender": str(sender.get("nickname") or data.get("user_id")),
            # sender 是显示用的昵称；sender_id 才是 QQ 号。
            # 缺了它，所有"按人区分"的插件（复读机、回复守卫、连发合并……）都会把
            # 同一个人发的多条消息当成不同人发的 —— 它们读的正是这个键。
            "sender_id": str(data.get("user_id") or ""),
            "sender_name": str(sender.get("nickname") or ""),
            # 本条消息 @ 了哪些 QQ 号（含 'all'）。插件按 QQ 号判定"是谁点名的"。
            "at_ids": at_ids,
            "at": at_self}


# OneBot 通知事件里，本项目当前支持上报给插件的那几种。
# 它们不是聊天消息，但插件需要看到（例如 poke-counter 统计被戳次数）。
NOTICE_KINDS = {
    "poke": "poke",                 # notice_type=poke，戳一戳
    "group_recall": "recall",       # 群消息撤回
    "friend_recall": "recall",      # 私聊消息撤回
    "group_increase": "member_in",  # 有人入群
    "group_decrease": "member_out",  # 有人退群
    "group_admin": "admin",         # 管理员变动
}


def onebot_notice(data, self_id=""):
    """把 OneBot 通知事件转成插件可见的伪消息；不支持的返回 None。

    产出结构与聊天消息同构（id/kind/target/text/sender/at），另外多一个 ``notice``
    字段说明事件类型，以及 ``notice_data`` 原样保留宿主字段。插件在 on_ingest 里
    按 ``message["notice"]`` 判断即可，不需要区分真正的聊天消息。
    """
    if not isinstance(data, dict) or data.get("post_type") != "notice":
        return None
    notice_type = str(data.get("notice_type") or "")
    if notice_type=='group_msg_emoji_like':
        group,user,mid=data.get('group_id'),data.get('user_id',data.get('operator_id')),data.get('message_id')
        if group is None or user is None or mid is None or not re.fullmatch(r'-?\d+',str(mid)):
            return None
        likes=data.get('likes')
        if not isinstance(likes,list):return None
        emojis=list(dict.fromkeys(str(item['emoji_id']) for item in likes if isinstance(item,dict) and re.fullmatch(r'\d{1,12}',str(item.get('emoji_id','')))))[:2]
        if not emojis:return None
        add=str(data.get('sub_type','add'))!='remove'
        return {'id':f'reaction:{data.get("time",0)}:{group}:{mid}:{user}:{",".join(emojis)}:{int(add)}',
                'kind':'group','target':str(group),'sender':str(user),'sender_id':str(user),'text':'','at':False,
                'notice':'emoji_reaction','notice_data':{'user':str(user),'message_id':str(mid),'emojis':emojis,'set':add}}
    kind = "poke" if notice_type == "notify" and data.get("sub_type") == "poke" else NOTICE_KINDS.get(notice_type)
    if not kind:
        return None

    group_id = data.get("group_id")
    user_id = data.get("user_id")
    operator_id = data.get("operator_id")
    if kind == "poke":
        # OneBot 11: user_id 是发起者，target_id 是被戳对象。
        target_id = data.get("target_id")
        own_id = str(self_id or data.get("self_id") or "")
        if not own_id or user_id in (None, "") or target_id in (None, ""):
            return None
        user_id, target_id = str(user_id), str(target_id)
        if user_id == own_id or target_id != own_id:
            return None
        conversation = str(group_id) if group_id not in (None, 0, "", "0") else user_id
        is_group = group_id not in (None, 0, "", "0")
        explicit_id = data.get("event_id") or data.get("id")
        event_id = str(explicit_id) if explicit_id else f"{data.get('time', '')}:poke:{conversation}:{target_id}:{user_id}"
        return {
            "id": event_id, "kind": "group" if is_group else "private",
            "target": conversation, "text": "", "sender": user_id,
            "sender_id": user_id, "self_id": own_id, "at": False, "notice": "poke",
            "notice_data": {"poked": target_id, "poker": user_id,
                            "group": str(group_id) if is_group else ""},
        }

    if group_id is None and user_id is None:
        return None
    return {
        "id": str(data.get("time") or "") + ":" + kind + ":" + str(user_id) + ":" + str(group_id or ""),
        "kind": "group" if group_id is not None else "private",
        "target": str(group_id if group_id is not None else data.get("user_id")),
        "text": "",
        "sender": str(operator_id or user_id or ""),
        "at": False,
        "notice": kind,
        "notice_data": {
            "user": "" if user_id is None else str(user_id),
            "operator": "" if operator_id is None else str(operator_id),
            "group": "" if group_id is None else str(group_id),
            "raw_type": notice_type,
        },
    }
