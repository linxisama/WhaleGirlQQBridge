"""兼容控制台保存的布尔/数字类型必须符合现有插件契约。"""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebCompatTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), '源码侧 JS 验证需要 Node.js')
    def test_browser_schema_and_exit_controls(self):
        result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('web_compat_checks.js'))],
            capture_output=True, timeout=15, creationflags=0x08000000 if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
