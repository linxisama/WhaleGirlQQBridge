"""音频网关公共契约（规划书 T01 的可运行部分）。

只定义“大家按同一套类型通信”的骨架，不含供应商调用、不含队列调度实现：

- 能力/模式/路由/状态的枚举与统一错误码；
- 作业、发送、资产的 Pydantic schema 与校验器；
- 作业幂等键与参数哈希；
- Provider 协议 + 可注册表 + 路由策略选择（纯函数，可单测）。

下游模块（资产、队列、各引擎适配、唱歌管线、本地 Agent、交付计划）都 import 这里，
保证“TTS / 三类唱歌 / 权限信封不能混用、不支持能力可明确拒绝”。

实现状态：契约层是**完成态**，可用；真正的“入队、执行、发送”在各桩模块里
（见 agents/workbuddy/reports/audio-framework.md 的交接说明）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator


# ── 能力与模式 ─────────────────────────────────────────────────────────────
class Capability(str, Enum):
    """统一能力编号（规划书 1.1）。"""
    TTS = 'tts'                # V1 把话说出来
    CHARACTER_TTS = 'character_tts'   # V2 角色音色
    SONG_IMPORT = 'song_import'       # S0 发本地歌曲
    SONG_GENERATE = 'song_generate'   # S1 按歌词原创
    SING_SCORE = 'sing_score'         # S2 照谱唱
    VOICE_CONVERT = 'voice_convert'   # S3 清唱换音色


class AudioMode(str, Enum):
    """POST /api/v2/audio/jobs 的 mode 字段取值。"""
    TTS = 'tts'
    SONG_GENERATE = 'song_generate'
    SING_SCORE = 'sing_score'
    VOICE_CONVERT = 'voice_convert'


# 每种模式需要的输入/产出边界。用来做“能不能做、缺什么”的硬校验，
# 而不是靠运行时猜。未知模式/缺输入会被 _validate_mode_specific 拒绝。
MODE_INPUTS: dict[AudioMode, tuple[str, ...]] = {
    AudioMode.TTS: ('text',),
    AudioMode.SONG_GENERATE: ('lyrics',),
    AudioMode.SING_SCORE: ('lyrics', 'score'),
    AudioMode.VOICE_CONVERT: ('source_audio', 'target_voice'),
}
MODE_CAPABILITY: dict[AudioMode, Capability] = {
    AudioMode.TTS: Capability.TTS,
    AudioMode.SONG_GENERATE: Capability.SONG_GENERATE,
    AudioMode.SING_SCORE: Capability.SING_SCORE,
    AudioMode.VOICE_CONVERT: Capability.VOICE_CONVERT,
}


class RoutePolicy(str, Enum):
    LOCAL_ONLY = 'local_only'
    CLOUD_ONLY = 'cloud_only'
    LOCAL_PREFERRED = 'local_preferred'


class DeliveryIntent(str, Enum):
    """意图本身不含 QQ 目标：发送目标由可信消息路由绑定（1.5.1/1.6.2）。"""
    PREVIEW = 'preview'
    SEND = 'send'


# ── 状态枚举 ───────────────────────────────────────────────────────────────
class JobState(str, Enum):
    QUEUED = 'queued'
    PREPARING = 'preparing'
    RUNNING = 'running'
    POSTPROCESSING = 'postprocessing'
    READY = 'ready'
    FAILED = 'failed'
    CANCEL_REQUESTED = 'cancel_requested'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    UNKNOWN = 'unknown'       # 外部生成结果待核实

    @property
    def terminal(self) -> bool:
        return self in {
            JobState.READY, JobState.FAILED, JobState.CANCELLED,
            JobState.EXPIRED, JobState.UNKNOWN,
        }


class DeliveryState(str, Enum):
    PENDING = 'pending'
    SENDING = 'sending'
    SENT = 'sent'
    SKIPPED = 'skipped'
    FAILED = 'failed'
    UNCERTAIN = 'uncertain'


class AgentJobState(str, Enum):
    AWAITING_APPROVAL = 'awaiting_approval'
    EXECUTING = 'executing'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    UNCERTAIN = 'uncertain'


# ── 统一错误码（3.4）───────────────────────────────────────────────────────
class ErrorCode(str, Enum):
    INVALID_INPUT = 'INVALID_INPUT'
    ASSET_INVALID = 'ASSET_INVALID'
    CAPABILITY_UNSUPPORTED = 'CAPABILITY_UNSUPPORTED'
    PROVIDER_UNAVAILABLE = 'PROVIDER_UNAVAILABLE'
    MODEL_NOT_READY = 'MODEL_NOT_READY'
    GPU_OOM = 'GPU_OOM'
    QUEUE_FULL = 'QUEUE_FULL'
    DEADLINE_EXCEEDED = 'DEADLINE_EXCEEDED'
    BUDGET_EXCEEDED = 'BUDGET_EXCEEDED'
    UNAUTHORIZED = 'UNAUTHORIZED'
    POLICY_DENIED = 'POLICY_DENIED'
    APPROVAL_EXPIRED = 'APPROVAL_EXPIRED'
    GENERATION_UNKNOWN = 'GENERATION_UNKNOWN'
    DELIVERY_UNCERTAIN = 'DELIVERY_UNCERTAIN'
    ACTION_UNCERTAIN = 'ACTION_UNCERTAIN'
    TRANSCODE_FAILED = 'TRANSCODE_FAILED'
    QQ_FORMAT_UNSUPPORTED = 'QQ_FORMAT_UNSUPPORTED'


class ContractError(Exception):
    """契约层可预期的拒绝；带错误码，下游能据此决定“重试还是停下”。"""

    def __init__(self, code: ErrorCode, message: str, **details):
        self.code = code
        self.details = details
        super().__init__(f'{code.value}: {message}')

    def to_payload(self) -> dict:
        """给 HTTP/日志用的脱敏结构，不含栈、不含本地私有路径。"""
        payload = {'code': self.code.value, 'message': str(self)}
        if self.details:
            payload['details'] = {k: str(v) for k, v in self.details.items()}
        return payload


def reject(code: ErrorCode, message: str, **details) -> None:
    raise ContractError(code, message, **details)


# ── 作业 schema ────────────────────────────────────────────────────────────
class AudioJobRequest(BaseModel):
    """POST /api/v2/audio/jobs 的入参（1.5.1 示意 + 唱歌/SVC 必需项）。

    只声明“字段存在与否合法”，模式相关的必填校验放在 create() 的
    _validate_mode_specific 里，保证错误文案精确。
    """
    client_request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    mode: AudioMode
    text: Optional[str] = None                # 1–300 字符在 _validate_mode_specific 统一判定
    lyrics: Optional[str] = None
    score: Optional[str] = None                 # S2 谱面引用（工程/资产 ID）
    source_audio: Optional[str] = None          # S3 源演唱资产 ID
    target_voice: Optional[str] = None          # S3 目标音色
    voice_profile_id: str = 'persona-default'
    route_policy: RoutePolicy = RoutePolicy.LOCAL_ONLY
    delivery_intent: DeliveryIntent = DeliveryIntent.PREVIEW
    language: str = 'zh'
    extra: dict = Field(default_factory=dict)   # 情绪/速度等，进参数哈希

    @field_validator('extra')
    @classmethod
    def _no_secrets(cls, value: dict) -> dict:
        bad = [k for k in value if k.lower() in {'api_key', 'token', 'secret', 'password'}]
        if bad:
            raise ValueError('extra 不允许携带凭据字段：' + ','.join(bad))
        return value


class AudioJobResult(BaseModel):
    """POST /api/v2/audio/jobs 的出参：先给作业编号，生成在后台排队。"""
    success: bool = True
    job_id: str
    state: JobState = JobState.QUEUED
    queued: bool = False
    duplicate: bool = False
    message: str = ''


def create_job(request: AudioJobRequest, *, subject_domain: str,
               principal: str) -> AudioJobResult:
    """按契约把一次请求规范化成一个可入队的作业。

    subject_domain + principal 是可信主体域（1.5.2 的“主体/会话域”），
    与“模型生成的用户 ID 不作为授权依据”一致：调用方必须显式传入，
    不能来自模型输出。

    返回已入队（QUEUED）；重复请求只建一个作业。真正的执行交给 JobScheduler。
    """
    request.model_validate(request.model_dump())   # 触发 validator
    if not subject_domain:
        reject(ErrorCode.UNAUTHORIZED, '缺少可信主体域，拒绝入队')
    _validate_mode_specific(request)

    # 语音参数快照固定：配置变化时取消或重建，不在合成中途换音色。
    param_hash = job_parameter_hash(request, subject_domain=subject_domain,
                                    principal=principal)
    job_id = hashlib.sha256(param_hash.encode()).hexdigest()[:24]
    # TODO(T04): 由 JobScheduler 持久化到 audio_jobs 表并做幂等去重
    #   —— 现在只返回规范化结果，落库/领取在 audio_jobs.py / audio_scheduler.py。
    return AudioJobResult(job_id=job_id, state=JobState.QUEUED)


def _validate_mode_specific(request: AudioJobRequest) -> None:
    """模式与输入必须匹配，缺输入就明确拒绝（规划书 1.1/2.3 T09 验收）。"""
    for required in MODE_INPUTS[request.mode]:
        if getattr(request, required, None) in (None, ''):
            reject(ErrorCode.INVALID_INPUT,
                   f'{request.mode.value} 缺少必需输入：{required}',
                   mode=request.mode.value)
    if request.mode is AudioMode.TTS:
        text = request.text.strip()
        if not 1 <= len(text) <= 300:
            reject(ErrorCode.INVALID_INPUT, 'tts 文本须为 1–300 字符')
    # 唱歌模式把“谱面/源演唱”当普通文本读给 TTS 是错误管线（3.6），此处硬拒。
    if request.mode is AudioMode.SING_SCORE and not (request.lyrics and request.score):
        reject(ErrorCode.CAPABILITY_UNSUPPORTED, 'sing_score 需要歌词与谱面同时提供')


def job_parameter_hash(request: AudioJobRequest, *, subject_domain: str,
                       principal: str) -> str:
    """参数哈希：同键不同内容 → 不同哈希 → 不同作业（1.5.1 幂等规则）。"""
    snapshot = {
        'mode': request.mode.value,
        'text': request.text,
        'lyrics': request.lyrics,
        'score': request.score,
        'source_audio': request.source_audio,
        'target_voice': request.target_voice,
        'voice_profile_id': request.voice_profile_id,
        'route_policy': request.route_policy.value,
        'delivery_intent': request.delivery_intent.value,
        'language': request.language,
        'extra': {k: v for k, v in sorted(request.extra.items())},
        'subject_domain': subject_domain,
        'principal': principal,
        'client_request_id': request.client_request_id,
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                                     ensure_ascii=False, default=str).encode()).hexdigest()


# ── 资产 / 发送 schema ──────────────────────────────────────────────────────
class AudioAssetMeta(BaseModel):
    """GET /api/v2/audio/assets/{audio_id} 的本机鉴权预览元数据。"""
    audio_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    media_type: str = 'audio/wav'
    sample_rate: int = 0
    channels: int = 1
    duration_seconds: float = 0.0
    size_bytes: int = 0
    content_hash: str
    owner_domain: str
    licensed: bool = False


class DeliveryPlanRequest(BaseModel):
    """POST /api/v2/audio/deliveries：显式发送计划（1.6.2 独立幂等键）。"""
    delivery_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    audio_asset_ids: list[str] = Field(min_length=1)
    segments: list[int] = Field(default_factory=list,
                                description='分段序号；空=整段。与目标/账号绑定。')
    kind: Literal['group', 'private']
    target: str = Field(pattern=r'^\d{1,32}$')
    account_id: str = Field(pattern=r'^\d{1,32}$')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')

    @field_validator('audio_asset_ids')
    @classmethod
    def _ids(cls, values: list[str]) -> list[str]:
        for v in values:
            if not _sha256_or_64hex(v):
                raise ValueError('资产编号须为 64 位十六进制内容哈希')
        return values


def _sha256_or_64hex(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value.lower())


class DeliveryStatus(BaseModel):
    delivery_id: str
    state: DeliveryState
    platform_message_id: str = ''
    uncertain_reason: str = ''


# ── Provider 协议与注册表 ──────────────────────────────────────────────────
@runtime_checkable
class ProviderAdapter(Protocol):
    """每个适配器只负责合成（1.2），不负责 QQ 发送。"""
    name: str
    capability: Capability
    supports_cloud: bool

    def available(self) -> bool: ...
    def ready(self) -> bool: ...
    async def synthesize(self, request: AudioJobRequest, profile: dict) -> bytes:
        """返回原始音频字节；网关负责校验/入库/转码，不在适配器里发 QQ。"""


_ADAPTERS: dict[str, type] = {}


def register_provider(adapter_cls: type) -> type:
    """登记一个 ProviderAdapter 子类。未登记 = 引擎不可用，路由策略会回退或拒绝。

    不做 issubclass 校验：ProviderAdapter 是带非方法字段的 Protocol，
    issubclass 对它会 TypeError。这里只要求“是类型 + 声明了必需成员”。
    """
    if not isinstance(adapter_cls, type):
        reject(ErrorCode.CAPABILITY_UNSUPPORTED, f'{adapter_cls} 不是类')
    for attr in ('name', 'capability', 'supports_cloud', 'synthesize'):
        if not hasattr(adapter_cls, attr):
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   f'{adapter_cls.__name__} 缺少 ProviderAdapter 成员：{attr}')
    _ADAPTERS[adapter_cls.name] = adapter_cls
    return adapter_cls


def available_providers(capability: Capability) -> list[type]:
    return [cls for cls in _ADAPTERS.values() if cls.capability is capability]


def select_provider(request: AudioJobRequest, *,
                   local_ready: set[str], cloud_allowed: bool,
                   profiles: Optional[dict] = None) -> str:
    """按路由策略挑引擎（4.5）。纯函数，便于单测，不碰真实网络。

    - local_preferred：本地就绪且能力等价优先；否则在“已允许出网且有云能力”时降级；
      两个都不满足 → 明确拒绝，不静默换音色。
    - 三个 TTS 引擎不串联处理同一句（4.1）：只返回一个主引擎名。
    """
    wanted = MODE_CAPABILITY[request.mode]
    local_names = {n for n in local_ready if wanted in _capability_of(n)}
    cloud_names = {n for n in _ADAPTERS if _ADAPTERS[n].supports_cloud
                   and n not in local_names}
    has_cloud = cloud_allowed and bool(cloud_names) and _cloud_capable(wanted, cloud_names)

    if request.route_policy is RoutePolicy.LOCAL_ONLY:
        if not local_names:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   f'{request.route_policy.value} 但本地无可用 {wanted.value} 引擎',
                   capability=wanted.value)
        return sorted(local_names)[0]
    if request.route_policy is RoutePolicy.CLOUD_ONLY:
        if not has_cloud:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   'cloud_only 但未允许出网或缺少云端能力',
                   capability=wanted.value)
        return sorted(cloud_names)[0]
    # LOCAL_PREFERRED
    if local_names:
        return sorted(local_names)[0]
    if has_cloud:
        return sorted(cloud_names)[0]
    reject(ErrorCode.CAPABILITY_UNSUPPORTED,
           '本地未就绪且云端不可用；按 local_preferred 不能静默换音色，需文字降级或排队',
           capability=wanted.value)
    raise AssertionError('unreachable')


def _cloud_capable(capability: Capability, names: set[str]) -> bool:
    # 云端只对 TTS/原创歌曲有把握；S2/S3 云精确能力未启用（1.4.2 的边界）。
    return capability in {Capability.TTS, Capability.CHARACTER_TTS,
                          Capability.SONG_GENERATE}


_CAPABILITY_CACHE: dict[str, Capability] = {}


def _capability_of(name: str) -> Optional[Capability]:
    cls = _ADAPTERS.get(name)
    return cls.capability if cls else None


def capability_matrix() -> dict:
    """GET /api/v2/audio/capabilities 的数据源。

    把“已登记/本地就绪/可出网”分开表示（1.5.1），不把所有引擎都写成“可用”。
    """
    out: dict[str, dict] = {}
    for name, cls in _ADAPTERS.items():
        out[name] = {
            'capability': cls.capability.value,
            'supports_cloud': bool(cls.supports_cloud),
            'available': False,   # TODO(T00/T05-T07): 由 readiness 探测回填
            'ready': False,
        }
    return out
