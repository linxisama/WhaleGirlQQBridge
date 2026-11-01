"""T11：本地 SVC 歌声换声（S3，RVC/SVC）+ 人声分离前置（可关闭）。

管线（1.3.4）：授权清唱/音轨 → 可选人声分离 → F0/内容特征 → RVC/SVC 推理
→ 与伴奏对齐混音。源音频提供旋律和演唱，转换模型提供音色。

边界（T11 验收）：
- 转换后旋律、字序、节奏保留，音色经试听认可；
- 伴奏与人声无明显漂移/削波；保存母版；
- **不**把 GPT-SoVITS TTS 权重当 RVC 权重用。

实现补充（2026-10-20）：推理由核心注入的 ``runner`` 执行；本类负责输入校验、
“TTS 权重冒充 RVC”拦截、结果 WAV/时长核对与混音。混音是纯 PCM 实现（无 FFmpeg）。
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional, Union

from ..audio_contracts import AudioJobRequest, Capability, ErrorCode, reject
from ..audio_postprocess import read_wav
from ..voice_service import validate_wav
from .mix import MixOptions, mix_pcm

Runner = Callable[..., Union[bytes, Awaitable[bytes]]]

_HEX64 = re.compile(r'^[a-f0-9]{64}$')
_MODEL_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
# 这些名字代表“文本转语音”权重，不能拿来当歌声转换模型。
_TTS_MARKERS = ('gpt_sovits', 'gpt-sovits', 'gpt_sovits_api', 'tts_api')


class LocalSvcEngine:
    capability = Capability.VOICE_CONVERT

    def __init__(self, config, runner: Optional[Runner] = None, target_model=None):
        self.config = config
        self.runner = runner
        self.target_model = target_model   # 目标音色模型（已登记，非任意路径）

    def available(self) -> bool:
        return self.runner is not None and bool(self.target_model)

    def _model_id(self) -> str:
        raw = self.target_model
        if isinstance(raw, dict):
            raw = raw.get('id') or raw.get('model_id') or ''
        model = str(raw or '').strip()
        if not model:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED, 'S3 缺少目标音色模型')
        if not _MODEL_ID.fullmatch(model):
            reject(ErrorCode.INVALID_INPUT, '目标音色须为已登记模型 ID，不接受文件路径')
        lowered = model.lower()
        if any(marker in lowered for marker in _TTS_MARKERS):
            reject(ErrorCode.POLICY_DENIED,
                   'GPT-SoVITS 等 TTS 权重不能当作 RVC/SVC 歌声模型使用')
        if isinstance(self.target_model, dict):
            kind = str(self.target_model.get('kind') or '').lower()
            if kind and kind not in {'rvc', 'svc'}:
                reject(ErrorCode.POLICY_DENIED,
                       '目标模型类型不是 RVC/SVC，拒绝用于歌声转换', kind=kind)
        return model

    def requires(self, job: AudioJobRequest) -> None:
        """源演唱（source_audio）+ 目标音色（target_voice）齐备且已授权。"""
        if not job.source_audio:
            reject(ErrorCode.INVALID_INPUT, 'S3 需要源演唱（source_audio）')
        if not job.target_voice:
            reject(ErrorCode.INVALID_INPUT, 'S3 需要目标音色（target_voice）')
        source = str(job.source_audio).strip()
        if not _HEX64.match(source):
            reject(ErrorCode.INVALID_INPUT, '源演唱须为已导入资产的内容哈希')
        if not self.available():
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   'S3 本地 SVC 未配置（缺目标音色模型）')
        self._model_id()

    async def convert(self, job: AudioJobRequest, vocals: bytes,
                      instrumental: Optional[bytes] = None,
                      options: Optional[MixOptions] = None) -> bytes:
        """歌声转换 + 与伴奏对齐混音，返回母版（高质量，不降采样）。"""
        result = await self.convert_with_meta(job, vocals, instrumental, options)
        return result['audio']

    async def convert_with_meta(self, job: AudioJobRequest, vocals: bytes,
                                instrumental: Optional[bytes] = None,
                                options: Optional[MixOptions] = None) -> dict:
        self.requires(job)
        try:
            duration = validate_wav(vocals)
        except ValueError as exc:
            reject(ErrorCode.ASSET_INVALID, '源演唱不是有效 WAV：' + str(exc))
        if instrumental is not None:
            try:
                validate_wav(instrumental)
            except ValueError as exc:
                reject(ErrorCode.ASSET_INVALID, '伴奏不是有效 WAV：' + str(exc))
        model = self._model_id()
        options = options or MixOptions()
        try:
            converted = self.runner(job, vocals, instrumental, model, options)
            if hasattr(converted, '__await__'):
                converted = await converted
        except Exception as exc:
            if getattr(exc, 'code', None) is not None:
                raise
            reject(ErrorCode.PROVIDER_UNAVAILABLE, 'S3 歌声转换失败',
                   error=type(exc).__name__)
        if not isinstance(converted, (bytes, bytearray)) or not converted:
            reject(ErrorCode.ASSET_INVALID, 'S3 转换没有返回音频')
        converted = bytes(converted)
        try:
            converted_duration = validate_wav(converted)
        except ValueError as exc:
            reject(ErrorCode.ASSET_INVALID, 'S3 转换结果不是有效 WAV：' + str(exc))
        if abs(converted_duration - duration) > max(0.5, duration * 0.05):
            reject(ErrorCode.TRANSCODE_FAILED,
                   '转换后时长与源演唱差异过大，疑似节奏漂移',
                   before=round(duration, 3), after=round(converted_duration, 3))
        info = read_wav(converted)
        result = {'audio': converted, 'duration_seconds': round(converted_duration, 3),
                  'sample_rate': info['sample_rate'], 'channels': info['channels'],
                  'model': model, 'source_audio': str(job.source_audio),
                  'vocal_separation': bool(options.allow_vocal_separation),
                  'verified': False,
                  'note': '旋律/字序/节奏保留与音色需试听验收'}
        if instrumental:
            master, stats = mix_pcm(converted, instrumental, options)
            result['audio'] = master
            result['mix'] = stats
            result['duration_seconds'] = stats['duration_seconds']
            try:
                result['verified_duration'] = validate_wav(master)
            except ValueError as exc:
                reject(ErrorCode.ASSET_INVALID, '混音母版不是有效 WAV：' + str(exc))
        return result

    def mix(self, vocals: bytes, instrumental: bytes,
            options: Optional[MixOptions] = None) -> bytes:
        """增益/峰值处理，保存母版；漂移/削波要报 TRANSCODE_FAILED。"""
        options = options or MixOptions()
        master, _stats = mix_pcm(vocals, instrumental, options)
        return master
