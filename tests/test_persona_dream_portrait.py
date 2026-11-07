"""按人设隔离：agent_portrait / agent_dream 的 persona 分流与默认人设向后兼容。

默认人设 persona='' 的路径、文件名、写入字节必须与改动前一致；非默认人设进
data_dir/personas/<slug>/，文件与 soul 记忆都互不可见。
"""
import datetime
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import agent_dream
from src import agent_portrait as ap
from src.unified_memory import UnifiedMemory

NOW = datetime.datetime(2026, 10, 26, 22, 30)
STAMP = NOW.timestamp()
BODY_A = '我是甲人设的一份自述。' * 12
BODY_B = '我是乙人设的一份自述。' * 12
DAY = '2026-10-25'


def write_diary(folder, blocks):
    lines = ['# 鲸鱼娘日记', '', '> 自动整理，中文可读。', '']
    for day, text in blocks:
        lines.extend(['## ' + day, '', text, ''])
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / '日记.md').write_text('\n'.join(lines), encoding='utf-8')


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        try:
            self.db.close()
        finally:
            self._tmp.cleanup()

    def soul_rows(self, persona=''):
        # strict_scope 只在 persona 非空时硬过滤，所以默认人设这里再按 persona 列收一次。
        rows = self.memory.search('', layer='soul', limit=50, persona=persona,
                                  strict_scope=True)
        slug = ap.persona_slug(persona)
        return [row for row in rows if str(row.get('persona') or '') == slug]

    def files_under(self):
        return sorted(str(path.relative_to(self.data)).replace(os.sep, '/')
                      for path in self.data.rglob('*') if path.is_file())

    @staticmethod
    def enabled(**extra):
        section = {'dream_enabled': True}
        section.update(extra)
        return {'ai': section}


class PersonaSlugTests(_Case):
    def test_empty_persona_is_default_and_maps_to_data_dir(self):
        for blank in ('', None, '   ', '\t\n'):
            self.assertEqual(ap.persona_slug(blank), '')
            self.assertEqual(ap.persona_dir(self.data, blank), self.data)
        self.assertEqual(ap.persona_dir(self.data), self.data)

    def test_slug_strips_separators_controls_and_caps_length(self):
        for raw in ('../evil', 'a/b', '..\\evil', '/abs', 'C:\\Windows', 'a\x00b\nc'):
            slug = ap.persona_slug(raw)
            self.assertNotIn('/', slug, raw)
            self.assertNotIn('\\', slug, raw)
            self.assertFalse(slug.startswith('.'), raw)
            self.assertFalse(any(ord(char) < 32 or ord(char) == 127 for char in slug), raw)
        self.assertEqual(ap.persona_slug('../evil'), '-evil')
        self.assertEqual(ap.persona_slug('a/b'), 'a-b')
        self.assertEqual(len(ap.persona_slug('长' * 200)), ap.PERSONA_MAX)
        self.assertEqual(ap.PERSONA_MAX, 64)
        self.assertEqual(ap.persona_slug('CON'), '_CON')

    def test_persona_dir_never_escapes_data_dir(self):
        root = self.data.resolve()
        for raw in ('../evil', 'a/b', '..\\evil', '../../etc', '/abs', 'C:\\Windows'):
            target = ap.persona_dir(self.data, raw).resolve()
            target.relative_to(root)  # 抛 ValueError 即为逃逸
            self.assertNotEqual(target, root)


class DefaultPersonaCompatTests(_Case):
    def test_default_paths_match_legacy(self):
        self.assertEqual(ap.portrait_path(self.data), self.data / '我是谁.md')
        self.assertEqual(ap.portrait_path(self.data, ''), self.data / '我是谁.md')
        self.assertEqual(ap.history_dir(self.data), self.data / '我是谁-history')
        self.assertEqual(agent_dream.ledger_path(self.data),
                         self.data / 'qq-only' / 'dream-seen.json')
        self.assertEqual(agent_dream.ledger_path('/tmp/demo'),
                         Path('/tmp/demo') / 'qq-only' / 'dream-seen.json')
        self.assertEqual(ap.read_portrait(self.data), '')

    def test_default_write_is_byte_identical(self):
        text = '第一段。\r\n\r\n\r\n\r\n第二段。  \r\n'
        result = ap.write_portrait(self.memory, self.data, text, now=NOW)
        expected = '第一段。\n\n第二段。'
        self.assertEqual(result['path'], str(self.data / '我是谁.md'))
        self.assertEqual(result['chars'], len(expected))
        self.assertFalse(result['truncated'])
        self.assertEqual(result['snapshot'], '')
        raw = (self.data / '我是谁.md').read_bytes()
        self.assertEqual(raw, expected.encode('utf-8'))
        self.assertNotIn(b'\xef\xbb\xbf', raw)
        self.assertNotIn(b'\r', raw)
        self.assertEqual(self.files_under(), ['我是谁.md'])

    def test_default_snapshot_uses_legacy_history_dir(self):
        first = ap.write_portrait(self.memory, self.data, BODY_A, now=NOW)
        second = ap.write_portrait(self.memory, self.data, BODY_B, now=NOW)
        folder = self.data / '我是谁-history'
        self.assertEqual(first['memory_id'], second['memory_id'])
        self.assertFalse(second['created'])
        self.assertEqual(second['snapshot'], str(folder / '20261026-223000.md'))
        self.assertEqual((folder / '20261026-223000.md').read_text(encoding='utf-8'), BODY_A)
        third = ap.write_portrait(self.memory, self.data, BODY_A, now=NOW)
        self.assertEqual(third['snapshot'], str(folder / '20261026-223000-2.md'))
        self.assertEqual(self.files_under(),
                         ['我是谁-history/20261026-223000-2.md',
                          '我是谁-history/20261026-223000.md',
                          '我是谁.md'])

    def test_default_memory_row_has_empty_persona(self):
        ap.write_portrait(self.memory, self.data, BODY_A, now=NOW)
        rows = self.soul_rows('')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['persona'], '')
        self.assertEqual(rows[0]['content'], BODY_A)


class PortraitPersonaIsolationTests(_Case):
    def test_portraits_are_isolated_between_personas(self):
        result_a = ap.write_portrait(self.memory, self.data, BODY_A, now=NOW, persona='甲')
        result_b = ap.write_portrait(self.memory, self.data, BODY_B, now=NOW, persona='乙')
        self.assertEqual(result_a['path'], str(self.data / 'personas' / '甲' / '我是谁.md'))
        self.assertEqual(result_b['path'], str(self.data / 'personas' / '乙' / '我是谁.md'))
        self.assertEqual(ap.read_portrait(self.data, '甲'), BODY_A)
        self.assertEqual(ap.read_portrait(self.data, '乙'), BODY_B)
        self.assertEqual(ap.read_portrait(self.data), '')
        self.assertNotEqual(result_a['memory_id'], result_b['memory_id'])
        self.assertEqual(sorted(row['persona'] for row in self.soul_rows('甲')), ['甲'])
        self.assertEqual(sorted(row['persona'] for row in self.soul_rows('乙')), ['乙'])

    def test_persona_write_does_not_touch_default_file(self):
        ap.write_portrait(self.memory, self.data, BODY_A, now=NOW, persona='甲')
        self.assertFalse((self.data / '我是谁.md').exists())
        self.assertFalse((self.data / '我是谁-history').exists())
        self.assertEqual(self.files_under(), ['personas/甲/我是谁.md'])
        self.assertEqual(len(self.soul_rows('')), 0)

    def test_evil_persona_stays_inside_data_dir(self):
        result = ap.write_portrait(self.memory, self.data, BODY_A, now=NOW, persona='../evil')
        target = Path(result['path']).resolve()
        target.relative_to(self.data.resolve())
        self.assertEqual(target.name, '我是谁.md')
        self.assertEqual(self.files_under(), ['personas/-evil/我是谁.md'])
        self.assertFalse((self.data.parent / 'evil').exists())
        self.assertEqual(ap.read_portrait(self.data, '../evil'), BODY_A)
        self.assertEqual(ap.read_portrait(self.data), '')

    def test_soul_digest_filters_by_persona(self):
        self.memory.remember('旧数据里的一句 soul', layer='soul', persona='')
        self.memory.remember('甲人设的 soul', layer='soul', persona='甲')
        self.memory.remember('乙人设的 soul', layer='soul', persona='乙')
        default = ap._soul_digest(self.memory)
        self.assertIn('旧数据里的一句 soul', default)
        self.assertIn('甲人设的 soul', default)
        self.assertIn('乙人设的 soul', default)
        only_a = ap._soul_digest(self.memory, '甲')
        self.assertIn('甲人设的 soul', only_a)
        self.assertNotIn('乙人设的 soul', only_a)
        self.assertNotIn('旧数据里的一句 soul', only_a)
        only_b = ap._soul_digest(self.memory, '乙')
        self.assertIn('乙人设的 soul', only_b)
        self.assertNotIn('甲人设的 soul', only_b)

    def test_build_prompt_reads_only_own_persona(self):
        ap.write_portrait(self.memory, self.data, BODY_A, now=NOW, persona='甲')
        prompt_a = ap.build_prompt(self.memory, self.data, persona='甲')
        prompt_b = ap.build_prompt(self.memory, self.data, persona='乙')
        prompt_default = ap.build_prompt(self.memory, self.data)
        self.assertIn(BODY_A[:40], prompt_a)
        self.assertNotIn(BODY_A[:40], prompt_b)
        self.assertNotIn(BODY_A[:40], prompt_default)

    def test_build_prompt_diary_isolated_by_persona(self):
        # 日记表没有 persona 列，所以默认人设仍吃 memory.diary_tail 的旧口径，
        # 非默认人设只读自己目录下的 日记.md，读不到全局那一份。
        global_diary = self.data / '日记.md'
        write_diary(self.data, [(DAY, '全局日记里的一段')])
        write_diary(self.data / 'personas' / '甲', [(DAY, '甲人设日记里的一段')])
        memory = UnifiedMemory(self.db, global_diary)
        legacy = ap.build_prompt(memory, self.data)
        self.assertIn('全局日记里的一段', legacy)
        self.assertNotIn('甲人设日记里的一段', legacy)
        only_a = ap.build_prompt(memory, self.data, persona='甲')
        self.assertIn('甲人设日记里的一段', only_a)
        self.assertNotIn('全局日记里的一段', only_a)
        only_b = ap.build_prompt(memory, self.data, persona='乙')
        self.assertNotIn('全局日记里的一段', only_b)
        self.assertNotIn('甲人设日记里的一段', only_b)


class DreamPersonaTests(_Case):
    def test_ledger_path_by_persona(self):
        self.assertEqual(agent_dream.ledger_path(self.data, '甲'),
                         self.data / 'personas' / '甲' / 'qq-only' / 'dream-seen.json')
        evil = agent_dream.ledger_path(self.data, '../evil')
        evil.resolve().relative_to(self.data.resolve())
        self.assertEqual(evil, self.data / 'personas' / '-evil' / 'qq-only' / 'dream-seen.json')

    def test_collect_reads_only_own_diary(self):
        write_diary(self.data, [(DAY, '全局日记里的一句素材')])
        write_diary(self.data / 'personas' / '甲', [(DAY, '甲人设自己的日记素材')])
        own = agent_dream.collect(self.memory, self.data, now=STAMP, persona='甲')
        self.assertEqual([item['text'] for item in own], ['甲人设自己的日记素材'])
        legacy = agent_dream.collect(self.memory, self.data, now=STAMP)
        self.assertEqual([item['text'] for item in legacy], ['全局日记里的一句素材'])

    def test_consolidate_writes_and_reads_isolated_memory(self):
        write_diary(self.data / 'personas' / '甲', [(DAY, '甲人设做过的一件小事')])
        write_diary(self.data / 'personas' / '乙', [(DAY, '甲人设做过的一件小事')])
        first = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                        now=STAMP, force=True, persona='甲')
        self.assertTrue(first['ok'], first)
        self.assertEqual(first['written'], 1)
        second = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=STAMP, force=True, persona='乙')
        self.assertTrue(second['ok'], second)
        self.assertEqual(second['written'], 1)
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertEqual(sorted(row['persona'] for row in self.soul_rows('甲')), ['甲'])
        self.assertEqual(sorted(row['persona'] for row in self.soul_rows('乙')), ['乙'])
        self.assertEqual(len(self.soul_rows('')), 0)

    def test_consolidate_ledgers_are_separate(self):
        write_diary(self.data, [(DAY, '全局账本的一条素材')])
        write_diary(self.data / 'personas' / '甲', [(DAY, '甲人设账本的一条素材')])
        agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                now=STAMP, force=True)
        agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                now=STAMP, force=True, persona='甲')
        legacy_keys = json.loads((self.data / 'qq-only' / 'dream-seen.json')
                                 .read_text(encoding='utf-8'))['keys']
        own_keys = json.loads((self.data / 'personas' / '甲' / 'qq-only' / 'dream-seen.json')
                              .read_text(encoding='utf-8'))['keys']
        self.assertEqual(len(legacy_keys), 1)
        self.assertEqual(len(own_keys), 1)
        self.assertNotEqual(legacy_keys, own_keys)

    def test_default_persona_still_reads_legacy_memory(self):
        self.memory.remember('旧版本留下的记忆素材', layer='fact', persona='')
        result = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=STAMP, force=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['written'], 1)
        rows = self.soul_rows('')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['persona'], '')
        self.assertEqual(rows[0]['content'], '旧版本留下的记忆素材')

    def test_named_persona_does_not_read_legacy_memory(self):
        self.memory.remember('旧版本留下的记忆素材', layer='fact', persona='')
        result = agent_dream.consolidate(self.memory, self.data, config=self.enabled(),
                                         now=STAMP, force=True, persona='甲')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'no_items')
        self.assertEqual(len(self.soul_rows('')), 0)


class SignatureCompatTests(unittest.TestCase):
    def test_persona_is_trailing_keyword_with_empty_default(self):
        import inspect
        for func in (ap.build_prompt, ap.write_portrait, ap.refresh,
                     agent_dream.consolidate):
            params = list(inspect.signature(func).parameters.values())
            self.assertEqual(params[-1].name, 'persona', func.__name__)
            self.assertEqual(params[-1].default, '', func.__name__)
            self.assertEqual(params[-1].kind, inspect.Parameter.KEYWORD_ONLY,
                             func.__name__)
        for func in (ap.portrait_path, ap.history_dir, ap.read_portrait,
                     agent_dream.ledger_path):
            params = list(inspect.signature(func).parameters.values())
            self.assertEqual(params[-1].name, 'persona', func.__name__)
            self.assertEqual(params[-1].default, '', func.__name__)


if __name__ == '__main__':
    unittest.main()
