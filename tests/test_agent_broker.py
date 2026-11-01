"""T15 Broker 测试：配对、可信信封、签名/nonce、取消/解绑、持久化。"""
import sqlite3
import time
import unittest

from src.audio_contracts import AgentJobState, ContractError, ErrorCode
from src.local_agent.broker import LocalAgentBroker
from src.local_agent.contracts import ActionEnvelope, ActionProposal, signature_for

SECRET = 'pairing-secret-1'
ACCOUNT = '10001'
OWNER = 'owner-openid'


def proposal(**over):
    base = {'job_id': 'job-1', 'device_id': 'dev-1', 'action': 'app.open',
            'args': {'app_id': 'notepad'}, 'expires_at': time.time() + 60,
            'policy_version': 1, 'nonce': 'nonce-12345678'}
    base.update(over)
    return ActionProposal(**base)


def envelope(job, **over):
    values = {'caller': OWNER, 'qq_account': ACCOUNT, 'message_id': 'msg-1',
              'kind': 'private', 'timestamp': time.time()}
    values.update(over)
    return ActionEnvelope(**values)


def signed(job, **over):
    env = envelope(job, **over)
    env.signature = signature_for(job, qq_account=env.qq_account,
                                  message_id=env.message_id, kind=env.kind,
                                  secret=SECRET)
    return env


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.secrets = {}
        self.broker = LocalAgentBroker(self.db, secret_store=self.secrets)
        self.broker.pair('dev-1', 'pubkey-fingerprint', qq_account=ACCOUNT,
                         principal=OWNER, secret=SECRET)

    def tearDown(self):
        self.db.close()

    def test_pair_returns_fingerprint_not_secret(self):
        result = self.broker.pair('dev-2', 'pubkey-2', qq_account='2',
                                  principal='other', secret='other-secret')
        self.assertNotIn('secret', result)
        self.assertTrue(result['fingerprint'])
        self.assertEqual(self.secrets['dev-2'], 'other-secret')

    def test_unpaired_device_cannot_submit(self):
        job = proposal(device_id='dev-x')
        with self.assertRaises(ContractError) as ctx:
            self.broker.submit(signed(job, **{'message_id': 'm'}), job)
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)

    def test_submit_creates_awaiting_approval_job(self):
        job = proposal()
        state = self.broker.submit(signed(job), job)
        self.assertIs(state, AgentJobState.AWAITING_APPROVAL)
        row = self.broker.get_job('job-1')
        self.assertEqual(row['state'], AgentJobState.AWAITING_APPROVAL.value)
        self.assertEqual(row['args_hash'], job.param_hash())

    def test_wrong_account_or_owner_is_rejected(self):
        job = proposal()
        with self.assertRaises(ContractError) as ctx:
            self.broker.submit(signed(job, qq_account='99999'), job)
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)
        job2 = proposal(job_id='job-2', nonce='nonce-22222222')
        with self.assertRaises(ContractError) as ctx2:
            self.broker.submit(signed(job2, caller='someone-else'), job2)
        self.assertEqual(ctx2.exception.code, ErrorCode.UNAUTHORIZED)

    def test_group_or_quote_message_is_not_authorization(self):
        for kind in ('group', 'quote', 'forward'):
            job = proposal(job_id='job-' + kind, nonce='nonce-' + kind + '123')
            with self.assertRaises(ContractError) as ctx:
                self.broker.submit(signed(job, kind=kind), job)
            self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_bad_signature_and_stale_envelope(self):
        job = proposal()
        env = signed(job)
        env.signature = 'f' * 64
        with self.assertRaises(ContractError) as ctx:
            self.broker.submit(env, job)
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)
        stale = proposal(job_id='job-2', nonce='nonce-22222222')
        with self.assertRaises(ContractError) as ctx2:
            self.broker.submit(signed(stale, timestamp=time.time() - 999), stale)
        self.assertEqual(ctx2.exception.code, ErrorCode.APPROVAL_EXPIRED)

    def test_expired_proposal_is_rejected(self):
        job = proposal(expires_at=time.time() - 1)
        with self.assertRaises(ContractError) as ctx:
            self.broker.submit(signed(job), job)
        self.assertEqual(ctx.exception.code, ErrorCode.APPROVAL_EXPIRED)

    def test_nonce_replay_is_rejected(self):
        job = proposal()
        self.broker.submit(signed(job), job)
        replay = proposal(job_id='job-2')
        with self.assertRaises(ContractError) as ctx:
            self.broker.submit(signed(replay), replay)
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_forged_approval_arguments_are_rejected(self):
        job = proposal(args={'app_id': 'notepad', 'approved': True})
        with self.assertRaises(ContractError) as ctx:
            self.broker.submit(signed(job), job)
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_higher_level_action_is_rejected(self):
        job = proposal(action='file.write')
        with self.assertRaises(ContractError) as ctx:
            self.broker.submit(signed(job), job)
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_capabilities_lists_only_l0_l1(self):
        actions = self.broker.capabilities('dev-1')
        self.assertIn('app.open', actions)
        self.assertIn('agent.health', actions)
        self.assertNotIn('file.write', actions)
        with self.assertRaises(ContractError):
            self.broker.capabilities('dev-unknown')
        with self.assertRaises(ContractError) as ctx:
            self.broker.capabilities('dev-1', ActionEnvelope(
                caller=OWNER, qq_account=ACCOUNT, message_id='m', kind='group'))
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_cancel_awaiting_becomes_cancelled(self):
        job = proposal()
        self.broker.submit(signed(job), job)
        self.assertEqual(self.broker.cancel('job-1'), AgentJobState.CANCELLED.value)
        self.assertEqual(self.broker.get_job('job-1')['state'],
                         AgentJobState.CANCELLED.value)

    def test_cancel_executing_becomes_uncertain(self):
        job = proposal()
        self.broker.submit(signed(job), job)
        self.broker.mark('job-1', AgentJobState.EXECUTING)
        self.assertEqual(self.broker.cancel('job-1'), AgentJobState.UNCERTAIN.value)
        self.assertEqual(self.broker.get_job('job-1')['state'],
                         AgentJobState.UNCERTAIN.value)

    def test_revoke_invalidates_device_and_pending_jobs(self):
        job = proposal()
        self.broker.submit(signed(job), job)
        self.assertGreaterEqual(self.broker.revoke('dev-1'), 1)
        self.assertEqual(self.broker.get_job('job-1')['state'],
                         AgentJobState.CANCELLED.value)
        again = proposal(job_id='job-2', nonce='nonce-22222222')
        with self.assertRaises(ContractError):
            self.broker.submit(signed(again, **{'message_id': 'msg-2'}), again)

    def test_pairing_survives_restart_with_shared_secret_store(self):
        job = proposal()
        self.broker.submit(signed(job), job)
        reopened = LocalAgentBroker(self.db, secret_store=self.secrets)
        again = proposal(job_id='job-2', nonce='nonce-22222222')
        env = signed(again, **{'message_id': 'msg-2'})
        self.assertIs(reopened.submit(env, again), AgentJobState.AWAITING_APPROVAL)


if __name__ == '__main__':
    unittest.main()
