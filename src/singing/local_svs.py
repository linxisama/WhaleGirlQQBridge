"""T10：本地 SVS 指定旋律唱歌（S2，DiffSinger 类引擎 + 兼容歌声库）。

管线（1.3.4）：歌词+MIDI/工程 → 检查歌词与音符映射 → 歌词转音素/时值/F0
→ SVS 声学模型 + 声码器 → 清唱 WAV → 可选伴奏混音 → 资产入库。

验收口径（T10）：15–30 秒固定工程要有**真实歌声**；人工确认歌词、音高/节拍
与指定谱面相符；与输入音符做时序/F0 核对。歌词朗读或直接返回参考音频不算数。

独立 venv/服务（3.2），先固定样例工程与歌声库版本；缺素材不能关闭任务。

实现补充（2026-10-20）：
- 本类不内置任何声学模型，只做“谱面/输入校验 + 渲染契约 + 结果核对”；
  真正的推理由核心注入的 ``runner(job, singing_input) -> bytes`` 执行。
- 渲染结果必须是完整 WAV，时长落在固定工程窗口内；与参考音频字节相同会被拒绝
  （防止把参考音频/试听片段当“生成结果”回传）。
"""
from __future__ import annotations

import hashlib
import json
import re
import wave
import io
from typing import Awaitable, Callable, Optional, Union

from ..audio_contracts import AudioJobRequest, Capability, ErrorCode, reject
from ..voice_service import validate_wav
from .inputs import SingingInput

Runner = Callable[[AudioJobRequest, SingingInput], Union[bytes, Awaitable[bytes]]]

_HEX64 = re.compile(r'^[a-f0-9]{64}$')
_PROJECT_SUFFIXES = ('.ds', '.json', '.yaml', '.yml', '.dsproj')


class LocalSvsEngine:
    """S2 推理入口（由独立 SVS 服务执行；本类只做契约与参数组装）。"""
    capability = Capability.SING_SCORE

    def __init__(self, config, runner: Optional[Runner] = None, *,
                 reference_audio: Optional[bytes] = None):
        self.config = config
        self.runner = runner          # 核心注入的“固定工程渲染器”
        self.reference_audio = reference_audio

    def _cfg(self, key: str, default=None):
        try:
            return self.config.get(key, default)
        except (TypeError, AttributeError):
            return default

    def available(self) -> bool:
        # 歌声库 + 推理服务都就绪才算可用（缺一个就 False，不关闭任务、标未配置）。
        return self.runner is not None

    def _score_window(self) -> tuple[float, float]:
        try:
            low = float(self._cfg('qq_only.voice.svs.min_seconds', 15.0))
            high = float(self._cfg('qq_only.voice.svs.max_seconds', 30.0))
        except (TypeError, ValueError):
            low, high = 15.0, 30.0
        return max(0.5, low), max(low, high)

    def requires(self, job: AudioJobRequest) -> None:
        """校验谱面为“固定工程格式”，歌词与音符映射完整。"""
        if not job.score:
            reject(ErrorCode.INVALID_INPUT, 'S2 需要谱面（score）')
        score = str(job.score).strip()
        if not (_HEX64.match(score) or score.lower().endswith(_PROJECT_SUFFIXES)
                or score.startswith('{')):
            reject(ErrorCode.INVALID_INPUT,
                   '谱面须为已导入工程资产或受支持的固定工程文件', score=score[:80])
        if not job.lyrics or not str(job.lyrics).strip():
            reject(ErrorCode.INVALID_INPUT, 'S2 需要歌词（不能只给谱面）')
        try:
            _validate_project_shape(score)
        except ValueError as exc:
            reject(ErrorCode.INVALID_INPUT, '谱面工程格式不正确：' + str(exc))
        if not self.available():
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   'S2 本地 SVS 未配置（缺兼容歌声库或推理服务）')

    async def render(self, job: AudioJobRequest, singing_input: SingingInput) -> bytes:
        """歌词转音素/时值/F0 → 声学模型 → 声码器 → 清唱 WAV 的渲染入口。"""
        result = await self.render_with_meta(job, singing_input)
        return result['audio']

    async def render_with_meta(self, job: AudioJobRequest, singing_input: SingingInput) -> dict:
        self.requires(job)
        runner = self.runner
        try:
            audio = runner(job, singing_input)
            if hasattr(audio, '__await__'):
                audio = await audio
        except Exception as exc:
            # 引擎自身错误映射成可解释状态；不在这里吞掉堆栈细节，只回错误类型。
            code = getattr(exc, 'code', None)
            if code is not None:
                raise
            reject(ErrorCode.PROVIDER_UNAVAILABLE, 'S2 渲染失败', error=type(exc).__name__)
        if not isinstance(audio, (bytes, bytearray)) or not audio:
            reject(ErrorCode.ASSET_INVALID, 'S2 渲染没有返回音频')
        audio = bytes(audio)
        try:
            duration = validate_wav(audio)
        except ValueError as exc:
            reject(ErrorCode.ASSET_INVALID, 'S2 渲染结果不是有效 WAV：' + str(exc))
        low, high = self._score_window()
        if not low <= duration <= high:
            reject(ErrorCode.ASSET_INVALID,
                   f'S2 渲染时长 {duration:.1f}s 不在固定工程窗口 {low:.0f}–{high:.0f}s 内',
                   duration=round(duration, 2))
        if self.reference_audio and hashlib.sha256(audio).digest() == hashlib.sha256(
                self.reference_audio).digest():
            reject(ErrorCode.ASSET_INVALID,
                   'S2 返回了与参考音频完全相同的内容，不能当作唱歌结果')
        with wave.open(io.BytesIO(audio), 'rb') as stream:
            rate = stream.getframerate()
            channels = stream.getnchannels()
        return {'audio': audio, 'duration': round(duration, 3),
                'sample_rate': rate, 'channels': channels,
                'score_hash': hashlib.sha256(str(job.score).encode()).hexdigest(),
                'lyrics': singing_input.lyrics,
                'engine': 'local_svs',
                'verified': False,
                'note': '歌词/音高/节拍仍需人工与谱面对照验收'}


def _validate_project_shape(score: str) -> None:
    """内联工程（JSON）必须有音符与歌词映射；文件/资产编号交给渲染器。"""
    if not score.lstrip().startswith('{'):
        return
    try:
        project = json.loads(score)
    except ValueError as exc:
        raise ValueError('JSON 无法解析') from exc
    if not isinstance(project, dict):
        raise ValueError('工程必须是 JSON 对象')
    notes = project.get('notes')
    if not isinstance(notes, list) or not notes:
        raise ValueError('工程缺少 notes 音符列表')
    for index, note in enumerate(notes):
        if not isinstance(note, dict):
            raise ValueError(f'第 {index} 个音符不是对象')
        for key in ('pitch', 'duration'):
            if key not in note:
                raise ValueError(f'第 {index} 个音符缺少 {key}')
        if not str(note.get('lyric', '')).strip():
            raise ValueError(f'第 {index} 个音符缺少歌词映射')
