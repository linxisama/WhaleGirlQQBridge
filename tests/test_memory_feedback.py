"""记忆命中反馈只统计真正进入提示文本的条目。"""
import sqlite3
import unittest

from src.unified_memory import UnifiedMemory


class MemoryFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def uses(self, identifier):
        return int(self.memory.get(identifier)['uses'])

    def test_group_prompt_does_not_touch_owner_only_memory(self):
        item = self.memory.remember('主人喜欢安静', source='desktop', layer='soul',
                                    channel='qq', importance=90)
        self.memory.context_for_prompt('安静', private=False, channel='qq')
        self.assertEqual(self.uses(item['id']), 0)

    def test_scope_filtered_memory_does_not_get_a_hit(self):
        item = self.memory.remember('控制台专用偏好', source='desktop', layer='fact',
                                    channel='console', keywords=['偏好'], importance=80)
        text = self.memory.context_for_prompt('偏好', private=True, channel='qq')
        self.assertNotIn('控制台专用偏好', text)
        self.assertEqual(self.uses(item['id']), 0)

    def test_injected_memory_gets_exactly_one_hit(self):
        item = self.memory.remember('主人喜欢安静', source='desktop', layer='fact',
                                    channel='qq', keywords=['安静'], importance=80)
        text = self.memory.context_for_prompt('安静', private=True, channel='qq')
        self.assertIn('主人喜欢安静', text)
        self.assertEqual(self.uses(item['id']), 1)
        self.memory.context_for_prompt('安静', private=True, channel='qq')
        self.assertEqual(self.uses(item['id']), 2)

    def test_search_can_skip_tracking(self):
        item = self.memory.remember('只在预览里出现', source='desktop', layer='fact',
                                    keywords=['预览'], importance=70)
        self.memory.search('预览', track=False)
        self.assertEqual(self.uses(item['id']), 0)
        self.memory.search('预览')
        self.assertEqual(self.uses(item['id']), 1)


if __name__ == '__main__':
    unittest.main()
