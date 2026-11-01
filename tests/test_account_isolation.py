"""多账号隔离测试：插件状态与人设图片目录必须按账号分开（并发穿数据是硬伤）。"""
import pathlib
import tempfile
import unittest

from src.account_book import AccountBook
from src.qq_app import QQRuntime


class DataIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        (self.root / 'data' / 'qq-only').mkdir(parents=True)
        (self.root / 'config.qq.yaml').write_text(
            'ai:\n  model: demo\nqq:\n  mode: mock\nqq_only:\n  mode: mock\n  auto_start: false\n',
            encoding='utf-8')
        self.book = AccountBook(self.root)


    @staticmethod
    def _close(runtime):
        """Runtime.close() 是 async；测试里只需要把 sqlite 句柄关掉，否则临时目录删不掉。"""
        try:
            runtime.db.close()
        except Exception:
            pass

    def test_default_account_keeps_legacy_paths(self):
        config, db = self.book.paths('default')
        runtime = QQRuntime(root=self.root, config_path=config, db_path=db)
        self.addCleanup(self._close, runtime)
        self.assertEqual(pathlib.Path(runtime.plugins.data_dir), self.root / 'data' / 'qq-only')

    def test_two_accounts_have_separate_plugin_dirs(self):
        first = self.book.add('A')
        second = self.book.add('B')
        runtimes = []
        for item in (first, second):
            config, db = self.book.paths(item['id'])
            runtime = QQRuntime(root=self.root, config_path=config, db_path=db,
                                data_root=config.parent / 'data' / 'qq-only')
            runtimes.append(runtime)
            self.addCleanup(self._close, runtime)
        first_dir = pathlib.Path(runtimes[0].plugins.data_dir).resolve()
        second_dir = pathlib.Path(runtimes[1].plugins.data_dir).resolve()
        self.assertNotEqual(first_dir, second_dir, '两个账号不能共用插件状态目录')
        self.assertNotEqual(first_dir, (self.root / 'data' / 'qq-only').resolve())
        # 插件状态文件写到各自目录，互不覆盖
        (first_dir / 'probe.json').write_text('{"a": 1}', encoding='utf-8')
        self.assertFalse((second_dir / 'probe.json').exists())

    def test_persona_images_are_per_account(self):
        created = self.book.add('C')
        config, db = self.book.paths(created['id'])
        runtime = QQRuntime(root=self.root, config_path=config, db_path=db,
                            data_root=config.parent / 'data' / 'qq-only')
        self.addCleanup(self._close, runtime)
        self.assertIn(str(config.parent), str(runtime.style.image_dir))


if __name__ == '__main__':
    unittest.main()
