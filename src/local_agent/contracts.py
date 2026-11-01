"""T15：设备接入与权限策略的契约（设备配对、可信信封、动作包、错误码）。

可信信封（1.7.2）：调用者、QQ 账号、原始消息 ID 与签名由 **Broker** 生成，
绝不从模型响应读取。审批令牌绑定“动作参数哈希 + 设备 + 主体 + 有效期 + 策略版本”，
任何参数变化都要重新审批。

拟定动作包（1.7.2）：
  {job_id, device_id, action, args:{...}, expires_at, policy_version, nonce}

实现补充（2026-10-20）：
- 首版用“配对时建立的本机共享密钥 + HMAC-SHA256”做签名（对称实现，便于本机独立
  进程校验；公钥/证书方案作为后续多机升级项，不降低当前拒绝规则）。
- 信封只接受“主人私聊”事件；群聊/引用/转发一律不作为授权证明。
- 参数里出现 ``approved``/``signature`` 等伪造审批字段直接拒绝——模型不能自己批准。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..audio_contracts import ErrorCode, reject


# L0/L1 首批可登记动作（1.7.3；L2/L3 首发不开放）。
ALLOWED_ACTIONS_L0L1 = {
    'agent.health', 'agent.capabilities',          # L0 只读
    'app.open', 'volume.set', 'media.pause',       # L1 已授权能力
}
LEVEL: dict[str, int] = {
    'agent.health': 0, 'agent.capabilities': 0,
    'app.open': 1, 'volume.set': 1, 'media.pause': 1,
}
# 模型/网页可能伪造的“已批准”字段：出现在参数里即拒绝。
FORBIDDEN_ARG_KEYS = {'approved', 'approved_by', 'approval', 'approval_token',
                      'signature', 'signed', 'is_owner', 'root', 'admin'}
_DEVICE_ID = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_ACCOUNT = re.compile(r'^\d{1,32}$')
MAX_ARGS_BYTES = 8 * 1024


@dataclass
class ActionEnvelope:
    """可信消息信封：由消息入站链生成，模型输出不能提供这些字段。"""
    caller: str
    qq_account: str
    message_id: str
    kind: str = 'private'          # 只接受 private（群聊/引用/转发不是授权）
    signature: str = ''
    timestamp: float = 0.0


@dataclass
class ActionProposal:
    """一次“已登记动作”的结构化提案（一次一个，自然语言多步骤后续再做）。"""
    job_id: str
    device_id: str
    action: str
    args: dict = field(default_factory=dict)
    expires_at: float = 0.0        # 短期有效时间
    policy_version: int = 1
    nonce: str = ''                # 一次性随机值（防重放）

    def canonical(self) -> str:
        raw = {'job_id': self.job_id, 'device_id': self.device_id,
               'action': self.action, 'args': self.args,
               'expires_at': round(float(self.expires_at), 3),
               'policy_version': int(self.policy_version), 'nonce': self.nonce}
        return json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(',', ':'))

    def param_hash(self) -> str:
        """参数摘要：审批绑定它；改任何参数都要重新审批（1.7.2）。"""
        raw = json.dumps({'a': self.action, 'args': self.args},
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def validate_shape(self) -> None:
        """动作包自身合法性（不含身份/签名，那些由 broker 校验）。"""
        if not _DEVICE_ID.match(str(self.device_id or '')):
            reject(ErrorCode.INVALID_INPUT, '设备编号格式不正确')
        if not self.job_id or not str(self.job_id).strip():
            reject(ErrorCode.INVALID_INPUT, '缺少动作作业编号')
        if not self.nonce or len(str(self.nonce)) < 8:
            reject(ErrorCode.INVALID_INPUT, '动作提案缺少一次性 nonce')
        if self.action not in ALLOWED_ACTIONS_L0L1:
            reject(ErrorCode.POLICY_DENIED, f'动作未登记：{self.action}')
        if LEVEL.get(self.action, 3) > 1:
            reject(ErrorCode.POLICY_DENIED, f'{self.action} 超出首版 L0/L1 范围')
        if not isinstance(self.args, dict):
            reject(ErrorCode.INVALID_INPUT, '动作参数必须是对象')
        bad = FORBIDDEN_ARG_KEYS.intersection({str(key).lower() for key in self.args})
        if bad:
            reject(ErrorCode.POLICY_DENIED,
                   '动作参数包含伪造审批字段：' + ','.join(sorted(bad)))
        try:
            encoded = json.dumps(self.args, ensure_ascii=False).encode('utf-8')
        except (TypeError, ValueError):
            reject(ErrorCode.INVALID_INPUT, '动作参数无法序列化')
        if len(encoded) > MAX_ARGS_BYTES:
            reject(ErrorCode.INVALID_INPUT, '动作参数过大')

    def check_envelope(self, envelope: ActionEnvelope, *, secret: str,
                       expected_account: str, expected_principal: str,
                       now: Optional[float] = None, max_skew: float = 300.0) -> None:
        """可信元数据校验（T15 验收：错误账号/主人/设备/签名/过期/重放全拒）。

        - 群聊/引用/转发：``kind != 'private'`` → POLICY_DENIED；
        - 账号/主人不匹配 → UNAUTHORIZED；
        - 时间戳偏差过大或提案过期 → APPROVAL_EXPIRED；
        - 签名不匹配 → UNAUTHORIZED（模型/网页无法伪造）。
        """
        if not isinstance(envelope, ActionEnvelope):
            reject(ErrorCode.UNAUTHORIZED, '缺少可信消息信封')
        if not envelope.caller or not envelope.qq_account or not envelope.message_id:
            reject(ErrorCode.UNAUTHORIZED, '缺少可信调用主体')
        if str(envelope.kind) != 'private':
            reject(ErrorCode.POLICY_DENIED,
                   '群聊/引用/转发不作为授权，动作只接受主人私聊')
        if not _ACCOUNT.match(str(envelope.qq_account)):
            reject(ErrorCode.UNAUTHORIZED, 'QQ 账号格式不正确')
        if expected_account and str(envelope.qq_account) != str(expected_account):
            reject(ErrorCode.UNAUTHORIZED, 'QQ 账号与设备绑定不一致')
        if expected_principal and str(envelope.caller) != str(expected_principal):
            reject(ErrorCode.UNAUTHORIZED, '调用主人与设备绑定不一致')
        current = time.time() if now is None else float(now)
        if envelope.timestamp and abs(current - float(envelope.timestamp)) > float(max_skew):
            reject(ErrorCode.APPROVAL_EXPIRED, '消息信封时间戳超出允许范围')
        expected = signature_for(self, qq_account=envelope.qq_account,
                                 message_id=envelope.message_id,
                                 kind=envelope.kind, secret=secret)
        if not envelope.signature or not hmac.compare_digest(
                str(envelope.signature), expected):
            reject(ErrorCode.UNAUTHORIZED, '动作签名无效（可信信封校验失败）')
        self.validate_shape()
        if self.expires_at and current > float(self.expires_at):
            reject(ErrorCode.APPROVAL_EXPIRED, '动作提案已过期')


def signature_for(proposal: ActionProposal, *, qq_account: str, message_id: str,
                  kind: str = 'private', secret: str) -> str:
    """首版本机共享密钥签名：绑定动作包 + 账号 + 原始消息 ID。"""
    if not secret:
        reject(ErrorCode.UNAUTHORIZED, '缺少设备配对密钥，无法签名')
    message = '|'.join((proposal.canonical(), str(qq_account), str(message_id),
                        str(kind)))
    return hmac.new(str(secret).encode('utf-8'), message.encode('utf-8'),
                    hashlib.sha256).hexdigest()


class DevicePair:
    """设备配对记录（配对密钥/公钥、授权版本；密钥走本机凭据保护，界面只显指纹）。"""

    def __init__(self, device_id: str, public_key: str, *,
                 bound_account: str, principal: str, policy_version: int = 1):
        self.device_id = device_id
        self.public_key = public_key
        self.bound_account = bound_account      # 机器人账号 + 主人私聊（换任一都要重验）
        self.principal = principal
        self.policy_version = policy_version

    def fingerprint(self) -> str:
        """只显示指纹，不显示密钥本身。"""
        return hashlib.sha256(str(self.public_key).encode()).hexdigest()[:16]


class CapabilityLevel(int):
    """能力等级（1.7.3）：L0/L1 首版，L2/L3 首发不开放。"""
    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3


def requestable_level(action: str) -> int:
    return LEVEL.get(action, CapabilityLevel.L3)   # 未知动作默认最高级 = 首版不放
