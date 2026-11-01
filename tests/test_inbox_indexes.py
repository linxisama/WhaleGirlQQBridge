"""inbox 大表的索引：按会话取记录、按状态数队列都该走索引，不该全表扫。

背景：市场插件那一批（好感度补扫、回复时机、花费统计）加上原有的历史重建、队列计数，
每个插件每轮都会按 (mode, account, room) 或 state 查 inbox。20 万行实测：
无索引 13.7 ms/次（SCAN + 临时 B 树排序），有索引 0.10 ms/次。
"""
import asyncio
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402

ROOM_QUERY = ("SELECT payload,created FROM inbox WHERE mode=? AND account=? AND room=?"
              " AND state IN ('sent','partial','pending','sending') ORDER BY created DESC LIMIT 200")
STATE_QUERY = "SELECT count(*) FROM inbox WHERE state=? AND created>?"


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    return root


class InboxIndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / "test.sqlite")

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def test_indexes_are_created(self):
        names = {row[0] for row in self.bot.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inbox'")}
        self.assertIn("inbox_room_created", names)
        self.assertIn("inbox_state_created", names)

    def test_room_query_uses_the_index(self):
        plan = " ".join(str(row) for row in self.bot.db.execute(
            "EXPLAIN QUERY PLAN " + ROOM_QUERY, (self.bot.mode, self.bot.account(), "group:1")))
        self.assertIn("inbox_room_created", plan)
        self.assertNotIn("SCAN inbox", plan, plan)

    def test_state_query_uses_the_index(self):
        plan = " ".join(str(row) for row in self.bot.db.execute(
            "EXPLAIN QUERY PLAN " + STATE_QUERY, ("pending", 0)))
        self.assertIn("inbox_state_created", plan)
        self.assertNotIn("SCAN inbox", plan, plan)

    def test_indexes_are_idempotent_on_restart(self):
        """老库升级：第二次启动再用 IF NOT EXISTS 建一次，不该报错、也不该重建。"""
        before = {row[0] for row in self.bot.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inbox'")}
        second = QQRuntime(root=self.root, db_path=self.root / "test.sqlite")
        try:
            after = {row[0] for row in second.db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inbox'")}
            self.assertEqual(before, after)
        finally:
            asyncio.run(second.close())

    def test_large_table_query_is_fast_enough(self):
        """粗测：插 5 万行后，按会话取 200 条不该慢到秒级（回归防线，不是性能基准）。"""
        rows = [(f"k{index}", self.bot.mode, self.bot.account(), f"group:{index % 20}", "{}",
                 "sent", "r", "", 1000.0 + index) for index in range(50000)]
        self.bot.db.executemany(
            "INSERT OR IGNORE INTO inbox(key,mode,account,room,payload,state,reply,error,created)"
            " VALUES(?,?,?,?,?,?,?,?,?)", rows)
        self.bot.db.commit()
        started = time.perf_counter()
        for _ in range(20):
            self.bot.db.execute(ROOM_QUERY, (self.bot.mode, self.bot.account(), "group:7")).fetchall()
        per_query = (time.perf_counter() - started) / 20
        self.assertLess(per_query, 0.05, f"每次查询 {per_query * 1000:.1f} ms，索引可能没生效")


if __name__ == "__main__":
    unittest.main()
