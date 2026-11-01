"""分层记忆 API：写入、按层读取、做梦整理。"""
import sqlite3
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.memory_api import register_memory_api
from src.unified_memory import UnifiedMemory


class MemoryLayersApiTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.memory = UnifiedMemory(self.db)
        app = FastAPI()
        app.state.bot = SimpleNamespace(
            unified_memory=self.memory,
            style=SimpleNamespace(active_name='鲸鱼娘'),
            diary_path=None,
        )
        register_memory_api(app)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()

    def test_add_read_and_dream(self):
        response = self.client.post('/api/memory/unified', json={
            'action': 'add', 'content': '主人喜欢先看结论', 'source': 'desktop',
            'layer': 'user', 'project': '控制台', 'confidence': 88,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['success'])

        data = self.client.get('/api/memory/unified', params={
            'layer': 'user', 'project': '控制台',
        }).json()
        self.assertEqual(data['items'][0]['content'], '主人喜欢先看结论')
        self.assertEqual(data['items'][0]['layer'], 'user')
        self.assertEqual(data['layers']['user'], 1)

        dream = self.client.post('/api/memory/unified', json={'action': 'dream'})
        self.assertEqual(dream.status_code, 200, dream.text)
        self.assertIn('merged', dream.json())

    def test_reflect_writes_lesson(self):
        response = self.client.post('/api/memory/unified', json={
            'action': 'reflect', 'summary': '先验证再汇报', 'project': '控制台',
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['item']['layer'], 'lesson')


if __name__ == '__main__':
    unittest.main()
