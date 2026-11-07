"""学习库技能裁剪：按价值而不是插入顺序（2026-11-07）。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent_learning import MAX_SKILLS, AgentLearning, SKILL_FORMAT


def _package(name, skill_id, triggers=("测试",)):
    return {"format": SKILL_FORMAT, "version": 1, "id": skill_id, "name": name,
            "instructions": "照着做就行", "triggers": list(triggers)}


class SkillTrimValueTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "learn.json"
        self.store = AgentLearning(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_disabled_skill_is_dropped_before_enabled_ones(self):
        """关掉的技能是用户明确说「别用」，裁剪时先丢它们。"""
        oldest = self.store.import_skill(_package("最早的", "oldest"), source="test")
        self.store.set_skill_enabled(oldest["id"], False)
        for index in range(MAX_SKILLS):
            self.store.import_skill(_package("技能%d" % index, "s%d" % index), source="test")
        ids = [s["id"] for s in self.store._state["skills"]]
        self.assertEqual(len(ids), MAX_SKILLS)
        self.assertNotIn("oldest", ids, "被关掉的技能不该比新技能活得更久")

    def test_newer_skill_survives_over_older_enabled_one(self):
        for index in range(MAX_SKILLS + 5):
            self.store.import_skill(_package("技能%d" % index, "s%d" % index), source="test")
        ids = [s["id"] for s in self.store._state["skills"]]
        self.assertEqual(len(ids), MAX_SKILLS)
        self.assertIn("s%d" % (MAX_SKILLS + 4), ids, "最新导入的必须留着")
        self.assertNotIn("s0", ids, "最旧的先出局")

    def test_trimmed_state_stays_readable(self):
        for index in range(MAX_SKILLS + 3):
            self.store.import_skill(_package("技能%d" % index, "s%d" % index), source="test")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(raw["skills"]), MAX_SKILLS)


if __name__ == "__main__":
    unittest.main()
