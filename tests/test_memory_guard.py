"""记忆守卫：写回体检、近似重复归档、140 天半衰期降权、注入分节预算。"""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src import memory_guard, prompt_budget
from src.unified_memory import UnifiedMemory

_COLUMNS = ('id,source,content,importance,created,last_used,uses,expires_at,tags,persona,'
            'layer,keywords,project,status,confidence,updated,evidence,goal,channel')
_DAY = 86400


def raw_row(memory, text, layer='fact', channel='qq', persona='', project='', importance=50,
            confidence=60, uses=0, now=None, idle_days=0.0, keywords='[]'):
    """直接写一行：remember 自带去重会挡掉"近似重复"，体检用例需要人为造脏数据。"""
    now = time.time() if now is None else float(now)
    stamp = now - idle_days * _DAY
    identifier = 'test-' + str(abs(hash((text, layer, persona, len(text)))))[:12]
    memory.db.execute('INSERT INTO unified_memories(' + _COLUMNS + ') VALUES('
                      + ','.join('?' * 19) + ')',
                      (identifier, 'qq', text, importance, stamp, stamp, uses, 0, '[]', persona,
                       layer, keywords, project, 'active', confidence, stamp, '', '', channel))
    memory.db.commit()
    return identifier


class MemoryGuardBase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        self.db.close()


class ScanTests(MemoryGuardBase):
    def test_scan_finds_long_secret_and_smell_rows(self):
        long_id = raw_row(self.memory, '很长的记忆' * 60)          # 300 字 > fact 上限 200
        secret_id = raw_row(self.memory, 'api_key: sk-abcdefghijklmnopqrst')
        raw_row(self.memory, '今天买了一台咖啡机', layer='fact')
        raw_row(self.memory, '然后就去吃饭了', layer='lesson')
        result = memory_guard.scan(self.memory)
        self.assertEqual(result['checked'], 4)
        kinds = {finding['kind'] for finding in result['findings']}
        self.assertEqual(kinds, {'too_long', 'secret', 'relative_time', 'diary_smell'})
        self.assertEqual(result['counts']['too_long'], 1)
        self.assertEqual(result['counts']['secret'], 1)
        self.assertEqual([f['id'] for f in result['findings'] if f['kind'] == 'too_long'], [long_id])
        self.assertIn(secret_id, [f['id'] for f in result['findings'] if f['kind'] == 'secret'])

    def test_scan_never_echoes_secret_value(self):
        raw_row(self.memory, 'token: abcdefghijklmnop')
        dumped = json.dumps(memory_guard.scan(self.memory)['findings'], ensure_ascii=False)
        self.assertNotIn('abcdefghijklmnop', dumped)
        self.assertIn('***', dumped)

    def test_scan_reports_near_duplicates_and_keeps_the_stronger(self):
        first = raw_row(self.memory, '主人喜欢在晚上喝无糖乌龙茶', importance=80, confidence=90)
        second = raw_row(self.memory, '主人喜欢在晚上喝无糖乌龙茶。', importance=40, confidence=50)
        findings = [f for f in memory_guard.scan(self.memory)['findings'] if f['kind'] == 'duplicate']
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['id'], second)
        self.assertNotEqual(findings[0]['id'], first)

    def test_exempt_layers_skip_smell_checks(self):
        raw_row(self.memory, '今天先聊到这里', layer='rules')
        raw_row(self.memory, '然后再说', layer='topic')
        kinds = [f['kind'] for f in memory_guard.scan(self.memory)['findings']]
        self.assertNotIn('relative_time', kinds)
        self.assertNotIn('diary_smell', kinds)


class ApplyTests(MemoryGuardBase):
    def test_apply_truncates_masks_and_archives_duplicate(self):
        long_id = raw_row(self.memory, '长' * 260)
        secret_id = raw_row(self.memory, '密码：hunter2hunter2')
        raw_row(self.memory, '主人喜欢在晚上喝无糖乌龙茶', importance=90, confidence=90)
        dup_id = raw_row(self.memory, '主人喜欢在晚上喝无糖乌龙茶。', importance=30, confidence=30)
        done = memory_guard.apply_findings(self.memory, memory_guard.scan(self.memory)['findings'])
        self.assertEqual(done, {'truncated': 1, 'masked': 1, 'archived_dup': 1})
        self.assertEqual(len(self.memory.get(long_id)['content']), memory_guard.LIMITS['fact'])
        self.assertIn('***', self.memory.get(secret_id)['content'])
        self.assertNotIn('hunter2', self.memory.get(secret_id)['content'])
        self.assertEqual(self.memory.get(dup_id)['status'], 'archived')
        self.assertEqual(self.memory.get(dup_id)['content'], '主人喜欢在晚上喝无糖乌龙茶。')  # 归档不删

    def test_relative_time_and_smell_are_not_rewritten(self):
        identifier = raw_row(self.memory, '今天他心情不好', layer='fact')
        memory_guard.apply_findings(self.memory, memory_guard.scan(self.memory)['findings'])
        self.assertEqual(self.memory.get(identifier)['content'], '今天他心情不好')


class DecayTests(MemoryGuardBase):
    def test_weight_halves_every_140_days(self):
        now = time.time()
        for idle, want in ((0, 1.0), (70, 0.7071), (140, 0.5), (280, 0.25)):
            item = {'last_used': now - idle * _DAY}
            self.assertAlmostEqual(memory_guard.weight(item, now), want, places=3)


    def test_decay_score_matches_reference_formula(self):
        now = time.time()
        score = memory_guard.decay_score(
            {'importance': 50, 'uses': 10, 'last_used': now - 140 * _DAY}, now)
        self.assertAlmostEqual(score, 0.75, places=3)

    def test_high_importance_rows_are_protected(self):
        now = time.time()
        raw_row(self.memory, '很重要的事', importance=80, idle_days=500, now=now)
        result = memory_guard.decay(self.memory, now=now, apply=False)
        self.assertEqual(result['candidates'], 0)

    def test_uses_lift_decay_score(self):
        now = time.time()
        raw_row(self.memory, '经常想起来的事', importance=40, uses=20,
                idle_days=400, now=now)
        result = memory_guard.decay(self.memory, now=now, apply=False)
        self.assertEqual(result['candidates'], 0)
        raw_row(self.memory, '从来没人提的事', importance=40, uses=0,
                idle_days=400, now=now)
        result = memory_guard.decay(self.memory, now=now, apply=False)
        self.assertEqual(result['candidates'], 1)

    def test_stale_threshold_needs_both_days_and_weight(self):
        now = time.time()
        fresh = raw_row(self.memory, '四十天前的事', idle_days=44, now=now)
        stale = raw_row(self.memory, '四十六天前的事', idle_days=46, now=now)
        result = memory_guard.decay(self.memory, now=now, apply=False)
        ids = [item['id'] for item in result['items']]
        self.assertIn(stale, ids)
        self.assertNotIn(fresh, ids)
        self.assertEqual(result['archived'], 0)              # 没 apply 不动数据
        self.assertEqual(self.memory.get(stale)['status'], 'active')

    def test_exempt_layers_never_decay(self):
        now = time.time()
        for layer in ('rules', 'soul', 'user', 'project'):
            raw_row(self.memory, '很久以前的' + layer, layer=layer, idle_days=400, now=now)
        result = memory_guard.decay(self.memory, now=now, apply=True)
        self.assertEqual(result['candidates'], 0)
        self.assertEqual(result['archived'], 0)

    def test_apply_archives_without_deleting(self):
        now = time.time()
        identifier = raw_row(self.memory, '两百天前的琐事', layer='topic', idle_days=200, now=now)
        result = memory_guard.decay(self.memory, now=now, apply=True)
        self.assertEqual(result['archived'], 1)
        self.assertEqual(self.memory.get(identifier)['status'], 'archived')
        self.assertEqual(self.memory.count(), 1)


class MaintainTests(MemoryGuardBase):
    def test_report_is_read_only(self):
        raw_row(self.memory, '长' * 260)
        before = self.memory.get(memory_guard.scan(self.memory)['findings'][0]['id'])['content']
        report = memory_guard.report(self.memory)
        self.assertEqual(report['checked'], 1)
        self.assertIn('fact', report['limits'])
        self.assertEqual(report['counts']['too_long'], 1)
        after = self.memory.get(memory_guard.scan(self.memory)['findings'][0]['id'])['content']
        self.assertEqual(before, after)

    def test_maintain_dry_run_changes_nothing(self):
        raw_row(self.memory, '长' * 260)
        result = memory_guard.maintain(self.memory, apply=False)
        self.assertFalse(result['applied'])
        self.assertEqual(result['truncated'], 0)
        self.assertEqual(result['counts']['too_long'], 1)
        # 一键整理也顺手带上三项审计（只报告）
        for key in ('dead_paths', 'conflicts', 'cold'):
            self.assertIn(key, result)

    def test_maintain_applies_and_reports_remaining(self):
        raw_row(self.memory, '长' * 260)
        result = memory_guard.maintain(self.memory, apply=True)
        self.assertTrue(result['applied'])
        self.assertEqual(result['truncated'], 1)
        self.assertEqual(result['remaining'], 1)


class WeakKeywordTests(MemoryGuardBase):
    def test_flags_only_long_rows_with_few_keywords(self):
        long_text = '主人喜欢在深夜整理当天所有聊天记录和记忆条目的习惯已经持续很久了'
        weak = raw_row(self.memory, long_text)
        raw_row(self.memory, '短句')                                   # 不够长
        raw_row(self.memory, long_text + '二',
                keywords=json.dumps(['主人', '深夜', '记录'], ensure_ascii=False))
        found = memory_guard.find_weak_keywords(memory_guard._active(self.memory))
        self.assertEqual([item['id'] for item in found], [weak])
        self.assertEqual(found[0]['keywords'], 0)
        self.assertIn('检索会打偏', found[0]['detail'])

    def test_weak_keywords_never_echo_secret(self):
        raw_row(self.memory, 'token: abcdefghijklmnop 这条很长很长很长很长很长很长')
        found = memory_guard.find_weak_keywords(memory_guard._active(self.memory))
        dumped = json.dumps(found, ensure_ascii=False)
        self.assertNotIn('abcdefghijklmnop', dumped)
        self.assertIn('***', dumped)

    def test_report_and_maintain_include_weak_keywords(self):
        raw_row(self.memory, '主人喜欢在深夜整理当天所有聊天记录和记忆条目的习惯已经持续很久了')
        report = memory_guard.report(self.memory)
        self.assertEqual(len(report['weak_keywords']), 1)
        result = memory_guard.maintain(self.memory, apply=False)
        self.assertEqual(len(result['weak_keywords']), 1)

    def test_maintain_fixes_weak_keywords_without_touching_content(self):
        text = '主人喜欢在深夜整理当天所有聊天记录和记忆条目的习惯已经持续很久了'
        identifier = raw_row(self.memory, text)
        before = self.memory.get(identifier)['updated']
        result = memory_guard.maintain(self.memory, apply=True)
        self.assertEqual(result['fixed_keywords'], 1)
        self.assertEqual(len(result['weak_keywords']), 0)
        row = self.memory.get(identifier)
        self.assertEqual(row['content'], text)
        self.assertEqual(row['updated'], before)
        self.assertGreaterEqual(len(row['keywords']), 3)

    def test_daily_writes_report_and_log_without_secret(self):
        raw_row(self.memory, 'api_key: sk-abcdefghijklmnopqrst 这条记忆特别长特别长特别长特别长特别长特别长')
        with tempfile.TemporaryDirectory() as folder:
            result = memory_guard.daily(self.memory, out_dir=folder, apply=True)
            self.assertTrue(Path(result['report']).is_file())
            self.assertTrue(Path(result['log']).is_file())
            log = Path(result['log']).read_text(encoding='utf-8')
            self.assertIn('补关键词 1', log)
            self.assertIn('关键词偏少 0', log)
            self.assertIn('每日日志', log)
            self.assertNotIn('abcdefghijklmnop', log)
            report = Path(result['report']).read_text(encoding='utf-8')
            self.assertIn('附、关键词偏少', report)
            self.assertNotIn('abcdefghijklmnop', report)


class BackupTests(unittest.TestCase):
    """整理前先备份：库开着 WAL 时也要拿到完整快照，且只保留最近几份。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.db_path = self.folder / 'messages.sqlite3'
        self.db = sqlite3.connect(str(self.db_path))
        self.memory = UnifiedMemory(self.db)
        raw_row(self.memory, '主人喜欢在周末整理记忆库')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_maintain_backs_up_before_apply(self):
        out = self.folder / '记忆' / '备份'
        result = memory_guard.maintain(self.memory, apply=True, backup_dir=out,
                                       db_path=str(self.db_path))
        backup = result['backup']
        self.assertTrue(backup['path'])
        self.assertGreater(backup['bytes'], 0)
        self.assertTrue(Path(backup['path']).is_file())
        check = sqlite3.connect(backup['path'])
        try:
            self.assertEqual(check.execute('SELECT COUNT(*) FROM unified_memories').fetchone()[0], 1)
        finally:
            check.close()

    def test_daily_logs_backup_name(self):
        out = self.folder / '记忆'
        result = memory_guard.daily(self.memory, out_dir=out, db_path=str(self.db_path), apply=True)
        self.assertTrue(result['backup']['name'])
        log = Path(result['log']).read_text(encoding='utf-8')
        self.assertIn('备份：', log)
        self.assertIn(result['backup']['name'], log)

    def test_backup_keeps_only_requested_number(self):
        out = self.folder / '备份'
        for index in range(3):
            memory_guard.backup(self.memory, out, db_path=str(self.db_path),
                                keep=2, now=1700000000 + index)
        self.assertEqual(len(list(out.glob('memory-*.db'))), 2)

    def test_maintain_without_db_path_skips_backup(self):
        out = self.folder / '备份'
        result = memory_guard.maintain(self.memory, apply=True, backup_dir=out, db_path='')
        self.assertEqual(result['backup']['skipped'], 'no_db_path')
        self.assertFalse(out.exists())

    def test_list_backups_reads_existing_files_only(self):
        out = self.folder / '备份'
        first = memory_guard.backup(self.memory, out, db_path=str(self.db_path), now=1700000000)
        second = memory_guard.backup(self.memory, out, db_path=str(self.db_path), now=1700000001)
        (out / 'notes.txt').write_text('不是备份', encoding='utf-8')
        items = memory_guard.list_backups(out, limit=10)
        self.assertEqual({item['name'] for item in items}, {first['name'], second['name']})
        self.assertTrue(all(item['bytes'] > 0 for item in items))
        self.assertEqual(memory_guard.list_backups(self.folder / '不存在'), [])


class PromptBudgetTests(unittest.TestCase):
    def test_clip_and_compose_cap_each_block(self):
        text, stats = prompt_budget.compose([('记忆', '记' * 2000, 1200, 2)], limit=6000)
        self.assertEqual(len(text), 1200)
        self.assertTrue(stats['blocks'][0]['truncated'])
        self.assertEqual(stats['total'], 1200)

    def test_over_budget_drops_lowest_priority_first(self):
        blocks = [('身份', 'A' * 100, 0, 0), ('记忆', 'B' * 300, 0, 2), ('日记', 'C' * 300, 0, 3)]
        text, stats = prompt_budget.compose(blocks, limit=450)
        self.assertIn('A' * 100, text)
        dropped = [b['name'] for b in stats['blocks'] if b['dropped']]
        self.assertEqual(dropped, ['日记'])
        self.assertTrue(text.endswith('B' * 300))

    def test_summary_calls_out_truncation_and_drops(self):
        _text, stats = prompt_budget.compose(
            [('记忆', 'x' * 1300, 1200, 2), ('日记', 'y' * 900, 900, 3), ('贴纸', 'z', 10, 9)],
            limit=1500)
        line = prompt_budget.summary(stats)
        self.assertIn('提示构成', line)
        self.assertIn('超长已截断', line)
        self.assertIn('已丢', line)
        self.assertIn('上限 1500', line)

    def test_empty_blocks_are_skipped(self):
        text, stats = prompt_budget.compose([('记忆', '', 1200, 2), ('日记', '   ', 900, 3)], limit=100)
        self.assertEqual(text, '')
        self.assertEqual(stats['blocks'], [])


class ContextBudgetTests(MemoryGuardBase):
    def test_stable_section_is_capped(self):
        for index in range(3):
            self.memory.remember('稳定层内容' * 30 + str(index), source='desktop',
                                 layer='soul', importance=90)
        text = self.memory.context_for_prompt('你好', private=True, first_turn=True)
        stable_block = text.split('【记忆导引】')[0]
        self.assertLessEqual(len(stable_block), 600 + 40)     # 600 预算 + 标题/标记

    def test_recent_memory_outranks_old_one(self):
        now = time.time()
        old = raw_row(self.memory, '咖啡机维修记录', idle_days=150, now=now)
        new = raw_row(self.memory, '咖啡机维修记录二', idle_days=0, now=now)
        found = [item['id'] for item in self.memory.search('咖啡机维修记录', limit=2)]
        self.assertEqual(found[0], new)
        self.assertIn(old, found)


if __name__ == '__main__':
    unittest.main()
