"""表情包提示的注入预算：总量封顶、JSON 始终完整、标签优先于长描述。

以前每个表情的描述各自截 120 字、标签先砍到 80 个，80×120≈9600 字，
宿主按 ai.plugin_chars（默认 800）从中间硬切，模型拿到的是半截 JSON。
这里守住三件事：整段不超过上限、任何长度都能解析、描述被截短时标签仍在。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.plugins.image_library import (HINT_NOTES_MID, HINT_PREFIX, HINT_TAIL,
                                       build_hint)

DESC = ('一只猫在疑惑地歪头看着屏幕上的鲸鱼，背景是蓝色的海洋和飘动的气泡，颜色偏暖。' * 4)[:120]


def parse_hint(text):
    """按固定前后缀取出标签数组与含义对象；格式坏了这里就会抛异常。"""
    head = text.index(HINT_PREFIX) + len(HINT_PREFIX)
    mid = text.index(HINT_NOTES_MID)
    labels = json.loads(text[head:mid])
    notes = json.loads(text[mid + len(HINT_NOTES_MID):text.index(HINT_TAIL)])
    return labels, notes


class FakeStickers:
    """假收藏表情库：ImageLibrary.entries() 只用到 entries()/path()/used()。"""

    def __init__(self, root, count, description=DESC, tags=('开心', '海洋')):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.items = []
        for index in range(count):
            (self.root / f'{index:064x}.png').write_bytes(b'\x89PNG' + bytes([index % 251]) * 40)
            self.items.append({'id': f'{index:064x}', 'label': f'贴纸{index:03d}',
                               'description': description, 'tags': list(tags),
                               'last_seen': 1000 - index})

    def entries(self):
        return [dict(item) for item in self.items]

    def path(self, ident):
        return str(self.root / f'{ident}.png')

    def used(self, ids):
        pass


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = None

    def plugin(self, **settings):
        if self.manager is None:
            from src.plugins.manager import PluginManager
            self.manager = PluginManager(self.root / 'plugins')
        values = {'library': '', 'autoSelect': True}
        values.update(settings)
        self.manager.update_settings('image_library', values)
        return self.manager._plugins['image_library']

    def hint(self, stickers, plugin=None, host_limit=None):
        runtime = SimpleNamespace(mode='official', stickers=stickers)
        if host_limit is not None:
            runtime.config = SimpleNamespace(
                get=lambda key, default=None: host_limit if key == 'ai.plugin_chars' else default)
        return (plugin or self.plugin()).on_pre_ai({}, [], runtime)['extra_system']

    # a) 200 个各带 120 字描述的表情，整段不超过上限
    def test_two_hundred_long_descriptions_stay_within_limit(self):
        text = self.hint(FakeStickers(self.root / 'img', 200))
        self.assertLessEqual(len(text), 800)
        labels, notes = parse_hint(text)
        self.assertTrue(labels)
        self.assertEqual(len(notes), len(labels))

    # b) 注入文本能 json 解析，不是半截
    def test_hint_is_complete_json_after_truncation(self):
        text = self.hint(FakeStickers(self.root / 'img', 200))
        labels, notes = parse_hint(text)
        self.assertTrue(all(isinstance(label, str) for label in labels))
        self.assertTrue(all(len(value) <= 40 for value in notes.values()))

    # c) 描述被截短时标签仍在
    def test_labels_survive_description_truncation(self):
        text = self.hint(FakeStickers(self.root / 'img', 200))
        labels, notes = parse_hint(text)
        for label in notes:
            self.assertIn(label, labels)
        self.assertTrue(any(value.endswith('…') for value in notes.values()))
        self.assertEqual([label for label in labels if label in notes], labels)

    # d) 没有描述的表情不会挤掉有描述的
    def test_undescribed_stickers_do_not_crowd_out_described(self):
        stickers = FakeStickers(self.root / 'img', 200, description='', tags=())
        for item in stickers.items[:12]:
            item['description'] = DESC
        labels, notes = parse_hint(self.hint(stickers))
        described = [f'贴纸{index:03d}' for index in range(12)]
        self.assertEqual(labels, described)          # 没含义的一条都没进来
        self.assertEqual(set(notes), set(described))

    def test_described_labels_come_first_when_budget_is_tight(self):
        stickers = FakeStickers(self.root / 'img', 200, description='', tags=())
        for item in stickers.items[-12:]:
            item['description'] = DESC
        labels, notes = parse_hint(self.hint(stickers))
        self.assertEqual(labels[:len(notes)], list(notes))
        self.assertTrue(set(notes) <= set(labels))

    # e) 上限为 0 / 极大都不崩
    def test_zero_limit_injects_nothing(self):
        self.assertEqual(self.hint(FakeStickers(self.root / 'img', 50), host_limit=0), '')
        self.assertEqual(self.hint(FakeStickers(self.root / 'img2', 50), self.plugin(hintChars=0)), '')

    def test_huge_limit_shows_more_labels_and_stays_complete(self):
        default = parse_hint(self.hint(FakeStickers(self.root / 'a', 200)))[0]
        text = self.hint(FakeStickers(self.root / 'b', 200), self.plugin(hintChars=4000),
                         host_limit=4000)
        labels, notes = parse_hint(text)
        self.assertLessEqual(len(text), 4000)
        self.assertGreater(len(labels), len(default))
        self.assertEqual(len(notes), len(labels))
        # 荒谬的上限也不能把函数带崩或切坏 JSON
        huge = build_hint(['甲'], {'甲': DESC}, 10 ** 6, 10 ** 6)
        self.assertEqual(parse_hint(huge), (['甲'], {'甲': DESC}))

    # 插件自己的上限不能超过宿主 ai.plugin_chars，否则宿主仍会把 JSON 切一半
    def test_host_plugin_chars_caps_below_plugin_setting(self):
        text = self.hint(FakeStickers(self.root / 'img', 200), host_limit=300)
        self.assertLessEqual(len(text), 300)
        parse_hint(text)

    # 描述字数上限可配置
    def test_desc_chars_is_configurable(self):
        text = self.hint(FakeStickers(self.root / 'img', 20), self.plugin(descChars=10))
        _, notes = parse_hint(text)
        self.assertTrue(notes)
        self.assertTrue(all(len(value) <= 10 for value in notes.values()))

    # 预算连 JSON 骨架都放不下时退回纯文本，不切半截 JSON
    def test_tiny_budget_falls_back_to_plain_text(self):
        text = self.hint(FakeStickers(self.root / 'img', 20), self.plugin(hintChars=120))
        self.assertLessEqual(len(text), 120)
        self.assertTrue(text.startswith('可发送图片表情包'))
        self.assertNotIn('["', text)

    # 既有语义：空图库仍是原来那句提示
    def test_empty_library_message_is_unchanged(self):
        text = self.hint(FakeStickers(self.root / 'img', 0))
        self.assertIn('当前图库没有可发送的图片', text)


if __name__ == '__main__':
    unittest.main()
