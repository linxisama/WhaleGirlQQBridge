"""所有可写数据目录回退都必须走 src/data_root.py，不能猜 __file__ 的位置。

背景（真机证据）：PyInstaller 冻结后 __file__ 在解包目录 _internal 里，模块若拿
__file__ 往上退一级或两级再拼 data，就会在安装目录里建出 _internal/data —— 装到
只读位置失败、装到可写位置则写进安装目录。5.2.54 起自检在
_internal/data/agent_workspace/console 建空目录，根因就在这里。
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.data_root as data_root
import src.app_log as app_log
import src.extension_host as extension_host
import src.extension_registry as extension_registry
from src.gui import pc_workspace


REPO_ROOT = Path(data_root.__file__).resolve().parents[1]


def _inside_repo(path):
    """path 解析后是否落在仓库根之下（Windows 大小写与盘符交给 os.path 判定）。"""
    child = os.path.normcase(str(Path(path).resolve()))
    root = os.path.normcase(str(REPO_ROOT.resolve()))
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:  # 不同盘符，必然不在仓库内
        return False


def _entry_points():
    """五处默认数据目录：返回 (模块名, __file__, 取值函数)。"""
    return [
        ('pc_workspace.default_data_dir', pc_workspace.__file__, pc_workspace.default_data_dir),
        ('extension_registry.default_data_dir', extension_registry.__file__, extension_registry.default_data_dir),
        ('extension_host.default_data_dir', extension_host.__file__, extension_host.default_data_dir),
        ('app_log._default_data_dir', app_log.__file__, app_log._default_data_dir),
    ]


def _not_frozen():
    return not getattr(sys, "frozen", False) and not getattr(sys, "_MEIPASS", None)


@contextlib.contextmanager
def frozen(localappdata):
    """模拟冻结构建：sys.frozen=True + LOCALAPPDATA，并确保没有显式数据目录环境变量。"""
    with mock.patch.object(sys, 'frozen', True, create=True), \
            mock.patch.dict(os.environ, {'LOCALAPPDATA': str(localappdata)}, clear=False):
        os.environ.pop('WHALE_QQ_DATA_DIR', None)
        os.environ.pop('WHALE_QQ_HOME', None)
        yield


class SourceModeTests(unittest.TestCase):
    """未冻结时五处取值必须与改动前逐字相同（都是仓库根下的 data/）。"""

    def setUp(self):
        if not _not_frozen():
            self.skipTest('冻结解释器下源码模式不适用')

    def test_module_data_root_is_repo_root(self):
        self.assertEqual(data_root.DATA_ROOT, REPO_ROOT)
        self.assertEqual(data_root.resolve_data_root(), REPO_ROOT)

    def test_four_entry_points_still_repo_data(self):
        expected = REPO_ROOT / "data"
        for name, _file, get in _entry_points():
            with self.subTest(name=name):
                self.assertEqual(get(), expected)


class FrozenModeTests(unittest.TestCase):
    """冻结时五处都不能落在 __file__ 旁边，必须落 LOCALAPPDATA/WhaleGirlQQ。"""

    def test_sys_frozen_returns_localappdata_and_creates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with frozen(tmp):
                folder = data_root.resolve_data_root()
            self.assertEqual(folder, Path(tmp) / 'WhaleGirlQQ')
            self.assertTrue(folder.is_dir(), 'LOCALAPPDATA/WhaleGirlQQ 应被建出来')

    def test_meipass_alone_also_treated_as_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sys, '_MEIPASS', tmp, create=True), \
                    mock.patch.dict(os.environ, {'LOCALAPPDATA': tmp}, clear=False):
                os.environ.pop('WHALE_QQ_DATA_DIR', None)
                os.environ.pop('WHALE_QQ_HOME', None)
                folder = data_root.resolve_data_root()
            self.assertEqual(folder, Path(tmp) / 'WhaleGirlQQ')

    def test_entry_points_under_localappdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            with frozen(tmp):
                got = [(name, get()) for name, _file, get in _entry_points()]
            expected = Path(tmp) / 'WhaleGirlQQ' / 'data'
            for name, value in got:
                with self.subTest(name=name):
                    self.assertEqual(value, expected)

    def test_entry_points_not_next_to_module_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_inside_repo = _inside_repo(tmp)
            with frozen(tmp):
                got = [(name, file, get()) for name, file, get in _entry_points()]
            for name, file, value in got:
                with self.subTest(name=name):
                    if tmp_inside_repo:
                        # TEMP 被指到仓库内时，伪造的 LOCALAPPDATA 天然落在仓库根下，
                        # 第三条断言必然假红；只是环境不可判定，不代表代码退化。
                        self.skipTest('临时目录位于仓库内，无法判定')
                    beside = Path(file).resolve().parents[1]
                    self.assertNotEqual(value, beside / "data")
                    self.assertNotEqual(value, beside.parent / "data")
                    joined = str(value).replace("\\", "/").lower()
                    self.assertNotIn(str(beside).replace("\\", "/").lower(), joined)


class EnvOverrideTests(unittest.TestCase):
    """pc_workspace.default_data_dir 的显式环境变量优先于 data_root。"""

    def test_whale_qq_data_dir_wins_in_source_mode(self):
        if not _not_frozen():
            self.skipTest('冻结解释器下源码模式不适用')
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {'WHALE_QQ_DATA_DIR': tmp}, clear=False):
                got = pc_workspace.default_data_dir()
            self.assertEqual(got, Path(tmp))

    def test_whale_qq_data_dir_wins_when_frozen(self):
        with tempfile.TemporaryDirectory() as explicit, tempfile.TemporaryDirectory() as local:
            with frozen(local):
                with mock.patch.dict(os.environ, {'WHALE_QQ_DATA_DIR': explicit}, clear=False):
                    got = pc_workspace.default_data_dir()
            self.assertEqual(got, Path(explicit))

    def test_whale_qq_home_second_priority(self):
        if not _not_frozen():
            self.skipTest('冻结解释器下源码模式不适用')
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {'WHALE_QQ_HOME': tmp}, clear=False):
                os.environ.pop('WHALE_QQ_DATA_DIR', None)
                got = pc_workspace.default_data_dir()
            self.assertEqual(got, Path(tmp) / 'data')


if __name__ == '__main__':
    unittest.main()
