"""burst_merge（连发合并）的离线测试。

注意：WorkBuddy 先交的那一版跑不起来（假设模块级 `META`、import 不存在的
`src.plugins.base`、调不存在的 `mgr.get()`/`mgr.close()`），核心按真实签名重写。

这个插件**默认开启**，所以它的行为影响每一条群聊回复。口径以源码为准：
- 只对群聊生效（私聊不需要等"对方发完"）；
- 同一 (会话, 发送者) 在 `capMs` 内再次发言时，返回 `{'delay': 剩余秒}`；
- 首次发言、超出窗口、不同发送者、不同会话都不返回延迟。
"""

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from src.plugins import PluginBase
from src.plugins.burst_merge import BurstMerge


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(BurstMerge.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def group_msg(sender_id="7", target="12345"):
    return {"id": "m", "kind": "group", "target": target, "text": "你好",
            "sender_id": sender_id, "media": []}


class BurstMergeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **settings):
        settings.setdefault("enabled", True)
        return BurstMerge(FakeManager(self.dir, settings))

    def call_at(self, plugin, message, now):
        with patch("src.plugins.burst_merge.time.time", return_value=now):
            return plugin.on_pre_ai(message, [], None)

    def test_first_message_has_no_delay(self):
        plugin = self.make(capMs=8000)
        self.assertIsNone(self.call_at(plugin, group_msg(), 1000.0))

    def test_second_message_within_window_delays(self):
        plugin = self.make(capMs=8000)
        self.call_at(plugin, group_msg(), 1000.0)
        hints = self.call_at(plugin, group_msg(), 1002.0)
        self.assertIsNotNone(hints)
        self.assertAlmostEqual(hints["delay"], 6.0, places=3)

    def test_delay_shrinks_as_time_passes(self):
        plugin = self.make(capMs=8000)
        self.call_at(plugin, group_msg(), 1000.0)
        self.assertAlmostEqual(self.call_at(plugin, group_msg(), 1006.0)["delay"], 2.0, places=3)

    def test_message_after_window_has_no_delay(self):
        plugin = self.make(capMs=8000)
        self.call_at(plugin, group_msg(), 1000.0)
        self.assertIsNone(self.call_at(plugin, group_msg(), 1009.0))

    def test_exactly_at_window_boundary_has_no_delay(self):
        plugin = self.make(capMs=8000)
        self.call_at(plugin, group_msg(), 1000.0)
        # elapsed == cap 时不小于 cap，不延迟
        self.assertIsNone(self.call_at(plugin, group_msg(), 1008.0))

    def test_different_senders_are_independent(self):
        plugin = self.make(capMs=8000)
        self.call_at(plugin, group_msg(sender_id="7"), 1000.0)
        self.assertIsNone(self.call_at(plugin, group_msg(sender_id="8"), 1001.0))

    def test_different_rooms_are_independent(self):
        plugin = self.make(capMs=8000)
        self.call_at(plugin, group_msg(target="111"), 1000.0)
        self.assertIsNone(self.call_at(plugin, group_msg(target="222"), 1001.0))

    def test_private_chat_never_delays(self):
        plugin = self.make(capMs=8000)
        message = group_msg()
        message["kind"] = "private"
        self.call_at(plugin, message, 1000.0)
        self.assertIsNone(self.call_at(plugin, message, 1001.0))

    def test_disabled_never_delays(self):
        plugin = self.make(enabled=False, capMs=8000)
        self.call_at(plugin, group_msg(), 1000.0)
        self.assertIsNone(self.call_at(plugin, group_msg(), 1001.0))

    def test_third_message_measures_from_previous(self):
        # 每次发言都会刷新"上次时间"，所以延迟总是相对上一条算
        plugin = self.make(capMs=8000)
        self.call_at(plugin, group_msg(), 1000.0)
        self.call_at(plugin, group_msg(), 1004.0)
        self.assertAlmostEqual(self.call_at(plugin, group_msg(), 1009.0)["delay"], 3.0, places=3)

    def test_state_does_not_grow_per_message(self):
        plugin = self.make(capMs=8000)
        for index in range(50):
            self.call_at(plugin, group_msg(sender_id=str(index)), float(1000 + index))
        # 每个 (会话, 发送者) 只留一条时间戳
        self.assertEqual(len(plugin._last_msg), 50)

    def test_garbage_message_does_not_raise(self):
        plugin = self.make(capMs=8000)
        with patch("src.plugins.burst_merge.time.time", return_value=1000.0):
            self.assertIsNone(plugin.on_pre_ai({}, [], None))
            self.assertIsNone(plugin.on_pre_ai({"kind": "group"}, [], None))

    def test_discovered_by_plugin_manager(self):
        from src.plugins.manager import PluginManager
        self.assertTrue(issubclass(BurstMerge, PluginBase))
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(pathlib.Path(tmp))
            self.assertIn("burst_merge", manager._plugins)
            self.assertEqual(manager._plugins["burst_merge"].META.id, "burst_merge")

    def test_default_cap_is_documented(self):
        self.assertEqual(BurstMerge.META.default_settings["capMs"], 8000)


if __name__ == "__main__":
    unittest.main()
