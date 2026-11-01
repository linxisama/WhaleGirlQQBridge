"""T05：已有 audio.cpp / GPT-SoVITS 的适配器抽取。

保留旧参数和声音行为（见 voice_service._request_parameters），
把“健康/就绪探测 + 分类错误”抽成独立适配器，供 v2 网关路由。

实现边界：
- 本适配器只合成（返回 bytes）；格式校验/入库/转码/发送不在这里。
- GPT 默认行为保持“不缓存、每次采样”（voice_service 现状，见 test_audio_cpp_voice）；
  短语音缓存属于网关（300s/64 条），适配器不私自缓存，避免两个缓存互相打架。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from ..audio_contracts import (
    AudioJobRequest, Capability, ErrorCode, register_provider, reject,
)
from ..voice_service import validate_wav
from ._http import classify_status, request_bytes, validate_loopback

_VOICE_RE = re.compile(r'[A-Za-z0-9_-]{1,100}')
_LANG_RE = re.compile(r'[A-Za-z-]{2,16}')


def _cfg(config, key: str, default=None):
    try:
        return config.get(key, default)
    except TypeError:
        return config.get(key) if key in config else default


def _validate_audio(audio: bytes, *, name: str) -> bytes:
    try:
        validate_wav(audio)
    except ValueError as exc:
        reject(ErrorCode.ASSET_INVALID, f'{name} 返回的不是有效 WAV：{exc}')
    return audio


def _overrides(profile, name: str) -> dict:
    mapping = (profile or {}).get('provider_map') or {}
    entry = mapping.get(name) if isinstance(mapping, dict) else None
    return entry if isinstance(entry, dict) else {}


@register_provider
class AudioCppProvider:
    """audio.cpp /v1/audio/speech —— 快速短句候选（1.3.2）。"""
    name = 'audio_cpp'
    capability = Capability.TTS
    supports_cloud = False
    _DEFAULT = 'http://127.0.0.1:18100/v1/audio/speech'

    def __init__(self, config, http_transport=None, probe: Optional[Callable[[], bool]] = None,
                 http_client=None):
        self.config = config
        self.http_transport = http_transport
        self.http_client = http_client
        # readiness 探测由核心注入（固定短句试合成或 /health）；列表存在 ≠ 已加载。
        self.probe = probe

    def available(self) -> bool:
        """进程可达（不是“模型已加载”——那要 ready()，见 3.2.2 四个检查点）。"""
        try:
            self._endpoint()
            self._payload('准备', {})
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

    async def synthesize(self, request: AudioJobRequest, profile: dict) -> bytes:
        payload = self._payload((request.text or '').strip(), profile)
        endpoint = self._endpoint()
        result = await request_bytes('POST', endpoint, payload=payload,
                                     transport=self.http_transport, timeout=60.0,
                                     client=self.http_client)
        classify_status(result.status, result.content, name='audio.cpp')
        return _validate_audio(result.content, name='audio.cpp')

    def _endpoint(self) -> str:
        endpoint = str(_cfg(self.config, 'qq_only.voice.audio_cpp.endpoint', self._DEFAULT))
        return validate_loopback(endpoint, name='audio.cpp', path='/v1/audio/speech')

    def _payload(self, text: str, profile: Optional[dict] = None) -> dict:
        if not 1 <= len(text) <= 300:
            reject(ErrorCode.INVALID_INPUT, '待合成文字须为 1–300 个字符')
        override = _overrides(profile, self.name)
        model = str(override.get('model')
                    or _cfg(self.config, 'qq_only.voice.audio_cpp.model', 'breeze-tts-2')).strip()
        voice = str(override.get('voice')
                    or _cfg(self.config, 'qq_only.voice.audio_cpp.voice', 'demo_3_woman')).strip()
        if not model or not _VOICE_RE.fullmatch(voice):
            reject(ErrorCode.INVALID_INPUT,
                   '请配置有效模型名称和音色库名称（不接受文件路径）')
        return {'model': model, 'input': text, 'voice': voice, 'response_format': 'wav'}


@register_provider
class GptSovitsProvider:
    """GPT-SoVITS /tts —— 已有角色音色的对白（1.3.3）。非 MIDI 歌声接口。"""
    name = 'gpt_sovits_api_v2'
    capability = Capability.CHARACTER_TTS
    supports_cloud = False
    _DEFAULT = 'http://127.0.0.1:9880/tts'

    def __init__(self, config, http_transport=None, probe: Optional[Callable[[], bool]] = None,
                 http_client=None):
        self.config = config
        self.http_transport = http_transport
        self.http_client = http_client
        self.probe = probe

    def available(self) -> bool:
        try:
            self._payload('准备', {})
            self._endpoint()
        except Exception:
            return False
        return True

    def ready(self) -> bool:
        """权重版本匹配 + 参考音频可被“服务进程”读取（不是机器人路径）。"""
        if not self.available():
            return False
        if self.probe is not None:
            try:
                return bool(self.probe())
            except Exception:
                return False
        return True

    async def synthesize(self, request: AudioJobRequest, profile: dict) -> bytes:
        payload = self._payload((request.text or '').strip(), profile)
        endpoint = self._endpoint()
        result = await request_bytes('POST', endpoint, payload=payload,
                                     transport=self.http_transport, timeout=60.0,
                                     client=self.http_client)
        classify_status(result.status, result.content, name='GPT-SoVITS')
        return _validate_audio(result.content, name='GPT-SoVITS')

    def _endpoint(self) -> str:
        endpoint = str(_cfg(self.config, 'qq_only.voice.gpt_sovits.endpoint', self._DEFAULT))
        return validate_loopback(endpoint, name='GPT-SoVITS', path='/tts')

    def _payload(self, text: str, profile: Optional[dict] = None) -> dict:
        if not 1 <= len(text) <= 300:
            reject(ErrorCode.INVALID_INPUT, '待合成文字须为 1–300 个字符')
        override = _overrides(profile, self.name)
        reference = str(override.get('ref_audio_path')
                        or _cfg(self.config, 'qq_only.voice.gpt_sovits.ref_audio_path', '')).strip()
        if not reference:
            reject(ErrorCode.ASSET_INVALID, '请先配置 GPT-SoVITS 的参考音频路径')
        if bool(_cfg(self.config, 'qq_only.voice.gpt_sovits.check_ref_local', False)):
            # 参考音频在“服务进程”里解析；本机存在性检查是可选项，默认不拦跨进程部署。
            path = Path(reference)
            if not path.is_file() or path.stat().st_size <= 0:
                reject(ErrorCode.ASSET_INVALID, '参考音频在本机不可读或为空',
                       ref=path.name)
            if path.suffix.lower() not in {'.wav', '.flac', '.mp3'}:
                reject(ErrorCode.ASSET_INVALID, '参考音频格式须为 wav/flac/mp3')
        text_lang = str(override.get('text_lang')
                        or _cfg(self.config, 'qq_only.voice.gpt_sovits.text_lang', 'zh')).strip()
        prompt_lang = str(override.get('prompt_lang')
                          or _cfg(self.config, 'qq_only.voice.gpt_sovits.prompt_lang', 'zh')).strip()
        for label, value in (('text_lang', text_lang), ('prompt_lang', prompt_lang)):
            if not _LANG_RE.fullmatch(value):
                reject(ErrorCode.INVALID_INPUT, f'{label} 须为 2–16 位语言代码',
                       value=value)
        return {'text': text, 'text_lang': text_lang, 'ref_audio_path': reference,
                'prompt_lang': prompt_lang,
                'prompt_text': str(override.get('prompt_text')
                                   or _cfg(self.config, 'qq_only.voice.gpt_sovits.prompt_text', '')),
                'media_type': 'wav', 'streaming_mode': False}
