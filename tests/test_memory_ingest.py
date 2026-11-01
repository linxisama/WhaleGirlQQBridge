"""记忆审计（失效路径 / 疑似冲突 / 冷宫）与零 token 提取。"""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src import memory_guard, memory_ingest
from src.unified_memory import UnifiedMemory

_DAY = 86400
_ROW = ('id,source,content,importance,created,last_used,uses,expires_at,tags,persona,'
        'layer,keywords,project,status,confidence,updated,evidence,goal,channel')


def raw_row(memory, text, layer='fact', project='', keywords='[]', uses=0,
            age_days=0.0, identifier=None):
    now = time.time()
    stamp = now - float(age_days) * _DAY
    identifier = identifier or ('t-' + str(abs(hash((text, layer, project))))[:12])
    memory.db.execute('INSERT INTO unified_memories(' + _ROW + ') VALUES('
                      + ','.join('?' * 19) + ')',
                      (identifier, 'qq', text, 50, stamp, stamp, uses, 0, '[]', '',
                       layer, keywords, project, 'active', 60, stamp, '', '', 'qq'))
    memory.db.commit()
    return identifier


class AuditBase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()


class DeadPathTests(AuditBase):
    def test_missing_path_is_reported(self):
        identifier = raw_row(self.memory, '资料在 D:\\no-such-dir-zzz\\plan.txt 里')
        found = memory_guard.find_dead_paths(memory_guard._active(self.memory))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['id'], identifier)
        self.assertEqual(found[0]['kind'], 'dead_path')
        self.assertIn('no-such-dir-zzz', found[0]['path'])

    def test_existing_path_is_not_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            import os
            os.makedirs(folder + '\\src')
            raw_row(self.memory, '仓库在 ' + folder + '\\src 里')
            self.assertEqual(memory_guard.find_dead_paths(memory_guard._active(self.memory)), [])

    def test_paths_in_dedupes_and_skips_relative(self):
        text = 'D:\\a\\b.txt 和 D:\\a\\b.txt 还有 src\\local.py'
        self.assertEqual(memory_guard.paths_in(text), ['D:\\a\\b.txt'])

    def test_truncated_path_counts_as_alive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            # 记忆里只写了目录名的一半：父目录里有同名前缀的子项，算活着
            real = folder + '\\project-2026'
            import os
            os.makedirs(real)
            self.assertTrue(memory_guard.path_alive(folder + '\\project'))
            self.assertFalse(memory_guard.path_alive(folder + '\\zzz-missing'))


class ConflictTests(AuditBase):
    def test_similar_but_different_rows_are_flagged(self):
        left = raw_row(self.memory, '主人喜欢喝冰美式不加糖', keywords='["主人","美式"]')
        right = raw_row(self.memory, '主人喜欢喝热美式加糖', keywords='["主人","美式"]')
        found = memory_guard.find_conflicts(memory_guard._active(self.memory))
        self.assertTrue(found)
        self.assertEqual(set(found[0]['ids']), {left, right})
        self.assertGreaterEqual(found[0]['similarity'], memory_guard.CONFLICT_LOW)
        self.assertLess(found[0]['similarity'], memory_guard.DUP_THRESHOLD)

    def test_near_duplicates_are_left_to_maintain(self):
        raw_row(self.memory, '主人喜欢喝冰美式不加糖')
        raw_row(self.memory, '主人喜欢喝冰美式不加糖。')
        self.assertEqual(memory_guard.find_conflicts(memory_guard._active(self.memory)), [])

    def test_different_layers_do_not_conflict(self):
        raw_row(self.memory, '主人喜欢喝冰美式不加糖', layer='fact')
        raw_row(self.memory, '主人喜欢喝热美式加糖', layer='lesson')
        self.assertEqual(memory_guard.find_conflicts(memory_guard._active(self.memory)), [])


class ColdTests(AuditBase):
    def test_old_unused_row_is_cold(self):
        identifier = raw_row(self.memory, '一条没人再用的旧记忆', age_days=40)
        found = memory_guard.find_cold(memory_guard._active(self.memory))
        self.assertEqual([item['id'] for item in found], [identifier])

    def test_recent_or_used_rows_are_not_cold(self):
        raw_row(self.memory, '刚写进来的记忆', age_days=1)
        raw_row(self.memory, '老的但用过', age_days=40, uses=3)
        self.assertEqual(memory_guard.find_cold(memory_guard._active(self.memory)), [])


class ReportTests(AuditBase):
    def test_report_includes_audit_keys_and_changes_nothing(self):
        raw_row(self.memory, '资料在 D:\\no-such-dir-zzz\\plan.txt 里', age_days=40)
        before = self.db.execute('SELECT count(*) FROM unified_memories').fetchone()[0]
        report = memory_guard.report(self.memory)
        self.assertIn('dead_paths', report)
        self.assertIn('conflicts', report)
        self.assertIn('cold', report)
        self.assertEqual(report['cold'][0]['layer'], 'fact')
        self.assertEqual(self.db.execute('SELECT count(*) FROM unified_memories').fetchone()[0], before)


    def test_write_report_creates_markdown_and_never_changes_rows(self):
        dead = raw_row(self.memory, r'资料在 D:\no-such-dir-zzz\plan.txt 里，api_key: sk-abcdefghijklmnop',
                       age_days=40)
        first = raw_row(self.memory, '主人喜欢周末去公园散步', project='生活',
                        keywords='["公园","周末"]')
        raw_row(self.memory, '主人喜欢周末去公园跑步', project='生活',
                keywords='["公园","周末"]')
        with tempfile.TemporaryDirectory() as folder:
            result = memory_guard.write_report(self.memory, out_dir=folder, db_path=':memory:')
            target = Path(result['path'])
            self.assertTrue(target.exists())
            report = target.read_text(encoding='utf-8')
            for heading in ('记忆体检报告', '一、概览', '二、疑似失效',
                            '三、疑似冲突', '四、冷宫候选', '附、关键词偏少'):
                self.assertIn(heading, report)
            self.assertNotIn('sk-abcdefghijklmnop', report)
            self.assertEqual(result['dead_paths'], 1)
            self.assertGreaterEqual(result['conflicts'], 1)
            self.assertGreaterEqual(result['cold'], 1)
        self.assertEqual(self.memory.get(dead)['status'], 'active')
        self.assertEqual(self.memory.get(first)['status'], 'active')


class CollectTests(unittest.TestCase):
    def test_only_decisions_survive(self):
        messages = [
            {'text': '以后别在群里发我的地址', 'ts': 1},
            {'text': '今天天气不错', 'ts': 2},
            {'text': '以后', 'ts': 3},
            {'text': '记住 password: hunter2hunter2', 'ts': 4},
            {'text': 'https://example.com/a', 'ts': 5},
            {'text': '。。。', 'ts': 6},
        ]
        result = memory_ingest.collect(messages)
        self.assertEqual([item['text'] for item in result['candidates']], ['以后别在群里发我的地址'])
        self.assertEqual(result['checked'], 6)
        self.assertEqual(result['skipped']['quiet'], 1)
        self.assertEqual(result['skipped']['secret'], 1)
        self.assertEqual(result['skipped']['noise'], 1)   # 纯标点太短，先被 short 挡掉
        self.assertEqual(result['skipped']['short'], 2)

    def test_duplicates_against_existing_are_skipped(self):
        result = memory_ingest.collect(
            [{'text': '以后统一用简体中文回复我', 'ts': 1}],
            existing=['以后统一用简体中文回复我'])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['skipped']['duplicate'], 1)

    def test_candidates_are_clipped_to_fact_limit(self):
        result = memory_ingest.collect([{'text': '记住 ' + '很长的规则' * 100, 'ts': 1}])
        self.assertEqual(len(result['candidates'][0]['text']), memory_ingest.MAX_FACT)
        self.assertTrue(result['candidates'][0]['keywords'])


class RunTests(AuditBase):
    def test_preview_writes_nothing(self):
        result = memory_ingest.run(self.memory, [{'text': '以后只发文字', 'ts': time.time()}])
        self.assertEqual(result['added'], 0)
        self.assertFalse(result['applied'])
        self.assertEqual(self.memory.count(), 0)

    def test_apply_writes_fact_layer_with_evidence(self):
        result = memory_ingest.run(self.memory, [{'text': '以后只发文字别发语音', 'ts': time.time()}],
                                   apply=True, persona='鲸鱼娘', channel='qq')
        self.assertEqual(result['added'], 1)
        items = self.memory.search('', 5)
        self.assertEqual(items[0]['layer'], 'fact')
        self.assertEqual(items[0]['source'], 'chat')
        self.assertTrue(items[0]['evidence'].startswith('主人私聊 '))

    def test_second_apply_does_not_duplicate(self):
        message = [{'text': '以后只发文字别发语音', 'ts': time.time()}]
        memory_ingest.run(self.memory, message, apply=True)
        again = memory_ingest.run(self.memory, message, apply=True)
        self.assertEqual(again['added'], 0)
        self.assertEqual(self.memory.count(), 1)


class FakeBot:
    def __init__(self, db, owners=('10001',)):
        self.db = db
        self.mode = 'onebot'
        self._owners = list(owners)

    def account(self):
        return '9999'

    def owner_ids(self):
        return list(self._owners)


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE inbox(key TEXT PRIMARY KEY,mode TEXT,account TEXT,room TEXT,'
                        'payload TEXT,state TEXT,reply TEXT,created REAL)')

    def tearDown(self):
        self.db.close()

    def add(self, room, sender, text, state='sent', created=None):
        self.db.execute('INSERT INTO inbox VALUES(?,?,?,?,?,?,?,?)',
                        (str(time.time()) + room + sender + text, 'onebot', '9999', room,
                         json.dumps({'sender_id': sender, 'text': text}, ensure_ascii=False),
                         state, '', time.time() if created is None else created))
        self.db.commit()

    def test_only_owner_private_messages_are_read(self):
        self.add('private:9999', '10001', '以后记得提醒我喝水')
        self.add('private:9999', '10002', '以后记得提醒我喝水')
        self.add('group:123', '10001', '以后记得提醒我喝水')
        self.add('private:9999', '10001', '以后记得提醒我吃饭', state='failed')
        messages = memory_ingest.inbox_messages(FakeBot(self.db))
        self.assertEqual([item['text'] for item in messages], ['以后记得提醒我喝水'])

    def test_without_owner_binding_nothing_is_read(self):
        self.add('private:9999', '10001', '以后记得提醒我喝水')
        self.assertEqual(memory_ingest.inbox_messages(FakeBot(self.db, owners=())), [])
