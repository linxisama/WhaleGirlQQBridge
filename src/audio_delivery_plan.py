"""T13：长音频交付计划（预览/乐句分段/独立幂等键/段序状态/可选文件通道）。

与 1.6.2 对齐：
- 歌曲默认发 ≤30 秒预览；长内容按乐句切 30–60 秒段，每段都小于时长/大小上限。
- 每段独立 request_id + 持久化发送前状态；某段 uncertain 即暂停后续段，
  展示“已确认/待核实”的段号，不自动重发整首。
- 完整曲优先走经 T13 单独验证的群/私聊文件能力；不支持时只保留本机文件并明确提示，
  绝不偷偷上传公网、不发不可访问的本地路径。
- 切号不发；段序可追踪。

本模块只做“计划与段状态”的纯逻辑（可单测）；真正发送原语由核心在
qqbridge_transport 集成，这里不直接写桥接文件。

实现补充（2026-10-20）：
- 每段 request_id 由（计划键 + 段序号 + 起止秒）确定性派生，重建计划不会换键，
  因此能安全重试同一段而不会重发整首；
- 乐句边界（sentence boundaries）优先于固定 30 秒切分；没有边界才退到等长切分；
- ``uncertain`` 会把计划整体暂停，后续段保持 pending 但不再可发。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Optional

from .audio_contracts import DeliveryState, ErrorCode, reject

DEFAULT_PREVIEW_SECONDS = 30
MAX_SEGMENT_SECONDS = 60
MIN_SEGMENT_SECONDS = 5
MAX_TOTAL_SECONDS = 600


@dataclass
class Segment:
    index: int
    start: float
    end: float
    request_id: str
    state: DeliveryState = DeliveryState.PENDING
    message_id: str = ''

    @property
    def duration(self) -> float:
        return round(max(0.0, self.end - self.start), 3)

    def to_payload(self) -> dict:
        return {'index': self.index, 'start': round(self.start, 3),
                'end': round(self.end, 3), 'duration': self.duration,
                'state': self.state.value, 'request_id': self.request_id,
                'message_id': self.message_id}


@dataclass
class DeliveryPlan:
    audio_asset_ids: list
    kind: str = 'private'
    target: str = ''
    account_id: str = ''
    segments: list = field(default_factory=list)
    preview_only: bool = True
    paused: bool = False
    aborted_reason: str = ''

    def to_payload(self) -> dict:
        return {'audio_asset_ids': self.audio_asset_ids,
                'segments': [segment.to_payload() for segment in self.segments],
                'kind': self.kind, 'target': self.target,
                'account_id': self.account_id,
                'preview_only': self.preview_only, 'paused': self.paused,
                'aborted_reason': self.aborted_reason,
                'confirmed': self.confirmed_segments(),
                'uncertain': self.uncertain_segments()}

    def confirmed_segments(self) -> list[int]:
        return [s.index for s in self.segments if s.state is DeliveryState.SENT]

    def uncertain_segments(self) -> list[int]:
        return [s.index for s in self.segments if s.state is DeliveryState.UNCERTAIN]


def _segment_request_id(request_id: str, index: int, start: float, end: float) -> str:
    raw = f'{request_id}|{index}|{start:.3f}|{end:.3f}'
    return f'{request_id}-s{index}-{hashlib.sha256(raw.encode()).hexdigest()[:10]}'


def _validate_total(total_seconds: float) -> float:
    try:
        total = float(total_seconds)
    except (TypeError, ValueError):
        reject(ErrorCode.INVALID_INPUT, '音频总时长须为数字')
    if not math.isfinite(total) or total <= 0:
        reject(ErrorCode.INVALID_INPUT, '音频总时长须大于 0')
    if total > MAX_TOTAL_SECONDS:
        reject(ErrorCode.INVALID_INPUT,
               f'单次交付总时长不得超过 {MAX_TOTAL_SECONDS} 秒，请拆分交付',
               total_seconds=total)
    return total


def _chunk_boundaries(total: float, boundaries: Optional[list],
                      max_seconds: float) -> list[tuple[float, float]]:
    """按乐句边界尽量填满每段，但不超过 max_seconds。"""
    points = [0.0]
    if boundaries:
        cleaned = sorted({round(float(item), 3) for item in boundaries
                          if isinstance(item, (int, float)) and 0 < float(item) < total})
        points.extend(cleaned)
    points.append(float(total))
    spans: list[tuple[float, float]] = []
    start = 0.0
    for point in points[1:]:
        if point - start > max_seconds:
            # 乐句本身过长：在段内按 max_seconds 硬切（仍受上限约束）。
            while point - start > max_seconds:
                spans.append((start, round(start + max_seconds, 3)))
                start += max_seconds
        spans.append((round(start, 3), round(point, 3)))
        start = point
    merged: list[tuple[float, float]] = []
    for span_start, span_end in spans:
        if span_end <= span_start:
            continue
        if merged and span_end - merged[-1][0] <= max_seconds:
            merged[-1] = (merged[-1][0], span_end)
        else:
            merged.append((span_start, span_end))
    return merged


def build_plan(*, audio_asset_ids: list, total_seconds: float,
               kind: str, target: str, account_id: str,
               request_id: str, preview_only: bool = True,
               preview_seconds: int = DEFAULT_PREVIEW_SECONDS,
               max_segment_seconds: int = MAX_SEGMENT_SECONDS,
               boundaries: Optional[list] = None) -> DeliveryPlan:
    """把一段音频切成“可发段”：预览 + 若干乐句段，每段独立幂等键（1.6.2）。"""
    if not audio_asset_ids:
        reject(ErrorCode.INVALID_INPUT, '交付计划至少需要一个音频资产')
    if not request_id:
        reject(ErrorCode.INVALID_INPUT, '交付计划缺少请求编号')
    if kind not in ('group', 'private'):
        reject(ErrorCode.INVALID_INPUT, '交付类型须为 group 或 private')
    if not str(target).isdigit() or not str(account_id).isdigit():
        reject(ErrorCode.INVALID_INPUT, '发送目标与账号须为数字编号')
    total = _validate_total(total_seconds)
    preview = max(MIN_SEGMENT_SECONDS, int(preview_seconds))
    capped = max(preview, min(int(max_segment_seconds), MAX_SEGMENT_SECONDS))
    plan = DeliveryPlan(audio_asset_ids=list(audio_asset_ids), kind=kind,
                        target=str(target), account_id=str(account_id),
                        preview_only=bool(preview_only))
    if preview_only and total > preview:
        spans = [(0.0, float(preview))]
    elif preview_only:
        spans = [(0.0, total)]
    else:
        spans = _chunk_boundaries(total, boundaries, capped)
        # 合并后仍可能超过上限（极端边界），再硬切一次。
        safe: list[tuple[float, float]] = []
        for span_start, span_end in spans:
            while span_end - span_start > capped:
                safe.append((round(span_start, 3), round(span_start + capped, 3)))
                span_start += capped
            if span_end - span_start > 0:
                safe.append((round(span_start, 3), round(span_end, 3)))
        spans = safe
    for index, (start, end) in enumerate(spans):
        plan.segments.append(Segment(
            index=index, start=start, end=end,
            request_id=_segment_request_id(str(request_id), index, start, end)))
    return plan


def mark_segment(plan: DeliveryPlan, index: int, state: DeliveryState,
                 request_id: Optional[str] = None, message_id: str = '') -> DeliveryPlan:
    """记录某段发送结果。uncertain 时**暂停**后续段（1.6.2），不自动重发。"""
    target_segment = next((segment for segment in plan.segments if segment.index == index), None)
    if target_segment is None:
        reject(ErrorCode.INVALID_INPUT, f'交付计划没有段 {index}')
    if request_id and request_id != target_segment.request_id:
        reject(ErrorCode.INVALID_INPUT, '段请求编号不匹配，拒绝写入',
               expected=target_segment.request_id)
    if target_segment.state is DeliveryState.SENT and state is not DeliveryState.SENT:
        # 已确认发送的段不允许被迟到的失败结果覆盖。
        return plan
    target_segment.state = state
    if message_id:
        target_segment.message_id = str(message_id)
    if state is DeliveryState.UNCERTAIN:
        plan.paused = True
        if not plan.aborted_reason:
            plan.aborted_reason = f'第 {index} 段结果待核实，暂停后续段，不自动重发'
    return plan


def can_continue(plan: DeliveryPlan, *, account_id: Optional[str] = None) -> tuple[bool, str]:
    """发送前检查：计划未暂停、账号未切换、还有待发段。"""
    if plan.aborted_reason:
        return False, plan.aborted_reason
    if plan.paused:
        return False, '计划已暂停（存在待核实段）'
    if account_id is not None and str(account_id) != str(plan.account_id):
        return False, '机器人账号已变化，整份交付计划作废（切号不发）'
    return True, ''


def next_pending(plan: DeliveryPlan, *, account_id: Optional[str] = None) -> Optional[Segment]:
    allowed, _reason = can_continue(plan, account_id=account_id)
    if not allowed:
        return None
    return next((segment for segment in plan.segments
                 if segment.state is DeliveryState.PENDING), None)


def file_channel_fallback(total_seconds: float, *, verified_file_capability: bool,
                          max_file_mb: int = 200) -> dict:
    """完整曲的“本机保存”兜底：无文件能力时明确提示，不上传公网（T13 验收）。"""
    total = _validate_total(total_seconds)
    if verified_file_capability:
        return {'deliver': 'file_channel', 'note': '走已验证文件通道',
                'total_seconds': round(total, 3), 'max_file_mb': int(max_file_mb)}
    return {'deliver': 'local_only',
            'note': '完整曲仅保留本机文件，未上传公网，未发不可访问路径',
            'total_seconds': round(total, 3), 'max_file_mb': int(max_file_mb)}
