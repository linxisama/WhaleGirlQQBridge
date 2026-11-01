import tempfile
import unittest
from pathlib import Path

from src.plugins.manager import PluginManager


class PluginAuditPathTests(unittest.TestCase):
    def test_data_root_gets_one_qq_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = PluginManager(root)
            self.assertEqual(manager.plugin_audit_path,
                             root / "qq-only" / "plugin_audit.jsonl")

    def test_plugin_root_does_not_get_a_second_qq_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "qq-only"
            manager = PluginManager(root)
            self.assertEqual(manager.plugin_audit_path,
                             root / "plugin_audit.jsonl")

    def test_qq_only_suffix_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "QQ-ONLY"
            manager = PluginManager(root)
            self.assertEqual(manager.plugin_audit_path,
                             root / "plugin_audit.jsonl")


if __name__ == "__main__":
    unittest.main()
