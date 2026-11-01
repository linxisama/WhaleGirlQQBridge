"""网络学习队列：去重、领取、完成/失败、统计。"""
import sqlite3
import unittest

from src.learning_queue import LearningQueue


class LearningQueueTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.queue = LearningQueue(self.db)

    def tearDown(self):
        self.db.close()

    def test_enqueue_dedupe_and_transitions(self):
        first = self.queue.enqueue('https://example.com/a')
        self.assertFalse(first['duplicate'])
        again = self.queue.enqueue('https://example.com/a')
        self.assertTrue(again['duplicate'])
        item = self.queue.next()
        self.assertEqual(item['target'], 'https://example.com/a')
        self.queue.finish(item['id'], 'ok')
        self.assertEqual(self.queue.stats().get('done'), 1)
        self.assertIsNone(self.queue.next())

    def test_fail_and_retry(self):
        item = self.queue.enqueue('https://example.com/b')
        claimed = self.queue.next()
        self.queue.fail(claimed['id'], 'network')
        self.assertEqual(self.queue.stats().get('failed'), 1)
        self.queue.retry(claimed['id'])
        self.assertEqual(self.queue.next()['id'], claimed['id'])

    def test_rejects_bad_targets(self):
        with self.assertRaises(ValueError):
            self.queue.enqueue('file:///secret')
        with self.assertRaises(ValueError):
            self.queue.enqueue('https://example.com', kind='other')


if __name__ == '__main__':
    unittest.main()
