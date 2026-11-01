"""内置 MCP 入口的分派与三个工具的 import 自检。"""
import unittest
from unittest import mock

from src import dsh_mcp_entry


class DshMcpEntryTests(unittest.TestCase):
    def test_check_imports_all_three_and_prints_ok(self):
        self.assertEqual(0, dsh_mcp_entry.run(['--mcp', 'check']))

    def test_memory_dispatches_to_memory_main(self):
        with mock.patch('tools.whale_memory_mcp.main', return_value=7) as main:
            self.assertEqual(7, dsh_mcp_entry.run(['--mcp', 'memory']))
        main.assert_called_once_with()

    def test_bot_dispatches_to_bot_main(self):
        with mock.patch('tools.whale_bot_mcp.main', return_value=8) as main:
            self.assertEqual(8, dsh_mcp_entry.run(['--mcp', 'bot']))
        main.assert_called_once_with()

    def test_tools_passes_remaining_arguments(self):
        with mock.patch('tools.whale_tools_mcp.main', return_value=9) as main:
            self.assertEqual(9, dsh_mcp_entry.run(['--mcp', 'tools', '--list']))
        main.assert_called_once_with(['--list'])

    def test_unknown_kind_is_usage_error(self):
        self.assertEqual(2, dsh_mcp_entry.run(['--mcp', 'nope']))
        self.assertEqual(2, dsh_mcp_entry.run([]))


if __name__ == '__main__':
    unittest.main()
