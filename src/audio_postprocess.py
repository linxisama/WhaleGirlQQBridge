"""T03：音频解码/转码（固定版本 FFmpeg 子进程，参数数组调用，**不**经 shell 拼接）。

用途：
- 转码为项目基准（PCM 16-bit / 24 kHz / 单声道，供 QQ 语音；母版另存）。
- 时长/大小/格式核对（“不是任意 WAV 都能进语音通道”，见 1.6.2）。
- 转换超时要能终止子进程（T03 验收）。

注意：FFmpeg 是“解码/转换工具”，不提供唱歌能力；把歌词当文本拉长音节/升降调
不在此列（1.1）。

实现补充（2026-10-20）：
- 所有调用都用参数数组，绝不经过 shell；子进程 stdout/stderr 被捕获，超时由
  ``subprocess.run(timeout=...)`` 终止子进程后转成 ``TRANSCODE_FAILED``。
- 本机没有 FFmpeg 时，对 16-bit PCM WAV 提供等效的纯 Python 转换
  （降混/升混/线性重采样），其它格式仍然明确报错——绝不假装转换成功。
"""
from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import Callable, Optional

from .audio_contracts import ErrorCode, reject

MAX_AUDIO_BYTES = 8 * 1024 * 1024

# 允许的衍生规格（与计划书 1.6.2 的 QQ 语音基准一致）。
WAV_SPECS = {
    (16000, 1, 16), (22050, 1, 16), (24000, 1, 16), (24000, 2, 16),
    (44100, 1, 16), (44100, 2, 16), (48000, 1, 16), (48000, 2, 16),
}


def _no_window_flags() -> int:
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def read_wav(data: bytes) -> dict:
    """解析 PCM WAV 头与帧，返回 {sample_rate, channels, bits, frames, duration}。

    只接受未压缩 PCM（config 1）；损坏/截断/非 WAV 一律 ASSET_INVALID，
    不把“猜出来的参数”当成可用音频。
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        reject(ErrorCode.ASSET_INVALID, '音频为空')
    if len(data) > MAX_AUDIO_BYTES:
        reject(ErrorCode.ASSET_INVALID, '音频超过 8MB 上限')
    try:
        with wave.open(io.BytesIO(bytes(data)), 'rb') as stream:
            channels = stream.getnchannels()
            rate = stream.getframerate()
            width = stream.getsampwidth()
            frames = stream.getnframes()
            raw = stream.readframes(frames)
    except (wave.Error, EOFError, ZeroDivisionError) as exc:
        reject(ErrorCode.ASSET_INVALID, '音频不是支持的 PCM WAV 格式', error=str(exc))
    expected = frames * channels * width
    if len(raw) != expected:
        reject(ErrorCode.ASSET_INVALID, 'WAV 内容不完整')
    if channels not in (1, 2) or width != 2 or rate <= 0:
        reject(ErrorCode.ASSET_INVALID, '仅支持 16-bit 单/双声道 PCM WAV')
    duration = frames / float(rate)
    if not 0 < duration <= 120:
        reject(ErrorCode.ASSET_INVALID, '音频时长须为 0–120 秒')
    return {'sample_rate': rate, 'channels': channels, 'bits': width * 8,
            'frames': frames, 'duration': duration, 'raw': raw}


def _resample(raw: bytes, src_rate: int, dst_rate: int, channels: int) -> bytes:
    """线性插值重采样（16-bit PCM）。仅在无 FFmpeg 时用于小段语音。"""
    if src_rate == dst_rate:
        return raw
    src_frames = len(raw) // (2 * channels)
    if src_frames <= 0:
        return b''
    src = struct.unpack('<%dh' % (len(raw) // 2), raw)
    dst_frames = max(1, int(round(src_frames * dst_rate / float(src_rate))))
    out = [0] * (dst_frames * channels)
    ratio = src_rate / float(dst_rate)
    for i in range(dst_frames):
        pos = i * ratio
        left = int(math.floor(pos))
        frac = pos - left
        right = min(left + 1, src_frames - 1)
        for ch in range(channels):
            a = src[left * channels + ch]
            b = src[right * channels + ch]
            out[i * channels + ch] = int(round(a + (b - a) * frac))
    return struct.pack('<%dh' % len(out), *out)


def convert_wav(data: bytes, *, sample_rate: int = 24000, channels: int = 1,
                bits: int = 16) -> bytes:
    """纯 Python 的 PCM WAV 转换（16-bit）。供无 FFmpeg 环境与单测使用。"""
    info = read_wav(data)
    if int(bits) != 16:
        reject(ErrorCode.TRANSCODE_FAILED, '纯离线转换仅支持 16-bit（需 FFmpeg 处理其它位深）')
    if int(channels) not in (1, 2):
        reject(ErrorCode.TRANSCODE_FAILED, '目标声道数须为 1 或 2')
    if not 8000 <= int(sample_rate) <= 48000:
        reject(ErrorCode.TRANSCODE_FAILED, '目标采样率须在 8000–48000 Hz')
    raw = info['raw']
    src_channels = info['channels']
    if src_channels != int(channels):
        samples = struct.unpack('<%dh' % (len(raw) // 2), raw)
        count = len(samples) // src_channels
        if channels == 1 and src_channels == 2:
            mono = [int(round((samples[i * 2] + samples[i * 2 + 1]) / 2.0)) for i in range(count)]
            raw = struct.pack('<%dh' % len(mono), *mono)
        elif channels == 2 and src_channels == 1:
            stereo = [0] * (count * 2)
            for i, value in enumerate(samples):
                stereo[i * 2] = stereo[i * 2 + 1] = value
            raw = struct.pack('<%dh' % len(stereo), *stereo)
        else:
            reject(ErrorCode.TRANSCODE_FAILED, '不支持的声道转换')
    raw = _resample(raw, info['sample_rate'], int(sample_rate), int(channels))
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(int(channels))
        stream.setsampwidth(2)
        stream.setframerate(int(sample_rate))
        stream.writeframes(raw)
    return buffer.getvalue()


class Transcoder:
    """FFmpeg 子进程封装。路径与参数全用数组，绝不拼 shell 字符串。"""

    def __init__(self, ffmpeg: str = 'ffmpeg', timeout: float = 30.0,
                 runner: Optional[Callable] = None):
        self.ffmpeg = ffmpeg
        self.timeout = float(timeout)
        # runner(cmd, timeout) -> CompletedProcess；测试注入用，生产走 subprocess.run。
        self.runner = runner

    # ── 能力 ────────────────────────────────────────────────────────────────
    def available(self) -> bool:
        """FFmpeg 可执行文件是否存在（不启动、不改系统）。"""
        candidate = str(self.ffmpeg)
        if Path(candidate).is_file():
            return True
        return shutil.which(candidate) is not None

    def _command(self, src: Path, dst: Path, *, sample_rate: int, channels: int,
                 bits: int, media_type: str) -> list[str]:
        audio_codec = {'wav': 'pcm_s16le', 'mp3': 'libmp3lame'}.get(media_type)
        if audio_codec is None:
            reject(ErrorCode.TRANSCODE_FAILED, f'不支持的目标格式：{media_type}')
        command = [str(self.ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin',
                   '-y', '-i', str(src), '-ar', str(int(sample_rate)),
                   '-ac', str(int(channels)), '-c:a', audio_codec]
        if media_type == 'wav':
            command += ['-sample_fmt', 's16']
        command.append(str(dst))
        return command

    # ── 转换 ────────────────────────────────────────────────────────────────
    def transcode(self, src: Path, dst: Path, *, sample_rate: int = 24000,
                  channels: int = 1, bits: int = 16, media_type: str = 'wav') -> None:
        """src → dst（按 spec）。参数数组调用；超时终止子进程（T03 验收）。"""
        src, dst = Path(src), Path(dst)
        if not src.is_file():
            reject(ErrorCode.ASSET_INVALID, '源音频不存在或不可读', path=src.name)
        if src.resolve() == dst.resolve():
            reject(ErrorCode.TRANSCODE_FAILED, '目标文件不能覆盖母版')
        dst.parent.mkdir(parents=True, exist_ok=True)
        if media_type == 'wav' and not self.available() and self.runner is None:
            # 本机没有 FFmpeg：16-bit PCM WAV 用等效离线转换，其它格式明确拒绝。
            data = src.read_bytes()
            dst.write_bytes(convert_wav(data, sample_rate=sample_rate,
                                         channels=channels, bits=bits))
            return
        command = self._command(src, dst, sample_rate=sample_rate, channels=channels,
                                bits=bits, media_type=media_type)
        try:
            if self.runner is not None:
                self.runner(command, self.timeout)
            else:
                subprocess.run(command, timeout=self.timeout, check=True,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=_no_window_flags())
        except subprocess.TimeoutExpired:
            reject(ErrorCode.TRANSCODE_FAILED,
                   '转码超时，已终止子进程', seconds=self.timeout)
        except FileNotFoundError:
            reject(ErrorCode.TRANSCODE_FAILED, '未找到 FFmpeg，可执行文件路径无效')
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or b'').decode('utf-8', 'replace')[:300]
            reject(ErrorCode.TRANSCODE_FAILED, '转码失败', detail=message)
        except OSError as exc:
            reject(ErrorCode.TRANSCODE_FAILED, '无法启动转码进程', error=str(exc))
        if not dst.is_file() or dst.stat().st_size <= 0:
            reject(ErrorCode.TRANSCODE_FAILED, '转码没有生成有效文件')

    # ── 探测 ────────────────────────────────────────────────────────────────
    def probe(self, path: Path) -> dict:
        """返回 {sample_rate, channels, bits_per_sample, duration, size}，供验收核对。

        WAV 直接读头（不依赖外部程序）；其它格式先尝试 ffprobe，失败明确报错。
        """
        path = Path(path)
        if not path.is_file():
            reject(ErrorCode.ASSET_INVALID, '音频文件不存在', path=path.name)
        size = path.stat().st_size
        suffix = path.suffix.lower()
        if suffix == '.wav' or self._looks_like_wav(path):
            info = read_wav(path.read_bytes())
            return {'sample_rate': info['sample_rate'], 'channels': info['channels'],
                    'bits_per_sample': info['bits'], 'duration': round(info['duration'], 3),
                    'size': size, 'media_type': 'audio/wav'}
        return self._probe_external(path, size)

    def _looks_like_wav(self, path: Path) -> bool:
        try:
            with path.open('rb') as handle:
                head = handle.read(12)
            return head[:4] == b'RIFF' and head[8:12] == b'WAVE'
        except OSError:
            return False

    def _probe_external(self, path: Path, size: int) -> dict:
        tool = str(Path(self.ffmpeg).with_name('ffprobe')) if Path(self.ffmpeg).is_file() else 'ffprobe'
        if shutil.which(tool) is None:
            reject(ErrorCode.TRANSCODE_FAILED, '无法探测该格式（缺少 ffprobe）')
        command = [tool, '-v', 'error', '-select_streams', 'a:0',
                   '-show_entries', 'stream=sample_rate,channels,duration,bits_per_sample',
                   '-of', 'default=noprint_wrappers=1', str(path)]
        try:
            completed = subprocess.run(command, timeout=self.timeout, check=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       creationflags=_no_window_flags())
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
            reject(ErrorCode.TRANSCODE_FAILED, '探测失败：文件可能损坏或不是音频')
        values: dict[str, str] = {}
        for line in completed.stdout.decode('utf-8', 'replace').splitlines():
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip()
        try:
            rate = int(values.get('sample_rate') or 0)
            channels = int(values.get('channels') or 0)
            duration = float(values.get('duration') or 0)
            bits = int(values.get('bits_per_sample') or 16)
        except ValueError:
            reject(ErrorCode.TRANSCODE_FAILED, '探测结果无法解析')
        if rate <= 0 or channels <= 0:
            reject(ErrorCode.TRANSCODE_FAILED, '文件没有可用音轨')
        return {'sample_rate': rate, 'channels': channels, 'bits_per_sample': bits,
                'duration': round(duration, 3), 'size': size,
                'media_type': 'audio/' + path.suffix.lstrip('.').lower()}
