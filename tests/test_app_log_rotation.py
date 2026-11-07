"""日志内核：脱敏、重复折叠、总量硬上限（2026-11-07）。

这三件事都不该影响主流程：任何一步出错只能安静降级，不能把日志调用变成异常源。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.app_log import AppLog, MASK, logs_dir, redact


class RedactTests(unittest.TestCase):
    def test_bearer_token_is_masked(self):
        out = redact("Authorization: Bearer abcdefghijklmnop")
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertIn(MASK, out)

    def test_api_key_assignment_is_masked(self):
        for text in ("api_key=sk-live-1234567890", "token: ghp_abcdefghijkl",
                     "password=correct-horse"):
            with self.subTest(text=text):
                out = redact(text)
                self.assertIn(MASK, out)
                self.assertNotIn("sk-live-1234567890", out)

    def test_sk_prefixed_key_is_masked(self):
        out = redact("key is sk-abcdefgh12345678 here")
        self.assertNotIn("abcdefgh12345678", out)

    def test_long_account_number_is_masked(self):
        self.assertNotIn("123456789", redact("群 123456789 退出了"))
        self.assertNotIn("13812345678", redact("手机 13812345678"))

    def test_short_numbers_and_plain_text_survive(self):
        for text in ("端口 8080 已监听", "第 12 行", "耗时 350ms"):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_empty_and_non_string_do_not_raise(self):
        self.assertEqual(redact(""), "")
        self.assertEqual(redact(None), "")
        self.assertIsInstance(redact(12345), str)


class RepeatCoalesceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="app-log-rep-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def _lines(self):
        folder = logs_dir(self.root)
        out = []
        for path in sorted(folder.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out

    def test_identical_entries_collapse_into_one_summary(self):
        log = AppLog(self.root, coalesce_window=60.0)
        for _ in range(5):
            log.info("qq", "同一句重复")
        log.flush_repeats()
        lines = self._lines()
        # 折叠窗口内只留第一条原文，窗口收尾补一条「重复 N 次：原文」的汇总。
        self.assertEqual(len(lines), 2, "5 次刷屏应折成 1 条原文 + 1 条汇总")
        self.assertEqual(lines[0]["message"], "同一句重复")
        self.assertIn("5", lines[1]["message"])
        self.assertIn("同一句重复", lines[1]["message"])

    def test_single_entry_is_not_summarised(self):
        log = AppLog(self.root, coalesce_window=60.0)
        log.info("qq", "只出现一次")
        log.flush_repeats()
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["message"], "只出现一次")

    def test_different_messages_are_not_merged(self):
        log = AppLog(self.root, coalesce_window=60.0)
        log.info("qq", "甲")
        log.info("qq", "乙")
        log.flush_repeats()
        self.assertEqual(len(self._lines()), 2)


class TotalSizeCapTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="app-log-cap-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def _total(self):
        folder = logs_dir(self.root)
        if not folder.exists():
            return 0
        return sum(p.stat().st_size for p in folder.glob("*.jsonl"))

    def test_total_size_stays_under_the_cap(self):
        cap = 64 * 1024
        log = AppLog(self.root, roll_bytes=8 * 1024, roll_keep=2, total_bytes=cap,
                     coalesce_window=0.0)
        for index in range(4000):
            log.info("qq", "填充 %05d %s" % (index, "x" * 80))
        log.flush_repeats()
        self.assertLessEqual(self._total(), cap * 2,
                             "总量硬上限：最多超出一个轮转文件的量")

    def test_roll_keeps_only_the_configured_archives(self):
        log = AppLog(self.root, roll_bytes=4 * 1024, roll_keep=2, total_bytes=1 << 20,
                     coalesce_window=0.0)
        for index in range(1200):
            log.info("qq", "行 %05d %s" % (index, "y" * 60))
        folder = logs_dir(self.root)
        archives = sorted(p.name for p in folder.glob("*.jsonl"))
        self.assertLessEqual(len(archives), 3, "正在写的一份 + roll_keep 份归档")

    def test_unwritable_dir_never_raises(self):
        log = AppLog(self.root, coalesce_window=0.0)
        with mock.patch.object(type(log), "_append", side_effect=OSError("disk full")):
            log.info("qq", "写不进去也不能抛")
            log.error("qq", "错误同样不能抛")
        self.assertGreaterEqual(log.write_failures, 1, "失败要计数，别悄悄吞掉")


if __name__ == "__main__":
    unittest.main()
