"""T07 预算账本测试：预留、结算、未知结果不释放。"""
import sqlite3
import unittest

from src.audio_billing import AudioBilling
from src.audio_contracts import ContractError, ErrorCode


class BillingTests(unittest.TestCase):
    def make(self, **over):
        options = dict(daily_limit=1.0, monthly_limit=10.0, per_request_limit=0.6,
                       price_per_1k_chars=1.0)
        options.update(over)
        return AudioBilling(**options)

    def test_estimate_by_characters(self):
        billing = AudioBilling(price_per_1k_chars=0.02)
        self.assertAlmostEqual(billing.estimate('x' * 1000), 0.02, places=6)
        self.assertAlmostEqual(billing.estimate(''), 0.00002, places=6)

    def test_per_request_limit_rejects_before_any_call(self):
        billing = self.make(per_request_limit=0.5)
        with self.assertRaises(ContractError) as ctx:
            billing.reserve('a', 0.6)
        self.assertEqual(ctx.exception.code, ErrorCode.BUDGET_EXCEEDED)
        self.assertEqual(billing.snapshot().reserved_today, 0.0)

    def test_daily_limit_counts_reserved(self):
        billing = self.make()
        billing.reserve('a', 0.4)
        billing.reserve('b', 0.4)
        with self.assertRaises(ContractError) as ctx:
            billing.reserve('c', 0.4)
        self.assertEqual(ctx.exception.code, ErrorCode.BUDGET_EXCEEDED)

    def test_monthly_limit_is_enforced(self):
        billing = self.make(daily_limit=100.0, monthly_limit=0.5)
        billing.reserve('a', 0.4)
        billing.settle('a', success=True, actual=0.4)
        with self.assertRaises(ContractError) as ctx:
            billing.reserve('b', 0.4)
        self.assertEqual(ctx.exception.code, ErrorCode.BUDGET_EXCEEDED)

    def test_monthly_limit_counts_reserved_too(self):
        billing = self.make(daily_limit=100.0, monthly_limit=0.5)
        billing.reserve('a', 0.3)
        with self.assertRaises(ContractError) as ctx:
            billing.reserve('b', 0.3)
        self.assertEqual(ctx.exception.code, ErrorCode.BUDGET_EXCEEDED)

    def test_successful_settle_counts_as_spent(self):
        billing = self.make()
        billing.reserve('a', 0.3)
        result = billing.settle('a', success=True, actual=0.25)
        self.assertEqual(result['state'], 'settled')
        snapshot = billing.snapshot()
        self.assertAlmostEqual(snapshot.spent_today, 0.25)
        self.assertAlmostEqual(snapshot.reserved_today, 0.0)

    def test_unsubmitted_failure_releases_budget(self):
        billing = self.make()
        billing.reserve('a', 0.3)
        billing.settle('a', success=False, submitted=False)
        self.compare = billing.snapshot()
        self.assertAlmostEqual(self.compare.reserved_today, 0.0)
        self.assertAlmostEqual(self.compare.remaining_today, 1.0)

    def test_unknown_result_keeps_reservation(self):
        billing = self.make()
        billing.reserve('a', 0.3)
        billing.settle('a', success=False, submitted=True)
        snapshot = billing.snapshot()
        self.assertAlmostEqual(snapshot.unknown_today, 0.3)
        self.assertAlmostEqual(snapshot.remaining_today, 0.7)
        self.assertEqual(billing.state_for('a'), 'unknown')

    def test_retry_allowed_after_release_not_after_unknown(self):
        billing = self.make()
        billing.reserve('a', 0.2)
        billing.settle('a', success=False, submitted=False)
        again = billing.reserve('a', 0.2)
        self.assertFalse(again['duplicate'])
        self.assertTrue(again.get('retried'))
        billing.settle('a', success=False, submitted=True)
        duplicate = billing.reserve('a', 0.2)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['state'], 'unknown')

    def test_duplicate_reserve_does_not_double_count(self):
        billing = self.make()
        billing.reserve('a', 0.3)
        billing.reserve('a', 0.3)
        self.assertAlmostEqual(billing.snapshot().reserved_today, 0.3)

    def test_sqlite_persistence(self):
        db = sqlite3.connect(':memory:')
        billing = AudioBilling(daily_limit=1.0, monthly_limit=5.0, db=db)
        billing.reserve('a', 0.2)
        billing.settle('a', success=True, actual=0.2)
        reopened = AudioBilling(daily_limit=1.0, monthly_limit=5.0, db=db)
        self.assertAlmostEqual(reopened.snapshot().spent_today, 0.2)
        self.assertEqual(len(reopened.entries()), 1)


if __name__ == '__main__':
    unittest.main()
