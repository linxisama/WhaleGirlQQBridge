"""T06：Qwen3-TTS WebUI 适配器（1.3.1）。

按本机部署的专用 REST 契约接入，**不**假定它提供 OpenAI 兼容 /v1/audio/speech：
- custom-voice  : 固定说话人
- voice-clone   : 参考音频克隆（参考音频从已导入资产读，再发本机服务）
- voice-design  : 描述式音色（可选创作，不保证跨次一致）

初版 segment_gen=False，等完整 WAV 再送 QQ。API 的 info 是提示文本，
不能解析它推断成功/文件数/耗时，以结构化字段和音频校验为准。

实现补充（2026-10-20）：
- 只认 ``audio_files_base64``（兼容 ``audio_files``/``audio_base64``），逐条解码并
  校验 WAV；多段按相同采样率/声道拼接成一条完整 WAV，参数不一致则明确拒绝。
- 模型未安装不下载；ready() 由核心注入探测（默认只做配置完整性 + 探活）。
"""
from __future__ import annotations

import base64
import binascii
import io
import wave
from pathlib import Path
from typing import Callable, Optional

from ..audio_contracts import (
    AudioJobRequest, Capability, ErrorCode, register_provider, reject,
)
from ..voice_service import validate_wav
from ._http import classify_status, request_bytes, validate_loopback

MODES = ('custom_voice', 'voice_clone', 'voice_design')
_ENDPOINTS = {
    'custom_voice': '/qwenapi/v1/custom-voice',
    'voice_clone': '/qwenapi/v1/voice-clone',
    'voice_design': '/qwenapi/v1/voice-design',
}
_MAX_SEGMENTS = 8


def _cfg(config, key: str, default=None):
    try:
        return config.get(key, default)
    except TypeError:
        return config.get(key) if key in config else default


def _join_wav(segments: list[bytes]) -> bytes:
    """把同参数的多段 WAV 按顺序拼成一条（保留采样率/声道）。"""
    if len(segments) == 1:
        return segments[0]
    first = wave.open(io.BytesIO(segments[0]), 'rb')
    try:
        rate, channels, width = first.getframerate(), first.getnchannels(), first.getsampwidth()
        frames = [first.readframes(first.getnframes())]
    finally:
        first.close()
    for segment in segments[1:]:
        with wave.open(io.BytesIO(segment), 'rb') as stream:
            if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (
                    rate, channels, width):
                reject(ErrorCode.ASSET_INVALID, '多段音频参数不一致，无法拼接')
            frames.append(stream.readframes(stream.getnframes()))
    raw = b''.join(frames)
    if len(raw) > 8 * 1024 * 1024:
        reject(ErrorCode.ASSET_INVALID, '拼接后的音频超过 8MB 上限')
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(raw)
    return buffer.getvalue()


def _overrides(profile, name: str) -> dict:
    mapping = (profile or {}).get('provider_map') or {}
    entry = mapping.get(name) if isinstance(mapping, dict) else None
    return entry if isinstance(entry, dict) else {}


@register_provider
class QwenWebUIProvider:
    """Qwen WebUI 三接口适配器（custom-voice / voice-clone / voice-design）。"""
    name = 'qwen_webui'
    capability = Capability.TTS
    supports_cloud = False
    _DEFAULT = 'http://127.0.0.1:7860'

    def __init__(self, config, http_transport=None, probe: Optional[Callable[[], bool]] = None,
                 http_client=None):
        self.config = config
        self.http_transport = http_transport
        self.http_client = http_client
        self.probe = probe

    # ── 配置/能力 ──────────────────────────────────────────────────────────
    def _base(self) -> str:
        base = str(_cfg(self.config, 'qq_only.voice.qwen.endpoint', self._DEFAULT)).rstrip('/')
        return validate_loopback(base, name='Qwen WebUI')

    def _endpoint(self, suffix: str) -> str:
        return self._base() + suffix

    def _mode(self, profile: Optional[dict] = None) -> str:
        entry = _overrides(profile, self.name)
        candidate = str(entry.get('mode')) if entry.get('mode') else ''
        candidate = candidate or str(_cfg(self.config, 'qq_only.voice.qwen.mode', 'custom_voice'))
        if candidate not in MODES:
            reject(ErrorCode.INVALID_INPUT, 'Qwen 模式须为 ' + '/'.join(MODES), mode=candidate)
        return candidate

    def _model(self, profile: Optional[dict] = None) -> str:
        override = _overrides(profile, self.name)
        model = str(override.get('model')
                    or _cfg(self.config, 'qq_only.voice.qwen.model', '')).strip()
        if not model:
            reject(ErrorCode.MODEL_NOT_READY, '尚未选择已准备的 Qwen 模型（不会自动下载）')
        return model

    def available(self) -> bool:
        try:
            self._base()
            self._model()
        except Exception:
            return False
        return True

    def ready(self) -> bool:
        if not self.available():
            return False
        if self.probe is not None:
            try:
                return bool(self.probe())
            except Exception:
                return False
        return True

    # ── 合成 ────────────────────────────────────────────────────────────────
    async def synthesize(self, request: AudioJobRequest, profile: dict) -> bytes:
        mode = self._mode(profile)
        payload = self._payload(request, profile, mode)
        result = await request_bytes('POST', self._endpoint(_ENDPOINTS[mode]),
                                     payload=payload, transport=self.http_transport,
                                     timeout=120.0, client=self.http_client)
        classify_status(result.status, result.content, name='Qwen WebUI')
        return self._decode(result.content)

    def _payload(self, request: AudioJobRequest, profile: dict, mode: str) -> dict:
        text = str(request.text or '').strip()
        if not 1 <= len(text) <= 300:
            reject(ErrorCode.INVALID_INPUT, '待合成文字须为 1–300 个字符')
        language = str((profile or {}).get('language') or request.language or 'zh')
        override = _overrides(profile, self.name)
        model = self._model(profile)
        if mode == 'custom_voice':
            speaker = str(override.get('speaker')
                          or _cfg(self.config, 'qq_only.voice.qwen.speaker', '')).strip()
            if not speaker:
                reject(ErrorCode.MODEL_NOT_READY, 'custom_voice 需要已准备的说话人（speaker）')
            return {'model_name': model, 'text': text, 'speaker': speaker,
                    'language': language,
                    'instruct': str(override.get('instruct')
                                    or _cfg(self.config, 'qq_only.voice.qwen.instruct', '')),
                    'segment_gen': False}
        if mode == 'voice_clone':
            reference = self._reference(profile)
            return {'model_name': model, 'text': text,
                    'ref_audio_base64': base64.b64encode(reference).decode('ascii'),
                    'ref_text': str((profile or {}).get('ref_text') or override.get('ref_text')
                                    or _cfg(self.config, 'qq_only.voice.qwen.ref_text', '')),
                    'language': language, 'segment_gen': False}
        return {'model_name': model, 'text': text,
                'instruct': str(override.get('instruct')
                                or _cfg(self.config, 'qq_only.voice.qwen.instruct', '')).strip(),
                'language': language, 'segment_gen': False}

    def _reference(self, profile: dict) -> bytes:
        """参考音频只从“已导入资产”或本机授权文件读，不接受 URL。"""
        raw = (profile or {}).get('ref_audio_bytes')
        if isinstance(raw, (bytes, bytearray)) and raw:
            data = bytes(raw)
        else:
            override = _overrides(profile, self.name)
            path_text = str(override.get('ref_audio_path')
                            or _cfg(self.config, 'qq_only.voice.qwen.ref_audio_path', '')).strip()
            if not path_text:
                reject(ErrorCode.ASSET_INVALID, 'voice_clone 需要已导入的参考音频')
            path = Path(path_text)
            if not path.is_file():
                reject(ErrorCode.ASSET_INVALID, '参考音频不存在或不可读', ref=path.name)
            if path.suffix.lower() not in {'.wav', '.flac', '.mp3'}:
                reject(ErrorCode.ASSET_INVALID, '参考音频格式须为 wav/flac/mp3')
            data = path.read_bytes()
        if not 44 <= len(data) <= 8 * 1024 * 1024:
            reject(ErrorCode.ASSET_INVALID, '参考音频大小须在 44B–8MB')
        return data

    @staticmethod
    def _decode(content: bytes) -> bytes:
        """解析 ``audio_files_base64``；空数组/非音频/超限都明确报错。"""
        import json
        try:
            payload = json.loads(content.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            # 直接返回音频字节的部署也兼容（但必须是有效 WAV）。
            try:
                validate_wav(content)
                return content
            except ValueError:
                reject(ErrorCode.ASSET_INVALID, 'Qwen 返回既非 JSON 也非有效 WAV')
        if isinstance(payload, dict) and payload.get('success') is False:
            message = str(payload.get('message') or payload.get('error') or '未知错误')
            if '未找到' in message or 'not found' in message.lower():
                reject(ErrorCode.MODEL_NOT_READY, 'Qwen 模型未就绪：' + message[:120])
            reject(ErrorCode.PROVIDER_UNAVAILABLE, 'Qwen 合成失败：' + message[:120])
        encoded = None
        if isinstance(payload, dict):
            for key in ('audio_files_base64', 'audio_files', 'audio_base64'):
                if key in payload:
                    encoded = payload[key]
                    break
        if encoded is None:
            reject(ErrorCode.PROVIDER_UNAVAILABLE, 'Qwen 响应缺少音频字段')
        if isinstance(encoded, str):
            encoded = [encoded]
        if not isinstance(encoded, list):
            reject(ErrorCode.ASSET_INVALID, 'Qwen 音频字段格式不正确')
        if not encoded:
            reject(ErrorCode.PROVIDER_UNAVAILABLE, 'Qwen 没有返回任何音频（空数组）')
        if len(encoded) > _MAX_SEGMENTS:
            reject(ErrorCode.ASSET_INVALID, f'Qwen 返回分段过多（>{_MAX_SEGMENTS}）')
        segments = []
        for item in encoded:
            if not isinstance(item, str) or not item:
                reject(ErrorCode.ASSET_INVALID, 'Qwen 音频分段为空或不是 base64 字符串')
            try:
                audio = base64.b64decode(item, validate=True)
            except (binascii.Error, ValueError):
                reject(ErrorCode.ASSET_INVALID, 'Qwen 音频分段不是合法 base64')
            try:
                validate_wav(audio)
            except ValueError as exc:
                reject(ErrorCode.ASSET_INVALID, 'Qwen 返回的非音频内容：' + str(exc))
            segments.append(audio)
        return _join_wav(segments)
