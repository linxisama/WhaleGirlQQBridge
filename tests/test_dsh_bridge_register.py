"""MCP 注册/撤销：临时 DSH_HOME、幂等、先备份、失败回滚、状态自检。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import dsh_memory_bridge as bridge
from src import local_agent_bridge as local

REPO = Path(__file__).resolve().parents[1]
BOT_SCRIPT = REPO / 'tools' / 'whale_bot_mcp.py'
BOT_URL = 'http://127.0.0.1:27499'


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dsh = Path(self.tmp.name) / '.dsh'
        self.path = bridge.profile_path(self.dsh)

    def tearDown(self):
        self.tmp.cleanup()

    def install_all(self, url=BOT_URL):
        return bridge.install_all(self.dsh, sys.executable, 'whale_memory_mcp.py', 'C:/bot',
                                  str(BOT_SCRIPT), url)

    def backups(self):
        return sorted(self.path.parent.glob('cordis.patch.yml.bak-whale-memory-*'))

    def test_uses_temp_home_only(self):
        self.assertNotEqual(self.dsh, Path.home() / '.dsh')
        self.install_all()
        self.assertTrue(str(self.path).startswith(str(self.dsh)))

    def test_install_all_registers_both_and_status_counts_tools(self):
        result = self.install_all()
        self.assertTrue(result['installed'])
        self.assertTrue(result['control_installed'])
        self.assertTrue(result['changed'])
        self.assertEqual(result['serverName'], 'whale_memory')
        self.assertEqual(result['controlServerName'], 'whale_bot')
        status = local.status(self.dsh)
        self.assertTrue(status['bot_registered'])
        self.assertEqual(status['bot_server_name'], 'whale_bot')
        self.assertEqual(status['bot_url'], BOT_URL)
        self.assertTrue(status['bot_command_exists'])
        self.assertTrue(status['bot_script_exists'])
        self.assertGreaterEqual(status['bot_tool_count'], 14)
        self.assertIn('bot_market_install', status['bot_tool_names'])
        self.assertTrue(status['bot_stdio_utf8'])
        self.assertTrue(status['bot_utf8_env'])
        self.assertTrue(status['bot_ready'], status['bot_problems'])
        self.assertIn('重启', status['hint'])

    def test_install_all_is_idempotent(self):
        self.install_all()
        first = self.path.read_text(encoding='utf-8')
        again = self.install_all()
        self.assertFalse(again['changed'])
        self.assertIsNone(again['backup'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), first)
        self.assertEqual(self.backups(), [])  # 没有原文件 / 内容没变，都不产生备份

    def test_update_backs_up_and_keeps_other_entries(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: mcp-snowluma\n  name: "@x/y"\n  config: {}\n',
                             encoding='utf-8')
        self.install_all()
        before_update = self.path.read_text(encoding='utf-8')
        result = self.install_all(url='http://127.0.0.1:27500')
        self.assertTrue(result['changed'])
        self.assertTrue(Path(result['backup']).is_file())
        self.assertEqual(self.backups()[-1].read_text(encoding='utf-8'), before_update)
        import yaml
        data = yaml.safe_load(self.path.read_text(encoding='utf-8'))
        rows = [row for item in data
                for row in (item['insert'] if isinstance(item, dict) and 'insert' in item
                            else [item])]
        self.assertEqual([row['id'] for row in rows],
                         ['mcp-snowluma', 'mcp-whale-memory', 'mcp-whale-bot'])
        self.assertEqual(rows[2]['config']['env']['WHALE_BOT_URL'], 'http://127.0.0.1:27500')
        self.assertEqual(rows[2]['config']['env']['PYTHONUTF8'], '1')
        self.assertEqual(rows[2]['config']['transport'], 'stdio')

    def test_remove_is_idempotent_and_keeps_other_entries(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: other\n  name: "@x/y"\n  config: {}\n', encoding='utf-8')
        self.install_all()
        removed = bridge.remove(self.dsh)
        self.assertTrue(removed['removed'])
        self.assertTrue(removed['changed'])
        again = bridge.remove(self.dsh)
        self.assertFalse(again['removed'])
        self.assertFalse(again['changed'])
        import yaml
        data = yaml.safe_load(self.path.read_text(encoding='utf-8'))
        self.assertEqual([item['id'] for item in data], ['other'])
        self.assertFalse(local.status(self.dsh)['bot_registered'])

    def test_write_failure_rolls_back(self):
        self.path.parent.mkdir(parents=True)
        original = '- id: other\n  name: "@x/y"\n  config: {}\n'
        self.path.write_text(original, encoding='utf-8')
        with mock.patch.object(bridge.os, 'replace', side_effect=OSError('磁盘写入失败')):
            with self.assertRaises(OSError):
                self.install_all()
        self.assertEqual(self.path.read_text(encoding='utf-8'), original)
        self.assertEqual(list(self.path.parent.glob('*.writing')), [])
        self.assertTrue(self.backups())

    def test_refuses_non_list_profile(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('not: a list\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.install_all()
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'not: a list\n')

    def test_status_of_empty_home(self):
        status = local.status(self.dsh)
        self.assertFalse(status['patch_exists'])
        self.assertFalse(status['bot_registered'])
        self.assertFalse(status['bot_ready'])
        self.assertEqual(status['bot_tool_count'], 0)
        self.assertTrue(status['bot_problems'])

    def test_status_flags_missing_script_and_missing_utf8(self):
        bridge.install_control(self.dsh, sys.executable, str(self.dsh / 'nope.py'), BOT_URL)
        status = local.status(self.dsh)
        self.assertTrue(status['bot_registered'])
        self.assertFalse(status['bot_script_exists'])
        self.assertFalse(status['bot_ready'])
        self.assertTrue(any('MCP 脚本不存在' in item for item in status['bot_problems']))

    def test_control_entry_env_pins_utf8(self):
        entry = bridge.build_control_entry('python.exe', 'x.py', BOT_URL)
        self.assertEqual(entry['config']['env'], {'WHALE_BOT_URL': BOT_URL, 'PYTHONUTF8': '1'})
        self.assertTrue(local.has_utf8_stdio(str(BOT_SCRIPT)))
        self.assertEqual(local.has_utf8_stdio(str(self.dsh / 'nope.py')), False)


if __name__ == '__main__':
    unittest.main()
