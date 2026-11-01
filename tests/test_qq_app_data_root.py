"""冻结模式下 src.qq_app 的可写数据根必须落到用户目录（LOCALAPPDATA/WhaleGirlQQ）。

背景：PyInstaller 冻结后 __file__ 在 _internal 解包目录，装到 Program Files 时只读。
账号簿 accounts.json / 运行时注册表 / 默认账号 config/db 若还写进 _internal 会失败。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.qq_app as qq_app


class FrozenDataRootTests(unittest.TestCase):

    def test_source_mode_uses_repo_root(self):
        # 源码模式：可写数据根 == 仓库根（__file__ 的 parents[1]）。
        self.assertEqual(qq_app.DATA_ROOT, qq_app.ROOT)
        self.assertEqual(qq_app.DATA_ROOT, Path(qq_app.__file__).resolve().parent.parent)

    def test_frozen_uses_localappdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(qq_app.sys, 'frozen', True, create=True),                  mock.patch.dict(qq_app.os.environ, {'LOCALAPPDATA': tmp}, clear=False):
                folder = qq_app._resolve_data_root()
            self.assertEqual(folder, Path(tmp) / 'WhaleGirlQQ')
            self.assertTrue(folder.is_dir(), 'LOCALAPPDATA/WhaleGirlQQ 应被建出来')

    def test_frozen_without_localappdata_falls_back_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(qq_app.sys, 'frozen', True, create=True),                  mock.patch.object(qq_app.Path, 'home', return_value=Path(tmp)),                  mock.patch.dict(qq_app.os.environ, {}, clear=False):
                qq_app.os.environ.pop('LOCALAPPDATA', None)
                folder = qq_app._resolve_data_root()
            self.assertEqual(folder, Path(tmp) / 'WhaleGirlQQ')
            self.assertTrue(folder.is_dir())

    def test_qqruntime_default_root_is_data_root(self):
        # QQRuntime() 不传 root 时也必须落到可写根，而不是解包目录。
        defaults = qq_app.QQRuntime.__init__.__defaults__
        self.assertIsNotNone(defaults)
        self.assertEqual(defaults[0], qq_app.DATA_ROOT)


if __name__ == '__main__':
    unittest.main()
