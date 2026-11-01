"""DSH 记忆桥注册：幂等、可撤销、先备份、坏配置不破坏。"""
import tempfile
import unittest
from pathlib import Path

import yaml

from src import dsh_memory_bridge as bridge


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


class DshBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dsh = Path(self.tmp.name) / '.dsh'
        self.path = bridge.profile_path(self.dsh)

    def tearDown(self):
        self.tmp.cleanup()

    def data(self):
        return yaml.safe_load(self.path.read_text(encoding='utf-8'))

    def test_install_and_status(self):
        result = bridge.install(self.dsh, 'python.exe', 'whale_memory_mcp.py', 'C:/bot')
        self.assertTrue(result['installed'])
        self.assertTrue(self.path.is_file())
        status = bridge.status(self.dsh)
        self.assertTrue(status['installed'])
        self.assertEqual(status['serverName'], 'whale_memory')
        self.assertEqual(status['memory_qq_home'], 'C:/bot')
        text = self.path.read_text(encoding='utf-8')
        self.assertIn('mcp-whale-memory', text)
        self.assertIn('whale_memory_mcp.py', text)

    def test_install_writes_insert_block_not_bare_row(self):
        """新增登记必须是 - insert: 块；裸 - id: 会被 DSH 静默丢弃。"""
        bridge.install(self.dsh, 'python.exe', 'whale_memory_mcp.py', 'C:/bot')
        data = self.data()
        self.assertEqual(len(data), 1)
        block = data[0]
        self.assertIsInstance(block, dict)
        self.assertEqual(list(block), ['insert'])
        self.assertNotIn('id', block)
        self.assertEqual([row['id'] for row in block['insert']], ['mcp-whale-memory'])
        status = bridge.status(self.dsh)
        self.assertEqual(status['insert_blocks'], 1)
        self.assertEqual(status['bare_entry_ids'], [])

    def test_install_is_idempotent_and_keeps_other_entries(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: other\n  name: "@x/y"\n  config: {}\n', encoding='utf-8')
        bridge.install(self.dsh, 'python.exe', 'a.py', 'C:/bot')
        bridge.install(self.dsh, 'python3.exe', 'b.py', 'D:/bot2')
        data = self.data()
        self.assertEqual(entry_ids(data), ['other', 'mcp-whale-memory'])
        self.assertEqual(data[0]['id'], 'other')          # 外来条目还是裸行，没被动过
        entry = rows(data)[1]
        self.assertEqual(entry['config']['command'], 'python3.exe')
        self.assertEqual(entry['config']['args'], ['b.py'])
        self.assertEqual(bridge.status(self.dsh)['insert_blocks'], 1)
        self.assertEqual(bridge.status(self.dsh)['bare_entry_ids'], [])

    def test_legacy_bare_entries_migrate_into_insert_block(self):
        """旧版本写的裸 - id: 行：再登记一次要搬进 insert 块，裸行删掉。"""
        self.path.parent.mkdir(parents=True)
        legacy = ('- id: other\n  name: "@x/y"\n'
                  '- id: mcp-whale-memory\n  name: "@deepseek-ai/dsh-mcp-client"\n'
                  '  config:\n    serverName: whale_memory\n    command: old.exe\n'
                  '- id: mcp-whale-bot\n  name: "@deepseek-ai/dsh-mcp-client"\n')
        self.path.write_text(legacy, encoding='utf-8')
        self.assertEqual(bridge.status(self.dsh)['bare_entry_ids'],
                         ['mcp-whale-memory', 'mcp-whale-bot'])
        result = bridge.install(self.dsh, 'python.exe', 'whale_memory_mcp.py', 'C:/bot')
        self.assertTrue(result['replaced'])            # 旧裸行算「已存在」= 迁移
        data = self.data()
        # 只迁走「这一次登记的那条」，别人的裸行（bot / other）先原样留着
        self.assertEqual([item['id'] for item in data if 'insert' not in item],
                         ['other', 'mcp-whale-bot'])
        self.assertEqual([row['id'] for row in whale_block(data)['insert']], ['mcp-whale-memory'])
        bridge.install_control(self.dsh, 'python.exe', 'whale_bot_mcp.py', 'http://127.0.0.1:27499')
        data = self.data()
        self.assertEqual([item['id'] for item in data if 'insert' not in item], ['other'])
        blocks = [item for item in data if 'insert' in item]
        self.assertEqual(len(blocks), 1)               # 两条搬进同一个块，不各开一个
        self.assertEqual([row['id'] for row in blocks[0]['insert']],
                         ['mcp-whale-memory', 'mcp-whale-bot'])
        self.assertEqual(blocks[0]['insert'][0]['config']['command'], 'python.exe')
        self.assertEqual(bridge.status(self.dsh)['bare_entry_ids'], [])

    def test_control_entry_install_and_status(self):
        result = bridge.install_control(self.dsh, 'python.exe', 'whale_bot_mcp.py', 'http://127.0.0.1:27499')
        self.assertTrue(result['installed'])
        self.assertEqual(result['entry']['config']['serverName'], 'whale_bot')
        status = bridge.status(self.dsh)
        self.assertTrue(status['control_installed'])
        self.assertTrue(status['installed'] is False)  # 记忆桥还没装
        bridge.install(self.dsh, 'python.exe', 'whale_memory_mcp.py', 'C:/bot')
        self.assertTrue(bridge.status(self.dsh)['installed'])
        bridge.remove(self.dsh)
        status = bridge.status(self.dsh)
        self.assertFalse(status['installed'])
        self.assertFalse(status['control_installed'])

    def test_remove_only_removes_bridge(self):
        bridge.install(self.dsh, 'python.exe', 'a.py', 'C:/bot')
        removed = bridge.remove(self.dsh)
        self.assertTrue(removed['removed'])
        self.assertFalse(bridge.status(self.dsh)['installed'])
        self.assertTrue(list(self.path.parent.glob('cordis.patch.yml.bak-whale-memory-*')))

    def test_refuses_non_list_profile(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('not: a list\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            bridge.install(self.dsh, 'python.exe', 'a.py', 'C:/bot')
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'not: a list\n')


    def test_all_entries_carry_long_reconnect_budget(self):
        bridge.install_all(self.dsh, 'python.exe', 'memory.py', 'C:/bot',
                           'control.py', '', tools_script='tools.py')
        status = bridge.status(self.dsh)
        self.assertTrue(status['reconnect_ok'])
        for row in rows(self.data()):
            if row.get('id') in bridge.ENTRY_IDS:
                self.assertEqual(bridge.RECONNECT, row['config']['reconnect'])

    def test_old_entry_without_reconnect_is_not_ready(self):
        bridge.install_all(self.dsh, 'python.exe', 'memory.py', 'C:/bot',
                           'control.py', '', tools_script='tools.py')
        data = self.data()
        for row in rows(data):
            if row.get('id') == bridge.ENTRY_ID:
                row['config'].pop('reconnect', None)
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                             encoding='utf-8')
        self.assertFalse(bridge.status(self.dsh)['reconnect_ok'])


if __name__ == '__main__':
    unittest.main()
