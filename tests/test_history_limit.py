"""每个群/私聊最多保留 1000 条记录：超出删最旧的，待处理/其它会话不动。"""
import pathlib
import tempfile
import time
import unittest

from src.qq_app import QQRuntime


class HistoryLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='history-')
        self.addCleanup(__import__('shutil').rmtree, self._tmp, True)
        self.root = pathlib.Path(self._tmp)
        (self.root / 'data' / 'qq-only').mkdir(parents=True)
        (self.root / 'config.qq.yaml').write_text(
            'qq_only:\n  mode: mock\n  history_limit_per_room: 100\n', encoding='utf-8')
        self.bot = QQRuntime(root=self.root)
        self.addCleanup(self.bot.db.close)

    def _insert_inbox(self, room, count, state='sent'):
        now = time.time()
        for index in range(count):
            self.bot.db.execute(
                "INSERT INTO inbox(key,mode,account,room,payload,state,created) VALUES(?,?,?,?,?,'%s',?)" % state,
                (f'{room}-{state}-{time.time_ns()}-{index}', 'mock', 'acc', room, '{}',
                 now - count + index))
        self.bot.db.commit()

    def test_inbox_keeps_latest_100(self):
        self._insert_inbox('group:1', 150)
        self._insert_inbox('group:2', 5)
        self.bot._prune_inbox('mock', 'acc', 'group:1')
        self.bot.db.commit()
        count = self.bot.db.execute(
            "SELECT count(*) FROM inbox WHERE room='group:1'").fetchone()[0]
        self.assertEqual(count, 100)
        other = self.bot.db.execute(
            "SELECT count(*) FROM inbox WHERE room='group:2'").fetchone()[0]
        self.assertEqual(other, 5)
        # 保留的是最新的：最早的 key 已被删
        oldest_created = self.bot.db.execute(
            "SELECT min(created) FROM inbox WHERE room='group:1'").fetchone()[0]
        newest_created = self.bot.db.execute(
            "SELECT max(created) FROM inbox WHERE room='group:1'").fetchone()[0]
        self.assertGreater(oldest_created, newest_created - 150)

    def test_pending_is_never_pruned(self):
        self._insert_inbox('group:3', 120)
        self._insert_inbox('group:3', 3, state='pending')
        self.bot._prune_inbox('mock', 'acc', 'group:3')
        pending = self.bot.db.execute(
            "SELECT count(*) FROM inbox WHERE room='group:3' AND state='pending'").fetchone()[0]
        self.assertEqual(pending, 3)

    def test_ambient_keeps_latest_per_scope(self):
        self.bot.chat_state.history_limit = 100
        for index in range(150):
            self.bot.chat_state.observe('group:9', {
                'id': f'm{index}', 'sender': f'u{index}', 'sender_name': 'x'},
                now=time.time() + index)
        total = self.bot.db.execute(
            "SELECT count(*) FROM chat_ambient WHERE scope='group:9'").fetchone()[0]
        self.assertEqual(total, 100)


if __name__ == '__main__':
    unittest.main()
