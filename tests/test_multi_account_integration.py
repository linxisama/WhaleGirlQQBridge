"""把多账号并发回归接进测试套件（复用 tools/verify_multi_account.py 的检查项）。"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import verify_multi_account  # noqa: E402


class MultiAccountConcurrencyTests(unittest.TestCase):
    def test_two_accounts_run_concurrently_and_stay_isolated(self):
        code = verify_multi_account.main()
        self.assertEqual(code, 0, '多账号并发回归未通过，见上方 JSON 报告')


if __name__ == '__main__':
    unittest.main()
