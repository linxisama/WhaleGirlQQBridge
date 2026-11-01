"""Previous host-control cases archived in the code backup after the robot-only scope correction.
Current admin behavior: test_robot_admin.py and test_pc_upload_binding.py.
Generic upload behavior: test_plugin_files.py.
"""
import unittest
from src.pc_backend import WindowsBackend,allowed_apps
class RetiredHostScopeTests(unittest.TestCase):
    def test_legacy_host_entry_cannot_execute_even_if_imported(self):
        with self.assertRaises(RuntimeError):WindowsBackend().execute('screenshot')
        self.assertEqual(allowed_apps(None,'应用 | any.exe'),{})
