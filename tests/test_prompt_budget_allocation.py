"""提示词预算的一次性规划入口：最小配额 + 按比例缩 + 句末截断 + JSON 整块。

旧 compose() 的行为必须原样保留（wiring/memory_guard 两套测试依赖它），
这里只测新增的 allocate() 和 clip() 的切法。
"""
import json
import unittest

from src.prompt_budget import DEFAULT_MINIMUMS, allocate, clip, compose, summary


class AllocateMinimumTests(unittest.TestCase):
    """a) 预算充足时各类块都不小于最小配额，且 total 不超限。"""

    def test_every_block_keeps_its_minimum(self):
        blocks = [
            ('人设', '人' * 3000, 3000, 0),
            ('插件', '插' * 900, 900, 1),
            ('记忆', '记' * 1400, 1400, 2),
            ('日记', '日' * 800, 800, 3),
        ]
        text, stats = allocate(blocks, limit=6000)
        by_name = {item['name']: item for item in stats['blocks']}
        for name, minimum in DEFAULT_MINIMUMS.items():
            self.assertGreaterEqual(by_name[name]['chars'], minimum, name)
            self.assertFalse(by_name[name]['dropped'], name)
        self.assertLessEqual(stats['total'], stats['limit'])
        self.assertEqual(stats['total'], len(text))
        self.assertEqual(stats['remaining'], stats['limit'] - stats['total'])

    def test_minimums_none_uses_defaults_and_empty_disables(self):
        blocks = [('日记', '日' * 800, 800, 3)]
        _text, with_default = allocate(blocks, limit=100)
        self.assertGreater(with_default['blocks'][0]['chars'], 0)
        _text, without = allocate(blocks, limit=100, minimums={})
        self.assertTrue(without['blocks'][0]['dropped'])


class AllocateOverflowTests(unittest.TestCase):
    """b) 总预算不足时按比例缩，且绝不超限。"""

    def test_shrinks_proportionally_without_exceeding_limit(self):
        blocks = [
            ('人设', '人' * 3000, 0, 0),
            ('插件', '插' * 800, 0, 1),
            ('记忆', '记' * 1200, 0, 2),
            ('日记', '日' * 700, 0, 3),
        ]
        text, stats = allocate(blocks, limit=2000)
        self.assertLessEqual(stats['total'], 2000)
        self.assertEqual(stats['total'], len(text))
        live = [item for item in stats['blocks'] if not item['dropped']]
        self.assertGreaterEqual(len(live), 3, '保底块不该被整块丢')
        for item in live:
            self.assertTrue(item['truncated'])
            self.assertIn('缩', item['reason'])

    def test_zero_priority_block_is_dropped_last(self):
        blocks = [('人设', '人' * 500, 0, 0), ('日记', '日' * 3000, 0, 3)]
        _text, stats = allocate(blocks, limit=600, minimums={})
        by_name = {item['name']: item for item in stats['blocks']}
        self.assertFalse(by_name['人设']['dropped'])
        self.assertTrue(by_name['日记']['dropped'])

    def test_minimum_never_exceeds_what_the_block_actually_has(self):
        # 单块上限比保底还小时，保底必须跟着块走，否则按比例缩会算出负数
        text, stats = allocate([('插件', '插' * 2000, 80, 1)], limit=6000)
        self.assertEqual(stats['blocks'][0]['chars'], 80)
        self.assertEqual(stats['total'], len(text))
        self.assertLessEqual(stats['total'], stats['limit'])

    def test_zero_limit_drops_everything(self):
        text, stats = allocate([('记忆', '记' * 500, 0, 2)], limit=0)
        self.assertEqual(text, '')
        self.assertEqual(stats['total'], 0)
        self.assertTrue(stats['blocks'][0]['dropped'])


class ClipSentenceTests(unittest.TestCase):
    """c) 句末优先：句号在 120 位时，切完必须以句号结尾。"""

    def test_cuts_at_sentence_end(self):
        text = '甲' * 119 + '。' + '乙' * 80
        self.assertEqual(len(text), 200)
        cut = clip(text, 150)
        self.assertTrue(cut.endswith('。'), cut[-3:])
        self.assertEqual(len(cut), 120)

    def test_falls_back_to_hard_cut_without_punctuation(self):
        self.assertEqual(clip('丙' * 200, 50), '丙' * 50)

    def test_ignores_punctuation_too_close_to_the_front(self):
        # 句号在第 10 位（不到一半），硬切比只剩 10 字更划算
        self.assertEqual(clip('丁' * 9 + '。' + '戊' * 190, 100), ('丁' * 9 + '。' + '戊' * 90))

    def test_newline_counts_as_sentence_end(self):
        self.assertEqual(clip('己' * 79 + '\n' + '庚' * 100, 100), '己' * 79 + '\n')

    def test_zero_limit_means_unlimited(self):
        self.assertEqual(clip('辛' * 500, 0), '辛' * 500)

    def test_short_text_untouched(self):
        self.assertEqual(clip('短句。', 100), '短句。')


class ClipJsonTests(unittest.TestCase):
    """d) JSON 块整块保留或整块丢弃，绝不出现半截。"""

    def test_json_that_does_not_fit_is_dropped_whole(self):
        payload = json.dumps({'标签': ['猫', '狗'], '说明': '很长' * 50}, ensure_ascii=False)
        self.assertGreater(len(payload), 60)
        self.assertEqual(clip(payload, 60), '')

    def test_json_that_fits_is_kept_verbatim(self):
        payload = json.dumps({'ok': True}, ensure_ascii=False)
        self.assertEqual(clip(payload, 200), payload)

    def test_json_block_never_half_in_composed_output(self):
        payload = json.dumps({'a': 'x' * 300}, ensure_ascii=False)
        text, stats = allocate([('插件', payload, 80, 1)], limit=6000, minimums={})
        self.assertEqual(text, '')
        self.assertEqual(stats['blocks'][0]['chars'], 0)
        self.assertTrue(stats['blocks'][0]['dropped'])
        with self.assertRaises(ValueError):
            json.loads(text)

    def test_plain_text_starting_with_brace_is_not_treated_as_json(self):
        self.assertEqual(clip('{不是 JSON 的说明文字', 5), '{不是 J')


class ComposeBackwardCompatTests(unittest.TestCase):
    """e) 旧 compose() 行为不变：逐块封顶 + 整块丢，无保底。"""

    def test_caps_each_block_and_marks_truncated(self):
        text, stats = compose([('记忆', '记' * 2000, 1200, 2)], limit=6000)
        self.assertEqual(len(text), 1200)
        self.assertTrue(stats['blocks'][0]['truncated'])
        self.assertEqual(stats['total'], 1200)

    def test_drops_lowest_priority_first(self):
        blocks = [('身份', 'A' * 100, 0, 0), ('记忆', 'B' * 300, 0, 2), ('日记', 'C' * 300, 0, 3)]
        text, stats = compose(blocks, limit=450)
        self.assertIn('A' * 100, text)
        self.assertEqual([b['name'] for b in stats['blocks'] if b['dropped']], ['日记'])
        self.assertTrue(text.endswith('B' * 300))

    def test_compose_does_not_apply_minimums(self):
        _text, stats = compose([('日记', '日' * 3000, 0, 3)], limit=100)
        self.assertTrue(stats['blocks'][0]['dropped'])

    def test_empty_blocks_are_skipped(self):
        text, stats = compose([('记忆', '', 1200, 2), ('日记', '   ', 900, 3)], limit=100)
        self.assertEqual(text, '')
        self.assertEqual(stats['blocks'], [])

    def test_stats_keep_the_old_keys(self):
        _text, stats = compose([('记忆', '记' * 10, 1200, 2)], limit=6000)
        self.assertEqual(set(stats['blocks'][0]) >= {'name', 'chars', 'dropped', 'truncated'}, True)


class SummaryTests(unittest.TestCase):
    """4) summary() 一行读出谁被丢、谁被截、为什么。"""

    def test_names_drop_and_truncation_reasons(self):
        # 保底模式下超预算只缩不丢，所以分成两个场景各验一次
        _text, shrunk = allocate([('人设', '人' * 3000, 0, 0), ('日记', '日' * 700, 0, 3)],
                                 limit=1200)
        self.assertIn('提示构成', shrunk and summary(shrunk))
        self.assertIn('上限 1200', summary(shrunk))
        self.assertIn('截：', summary(shrunk))
        _text, dropped = allocate([('人设', '人' * 800, 0, 0), ('日记', '日' * 3000, 0, 3)],
                                  limit=1000, minimums={})
        line = summary(dropped)
        self.assertIn('丢：日记', line)
        self.assertIn('优先级 3', line)

    def test_empty_stats(self):
        self.assertEqual(summary({}), '提示构成：无注入块')


if __name__ == '__main__':
    unittest.main()
