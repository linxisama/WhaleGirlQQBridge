"""学习库与学习队列的人格隔离：各学各的、老数据能读、写入原子。"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agent_learning import AgentLearning, persona_file
from src.learning_queue import LearningQueue

SETTINGS = [{'type': 'settings', 'values': {'reply_timeout': 15}}]
PLUGIN = [{'type': 'plugin', 'id': 'memes', 'enabled': True}]


class PersonaLearningScopeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.path = self.root / 'agent_learning.json'

    def tearDown(self):
        self.dir.cleanup()

    def test_two_personas_learn_separately(self):
        whale = AgentLearning(self.path, persona='小鲸')
        sea = AgentLearning(self.path, persona='小海')
        self.assertEqual(whale.path, persona_file(self.path, '小鲸'))
        self.assertNotEqual(whale.path, sea.path)

        whale.record('把回复调快一点', SETTINGS, summary='等待上限 20 → 15')
        whale.set_preference('称呼', '老鲸')
        sea.record('打开表情插件', PLUGIN)

        self.assertEqual([h['intent'] for h in whale.match_lessons('把回复调快一点')], ['把回复调快一点'])
        self.assertNotIn('打开表情插件', [h['intent'] for h in whale.match_lessons('打开表情插件')])
        self.assertEqual([h['intent'] for h in sea.match_lessons('打开表情插件')], ['打开表情插件'])
        self.assertNotIn('把回复调快一点', [h['intent'] for h in sea.match_lessons('把回复调快一点')])
        self.assertEqual(whale.get_preference('称呼')['value'], '老鲸')
        self.assertEqual(sea.get_preference('称呼'), {})
        self.assertEqual(whale.stats()['persona'], '小鲸')
        # 默认人格也看不到别人格学到的东西
        self.assertEqual(AgentLearning(self.path).stats()['lessons'], 0)

    def test_legacy_single_file_reads_as_default_persona(self):
        legacy = {
            'version': 1,
            'preferences': {'称呼': {'value': '老鲸', 'note': '', 'updated': 1.0}},
            'lessons': [{'id': 'l1', 'intent': '把回复调快一点', 'bigrams': [], 'actions': SETTINGS,
                         'summary': '等待上限 20 → 15', 'ok': 2, 'fail': 0, 'note': '',
                         'created': 1.0, 'updated': 1.0}],
            'skills': [{'id': 'tone', 'name': '语气调整', 'instructions': '先问清楚。',
                        'triggers': ['语气'], 'permissions': [], 'enabled': True, 'imported': 1.0}],
        }
        self.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
        store = AgentLearning(self.path)
        self.assertEqual(store.get_preference('称呼')['value'], '老鲸')
        self.assertEqual(store.match_lessons('把回复调快一点')[0]['id'], 'l1')
        self.assertEqual(store.match_skills('语气')[0]['id'], 'tone')
        # 默认人格写回后仍是老结构，老工具（whale_memory_mcp 直接读这个文件）照旧能读
        store.record('开启机器人', [{'type': 'service', 'running': True}])
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(len(data['lessons']), 2)
        self.assertEqual(data['preferences']['称呼']['value'], '老鲸')
        self.assertEqual(data['skills'][0]['id'], 'tone')
        # 新人格不继承默认人格的数据，否则隔离就是空的
        other = AgentLearning(self.path, persona='小鲸')
        self.assertEqual(other.stats()['lessons'], 0)
        self.assertFalse(other.path.exists())

    def test_queue_is_scoped_by_persona(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        queue = LearningQueue(db)

        first = queue.enqueue('https://example.com/a', persona='小鲸')
        self.assertFalse(first['duplicate'])
        self.assertTrue(queue.enqueue('https://example.com/a', persona='小鲸')['duplicate'])
        other = queue.enqueue('https://example.com/a', persona='小海')
        self.assertFalse(other['duplicate'])          # 同一目标，两个人格各排一条

        item = queue.next('小鲸')
        self.assertEqual(item['id'], first['id'])
        self.assertEqual(item['persona'], '小鲸')
        self.assertIsNone(queue.next())               # 默认人格取不到别人格的任务
        self.assertEqual(queue.next('小海')['id'], other['id'])
        self.assertEqual([i['id'] for i in queue.list(persona='小鲸')], [first['id']])

    def test_old_queue_table_gets_persona_column(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('''CREATE TABLE learning_queue(
            id TEXT PRIMARY KEY, kind TEXT, target TEXT, status TEXT,
            attempts INTEGER, created REAL, updated REAL, result TEXT, error TEXT)''')
        db.execute("INSERT INTO learning_queue VALUES('old1','web','https://example.com/old',"
                   "'pending',0,1,1,'','')")
        db.commit()

        queue = LearningQueue(db)                     # 老库补列，不重建表
        columns = [row[1] for row in db.execute('PRAGMA table_info(learning_queue)')]
        self.assertIn('persona', columns)
        self.assertTrue(queue.enqueue('https://example.com/old')['duplicate'])   # 老行落在默认人格
        self.assertFalse(queue.enqueue('https://example.com/old', persona='小鲸')['duplicate'])
        self.assertEqual(queue.next()['id'], 'old1')

    def test_save_is_atomic(self):
        store = AgentLearning(self.path, persona='小鲸')
        store.record('把回复调快一点', SETTINGS)
        before = store.path.read_text(encoding='utf-8')
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(Path(dst).read_text(encoding='utf-8'))
            return real_replace(src, dst)

        with patch('src.agent_learning.os.replace', side_effect=spy):
            store.set_preference('称呼', '老鲸')
        # 新内容只在 replace 那一刻整体出现，替换前目标文件还是旧的完整 JSON
        self.assertEqual(json.loads(seen[0])['preferences'], {})
        self.assertEqual(len(json.loads(seen[0])['lessons']), 1)
        self.assertEqual(json.loads(before)['preferences'], {})
        self.assertEqual(AgentLearning(self.path, persona='小鲸').get_preference('称呼')['value'], '老鲸')

    def test_failed_save_keeps_previous_file(self):
        store = AgentLearning(self.path, persona='小鲸')
        store.record('把回复调快一点', SETTINGS)
        before = store.path.read_text(encoding='utf-8')
        with patch('src.agent_learning.os.replace', side_effect=OSError('磁盘满')):
            with self.assertRaises(OSError):
                store.set_preference('称呼', '老鲸')
        self.assertEqual(store.path.read_text(encoding='utf-8'), before)
        leftovers = [p.name for p in self.root.iterdir() if p.name != store.path.name]
        self.assertEqual(leftovers, [])               # 失败的临时文件不留在目录里
        store.set_preference('称呼', '老鲸')           # 失败之后还能正常写
        self.assertEqual(AgentLearning(self.path, persona='小鲸').get_preference('称呼')['value'], '老鲸')

    def test_second_instance_does_not_clobber_first(self):
        first = AgentLearning(self.path, persona='小鲸')
        second = AgentLearning(self.path, persona='小鲸')
        first.record('把回复调快一点', SETTINGS)
        second.record('打开表情插件', PLUGIN)
        intents = [l['intent'] for l in AgentLearning(self.path, persona='小鲸').overview()['lessons']]
        self.assertIn('把回复调快一点', intents)
        self.assertIn('打开表情插件', intents)


if __name__ == '__main__':
    unittest.main()
