"""agent_portrait 的离线测试：不联网、不 sleep，data_dir 与 db 全部临时。"""
import datetime
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import agent_portrait as ap
from src.unified_memory import UnifiedMemory

FENCE = chr(96) * 3
NOW = datetime.datetime(2026, 3, 4, 5, 6, 7)


class NestedConfig:
    """模拟 config_manager.get() 的点号取值。"""

    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        node = self.data
        for part in str(key).split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class PortraitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.db = sqlite3.connect(':memory:')
        self.memory = UnifiedMemory(self.db)

    def tearDown(self):
        try:
            self.db.close()
        finally:
            self._tmp.cleanup()

    # ---------- settings ----------

    def test_settings_missing_key_is_off(self):
        self.assertEqual(ap.settings(NestedConfig({})), {'enabled': False})
        self.assertEqual(ap.settings(None), {'enabled': False})
        self.assertEqual(ap.DEFAULTS, {'enabled': False})

    def test_settings_enabled_true(self):
        self.assertTrue(ap.settings(NestedConfig({'ai': {'portrait_enabled': True}}))['enabled'])

    def test_settings_invalid_value_falls_back_to_false(self):
        for bad in ('maybe', '', ' ', 0, None, [], 'off'):
            self.assertFalse(ap.settings(NestedConfig({'ai': {'portrait_enabled': bad}}))['enabled'],
                             f'{bad!r} 应回落 False')

    def test_settings_accepts_plain_nested_dict(self):
        # yaml.safe_load 出来的就是这种普通嵌套 dict，不能只认扁平点号键。
        self.assertTrue(ap.settings({'ai': {'portrait_enabled': True}})['enabled'])

    def test_settings_nested_dict_dirty_value_stays_off(self):
        self.assertFalse(ap.settings({'ai': {'portrait_enabled': 'off'}})['enabled'])
        self.assertFalse(ap.settings({'ai': {'portrait_enabled': 'maybe'}})['enabled'])

    def test_settings_accepts_dotted_key_object(self):
        class _C:
            def get(self, key, default=None):
                return True if key == 'ai.portrait_enabled' else default

        self.assertTrue(ap.settings(_C())['enabled'])

    def test_settings_accepts_config_attribute_dict(self):
        class _C:
            config = {'ai': {'portrait_enabled': True}}

            def get(self, key, default=None):
                return default

        self.assertTrue(ap.settings(_C())['enabled'])

    # ---------- 路径 / 读 ----------

    def test_paths(self):
        self.assertEqual(ap.portrait_path(self.data_dir), self.data_dir / '我是谁.md')
        self.assertEqual(ap.history_dir(self.data_dir), self.data_dir / '我是谁-history')

    def test_read_portrait_missing_returns_empty(self):
        self.assertEqual(ap.read_portrait(self.data_dir), '')
        self.assertFalse((self.data_dir / '我是谁.md').exists())

    def test_read_portrait_strips_bom(self):
        (self.data_dir / '我是谁.md').write_bytes(b'\xef\xbb\xbf' + '我是小鲸鱼。'.encode('utf-8'))
        self.assertEqual(ap.read_portrait(self.data_dir), '我是小鲸鱼。')

    # ---------- build_prompt ----------

    def test_build_prompt_uses_previous_and_soul_but_skips_own_portrait(self):
        (self.data_dir / '我是谁.md').write_text('上一版：我是小鲸鱼。', encoding='utf-8')
        self.memory.remember('我说话简短，喜欢安静。', source='qq', layer='soul',
                             importance=80, confidence=80, tags=['style'])
        self.memory.remember('自画像正文不该当素材喂回去。', source='portrait', layer='soul',
                             importance=95, confidence=95, tags=['portrait'])
        prompt = ap.build_prompt(self.memory, self.data_dir)
        self.assertIn('上一版：我是小鲸鱼。', prompt)
        self.assertIn('我说话简短，喜欢安静。', prompt)
        self.assertNotIn('自画像正文不该当素材喂回去。', prompt)
        self.assertIn('第一人称', prompt)
        self.assertIn('只输出正文', prompt)
        self.assertTrue(prompt.endswith(ap.INSTRUCTION))

    def test_build_prompt_without_material_keeps_instruction(self):
        prompt = ap.build_prompt(self.memory, self.data_dir)
        self.assertEqual(prompt, ap.INSTRUCTION)

    def test_build_prompt_includes_diary_tail(self):
        diary = self.data_dir / 'diary.md'
        diary.write_text('## 2026-03-03\n今天和小鲸鱼聊了很久。', encoding='utf-8')
        memory = UnifiedMemory(self.db, diary_path=diary)
        prompt = ap.build_prompt(memory, self.data_dir)
        self.assertIn('今天和小鲸鱼聊了很久。', prompt)

    def test_build_prompt_respects_max_chars(self):
        (self.data_dir / '我是谁.md').write_text('旧' * 5000, encoding='utf-8')
        for _ in range(6):
            self.memory.remember('素材' * 500, source='qq', layer='soul',
                                 importance=70, confidence=70, tags=['bulk'])
        prompt = ap.build_prompt(self.memory, self.data_dir, max_chars=1200)
        self.assertLessEqual(len(prompt), 1200)
        self.assertTrue(prompt.endswith(ap.INSTRUCTION))
        # 预算真裁了素材：旧版自述被截短，soul 段落整段让位
        self.assertGreater(len(prompt), 800)
        self.assertNotIn('素材素材素材素材素材素材素材素材素材', prompt)

    # ---------- write_portrait ----------

    def test_first_write_has_no_snapshot(self):
        result = ap.write_portrait(self.memory, self.data_dir, '第一版自述正文。', now=NOW)
        self.assertTrue(result['ok'])
        self.assertEqual(result['snapshot'], '')
        self.assertFalse(ap.history_dir(self.data_dir).exists())
        self.assertEqual(ap.portrait_path(self.data_dir).read_text(encoding='utf-8'), '第一版自述正文。')
        self.assertEqual(result['chars'], len('第一版自述正文。'))
        self.assertFalse(result['truncated'])
        self.assertEqual(result['path'], str(ap.portrait_path(self.data_dir)))

    def test_second_write_snapshots_previous_body(self):
        first = ap.write_portrait(self.memory, self.data_dir, '第一版自述正文。', now=NOW)
        second = ap.write_portrait(self.memory, self.data_dir, '第二版自述正文。', now=NOW)
        self.assertEqual(first['snapshot'], '')
        snapshot = Path(second['snapshot'])
        self.assertEqual(snapshot.parent, ap.history_dir(self.data_dir))
        self.assertEqual(snapshot.name, '20260304-050607.md')
        self.assertEqual(snapshot.read_text(encoding='utf-8'), '第一版自述正文。')
        self.assertEqual(ap.read_portrait(self.data_dir), '第二版自述正文。')

    def test_same_second_snapshots_do_not_overwrite(self):
        ap.write_portrait(self.memory, self.data_dir, '第一版自述正文。', now=NOW)
        ap.write_portrait(self.memory, self.data_dir, '第二版自述正文。', now=NOW)
        third = ap.write_portrait(self.memory, self.data_dir, '第三版自述正文。', now=NOW)
        self.assertEqual(Path(third['snapshot']).name, '20260304-050607-2.md')
        self.assertEqual(Path(third['snapshot']).read_text(encoding='utf-8'), '第二版自述正文。')
        names = sorted(item.name for item in ap.history_dir(self.data_dir).iterdir())
        self.assertEqual(names, ['20260304-050607-2.md', '20260304-050607.md'])

    def test_file_is_utf8_without_bom_and_lf_only(self):
        ap.write_portrait(self.memory, self.data_dir, '第一行。\r\n\r\n\r\n第二行。', now=NOW)
        raw = ap.portrait_path(self.data_dir).read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\r\n', raw)
        self.assertEqual(raw.decode('utf-8'), '第一行。\n\n第二行。')

    def test_long_body_is_truncated(self):
        result = ap.write_portrait(self.memory, self.data_dir, '长' * 5000, now=NOW)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['chars'], ap.MAX_BODY)
        self.assertEqual(len(ap.read_portrait(self.data_dir)), ap.MAX_BODY)
        self.assertFalse([p for p in self.data_dir.iterdir() if p.name.startswith('.tmp')])

    def test_memory_upsert_keeps_single_soul_entry(self):
        first = ap.write_portrait(self.memory, self.data_dir, '第一版自述正文，写得很认真。', now=NOW)
        self.assertTrue(first['created'])
        second = ap.write_portrait(self.memory, self.data_dir, '第二版自述正文，改了几处措辞。', now=NOW)
        self.assertFalse(second['created'])
        self.assertEqual(second['memory_id'], first['memory_id'])
        rows = [item for item in self.memory.search('', layer='soul', limit=30)
                if 'portrait' in (item.get('tags') or [])]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['content'], '第二版自述正文，改了几处措辞。')
        self.assertEqual(rows[0]['importance'], 95)
        self.assertEqual(rows[0]['layer'], 'soul')
        self.assertRegex(first['memory_id'], r'^[0-9a-f]{32}$')  # UnifiedMemory 的 id 是 uuid4().hex

    # ---------- 清洗 ----------

    def test_clean_text_drops_fence_and_title(self):
        raw = FENCE + 'markdown\n# 我是谁\n\n第一段。\n\n\n第二段。\n' + FENCE
        self.assertEqual(ap.clean_text(raw), '第一段。\n\n第二段。')
        self.assertEqual(ap.clean_text('我是谁：我是小鲸鱼。\n\n第二段。'), '我是小鲸鱼。\n\n第二段。')
        self.assertEqual(ap.clean_text('我是谁:\n\n正文一。'), '正文一。')
        self.assertEqual(ap.clean_text('  正文，没有标题。  \n'), '正文，没有标题。')

    # ---------- refresh ----------

    def test_refresh_disabled_never_calls_generate(self):
        calls = []

        def generate(prompt):
            calls.append(prompt)
            return '不该被调用'

        self.assertEqual(ap.refresh(self.memory, self.data_dir, generate=generate),
                         {'ok': False, 'reason': 'disabled'})
        self.assertEqual(ap.refresh(self.memory, self.data_dir, generate=generate,
                                    config=NestedConfig({'ai': {'portrait_enabled': False}})),
                         {'ok': False, 'reason': 'disabled'})
        self.assertEqual(calls, [])
        self.assertFalse((self.data_dir / '我是谁.md').exists())

    def test_refresh_too_short_writes_nothing(self):
        result = ap.refresh(self.memory, self.data_dir, generate=lambda prompt: '我是小鲸鱼。',
                            config=NestedConfig({'ai': {'portrait_enabled': True}}), now=NOW)
        self.assertEqual(result, {'ok': False, 'reason': 'too_short'})
        self.assertFalse((self.data_dir / '我是谁.md').exists())
        self.assertEqual(self.memory.search('', layer='soul', limit=30), [])

    def test_refresh_generate_failed(self):
        def generate(prompt):
            raise RuntimeError('模型超时')

        result = ap.refresh(self.memory, self.data_dir, generate=generate,
                            config=NestedConfig({'ai': {'portrait_enabled': True}}), now=NOW)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'generate_failed')
        self.assertIn('模型超时', result['detail'])

    def test_refresh_success_writes_file_and_memory(self):
        seen = {}

        def generate(prompt):
            seen['prompt'] = prompt
            return FENCE + '\n我是谁：\n' + '我是一个在小群里陪人聊天的机器人。' * 6 + '\n' + FENCE

        result = ap.refresh(self.memory, self.data_dir, generate=generate, now=NOW,
                            config=NestedConfig({'ai': {'portrait_enabled': True}}))
        self.assertTrue(result['ok'])
        self.assertEqual(result['snapshot'], '')
        self.assertIn('只输出正文', seen['prompt'])
        body = ap.read_portrait(self.data_dir)
        self.assertNotIn(FENCE, body)
        self.assertFalse(body.startswith('我是谁'))
        self.assertEqual(result['chars'], len(body))
        self.assertEqual(result['path'], str(ap.portrait_path(self.data_dir)))
        rows = [item for item in self.memory.search('', layer='soul', limit=30)
                if 'portrait' in (item.get('tags') or [])]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['content'], body)
        self.assertEqual(result['memory_id'], str(rows[0]['id']))
        self.assertTrue(result['created'])

    def test_refresh_with_nested_dict_config_writes_file(self):
        body = '我是一个在小群里陪人聊天的机器人。' * 6
        result = ap.refresh(self.memory, self.data_dir, generate=lambda prompt: body,
                            config={'ai': {'portrait_enabled': True}}, now=NOW)
        self.assertTrue(result['ok'])
        written = ap.read_portrait(self.data_dir)
        self.assertEqual(written, body)
        self.assertGreaterEqual(len(written), ap.MIN_BODY)
        self.assertEqual(result['path'], str(ap.portrait_path(self.data_dir)))
        self.assertFalse(ap.history_dir(self.data_dir).exists())

    def test_refresh_rejects_async_generate(self):
        async def generate(prompt):
            return '不会跑到这里'

        result = ap.refresh(self.memory, self.data_dir, generate=generate,
                            config=NestedConfig({'ai': {'portrait_enabled': True}}), now=NOW)
        self.assertEqual(result, {'ok': False, 'reason': 'generate_async_unsupported'})
        self.assertFalse((self.data_dir / '我是谁.md').exists())

    # ---------- import 副作用 ----------

    def test_import_creates_no_files(self):
        with tempfile.TemporaryDirectory() as fresh:
            env = dict(os.environ)
            env['PYTHONPATH'] = str(ROOT)
            env.pop('PYTHONIOENCODING', None)
            env['PYTHONIOENCODING'] = 'utf-8'
            done = subprocess.run([sys.executable, '-c', 'import src.agent_portrait'],
                                  cwd=fresh, env=env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(os.listdir(fresh), [])
            self.assertEqual(done.stderr, '')


if __name__ == '__main__':
    unittest.main()
