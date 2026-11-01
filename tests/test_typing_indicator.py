"""typing_indicator（正在输入提示）与 on_ready 框架钩子的离线测试。

这个插件早先是**空实现**（返回 {"typing": True}，被管理器丢弃），所以测试的第一要务
是证明它现在真的会调 set_input_status，而不是又回到"看起来装了、其实没效果"。
"""

import asyncio
import unittest

from src.plugins.manager import PluginManager
from src.plugins.typing_indicator import (
    EVENT_TYPE_SPEAKING,
    EVENT_TYPE_TYPING,
    TypingIndicator,
)


class FakeManager:
    def __init__(self, settings=None):
        self._settings = dict(TypingIndicator.META.default_settings)
        self._settings.update({"enabled": True})
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class FakeOneBot:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    async def call(self, action, params=None):
        self.calls.append((action, params))
        if self.raises:
            raise RuntimeError("protocol does not support set_input_status")
        return {}


class FakeRuntime:
    def __init__(self, mode="onebot", onebot=None):
        self.mode = mode
        self.onebot = onebot if onebot is not None else FakeOneBot()


def private_msg(target="7"):
    return {"id": "m", "kind": "private", "target": target, "text": "在吗",
            "sender": "某人", "sender_id": "7", "at": False}


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **settings):
        return TypingIndicator(FakeManager(settings))

    async def test_private_message_triggers_input_status(self):
        plugin = self.make()
        runtime = FakeRuntime()
        hints = plugin.on_pre_ai(private_msg(), [], runtime)
        self.assertIsNotNone(hints)
        self.assertTrue(callable(hints.get("on_ready")))
        await hints["on_ready"]()
        self.assertEqual(runtime.onebot.calls,
                         [("set_input_status", {"user_id": 7, "event_type": EVENT_TYPE_TYPING})])

    async def test_speaking_event_type_is_used(self):
        plugin = self.make(eventType="speaking")
        runtime = FakeRuntime()
        hints = plugin.on_pre_ai(private_msg(), [], runtime)
        await hints["on_ready"]()
        self.assertEqual(runtime.onebot.calls[0][1]["event_type"], EVENT_TYPE_SPEAKING)

    async def test_disabled_plugin_returns_none(self):
        plugin = TypingIndicator(FakeManager({"enabled": False}))
        self.assertIsNone(plugin.on_pre_ai(private_msg(), [], FakeRuntime()))

    async def test_show_typing_off(self):
        plugin = self.make(showTyping=False)
        self.assertIsNone(plugin.on_pre_ai(private_msg(), [], FakeRuntime()))

    async def test_group_skipped_by_default(self):
        plugin = self.make()
        message = private_msg()
        message["kind"] = "group"
        self.assertIsNone(plugin.on_pre_ai(message, [], FakeRuntime()))

    async def test_group_allowed_when_private_only_off(self):
        plugin = self.make(privateOnly=False)
        message = private_msg()
        message["kind"] = "group"
        self.assertIsNotNone(plugin.on_pre_ai(message, [], FakeRuntime()))

    async def test_official_mode_does_not_call(self):
        plugin = self.make()
        runtime = FakeRuntime(mode="official")
        hints = plugin.on_pre_ai(private_msg(), [], runtime)
        await hints["on_ready"]()
        self.assertEqual(runtime.onebot.calls, [])

    async def test_protocol_error_is_swallowed(self):
        plugin = self.make()
        runtime = FakeRuntime(onebot=FakeOneBot(raises=True))
        hints = plugin.on_pre_ai(private_msg(), [], runtime)
        await hints["on_ready"]()      # 不该抛出去

    async def test_cooldown_suppresses_second_call(self):
        plugin = self.make(cooldownSec=3600)
        runtime = FakeRuntime()
        first = plugin.on_pre_ai(private_msg(), [], runtime)
        second = plugin.on_pre_ai(private_msg(), [], runtime)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_cooldown_is_per_chat(self):
        plugin = self.make(cooldownSec=3600)
        first = plugin.on_pre_ai(private_msg(target="7"), [], FakeRuntime())
        other = plugin.on_pre_ai(private_msg(target="8"), [], FakeRuntime())
        self.assertIsNotNone(first)
        self.assertIsNotNone(other)

    async def test_zero_cooldown_allows_repeat(self):
        plugin = self.make(cooldownSec=0)
        self.assertIsNotNone(plugin.on_pre_ai(private_msg(), [], FakeRuntime()))
        self.assertIsNotNone(plugin.on_pre_ai(private_msg(), [], FakeRuntime()))

    async def test_notice_event_is_ignored(self):
        plugin = self.make()
        message = private_msg()
        message["notice"] = "poke"
        self.assertIsNone(plugin.on_pre_ai(message, [], FakeRuntime()))

    async def test_missing_target_is_ignored(self):
        plugin = self.make()
        message = private_msg()
        message["target"] = ""
        self.assertIsNone(plugin.on_pre_ai(message, [], FakeRuntime()))

    async def test_min_delay_suppresses_rapid_messages(self):
        # 消息来得很密时不发状态；隔得够久就正常发
        plugin = self.make(minDelayMs=2000, cooldownSec=0)
        runtime = FakeRuntime()
        first = plugin.on_pre_ai(private_msg(), [], runtime)
        self.assertIsNotNone(first)                     # 第一条总是发
        self.assertIsNone(plugin.on_pre_ai(private_msg(), [], runtime))   # 紧跟的一条不发
        key = TypingIndicator.chat_key(private_msg())
        plugin._last_message_at[key] -= 10              # 假装隔了 10 秒
        self.assertIsNotNone(plugin.on_pre_ai(private_msg(), [], runtime))

    async def test_min_delay_zero_always_notifies(self):
        plugin = self.make(minDelayMs=0, cooldownSec=0)
        self.assertIsNotNone(plugin.on_pre_ai(private_msg(), [], FakeRuntime()))
        self.assertIsNotNone(plugin.on_pre_ai(private_msg(), [], FakeRuntime()))

    async def test_default_settings_are_disabled(self):
        self.assertFalse(TypingIndicator.META.default_settings["enabled"])

    async def test_description_no_longer_says_placeholder(self):
        # 早先的 description 写着"（预留）"，说明它只是个占位
        self.assertNotIn("预留", TypingIndicator.META.description)


class OnReadyFrameworkTests(unittest.IsolatedAsyncioTestCase):
    """on_ready 是新加的框架口子：管理器负责收集，工作循环负责执行。"""

    def test_manager_collects_on_ready_and_keeps_first(self):
        manager = PluginManager.__new__(PluginManager)
        plugin = TypingIndicator(FakeManager())
        manager._plugins = {"typing_indicator": plugin}
        hints = manager.run_on_pre_ai(private_msg(), [], FakeRuntime())
        self.assertTrue(callable(hints["on_ready"]))
        # 其余字段保持既有语义
        self.assertEqual(hints["delay"], 0.0)
        self.assertFalse(hints["block"])
        self.assertEqual(hints["extra_system"], "")

    def test_manager_result_has_on_ready_key_even_without_plugins(self):
        manager = PluginManager.__new__(PluginManager)
        manager._plugins = {}
        hints = manager.run_on_pre_ai(private_msg(), [], FakeRuntime())
        self.assertIn("on_ready", hints)
        self.assertIsNone(hints["on_ready"])

    def test_plugin_exception_does_not_break_hints(self):
        class Broken(TypingIndicator):
            def on_pre_ai(self, message, conversation, runtime):
                raise RuntimeError("boom")

        manager = PluginManager.__new__(PluginManager)
        manager._plugins = {"typing_indicator": Broken(FakeManager())}
        hints = manager.run_on_pre_ai(private_msg(), [], FakeRuntime())
        self.assertEqual(hints["delay"], 0.0)
        self.assertIsNone(hints["on_ready"])

    def test_non_callable_on_ready_is_ignored(self):
        class Sloppy(TypingIndicator):
            def on_pre_ai(self, message, conversation, runtime):
                return {"on_ready": "not-callable"}

        manager = PluginManager.__new__(PluginManager)
        manager._plugins = {"typing_indicator": Sloppy(FakeManager())}
        hints = manager.run_on_pre_ai(private_msg(), [], FakeRuntime())
        self.assertIsNone(hints["on_ready"])


if __name__ == "__main__":
    unittest.main()
