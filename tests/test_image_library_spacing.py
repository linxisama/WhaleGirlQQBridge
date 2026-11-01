"""表情包回复的空格：``[[表情:标签]]`` 摘掉后不能留下多余空白。

模型很自然地写成"晚安 [[表情:睡觉]]""好呀 [[表情:开心]] 晚安""晚安 [[表情:睡觉]]。"，
直接删标记就会把双空格、标点前空格原样发到 QQ —— 用户看到的就是"表情包回复里有空格"。
"""
import unittest

from src.plugins.image_library import MARKER, strip_markers


class StripMarkerSpacingTests(unittest.TestCase):
    def test_trailing_marker_leaves_no_trailing_space(self):
        self.assertEqual(strip_markers('晚安 [[表情:睡觉]]'), '晚安')

    def test_leading_marker_leaves_no_leading_space(self):
        self.assertEqual(strip_markers('[[表情:开心]] 在的'), '在的')

    def test_middle_marker_collapses_to_one_space(self):
        self.assertEqual(strip_markers('好呀 [[表情:开心]] 晚安'), '好呀 晚安')

    def test_no_space_before_punctuation(self):
        self.assertEqual(strip_markers('晚安 [[表情:睡觉]]。'), '晚安。')

    def test_marker_only_becomes_empty(self):
        self.assertEqual(strip_markers('[[表情:睡觉]]'), '')

    def test_newlines_are_preserved(self):
        self.assertEqual(strip_markers('第一句\n第二句 [[表情:睡觉]]'), '第一句\n第二句')

    def test_full_width_colon_and_other_labels(self):
        self.assertEqual(strip_markers('好 [[表情：猫猫]] ！'), '好！')

    def test_text_without_marker_is_untouched(self):
        self.assertEqual(strip_markers('普通回复，没有标记。'), '普通回复，没有标记。')

    def test_marker_regex_still_matches_labels(self):
        self.assertEqual(MARKER.findall('晚安 [[表情:睡觉]]'), ['睡觉'])
        self.assertEqual(MARKER.findall('[[表情：猫猫]] 好'), ['猫猫'])


if __name__ == '__main__':
    unittest.main()
