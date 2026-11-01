"""T11：唱歌混音（母版质量）。

母版保留原采样率/双声道；QQ 语音用**独立的单声道衍生文件**（1.3.4）。
QQ 交付基准建议 PCM 16-bit / 24 kHz / 单声道（1.6.2），但母版不能为迁就
语音限制而降质覆盖。

实现补充（2026-10-20）：纯 Python 16-bit PCM 混音（不依赖 FFmpeg），
增益/峰值/长度对齐都有明确口径；超出漂移或削波阈值直接报 TRANSCODE_FAILED，
不把“听起来还行”当成验收通过。
"""
from __future__ import annotations

import io
import struct
import wave
from dataclasses import dataclass

from ..audio_contracts import ErrorCode, reject
from ..audio_postprocess import read_wav


@dataclass
class MixOptions:
    """混音/衍生参数；默认值都是“保守但可验收”的起点。"""
    master_sample_rate: int = 0        # 0 = 保持母版原采样率
    master_channels: int = 2
    master_bits: int = 16
    # 衍生（QQ 语音）规格
    derived_sample_rate: int = 24000
    derived_channels: int = 1
    derived_bits: int = 16
    vocal_gain_db: float = 0.0
    instrumental_gain_db: float = -6.0
    limit_peak: bool = True           # 峰值处理，防削波
    allow_vocal_separation: bool = False  # 人声分离默认关（T11 可选项）
    max_drift_seconds: float = 0.25   # 人声/伴奏对齐容差
    max_clipped_ratio: float = 0.01   # 允许被限幅的样本比例上限


def derived_spec(options: MixOptions) -> dict:
    """QQ 语音衍生文件的规范（供 audio_postprocess / delivery 用）。

    不是平台硬性格式，仍需 T02 实机确认（1.6.2）。
    """
    return {'sample_rate': options.derived_sample_rate, 'channels': options.derived_channels,
            'bits_per_sample': options.derived_bits, 'media_type': 'audio/wav'}


def _decode(data: bytes, label: str) -> tuple[list[float], int, int]:
    try:
        info = read_wav(data)
    except Exception as exc:
        code = getattr(exc, 'code', None)
        if code is not None:
            reject(code, f'{label} 不是有效 WAV')
        reject(ErrorCode.ASSET_INVALID, f'{label} 不是有效 WAV')
    samples = struct.unpack('<%dh' % (len(info['raw']) // 2), info['raw'])
    return list(samples), info['sample_rate'], info['channels']


def _to_channels(samples: list[float], src: int, dst: int) -> list[float]:
    if src == dst:
        return samples
    frames = len(samples) // src
    if dst == 1 and src == 2:
        return [(samples[i * 2] + samples[i * 2 + 1]) / 2.0 for i in range(frames)]
    if dst == 2 and src == 1:
        out = [0.0] * (frames * 2)
        for i, value in enumerate(samples):
            out[i * 2] = out[i * 2 + 1] = value
        return out
    reject(ErrorCode.TRANSCODE_FAILED, f'不支持的声道转换：{src} → {dst}')


def _encode(samples: list[int], rate: int, channels: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack('<%dh' % len(samples), *samples))
    return buffer.getvalue()


def mix_pcm(vocals: bytes, instrumental: bytes, options: MixOptions) -> tuple[bytes, dict]:
    """按增益混合人声与伴奏，返回 (母版 WAV, 统计)。"""
    vocal_samples, vocal_rate, vocal_channels = _decode(vocals, '人声')
    inst_samples, inst_rate, inst_channels = _decode(instrumental, '伴奏')
    if vocal_rate != inst_rate:
        reject(ErrorCode.TRANSCODE_FAILED, '人声与伴奏采样率不一致，不能直接混音')
    rate = int(options.master_sample_rate or vocal_rate)
    if rate != vocal_rate:
        reject(ErrorCode.TRANSCODE_FAILED,
               '请求的母版采样率与素材不一致（需先重采样，不能静默改变母版）')
    target_channels = int(options.master_channels or max(vocal_channels, inst_channels))
    vocal_samples = _to_channels(vocal_samples, vocal_channels, target_channels)
    inst_samples = _to_channels(inst_samples, inst_channels, target_channels)
    vocal_frames = len(vocal_samples) // target_channels
    inst_frames = len(inst_samples) // target_channels
    drift = abs(vocal_frames - inst_frames) / float(rate)
    if drift > float(options.max_drift_seconds):
        reject(ErrorCode.TRANSCODE_FAILED,
               f'人声与伴奏长度相差 {drift:.3f}s，超过对齐容差',
               drift_seconds=round(drift, 3))
    frames = max(vocal_frames, inst_frames)
    vocal_samples += [0.0] * ((frames - vocal_frames) * target_channels)
    inst_samples += [0.0] * ((frames - inst_frames) * target_channels)
    vocal_gain = 10 ** (float(options.vocal_gain_db) / 20.0)
    inst_gain = 10 ** (float(options.instrumental_gain_db) / 20.0)
    mixed = [vocal_samples[i] * vocal_gain + inst_samples[i] * inst_gain
             for i in range(len(vocal_samples))]
    peak_before = max((abs(value) for value in mixed), default=0.0)
    clipped = 0
    if peak_before > 32767:
        if not options.limit_peak:
            reject(ErrorCode.TRANSCODE_FAILED, '混音峰值超过 16-bit 上限且未启用限幅')
        ratio = 32767.0 / peak_before
        mixed = [value * ratio for value in mixed]
        clipped = sum(1 for value in mixed if abs(value) >= 32766)
    if frames and clipped / frames > float(options.max_clipped_ratio):
        reject(ErrorCode.TRANSCODE_FAILED,
               f'混音削波比例过高（{clipped / frames:.1%}），请降低增益',
               clipped_ratio=round(clipped / frames, 4))
    quantized = [max(-32768, min(32767, int(round(value)))) for value in mixed]
    master = _encode(quantized, rate, target_channels)
    stats = {'sample_rate': rate, 'channels': target_channels,
             'duration_seconds': round(frames / float(rate), 3),
             'peak_before': round(peak_before, 1),
             'clipped_samples': clipped,
             'drift_seconds': round(drift, 4),
             'vocals_gain_db': float(options.vocal_gain_db),
             'instrumental_gain_db': float(options.instrumental_gain_db)}
    return master, stats


def silence_wav(seconds: float, *, rate: int = 24000, channels: int = 1) -> bytes:
    frames = max(1, int(rate * float(seconds)))
    return _encode([0] * (frames * channels), rate, channels)
