"""默认人设归一：style_manager.active_name 恒为 tsundere_whale_girl，隔离逻辑要认成 ''。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import persona_scope
from src.agent_learning import AgentLearning
from src.style_manager import DEFAULT_PERSONA


def bot_with(name):
    return SimpleNamespace(style=SimpleNamespace(active_name=name))


class ScopeTests(unittest.TestCase):
    def test_empty_stays_empty(self):
        self.assertEqual(persona_scope.scope(''), '')
        self.assertEqual(persona_scope.scope(None), '')
        self.assertEqual(persona_scope.scope('   '), '')

    def test_default_persona_maps_to_empty(self):
        self.assertEqual(persona_scope.scope(DEFAULT_PERSONA), '',
                         '默认人设必须归一成空串，否则老学习库读不到')
        self.assertEqual(persona_scope.scope('default'), '')

    def test_case_insensitive_aliases(self):
        self.assertEqual(persona_scope.scope('DEFAULT'), '')

    def test_named_persona_survives(self):
        self.assertEqual(persona_scope.scope('whale'), 'whale')
        self.assertEqual(persona_scope.scope('  sea  '), 'sea')

    def test_long_name_is_capped(self):
        self.assertEqual(len(persona_scope.scope('x' * 200)), persona_scope.PERSONA_MAX)

    def test_active_scope_reads_style(self):
        self.assertEqual(persona_scope.active_scope(bot_with(DEFAULT_PERSONA)), '')
        self.assertEqual(persona_scope.active_scope(bot_with('whale')), 'whale')

    def test_active_scope_tolerates_missing_style(self):
        self.assertEqual(persona_scope.active_scope(SimpleNamespace()), '')
        self.assertEqual(persona_scope.active_scope(SimpleNamespace(style=None)), '')

    def test_is_default(self):
        self.assertTrue(persona_scope.is_default(DEFAULT_PERSONA))
        self.assertFalse(persona_scope.is_default('whale'))


class LearningPathTests(unittest.TestCase):
    def test_default_persona_keeps_legacy_path(self):
        base = Path('C:/x/agent_learning.json')
        self.assertEqual(AgentLearning(base, persona_scope.scope(DEFAULT_PERSONA)).path, base)

    def test_named_persona_gets_own_file(self):
        base = Path('C:/x/agent_learning.json')
        path = AgentLearning(base, persona_scope.scope('whale')).path
        self.assertNotEqual(path, base)
        self.assertIn('whale', path.name)


class ConsoleAgentLearningTests(unittest.TestCase):
    def _agent(self, name):
        from src.console_agent import ConsoleAgent
        agent = ConsoleAgent.__new__(ConsoleAgent)
        agent.bot = bot_with(name)
        base = AgentLearning(Path('C:/x/agent_learning.json'))
        agent._learning_base = base
        agent._learning_cache = {}
        return agent

    def test_default_persona_uses_base_store(self):
        agent = self._agent(DEFAULT_PERSONA)
        self.assertIs(agent.learning, agent._learning_base,
                      '默认人设切到 tsundere_whale_girl 后必须还读老文件')

    def test_default_alias_uses_base_store(self):
        agent = self._agent('default')
        self.assertIs(agent.learning, agent._learning_base)

    def test_named_persona_is_cached_separately(self):
        agent = self._agent('whale')
        first = agent.learning
        self.assertIsNot(first, agent._learning_base)
        self.assertIs(agent.learning, first, '同一人设复用同一实例')


if __name__ == '__main__':
    unittest.main()