"""T16：LocalAgent 独立进程运行时（状态机 + 心跳 + 任务取消 + 急停）。

设计（1.7.2 / 1.7.4）：
- 独立进程，以当前登录用户权限跑；不要求管理员（T16 验收）。
- IPC 方法 agent.health / capabilities / submit / get_job / cancel / revoke；
  心跳约 10s，连续 3 次丢失标离线；短动作执行预算约 15s。
- 任务条 + 托盘停止入口 + 全局急停（1.7.4）：急停在“下一动作前”生效。
- 重启不重放执行中动作；取消/断线标 uncertain 并核对真实状态。

本模块只做“进程侧状态与生命周期”；具体动作委托给 WindowsExecutor，
策略闸始终在 policy.authorize（所有动作执行器，1.7.1）。

实现补充（2026-10-20）：动作执行放到线程里并加 15s 预算；成功后作废该次批准，
失败/超时写回 agent_jobs；急停只清未执行授权并阻止后续动作，不撤销已完成动作。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..audio_contracts import AgentJobState, ContractError, ErrorCode, reject
from .broker import LocalAgentBroker
from .contracts import ActionProposal, requestable_level
from .policy import ActionPolicy
from .windows_executor import WindowsExecutor


class AgentRuntime:
    """一个本地 Agent 设备的运行状态机（独立进程内一个实例）。"""

    HEARTBEAT_SECONDS = 10
    OFFLINE_AFTER_LOST = 3
    SHORT_ACTION_BUDGET = 15.0

    def __init__(self, device_id: str, db=None, store=None, *,
                 policy: Optional[ActionPolicy] = None,
                 broker: Optional[LocalAgentBroker] = None,
                 executor: Optional[WindowsExecutor] = None, clock=None):
        self.device_id = str(device_id)
        self._clock = clock or time.time
        self.policy = policy or ActionPolicy()
        self.broker = broker or LocalAgentBroker(db=db, store=store, policy=self.policy,
                                                 clock=self._clock)
        self.executor = executor or WindowsExecutor(self.policy, store=store or db)
        self.online = False
        self._last_heartbeat = self._clock()
        self._stopped = False

    # ── 心跳 ────────────────────────────────────────────────────────────────
    def heartbeat(self, now: Optional[float] = None) -> bool:
        """收到心跳置在线；超过 3×10s 没有心跳标离线（1.7.2）。"""
        current = self._clock() if now is None else float(now)
        if self._stopped:
            self.online = False
            return False
        gap = current - self._last_heartbeat
        self._last_heartbeat = current
        if gap > self.HEARTBEAT_SECONDS * self.OFFLINE_AFTER_LOST:
            self.online = False
            return False
        self.online = True
        return True

    # ── 执行 ────────────────────────────────────────────────────────────────
    async def run_action(self, proposal: ActionProposal) -> AgentJobState:
        """执行一个已登记动作：先过策略闸，再执行，执行完核对真实状态。"""
        if self._stopped:
            return AgentJobState.UNCERTAIN
        if requestable_level(proposal.action) > 1:
            reject(ErrorCode.POLICY_DENIED, '首版只允许 L0/L1 动作')
        job = self.broker.get_job(proposal.job_id)
        if job is None:
            reject(ErrorCode.UNAUTHORIZED, '动作未登记（缺少 broker 提交记录）')
        if job.get('device_id') != self.device_id:
            reject(ErrorCode.UNAUTHORIZED, '动作作业不属于本设备')
        if job.get('state') in (AgentJobState.CANCELLED.value, AgentJobState.UNCERTAIN.value,
                                AgentJobState.SUCCEEDED.value, AgentJobState.FAILED.value):
            return AgentJobState(job['state'])
        if requestable_level(proposal.action) == 0:
            # L0 只读：不需要本地审批，但仍要在主人私聊路径上（由 broker 校验）。
            return await self._run_readonly(proposal)
        self.policy.authorize(proposal, now=self._clock())
        self.broker.mark(proposal.job_id, AgentJobState.EXECUTING)
        try:
            state = await asyncio.wait_for(
                asyncio.to_thread(self._dispatch, proposal),
                timeout=self.SHORT_ACTION_BUDGET)
        except asyncio.TimeoutError:
            self.broker.mark(proposal.job_id, AgentJobState.UNCERTAIN,
                             result_status='动作执行超时，真实状态待核实')
            self.policy.audit(self.broker.store, proposal.job_id, 'execution timeout')
            return AgentJobState.UNCERTAIN
        except ContractError as exc:
            self.broker.mark(proposal.job_id, AgentJobState.FAILED,
                             result_status=exc.code.value)
            self.policy.audit(self.broker.store, proposal.job_id,
                              'denied ' + exc.code.value)
            return AgentJobState.FAILED
        except Exception as exc:  # noqa: BLE001 —— 执行器异常不能把进程带崩
            self.broker.mark(proposal.job_id, AgentJobState.UNCERTAIN,
                             result_status='执行异常：' + type(exc).__name__)
            return AgentJobState.UNCERTAIN
        if state is AgentJobState.SUCCEEDED:
            self.policy.consume(proposal.param_hash())
        self.broker.mark(proposal.job_id, state)
        self.policy.audit(self.broker.store, proposal.job_id, 'executed ' + state.value)
        return state

    def _dispatch(self, proposal: ActionProposal) -> AgentJobState:
        action = proposal.action
        args = proposal.args or {}
        if action == 'app.open':
            app_id = str(args.get('app_id') or '')
            if not app_id:
                reject(ErrorCode.INVALID_INPUT, 'app.open 缺少 app_id')
            extra = {key: value for key, value in args.items() if key != 'app_id'}
            return self.executor.open_app(proposal, app_id, extra or None)
        if action == 'volume.set':
            return self.executor.set_volume(proposal, args.get('level'))
        if action == 'media.pause':
            return self.executor.pause_media(proposal)
        reject(ErrorCode.POLICY_DENIED, f'动作未实现：{action}')

    async def _run_readonly(self, proposal: ActionProposal) -> AgentJobState:
        if proposal.action == 'agent.health':
            result = self.executor.health(self.device_id)
        else:
            result = {'capabilities': self.broker.capabilities(self.device_id)}
        self.broker.mark(proposal.job_id, AgentJobState.SUCCEEDED,
                         result_status=str(result)[:200])
        self.policy.audit(self.broker.store, proposal.job_id, 'readonly ' + proposal.action)
        return AgentJobState.SUCCEEDED

    # ── 急停/取消 ───────────────────────────────────────────────────────────
    def stop(self) -> int:
        """本机急停：清空未执行授权；已完成动作无法保证撤销（1.7.4）。"""
        self._stopped = True
        self.online = False
        cleared = self.policy.revoke()
        self.policy.audit(self.broker.store, 'emergency-stop', 'all approvals cleared')
        return cleared

    def resume(self) -> None:
        """解除急停（仍需重新配对/批准才会再执行动作）。"""
        self._stopped = False
        self._last_heartbeat = self._clock()

    def cancel_job(self, job_id: str) -> str:
        """不自动重放；执行中的标 uncertain 并核对真实状态。"""
        state = self.broker.cancel(job_id)
        self.policy.audit(self.broker.store, job_id, 'cancelled ' + str(state))
        return state

    @property
    def stopped(self) -> bool:
        return self._stopped
