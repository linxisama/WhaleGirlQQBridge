"""T16 运行时测试：心跳、审批执行、急停、超时、取消不重放。"""
import asyncio
import sqlite3
import sys
import time
import unittest

from src.audio_contracts import AgentJobState, ContractError, ErrorCode
from src.local_agent.broker import LocalAgentBroker
from src.local_agent.contracts import ActionEnvelope, ActionProposal, signature_for
from src.local_agent.policy import ActionPolicy
from src.local_agent.runtime import AgentRuntime
from src.local_agent.windows_executor import WindowsExecutor

SECRET = 'runtime-secret'
ACCOUNT = '10001'
OWNER = 'owner-openid'


def proposal(**over):
    base = {'job_id': 'job-1', 'device_id': 'dev-1', 'action': 'app.open',
            'args': {'app_id': 'python'}, 'expires_at': time.time() + 60,
            'policy_version': 1, 'nonce': 'nonce-12345678'}
    base.update(over)
    return ActionProposal(**base)


def envelope(job):
    env = ActionEnvelope(caller=OWNER, qq_account=ACCOUNT, message_id='msg-1',
                         kind='private', timestamp=1000.0)
    env.signature = signature_for(job, qq_account=env.qq_account,
                                  message_id=env.message_id, kind=env.kind,
                                  secret=SECRET)
    return env


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.db = sqlite3.connect(':memory:')
        self.policy = ActionPolicy()
        self.broker = LocalAgentBroker(self.db, policy=self.policy,
                                       clock=lambda: self.now)
        self.broker.pair('dev-1', 'fingerprint', qq_account=ACCOUNT,
                         principal=OWNER, secret=SECRET)
        self.launches = []

        def launcher(path, argv):
            self.launches.append((path, argv))

        self.executor = WindowsExecutor(
            self.policy,
            app_whitelist={'python': {'path': sys.executable, 'arg_keys': ('path',)}},
            app_launcher=launcher,
            session_probe=lambda: {'locked': False, 'interactive': True},
            volume_backend=None)
        self.runtime = AgentRuntime('dev-1', db=self.db, policy=self.policy,
                                    broker=self.broker, executor=self.executor,
                                    clock=lambda: self.now)

    async def asyncTearDown(self):
        self.db.close()

    def submit(self, job, *, grant=True):
        self.broker.submit(envelope(job), job)
        if grant:
            self.policy.grant(job.param_hash(), device_id='dev-1', principal=OWNER,
                              expires_at=time.time() + 120, job_id=job.job_id)
        return job

    async def test_approved_action_runs_once_and_consumes_approval(self):
        job = self.submit(proposal())
        state = await self.runtime.run_action(job)
        self.assertIs(state, AgentJobState.SUCCEEDED)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.broker.get_job('job-1')['state'],
                         AgentJobState.SUCCEEDED.value)
        self.assertIs(await self.runtime.run_action(job), AgentJobState.SUCCEEDED)
        self.assertEqual(len(self.launches), 1)

    async def test_missing_approval_is_rejected_and_not_executed(self):
        job = self.submit(proposal(), grant=False)
        with self.assertRaises(ContractError) as ctx:
            await self.runtime.run_action(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)
        self.assertEqual(self.launches, [])

    async def test_unregistered_job_is_rejected(self):
        job = proposal()
        self.policy.grant(job.param_hash(), device_id='dev-1', principal=OWNER,
                          expires_at=time.time() + 120)
        with self.assertRaises(ContractError) as ctx:
            await self.runtime.run_action(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)

    async def test_emergency_stop_blocks_next_action(self):
        job = self.submit(proposal())
        self.runtime.stop()
        self.assertIs(await self.runtime.run_action(job), AgentJobState.UNCERTAIN)
        self.assertEqual(self.launches, [])
        self.runtime.resume()
        self.assertTrue(self.runtime._stopped is False)

    async def test_timeout_marks_uncertain(self):
        def slow_launcher(path, argv):
            time.sleep(0.2)

        executor = WindowsExecutor(
            self.policy,
            app_whitelist={'python': {'path': sys.executable, 'arg_keys': ('path',)}},
            app_launcher=slow_launcher,
            session_probe=lambda: {'locked': False, 'interactive': True})
        runtime = AgentRuntime('dev-1', db=self.db, policy=self.policy,
                               broker=self.broker, executor=executor,
                               clock=lambda: self.now)
        runtime.SHORT_ACTION_BUDGET = 0.05
        job = self.submit(proposal())
        state = await runtime.run_action(job)
        self.assertIs(state, AgentJobState.UNCERTAIN)
        self.assertEqual(self.broker.get_job('job-1')['state'],
                         AgentJobState.UNCERTAIN.value)

    async def test_locked_session_marks_failed(self):
        executor = WindowsExecutor(
            self.policy,
            app_whitelist={'python': {'path': sys.executable, 'arg_keys': ('path',)}},
            app_launcher=lambda path, argv: self.launches.append(path),
            session_probe=lambda: {'locked': True, 'interactive': True})
        runtime = AgentRuntime('dev-1', db=self.db, policy=self.policy,
                               broker=self.broker, executor=executor,
                               clock=lambda: self.now)
        job = self.submit(proposal())
        state = await runtime.run_action(job)
        self.assertIs(state, AgentJobState.FAILED)
        self.assertEqual(self.launches, [])

    async def test_cancel_awaiting_job_is_not_replayed(self):
        job = self.submit(proposal())
        self.assertEqual(self.runtime.cancel_job('job-1'), AgentJobState.CANCELLED.value)
        self.assertIs(await self.runtime.run_action(job), AgentJobState.CANCELLED)
        self.assertEqual(self.launches, [])

    async def test_l0_health_needs_no_local_approval(self):
        job = proposal(job_id='health-1', action='agent.health', args={},
                       nonce='nonce-health-1')
        self.submit(job, grant=False)
        state = await self.runtime.run_action(job)
        self.assertIs(state, AgentJobState.SUCCEEDED)

    def test_heartbeat_marks_online_offline_and_back(self):
        self.assertTrue(self.runtime.heartbeat(1005.0))
        self.assertTrue(self.runtime.heartbeat(1016.0))
        self.assertFalse(self.runtime.heartbeat(1050.0))
        self.assertFalse(self.runtime.online)
        self.assertTrue(self.runtime.heartbeat(1055.0))
        self.assertTrue(self.runtime.online)

    def test_stopped_runtime_never_reports_online(self):
        self.runtime.stop()
        self.assertFalse(self.runtime.heartbeat(1005.0))
        self.assertFalse(self.runtime.online)


if __name__ == '__main__':
    unittest.main()
