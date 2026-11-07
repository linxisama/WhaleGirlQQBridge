"""控制台助手的学习库按人设隔离（2026-11-07）。

不同人设的语气、偏好、做过的操作都不一样，混在一个库里会让 A 学到的做法被 B 直接
复用。默认人设仍用原文件，老用户已有的学习记录不会消失。
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.agent_learning import AgentLearning
from src.console_agent import ConsoleAgent


def _agent(base_path, persona="default"):
    """只装学习库相关的三个属性，不构造整个 ConsoleAgent（那要真实 bot 与数据库）。"""
    agent = object.__new__(ConsoleAgent)
    agent._learning_base = AgentLearning(base_path)
    agent._learning_cache = {}
    agent.bot = SimpleNamespace(style=SimpleNamespace(active_name=persona))
    return agent


class ConsoleLearningPersonaTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name) / "agent_learning.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_default_persona_keeps_the_original_file(self):
        agent = _agent(self.base)
        self.assertIs(agent.learning, agent._learning_base)
        self.assertEqual(agent.learning.path, self.base)

    def test_empty_persona_name_falls_back_to_the_original_file(self):
        agent = _agent(self.base, persona="")
        self.assertIs(agent.learning, agent._learning_base)

    def test_other_persona_gets_its_own_file(self):
        agent = _agent(self.base, persona="鲸鱼娘")
        store = agent.learning
        self.assertIsNot(store, agent._learning_base)
        self.assertEqual(store.persona, "鲸鱼娘")
        self.assertNotEqual(store.path, self.base)
        self.assertIn("鲸鱼娘", store.path.name)

    def test_same_persona_reuses_one_instance(self):
        agent = _agent(self.base, persona="鲸鱼娘")
        self.assertIs(agent.learning, agent.learning)

    def test_lessons_do_not_leak_across_personas(self):
        agent = _agent(self.base, persona="甲")
        agent.learning.record("把回复调快一点", [{"type": "settings", "key": "reply_timeout",
                                                 "values": {"reply_timeout": 15}}])
        self.assertEqual(len(agent.learning.overview()["lessons"]), 1)
        agent.bot.style.active_name = "乙"
        self.assertEqual(agent.learning.overview()["lessons"], [],
                         "另一个人设不该看到甲学到的做法")
        agent.bot.style.active_name = "default"
        self.assertEqual(agent.learning.overview()["lessons"], [])

    def test_persona_name_with_odd_characters_stays_inside_the_directory(self):
        agent = _agent(self.base, persona="../../etc/passwd")
        path = agent.learning.path
        self.assertEqual(path.parent, self.base.parent,
                         "人设名不能把学习库写到别的目录去")


if __name__ == "__main__":
    unittest.main()
