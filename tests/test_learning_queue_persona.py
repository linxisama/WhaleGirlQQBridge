"""学习队列的人设隔离：next_any 取任意人设、写库落到排它的那个人格。"""
import sqlite3
import unittest

from src.learning_queue import LearningQueue


class QueuePersonaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.queue = LearningQueue(self.db)

    def test_next_any_sees_every_persona(self):
        self.queue.enqueue("https://example.com/a", "web", persona="whale")
        self.queue.enqueue("https://example.com/b", "web", persona="sea")
        first = self.queue.next_any()
        second = self.queue.next_any()
        self.assertEqual(first["persona"], "whale")
        self.assertEqual(second["persona"], "sea")
        self.assertIsNone(self.queue.next_any(), "取完就该空了")

    def test_next_stays_persona_scoped(self):
        """按人设取仍只看到自己的：别人格的待学任务不能被当前人设消化掉。"""
        self.queue.enqueue("https://example.com/a", "web", persona="whale")
        self.assertIsNone(self.queue.next(persona="sea"))
        self.assertEqual(self.queue.next(persona="whale")["persona"], "whale")

    def test_duplicate_target_is_per_persona(self):
        first = self.queue.enqueue("https://example.com/same", "web", persona="whale")
        self.assertFalse(first["duplicate"])
        second = self.queue.enqueue("https://example.com/same", "web", persona="sea")
        self.assertFalse(second["duplicate"], "别人格学过的目标，这个人格还得自己学")
        again = self.queue.enqueue("https://example.com/same", "web", persona="whale")
        self.assertTrue(again["duplicate"])

    def test_legacy_rows_without_persona_are_still_served(self):
        """老库补列后老记录 persona 为空串，next_any 必须还能取到。"""
        self.queue.enqueue("https://example.com/old", "web", persona="")
        item = self.queue.next_any()
        self.assertEqual(item["target"], "https://example.com/old")
        self.assertEqual(item["persona"], "")


if __name__ == "__main__":
    unittest.main()
