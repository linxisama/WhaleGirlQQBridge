"""T09：歌曲工作流基础 + S0（分型、输入检查、分阶段状态、素材绑定、短片段预览）。

设计：
- 用 Capability 把 S0/S1/S2/S3 分型，缺输入就按 audio_contracts 明确拒绝，
  绝不把“导入播放”标成“生成成功”（T09 验收）。
- 唱歌是独立队列（1.5.3 上限 5、同 GPU 并发 1），不占 QQ 消息处理线程。
- 歌词不会被解释成电脑命令（T09 验收）：本管线只做音频，不产结构化动作。

引擎调用留桩：S1→cloud_music/local_s1、S2→local_svs、S3→local_svc。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..audio_contracts import (
    AudioJobRequest, AudioMode, Capability, ContractError, ErrorCode,
    MODE_INPUTS, MODE_CAPABILITY, create_job, reject,
)


@dataclass
class SingingPlan:
    """一次唱歌任务的规范化结果：分型 + 缺什么 + 目标片段长度。"""
    mode: AudioMode
    capability: Capability
    missing_inputs: tuple[str, ...] = field(default_factory=tuple)
    target_seconds: int = 30          # 首版 15–30 秒片段
    needs_score: bool = False
    needs_source_vocal: bool = False
    needs_music_model: bool = False


def classify(request: AudioJobRequest, *,
             has_local_svs: bool = False, has_local_svc: bool = False,
             has_music_api: bool = False, has_local_s1: bool = False) -> SingingPlan:
    """把 mode 映射成“能不能做、缺什么”的显式计划（对应 S1–S3 的外部前提）。

    - S2 无谱 / 无 SVS 引擎与歌声库 → 明确拒绝，不能拿歌词朗读冒充。
    - S3 无源演唱 / 无目标音色模型 → 明确拒绝。
    - S1 无音乐模型/云 API → 显示“原创未配置”，保留 S0/S2/S3。
    """
    cap = MODE_CAPABILITY[request.mode]
    plan = SingingPlan(mode=request.mode, capability=cap)

    if cap is Capability.SING_SCORE:
        plan.needs_score = True
        ok = bool(request.score) and has_local_svs
        plan.missing_inputs = tuple(f for f in ('score',) if not getattr(request, f, None))
        if not ok:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   'S2 需要谱面与已就绪的 SVS 引擎/歌声库；当前未满足，拒绝以朗读冒充',
                   mode=request.mode.value, has_local_svs=has_local_svs)
    elif cap is Capability.VOICE_CONVERT:
        plan.needs_source_vocal = True
        ok = bool(request.source_audio) and bool(request.target_voice) and has_local_svc
        if not ok:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   'S3 需要源演唱与目标音色模型；当前未满足', mode=request.mode.value)
    elif cap is Capability.SONG_GENERATE:
        plan.needs_music_model = True
        ok = (has_music_api or has_local_s1) and bool(request.lyrics)
        if not ok:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   'S1 本地/云原创未配置；保留 S0/S2/S3', mode=request.mode.value)

    return plan


def preview(plan: SingingPlan) -> dict:
    """返回给 QQ 的短任务状态：任务号、排队阶段、预计片段长度。

    阶段通知合并与限频（1.6.3）：不要每次轮询都发一条。
    """
    return {'capability': plan.capability.value, 'mode': plan.mode.value,
            'target_seconds': plan.target_seconds, 'state': 'queued',
            'message': '唱歌任务已排队，完成后可预览或按原意图发送'}


def start(request: AudioJobRequest, *, subject_domain: str, principal: str,
          has_local_svs: bool = False, has_local_svc: bool = False,
          has_music_api: bool = False, has_local_s1: bool = False) -> dict:
    """规范化 + 校验 + 分型，返回可入队/预览的结果。

    TODO(T09/T04): 把 create_job 的结果交给独立唱歌队列（上限 5、同 GPU 并发 1）；
    取消时丢弃后到结果（不能通过“发旧结果”假装完成）。
    """
    create_job(request, subject_domain=subject_domain, principal=principal)
    plan = classify(request, has_local_svs=has_local_svs, has_local_svc=has_local_svc,
                    has_music_api=has_music_api, has_local_s1=has_local_s1)
    return {'plan': plan, 'preview': preview(plan)}
