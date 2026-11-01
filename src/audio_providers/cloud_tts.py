"""T07：云端 TTS 适配 + 预算与出网策略（1.4 / 1.4.1）。

边界（规划书强调，别违反）：
- 云调用只发“合成所需文本/歌词/已授权素材”，不能附整段聊天/日记/本地目录。
- 未获出网允许时零请求；请求已提交而结果未知时**不自动重复计费**。
- 临时音频 URL 先取回（校验域名/重定向/大小/超时/格式）再转项目 PCM WAV；
  URL 过期不触发重新合成。

默认候选：百炼 qwen3-tts-flash（北京地域），端点/模型/音色以官方 schema 固定。
密钥从运行环境/本机凭据管理器读取，日志只写 provider 与请求 ID。

实现补充（2026-10-20）：
- 供应商可能返回 URL / Base64 / 裸 PCM，三条路径分开处理，绝不统一当 WAV；
- 预算用 ``audio_billing.AudioBilling``（预留→结算）；已提交但未知会保留预留；
- 下载域名默认只信 ``dashscope.aliyuncs.com`` 与 ``*.aliyuncs.com``，且不跟随重定向。
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import wave
from urllib.parse import urlsplit

from ..audio_contracts import (
    AudioJobRequest, Capability, ErrorCode, register_provider, reject,
)
from ..voice_service import validate_wav
from ._http import classify_status, request_bytes, validate_outbound

_DEFAULT_ENDPOINT = ('https://dashscope.aliyuncs.com/api/v1/services/aigc/'
                     'multimodal-generation/generation')


def _cfg(config, key: str, default=None):
    try:
        return config.get(key, default)
    except TypeError:
        return config.get(key) if key in config else default


def _key(request: AudioJobRequest) -> str:
    return str(request.client_request_id)


def _wrap_pcm(data: bytes, *, sample_rate: int, channels: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(int(channels))
        stream.setsampwidth(2)
        stream.setframerate(int(sample_rate))
        stream.writeframes(data)
    return buffer.getvalue()


@register_provider
class CloudTtsProvider:
    name = 'cloud_tts'
    capability = Capability.TTS
    supports_cloud = True
    _DEFAULT = _DEFAULT_ENDPOINT

    def __init__(self, config, credentials=None, billing=None, http_transport=None,
                 http_client=None):
        self.config = config
        self.credentials = credentials    # 由核心注入，绝不在配置明文落盘
        self.billing = billing            # AudioBilling（预留/结算）
        self.http_transport = http_transport
        self.http_client = http_client

    # ── 配置 ────────────────────────────────────────────────────────────────
    def _allow_outbound(self) -> bool:
        return bool(_cfg(self.config, 'qq_only.voice.cloud.allow_outbound', False))

    def _api_key(self) -> str:
        credentials = self.credentials
        key = getattr(credentials, 'api_key', None)
        if not key and isinstance(credentials, str):
            key = credentials
        if not key and isinstance(credentials, dict):
            key = credentials.get('api_key')
        return str(key or '').strip()

    def _endpoint(self) -> str:
        endpoint = str(_cfg(self.config, 'qq_only.voice.cloud.endpoint', self._DEFAULT))
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'https' or (parsed.hostname or '').lower() != 'dashscope.aliyuncs.com':
            reject(ErrorCode.INVALID_INPUT, '云端 TTS 端点须为官方 https 地址')
        return endpoint

    def _download_hosts(self) -> set[str]:
        configured = _cfg(self.config, 'qq_only.voice.cloud.download_hosts', None)
        if isinstance(configured, (list, tuple)) and configured:
            return {str(host).lower() for host in configured}
        return {'dashscope.aliyuncs.com'}

    # ── 能力 ────────────────────────────────────────────────────────────────
    def available(self) -> bool:
        # 三者齐了才“可用”：允许出网、有预算、密钥就位。
        return (self._allow_outbound() and self.billing is not None
                and bool(self._api_key()))

    def ready(self) -> bool:
        return self.available()

    # ── 合成 ────────────────────────────────────────────────────────────────
    async def synthesize(self, request: AudioJobRequest, profile: dict) -> bytes:
        # 未允许出网时零请求（T07 验收）。
        if not self._allow_outbound():
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   '未允许云出网，拒绝任何云端请求')
        if self.billing is None:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED, '未配置云预算，拒绝付费合成')
        key = self._api_key()
        if not key:
            reject(ErrorCode.UNAUTHORIZED, '云端 TTS 缺少服务端密钥')
        text = str(request.text or '').strip()
        if not 1 <= len(text) <= 300:
            reject(ErrorCode.INVALID_INPUT, '待合成文字须为 1–300 个字符')
        model = str(_cfg(self.config, 'qq_only.voice.cloud.model', 'qwen3-tts-flash')).strip()
        voice = str(_cfg(self.config, 'qq_only.voice.cloud.voice', 'Cherry')).strip()
        if not model or not voice:
            reject(ErrorCode.INVALID_INPUT, '请先配置云模型与云音色')
        request_key = _key(request)
        state = self.billing.state_for(request_key)
        if state in ('unknown', 'reserved'):
            # 上次已提交但结果未知：不自动重提，等人工/对账后处理（不重复计费）。
            reject(ErrorCode.GENERATION_UNKNOWN,
                   '该请求已有未结算的云端调用，未自动重试', provider='cloud_tts')
        if state == 'settled':
            reject(ErrorCode.INVALID_INPUT, '该请求已完成计费，拒绝重复提交', provider='cloud_tts')
        estimated = self.billing.estimate(text)
        self.billing.reserve(request_key, estimated)
        payload = {'model': model, 'input': {'text': text, 'voice': voice}}
        try:
            result = await request_bytes(
                'POST', self._endpoint(), payload=payload, transport=self.http_transport,
                headers={'Authorization': 'Bearer ' + key}, timeout=120.0,
                client=self.http_client)
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code == ErrorCode.DEADLINE_EXCEEDED:
                # 请求可能已送达：不重复提交、不释放预留。
                self.billing.settle(request_key, submitted=True, success=False,
                                    note='请求超时，结果未知，未自动重提')
                reject(ErrorCode.GENERATION_UNKNOWN,
                       '云端合成结果未知（请求可能已提交），未自动重试', provider='cloud_tts')
            self.billing.settle(request_key, submitted=False, success=False,
                                note='请求未提交成功')
            raise
        if result.status >= 400:
            if result.status in (401, 403, 429):
                self.billing.settle(request_key, submitted=False, success=False,
                                    note=f'HTTP {result.status}')
            elif result.status >= 500:
                self.billing.settle(request_key, submitted=True, success=False,
                                    note=f'HTTP {result.status}')
            else:
                self.billing.settle(request_key, submitted=False, success=False,
                                    note=f'HTTP {result.status}')
            classify_status(result.status, result.content, name='云端 TTS')
        try:
            audio = await self._extract(result.content, result.content_type)
            validate_wav(audio)
        except Exception:
            # 生成已完成（音频可能已计费），只是解码/下载失败：
            self.billing.settle(request_key, success=True, actual=estimated,
                                note='生成完成，但音频解码失败')
            raise
        self.billing.settle(request_key, success=True, actual=estimated,
                            note='synthesized')
        return audio

    # ── 响应分流 ────────────────────────────────────────────────────────────
    async def _extract(self, content: bytes, content_type: str) -> bytes:
        if content[:4] == b'RIFF':
            return content
        try:
            payload = json.loads(content.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            reject(ErrorCode.ASSET_INVALID, '云端 TTS 返回既非 JSON 也非 WAV')
        if not isinstance(payload, dict):
            reject(ErrorCode.ASSET_INVALID, '云端 TTS 响应结构不正确')
        output = payload.get('output') if isinstance(payload.get('output'), dict) else {}
        audio = output.get('audio') if isinstance(output.get('audio'), dict) else {}
        if not audio and isinstance(payload.get('audio'), dict):
            audio = payload['audio']
        url = (audio.get('url') or payload.get('audio_url') or '')
        encoded = (audio.get('data') or payload.get('audio_base64') or '')
        if url:
            return await self._download(str(url))
        if encoded:
            try:
                data = base64.b64decode(str(encoded), validate=True)
            except (binascii.Error, ValueError):
                reject(ErrorCode.ASSET_INVALID, '云端 TTS 音频不是合法 base64')
            if data[:4] == b'RIFF':
                return data
            fmt = str(audio.get('format') or '').lower()
            if fmt in ('pcm', 'raw'):
                return _wrap_pcm(data,
                                 sample_rate=int(_cfg(self.config, 'qq_only.voice.cloud.sample_rate', 24000)),
                                 channels=int(_cfg(self.config, 'qq_only.voice.cloud.channels', 1)))
            reject(ErrorCode.ASSET_INVALID, '云端 TTS 返回的 Base64 不是 WAV 且未声明 PCM 格式')
        reject(ErrorCode.ASSET_INVALID, '云端 TTS 响应缺少音频 URL 或数据')

    async def _download(self, url: str) -> bytes:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        allowed = self._download_hosts()
        if not (host in allowed or any(host == item or host.endswith('.' + item)
                                       for item in allowed)):
            reject(ErrorCode.ASSET_INVALID, '云端 TTS 返回了未授权下载域名', host=host)
        validate_outbound(url, allowed_hosts={host}, name='云端 TTS 临时地址')
        result = await request_bytes('GET', url, transport=self.http_transport,
                                     timeout=60.0, follow_redirects=False,
                                     client=self.http_client)
        if 300 <= result.status < 400:
            reject(ErrorCode.ASSET_INVALID, '临时音频 URL 发生重定向，拒绝跨域跟随')
        if result.status in (403, 404, 410):
            reject(ErrorCode.ASSET_INVALID, '临时音频 URL 已过期，不重新合成')
        classify_status(result.status, result.content, name='云端 TTS 下载')
        return result.content

    # ── 预算钩子（供网关调用；与 billing 同步） ─────────────────────────────
    def reserve(self, request: AudioJobRequest) -> dict:
        if self.billing is None:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED, '未配置云预算')
        return self.billing.reserve(_key(request),
                                    self.billing.estimate(str(request.text or '')))

    def settle(self, request: AudioJobRequest, success: bool, *,
               submitted: bool = True, actual: float | None = None) -> dict:
        if self.billing is None:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED, '未配置云预算')
        return self.billing.settle(_key(request), success=success, submitted=submitted,
                                   actual=actual)

    def budget_snapshot(self) -> dict:
        if self.billing is None:
            return {}
        return self.billing.snapshot().to_dict()
