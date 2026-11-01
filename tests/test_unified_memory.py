"""统一记忆：来源隔离、检索、遗忘、日记、旧表镜像。"""
import sqlite3
import time
import unittest
from pathlib import Path

from src.unified_memory import UnifiedMemory


class UnifiedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def test_remember_search_source_and_forget(self):
        qq = self.memory.remember('群里有人喜欢猫', source='qq', importance=60)
        desk = self.memory.remember('桌面偏好：先给结论', source='desktop', importance=80)
        self.assertEqual(self.memory.search('猫')[0]['id'], qq['id'])
        self.assertEqual(self.memory.search('结论')[0]['id'], desk['id'])
        self.assertEqual(self.memory.search('', source='qq')[0]['id'], qq['id'])
        self.memory.forget(qq['id'])
        self.assertIsNone(self.memory.get(qq['id']))

    def test_diary_and_forgetful_maintenance(self):
        self.memory.remember('短期临时', importance=10, ttl_days=-1)
        result = self.memory.maintain()
        self.assertGreaterEqual(result['expired'], 1)
        diary = self.memory.write_diary('2026-10-24', '今天完成统一记忆测试')
        self.assertEqual(diary['day'], '2026-10-24')
        self.assertEqual(self.memory.diary('2026-10-24')['content'], '今天完成统一记忆测试')
        self.assertEqual(self.memory.diaries(5)[0]['day'], '2026-10-24')

    def test_diary_exports_chinese_markdown(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / '日记.md'
            memory = UnifiedMemory(self.db, target)
            memory.write_diary('2026-10-24', '今天测试中文日记')
            text = target.read_text(encoding='utf-8')
            self.assertIn('# 鲸鱼娘日记', text)
            self.assertIn('2026-10-24', text)
            self.assertIn('今天测试中文日记', text)

    def test_diary_writes_one_txt_per_day_and_tail_prefers_them(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            memory = UnifiedMemory(self.db, folder / '日记.md', folder / '日记')
            memory.write_diary('2026-10-23', '第一天')
            memory.write_diary('2026-10-24', '第二天')
            self.assertEqual((folder / '日记' / '2026-10-23.txt').read_text(encoding='utf-8').strip(),
                             '第一天')
            self.assertEqual((folder / '日记' / '2026-10-24.txt').read_text(encoding='utf-8').strip(),
                             '第二天')
            tail = memory.diary_tail(100)
            self.assertIn('第二天', tail)
            self.assertEqual(len(memory.export_daily_files()), 2)

    def test_first_turn_layers_and_group_privacy(self):
        self.memory.remember('主人喜欢安静', source='desktop', layer='soul', importance=90)
        self.memory.remember('主人叫阿岚', source='desktop', layer='user', importance=90)
        self.memory.remember('不主动提敏感话题', source='desktop', layer='rules', importance=80)
        self.memory.remember('项目：整理日记', source='desktop', layer='project', importance=70)
        private = self.memory.context_for_prompt('你好', private=True, first_turn=True)
        self.assertIn('主人喜欢安静', private)
        self.assertIn('主人叫阿岚', private)
        self.assertIn('不主动提敏感话题', private)
        group = self.memory.context_for_prompt('你好', private=False, first_turn=True)
        self.assertNotIn('主人喜欢安静', group)
        self.assertNotIn('主人叫阿岚', group)
        self.assertIn('不主动提敏感话题', group)

    def test_later_turn_uses_keyword_and_project(self):
        self.memory.remember('群A周末讨论咖啡豆', source='qq', layer='fact',
                             project='群A', keywords=['咖啡', '周末'], importance=70)
        self.memory.remember('群A喜欢猫', source='qq', layer='fact',
                             project='群A', keywords=['猫'], importance=60)
        text = self.memory.context_for_prompt('咖啡怎么样', project='群A', first_turn=False)
        self.assertIn('咖啡豆', text)
        self.assertNotIn('喜欢猫', text)

    def test_update_corrects_content_and_layer(self):
        item = self.memory.remember('旧说法', source='desktop', layer='fact')
        changed = self.memory.update(item['id'], content='更正后的说法', layer='lesson',
                                     confidence=90, project='控制台')
        self.assertEqual(changed['content'], '更正后的说法')
        self.assertEqual(changed['layer'], 'lesson')
        self.assertEqual(changed['confidence'], 90)
        self.assertEqual(changed['project'], '控制台')

    def test_duplicate_merge_and_dream_archives_old_topics(self):
        first = self.memory.remember('同一件事实', source='desktop', layer='fact', importance=50)
        second = self.memory.remember('同一件事实', source='desktop', layer='fact', importance=80)
        self.assertTrue(second.get('merged'))
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.memory.count(), 1)
        topic = self.memory.remember('旧话题', source='desktop', layer='topic')
        self.db.execute('UPDATE unified_memories SET updated=? WHERE id=?',
                        (time.time() - 9 * 86400, topic['id']))
        self.db.commit()
        result = self.memory.dream()
        self.assertGreaterEqual(result['archived_topics'], 1)
        self.assertEqual(self.memory.get(topic['id'])['status'], 'archived')

    def test_legacy_tables_are_mirrored_without_touching_originals(self):
        self.db.execute('CREATE TABLE self_memories(id TEXT,persona TEXT,content TEXT,enabled INTEGER,created REAL)')
        self.db.execute('INSERT INTO self_memories VALUES("1","p","旧手动记忆",1,0)')
        self.db.execute('CREATE TABLE chat_memories(id TEXT,scope TEXT,subject TEXT,content TEXT,status TEXT,hits INTEGER,updated REAL)')
        self.db.execute("INSERT INTO chat_memories VALUES('2','g','u','旧确认档案','confirmed',2,0)")
        self.db.commit()
        memory = UnifiedMemory(self.db)
        self.assertIn('旧手动记忆', [x['content'] for x in memory.search('手动')])
        self.assertIn('旧确认档案', [x['content'] for x in memory.search('档案')])
        self.assertEqual(self.db.execute('SELECT count(*) FROM self_memories').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
