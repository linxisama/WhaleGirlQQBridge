"""第三条登记（工具桥 whale_tools）：insert 形状、幂等、备份、只撤一条/全撤、坏配置不崩。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from src import dsh_memory_bridge as bridge

TIMEOUT_MS = 30000
DSH_OWN_BLOCK = ('- insert:\n'
                 '    - id: code-runtime\n'
                 "      name: '@deepseek-ai/dsh-code-runtime-worker-thread'\n")


def rows(data):
    """展开 insert 块后的登记行（读形状无关，只关心「登记到了没有」）。"""
    out = []
    for item in data or []:
        nested = item.get('insert') if isinstance(item, dict) else None
        if isinstance(nested, list):
            out.extend(row for row in nested if isinstance(row, dict))
        elif isinstance(item, dict):
            out.append(item)
    return out


def entry_ids(data):
    return [row.get('id') for row in rows(data)]


def whale_block(data):
    """承载鲸鱼三条登记的 insert 块（没有则返回 None）。"""
    for item in data or []:
        nested = item.get('insert') if isinstance(item, dict) else None
        if isinstance(nested, list) and any(row.get('id') in bridge.ENTRY_IDS
                                           for row in nested if isinstance(row, dict)):
            return item
    return None


def bare_ids(data):
    return [item['id'] for item in data or [] if isinstance(item, dict) and 'insert' not in item]


class DshToolsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dsh = Path(self.tmp.name) / '.dsh'
        self.path = bridge.profile_path(self.dsh)

    def tearDown(self):
        self.tmp.cleanup()

    def install_tools(self, script='tools/whale_tools_mcp.py', home='C:/bot'):
        return bridge.install_tools(self.dsh, 'python.exe', script, home)

    def install_two(self):
        bridge.install(self.dsh, 'python.exe', 'whale_memory_mcp.py', 'C:/bot')
        bridge.install_control(self.dsh, 'python.exe', 'whale_bot_mcp.py', 'http://127.0.0.1:27499')

    def data(self):
        return yaml.safe_load(self.path.read_text(encoding='utf-8'))

    def backups(self):
        return sorted(self.path.parent.glob('cordis.patch.yml.bak-whale-memory-*'))

    def test_build_tools_entry_shape(self):
        entry = bridge.build_tools_entry('python.exe', 'tools/whale_tools_mcp.py', 'C:/bot')
        self.assertEqual(entry['id'], 'mcp-whale-tools')
        self.assertEqual(entry['name'], '@deepseek-ai/dsh-mcp-client')
        config = entry['config']
        self.assertEqual(config['serverName'], 'whale_tools')
        self.assertEqual(config['transport'], 'stdio')
        self.assertEqual(config['command'], 'python.exe')
        self.assertEqual(config['args'], ['tools/whale_tools_mcp.py'])
        self.assertEqual(config['env'], {'WHALE_QQ_HOME': 'C:/bot', 'PYTHONUTF8': '1'})
        self.assertEqual(config['toolCallTimeoutMs'], TIMEOUT_MS)

    def test_tools_entry_written_as_insert_block(self):
        """新增的登记必须落在 - insert: 下；顶层裸 - id: 会被 DSH 静默丢弃。"""
        self.install_tools()
        data = self.data()
        self.assertEqual(len(data), 1)
        self.assertEqual(list(data[0]), ['insert'])
        self.assertEqual([row['id'] for row in data[0]['insert']], ['mcp-whale-tools'])
        self.assertEqual(bare_ids(data), [])
        status = bridge.status(self.dsh)
        self.assertEqual(status['insert_blocks'], 1)
        self.assertEqual(status['bare_entry_ids'], [])

    def test_install_tools_and_status(self):
        result = self.install_tools()
        self.assertTrue(result['changed'])
        self.assertFalse(result['tools_replaced'])
        self.assertEqual(result['toolsServerName'], 'whale_tools')
        status = bridge.status(self.dsh)
        self.assertTrue(status['tools_installed'])
        self.assertEqual(status['toolsServerName'], 'whale_tools')
        self.assertEqual(status['tools_command'], 'python.exe')
        self.assertEqual(status['tools_script'], 'tools/whale_tools_mcp.py')
        self.assertEqual(status['tools_qq_home'], 'C:/bot')
        self.assertTrue(status['tools_utf8_env'])
        self.assertEqual(status['tools_timeout_ms'], TIMEOUT_MS)
        self.assertFalse(status['installed'])
        self.assertFalse(status['control_installed'])
        self.assertEqual(status['insert_blocks'], 1)
        self.assertEqual(status['bare_entry_ids'], [])

    def test_tools_entry_appended_after_existing_two(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: other\n  name: "@x/y"\n  config: {}\n', encoding='utf-8')
        self.install_two()
        self.install_tools()
        self.assertEqual(entry_ids(self.data()),
                         ['other', 'mcp-whale-memory', 'mcp-whale-bot', 'mcp-whale-tools'])
        blocks = [item for item in self.data() if 'insert' in item]
        self.assertEqual(len(blocks), 1)       # 三条共用一个 insert 块
        self.assertEqual(bare_ids(self.data()), ['other'])

    def test_tools_entry_keeps_other_two_untouched(self):
        self.install_two()
        before = {row['id']: dict(row['config']) for row in rows(self.data())}
        self.install_tools()
        after = {row['id']: dict(row['config']) for row in rows(self.data())}
        self.assertEqual(after['mcp-whale-memory'], before['mcp-whale-memory'])
        self.assertEqual(after['mcp-whale-bot'], before['mcp-whale-bot'])

    def test_install_tools_is_idempotent(self):
        self.install_tools()
        self.install_two()
        again = self.install_tools()
        self.assertFalse(again['changed'])
        self.assertIsNone(again['backup'])
        ids = entry_ids(self.data())
        self.assertEqual(ids.count('mcp-whale-tools'), 1)
        self.assertEqual(len(ids), 3)

    def test_reinstall_twice_keeps_single_insert_block_without_duplicates(self):
        self.install_tools()
        first = self.path.read_text(encoding='utf-8')
        self.install_tools()
        self.install_tools(script='tools/whale_tools_mcp.py')
        data = self.data()
        blocks = [item for item in data if 'insert' in item]
        self.assertEqual(len(blocks), 1)
        self.assertEqual([row['id'] for row in blocks[0]['insert']], ['mcp-whale-tools'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), first)

    def test_dsh_own_insert_block_survives_and_stays_separate(self):
        """DSH 自己的 insert 块不合并、不被卸载误删。"""
        self.path.parent.mkdir(parents=True)
        self.path.write_text(DSH_OWN_BLOCK, encoding='utf-8')
        self.install_tools()
        data = self.data()
        self.assertEqual(len(data), 2)
        self.assertEqual([row['id'] for row in data[0]['insert']], ['code-runtime'])
        self.assertEqual([row['id'] for row in data[1]['insert']], ['mcp-whale-tools'])
        bridge.remove(self.dsh)
        data = self.data()
        self.assertEqual(len(data), 1)
        self.assertEqual([row['id'] for row in data[0]['insert']], ['code-runtime'])

    def test_legacy_bare_tools_entry_migrates_into_insert_block(self):
        """旧版本写下的裸 - id: mcp-whale-tools 要搬进 insert 块。"""
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: other\n  name: "@x/y"\n'
                             '- id: mcp-whale-tools\n  name: "@deepseek-ai/dsh-mcp-client"\n'
                             '  config:\n    serverName: whale_tools\n    command: old.exe\n'
                             '    toolCallTimeoutMs: 1000\n', encoding='utf-8')
        self.assertEqual(bridge.status(self.dsh)['bare_entry_ids'], ['mcp-whale-tools'])
        result = self.install_tools()
        self.assertTrue(result['tools_replaced'])
        data = self.data()
        self.assertEqual(bare_ids(data), ['other'])
        block = whale_block(data)
        self.assertEqual([row['id'] for row in block['insert']], ['mcp-whale-tools'])
        self.assertEqual(block['insert'][0]['config']['command'], 'python.exe')
        self.assertEqual(block['insert'][0]['config']['toolCallTimeoutMs'], TIMEOUT_MS)
        self.assertEqual(bridge.status(self.dsh)['bare_entry_ids'], [])

    def test_unchanged_content_does_not_write_file(self):
        self.install_two()
        first = self.install_tools()
        text = self.path.read_text(encoding='utf-8')
        stamp = self.path.stat().st_mtime_ns
        backup_count = len(self.backups())
        again = self.install_tools()
        self.assertTrue(first['changed'])
        self.assertFalse(again['changed'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), text)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)
        self.assertEqual(len(self.backups()), backup_count)

    def test_changed_tools_entry_backs_up_before_write(self):
        self.install_tools()
        before = self.path.read_text(encoding='utf-8')
        result = self.install_tools(script='tools/whale_tools_mcp_v2.py', home='D:/bot2')
        self.assertTrue(result['changed'])
        self.assertTrue(result['tools_replaced'])
        self.assertTrue(Path(result['backup']).is_file())
        self.assertEqual(self.backups()[-1].read_text(encoding='utf-8'), before)
        config = rows(self.data())[-1]['config']
        self.assertEqual(config['args'], ['tools/whale_tools_mcp_v2.py'])
        self.assertEqual(config['env']['WHALE_QQ_HOME'], 'D:/bot2')

    def test_backup_file_name_prefix(self):
        self.install_tools()
        self.install_tools(script='tools/other.py')
        names = [item.name for item in self.backups()]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith('cordis.patch.yml.bak-whale-memory-'))

    def test_tool_call_timeout_lands_in_config(self):
        self.install_two()
        self.install_tools()
        timeouts = {row['id']: row['config']['toolCallTimeoutMs'] for row in rows(self.data())}
        self.assertEqual(timeouts['mcp-whale-tools'], TIMEOUT_MS)
        self.assertEqual(timeouts['mcp-whale-memory'], 20000)
        self.assertEqual(timeouts['mcp-whale-bot'], 60000)

    def test_remove_only_tools_entry(self):
        self.install_two()
        self.install_tools()
        result = bridge.remove(self.dsh, only='mcp-whale-tools')
        self.assertTrue(result['removed'])
        self.assertTrue(result['changed'])
        self.assertEqual(entry_ids(self.data()), ['mcp-whale-memory', 'mcp-whale-bot'])
        status = bridge.status(self.dsh)
        self.assertFalse(status['tools_installed'])
        self.assertTrue(status['installed'])
        self.assertTrue(status['control_installed'])

    def test_remove_tools_keeps_other_insert_content(self):
        """撤 tools 时块里别人的行和 DSH 自己的块都必须留着。"""
        self.path.parent.mkdir(parents=True)
        self.path.write_text(DSH_OWN_BLOCK, encoding='utf-8')
        self.install_two()
        self.install_tools()
        self.assertEqual(len(self.data()), 2)
        bridge.remove(self.dsh, only=bridge.TOOLS_ENTRY_ID)
        data = self.data()
        self.assertEqual(len(data), 2)
        self.assertEqual([row['id'] for row in data[0]['insert']], ['code-runtime'])
        self.assertEqual([row['id'] for row in data[1]['insert']],
                         ['mcp-whale-memory', 'mcp-whale-bot'])

    def test_remove_all_three_entries(self):
        self.install_two()
        self.install_tools()
        result = bridge.remove(self.dsh)
        self.assertTrue(result['removed'])
        self.assertEqual(self.data(), [])
        status = bridge.status(self.dsh)
        self.assertFalse(status['installed'])
        self.assertFalse(status['control_installed'])
        self.assertFalse(status['tools_installed'])
        again = bridge.remove(self.dsh)
        self.assertFalse(again['removed'])
        self.assertFalse(again['changed'])

    def test_remove_unknown_entry_id_raises(self):
        self.install_tools()
        with self.assertRaises(ValueError):
            bridge.remove(self.dsh, only='mcp-whale-nope')
        self.assertTrue(bridge.status(self.dsh)['tools_installed'])

    def test_bad_yaml_does_not_crash(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('not: a list\n', encoding='utf-8')
        status = bridge.status(self.dsh)
        self.assertFalse(status['tools_installed'])
        self.assertIn('error', status)
        with self.assertRaises(ValueError):
            self.install_tools()
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'not: a list\n')

    def test_install_all_registers_three_when_tools_script_given(self):
        result = bridge.install_all(self.dsh, 'python.exe', 'whale_memory_mcp.py', 'C:/bot',
                                    'whale_bot_mcp.py', 'http://127.0.0.1:27499',
                                    tools_script='tools/whale_tools_mcp.py')
        self.assertTrue(result['tools_installed'])
        self.assertEqual(result['toolsServerName'], 'whale_tools')
        self.assertEqual(entry_ids(self.data()),
                         ['mcp-whale-memory', 'mcp-whale-bot', 'mcp-whale-tools'])
        self.assertEqual(len([item for item in self.data() if 'insert' in item]), 1)
        again = bridge.install_all(self.dsh, 'python.exe', 'whale_memory_mcp.py', 'C:/bot',
                                   'whale_bot_mcp.py', 'http://127.0.0.1:27499',
                                   tools_script='tools/whale_tools_mcp.py')
        self.assertFalse(again['changed'])

    def test_install_all_without_tools_script_keeps_two_entries(self):
        bridge.install_all(self.dsh, 'python.exe', 'whale_memory_mcp.py', 'C:/bot',
                           'whale_bot_mcp.py', 'http://127.0.0.1:27499')
        self.assertEqual(entry_ids(self.data()), ['mcp-whale-memory', 'mcp-whale-bot'])
        self.assertFalse(bridge.status(self.dsh)['tools_installed'])

    def test_write_failure_rolls_back_tools_entry(self):
        self.path.parent.mkdir(parents=True)
        original = '- id: other\n  name: "@x/y"\n  config: {}\n'
        self.path.write_text(original, encoding='utf-8')
        with mock.patch.object(bridge.os, 'replace', side_effect=OSError('磁盘写入失败')):
            with self.assertRaises(OSError):
                self.install_tools()
        self.assertEqual(self.path.read_text(encoding='utf-8'), original)
        self.assertEqual(list(self.path.parent.glob('*.writing')), [])
        self.assertTrue(self.backups())


if __name__ == '__main__':
    unittest.main()
