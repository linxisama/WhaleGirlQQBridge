"""Agent 管理员：单一绑定、脱敏、解绑、非法号拒绝。"""
import tempfile
import unittest
from pathlib import Path

from src.agent_admin import AgentAdmin


class AgentAdminTests(unittest.TestCase):
    def test_bind_mask_unbind_and_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'admin.json'
            admin = AgentAdmin(path)
            self.assertFalse(admin.status()['bound'])
            status = admin.bind('10001')
            self.assertTrue(status['bound'])
            self.assertTrue(status['masked'].endswith('01'))
            self.assertNotIn('10001', status['masked'])
            again = AgentAdmin(path)
            self.assertTrue(again.is_admin('10001'))
            again.unbind()
            self.assertFalse(again.is_admin('10001'))
            with self.assertRaises(ValueError):
                again.bind('abc')
            with self.assertRaises(ValueError):
                again.bind('123')

    def test_only_one_admin(self):
        with tempfile.TemporaryDirectory() as folder:
            admin = AgentAdmin(Path(folder) / 'admin.json')
            admin.bind('10001')
            admin.bind('20002')
            self.assertFalse(admin.is_admin('10001'))
            self.assertTrue(admin.is_admin('20002'))


if __name__ == '__main__':
    unittest.main()
