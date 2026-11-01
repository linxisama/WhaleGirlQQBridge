"""sleep_mode（睡眠时段）的离线测试。

注意：这两个插件的测试由 WorkBuddy 先交了一版，但那一版**跑不起来**
（假设模块级 `META`、import 了不存在的 `src.plugins.base`、调了不存在的
`mgr.get()`/`mgr.close()`、调钩子时少传 `runtime`），核心按真实签名重写。

行为口径以源码为准：
- `on_block` 在睡眠窗口内返回 True（拦下），窗口外 False；
- 窗口跨零点（22:00-07:00）与不跨零点（01:00-06:00）都支持，**起点包含、终点不包含**；
- `allowAt` 为真且消息 `at` 为真时，即使睡着也放行。
"""

import pathlib
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from src.plugins import PluginBase
from src.plugins.sleep_mode import SleepMode


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(SleepMode.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def group_msg(at=False):
    return {"id": "m", "kind": "group", "target": "12345", "text": "你好",
            "sender_id": "7", "at": at, "at_ids": ["999"] if at else [], "media": []}


class SleepModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        settings.setdefault("enabled", True)
        return SleepMode(FakeManager(self.dir, settings))

    def blocking(self, plugin, hour, minute=0, message=None):
        """在指定时刻调用 on_block。"""
        with patch("src.plugins.sleep_mode.datetime") as fake:
            fake.now.return_value = datetime(2026, 10, 13, hour, minute)
            return plugin.on_block(message or group_msg(), None)

    # ── 基本开关 ──────────────────────────────────────────────────────────
    def test_disabled_never_blocks(self):
        plugin = self.make(enabled=False, startTime="00:00", endTime="23:59")
        self.assertFalse(self.blocking(plugin, 3))

    def test_during_window_blocks(self):
        plugin = self.make(startTime="00:00", endTime="07:00")
        self.assertTrue(self.blocking(plugin, 3))

    def test_outside_window_allows(self):
        plugin = self.make(startTime="00:00", endTime="07:00")
        self.assertFalse(self.blocking(plugin, 12))

    # ── 边界：起点包含、终点不包含 ────────────────────────────────────────
    def test_window_start_inclusive(self):
        plugin = self.make(startTime="01:00", endTime="06:00")
        self.assertTrue(self.blocking(plugin, 1, 0))

    def test_window_end_exclusive(self):
        plugin = self.make(startTime="01:00", endTime="06:00")
        self.assertFalse(self.blocking(plugin, 6, 0))

    def test_just_before_end_still_blocked(self):
        plugin = self.make(startTime="01:00", endTime="06:00")
        self.assertTrue(self.blocking(plugin, 5, 59))

    # ── 跨零点 ────────────────────────────────────────────────────────────
    def test_cross_midnight_blocks_late_evening(self):
        plugin = self.make(startTime="22:00", endTime="07:00")
        self.assertTrue(self.blocking(plugin, 23))

    def test_cross_midnight_blocks_early_morning(self):
        plugin = self.make(startTime="22:00", endTime="07:00")
        self.assertTrue(self.blocking(plugin, 3))

    def test_cross_midnight_allows_midday(self):
        plugin = self.make(startTime="22:00", endTime="07:00")
        self.assertFalse(self.blocking(plugin, 12))

    def test_cross_midnight_end_exclusive(self):
        plugin = self.make(startTime="22:00", endTime="07:00")
        self.assertFalse(self.blocking(plugin, 7, 0))

    # ── @ 放行 ────────────────────────────────────────────────────────────
    def test_allow_at_lets_mention_through(self):
        plugin = self.make(startTime="00:00", endTime="07:00", allowAt=True)
        self.assertFalse(self.blocking(plugin, 3, message=group_msg(at=True)))

    def test_allow_at_false_still_blocks_mention(self):
        plugin = self.make(startTime="00:00", endTime="07:00", allowAt=False)
        self.assertTrue(self.blocking(plugin, 3, message=group_msg(at=True)))

    # ── 容错 ──────────────────────────────────────────────────────────────
    def test_invalid_times_do_not_raise(self):
        for start, end in (("abc", "07:00"), ("00:00", "xyz"), ("", ""), ("25:99", "07:00")):
            with self.subTest(start=start, end=end):
                plugin = self.make(startTime=start, endTime=end)
                self.assertIsInstance(self.blocking(plugin, 3), bool)

    def test_same_window_never_blocks(self):
        # start == end 会被 _in_window 判为"不睡觉"（非跨零点且区间为空）
        plugin = self.make(startTime="08:00", endTime="08:00")
        self.assertFalse(self.blocking(plugin, 8, 0))

    def test_on_ingest_is_inherited_noop(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest(group_msg(), None))

    # ── 结构与发现 ────────────────────────────────────────────────────────
    def test_discovered_by_plugin_manager(self):
        from src.plugins.manager import PluginManager
        self.assertTrue(issubclass(SleepMode, PluginBase))
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(pathlib.Path(tmp))
            self.assertIn("sleep_mode", manager._plugins)
            self.assertEqual(manager._plugins["sleep_mode"].META.id, "sleep_mode")


if __name__ == "__main__":
    unittest.main()
