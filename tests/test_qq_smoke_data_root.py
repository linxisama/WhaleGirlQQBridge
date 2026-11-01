"""自检（src/gui/smoke.py）的运行期数据必须落在可写数据根，不能落在解包区。

背景：冻结模式下 gui.BUNDLE 与 __file__ 都在安装目录内的 _MEIPASS 解包区，装到
Program Files 时只读。自检若把 config / db / agent_workspace 写在那里，写入会静默
失败，而自检仍报 ok=true —— 看起来通过，实际什么都没验到。
"""
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import src.qq_app as qq_app
from src import agent_workspace
from src.gui import smoke
from src.log_api import data_dir_of


class SelfcheckRootTests(unittest.TestCase):
    def test_default_is_under_the_writable_data_root(self):
        with smoke.selfcheck_root() as path:
            self.assertTrue(Path(path).is_dir())
            self.assertEqual(Path(path).resolve().parent, Path(qq_app.DATA_ROOT).resolve())

    def test_explicit_data_root_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            with smoke.selfcheck_root(tmp) as path:
                self.assertEqual(Path(path).resolve().parent, Path(tmp).resolve())

    def test_directory_is_cleaned_up_after_the_with_block(self):
        with smoke.selfcheck_root() as path:
            (Path(path) / 'probe.txt').write_text('x', encoding='utf-8')
        self.assertFalse(Path(path).exists(), '自检临时目录必须退出时删除')

    def test_runtime_data_follows_the_selfcheck_root(self):
        # 自检把 QQRuntime(root=...) 指到临时目录后，控制台笔记的工作区也必须跟着走，
        # 不能回到 gui.BUNDLE（冻结模式 = _MEIPASS 解包区）。
        with smoke.selfcheck_root() as path:
            base = Path(path).resolve()
            bot = SimpleNamespace(root=str(path))
            workspace = Path(agent_workspace.root(data_dir_of(bot), 'console')).resolve()
            self.assertEqual(workspace, base / 'data' / 'agent_workspace' / 'console')
            self.assertIn(str(base), str(workspace))

    def test_run_uses_the_helper_instead_of_the_system_temp_dir(self):
        source = inspect.getsource(smoke.run)
        self.assertIn('selfcheck_root(', source)
        self.assertNotIn('TemporaryDirectory(', source,
                         'run() 会退回系统临时目录，冻结模式下又写不进解包区了')


if __name__ == '__main__':
    unittest.main()
