"""T04 作业持久化测试：幂等、状态机、重启恢复。"""
import hashlib
import sqlite3
import unittest

from src.audio_contracts import (
    AudioJobRequest, AudioMode, ContractError, ErrorCode, JobState, job_parameter_hash,
)
from src.audio_jobs import AudioJobStore


def request(client_request_id='req-1', **over):
    base = {'client_request_id': client_request_id, 'mode': AudioMode.TTS, 'text': '你好'}
    base.update(over)
    return AudioJobRequest(**base)


def add_job(store, req, *, domain='private:1', principal='p', state=None):
    param_hash = job_parameter_hash(req, subject_domain=domain, principal=principal)
    job_id = hashlib.sha256(param_hash.encode()).hexdigest()[:24]
    result = store.enqueue(req, subject_domain=domain, principal=principal,
                           job_id=job_id, param_hash=param_hash)
    if state is not None and state != JobState.QUEUED:
        store.mark(job_id, state)
    return result['job_id']


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.store = AudioJobStore(self.db)

    def tearDown(self):
        self.db.close()

    def test_duplicate_request_creates_one_job(self):
        req = request()
        first = add_job(self.store, req)
        second = add_job(self.store, req)
        self.assertEqual(first, second)
        self.assertFalse(add_job(self.store, req) == '')
        count = self.db.execute('SELECT COUNT(*) FROM audio_jobs').fetchone()[0]
        self.assertEqual(count, 1)

    def test_enqueue_reports_duplicate_for_same_key(self):
        req = request()
        param_hash = job_parameter_hash(req, subject_domain='private:1', principal='p')
        job_id = hashlib.sha256(param_hash.encode()).hexdigest()[:24]
        first = self.store.enqueue(req, subject_domain='private:1', principal='p',
                                   job_id=job_id, param_hash=param_hash)
        second = self.store.enqueue(req, subject_domain='private:1', principal='p',
                                    job_id=job_id, param_hash=param_hash)
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['job_id'], second['job_id'])

    def test_same_request_id_different_content_is_a_new_job(self):
        first = add_job(self.store, request(text='你好'))
        second = add_job(self.store, request(text='再见'))
        self.assertNotEqual(first, second)

    def test_missing_subject_domain_is_unauthorized(self):
        req = request()
        with self.assertRaises(ContractError) as ctx:
            self.store.enqueue(req, subject_domain='', principal='p',
                               job_id='x' * 24, param_hash='h')
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)

    def test_terminal_state_is_locked(self):
        job_id = add_job(self.store, request())
        self.assertTrue(self.store.mark(job_id, JobState.READY))
        self.assertFalse(self.store.mark(job_id, JobState.CANCELLED))
        self.assertEqual(self.store.get(job_id)['state'], JobState.READY.value)

    def test_get_returns_request_snapshot_and_no_percentage(self):
        job_id = add_job(self.store, request())
        row = self.store.get(job_id)
        self.assertEqual(row['request']['text'], '你好')
        self.assertNotIn('percent', row)
        self.assertFalse(row['terminal'])
        self.assertEqual(row['subject_domain'], 'private:1')

    def test_active_count_and_state_counts(self):
        queued = add_job(self.store, request(text='a'))
        done = add_job(self.store, request(text='b'))
        self.store.mark(done, JobState.READY)
        self.assertEqual(self.store.active_count('private:1'), 1)
        self.assertEqual(self.store.active_count(), 1)
        self.assertEqual(self.store.state_counts().get('ready'), 1)
        self.assertIsNotNone(queued)

    def test_reconcile_restart_marks_unknown_and_cancelled(self):
        running = add_job(self.store, request(text='r'), state=JobState.RUNNING)
        preparing = add_job(self.store, request(text='p'), state=JobState.PREPARING)
        queued = add_job(self.store, request(text='q'))
        cancelled_req = add_job(self.store, request(text='c'), state=JobState.CANCEL_REQUESTED)
        affected = self.store.reconcile_restart(now=1000.0)
        self.assertGreaterEqual(affected, 3)
        self.assertEqual(self.store.get(running)['state'], JobState.UNKNOWN.value)
        self.assertEqual(self.store.get(preparing)['state'], JobState.UNKNOWN.value)
        self.assertEqual(self.store.get(queued)['state'], JobState.QUEUED.value)
        self.assertEqual(self.store.get(cancelled_req)['state'], JobState.CANCELLED.value)

    def test_queued_requests_only_returns_queued(self):
        queued = add_job(self.store, request(text='q'))
        add_job(self.store, request(text='u'), state=JobState.UNKNOWN)
        rows = self.store.queued_requests()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0]['job_id'], queued)
        self.assertEqual(rows[0][1].text, 'q')

    def test_mark_unknown_job_returns_false(self):
        self.assertFalse(self.store.mark('missing', JobState.READY))
        self.assertIsNone(self.store.get('missing'))


if __name__ == '__main__':
    unittest.main()
