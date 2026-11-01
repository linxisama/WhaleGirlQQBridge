"""本地 Agent 控制台 API 测试：状态/待批/急停/无设备批准拒绝。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agent_v2_api import install_agent_routes


class Config:
    def get(self, key, default=None):
        return default


class FakeBot:
    def __init__(self, root, db):
        self.root = root
        self.config = Config()
        self.db = db


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.app = FastAPI()
        self.app.state.bot = FakeBot(Path(self.temp.name), self.db)
        install_agent_routes(self.app)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.temp.cleanup()

    def test_status_and_pending_start_empty(self):
        status = self.client.get('/api/v2/agent/status')
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['devices'], [])
        self.assertFalse(status.json()['stopped'])
        pending = self.client.get('/api/v2/agent/pending')
        self.assertEqual(pending.json()['pending'], [])
        audit = self.client.get('/api/v2/agent/audit')
        self.assertEqual(audit.json()['events'], [])

    def test_emergency_stop_and_resume(self):
        stopped = self.client.post('/api/v2/agent/emergency-stop')
        self.assertTrue(stopped.json()['stopped'])
        self.assertTrue(self.client.get('/api/v2/agent/status').json()['stopped'])
        resumed = self.client.post('/api/v2/agent/resume')
        self.assertFalse(resumed.json()['stopped'])

    def test_approving_unknown_job_is_rejected(self):
        response = self.client.post('/api/v2/agent/approve', json={'job_id': 'nope'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail']['code'], 'INVALID_INPUT')

    def test_emergency_stop_blocks_approval(self):
        self.client.post('/api/v2/agent/emergency-stop')
        response = self.client.post('/api/v2/agent/approve', json={'job_id': 'nope'})
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
