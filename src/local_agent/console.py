"""T16/T17：本地 Agent 控制台（设备、待批动作、批准/拒绝、急停、审计）。

控制台只做“本地人机确认”和状态汇总；所有动作仍由 ``ActionPolicy`` 闸门放行，
批准不会被聊天/模型路径自动触发。首版不通过 HTTP 暴露配对入口，配对由本机
初始化代码（或后续安装向导）调用 ``pair()`` 完成。
"""
from __future__ import annotations

from typing import Optional

from ..audio_contracts import AgentJobState, ErrorCode, reject
from .broker import LocalAgentBroker
from .contracts import ActionProposal
from .policy import ActionPolicy
from .runtime import AgentRuntime
from .windows_executor import WindowsExecutor


class AgentConsole:
    def __init__(self, root, config, db, *, secret_store=None,
                 app_whitelist: Optional[dict] = None, clock=None):
        self.root = root
        self.config = config
        self.db = db
        self.policy = ActionPolicy()
        self.broker = LocalAgentBroker(db, secret_store=secret_store, clock=clock)
        self.policy = self.broker.policy
        self.executor = WindowsExecutor(self.policy, store=db,
                                        app_whitelist=app_whitelist)
        self._runtimes: dict[str, AgentRuntime] = {}
        self._stopped = False

    # ── 设备 ────────────────────────────────────────────────────────────────
    def pair(self, device_id: str, public_key: str, *, qq_account: str,
             principal: str, secret: str = '', policy_version: int = 1) -> dict:
        for runtime in self._runtimes.values():
            runtime.stop()
        self._runtimes.clear()
        return self.broker.pair(device_id, public_key, qq_account=qq_account,
                                principal=principal, secret=secret or None,
                                policy_version=policy_version)

    def devices(self) -> list[dict]:
        if self.db is None:
            return []
        rows = self.db.execute(
            'SELECT device_id,public_key,paired,policy_ver,bound_account FROM agent_devices '
            'ORDER BY created_at').fetchall()
        return [{'device_id': row[0], 'fingerprint': str(row[1] or '')[:16],
                 'paired': bool(row[2]), 'policy_version': int(row[3] or 1),
                 'bound_account': row[4] or ''} for row in rows]

    def runtime_for(self, device_id: str) -> AgentRuntime:
        device_id = str(device_id)
        if device_id not in self._runtimes:
            self._runtimes[device_id] = AgentRuntime(
                device_id, db=self.db, policy=self.policy, broker=self.broker,
                executor=self.executor)
        return self._runtimes[device_id]

    # ── 待批动作 ────────────────────────────────────────────────────────────
    def pending(self) -> list[dict]:
        items = []
        for job in self.broker.pending_jobs():
            info = self.broker.approval_info(job['job_id']) or {}
            args = self.broker.job_args(job['job_id']) or {}
            items.append({**job,
                          'principal': info.get('principal', ''),
                          'arg_summary': self._summarize_args(args),
                          'policy_version': info.get('policy_version', 1)})
        return items

    @staticmethod
    def _summarize_args(args: dict) -> str:
        """脱敏摘要：只显示键与短值，不展示整段路径/文本。"""
        parts = []
        for key in sorted(args):
            value = args[key]
            text = str(value)
            if len(text) > 40:
                text = text[:37] + '...'
            if key.lower() in {'token', 'secret', 'password', 'api_key'}:
                text = '***'
            parts.append(f'{key}={text}')
        return ', '.join(parts)[:200]

    def approve(self, job_id: str) -> dict:
        """本地点击“允许”→ 授权该参数哈希并立即尝试执行。"""
        info = self.broker.approval_info(job_id)
        if info is None:
            reject(ErrorCode.INVALID_INPUT, '待批动作不存在')
        if info['state'] != AgentJobState.AWAITING_APPROVAL.value:
            reject(ErrorCode.INVALID_INPUT, '该动作已不是待批状态', state=info['state'])
        if self._stopped:
            reject(ErrorCode.POLICY_DENIED, '急停未解除，拒绝批准新动作')
        args = self.broker.job_args(job_id) or {}
        proposal = ActionProposal(job_id=job_id, device_id=info['device_id'],
                                  action=info['action'], args=args,
                                  policy_version=int(info['policy_version']),
                                  nonce=str(info['nonce']))
        self.policy.grant(proposal.param_hash(), device_id=proposal.device_id,
                          principal=info.get('principal', ''), job_id=job_id)
        runtime = self.runtime_for(proposal.device_id)
        if self._stopped:
            runtime.stop()
        state = self._run_sync(runtime, proposal)
        return {'job_id': job_id, 'state': getattr(state, 'value', str(state)),
                'action': proposal.action}

    @staticmethod
    def _run_sync(runtime: AgentRuntime, proposal: ActionProposal):
        import asyncio

        async def run():
            return await runtime.run_action(proposal)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(run())
        # 已在事件循环里（FastAPI）：交给独立线程跑，避免嵌套事件循环。
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, run()).result()

    def reject(self, job_id: str) -> dict:
        state = self.broker.cancel(job_id)
        return {'job_id': job_id, 'state': state}

    # ── 急停 ────────────────────────────────────────────────────────────────
    def emergency_stop(self) -> dict:
        self._stopped = True
        cleared = self.policy.revoke()
        for runtime in self._runtimes.values():
            runtime.stop()
        self.policy.audit(self.db, 'emergency-stop', 'console emergency stop')
        return {'stopped': True, 'cleared_approvals': cleared}

    def resume(self) -> dict:
        self._stopped = False
        for runtime in self._runtimes.values():
            runtime.resume()
        return {'stopped': False}

    @property
    def stopped(self) -> bool:
        return self._stopped

    # ── 审计 ────────────────────────────────────────────────────────────────
    def audit(self, limit: int = 50) -> list[dict]:
        if self.db is None:
            return []
        rows = self.db.execute(
            'SELECT job_id,event,at FROM agent_audit ORDER BY at DESC LIMIT ?',
            (max(1, min(int(limit), 500)),)).fetchall()
        return [{'job_id': row[0], 'event': row[1], 'at': float(row[2] or 0)}
                for row in rows]

    def status(self) -> dict:
        devices = self.devices()
        capabilities: list[str] = []
        if devices:
            try:
                capabilities = sorted(self.broker.capabilities(devices[0]['device_id']))
            except Exception:
                capabilities = []
        return {'devices': devices, 'pending': len(self.broker.pending_jobs()),
                'stopped': self._stopped,
                'policy_version': self.policy.policy_version,
                'capabilities': capabilities}
