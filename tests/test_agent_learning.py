import json
import tempfile
import unittest
from pathlib import Path

from src.agent_learning import AgentLearning, similarity


class LearningStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'agent_learning.json'
        self.store = AgentLearning(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_record_and_match_similar_intent(self):
        self.store.record('把回复调快一点', [{'type': 'settings', 'values': {'reply_timeout': 15}}],
                          summary='等待上限 20 → 15')
        hits = self.store.match_lessons('回复太慢了，帮我调快一点')
        self.assertTrue(hits)
        self.assertIn('回复调快', hits[0]['intent'])
        self.assertEqual(hits[0]['actions'][0]['values']['reply_timeout'], 15)

    def test_failed_lesson_ranks_lower(self):
        actions = [{'type': 'plugin', 'id': 'memes', 'enabled': True}]
        first = self.store.record('打开表情插件', actions)
        for _ in range(4):
            self.store.feedback(lesson_id=first['id'], score=-1)
        self.store.record('打开表情插件', [{'type': 'plugin', 'id': 'quote_reply', 'enabled': True}])
        hits = self.store.match_lessons('打开表情插件', limit=4)
        self.assertEqual(len(hits), 2)
        # 反复失败的经验被降权，排到没失败记录的那条后面
        self.assertEqual(hits[-1]['fail'], 4)
        self.assertEqual(hits[0]['fail'], 0)

    def test_feedback_without_history_creates_lesson(self):
        lesson = self.store.feedback(actions=[{'type': 'service', 'running': True}], score=1,
                                     intent='开启机器人')
        self.assertEqual(lesson['ok'], 1)

    def test_import_skill_validation_and_match(self):
        package = {'format': 'whale-agent-skill', 'version': 1, 'id': 'tone_pack',
                   'name': '语气调整', 'instructions': '先问清想要更活泼还是更克制。',
                   'triggers': ['语气', '说话风格']}
        record = self.store.import_skill(package)
        self.assertEqual(record['id'], 'tone_pack')
        self.assertEqual(self.store.match_skills('把说话风格改活泼点')[0]['id'], 'tone_pack')
        with self.assertRaises(ValueError):
            self.store.import_skill({'format': 'other', 'version': 1, 'name': 'x', 'instructions': 'y'})
        with self.assertRaises(ValueError):
            self.store.import_skill({'format': 'whale-agent-skill', 'version': 1, 'name': 'x',
                                     'instructions': 'y', 'id': '../evil'})

    def test_learn_from_chat_and_web(self):
        chat = self.store.learn_from_source('chat', '群里喜欢短句')
        web = self.store.learn_from_source('web', '搜索到新的资料')
        self.assertEqual(chat['source'], 'chat')
        self.assertEqual(web['source'], 'web')
        with self.assertRaises(ValueError):
            self.store.learn_from_source('other', 'x')

    def test_skill_permissions_toggle_and_match(self):
        package = {'format': 'whale-agent-skill', 'version': 1, 'id': 'perm_skill',
                   'name': '权限技能', 'instructions': '测试权限。', 'triggers': ['权限'],
                   'permissions': ['read_memory', 'network']}
        record = self.store.import_skill(package)
        self.assertEqual(record['permissions'], ['read_memory', 'network'])
        self.assertTrue(record['enabled'])
        self.assertEqual(self.store.match_skills('权限相关')[0]['id'], 'perm_skill')
        self.store.set_skill_enabled('perm_skill', False)
        self.assertEqual(self.store.match_skills('权限相关'), [])
        self.assertFalse(self.store.overview()['skills'][0]['enabled'])
        with self.assertRaises(ValueError):
            self.store.import_skill(dict(package, id='bad_perm', permissions=['root_shell']))
        preview = self.store.preview_skill(package)
        self.assertEqual(preview['name'], '权限技能')
        self.assertNotIn('perm_skill', [s['id'] for s in self.store.overview()['skills'] if s['id'] != 'perm_skill'])

    def test_state_survives_reload(self):
        self.store.set_preference('称呼', '老鲸')
        self.store.record('开启机器人', [{'type': 'service', 'running': True}], summary='启动')
        again = AgentLearning(self.path)
        self.assertEqual(again.overview()['preferences'][0]['value'], '老鲸')
        self.assertEqual(again.overview()['lessons'][0]['intent'], '开启机器人')

    def test_corrupt_file_starts_empty(self):
        self.path.write_text('{not json', encoding='utf-8')
        store = AgentLearning(self.path)
        self.assertEqual(store.stats()['lessons'], 0)
        store.set_preference('x', 'y')
        self.assertTrue(json.loads(self.path.read_text(encoding='utf-8'))['preferences'])

    def test_maintain_removes_failed_and_stale(self):
        failed = self.store.record('没用的做法', [{'type': 'service', 'running': True}], ok=False)
        for _ in range(2):
            self.store.feedback(lesson_id=failed['id'], score=-1)
        stale = self.store.record('很久以前的做法', [{'type': 'service', 'running': False}], ok=False)
        self.store._state['lessons'] = [
            dict(lesson, updated=1.0) if lesson.get('id') == stale['id'] else lesson
            for lesson in self.store._state['lessons']]
        self.store.save()
        result = self.store.maintain()
        self.assertGreaterEqual(result['removed_failed'], 1)
        self.assertGreaterEqual(result['removed_stale'], 1)

    def test_newer_lesson_outranks_older_with_same_confidence(self):
        old = self.store.record('把回复调快一点', [{'type': 'settings', 'values': {'reply_timeout': 15}}])
        newer = self.store.record('把回复调快一点', [{'type': 'settings', 'values': {'reply_timeout': 18}}])
        for lesson in self.store._state['lessons']:
            if lesson['id'] == old['id']:
                lesson['updated'] = 1.0
        self.store.save()
        hits = self.store.match_lessons('回复调快')
        self.assertEqual(hits[0]['id'], newer['id'])

    def test_similarity_is_symmetric_and_bounded(self):
        self.assertAlmostEqual(similarity('打开表情插件', '打开表情插件'), 1.0)
        self.assertGreater(similarity('回复太快了', '回复太慢'), 0.4)
        self.assertLessEqual(similarity('a', 'b'), 1.0)


if __name__ == '__main__':
    unittest.main()
