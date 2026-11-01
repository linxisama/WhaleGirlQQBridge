"""T16 Windows 执行器测试：白名单、shell 注入、锁屏、音量/媒体核对。"""
import sys
import time
import unittest

from src.audio_contracts import AgentJobState, ContractError, ErrorCode
from src.local_agent.contracts import ActionProposal
from src.local_agent.policy import ActionPolicy
from src.local_agent.windows_executor import WindowsExecutor


def proposal(**over):
    base = {'job_id': 'job-1', 'device_id': 'dev-1', 'action': 'app.open',
            'args': {'app_id': 'python'}, 'expires_at': time.time() + 60,
            'policy_version': 1, 'nonce': 'nonce-12345678'}
    base.update(over)
    return ActionProposal(**base)


class FakeVolume:
    def __init__(self, current=None):
        self.value = 0
        self._current = current

    def set(self, value):
        self.value = int(value)

    def current(self):
        return self.value if self._current is None else self._current


class FakeMedia:
    def __init__(self, playing=True):
        self.playing = playing
        self.paused = 0

    def pause(self):
        self.paused += 1
        self.playing = False

    def is_playing(self):
        return self.playing


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.policy = ActionPolicy()
        self.launches = []

        def launcher(path, argv):
            self.launches.append((path, argv))
            return None

        self.executor = WindowsExecutor(
            self.policy,
            app_whitelist={'python': {'path': sys.executable,
                                      'arg_keys': ('path', 'values')},
                           'bare': sys.executable},
            app_launcher=launcher,
            volume_backend=FakeVolume(),
            media_backend=FakeMedia())
        self.job = proposal()
        self.policy.grant(self.job.param_hash(), device_id='dev-1',
                          principal='owner', job_id='job-1')

    def test_open_whitelisted_app_passes_structured_args(self):
        state = self.executor.open_app(self.job, 'python',
                                       {'path': 'C:/music/song.wav'})
        self.assertIs(state, AgentJobState.SUCCEEDED)
        self.assertEqual(self.launches[0][0], sys.executable)
        self.assertEqual(self.launches[0][1], ['C:/music/song.wav'])

    def test_unregistered_app_is_refused(self):
        with self.assertRaises(ContractError) as ctx:
            self.executor.open_app(self.job, 'calc')
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_app_id_shell_injection_is_refused(self):
        with self.assertRaises(ContractError) as ctx:
            self.executor.open_app(self.job, 'python & calc')
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_shell_metacharacters_in_args_are_refused(self):
        for value in ('a && calc', 'x; shutdown', '$(whoami)', 'a | b'):
            with self.assertRaises(ContractError) as ctx:
                self.executor.open_app(self.job, 'python', {'path': value})
            self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_unregistered_argument_key_is_refused(self):
        with self.assertRaises(ContractError) as ctx:
            self.executor.open_app(self.job, 'python', {'script': 'x'})
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_plain_path_app_rejects_any_arguments(self):
        with self.assertRaises(ContractError) as ctx:
            self.executor.open_app(self.job, 'bare', {'path': 'x'})
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

    def test_locked_session_blocks_execution(self):
        executor = WindowsExecutor(
            self.policy, app_whitelist={'python': sys.executable},
            app_launcher=lambda path, argv: self.launches.append(path),
            session_probe=lambda: {'locked': True, 'interactive': True})
        with self.assertRaises(ContractError) as ctx:
            executor.open_app(self.job, 'python')
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)
        self.assertEqual(self.launches, [])

    def test_missing_approval_blocks_execution(self):
        job = proposal(job_id='job-2', nonce='nonce-22222222',
                       args={'app_id': 'bare'})
        with self.assertRaises(ContractError) as ctx:
            self.executor.open_app(job, 'python')
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)

    def test_volume_is_set_and_verified(self):
        state = self.executor.set_volume(self.job, 42)
        self.assertIs(state, AgentJobState.SUCCEEDED)
        self.assertEqual(self.executor.volume_backend.value, 42)
        with self.assertRaises(ContractError):
            self.executor.set_volume(self.job, 101)

    def test_volume_mismatch_is_uncertain(self):
        executor = WindowsExecutor(self.policy, volume_backend=FakeVolume(current=10))
        self.assertIs(executor.set_volume(self.job, 80), AgentJobState.UNCERTAIN)

    def test_volume_without_backend_is_unsupported(self):
        executor = WindowsExecutor(self.policy)
        with self.assertRaises(ContractError) as ctx:
            executor.set_volume(self.job, 10)
        self.assertEqual(ctx.exception.code, ErrorCode.CAPABILITY_UNSUPPORTED)

    def test_pause_media_checks_real_state(self):
        self.assertIs(self.executor.pause_media(self.job), AgentJobState.SUCCEEDED)
        self.assertEqual(self.executor.media_backend.paused, 1)

    def test_health_hides_secrets_and_reports_lock(self):
        executor = WindowsExecutor(self.policy,
                                   session_probe=lambda: {'locked': True, 'interactive': True})
        health = executor.health('dev-1')
        self.assertFalse(health['online'])
        self.assertTrue(health['locked'])
        self.assertNotIn('path', health)

    def test_revoke_clears_approvals_immediately(self):
        self.assertEqual(self.executor.revoke('dev-1'), 1)
        with self.assertRaises(ContractError):
            self.executor.open_app(self.job, 'python')


if __name__ == '__main__':
    unittest.main()
