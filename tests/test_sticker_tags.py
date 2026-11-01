"""表情库标签/语义检索：标签参与提示与本地检索，不调模型。"""
import base64
import tempfile
import unittest
from pathlib import Path

from src.sticker_library import StickerLibrary, derive_tags

# 1x1 transparent PNG
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2n0sAAAAASUVORK5CYII=')


class StickerTagTests(unittest.TestCase):
    def test_derive_tags(self):
        self.assertEqual(derive_tags('开心的笑脸，蓝色背景')[:2], ['开心的笑脸', '蓝色背景'])

    def test_put_search_and_update_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            store = StickerLibrary(Path(folder))
            item, new = store.put(PNG, '开心', '笑脸，蓝色背景', tags=['笑脸', '蓝色'])
            self.assertTrue(new)
            self.assertEqual(item['tags'], ['笑脸', '蓝色'])
            self.assertEqual(store.search('笑脸')[0]['id'], item['id'])
            store.update(item['id'], '开心', '笑脸，蓝色背景', tags=['开心', '表情'])
            self.assertIn('表情', store.get(item['id'])['tags'] if hasattr(store, 'get') else store.search('开心')[0]['tags'])


if __name__ == '__main__':
    unittest.main()
