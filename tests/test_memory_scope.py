"""统一记忆 scope（通道 + 人设）：迁移、默认值、跨 scope 召回排序、合并隔离。"""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.unified_memory import CHANNELS, DEFAULT_CHANNEL, _COLUMNS, UnifiedMemory

LEGACY_BASE = '''CREATE TABLE unified_memories(
    id TEXT PRIMARY KEY, source TEXT, content TEXT, importance INTEGER,
    created REAL, last_used REAL, uses INTEGER, expires_at REAL,
    tags TEXT, persona TEXT)'''
LEGACY_EXTRAS = (
    ('layer', "TEXT NOT NULL DEFAULT 'fact'"),
    ('keywords', "TEXT NOT NULL DEFAULT '[]'"),
    ('project', "TEXT NOT NULL DEFAULT ''"),
    ('status', "TEXT NOT NULL DEFAULT 'active'"),
    ('confidence', 'INTEGER NOT NULL DEFAULT 60'),
    ('updated', 'REAL NOT NULL DEFAULT 0'),
    ('evidence', "TEXT NOT NULL DEFAULT ''"),
    ('goal', "TEXT NOT NULL DEFAULT ''"),
)
COLUMN_NAMES = [name.strip() for name in _COLUMNS.split(',')]
LEGACY_COLUMNS = [name for name in COLUMN_NAMES if name != 'channel']
_PLACEHOLDERS = ','.join('?' * len(COLUMN_NAMES))
_LEGACY_PLACEHOLDERS = ','.join('?' * len(LEGACY_COLUMNS))


def _values(identifier, content, channel=DEFAULT_CHANNEL, persona='', layer='fact',
            project='', importance=50, status='active', updated=None, source='desktop'):
    now = time.time() if updated is None else updated
    return {
        'id': identifier, 'source': source, 'content': content,
        'importance': importance, 'created': now, 'last_used': now, 'uses': 0,
        'expires_at': 0, 'tags': '[]', 'persona': persona, 'layer': layer,
        'keywords': '[]', 'project': project, 'status': status,
        'confidence': importance, 'updated': now, 'evidence': '', 'goal': '',
        'channel': channel,
    }


def seed(db, identifier, content, **kwargs):
    """直接入库，绕过 remember 的相似合并，用来造重复行/指定 scope。"""
    values = _values(identifier, content, **kwargs)
    db.execute('INSERT INTO unified_memories(' + _COLUMNS + ') VALUES(' + _PLACEHOLDERS + ')',
               tuple(values[name] for name in COLUMN_NAMES))
    db.commit()


def make_legacy_db(with_extras=True):
    db = sqlite3.connect(':memory:')
    db.execute(LEGACY_BASE)
    if with_extras:
        for name, ddl in LEGACY_EXTRAS:
            db.execute(f'ALTER TABLE unified_memories ADD COLUMN {name} {ddl}')
    db.commit()
    return db


def seed_legacy(db, identifier, content, persona=None, layer='soul', importance=60,
                source='desktop'):
    values = _values(identifier, content, persona=persona, layer=layer, importance=importance,
                     source=source)
    db.execute('INSERT INTO unified_memories(' + ','.join(LEGACY_COLUMNS) + ') VALUES('
               + _LEGACY_PLACEHOLDERS + ')',
               tuple(values[name] for name in LEGACY_COLUMNS))
    db.commit()


class ScopeMigrationTests(unittest.TestCase):
    """老库（没有 channel 列）打开后必须补列、保数据、可重复执行。"""

    def test_legacy_db_gets_channel_column_and_keeps_rows(self):
        db = make_legacy_db()
        seed_legacy(db, 'old-1', '老记忆：主人养的猫叫团子', persona='鲸鱼娘')
        seed_legacy(db, 'old-2', '老记忆：项目代号是鲸歌')
        memory = UnifiedMemory(db)
        self.assertEqual(memory.count(), 2)
        self.assertEqual(memory.get('old-1')['persona'], '鲸鱼娘')
        columns = [row[1] for row in db.execute('PRAGMA table_info(unified_memories)')]
        self.assertIn('channel', columns)
        self.assertEqual(columns.count('channel'), 1)
        db.close()

    def test_legacy_rows_read_as_console_and_empty_persona(self):
        db = make_legacy_db()
        seed_legacy(db, 'old-1', '老记忆：默认通道', persona=None)
        seed_legacy(db, 'old-2', '老记忆：有名人设', persona='鲸鱼娘')
        memory = UnifiedMemory(db)
        self.assertEqual(memory.get('old-1')['channel'], 'console')
        self.assertEqual(memory.get('old-1')['persona'], '')
        self.assertEqual(memory.get('old-2')['channel'], 'console')
        self.assertEqual(memory.get('old-2')['persona'], '鲸鱼娘')
        db.close()

    def test_oldest_schema_without_layers_migrates(self):
        db = sqlite3.connect(':memory:')
        db.execute(LEGACY_BASE)
        values = _values('oldest', '最老的记忆：只有基础列')
        db.execute('INSERT INTO unified_memories VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (values['id'], values['source'], values['content'], values['importance'],
                    values['created'], values['last_used'], values['uses'],
                    values['expires_at'], values['tags'], None))
        db.commit()
        memory = UnifiedMemory(db)
        item = memory.get('oldest')
        self.assertEqual(item['layer'], 'fact')
        self.assertEqual(item['channel'], 'console')
        self.assertEqual(item['persona'], '')
        self.assertEqual(memory.count(), 1)
        db.close()

    def test_migration_is_idempotent(self):
        db = make_legacy_db()
        seed_legacy(db, 'old-1', '老记忆：幂等检查')
        first = UnifiedMemory(db)
        self.assertEqual(first.count(), 1)
        columns_after_first = [row[1] for row in db.execute('PRAGMA table_info(unified_memories)')]
        second = UnifiedMemory(db)
        third = UnifiedMemory(db)
        self.assertEqual(second.count(), 1)
        self.assertEqual(third.count(), 1)
        self.assertEqual(
            columns_after_first,
            [row[1] for row in db.execute('PRAGMA table_info(unified_memories)')])
        self.assertEqual(third.get('old-1')['channel'], 'console')
        db.close()

    def test_migration_creates_scope_index(self):
        db = make_legacy_db()
        UnifiedMemory(db)
        indexes = {}
        for name in [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='unified_memories'")]:
            indexes[name] = [row[2] for row in db.execute(f'PRAGMA index_info({name})')]
        self.assertIn('unified_scope', indexes)
        self.assertEqual(indexes['unified_scope'], ['channel', 'persona'])
        db.close()

    def test_migration_failure_is_not_silent(self):
        db = make_legacy_db()

        class FlakyDB:
            """在补 channel 列时炸掉，验证错误不会被吞成静默。"""

            def __init__(self, real):
                self.real = real

            def execute(self, sql, params=()):
                if 'ADD COLUMN channel' in sql:
                    raise sqlite3.OperationalError('disk I/O error')
                return self.real.execute(sql, params)

            def commit(self):
                return self.real.commit()

        with self.assertRaises(RuntimeError) as caught:
            UnifiedMemory(FlakyDB(db))
        self.assertIn('scope 迁移失败', str(caught.exception))
        self.assertIn('disk I/O error', str(caught.exception))
        db.close()


class ScopeDefaultTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def test_default_channel_is_console(self):
        item = self.memory.remember('没写通道的记忆')
        self.assertEqual(item['channel'], 'console')
        self.assertEqual(DEFAULT_CHANNEL, 'console')

    def test_default_persona_is_empty(self):
        item = self.memory.remember('没写人设的记忆')
        self.assertEqual(item['persona'], '')
        self.assertEqual(CHANNELS, ('qq', 'console', 'local'))

    def test_remember_rejects_unknown_channel(self):
        with self.assertRaises(ValueError):
            self.memory.remember('通道写错的记忆', channel='wechat')
        self.assertEqual(self.memory.count(), 0)

    def test_remember_normalizes_channel_case(self):
        item = self.memory.remember('大写通道', channel='  QQ  ')
        self.assertEqual(item['channel'], 'qq')
        self.assertEqual(self.memory.get(item['id'])['channel'], 'qq')

    def test_explicit_channels_roundtrip(self):
        for channel in CHANNELS:
            item = self.memory.remember(f'{channel} 通道的记忆', channel=channel, persona='P')
            self.assertEqual(self.memory.get(item['id'])['channel'], channel)

    def test_empty_db(self):
        self.assertEqual(self.memory.count(), 0)
        self.assertEqual(self.memory.search(''), [])
        self.assertEqual(self.memory.search('随便'), [])
        self.assertEqual(self.memory.context_for_prompt('随便'), '')
        self.assertEqual(self.memory.context_for_prompt('', first_turn=True), '')


class ChannelBackfillTests(unittest.TestCase):
    """W4c：按 source='qq' 幂等回填 channel，别的一概不动。"""

    def test_backfill_maps_qq_rows_only(self):
        db = make_legacy_db()
        seed_legacy(db, 'qq-old', 'QQ 老记忆：群里说过周五聚餐', source='qq')
        seed_legacy(db, 'desk-old', '桌面老记忆：偏好先给结论', source='desktop')
        memory = UnifiedMemory(db)
        self.assertEqual(memory.get('qq-old')['channel'], 'qq')
        self.assertEqual(memory.get('desk-old')['channel'], 'console')
        self.assertEqual(memory.count(), 2)
        db.close()

    def test_backfill_is_idempotent_and_spares_other_rows(self):
        db = make_legacy_db()
        seed_legacy(db, 'qq-old', 'QQ 老记忆：群里说过周五聚餐', source='qq')
        seed_legacy(db, 'desk-old', '桌面老记忆：偏好先给结论', source='desktop')
        UnifiedMemory(db)
        seed(db, 'qq-local', 'QQ 新链路显式写 local 通道', channel='local', source='qq')
        seed(db, 'desk-qq', '桌面行被显式标成 qq 通道', channel='qq', source='desktop')
        UnifiedMemory(db)
        UnifiedMemory(db)
        rows = {row[0]: row[1] for row in
                db.execute('SELECT id,channel FROM unified_memories')}
        self.assertEqual(rows['qq-old'], 'qq')
        self.assertEqual(rows['desk-old'], 'console')
        self.assertEqual(rows['qq-local'], 'local')
        self.assertEqual(rows['desk-qq'], 'qq')
        self.assertEqual(len(rows), 4)
        db.close()

    def test_backfilled_qq_row_reaches_qq_prompt(self):
        """这条就是回填的意义：QQ 老记忆不能在 channel='qq' 的提示里消失。"""
        db = make_legacy_db()
        seed_legacy(db, 'qq-old', 'QQ 老记忆：主人提过周五聚餐', source='qq', layer='fact')
        memory = UnifiedMemory(db)
        text = memory.context_for_prompt('聚餐', channel='qq')
        self.assertIn('周五聚餐', text)
        self.assertIn('同通道', text)
        self.assertNotIn('跨通道', text)
        hits = memory.search('', channel='qq', strict_scope=True)
        self.assertEqual([hit['id'] for hit in hits], ['qq-old'])
        db.close()

    def test_backfill_does_not_touch_qq_rows_already_channels(self):
        db = sqlite3.connect(':memory:')
        memory = UnifiedMemory(db)
        qq = memory.remember('QQ 新写入：群里说了周五聚餐', source='qq', channel='qq')
        local = memory.remember('本地写入：周四对账', source='qq', channel='local')
        UnifiedMemory(db)
        self.assertEqual(memory.get(qq['id'])['channel'], 'qq')
        self.assertEqual(memory.get(local['id'])['channel'], 'local')
        db.close()


class ChannelValidationTests(unittest.TestCase):
    """读路径与写路径对非法通道的行为一致：直接 ValueError。"""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def test_search_rejects_unknown_channel(self):
        self.memory.remember('一条普通记忆')
        with self.assertRaises(ValueError):
            self.memory.search('', channel='wechat')
        with self.assertRaises(ValueError):
            self.memory.search('', channel='wechat', strict_scope=True)
        self.assertEqual(len(self.memory.search('', channel='')), 1)  # 空通道 = 不过滤、不报错

    def test_context_for_prompt_rejects_unknown_channel(self):
        self.memory.remember('一条普通记忆')
        with self.assertRaises(ValueError):
            self.memory.context_for_prompt('', channel='wechat')
        with self.assertRaises(ValueError):
            self.memory.context_for_prompt('', channel='wechat', first_turn=True, private=True)

    def test_search_accepts_uppercase_channel(self):
        item = self.memory.remember('QQ 记忆：群里说了聚餐', channel='qq')
        hits = self.memory.search('', channel=' QQ ', strict_scope=True)
        self.assertEqual([hit['id'] for hit in hits], [item['id']])


class ProjectLayerScopeTests(unittest.TestCase):
    """W4d：首轮注入的 layer='project' 段落也必须过 scope 冲突判定（原来漏了）。"""

    GROUP = '123456'

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def _seed_project_row(self, channel=DEFAULT_CHANNEL, persona=''):
        return seed(self.db, 'proj-1', '控制台项目正文：内部代号鲸歌，勿外传',
                    channel=channel, persona=persona, layer='project', project=self.GROUP)

    def test_first_turn_project_section_skips_cross_channel_row(self):
        self._seed_project_row()
        text = self.memory.context_for_prompt('', project=self.GROUP, channel='qq',
                                              allow_cross_scope=False, first_turn=True)
        self.assertEqual(text, '')
        self.assertNotIn('内部代号鲸歌', text)

    def test_first_turn_project_section_keeps_same_channel_row(self):
        self._seed_project_row()
        text = self.memory.context_for_prompt('', project=self.GROUP, channel='console',
                                              allow_cross_scope=False, first_turn=True)
        self.assertIn('内部代号鲸歌', text)
        self.assertIn('项目：', text)
        self.assertIn('同通道', text)
        self.assertNotIn('跨通道', text)

    def test_first_turn_project_section_follows_allow_cross_scope(self):
        self._seed_project_row()
        text = self.memory.context_for_prompt('', project=self.GROUP, channel='qq',
                                              allow_cross_scope=True, first_turn=True)
        self.assertIn('内部代号鲸歌', text)
        self.assertIn('跨通道', text)

    def test_first_turn_project_section_skips_cross_persona_row(self):
        self._seed_project_row(persona='鲸鱼娘')
        blocked = self.memory.context_for_prompt('', project=self.GROUP, persona='别的皮套',
                                                 first_turn=True)
        self.assertEqual(blocked, '')
        allowed = self.memory.context_for_prompt('', project=self.GROUP, persona='鲸鱼娘',
                                                 first_turn=True)
        self.assertIn('内部代号鲸歌', allowed)
        self.assertIn('同人设', allowed)


class ReflectScopeTests(unittest.TestCase):
    """W4e：reflect 透传 channel/persona；不传时与改前完全一致。"""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def test_reflect_without_scope_keeps_old_behaviour(self):
        item = self.memory.reflect('教训：先备份再改库', lesson='下次别再手改生产库的索引文件')
        self.assertEqual(item['channel'], DEFAULT_CHANNEL)
        self.assertEqual(item['persona'], '')
        self.assertEqual(item['layer'], 'lesson')
        self.assertEqual(item['source'], 'chat')
        rows = self.db.execute(
            'SELECT channel,persona,layer,source FROM unified_memories ORDER BY rowid').fetchall()
        self.assertEqual(rows, [('console', '', 'lesson', 'chat'),
                                ('console', '', 'lesson', 'chat')])

    def test_reflect_passes_scope_to_both_lesson_rows(self):
        item = self.memory.reflect('教训：桌面先给结论再展开', lesson='下次不要替用户改配置',
                                   channel='console', persona='鲸鱼娘')
        self.assertEqual(item['channel'], 'console')
        self.assertEqual(item['persona'], '鲸鱼娘')
        pairs = self.db.execute(
            "SELECT channel,persona FROM unified_memories WHERE layer='lesson'").fetchall()
        self.assertEqual(pairs, [('console', '鲸鱼娘'), ('console', '鲸鱼娘')])

    def test_reflected_lesson_scores_same_persona(self):
        self.memory.reflect('教训：桌面先给结论再展开', channel='console', persona='鲸鱼娘')
        same = self.memory.search('', persona='鲸鱼娘', limit=5)
        self.assertEqual(same[0]['scope_score'], 1)
        self.assertEqual(same[0]['scope_mark'], '【同人设】')
        other = self.memory.search('', persona='别的皮套', limit=5)
        self.assertEqual(other[0]['scope_score'], 0)
        self.assertEqual(other[0]['scope_mark'], '【跨人设】')

    def test_reflect_honours_channel_validation(self):
        with self.assertRaises(ValueError):
            self.memory.reflect('教训：通道写错', channel='wechat')


class ScopeSearchTests(unittest.TestCase):
    """跨 scope 召回不被过滤，但同 scope 排前面。"""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)
        self.items = {
            'qa': self.memory.remember('群里说周五聚餐', channel='qq', persona='A', importance=10),
            'ca': self.memory.remember('猫粮放在厨房柜子', channel='console', persona='A', importance=70),
            'qb': self.memory.remember('周报模板放在共享盘', channel='qq', persona='B', importance=80),
            'cb': self.memory.remember('桌面偏好先给结论', channel='console', persona='B', importance=95),
        }

    def tearDown(self):
        self.db.close()

    def test_cross_scope_rows_are_not_filtered(self):
        hits = self.memory.search('', channel='qq', persona='A', limit=10)
        self.assertEqual(len(hits), 4)
        self.assertEqual({hit['channel'] for hit in hits}, {'qq', 'console'})

    def test_same_scope_sorts_first(self):
        hits = self.memory.search('', channel='qq', persona='A', limit=10)
        self.assertEqual(hits[0]['id'], self.items['qa']['id'])
        scores = [hit['scope_score'] for hit in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(scores[0], 2)
        self.assertEqual(hits[-1]['id'], self.items['cb']['id'])
        self.assertEqual(hits[-1]['scope_score'], 0)

    def test_scope_score_formula(self):
        hits = self.memory.search('', channel='qq', persona='A', limit=10)
        scored = {hit['id']: hit['scope_score'] for hit in hits}
        self.assertEqual(scored[self.items['qa']['id']], 2)
        self.assertEqual(scored[self.items['ca']['id']], 1)
        self.assertEqual(scored[self.items['qb']['id']], 1)
        self.assertEqual(scored[self.items['cb']['id']], 0)

    def test_channel_only_scope_score(self):
        hits = self.memory.search('', channel='qq', limit=10)
        top = {hit['id'] for hit in hits[:2]}
        self.assertEqual(top, {self.items['qa']['id'], self.items['qb']['id']})
        self.assertEqual([hit['scope_score'] for hit in hits], [1, 1, 0, 0])

    def test_persona_only_scope_score(self):
        hits = self.memory.search('', persona='A', limit=10)
        top = {hit['id'] for hit in hits[:2]}
        self.assertEqual(top, {self.items['qa']['id'], self.items['ca']['id']})
        self.assertEqual([hit['scope_score'] for hit in hits], [1, 1, 0, 0])

    def test_no_scope_keeps_old_order(self):
        hits = self.memory.search('', limit=10)
        self.assertEqual([hit['id'] for hit in hits][0], self.items['cb']['id'])
        self.assertEqual([hit['scope_score'] for hit in hits], [0, 0, 0, 0])
        self.assertEqual([hit['scope_mark'] for hit in hits], ['', '', '', ''])

    def test_strict_scope_filters_channel(self):
        plain = self.memory.remember('没人设的普通记忆：记得带伞')
        hits = self.memory.search('', channel='qq', persona='A', strict_scope=True, limit=10)
        self.assertEqual({hit['id'] for hit in hits}, {self.items['qa']['id']})
        self.assertNotIn(plain['id'], {hit['id'] for hit in hits})

    def test_strict_scope_filters_persona(self):
        hits = self.memory.search('', persona='B', strict_scope=True, limit=10)
        self.assertEqual(sorted(hit['id'] for hit in hits),
                         sorted([self.items['qb']['id'], self.items['cb']['id']]))

    def test_strict_scope_without_scope_is_noop(self):
        hits = self.memory.search('', strict_scope=True, limit=10)
        self.assertEqual(len(hits), 4)

    def test_update_keeps_scope(self):
        item = self.memory.update(self.items['qa']['id'], content='改过的内容')
        self.assertEqual(item['channel'], 'qq')
        self.assertEqual(item['persona'], 'A')


class ScopeMergeTests(unittest.TestCase):
    """相似合并只在同 scope 内发生。"""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def test_different_persona_not_merged(self):
        self.memory.remember('主人喜欢喝无糖乌龙茶', channel='console', persona='A')
        self.memory.remember('主人喜欢喝无糖乌龙茶', channel='console', persona='B')
        self.assertEqual(self.memory.count(), 2)
        personas = sorted(
            item['persona'] for item in self.memory.search('', limit=10))
        self.assertEqual(personas, ['A', 'B'])

    def test_different_channel_not_merged(self):
        self.memory.remember('主人喜欢喝无糖乌龙茶', channel='qq', persona='A')
        self.memory.remember('主人喜欢喝无糖乌龙茶', channel='console', persona='A')
        self.assertEqual(self.memory.count(), 2)

    def test_same_scope_still_merges(self):
        first = self.memory.remember('主人喜欢喝无糖乌龙茶', channel='qq', persona='A')
        second = self.memory.remember('主人喜欢喝无糖乌龙茶', channel='qq', persona='A')
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second.get('merged'))
        self.assertEqual(self.memory.count(), 1)
        self.assertEqual(second['channel'], 'qq')
        self.assertEqual(second['persona'], 'A')

    def test_default_scope_merge_unaffected(self):
        first = self.memory.remember('偏好：回答先给结论')
        second = self.memory.remember('偏好：回答先给结论')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.memory.count(), 1)

    def test_dream_does_not_merge_across_persona(self):
        seed(self.db, 'd1', '重复事实：主人在杭州', persona='A')
        seed(self.db, 'd2', '重复事实：主人在杭州', persona='B')
        result = self.memory.dream()
        self.assertEqual(result['merged'], 0)
        self.assertEqual(self.memory.count(), 2)

    def test_dream_still_merges_same_scope(self):
        seed(self.db, 'd1', '重复事实：主人在杭州', persona='A')
        seed(self.db, 'd2', '重复事实：主人在杭州', persona='A')
        result = self.memory.dream()
        self.assertEqual(result['merged'], 1)
        self.assertEqual(self.memory.count(), 1)


class ScopePromptTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()

    def test_context_marks_same_scope(self):
        self.memory.remember('主人喜欢清晨的雨声', channel='console', persona='鲸鱼娘')
        text = self.memory.context_for_prompt('雨声', channel='console', persona='鲸鱼娘')
        self.assertIn('主人喜欢清晨的雨声', text)
        self.assertIn('【同通道/同人设】', text)

    def test_context_drops_cross_persona_by_default(self):
        self.memory.remember('鲸鱼娘的记忆：主人喜欢清晨的雨声',
                             channel='console', persona='鲸鱼娘')
        self.memory.remember('黑猫的记忆：主人怕打雷的雨声',
                             channel='console', persona='黑猫')
        text = self.memory.context_for_prompt('雨声', channel='console', persona='鲸鱼娘')
        self.assertIn('鲸鱼娘的记忆', text)
        self.assertNotIn('黑猫的记忆', text)

    def test_context_allow_cross_scope_marks_cross(self):
        self.memory.remember('鲸鱼娘的记忆：主人喜欢清晨的雨声',
                             channel='console', persona='鲸鱼娘')
        self.memory.remember('黑猫的记忆：主人怕打雷的雨声',
                             channel='console', persona='黑猫')
        text = self.memory.context_for_prompt('雨声', channel='console', persona='鲸鱼娘',
                                              allow_cross_scope=True)
        self.assertIn('黑猫的记忆', text)
        self.assertIn('【同通道/跨人设】', text)

    def test_context_without_scope_keeps_old_output(self):
        self.memory.remember('主人喜欢清晨的雨声', channel='console', persona='鲸鱼娘')
        text = self.memory.context_for_prompt('雨声')
        self.assertIn('主人喜欢清晨的雨声', text)
        self.assertNotIn('同人设', text)
        self.assertNotIn('同通道', text)
        self.assertNotIn('跨通道', text)

    def test_context_first_turn_marks_scope(self):
        self.memory.remember('我是鲸鱼娘，住在鲸歌号上', layer='soul', channel='console',
                             persona='鲸鱼娘')
        text = self.memory.context_for_prompt('', channel='console', persona='鲸鱼娘',
                                              first_turn=True, private=True)
        self.assertIn('鲸鱼娘', text)
        self.assertIn('同人设', text)


class ScopeSafetyTests(unittest.TestCase):
    def test_sql_injection_characters_are_escaped(self):
        db = sqlite3.connect(':memory:')
        memory = UnifiedMemory(db)
        evil = "'; DROP TABLE unified_memories; --"
        item = memory.remember('注入测试内容 ' + evil, persona=evil, channel='local')
        self.assertEqual(item['persona'], evil)
        hits = memory.search(evil, channel='local', persona=evil, strict_scope=True)
        self.assertEqual(hits[0]['id'], item['id'])
        self.assertEqual(memory.count(), 1)
        db.close()

    def test_legacy_row_visible_to_unscoped_search(self):
        db = make_legacy_db()
        seed_legacy(db, 'old-1', '老记忆：团子爱吃冻干', persona='鲸鱼娘')
        memory = UnifiedMemory(db)
        hits = memory.search('冻干')
        self.assertEqual(hits[0]['id'], 'old-1')
        self.assertEqual(hits[0]['channel'], 'console')
        db.close()

    def test_two_connections_concurrent_writes(self):
        errors = []

        def worker(tag):
            try:
                conn = sqlite3.connect(str(path), timeout=10)
            except Exception as error:  # pragma: no cover
                errors.append(repr(error))
                return
            try:
                memory = UnifiedMemory(conn)
                for index in range(10):
                    memory.remember(f'并发写入 {tag} {index}', channel='local',
                                    persona='并发', project=f'p{index}', importance=40)
            except Exception as error:
                errors.append(f'{tag}: {error!r}')
            finally:
                conn.close()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'memory.sqlite3'
            threads = [threading.Thread(target=worker, args=(tag,)) for tag in ('甲', '乙')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            self.assertEqual(errors, [])
            conn = sqlite3.connect(str(path), timeout=10)
            count = conn.execute('SELECT count(*) FROM unified_memories').fetchone()[0]
            channels = conn.execute(
                'SELECT DISTINCT channel FROM unified_memories').fetchall()
            conn.close()
        self.assertEqual(count, 20)
        self.assertEqual(channels, [('local',)])


if __name__ == '__main__':
    unittest.main()
