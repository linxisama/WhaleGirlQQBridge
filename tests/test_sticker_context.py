"""表情要贴合语境：没有相关标签就不发，别轮转发无关图。"""
import unittest

from src.plugins.sticker_cadence import StickerCadence


class StickerContextTests(unittest.TestCase):
    def test_picks_matching_label(self):
        labels = ['开心', '疑惑', '生气', '晚安']
        self.assertEqual(StickerCadence.best_label('今天好开心啊哈哈哈哈', labels), '开心')

    def test_uses_description_notes(self):
        labels = ['贴纸1', '贴纸2']
        notes = {'贴纸2': '一只猫在疑惑地歪头'}
        self.assertEqual(StickerCadence.best_label('她歪头疑惑', labels, notes), '贴纸2')

    def test_no_match_means_no_sticker(self):
        self.assertIsNone(StickerCadence.best_label('今天天气不错', ['开心', '生气']))

    def test_empty_query_means_no_sticker(self):
        self.assertIsNone(StickerCadence.best_label('', ['开心']))


if __name__ == '__main__':
    unittest.main()
