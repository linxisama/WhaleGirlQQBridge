"""发布 zip 写出：正斜杠条目、目录条目（含空目录）、非 ASCII 往返、可复现、拒绝覆盖。

只用 tempfile + 标准库，不碰 GUI/网络/QQ/构建产物，整模块秒级。
"""
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_release import write_archive

FILES=('keep.py','notes.txt','nested/a/b.txt','中文/名字.txt','assets/图标.png')
DIRECTORIES=('empty','nested','nested/a','中文','assets')
SKIPPED=('stale.pyc','nested/__pycache__/junk.pyc')


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        base=Path(self.tmp.name)
        self.directory=base/'release'
        # 空目录必须留空：它只能靠显式目录条目进包。
        (self.directory/'empty').mkdir(parents=True)
        for name in FILES:
            path=self.directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('payload:'+name,encoding='utf-8')
        for name in SKIPPED:
            path=self.directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('bytecode',encoding='utf-8')
        self.archive=base/'out'/'whalegirl-release.zip'

    def tearDown(self):
        self.tmp.cleanup()

    def names(self,archive=None):
        with zipfile.ZipFile(archive or self.archive) as opened:
            return opened.namelist()

    def test_entries_use_forward_slashes_without_empty_names(self):
        result=write_archive(self.directory,self.archive)
        names=self.names()
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name,name)
            self.assertNotIn('\\',name,name)
            self.assertFalse(name.startswith('/'),name)
            self.assertNotIn('..',name.split('/'),name)
        # 返回的统计必须与包内实况、磁盘实况三方一致。
        self.assertEqual(result['entries'],len(names))
        self.assertEqual(result['files'],len(FILES))
        self.assertEqual(result['dirs'],len(DIRECTORIES))
        self.assertEqual(result['bytes'],self.archive.stat().st_size)
        self.assertEqual(result['sha256'],hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_directories_are_written_as_explicit_entries(self):
        write_archive(self.directory,self.archive)
        names=set(self.names())
        for name in DIRECTORIES:
            self.assertIn(name+'/',names,name)
        with zipfile.ZipFile(self.archive) as opened:
            self.assertEqual(opened.getinfo('empty/').file_size,0)
            self.assertEqual(opened.read('empty/'),b'')
        # 有内容的目录同样要有自己的条目，不能因为父目录存在就省略。
        self.assertIn('nested/',names)
        self.assertIn('nested/a/',names)
        self.assertIn('nested/a/b.txt',names)

    def test_non_ascii_names_round_trip(self):
        write_archive(self.directory,self.archive)
        names=self.names()
        self.assertIn('中文/名字.txt',names)
        self.assertIn('中文/',names)
        self.assertIn('assets/图标.png',names)
        with zipfile.ZipFile(self.archive) as opened:
            self.assertEqual(opened.read('中文/名字.txt'),(self.directory/'中文'/'名字.txt').read_bytes())
            self.assertTrue(opened.getinfo('中文/名字.txt').flag_bits & 0x800,'非 ASCII 名必须置 UTF-8 标志位')

    def test_deterministic_rebuild_is_byte_identical(self):
        first=write_archive(self.directory,self.archive,deterministic=True)
        self.archive.unlink()
        second=write_archive(self.directory,self.archive,deterministic=True)
        self.assertEqual(first['sha256'],second['sha256'])
        self.assertEqual(first['bytes'],second['bytes'])
        self.assertEqual(first['sha256'],hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(self.names(),self.names())

    def test_mtime_archive_keeps_same_entries_without_deterministic(self):
        deterministic=write_archive(self.directory,self.archive,deterministic=True)
        self.archive.unlink()
        # 非 deterministic 不要求字节相同（沿用 mtime），但条目集合必须一致。
        plain=write_archive(self.directory,self.archive)
        self.assertEqual((plain['entries'],plain['files'],plain['dirs']),
                         (deterministic['entries'],deterministic['files'],deterministic['dirs']))
        self.assertEqual(plain['sha256'],hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_existing_archive_is_never_overwritten(self):
        self.archive.parent.mkdir(parents=True,exist_ok=True)
        self.archive.write_bytes(b'previous release')
        with self.assertRaises(FileExistsError):
            write_archive(self.directory,self.archive)
        self.assertEqual(self.archive.read_bytes(),b'previous release')

    def test_pycache_and_pyc_are_skipped(self):
        write_archive(self.directory,self.archive)
        names=self.names()
        self.assertFalse([name for name in names if '__pycache__' in name],names)
        self.assertFalse([name for name in names if name.lower().endswith('.pyc')],names)
        self.assertIn('keep.py',names)
        self.assertEqual(len(names),len(FILES)+len(DIRECTORIES))


if __name__=='__main__':
    unittest.main()
