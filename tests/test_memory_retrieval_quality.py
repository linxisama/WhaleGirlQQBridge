"""记忆检索质量：相关度优先、整条丢弃、跨层去重、字数统计自洽、空库不崩。"""
import sqlite3
import time
import unittest

from src.unified_memory import UnifiedMemory

_DAY = 86400
_COLUMNS = ('id,source,content,importance,created,last_used,uses,expires_at,tags,persona,'
            'layer,keywords,project,status,confidence,updated,evidence,goal,channel')


def raw_row(memory, text, layer='fact', importance=50, confidence=60, idle_days=0.0,
            uses=0, channel='console', keywords='[]'):
    """直接写一行并指定年龄：remember 会刷新 updated，测不了"旧但相关"。"""
    stamp = time.time() - idle_days * _DAY
    identifier = 'q-' + str(abs(hash((text, layer, idle_days, importance))))[:12]
    memory.db.execute('INSERT INTO unified_memories(' + _COLUMNS + ') VALUES('
                      + ','.join('?' * 19) + ')',
                      (identifier, 'desktop', text, importance, stamp, stamp, uses, 0,
                       '[]', '', layer, keywords, '', 'active', confidence, stamp, '', '',
                       channel))
    memory.db.commit()
    return identifier


class RetrievalQualityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def test_a_relevant_old_outranks_loosely_related_new(self):
        old = raw_row(self.memory, '主人喜欢无糖乌龙茶', importance=40, confidence=50,
                      idle_days=400)
        new = raw_row(self.memory, '乌龙茶具放在厨房柜子里', importance=95, confidence=95,
                      idle_days=0)
        hits = self.memory.search('无糖乌龙茶', limit=5)
        self.assertEqual([hit['id'] for hit in hits], [old, new])
        self.assertGreater(hits[0]['score'], hits[1]['score'])
        self.assertEqual(hits[0]['relevance'], 1.0)
        self.assertLess(hits[1]['relevance'], 1.0)
        # why 要能直接读：不看公式也知道为什么排这个顺序。
        self.assertIn('正文命中', hits[0]['why'])
        self.assertIn('重要40', hits[0]['why'])
        self.assertIn('相关', hits[1]['why'])

    def test_b_keyword_truncation_does_not_hide_a_memory(self):
        """长记忆的关键词只存前 24 个二元组，尾部的词查不到；正文扫描要兜住。"""
        text = ('主人最近在准备搬家，列了一堆要买的东西：书桌、台灯、收纳箱、窗帘、地毯，'
                '还有无糖乌龙茶')
        # remember() 返回整条记录（memory_api / agent_dream / test_unified_memory 都这么用），
        # 取 id 再喂给 get()；直接传 dict 会让 sqlite3 报 "type 'dict' is not supported"。
        identifier = self.memory.remember(text, source='desktop', importance=50)['id']
        self.assertNotIn('乌龙', self.memory.get(identifier)['keywords'])
        hits = self.memory.search('乌龙茶喝完了', limit=5)
        self.assertEqual([hit['id'] for hit in hits], [identifier])

    def test_c_over_budget_drops_whole_lines_not_half_ones(self):
        for index in range(6):
            self.memory.remember('第%d条：' % index + '很长的记忆内容' * 25, layer='fact',
                                 importance=90, channel='qq', keywords=['很长'])
        full = self.memory.context_for_prompt('很长', private=True, channel='qq',
                                             max_chars=100000)
        text = self.memory.context_for_prompt('很长', private=True, channel='qq', max_chars=300)
        self.assertLessEqual(len(text), 300)
        self.assertLess(len(text), len(full))
        # 注入的每一行都必须是 full 里完整的一行；硬截断会切出半条，这里就会不等。
        text_lines = text.split('\n')
        self.assertEqual(text_lines, full.split('\n')[:len(text_lines)])
        self.assertGreater(self.memory.last_context_stats['dropped'], 0)

    def test_d_same_content_across_layers_is_injected_once(self):
        body = '跨层重复的同一段记忆' + '，补充细节' * 60
        self.memory.remember(body, layer='soul', channel='qq', keywords=['跨层'])
        self.memory.remember(body, layer='fact', channel='qq', keywords=['跨层'])
        text = self.memory.context_for_prompt('跨层', private=True, channel='qq',
                                             first_turn=True)
        self.assertEqual(text.count('跨层重复的同一段记忆'), 1)

    def test_e_stats_match_the_injected_text(self):
        self.memory.remember('主人喜欢先看结论', channel='qq', keywords=['结论'])
        self.memory.remember('主人怕吵', channel='qq', keywords=['安静'])
        text = self.memory.context_for_prompt('结论', private=True, channel='qq',
                                             first_turn=True, max_chars=1600)
        stats = self.memory.last_context_stats
        self.assertEqual(stats['chars'], len(text))
        self.assertLessEqual(stats['chars'], stats['max_chars'])
        body = text.split('\n【记忆导引】')[0].split('\n', 1)[1]
        lines = body.split('\n')
        self.assertEqual(stats['items'], len(lines))
        self.assertEqual(stats['line_chars'], sum(len(line) for line in lines) + len(lines) - 1)

    def test_f_empty_and_archived_only_do_not_crash(self):
        self.assertEqual(self.memory.search('随便'), [])
        self.assertEqual(self.memory.context_for_prompt('随便'), '')
        self.assertEqual(self.memory.last_context_stats['items'], 0)
        self.memory.remember('已经归档的旧事', status='archived', channel='qq',
                             keywords=['旧事'])
        self.assertEqual(self.memory.search('旧事'), [])
        self.assertEqual(self.memory.context_for_prompt('旧事', private=True, channel='qq'), '')
        self.assertEqual(self.memory.last_context_stats['chars'], 0)


if __name__ == '__main__':
    unittest.main()
