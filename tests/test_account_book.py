"""多账号：账号簿单元测试（路径隔离、迁移、增删、克隆配置）。"""
import json
import pathlib
import tempfile
import unittest

from src.account_book import AccountBook, DEFAULT_ACCOUNT_ID


class AccountBookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        (self.root / 'data' / 'qq-only').mkdir(parents=True)
        (self.root / 'config.qq.yaml').write_text('ai:\n  model: demo\n', encoding='utf-8')

    def test_first_run_registers_existing_single_account_without_moving(self):
        book = AccountBook(self.root)
        items = book.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], DEFAULT_ACCOUNT_ID)
        self.assertEqual(book.active(), DEFAULT_ACCOUNT_ID)
        config, db = book.paths()
        self.assertEqual(config, self.root / 'config.qq.yaml')
        self.assertEqual(db, self.root / 'data' / 'qq-only' / 'messages.sqlite3')
        self.assertTrue((self.root / 'config.qq.yaml').is_file(), '原配置不能被动过')

    def test_add_account_copies_config_but_not_history(self):
        book = AccountBook(self.root)
        created = book.add('小号')
        config, db = book.paths(created['id'])
        self.assertTrue(config.is_file(), '新账号应复制一份配置')
        self.assertIn('demo', config.read_text(encoding='utf-8'))
        self.assertNotEqual(db, self.root / 'data' / 'qq-only' / 'messages.sqlite3')
        self.assertEqual(book.active(), created['id'])
        self.assertEqual(len(book.items()), 2)

    def test_select_persists_across_instances(self):
        book = AccountBook(self.root)
        created = book.add('A')
        book.select(DEFAULT_ACCOUNT_ID)
        again = AccountBook(self.root)
        self.assertEqual(again.active(), DEFAULT_ACCOUNT_ID)
        self.assertEqual(len(again.items()), 2)
        again.select(created['id'])
        self.assertEqual(AccountBook(self.root).active(), created['id'])

    def test_remove_keeps_folder_and_cannot_remove_last(self):
        book = AccountBook(self.root)
        created = book.add('临时')
        folder = self.root / 'data' / 'accounts' / created['id']
        self.assertTrue(folder.is_dir())
        book.remove(created['id'])
        self.assertTrue(folder.is_dir(), '删除账号不能删数据目录')
        self.assertEqual(len(book.items()), 1)
        with self.assertRaises(ValueError):
            book.remove(DEFAULT_ACCOUNT_ID)

    def test_rename_and_labels(self):
        book = AccountBook(self.root)
        created = book.add('旧名')
        book.rename(created['id'], '主号')
        self.assertEqual(book.get(created['id'])['label'], '主号')
        self.assertIn((created['id'], '主号'), book.labels())

    def test_unknown_account_raises(self):
        book = AccountBook(self.root)
        with self.assertRaises(KeyError):
            book.get('nope')
        with self.assertRaises(KeyError):
            book.select('nope')


if __name__ == '__main__':
    unittest.main()
