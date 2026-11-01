"""src/gui/appearance.py 的离线单元测试。

核心审核修正说明：原候选测的是 background_style 返回 CSS 片段，
但实测 Qt 样式表既忽略 background-size、又读不到中文/空格路径，
该函数已按真实可用行为移除；背景绘制改由 qq_gui.py 用 QPixmap 自绘。
"""

import os
import tempfile
import unittest
from pathlib import Path

from src.gui.appearance import (
    import_background,
    resolve_background,
    clear_background,
)


class ImportBackgroundTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_source(self, name="test.png", content=b"\x89PNG fake data"):
        path = self.root / name
        path.write_bytes(content)
        return path

    def test_import_copies_bytes_and_returns_relative_posix_path(self):
        src = self._make_source()
        rel = import_background(src, self.root)
        self.assertTrue(rel.startswith("data/backgrounds/"))
        self.assertTrue(rel.endswith(".png"))
        target = self.root / rel
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), src.read_bytes())

    def test_same_content_reuses_same_target_name(self):
        src = self._make_source()
        first = import_background(src, self.root)
        second = import_background(src, self.root)
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.root / "data" / "backgrounds").iterdir())), 1)

    def test_different_content_does_not_overwrite(self):
        first = import_background(self._make_source("a.png", b"one"), self.root)
        second = import_background(self._make_source("b.png", b"two"), self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(len(list((self.root / "data" / "backgrounds").iterdir())), 2)

    def test_uppercase_extension_is_accepted_and_lowercased(self):
        rel = import_background(self._make_source("photo.PNG"), self.root)
        self.assertTrue(rel.endswith(".png"))

    def test_rejected_extension(self):
        for name in ("evil.txt", "noext", "image.svg"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    import_background(self._make_source(name, b"x"), self.root)

    def test_missing_source_rejected(self):
        with self.assertRaises(ValueError):
            import_background(self.root / "missing.png", self.root)

    def test_directory_source_rejected(self):
        directory = self.root / "adir.png"
        directory.mkdir()
        with self.assertRaises(ValueError):
            import_background(directory, self.root)

    def test_non_ascii_filename_works(self):
        rel = import_background(self._make_source("鲸鱼娘 背景.png"), self.root)
        self.assertTrue((self.root / rel).is_file())


class ResolveBackgroundTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target = self.root / "data" / "backgrounds" / "abc.png"
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_relative_path(self):
        self.assertEqual(resolve_background("data/backgrounds/abc.png", self.root), self.target)

    def test_none_and_empty_return_none(self):
        self.assertIsNone(resolve_background(None, self.root))
        self.assertIsNone(resolve_background("", self.root))

    def test_missing_file_returns_none(self):
        self.assertIsNone(resolve_background("data/backgrounds/missing.png", self.root))

    def test_traversal_returns_none(self):
        self.assertIsNone(resolve_background("../../secret.png", self.root))

    def test_absolute_path_inside_root_is_accepted(self):
        self.assertEqual(resolve_background(str(self.target), self.root), self.target)

    def test_non_string_input_returns_none(self):
        for bad in (123, ["a"], {"p": 1}):
            with self.subTest(bad=bad):
                self.assertIsNone(resolve_background(bad, self.root))

    def test_directory_returns_none(self):
        self.assertIsNone(resolve_background("data/backgrounds", self.root))


class ClearBackgroundTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_none_and_empty_are_noop(self):
        clear_background(None, self.root)
        clear_background("", self.root)

    def test_clear_existing_file_and_prune_empty_dirs(self):
        rel = import_background(self._file("src.png", b"img"), self.root)
        target = self.root / rel
        self.assertTrue(target.is_file())
        clear_background(rel, self.root)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "data" / "backgrounds").exists())
        self.assertFalse((self.root / "data").exists())

    def test_clear_missing_file_is_silent(self):
        (self.root / "data" / "backgrounds").mkdir(parents=True)
        clear_background("data/backgrounds/nope.png", self.root)

    def test_clear_traversal_raises(self):
        with self.assertRaises(ValueError):
            clear_background("../../outside.png", self.root)

    def test_clear_keeps_other_files(self):
        keep = self._file("keep.png", b"keep")
        rel_keep = import_background(keep, self.root)
        rel_drop = import_background(self._file("drop.png", b"drop"), self.root)
        clear_background(rel_drop, self.root)
        self.assertTrue((self.root / rel_keep).is_file())
        self.assertFalse((self.root / rel_drop).exists())

    def _file(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        return path


if __name__ == "__main__":
    unittest.main()
