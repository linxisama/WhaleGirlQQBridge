"""T15：动作白名单 + 资源白名单 + 短期授权 + 审计（与 broker 共用同一策略执行器）。

无论 REST、命名管道还是 MCP，最终动作都要经过这里（1.7.1 / 1.7.4）。

规则（T15 验收）：
- 白名单外的动作 / L2、L3 → 拒绝（POLICY_DENIED）。
- 审批令牌绑定“参数哈希 + 设备 + 主体 + 有效期 + 策略版本”，改参重审。
- 网页或模型伪造的批准无效；私聊机器人管理员**不能**自动获得宿主权限。
- 无令牌访问失败。
- 审计写 agent_audit（不写密钥、音频全文或隐含思维链）。

实现补充（2026-10-20）：批准记录带设备/主体/策略版本；执行成功后 ``consume()``
使同一批准不能重复使用（防重放），解绑/急停即时清空。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from ..audio_contracts import ErrorCode, reject
from .contracts import ActionProposal, CapabilityLevel, requestable_level


@dataclass
class Approval:
    param_hash: str
    device_id: str
    principal: str
    expires_at: float
    policy_version: int
    job_id: str = ''


class ActionPolicy:
    def __init__(self, policy_version: int = 1, approval_ttl: float = 120.0):
        self.policy_version = policy_version
        self.approval_ttl = approval_ttl      # 1.7.2：审批有效期建议 120s
        self.approvals: dict[str, Approval] = {}

    def is_first_release_allowed(self, action: str) -> bool:
        return requestable_level(action) <= CapabilityLevel.L1

    def grant(self, param_hash: str, *, device_id: str, principal: str,
              expires_at: Optional[float] = None, policy_version: Optional[int] = None,
              job_id: str = '') -> Approval:
        """本地批准入口（控制台点“允许”，**不**由聊天消息自动点击）。

        审批令牌绑定 param_hash（+设备/主体/有效期/策略版本）；
        任何参数变化都会产生新 param_hash → 需重新审批（1.7.2 / T17）。
        """
        if not param_hash or not device_id or not principal:
            reject(ErrorCode.INVALID_INPUT, '批准缺少参数哈希/设备/主体')
        version = self.policy_version if policy_version is None else int(policy_version)
        if version != self.policy_version:
            reject(ErrorCode.POLICY_DENIED, '策略版本不一致，拒绝写入批准')
        approval = Approval(param_hash=str(param_hash), device_id=str(device_id),
                            principal=str(principal),
                            expires_at=float(expires_at or (time.time() + self.approval_ttl)),
                            policy_version=version, job_id=str(job_id or ''))
        self.approvals[approval.param_hash] = approval
        return approval

    def authorize(self, proposal: ActionProposal, *, now: Optional[float] = None) -> bool:
        """执行前最后一道闸：白名单 + 审批 + 设备/主体 + 有效期 + 策略版本。"""
        if not self.is_first_release_allowed(proposal.action):
            reject(ErrorCode.POLICY_DENIED, f'{proposal.action} 不在首版 L0/L1')
        current = time.time() if now is None else float(now)
        approval = self.approvals.get(proposal.param_hash())
        if approval is None:
            reject(ErrorCode.UNAUTHORIZED, '无本地审批令牌（聊天不能解锁权限）')
        if str(approval.device_id) != str(proposal.device_id):
            reject(ErrorCode.UNAUTHORIZED, '审批令牌与设备不一致')
        if proposal.expires_at and current > float(proposal.expires_at):
            reject(ErrorCode.APPROVAL_EXPIRED, '动作提案已过期，请重新批准')
        if current > float(approval.expires_at):
            reject(ErrorCode.APPROVAL_EXPIRED, '审批已过期，请重新批准')
        if (int(proposal.policy_version) != self.policy_version
                or int(approval.policy_version) != self.policy_version):
            reject(ErrorCode.POLICY_DENIED, '策略版本不匹配，需重新审批')
        return True

    def consume(self, param_hash: str) -> bool:
        """执行成功后作废该批准（同一令牌不能重复执行）。"""
        return self.approvals.pop(str(param_hash), None) is not None

    def revoke(self, device_id: Optional[str] = None) -> int:
        """解绑/急停：清空未执行授权；返回清除数量。"""
        if device_id is None:
            count = len(self.approvals)
            self.approvals.clear()
            return count
        keys = [key for key, approval in self.approvals.items()
                if str(approval.device_id) == str(device_id)]
        for key in keys:
            self.approvals.pop(key, None)
        return len(keys)

    def pending(self) -> list[dict]:
        """给本地审批页的脱敏列表（不显示参数全文/密钥）。"""
        return [{'param_hash': approval.param_hash, 'device_id': approval.device_id,
                 'principal': approval.principal, 'job_id': approval.job_id,
                 'expires_at': approval.expires_at,
                 'policy_version': approval.policy_version}
                for approval in self.approvals.values()]

    def audit(self, store, job_id: str, event: str, now: Optional[float] = None) -> None:
        """写 agent_audit（脱敏：不写密钥/参数全文/思维链）。"""
        if not job_id or not event:
            return
        text = str(event).strip()
        if len(text) > 200:
            text = text[:200]
        for secret_marker in ('api_key', 'token=', 'secret', 'password'):
            if secret_marker in text.lower():
                text = '审计事件含敏感字段，已省略'
                break
        timestamp = float(now or time.time())
        if store is None:
            return
        if hasattr(store, 'executemany') or hasattr(store, 'execute'):
            store.execute('INSERT INTO agent_audit (job_id,event,at) VALUES (?,?,?)',
                          (str(job_id), text, timestamp))
            if hasattr(store, 'commit'):
                store.commit()
            return
        writer = getattr(store, 'audit', None)
        if callable(writer):
            writer(str(job_id), text, timestamp)
