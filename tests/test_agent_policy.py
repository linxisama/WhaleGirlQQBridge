"""T15 策略测试：审批绑定、改参重审、过期、设备/版本、审计。"""
import sqlite3
import time
import unittest

from src.audio_contracts import ContractError, ErrorCode
from src.audio_contracts_db import initialize
from src.local_agent.contracts import ActionProposal
from src.local_agent.policy import ActionPolicy


def proposal(**over):
    base = {'job_id': 'job-1', 'device_id': 'dev-1', 'action': 'app.open',
            'args': {'app_id': 'notepad'}, 'expires_at': time.time() + 60,
            'policy_version': 1, 'nonce': 'nonce-12345678'}
    base.update(over)
    return ActionProposal(**base)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ActionPolicy(policy_version=1, approval_ttl=120)
        self.db = sqlite3.connect(':memory:')
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def grant(self, job, **over):
        values = {'device_id': 'dev-1', 'principal': 'owner-openid'}
        values.update(over)
        return self.policy.grant(job.param_hash(), **values)

    def test_without_approval_is_unauthorized(self):
        with self.assertRaises(ContractError) as ctx:
            self.policy.authorize(proposal())
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)

    def test_approval_allows_execution(self):
        job = proposal()
        self.grant(job)
        self.assertTrue(self.policy.authorize(job))

    def test_argument_change_requires_new_approval(self):
        job = proposal()
        self.grant(job)
        changed = proposal(args={'app_id': 'calc'})
        with self.assertRaises(ContractError) as ctx:
            self.policy.authorize(changed)
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)

    def test_wrong_device_is_rejected(self):
        job = proposal()
        self.grant(job, device_id='dev-2')
        with self.assertRaises(ContractError) as ctx:
            self.policy.authorize(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)

    def test_expired_approval_and_proposal(self):
        job = proposal(expires_at=time.time() + 60)
        self.grant(job, expires_at=time.time() - 1)
        with self.assertRaises(ContractError) as ctx:
            self.policy.authorize(job)
        self.assertEqual(ctx.exception.code, ErrorCode.APPROVAL_EXPIRED)
        future = proposal(expires_at=time.time() - 1)
        self.grant(future)
        with self.assertRaises(ContractError) as ctx2:
            self.policy.authorize(future)
        self.assertEqual(ctx2.exception.code, ErrorCode.APPROVAL_EXPIRED)

    def test_policy_version_mismatch(self):
        job = proposal(policy_version=2)
        self.grant(job)
        with self.assertRaises(ContractError) as ctx:
            self.policy.authorize(job)
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)
        with self.assertRaises(ContractError):
            self.policy.grant('x' * 32, device_id='dev-1', principal='p',
                              policy_version=99)

    def test_higher_level_actions_are_denied(self):
        for action in ('file.write', 'window.click', 'agent.submit'):
            job = proposal(action=action)
            with self.assertRaises(ContractError) as ctx:
                self.policy.authorize(job)
            self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_consume_makes_approval_one_shot(self):
        job = proposal()
        self.grant(job)
        self.assertTrue(self.policy.authorize(job))
        self.assertTrue(self.policy.consume(job.param_hash()))
        self.assertFalse(self.policy.consume(job.param_hash()))
        with self.assertRaises(ContractError):
            self.policy.authorize(job)

    def test_revoke_clears_all_or_one_device(self):
        first = proposal(job_id='a')
        second = proposal(job_id='b', device_id='dev-2', args={'app_id': 'calc'})
        self.grant(first)
        self.grant(second, device_id='dev-2')
        self.assertEqual(self.policy.revoke('dev-1'), 1)
        with self.assertRaises(ContractError):
            self.policy.authorize(first)
        self.assertTrue(self.policy.authorize(second))
        self.assertEqual(self.policy.revoke(), 1)
        self.assertEqual(self.policy.pending(), [])

    def test_audit_is_written_and_sanitized(self):
        self.policy.audit(self.db, 'job-1', 'submitted app.open')
        self.policy.audit(self.db, 'job-1', 'api_key=secret-value')
        rows = self.db.execute('SELECT job_id,event FROM agent_audit ORDER BY id').fetchall()
        self.assertEqual(rows[0][0], 'job-1')
        self.assertIn('app.open', rows[0][1])
        self.assertNotIn('secret-value', rows[1][1])


if __name__ == '__main__':
    unittest.main()
