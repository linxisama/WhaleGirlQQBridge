"""基础包也必须能识别长图：核心切片，不依赖市场 long_image_reader。"""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QImage, QColor

from src.sticker_library import tiles_for_vision
from src.plugins.manager import PluginManager


def tall_png(width=900, height=9000):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor('#ffffff'))
    for y in range(0, height, 40):
        for x in range(0, width, 30):
            image.setPixelColor(x, y, QColor('#222222'))
    buffer = QBuffer(); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    return bytes(buffer.data())


class LongImageCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_tiles_keep_readable_width_and_cover_height(self):
        data = tall_png()
        tiles = tiles_for_vision(data, max_tiles=6)
        self.assertGreaterEqual(len(tiles), 2)
        self.assertLessEqual(len(tiles), 6)
        heights = []
        for tile in tiles:
            raw = base64.b64decode(tile.split(',', 1)[1])
            image = QImage.fromData(raw)
            self.assertEqual(image.width(), 900)
            self.assertGreater(image.height(), 1000)
            heights.append(image.height())
        self.assertGreaterEqual(sum(heights), 9000)

    async def test_plugin_uses_tiles_when_long_reader_absent(self):
        manager = PluginManager(Path(tempfile.mkdtemp()))
        manager._plugins.pop('long_image_reader', None)
        data = tall_png(600, 6000)
        store = SimpleNamespace(resolve=AsyncMock(return_value={
            'data': data, 'width': 600, 'height': 6000, 'ext': 'png'}),
            record_description=lambda *a, **k: None)
        ai = SimpleNamespace(api_url='https://api.deepseek.com/v1/chat/completions',
                             model='deepseek-chat',
                             describe_images=AsyncMock(return_value='长图文字：第一段 第二段'))
        runtime = SimpleNamespace(ai=ai, mode='onebot', note=lambda text: None,
                                  stickers=store, account=lambda: 'a')
        message = {'media': [{'type': 'image', 'url': 'https://example.test/x.png'}]}
        await manager.describe_images(message, runtime)
        args = ai.describe_images.await_args
        self.assertGreater(len(args.args[0]), 1)
        self.assertIn('长图切成', message['vision_status'] + message['vision_description'])
        self.assertEqual(message['vision_status'], 'ok')


if __name__ == '__main__':
    unittest.main()
