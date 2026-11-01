"""Regression: market plugins are absent from the installer, yet QQ must receive."""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.gui.smoke import check_packaged_ingest
from src.qq_app import QQRuntime


class PackagedIngestTests(unittest.TestCase):
    def test_bridge_messages_queue_without_optional_plugin_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(Path(__file__).resolve().parents[1] / 'styles', root / 'styles')
            config = root / 'config.qq.yaml'
            config.write_text('qq_only:\n  mode: qqbridge\n', encoding='utf-8')
            bot = QQRuntime(root=root, config_path=config, db_path=root / 'messages.sqlite')
            try:
                with patch.dict(sys.modules, {'src.plugins.poke_counter': None}):
                    result = asyncio.run(check_packaged_ingest(bot))
                self.assertTrue(result['private_queued'])
                self.assertTrue(result['group_queued'])
                self.assertTrue(result['deduplicated'])
                self.assertEqual(result['real_messages_sent'], 0)
            finally:
                asyncio.run(bot.close())
