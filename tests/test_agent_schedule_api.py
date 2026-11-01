"""Agent 定时任务 HTTP：创建、运行、启停、删除。"""
import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from fastapi.testclient import TestClient

from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root


class AgentScheduleApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = build_root(cls.tmp.name)
        cls.bot = QQRuntime(root=root, db_path=root / 'test.sqlite')
        cls.client = TestClient(create_app(cls.bot))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.tmp.cleanup()

    def test_crud_and_run(self):
        listing = self.client.get('/api/assistant/schedules').json()
        self.assertTrue(listing['success'])
        created = self.client.post('/api/assistant/schedules', json={
            'name': '连接检查', 'action': 'connection_check', 'every_minutes': 30})
        self.assertEqual(created.status_code, 200, created.text)
        task = created.json()['task']
        self.assertEqual(task['name'], '连接检查')

        ran = self.client.post(f"/api/assistant/schedules/{task['id']}/run")
        self.assertEqual(ran.status_code, 200, ran.text)
        self.assertIn('连接', ran.json()['task']['last_result'])

        toggled = self.client.post(f"/api/assistant/schedules/{task['id']}/toggle",
                                   json={'enabled': False})
        self.assertEqual(toggled.status_code, 200)
        self.assertFalse(toggled.json()['tasks'][0]['enabled'])

        deleted = self.client.post(f"/api/assistant/schedules/{task['id']}/delete")
        self.assertTrue(deleted.json()['removed'])
        self.assertEqual(deleted.json()['tasks'], [])

    def test_invalid_interval(self):
        response = self.client.post('/api/assistant/schedules', json={
            'name': 'x', 'action': 'daily_digest', 'every_minutes': 1})
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
