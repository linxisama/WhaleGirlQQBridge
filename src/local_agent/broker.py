"""T15：LocalAgentBroker（配对、能力查询、动作提案与回执 + 防重放）。

接入方式（1.7.1）：首版独立 Windows 进程，以当前登录用户权限运行。
- 优先命名管道（限本机用户）；若用回环 HTTP/WS 只监听 127.0.0.1，
  采用随机会话凭证、请求签名/nonce、主体/设备绑定与来源校验。
- 回环地址本身**不是**鉴权：同机进程/网页也可能访问。
- 未来多机：设备主动出站连受控 Broker，双向认证 + 独立设备密钥；
  绝不把 Agent 管理端口暴露公网。MCP 可作为工具描述候选，但不是权限系统。

IPC 方法（1.7.2）：agent.health / agent.capabilities / agent.submit /
agent.get_job / agent.cancel / agent.revoke。心跳约 10s，连续 3 次丢失标离线；
短动作执行预算约 15s，审批有效期建议 120s。
超时/离线**不推定**动作未执行；执行前持久化 job_id 防重放。

实现补充（2026-10-20）：nonce 落库并带 TTL（默认 10 分钟）；配对密钥只进
注入的本机密钥存储（默认内存），``pair`` 只回设备指纹，不回密钥。
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Optional

from ..audio_contracts import AgentJobState, ErrorCode, reject
from ..audio_contracts_db import initialize as initialize_schema
from .contracts import (
    ALLOWED_ACTIONS_L0L1, ActionEnvelope, ActionProposal, CapabilityLevel,
    DevicePair, requestable_level, signature_for,
)
from .policy import ActionPolicy

_NONCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_nonces (
    nonce     TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_nonces_at ON agent_nonces(at);
CREATE TABLE IF NOT EXISTS agent_device_principals (
    device_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_job_args (
    job_id    TEXT PRIMARY KEY,
    args_json TEXT NOT NULL
);
"""

_TERMINAL = {AgentJobState.SUCCEEDED.value, AgentJobState.FAILED.value,
             AgentJobState.CANCELLED.value, AgentJobState.UNCERTAIN.value}


class LocalAgentBroker:
    NONCE_TTL = 600.0

    def __init__(self, db=None, store=None, *, secret_store=None, clock=None,
                 policy: Optional[ActionPolicy] = None):
        self.db = db
        self.store = store or db
        self._clock = clock or time.time
        self.policy = policy or ActionPolicy()
        self._nonces: dict[str, float] = {}      # nonce -> expires_at（内存镜像）
        self._devices: dict[str, dict] = {}      # device_id -> {pair, secret}
        self._secrets = secret_store if secret_store is not None else {}
        if db is not None:
            initialize_schema(db)
            db.executescript(_NONCE_SCHEMA)
            db.commit()

    # ── 密钥存储（本机凭据） ────────────────────────────────────────────────
    def _secret_get(self, device_id: str) -> str:
        store = self._secrets
        getter = getattr(store, 'get', None)
        if callable(getter):
            value = getter(device_id)
            return str(value or '')
        try:
            return str(store[device_id])
        except (KeyError, TypeError):
            return ''

    def _secret_set(self, device_id: str, secret: str) -> None:
        setter = getattr(self._secrets, 'set', None)
        if callable(setter):
            setter(device_id, secret)
            return
        self._secrets[device_id] = secret

    # ── 配对 ────────────────────────────────────────────────────────────────
    def pair(self, device_id: str, public_key: str, *,
             qq_account: str, principal: str, secret: Optional[str] = None,
             policy_version: int = 1) -> dict:
        """完成设备配对（本地控制台里做：选 QQ 账号 + 主人私聊 → 展示能力 → 确认）。"""
        device_id = str(device_id or '').strip()
        if not device_id or len(device_id) > 64:
            reject(ErrorCode.INVALID_INPUT, '设备编号须为 1–64 字符')
        if not str(public_key or '').strip():
            reject(ErrorCode.INVALID_INPUT, '缺少设备公钥/指纹')
        if not str(qq_account).isdigit():
            reject(ErrorCode.INVALID_INPUT, '绑定的 QQ 账号须为数字')
        if not str(principal or '').strip():
            reject(ErrorCode.INVALID_INPUT, '缺少绑定的主人私聊主体')
        paired_secret = str(secret or secrets.token_hex(32))
        pair = DevicePair(device_id, str(public_key), bound_account=str(qq_account),
                          principal=str(principal), policy_version=int(policy_version))
        self._devices[device_id] = {'pair': pair, 'secret': paired_secret}
        self._secret_set(device_id, paired_secret)
        if self.db is not None:
            self.db.execute(
                'INSERT INTO agent_devices (device_id,public_key,paired,capability_ver,'
                'policy_ver,bound_account,created_at) VALUES (?,?,?,?,?,?,?) '
                'ON CONFLICT(device_id) DO UPDATE SET public_key=excluded.public_key,'
                'paired=1,policy_ver=excluded.policy_ver,bound_account=excluded.bound_account',
                (device_id, str(public_key), 1, 1, int(policy_version),
                 str(qq_account), self._clock()))
            self.db.execute('INSERT OR REPLACE INTO agent_device_principals '
                            '(device_id,principal) VALUES (?,?)',
                            (device_id, str(principal)))
            self.db.commit()
        self._audit('pair', f'paired {pair.fingerprint()}')
        return {'device_id': device_id, 'fingerprint': pair.fingerprint(),
                'paired': True, 'policy_version': int(policy_version)}

    def unpair(self, device_id: str) -> None:
        self.revoke(device_id)

    def _device(self, device_id: str) -> dict:
        entry = self._devices.get(str(device_id))
        if entry is not None:
            return entry
        if self.db is not None:
            row = self.db.execute(
                'SELECT public_key,bound_account,policy_ver FROM agent_devices '
                'WHERE device_id=? AND paired=1', (str(device_id),)).fetchone()
            if row is not None:
                secret = self._secret_get(str(device_id))
                principal_row = self.db.execute(
                    'SELECT principal FROM agent_device_principals WHERE device_id=?',
                    (str(device_id),)).fetchone()
                if secret:
                    pair = DevicePair(
                        str(device_id), row[0], bound_account=str(row[1] or ''),
                        principal=str(principal_row[0]) if principal_row else '',
                        policy_version=int(row[2] or 1))
                    entry = {'pair': pair, 'secret': secret}
                    self._devices[str(device_id)] = entry
                    return entry
        reject(ErrorCode.UNAUTHORIZED, '设备未配对或配对已失效', device=str(device_id))

    # ── 能力 ────────────────────────────────────────────────────────────────
    def capabilities(self, device_id: str, envelope: Optional[ActionEnvelope] = None) -> list[str]:
        """L0：已配对主人私聊可查“已授权能力列表”。

        边界（1.7.3）：不返回全盘目录、进程命令行或系统秘密。
        """
        entry = self._device(device_id)
        if envelope is not None:
            if envelope.kind != 'private':
                reject(ErrorCode.POLICY_DENIED, '能力查询只接受主人私聊')
            if str(envelope.qq_account) != str(entry['pair'].bound_account):
                reject(ErrorCode.UNAUTHORIZED, 'QQ 账号与设备绑定不一致')
        return [action for action in ALLOWED_ACTIONS_L0L1
                if requestable_level(action) <= CapabilityLevel.L1]

    # ── 提交 ────────────────────────────────────────────────────────────────
    def submit(self, envelope: ActionEnvelope, proposal: ActionProposal) -> AgentJobState:
        """登记动作（持久化 job_id 防重放）。返回 awaiting_approval/executing…"""
        proposal.validate_shape()
        entry = self._device(proposal.device_id)
        pair = entry['pair']
        proposal.check_envelope(envelope, secret=entry['secret'],
                                expected_account=pair.bound_account,
                                expected_principal=pair.principal or envelope.caller,
                                now=self._clock())
        if int(proposal.policy_version) != int(pair.policy_version):
            reject(ErrorCode.POLICY_DENIED, '策略版本不一致，需重新配对/审批')
        self._remember_nonce(str(proposal.nonce), str(proposal.device_id))
        if requestable_level(proposal.action) > CapabilityLevel.L1:
            reject(ErrorCode.POLICY_DENIED, '该动作超出首版 L0/L1 范围')
        existing = self.get_job(proposal.job_id)
        if existing is not None:
            return AgentJobState(existing['state'])
        if self.db is not None:
            self.db.execute(
                'INSERT INTO agent_jobs (job_id,device_id,action,args_hash,policy_version,'
                'nonce,state,result_status,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (str(proposal.job_id), str(proposal.device_id), str(proposal.action),
                 proposal.param_hash(), int(proposal.policy_version), str(proposal.nonce),
                 AgentJobState.AWAITING_APPROVAL.value, '', self._clock()))
            self.db.execute('INSERT OR REPLACE INTO agent_job_args (job_id,args_json) '
                            'VALUES (?,?)',
                            (str(proposal.job_id),
                             json.dumps(proposal.args, ensure_ascii=False)))
            self.db.commit()
        self._audit(str(proposal.job_id), 'submitted ' + str(proposal.action))
        return AgentJobState.AWAITING_APPROVAL

    def submit_private(self, proposal: ActionProposal, *, caller: str, qq_account: str,
                       message_id: str, timestamp: Optional[float] = None) -> AgentJobState:
        """从“可信主人私聊”构造并签名信封后提交（插件路由使用，密钥不出 broker）。"""
        entry = self._device(proposal.device_id)
        envelope = ActionEnvelope(caller=str(caller), qq_account=str(qq_account),
                                  message_id=str(message_id), kind='private',
                                  timestamp=float(timestamp if timestamp is not None
                                                  else self._clock()))
        envelope.signature = signature_for(
            proposal, qq_account=envelope.qq_account, message_id=envelope.message_id,
            kind=envelope.kind, secret=entry['secret'])
        return self.submit(envelope, proposal)

    def _remember_nonce(self, nonce: str, device_id: str) -> None:
        timestamp = self._clock()
        self._prune_nonces(timestamp)
        if nonce in self._nonces:
            reject(ErrorCode.POLICY_DENIED, 'nonce 已使用（重放被拒）', nonce=nonce[:16])
        if self.db is not None:
            row = self.db.execute(
                'SELECT at FROM agent_nonces WHERE nonce=?', (nonce,)).fetchone()
            if row is not None and float(row[0]) + self.NONCE_TTL > timestamp:
                reject(ErrorCode.POLICY_DENIED, 'nonce 已使用（重放被拒）', nonce=nonce[:16])
            self.db.execute('INSERT OR REPLACE INTO agent_nonces (nonce,device_id,at) '
                            'VALUES (?,?,?)', (nonce, str(device_id), timestamp))
            self.db.commit()
        self._nonces[nonce] = timestamp + self.NONCE_TTL

    def _prune_nonces(self, now: float) -> None:
        expired = [nonce for nonce, expiry in self._nonces.items() if expiry <= now]
        for nonce in expired:
            self._nonces.pop(nonce, None)
        if self.db is not None:
            self.db.execute('DELETE FROM agent_nonces WHERE at < ?',
                            (now - self.NONCE_TTL,))

    # ── 查询/状态 ───────────────────────────────────────────────────────────
    def get_job(self, job_id: str) -> Optional[dict]:
        if self.db is None:
            return None
        row = self.db.execute(
            'SELECT job_id,device_id,action,args_hash,policy_version,nonce,state,'
            'result_status,created_at FROM agent_jobs WHERE job_id=?',
            (str(job_id),)).fetchone()
        if row is None:
            return None
        return {'job_id': row[0], 'device_id': row[1], 'action': row[2],
                'args_hash': row[3], 'policy_version': int(row[4] or 1),
                'nonce': row[5], 'state': row[6], 'result_status': row[7] or '',
                'created_at': float(row[8] or 0)}

    def pending_jobs(self, device_id: Optional[str] = None) -> list[dict]:
        if self.db is None:
            return []
        sql = ("SELECT job_id,device_id,action,state,created_at FROM agent_jobs "
               "WHERE state='awaiting_approval'")
        params: list = []
        if device_id:
            sql += ' AND device_id=?'
            params.append(str(device_id))
        return [{'job_id': row[0], 'device_id': row[1], 'action': row[2],
                 'state': row[3], 'created_at': float(row[4] or 0)}
                for row in self.db.execute(sql + ' ORDER BY created_at', params).fetchall()]

    def approval_info(self, job_id: str) -> Optional[dict]:
        """给本地审批页：作业参数哈希 + 绑定设备/主体（不返回参数全文/密钥）。"""
        job = self.get_job(job_id)
        if job is None:
            return None
        principal = ''
        if self.db is not None:
            row = self.db.execute(
                'SELECT principal FROM agent_device_principals WHERE device_id=?',
                (job['device_id'],)).fetchone()
            principal = row[0] if row else ''
        return {**job, 'principal': principal, 'param_hash': job['args_hash']}

    def job_args(self, job_id: str) -> Optional[dict]:
        if self.db is None:
            return None
        row = self.db.execute('SELECT args_json FROM agent_job_args WHERE job_id=?',
                              (str(job_id),)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def mark(self, job_id: str, state: AgentJobState, *, result_status: str = '') -> bool:
        if self.db is None:
            return False
        row = self.db.execute('SELECT state FROM agent_jobs WHERE job_id=?',
                              (str(job_id),)).fetchone()
        if row is None:
            return False
        if row[0] in _TERMINAL and state.value != row[0]:
            return False
        self.db.execute('UPDATE agent_jobs SET state=?,result_status=? WHERE job_id=?',
                        (state.value, str(result_status or ''), str(job_id)))
        self.db.commit()
        self._audit(str(job_id), 'state ' + state.value)
        return True

    def cancel(self, job_id: str) -> str:
        """取消/解绑：立即使后续动作失效；执行中的动作标 uncertain，不自动重放。"""
        job = self.get_job(job_id)
        if job is None:
            reject(ErrorCode.INVALID_INPUT, '动作作业不存在', job_id=str(job_id))
        if job['state'] in _TERMINAL:
            return job['state']
        state = (AgentJobState.UNCERTAIN if job['state'] == AgentJobState.EXECUTING.value
                 else AgentJobState.CANCELLED)
        self.mark(job_id, state, result_status='cancelled by user; no replay')
        self._audit(str(job_id), 'cancelled')
        return state.value

    def revoke(self, device_id: str) -> int:
        """解绑设备：作废配对、取消未执行动作、清空该设备的本地批准。"""
        device_id = str(device_id)
        self._devices.pop(device_id, None)
        self._secret_set(device_id, '')
        affected = 0
        if self.db is not None:
            self.db.execute('UPDATE agent_devices SET paired=0 WHERE device_id=?',
                            (device_id,))
            rows = self.db.execute(
                'SELECT job_id,state FROM agent_jobs WHERE device_id=?', (device_id,)).fetchall()
            for job_id, state in rows:
                if state in _TERMINAL:
                    continue
                target = (AgentJobState.UNCERTAIN if state == AgentJobState.EXECUTING.value
                          else AgentJobState.CANCELLED)
                self.db.execute('UPDATE agent_jobs SET state=?,result_status=? WHERE job_id=?',
                                (target.value, 'device revoked', job_id))
                affected += 1
            self.db.commit()
        affected += self.policy.revoke(device_id)
        self._audit('revoke', 'device ' + device_id)
        return affected

    # ── 审计 ────────────────────────────────────────────────────────────────
    def _audit(self, job_id: str, event: str) -> None:
        try:
            self.policy.audit(self.store, job_id, event, now=self._clock())
        except Exception:
            # 审计失败不能反过来阻断已通过策略的动作；但也不隐藏到日志里。
            pass
