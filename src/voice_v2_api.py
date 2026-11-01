"""v2 音频网关 API（规划书 1.5.1 的 7 条接口 + 控制台需要的只读辅助）。

设计约束（与旧 voice_api.py 一致，沿用现有 admin 中间件保护）：
- 保留旧 /api/voice/* 不变，新增 /api/v2/audio/*；
- 每个返回都是可机器解析的结构，错误用 audio_contracts.ContractError 的错误码；
- 发送目标只能来自可信消息事件，绝不能来自模型输出（1.6.1/1.7.2）。

接线（2026-10-20）：
- 本模块自己创建并持有 ``AudioGateway``（懒加载），DB 初始化幂等，不改旧表；
- 发送原语由 ``app.state.bot`` 提供（qqbridge.send_voice），并复用 ``voice_sends``
  做请求级幂等；没有可用机器人时，计划只落“本机保存”，不假装已发；
- 可信主体：本机控制台固定为 ``console:local`` / ``local-admin``；消息入站链可
  通过 ``app.state.audio_identity``（dict 或可调用）覆盖，绝不读取请求体里的身份。
"""
from __future__ import annotations

import json
import re

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .audio_contracts import (
    AudioAssetMeta, AudioJobRequest, AudioMode, ContractError, DeliveryPlanRequest,
    ErrorCode,
)
from .audio_gateway import AudioGateway
from .audio_console import EngineSettingsRequest, settings as engine_settings, save_settings, probe

CONSOLE_DOMAIN = 'console:local'
CONSOLE_PRINCIPAL = 'local-admin'


class DeliverySendRequest(BaseModel):
    segments: list[int] = Field(default_factory=list)


class VoiceImportRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)
    table_path: str = ''
    provider: str = ''
    language: str = 'zh'
    licensed: bool = False
    allow_cloud: bool = False


def ensure_audio_gateway(app) -> AudioGateway:
    """创建/复用网关并挂到 app/bot（启动时与首次请求共用同一份状态）。"""
    existing = getattr(app.state, 'audio_gateway', None)
    if existing is not None:
        return existing
    bot = app.state.bot
    service = AudioGateway(bot.root, bot.config, bot.db)
    service.set_sender(_make_sender(app, bot))
    app.state.audio_gateway = service
    # 插件（T08 自动语音）通过 bot 找到网关；仍受同一发送/权限检查。
    try:
        bot.audio_gateway = service
    except Exception:
        pass
    return service


def install_v2_audio_routes(app):
    # ── 网关/身份 ───────────────────────────────────────────────────────────
    def gateway() -> AudioGateway:
        return ensure_audio_gateway(app)

    def identity(request: Request) -> tuple[str, str]:
        resolver = getattr(app.state, 'audio_identity', None)
        if callable(resolver):
            value = resolver(request) or {}
        elif isinstance(resolver, dict):
            value = resolver
        else:
            value = {}
        domain = str(value.get('subject_domain') or CONSOLE_DOMAIN)
        principal = str(value.get('principal') or CONSOLE_PRINCIPAL)
        return domain, principal

    # ── 1. 能力发现 ─────────────────────────────────────────────────────────
    @app.get('/api/v2/audio/capabilities')
    async def capabilities():
        return gateway().capabilities()

    @app.get('/api/v2/audio/settings')
    async def settings():
        return engine_settings(app.state.bot.config)

    @app.post('/api/v2/audio/settings')
    async def update_settings(body: EngineSettingsRequest):
        service = gateway()
        # Running jobs read configuration through their adapters. Do not let a
        # settings save change an in-flight request or queued voice unexpectedly.
        active = service.db.execute("SELECT COUNT(*) FROM audio_jobs WHERE state IN "
                                    "('queued','preparing','running','postprocessing',"
                                    "'cancel_requested')").fetchone()[0]
        if active:
            raise HTTPException(409, '仍有音频任务，请完成或取消后再保存引擎设置')
        try:
            result = save_settings(app.state.bot.config, body)
        except ContractError as exc:
            raise _http(exc)
        except Exception:
            raise HTTPException(500, '保存引擎设置失败，原设置已保留') from None
        service._adapters.clear()
        service.cache.clear()
        return result

    @app.post('/api/v2/audio/probe')
    async def probe_engine(body: EngineSettingsRequest):
        try:
            return await probe(app.state.bot.config, body,
                               transport=gateway().http_transport)
        except ContractError as exc:
            raise _http(exc)

    # ── 音色库（导入多个参考音频 / 标签表） ─────────────────────────────────
    @app.get('/api/v2/audio/voices')
    async def voices():
        return {'voices': gateway().list_voices()}

    @app.post('/api/v2/audio/voices/import')
    async def import_voices(body: VoiceImportRequest):
        try:
            imported = gateway().import_voices(
                paths=body.paths, table_path=body.table_path,
                provider=body.provider, language=body.language,
                licensed=body.licensed, allow_cloud=body.allow_cloud)
            return {'success': True, 'imported': imported, 'count': len(imported)}
        except ContractError as exc:
            raise _http(exc)

    @app.delete('/api/v2/audio/voices/{profile_id}')
    async def delete_voice(profile_id: str):
        if not gateway().delete_voice(profile_id):
            raise HTTPException(404, '音色不存在')
        return {'success': True, 'profile_id': profile_id}

    @app.post('/api/v2/audio/voices/{profile_id}/default')
    async def set_default_voice(profile_id: str):
        """把某个已导入音色设为自动语音的默认音色（持久化到配置）。"""
        service = gateway()
        if service.voices.get(profile_id) is None:
            raise HTTPException(404, '音色不存在')
        bot = app.state.bot
        try:
            bot.config.set('qq_only.voice.default_profile', profile_id)
            bot.config.save_atomic()
        except Exception as exc:
            raise HTTPException(500, '保存默认音色失败：' + type(exc).__name__)
        service.cache.clear()
        return {'success': True, 'default': profile_id}

    @app.get('/api/v2/audio/status')
    async def status(request: Request):
        domain, _principal = identity(request)
        service = gateway()
        return {'capabilities': service.capabilities(),
                'jobs': service.recent_jobs(subject_domain=domain, limit=30),
                'data_root': str(service.root.resolve()),
                'schema_version': 'audio-contracts-v1'}

    # ── 2. 创建作业 ─────────────────────────────────────────────────────────
    @app.post('/api/v2/audio/jobs')
    async def create(body: AudioJobRequest, request: Request):
        domain, principal = identity(request)
        if body.mode is not AudioMode.TTS:
            raise HTTPException(503, detail={
                'code': ErrorCode.CAPABILITY_UNSUPPORTED.value,
                'message': f'{body.mode.value} 尚未接入音频网关；S1/S2/S3 使用独立管线',
            })
        try:
            result = await gateway().create_tts(
                text=body.text or '', subject_domain=domain, principal=principal,
                delivery_intent=body.delivery_intent.value,
                route_policy=body.route_policy.value,
                client_request_id=body.client_request_id,
                voice_profile_id=body.voice_profile_id)
            return {'success': True, **result}
        except ContractError as exc:
            raise _http(exc)

    # ── 3. 查作业状态 ───────────────────────────────────────────────────────
    @app.get('/api/v2/audio/jobs')
    async def jobs(request: Request, limit: int = 50):
        domain, _principal = identity(request)
        return {'jobs': gateway().recent_jobs(subject_domain=domain, limit=limit)}

    @app.get('/api/v2/audio/jobs/{job_id}')
    async def job_status(job_id: str, request: Request):
        domain, _principal = identity(request)
        try:
            row = gateway().get_job(job_id, subject_domain=domain)
        except ContractError as exc:
            raise _http(exc)
        if row is None:
            raise HTTPException(404, '作业不存在')
        return row

    # ── 4. 取消 ─────────────────────────────────────────────────────────────
    @app.post('/api/v2/audio/jobs/{job_id}/cancel')
    async def cancel(job_id: str, request: Request):
        domain, _principal = identity(request)
        try:
            return await gateway().cancel(job_id, subject_domain=domain)
        except ContractError as exc:
            raise _http(exc)

    # ── 5. 资产预览/元数据 ──────────────────────────────────────────────────
    @app.get('/api/v2/audio/assets/{audio_id}')
    async def asset(audio_id: str, request: Request) -> AudioAssetMeta:
        domain, _principal = identity(request)
        try:
            meta = gateway().asset_meta(audio_id, subject_domain=domain)
        except ContractError as exc:
            raise _http(exc)
        return AudioAssetMeta(
            audio_id=meta['audio_id'], media_type=meta.get('media_type', 'audio/wav'),
            sample_rate=int(meta.get('sample_rate') or 0),
            channels=int(meta.get('channels') or 1),
            duration_seconds=float(meta.get('duration_seconds') or 0.0),
            size_bytes=int(meta.get('size_bytes') or 0),
            content_hash=meta.get('content_hash', audio_id),
            owner_domain=meta.get('owner_domain', domain),
            licensed=bool(meta.get('licensed', False)))

    # ── 6. 显式发送计划 ────────────────────────────────────────────────────
    @app.post('/api/v2/audio/deliveries')
    async def delivery(body: DeliveryPlanRequest, request: Request):
        domain, _principal = identity(request)
        try:
            return gateway().submit_delivery(body, subject_domain=domain)
        except ContractError as exc:
            raise _http(exc)

    # ── 7. 查询/执行发送结果 ────────────────────────────────────────────────
    @app.get('/api/v2/audio/deliveries/{delivery_id}')
    async def delivery_status(delivery_id: str, request: Request):
        domain, _principal = identity(request)
        try:
            return gateway().delivery_status(delivery_id, subject_domain=domain)
        except ContractError as exc:
            raise _http(exc)

    @app.post('/api/v2/audio/deliveries/{delivery_id}/send')
    async def delivery_send(delivery_id: str, body: DeliverySendRequest,
                            request: Request):
        domain, _principal = identity(request)
        try:
            return await gateway().send_delivery(
                delivery_id, subject_domain=domain,
                segments=body.segments or None)
        except ContractError as exc:
            raise _http(exc)


# ── 发送原语（QQ 桥接） ─────────────────────────────────────────────────────
def _make_sender(app, bot):
    """返回 gateway.set_sender 需要的协程；所有检查在真正发送前重做一遍。"""

    async def send(*, kind: str, target: str, account_id: str, path,
                   request_id: str) -> dict:
        if kind not in {'group', 'private'} or not str(target).isdigit():
            raise ValueError('发送目标无效')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(request_id)):
            raise ValueError('request_id 格式无效')
        lock = getattr(bot, 'lock', None)
        if lock is None:
            raise ValueError('机器人未就绪')
        async with lock:
            if getattr(bot, 'mode', '') != 'qqbridge':
                raise ValueError('当前不是 QQ Bridge 模式，不能发送语音')
            bridge = getattr(bot, 'bridge', None)
            if bridge is None or not getattr(bridge, 'online', False):
                raise ValueError('QQ 桥接未连接')
            if not str(account_id).isdigit() or str(bot.account()) != str(account_id):
                raise ValueError('账号已变化，拒绝发送（切号不发）')
            if hasattr(bot, 'can_send_proactive') and not bot.can_send_proactive(
                    kind, str(target)):
                raise ValueError('当前回复规则不允许发送到该会话')
            payload = json.dumps([str(account_id), kind, str(target), str(path)],
                                 ensure_ascii=False)
            existing = bot.db.execute(
                'SELECT payload,state,result FROM voice_sends WHERE request_id=?',
                (str(request_id),)).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValueError('request_id 已用于其他语音或目标')
                return {'duplicate': True, 'state': existing[1],
                        'result': json.loads(existing[2] or '{}')}
            audio = path.read_bytes() if hasattr(path, 'read_bytes') else bytes(path)
            bot.db.execute('INSERT INTO voice_sends VALUES(?,?,?,?)',
                           (str(request_id), payload, 'sending', '{}'))
            bot.db.commit()
            try:
                result = await bridge.send_voice(kind, str(target), audio)
            except BaseException:
                bot.db.execute("UPDATE voice_sends SET state='uncertain' WHERE request_id=?",
                               (str(request_id),))
                bot.db.commit()
                raise
            result = ({'message_id': str(result.get('message_id') or result.get('id') or '')}
                      if isinstance(result, dict) else {})
            bot.db.execute("UPDATE voice_sends SET state='sent',result=? WHERE request_id=?",
                           (json.dumps(result, ensure_ascii=False), str(request_id)))
            bot.db.commit()
            return result

    return send


def _http(exc: ContractError) -> HTTPException:
    status = 400 if exc.code in {
        ErrorCode.INVALID_INPUT, ErrorCode.CAPABILITY_UNSUPPORTED,
        ErrorCode.UNAUTHORIZED, ErrorCode.POLICY_DENIED, ErrorCode.ASSET_INVALID,
    } else 503
    return HTTPException(status, detail=exc.to_payload())
