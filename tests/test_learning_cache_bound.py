"""学习库缓存：人设多了以后不能无限攒句柄。"""
import tempfile
import unittest
from pathlib import Path

from src.console_agent import ConsoleAgent, LEARNING_CACHE_LIMIT
from src.agent_learning import AgentLearning


class _Style:
    def __init__(self):
        self.active_name = "default"


class _Bot:
    def __init__(self, base):
        self.style = _Style()
        self.config = type("C", (), {"config_path": str(base / "config.yaml")})()


class LearningCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)
        # 绕开 __init__ 的其它依赖，只装本测试要用的三样。
        self.agent = ConsoleAgent.__new__(ConsoleAgent)
        self.agent.bot = _Bot(self.base)
        self.agent._learning_base = AgentLearning(self.base / "agent_learning.json")
        self.agent._learning_cache = {}

    def tearDown(self):
        self.dir.cleanup()

    def test_default_persona_uses_base_store(self):
        self.assertIs(self.agent._learning_for("default"), self.agent._learning_base)
        self.assertIs(self.agent._learning_for(""), self.agent._learning_base)

    def test_cache_is_bounded(self):
        for index in range(LEARNING_CACHE_LIMIT + 4):
            self.agent._learning_for("p%d" % index)
        self.assertLessEqual(len(self.agent._learning_cache), LEARNING_CACHE_LIMIT)

    def test_recently_used_persona_stays_cached(self):
        for index in range(LEARNING_CACHE_LIMIT):
            self.agent._learning_for("p%d" % index)
        self.agent._learning_for("p0")          # 命中后应挪到最新
        self.agent._learning_for("extra")       # 触发淘汰，最旧的 p1 出局
        self.assertIn("p0", self.agent._learning_cache, "刚用过的人设不该被淘汰")
        self.assertNotIn("p1", self.agent._learning_cache)

    def test_personas_do_not_share_a_store(self):
        first = self.agent._learning_for("whale")
        second = self.agent._learning_for("sea")
        self.assertIsNot(first, second)
        self.assertNotEqual(str(first.path), str(second.path))


if __name__ == "__main__":
    unittest.main()
