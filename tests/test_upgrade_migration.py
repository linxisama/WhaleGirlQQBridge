"""覆盖升级：配置备份、数据保留、版本记录、重复运行幂等。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.upgrade_migration import UpgradeManager


class UpgradeMigrationTests(unittest.TestCase):
    def test_upgrade_backs_up_config_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = base / 'config.qq.yaml'
            config.write_text('ai:\n  model: old\n', encoding='utf-8')
            db = sqlite3.connect(base / 'messages.sqlite3')
            db.execute('CREATE TABLE inbox(key TEXT PRIMARY KEY)')
            db.execute("INSERT INTO inbox VALUES('keep')")
            first = UpgradeManager('5.2.16', db, config, base).run()
            self.assertTrue(first['changed'])
            self.assertTrue(Path(first['backup']).is_file())
            second = UpgradeManager('5.2.16', db, config, base).run()
            self.assertFalse(second['changed'])
            self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0], 1)
            self.assertEqual((base / 'upgrade_state.json').exists(), True)
            third = UpgradeManager('5.2.17', db, config, base).run()
            self.assertTrue(third['changed'])
            self.assertTrue(Path(third['backup']).is_file())
            self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0], 1)
            db.close()

    def test_config_is_not_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = base / 'config.qq.yaml'
            original = 'ai:\n  model: keep-me\n'
            config.write_text(original, encoding='utf-8')
            db = sqlite3.connect(base / 'messages.sqlite3')
            UpgradeManager('5.2.16', db, config, base).run()
            UpgradeManager('5.2.17', db, config, base).run()
            self.assertEqual(config.read_text(encoding='utf-8'), original)
            db.close()


if __name__ == '__main__':
    unittest.main()
