"""SendGate 单元测试：最小间隔 / 冷却触发·翻倍·封顶·复位 / 非法值回落。

用 FakeClock + monkeypatch 替换 src.qq_send_gate 的 time.monotonic 与
asyncio.sleep，所有用例在虚拟时钟下运行，不真正 sleep 800ms。
"""
import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import src.qq_send_gate as gate_mod
from src.qq_send_gate import SendGate

_REAL_SLEEP = asyncio.sleep


class FakeClock:
    """虚拟单调时钟：monotonic() 返回 now，sleep() 直接推进 now。"""

    def __init__(self, now=0.0):
        self.now = now
        self.slept = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        await _REAL_SLEEP(0)


def patch_clock(clock):
    stack = ExitStack()
    stack.enter_context(patch.object(gate_mod.time, "monotonic", clock.monotonic))
    stack.enter_context(patch.object(gate_mod.asyncio, "sleep", clock.sleep))
    return stack


class SendGateTests(unittest.IsolatedAsyncioTestCase):

    async def test_five_sends_keep_800ms_gap(self):
        clock = FakeClock(0.0)
        gate = SendGate()
        with patch_clock(clock):
            intervals = [await gate.acquire() for _ in range(5)]
        self.assertEqual(intervals[0], 0.0)
        for gap in intervals[1:]:
            self.assertGreaterEqual(gap, 0.8 - 1e-9)
        self.assertAlmostEqual(clock.now, 3.2, places=9)
        self.assertEqual(len(clock.slept), 4)

    async def test_rate_codes_trigger_and_acquire_waits_until_cooldown_end(self):
        for code in (429, 11253, 11254, "429", "11253"):
            with self.subTest(code=code):
                clock = FakeClock(1000.0)
                gate = SendGate()
                with patch_clock(clock):
                    self.assertTrue(await gate.report_code(code))
                    self.assertTrue(gate.in_cooldown())
                    self.assertAlmostEqual(gate.cooldown_left_s(), 30.0, places=6)
                    interval = await gate.acquire()
                    self.assertEqual(clock.now, 1030.0)
                    self.assertEqual(interval, 0.0)  # 首次发送仍返回 0
                    self.assertFalse(gate.in_cooldown())

    async def test_consecutive_trigger_doubles_then_caps(self):
        clock = FakeClock(0.0)
        gate = SendGate()
        with patch_clock(clock):
            self.assertTrue(await gate.report_code(429))
            self.assertEqual(gate.cooldown_count, 1)
            self.assertAlmostEqual(gate.cooldown_left_s(), 30.0, places=6)

            clock.now = 29.0                      # 距上次触发 <300s：翻倍
            self.assertTrue(await gate.report_code(11253))
            self.assertEqual(gate.cooldown_count, 2)
            self.assertAlmostEqual(gate.cooldown_left_s(), 60.0, places=6)

            clock.now = 100.0                     # 29+60=89 已到期，但距触发 71s<300s
            self.assertTrue(await gate.report_code(429))
            self.assertEqual(gate.cooldown_count, 3)
            self.assertAlmostEqual(gate.cooldown_left_s(), 120.0, places=6)

            clock.now = 250.0
            self.assertTrue(await gate.report_code(429))
            self.assertEqual(gate.cooldown_count, 4)
            self.assertAlmostEqual(gate.cooldown_left_s(), 120.0, places=6)

    async def test_normal_codes_never_trigger_and_never_clear_cooldown(self):
        clock = FakeClock(0.0)
        gate = SendGate()
        with patch_clock(clock):
            for code in (0, 200, 11298, "0", "ok", None, True, 404):
                self.assertFalse(await gate.report_code(code))
            self.assertIsNone(gate.cooldown_until)

            self.assertTrue(await gate.report_code(429))
            self.assertEqual(gate.cooldown_count, 1)
            until = gate.cooldown_until
            self.assertFalse(await gate.report_code(0))
            self.assertFalse(await gate.report_code(11298))
            self.assertEqual(gate.cooldown_until, until)
            self.assertEqual(gate.cooldown_count, 1)

    async def test_count_resets_after_300s_without_trigger(self):
        clock = FakeClock(0.0)
        gate = SendGate()
        with patch_clock(clock):
            self.assertTrue(await gate.report_code(429))
            self.assertEqual(gate.cooldown_count, 1)
            clock.now = 301.0
            self.assertTrue(await gate.report_code(429))
            self.assertEqual(gate.cooldown_count, 1)
            self.assertAlmostEqual(gate.cooldown_left_s(), 30.0, places=6)

    async def test_min_gap_zero_means_no_wait(self):
        clock = FakeClock(0.0)
        gate = SendGate(min_gap_ms=0)
        with patch_clock(clock):
            intervals = [await gate.acquire() for _ in range(5)]
        self.assertEqual(intervals, [0.0, 0.0, 0.0, 0.0, 0.0])
        self.assertEqual(clock.now, 0.0)

    def test_invalid_min_gap_falls_back_to_800(self):
        for bad in ("abc", None, -5, -800, ""):
            with self.subTest(bad=bad):
                gate = SendGate(min_gap_ms=bad)
                self.assertEqual(gate.status()["min_gap_ms"], 800.0)

    def test_status_exposes_queryable_fields(self):
        gate = SendGate()
        status = gate.status()
        for key in ("last_send_ts", "cooldown_until", "cooldown_count",
                    "consecutive_hits", "in_cooldown", "cooldown_left_s", "min_gap_ms"):
            self.assertIn(key, status)
        self.assertEqual(status["min_gap_ms"], 800.0)
        self.assertFalse(status["in_cooldown"])


if __name__ == "__main__":
    unittest.main()
