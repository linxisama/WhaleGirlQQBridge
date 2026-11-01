"""T04 调度器测试：队列限额、取消、超时、重启恢复。"""
import asyncio
import hashlib
import sqlite3
import time
import unittest

import src.audio_scheduler  # noqa: F401  （确保包内导入路径可用）
from src.audio_contracts import (
    AudioJobRequest, AudioMode, ContractError, ErrorCode, JobState, job_parameter_hash,
)
from src.audio_jobs import AudioJobStore
from src.audio_scheduler import AudioScheduler


def request(client_request_id='req-1', **over):
    base = {'client_request_id': client_request_id, 'mode': AudioMode.TTS, 'text': '你好'}
    base.update(over)
    return AudioJobRequest(**base)


def add_job(store, req, *, domain='private:1', principal='p', state=None):
    param_hash = job_parameter_hash(req, subject_domain=domain, principal=principal)
    job_id = hashlib.sha256(param_hash.encode()).hexdigest()[:24]
    store.enqueue(req, subject_domain=domain, principal=principal,
                  job_id=job_id, param_hash=param_hash)
    if state is not None and state != JobState.QUEUED:
        store.mark(job_id, state)
    return job_id


class FakeAdapter:
    def __init__(self, audio=b'RIFF-fake-wave'):
        self.audio = audio
        self.calls = 0

    async def synthesize(self, request, profile):
        self.calls += 1
        return self.audio


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.store = AudioJobStore(self.db)
        self.adapter = FakeAdapter()
        self.scheduler = AudioScheduler(self.store, lambda req: (self.adapter, {}))

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.db.close()

    async def wait_state(self, job_id, state, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.store.get(job_id)
            if row and row['state'] == state:
                return row
            await asyncio.sleep(0.01)
        self.fail(f'{job_id} 未到达 {state}，当前 {self.store.get(job_id)}')

    async def test_short_job_runs_to_ready(self):
        job_id = add_job(self.store, request())
        result = await self.scheduler.submit_short(job_id, request())
        self.assertTrue(result['queued'])
        await self.wait_state(job_id, JobState.READY.value)
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.scheduler.peek_result(job_id), b'RIFF-fake-wave')

    async def test_second_submit_is_duplicate(self):
        self.scheduler._ensure_workers = lambda: None
        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request())
        again = await self.scheduler.submit_short(job_id, request())
        self.assertTrue(again['duplicate'])
        self.assertEqual(self.scheduler._short_q.qsize(), 1)

    async def test_queue_full_has_explicit_error(self):
        self.scheduler._ensure_workers = lambda: None
        self.scheduler._short_q = asyncio.Queue(maxsize=1)
        first = add_job(self.store, request(text='a'))
        second = add_job(self.store, request(text='b'))
        await self.scheduler.submit_short(first, request(text='a'))
        with self.assertRaises(ContractError) as ctx:
            await self.scheduler.submit_short(second, request(text='b'))
        self.assertEqual(ctx.exception.code, ErrorCode.QUEUE_FULL)
        self.assertEqual(self.store.get(second)['state'], JobState.FAILED.value)

    async def test_per_session_limit(self):
        self.scheduler._ensure_workers = lambda: None
        add_job(self.store, request(text='a'))
        add_job(self.store, request(text='b'))
        third = add_job(self.store, request(text='c'))
        with self.assertRaises(ContractError) as ctx:
            await self.scheduler.submit_short(third, request(text='c'))
        self.assertEqual(ctx.exception.code, ErrorCode.QUEUE_FULL)

    async def test_cancel_queued_job_never_runs(self):
        self.scheduler._ensure_workers = lambda: None
        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request())
        result = await self.scheduler.cancel(job_id)
        self.assertEqual(result['state'], JobState.CANCELLED.value)
        self.assertEqual(self.store.get(job_id)['state'], JobState.CANCELLED.value)
        self.assertEqual(self.adapter.calls, 0)

    async def test_cancel_running_job_discards_late_result(self):
        started = asyncio.Event()

        async def slow(request_, adapter, profile):
            started.set()
            await asyncio.sleep(5)
            return b'late'

        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request(), slow)
        await started.wait()
        await self.wait_state(job_id, JobState.RUNNING.value)
        await self.scheduler.cancel(job_id)
        await self.wait_state(job_id, JobState.CANCELLED.value)
        await asyncio.sleep(0.05)
        self.assertEqual(self.store.get(job_id)['state'], JobState.CANCELLED.value)

    async def test_timeout_becomes_deadline_exceeded(self):
        async def too_slow(request_, adapter, profile):
            await asyncio.sleep(1)
            return b'late'

        self.scheduler._short_timeout = 0.05
        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request(), too_slow)
        row = await self.wait_state(job_id, JobState.FAILED.value)
        self.assertEqual(row['error_code'], ErrorCode.DEADLINE_EXCEEDED.value)

    async def test_unknown_generation_keeps_job_unknown(self):
        async def unknown(request_, adapter, profile):
            raise ContractError(ErrorCode.GENERATION_UNKNOWN, '供应商结果未知')

        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request(), unknown)
        row = await self.wait_state(job_id, JobState.UNKNOWN.value)
        self.assertEqual(row['error_code'], ErrorCode.GENERATION_UNKNOWN.value)

    async def test_song_uses_its_own_queue(self):
        job_id = add_job(self.store, request(mode=AudioMode.SONG_GENERATE, text=None,
                                            lyrics='啦啦'))
        result = await self.scheduler.submit_song(job_id, request())
        self.assertTrue(result['queued'])
        await self.wait_state(job_id, JobState.READY.value)
        self.assertEqual(self.scheduler._short_q.qsize(), 0)

    async def test_recovery_requeues_only_queued(self):
        waiting = add_job(self.store, request(text='waiting'))
        uncertain = add_job(self.store, request(text='unknown'), state=JobState.RUNNING)
        affected = await self.scheduler.recover_on_restart()
        self.assertGreaterEqual(affected, 2)
        self.assertEqual(self.store.get(uncertain)['state'], JobState.UNKNOWN.value)
        await self.wait_state(waiting, JobState.READY.value)

    async def test_close_leaves_unstarted_job_queued(self):
        self.scheduler._ensure_workers = lambda: None
        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request())
        await self.scheduler.close()
        self.assertEqual(self.store.get(job_id)['state'], JobState.QUEUED.value)

    async def test_wait_for_returns_on_completion_without_polling(self):
        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request())
        row = await self.scheduler.wait_for(job_id, timeout=2.0)
        self.assertEqual(row['state'], JobState.READY.value)

    async def test_wait_for_already_terminal_returns_immediately(self):
        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request())
        await self.wait_state(job_id, JobState.READY.value)
        row = await self.scheduler.wait_for(job_id, timeout=2.0)
        self.assertEqual(row['state'], JobState.READY.value)

    async def test_wait_for_timeout_returns_current_state(self):
        self.scheduler._ensure_workers = lambda: None
        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request())
        row = await self.scheduler.wait_for(job_id, timeout=0.05)
        self.assertEqual(row['state'], JobState.QUEUED.value)

    async def test_runner_receives_resolved_adapter(self):
        seen = {}

        async def runner(request_, adapter, profile):
            seen['adapter'] = adapter
            seen['profile'] = profile
            return b'audio'

        job_id = add_job(self.store, request())
        await self.scheduler.submit_short(job_id, request(), runner)
        await self.wait_state(job_id, JobState.READY.value)
        self.assertIs(seen['adapter'], self.adapter)
        self.assertEqual(seen['profile'], {})


if __name__ == '__main__':
    unittest.main()
