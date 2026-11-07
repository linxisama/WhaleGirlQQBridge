"""学习库裁剪：攒满上限后按价值保留，而不是按插入顺序丢最早的。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent_learning import AgentLearning, MAX_LESSONS


def _actions(tag):
    return [{"type": "plugin", "id": tag, "enabled": True}]


class TrimByValueTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = AgentLearning(Path(self.dir.name) / "learn.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_old_but_useful_lesson_survives_trim(self):
        """最老但最常成功的一条不能被裁掉：它是最早教会机器人怎么干活的。"""
        useful = self.store.record("最早学会的做法", _actions("keep"))
        for _ in range(5):
            self.store.feedback(lesson_id=useful["id"], score=1)
        for index in range(MAX_LESSONS):
            self.store.record("填充做法%d" % index, _actions("fill%d" % index))
        self.store.save()
        ids = [item["id"] for item in self.store._state["lessons"]]
        self.assertEqual(len(ids), MAX_LESSONS)
        self.assertIn(useful["id"], ids, "高成功次数的老经验必须留下")

    def test_failed_lesson_is_dropped_first(self):
        """失败次数最多的先出局。"""
        bad = self.store.record("总失败的做法", _actions("bad"))
        for _ in range(4):
            self.store.feedback(lesson_id=bad["id"], score=-1)
        for index in range(MAX_LESSONS):
            self.store.record("普通做法%d" % index, _actions("ok%d" % index))
        self.store.save()
        ids = [item["id"] for item in self.store._state["lessons"]]
        self.assertNotIn(bad["id"], ids, "失败多的应当先被裁掉")
        self.assertEqual(len(ids), MAX_LESSONS)

    def test_trim_keeps_state_readable(self):
        for index in range(MAX_LESSONS + 5):
            self.store.record("做法%d" % index, _actions("x%d" % index))
        self.store.save()
        reloaded = json.loads((Path(self.dir.name) / "learn.json").read_text(encoding="utf-8"))
        self.assertEqual(len(reloaded["lessons"]), MAX_LESSONS)


if __name__ == "__main__":
    unittest.main()
