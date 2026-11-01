"""夜间固化：配置读取、素材收集、聚类、账本去重与只写 soul 层的边界。"""
import json
import random
import re
import sqlite3
import tempfile
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from src import agent_dream
from src.unified_memory import UnifiedMemory

NOW = datetime(2026, 10, 26, 22, 30).timestamp()
DAY_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
COLUMNS = ('id,source,content,importance,created,last_used,uses,expires_at,tags,persona,'
           'layer,keywords,project,status,confidence,updated,evidence,goal,channel')
SNAPSHOT = ('content', 'importance', 'layer', 'status', 'project', 'tags', 'evidence',
            'goal', 'channel', 'persona', 'confidence')


def write_diary(folder, blocks):
    lines = ['# 鲸鱼娘日记', '', '> 自动整理，中文可读。', '']
    for day, text in blocks:
        lines.extend(['## ' + day, '', text, ''])
    (Path(folder) / '日记.md').write_text('\n'.join(lines), encoding='utf-8')


def insert_raw(db, content, *, source='desktop', layer='fact', importance=50,
               updated=None, tags='[]'):
    stamp = time.time() if updated is None else float(updated)
    db.execute('INSERT INTO unified_memories(' + COLUMNS + ') VALUES('
               + ','.join('?' * 19) + ')',
               ('raw-' + uuid.uuid4().hex, source, content, importance, stamp, stamp, 0, 0,
                tags, '', layer, '[]', '', 'active', importance, stamp, '', '', 'console'))
    db.commit()


class _MemoryCase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ledger(self):
        return agent_dream.ledger_path(self.data)

    def ledger_keys(self):
        return json.loads(self.ledger().read_text(encoding='utf-8'))['keys']

    @staticmethod
    def enabled(**extra):
        section = {'dream_enabled': True}
        section.update(extra)
        return {'ai': section}

    @staticmethod
    def snapshot(item):
        return {key: item[key] for key in SNAPSHOT}


class SettingsTests(unittest.TestCase):
    def test_defaults_when_config_missing(self):
        self.assertEqual(agent_dream.settings(None), agent_dream.DEFAULTS)
        self.assertEqual(agent_dream.settings({}), agent_dream.DEFAULTS)
        self.assertIsNot(agent_dream.settings(None), agent_dream.DEFAULTS)

    def test_nested_section_is_read(self):
        conf = agent_dream.settings({'ai': {'dream_enabled': True, 'dream_lookback_days': 5,
                                            'dream_max_clusters': 4, 'dream_rate': 0.5}})
        self.assertTrue(conf['enabled'])
        self.assertEqual((conf['lookback_days'], conf['max_clusters']), (5, 4))
        self.assertEqual(conf['rate'], 0.5)

    def test_values_are_clamped(self):
        high = agent_dream.settings({'ai': {'dream_lookback_days': 999,
                                            'dream_max_clusters': 99, 'dream_rate': 5}})
        low = agent_dream.settings({'ai': {'dream_lookback_days': 0,
                                           'dream_max_clusters': 0, 'dream_rate': -2}})
        self.assertEqual((high['lookback_days'], high['max_clusters'], high['rate']),
                         (30, 10, 1.0))
        self.assertEqual((low['lookback_days'], low['max_clusters'], low['rate']),
                         (1, 1, 0.0))

    def test_config_object_with_get(self):
        class Config:
            def __init__(self):
                self.config = {'ai': {'dream_enabled': True, 'dream_rate': 0.2}}

            def get(self, key, default=None):
                value = self.config
                for part in key.split('.'):
                    if not isinstance(value, dict) or part not in value:
                        return default
                    value = value[part]
                return value

        conf = agent_dream.settings(Config())
        self.assertTrue(conf['enabled'])
        self.assertEqual(conf['rate'], 0.2)

    def test_flat_dotted_keys_are_read(self):
        conf = agent_dream.settings({'ai.dream_enabled': 1, 'ai.dream_rollback': 9,
                                     'ai.dream_max_clusters': 7})
        self.assertTrue(conf['enabled'])
        self.assertEqual(conf['max_clusters'], 7)

    def test_invalid_values_fall_back(self):
        conf = agent_dream.settings({'ai': {'dream_enabled': '也许', 'dream_lookback_days': 'abc',
                                            'dream_max_clusters': None, 'dream_rate': 'NaN'}})
        self.assertEqual(conf, agent_dream.DEFAULTS)

    def test_boolean_strings(self):
        for value in ('true', 'on', '开', 1):
            self.assertTrue(agent_dream.settings({'ai': {'dream_enabled': value}})['enabled'])
        for value in ('false', 'off', '关闭', 0):
            self.assertFalse(agent_dream.settings({'ai': {'dream_enabled': value}})['enabled'])


class LedgerTests(_MemoryCase):
    def test_path_layout(self):
        self.assertEqual(agent_dream.ledger_path('/tmp/demo'),
                         Path('/tmp/demo') / 'qq-only' / 'dream-seen.json')
        self.assertEqual(agent_dream.ledger_path(Path('/tmp/demo')).name, 'dream-seen.json')

    def test_missing_or_corrupt_ledger_reads_empty(self):
        self.assertEqual(agent_dream._read_ledger(self.ledger()), [])
        self.ledger().parent.mkdir(parents=True)
        self.ledger().write_text('这不是 JSON', encoding='utf-8')
        self.assertEqual(agent_dream._read_ledger(self.ledger()), [])
        self.ledger().write_text('{"keys": "坏结构"}', encoding='utf-8')
        self.assertEqual(agent_dream._read_ledger(self.ledger()), [])
        self.ledger().write_text('{"version": 1, "keys": ["a", 1, "", "b"]}', encoding='utf-8')
        self.assertEqual(agent_dream._read_ledger(self.ledger()), ['a', 'b'])

    def test_write_keeps_at_most_4000_fifo(self):
        keys = ['k%05d' % index for index in range(4001)]
        agent_dream._write_ledger(self.ledger(), keys)
        saved = self.ledger_keys()
        self.assertEqual(len(saved), agent_dream.LEDGER_MAX)
        self.assertEqual(saved[0], keys[1])
        self.assertEqual(saved[-1], keys[-1])

    def test_key_uses_date_and_normalized_text(self):
        left = agent_dream._key({'date': '2026-10-26', 'text': '周末 去海边！'})
        right = agent_dream._key({'date': '2026-10-26', 'text': '周末去海边'})
        other = agent_dream._key({'date': '2026-10-25', 'text': '周末去海边'})
        self.assertEqual(left, right)
        self.assertNotEqual(left, other)
        self.assertEqual(len(left), 16)


class ClusterTests(unittest.TestCase):
    def test_similarity_criterion_alone(self):
        left, right = '周末想去海边', '周末不去海边'
        self.assertGreaterEqual(agent_dream._ratio(left, right), 0.3)
        self.assertLess(agent_dream._lcs_len(left, right), 4)
        self.assertTrue(agent_dream._same_thing(left, right))

    def test_lcs_criterion_alone(self):
        left = '四字连续' + '甲乙丙丁戊己庚辛壬癸子丑'
        right = '卯辰巳午未申酉戌亥天地玄黄' + '四字连续'
        self.assertLess(agent_dream._ratio(left, right), 0.3)
        self.assertGreaterEqual(agent_dream._lcs_len(left, right), 4)
        self.assertTrue(agent_dream._same_thing(left, right))

    def test_unrelated_texts_are_not_same(self):
        self.assertFalse(agent_dream._same_thing('甲乙丙丁戊己庚辛', '子丑寅卯辰巳午未'))
        self.assertEqual(agent_dream._ratio('', '空的'), 0.0)

    def test_cluster_groups_same_topic_and_sorts_by_size(self):
        items = [{'text': text, 'from': 'diary', 'date': '2026-10-26'}
                 for text in ('周末想去海边看日落', '周末去海边看日落吧', '周末想去海边',
                              '数据库迁移前要备份', '明天整理会议记录')]
        clusters = agent_dream._cluster(items)
        self.assertEqual([len(cluster) for cluster in clusters], [3, 1, 1])
        self.assertEqual({member['text'] for member in clusters[0]},
                         {'周末想去海边看日落', '周末去海边看日落吧', '周末想去海边'})

    def test_same_day_paragraphs_are_not_one_cluster(self):
        items = [{'text': '主人今天带我去吃火锅，很开心。', 'from': 'diary', 'date': '2026-10-26'},
                 {'text': '今天研究插件架构，写了不少代码。', 'from': 'diary', 'date': '2026-10-26'}]
        self.assertFalse(agent_dream._same_thing(items[0]['text'], items[1]['text']))
        clusters = agent_dream._cluster(items)
        self.assertEqual([len(cluster) for cluster in clusters], [1, 1])


class CollectTests(_MemoryCase):
    def test_both_sources_and_date_shape(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落'),
                                ('2026-10-25', '今天整理了夜间固化')])
        self.memory.remember('主人的生日是十月五日', source='qq', layer='fact')
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual({item['from'] for item in items}, {'diary', 'memory'})
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertRegex(item['date'], DAY_RE)
            self.assertGreaterEqual(len(item['text']), 4)

    def test_short_and_blank_text_is_skipped(self):
        write_diary(self.data, [('2026-10-26', '好的')])
        insert_raw(self.db, '在')
        insert_raw(self.db, 'abcd')
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual([item['text'] for item in items], ['abcd'])

    def test_same_text_is_deduped(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        insert_raw(self.db, '周末想去海边看日落')
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['from'], 'diary')

    def test_same_day_paragraphs_stay_separate(self):
        write_diary(self.data, [('2026-10-26',
                                 '主人今天带我去吃火锅，很开心。\n\n今天研究插件架构。')])
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual([item['text'] for item in items],
                         ['主人今天带我去吃火锅，很开心。', '今天研究插件架构。'])
        self.assertEqual({item['date'] for item in items}, {'2026-10-26'})
        self.assertNotIn('火锅', items[1]['text'])

    def test_long_paragraph_is_split_under_max_text(self):
        text = '。'.join('研究插件架构的第%d步要写清楚边界' % index for index in range(20)) + '。'
        self.assertGreater(len(text), agent_dream.MAX_TEXT)
        write_diary(self.data, [('2026-10-26', text)])
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertGreater(len(items), 1)
        for item in items:
            self.assertLessEqual(len(item['text']), agent_dream.MAX_TEXT)
        self.assertEqual(''.join(item['text'] for item in items), text)
        write_diary(self.data, [('2026-10-25', '丙' * 300 + '丁' * 400)])
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        tail = [item['text'] for item in items if item['date'] == '2026-10-25']
        self.assertEqual([len(piece) for piece in tail], [300, 300, 100])

    def test_same_day_repeated_paragraph_is_deduped(self):
        write_diary(self.data, [('2026-10-26',
                                 '周末想去海边看日落。\n\n周末想去海边看日落！\n\n今天研究插件架构。')])
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual([item['text'] for item in items],
                         ['周末想去海边看日落。', '今天研究插件架构。'])

    def test_diary_rows_follow_the_same_split(self):
        self.memory.write_diary('2026-10-26',
                                '今天整理夜间固化的测试用例。\n\n顺便跑了整个测试套件。')
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual([item['text'] for item in items],
                         ['今天整理夜间固化的测试用例。', '顺便跑了整个测试套件。'])
        self.assertEqual({item['from'] for item in items}, {'diary'})

    def test_soul_and_portrait_are_excluded(self):
        insert_raw(self.db, '我是一只会做梦的鲸鱼', layer='soul')
        insert_raw(self.db, '画像：主人喜欢安静', source='portrait')
        self.memory.remember('项目要整理夜间固化', layer='project')
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual([item['text'] for item in items], ['项目要整理夜间固化'])

    def test_diary_outside_lookback_is_skipped(self):
        write_diary(self.data, [('2026-10-14', '很久以前的日记不该被固化'),
                                ('2026-10-25', '昨天的日记可以固化')])
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual([item['text'] for item in items], ['昨天的日记可以固化'])

    def test_lookback_one_day_reads_only_today(self):
        write_diary(self.data, [('2026-10-26', '今天的日记可以固化'),
                                ('2026-10-25', '昨天的日记不该被固化')])
        items = agent_dream.collect(self.memory, self.data, lookback_days=1, now=NOW)
        self.assertEqual([item['text'] for item in items], ['今天的日记可以固化'])

    def test_falls_back_to_diary_rows(self):
        self.memory.write_diary('2026-10-26', '今天整理了夜间固化的测试用例')
        items = agent_dream.collect(self.memory, self.data, lookback_days=3, now=NOW)
        self.assertEqual(items, [{'text': '今天整理了夜间固化的测试用例',
                                  'from': 'diary', 'date': '2026-10-26'}])

    def test_empty_sources_return_empty_list(self):
        self.assertEqual(agent_dream.collect(self.memory, self.data,
                                             lookback_days=3, now=NOW), [])


class ConsolidateTests(_MemoryCase):
    def test_disabled_without_force(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        result = agent_dream.consolidate(self.memory, self.data, config=None, now=NOW,
                                         rng=random.Random(1))
        self.assertEqual(set(result), {'ok', 'reason', 'clusters', 'written', 'items'})
        self.assertEqual((result['ok'], result['reason'], result['written']),
                         (False, 'disabled', 0))
        self.assertEqual(self.memory.count(), 0)
        self.assertFalse(self.ledger().exists())

    def test_no_items_when_nothing_to_consolidate(self):
        result = agent_dream.consolidate(self.memory, self.data, config=self.enabled(), now=NOW)
        self.assertEqual((result['ok'], result['reason'], result['written']),
                         (False, 'no_items', 0))

    def test_force_bypasses_disabled_and_creates_soul_row(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        result = agent_dream.consolidate(self.memory, self.data, config=None, now=NOW,
                                         force=True, rng=random.Random(1))
        self.assertEqual((result['ok'], result['reason'], result['written'], result['clusters']),
                         (True, 'ok', 1, 1))
        entry = result['items'][0]
        self.assertTrue(entry['created'])
        self.assertNotIn('updated', entry)
        self.assertEqual((entry['mentions'], entry['importance']), (1, 55))
        saved = self.memory.get(entry['id'])
        self.assertEqual(saved['layer'], 'soul')
        self.assertIn('dream', saved['tags'])
        self.assertEqual(saved['channel'], 'console')

    def test_same_day_paragraphs_become_two_soul_rows(self):
        write_diary(self.data, [('2026-10-26',
                                 '主人今天带我去吃火锅，很开心。\n\n今天研究插件架构，写了不少代码。')])
        result = agent_dream.consolidate(self.memory, self.data,
                                         config=self.enabled(dream_max_clusters=10),
                                         now=NOW, force=True)
        self.assertEqual((result['clusters'], result['written']), (2, 2))
        rows = self.memory.search(layer='soul', limit=30)
        self.assertEqual(sorted(item['content'] for item in rows),
                         ['主人今天带我去吃火锅，很开心。', '今天研究插件架构，写了不少代码。'])
        for item in rows:
            self.assertLessEqual(len(item['content']), agent_dream.MAX_TEXT)

    def test_rate_gate_blocks_every_cluster(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落'),
                                ('2026-10-25', '数据库迁移前要备份'),
                                ('2026-10-24', '明天整理会议记录')])
        result = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=NOW, rng=random.Random(0))
        self.assertEqual((result['ok'], result['reason'], result['written'], result['clusters']),
                         (False, 'rate_skipped', 0, 3))
        self.assertEqual(self.memory.count(), 0)
        self.assertFalse(self.ledger().exists())

    def test_rate_gate_passes_and_records_ledger(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        result = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=NOW, rng=random.Random(1))
        self.assertEqual((result['ok'], result['reason'], result['written']), (True, 'ok', 1))
        self.assertEqual(len(self.ledger_keys()), 1)

    def test_max_clusters_truncates_the_night(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落'),
                                ('2026-10-25', '数据库迁移前要备份'),
                                ('2026-10-24', '明天整理会议记录'),
                                ('2026-10-23', '今天菜市场的草莓很便宜'),
                                ('2026-10-22', '下周要交一份周报')])
        result = agent_dream.consolidate(
            self.memory, self.data, config=self.enabled(dream_lookback_days=10,
                                                        dream_max_clusters=2),
            now=NOW, force=True)
        self.assertEqual((result['clusters'], result['written']), (5, 2))
        self.assertEqual(self.memory.layers()['soul'], 2)
        self.assertEqual(len(self.ledger_keys()), 2)

    def test_importance_follows_mentions(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落'),
                                ('2026-10-25', '周末去海边看日落吧'),
                                ('2026-10-24', '周末想去海边')])
        result = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=NOW, force=True)
        entry = result['items'][0]
        self.assertEqual((entry['mentions'], entry['importance']), (3, 85))
        self.assertEqual(self.memory.get(entry['id'])['importance'], 85)

    def test_importance_is_capped_at_100(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落'),
                                ('2026-10-25', '周末想去海边看日落了'),
                                ('2026-10-24', '周末想去看海边日落'),
                                ('2026-10-23', '周末想去海边看日落呀'),
                                ('2026-10-22', '周末要去海边看日落')])
        result = agent_dream.consolidate(
            self.memory, self.data, config=self.enabled(dream_lookback_days=10),
            now=NOW, force=True)
        self.assertEqual(result['written'], 1)
        self.assertEqual((result['items'][0]['mentions'], result['items'][0]['importance']),
                         (5, 100))

    def test_only_soul_layer_is_written(self):
        fact = self.memory.remember('主人喜欢安静', layer='fact', importance=70)
        lesson = self.memory.remember('回答问题先给结论', layer='lesson', importance=65)
        project = self.memory.remember('项目要整理夜间固化', layer='project', importance=60)
        topic = self.memory.remember('这周在聊草莓价格', layer='topic', importance=40)
        before = {item['id']: self.snapshot(item) for item in (fact, lesson, project, topic)}
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        result = agent_dream.consolidate(
            self.memory, self.data, config=self.enabled(dream_max_clusters=10),
            now=NOW, force=True)
        self.assertEqual(result['written'], 5)
        for identifier, saved in before.items():
            item = self.memory.get(identifier)
            self.assertEqual(self.snapshot(item), saved)
            self.assertNotIn('dream', item['tags'])
        soul_rows = [item for item in self.memory.search(layer='soul', limit=30)]
        self.assertEqual(len(soul_rows), 5)
        for item in soul_rows:
            self.assertEqual(item['layer'], 'soul')
            self.assertIn('dream', item['tags'])

    def test_second_run_writes_nothing(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        first = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                        now=NOW, force=True)
        second = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=NOW, force=True)
        self.assertEqual((first['written'], second['written']), (1, 0))
        self.assertEqual(second['reason'], 'no_items')
        self.assertEqual(self.memory.layers()['soul'], 1)
        self.assertEqual(len(self.ledger_keys()), 1)

    def test_same_topic_updates_existing_dream_row(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        first = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                        now=NOW, force=True)
        self.ledger().unlink()
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落的计划'),
                                ('2026-10-25', '周末想去海边看日落')])
        second = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=NOW, force=True)
        entry = second['items'][0]
        self.assertEqual(entry['id'], first['items'][0]['id'])
        self.assertTrue(entry['updated'])
        self.assertNotIn('created', entry)
        saved = self.memory.get(entry['id'])
        self.assertEqual(saved['content'], '周末想去海边看日落的计划')
        self.assertEqual(saved['importance'], 70)
        self.assertEqual(self.memory.layers()['soul'], 1)

    def test_corrupt_ledger_is_tolerated(self):
        self.ledger().parent.mkdir(parents=True)
        self.ledger().write_text('坏掉的账本', encoding='utf-8')
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        result = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=NOW, force=True)
        self.assertEqual((result['ok'], result['written']), (True, 1))
        self.assertEqual(len(self.ledger_keys()), 1)

    def test_ledger_keeps_4000_keys_after_run(self):
        keys = ['k%05d' % index for index in range(agent_dream.LEDGER_MAX)]
        agent_dream._write_ledger(self.ledger(), keys)
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        result = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=NOW, force=True)
        self.assertEqual(result['written'], 1)
        saved = self.ledger_keys()
        self.assertEqual(len(saved), agent_dream.LEDGER_MAX)
        self.assertNotIn(keys[0], saved)
        self.assertEqual(saved[-1],
                         agent_dream._key({'date': '2026-10-26', 'text': '周末想去海边看日落'}))

    def test_error_is_captured_not_raised(self):
        class Boom:
            def diaries(self, limit=30):
                return []

            def search(self, **kwargs):
                return []

            def remember(self, *args, **kwargs):
                raise RuntimeError('记忆库写坏了')

            def update(self, *args, **kwargs):
                raise RuntimeError('记忆库写坏了')

        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        result = agent_dream.consolidate(Boom(), self.data, config=self.enabled(),
                                         now=NOW, force=True)
        self.assertEqual((result['ok'], result['reason'], result['written']),
                         (False, 'error', 0))
        self.assertIn('记忆库写坏了', result['detail'])
        self.assertIn('RuntimeError', result['detail'])
        self.assertEqual(result['items'], [])

    def test_missing_memory_object_is_an_error_not_a_crash(self):
        write_diary(self.data, [('2026-10-26', '周末想去海边看日落')])
        result = agent_dream.consolidate(None, self.data, config=self.enabled(),
                                         now=NOW, force=True)
        self.assertEqual(result['reason'], 'error')


if __name__ == '__main__':
    unittest.main()
