"""T12：云端原创歌曲（S1，ElevenLabs Music 官方 /v1/music）。

边界（1.4.2 / T12 验收）：
- 单次 prompt 模式先要 15–30 秒；指定歌词用官方 schema 验证过的 composition_plan，
  不能同时发 prompt 和 composition_plan。
- force_instrumental:false **不足以保证有歌声**：产物要单独做“确有演唱/歌词可懂”验收，
  纯伴奏不能标“唱歌通过”。
- 网关内部异步任务：供应商只给同步长请求也由本地作业执行器等待，QQ 接入线程立即释放。
- 无法远程取消时，取消本地交付并丢弃后到结果，显示“供应商可能继续生成/计费”。
- 随机原创**不能**标成“某首指定歌曲翻唱”；未知参考素材不出网。

依赖 T07 的出网/预算接口；账号需具备付费音乐 API 权限并能连通。

实现补充（2026-10-20）：供应商可能返回音频字节、JSON 里的临时 URL 或 Base64，
三条路径分开处理；``vocal_verified`` 默认 False，必须由试听/ASR 复核后显式接受。
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Callable, Literal, Optional
from urllib.parse import urlsplit

from ..audio_contracts import AudioJobRequest, Capability, ErrorCode, reject
from ..audio_billing import AudioBilling
from ..audio_providers._http import classify_status, request_bytes


def _cfg(config, key: str, default=None):
    try:
        return config.get(key, default)
    except TypeError:
        return config.get(key) if key in config else default


def _credential(credentials) -> str:
    key = getattr(credentials, 'api_key', None)
    if not key and isinstance(credentials, str):
        key = credentials
    if not key and isinstance(credentials, dict):
        key = credentials.get('api_key')
    return str(key or '').strip()


class CloudMusicProvider:
    capability = Capability.SONG_GENERATE
    supports_cloud = True
    _URL = 'https://api.elevenlabs.io/v1/music'   # model_id 固定在适配器配置

    def __init__(self, config, credentials=None, billing: Optional[AudioBilling] = None,
                 http_transport=None, http_client=None):
        self.config = config
        self.credentials = credentials   # xi-api-key，服务端持有，日志只写请求 ID
        self.billing = billing
        self.http_transport = http_transport
        self.http_client = http_client

    # ── 能力 ────────────────────────────────────────────────────────────────
    def available(self) -> bool:
        return (bool(_cfg(self.config, 'qq_only.voice.cloud.allow_outbound', False))
                and bool(_credential(self.credentials)) and self.billing is not None)

    def _endpoint(self) -> str:
        endpoint = str(_cfg(self.config, 'qq_only.voice.music.endpoint', self._URL))
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'https' or (parsed.hostname or '').lower() != 'api.elevenlabs.io':
            reject(ErrorCode.INVALID_INPUT, '云音乐端点须为官方 https 地址')
        return endpoint

    def _download_hosts(self) -> set[str]:
        configured = _cfg(self.config, 'qq_only.voice.music.download_hosts', None)
        if isinstance(configured, (list, tuple)) and configured:
            return {str(host).lower() for host in configured}
        return {'api.elevenlabs.io', 'elevenlabs.io', 'storage.googleapis.com'}

    # ── 生成 ────────────────────────────────────────────────────────────────
    async def generate(self, job: AudioJobRequest, *,
                       mode: Literal['prompt', 'composition_plan'],
                       prompt: Optional[str] = None,
                       composition_plan: Optional[dict] = None,
                       instrumental: bool = False, target_seconds: int = 30,
                       verify_vocals: Optional[Callable[[bytes], bool]] = None) -> dict:
        """生成一段含人声、歌词可辨的短曲；返回 {audio, provider_song_id, request_id}。"""
        if mode not in ('prompt', 'composition_plan'):
            reject(ErrorCode.INVALID_INPUT, '云音乐模式须为 prompt 或 composition_plan')
        if mode == 'prompt':
            if not prompt or not str(prompt).strip():
                reject(ErrorCode.INVALID_INPUT, 'prompt 模式需要提示词')
            if composition_plan is not None:
                reject(ErrorCode.INVALID_INPUT, '不能同时发送 prompt 和 composition_plan')
        else:
            if not isinstance(composition_plan, dict) or not composition_plan:
                reject(ErrorCode.INVALID_INPUT, 'composition_plan 模式需要结构化计划')
            if prompt:
                reject(ErrorCode.INVALID_INPUT, '不能同时发送 prompt 和 composition_plan')
        if not self.available():
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   '云音乐未配置（需允许出网 + 付费 API 权限 + 预算）')
        try:
            seconds = int(target_seconds)
        except (TypeError, ValueError):
            reject(ErrorCode.INVALID_INPUT, '目标时长须为整数秒')
        if not 5 <= seconds <= 120:
            reject(ErrorCode.INVALID_INPUT, '原创曲目首版时长须为 5–120 秒')
        text_for_estimate = str(prompt or json.dumps(composition_plan, ensure_ascii=False))
        estimated = (self.billing.estimate_seconds(seconds)
                     if self.billing.price_per_second > 0 else self.billing.estimate(text_for_estimate))
        key = str(job.client_request_id)
        state = self.billing.state_for(key)
        if state in ('reserved', 'unknown'):
            reject(ErrorCode.GENERATION_UNKNOWN,
                   '该请求已有未结算的云音乐调用，未自动重试', provider='cloud_music')
        if state == 'settled':
            reject(ErrorCode.INVALID_INPUT, '该请求已完成计费，拒绝重复提交')
        self.billing.reserve(key, estimated)
        body = {'music_length_ms': seconds * 1000,
                'force_instrumental': bool(instrumental)}
        if mode == 'prompt':
            body['prompt'] = str(prompt).strip()
        else:
            body['composition_plan'] = composition_plan
        try:
            result = await request_bytes(
                'POST', self._endpoint(), payload=body, transport=self.http_transport,
                headers={'xi-api-key': _credential(self.credentials)}, timeout=300.0,
                max_bytes=32 * 1024 * 1024, client=self.http_client)
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code == ErrorCode.DEADLINE_EXCEEDED:
                self.billing.settle(key, submitted=True, success=False,
                                    note='长请求超时，结果未知，未自动重提')
                reject(ErrorCode.GENERATION_UNKNOWN,
                       '云音乐生成结果未知（请求可能已提交），未自动重试',
                       provider='cloud_music')
            self.billing.settle(key, submitted=False, success=False, note='请求未提交')
            raise
        if result.status >= 400:
            submitted = result.status >= 500
            self.billing.settle(key, submitted=submitted, success=False,
                                note=f'HTTP {result.status}')
            classify_status(result.status, result.content, name='云音乐')
        try:
            audio, media_type = await self._extract_audio(result.content,
                                                          result.content_type)
        except Exception:
            self.billing.settle(key, success=True, actual=estimated,
                                note='生成完成但音频解析失败')
            raise
        vocal_ok = None
        if verify_vocals is not None:
            try:
                vocal_ok = bool(verify_vocals(audio))
            except Exception:
                vocal_ok = False
            if not vocal_ok:
                self.billing.settle(key, success=True, actual=estimated,
                                    note='产物未通过人声验收（纯伴奏不计）')
                reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                       '云音乐产物未通过“确有演唱/歌词可辨”验收，不能标为唱歌成功',
                       provider='cloud_music')
        self.billing.settle(key, success=True, actual=estimated, note='generated')
        return {'audio': audio, 'media_type': media_type,
                'provider': 'cloud_music', 'provider_song_id': result.url.rsplit('/', 1)[-1],
                'request_id': key, 'duration_seconds': seconds,
                'vocal_verified': vocal_ok,
                'singing_verified': False,
                'warning': ('未验证是否含人声；纯伴奏不能标为唱歌成功'
                            if vocal_ok is None else ''),
                'cancel_note': '供应商无法远程取消时，可能继续生成/计费'}

    # ── 响应分流 ────────────────────────────────────────────────────────────
    async def _extract_audio(self, content: bytes, content_type: str) -> tuple[bytes, str]:
        lowered = (content_type or '').lower()
        if lowered.startswith('audio/') or content[:4] == b'RIFF' or content[:3] == b'ID3':
            media = 'audio/wav' if content[:4] == b'RIFF' else (lowered.split(';')[0] or 'audio/mpeg')
            return content, media
        try:
            payload = json.loads(content.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            reject(ErrorCode.ASSET_INVALID, '云音乐返回既非音频也非 JSON')
        if not isinstance(payload, dict):
            reject(ErrorCode.ASSET_INVALID, '云音乐响应结构不正确')
        url = str(payload.get('audio_url') or payload.get('url') or '')
        encoded = payload.get('audio_base64') or payload.get('data') or ''
        if url:
            return await self._download(url)
        if encoded:
            try:
                data = base64.b64decode(str(encoded), validate=True)
            except (binascii.Error, ValueError):
                reject(ErrorCode.ASSET_INVALID, '云音乐音频不是合法 base64')
            media = 'audio/wav' if data[:4] == b'RIFF' else 'audio/mpeg'
            return data, media
        reject(ErrorCode.ASSET_INVALID, '云音乐响应缺少音频')

    async def _download(self, url: str) -> tuple[bytes, str]:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        allowed = self._download_hosts()
        if not (host in allowed or any(host == item or host.endswith('.' + item)
                                       for item in allowed)):
            reject(ErrorCode.ASSET_INVALID, '云音乐返回了未授权下载域名', host=host)
        if parsed.scheme != 'https' or parsed.username or parsed.password:
            reject(ErrorCode.ASSET_INVALID, '云音乐下载地址不是安全的 https 地址')
        result = await request_bytes('GET', url, transport=self.http_transport,
                                     timeout=120.0, follow_redirects=False,
                                     max_bytes=32 * 1024 * 1024, client=self.http_client)
        if 300 <= result.status < 400:
            reject(ErrorCode.ASSET_INVALID, '云音乐下载发生重定向，拒绝跨域跟随')
        if result.status in (403, 404, 410):
            reject(ErrorCode.ASSET_INVALID, '云音乐临时地址已过期，不重新生成')
        classify_status(result.status, result.content, name='云音乐下载')
        media = 'audio/wav' if result.content[:4] == b'RIFF' else (
            result.content_type.split(';')[0] or 'audio/mpeg')
        return result.content, media


def accept_as_singing(result: dict, *, heard_lyrics: bool, verified_by: str = '') -> dict:
    """人工/ASR 复核后把产物标成“唱歌验收通过”；纯伴奏不得调用本函数。"""
    if not isinstance(result, dict) or not result.get('audio'):
        reject(ErrorCode.ASSET_INVALID, '没有可验收的云音乐产物')
    if not heard_lyrics:
        reject(ErrorCode.CAPABILITY_UNSUPPORTED,
               '未确认歌词可辨/确有演唱，不能标为唱歌通过')
    result['singing_verified'] = True
    result['vocal_verified'] = True
    result['verified_by'] = str(verified_by or 'manual')
    return result
