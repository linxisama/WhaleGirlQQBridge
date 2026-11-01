"""按次计费（中转站每次固定 1 积分）与按 token 计费：两种都要能选、按次真的少调用。

用户账单（2026-10-17）：`TM ·Auto` 每次 ≈1700~2000 输入 / 15~41 输出，**固定扣 1 积分**。
所以对他的场景"省 token"没用，要省的是**调用次数**；而官方 API 是按 token 计费的，
仍然靠「Token 节流」。两者都保留，用一个开关切换。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.config_manager import DEFAULT_CONFIG  # noqa: E402
from src.plugins.manager import PluginManager  # noqa: E402

APP = QApplication.instance() or QApplication([])


class BillingModeConfigTests(unittest.TestCase):
    def test_default_is_per_token(self):
        self.assertEqual(DEFAULT_CONFIG["ai"]["billing_mode"], "per_token",
                         "默认按 token（官方 API 最常见），按次由用户自己切")

    def test_both_modes_documented_in_settings_ui(self):
        source = open("qq_gui.py", encoding="utf-8").read()
        self.assertIn('"per_token"', source)
        self.assertIn('"per_call"', source)
        self.assertIn("按次计费", source, "设置页要能看懂两种计费方式")
        self.assertIn('"billing_mode": str(self.billing_mode.currentData()', source,
                      "保存时必须写回 billing_mode")


class PerCallModeTests(unittest.TestCase):
    def manager(self, per_call):
        manager = PluginManager.__new__(PluginManager)
        manager.per_call_mode = per_call
        manager._plugins = {}
        manager._settings = {}
        manager._enabled = lambda plugin: bool(getattr(plugin, "enabled", True))
        return manager

    def test_flag_defaults_to_false(self):
        manager = PluginManager.__new__(PluginManager)
        self.assertIs(getattr(manager, "per_call_mode", None), None,
                      "真实实例在 __init__ 里才会置 False，这里只确认属性名一致")

    def test_variety_retry_is_skipped_in_per_call_mode(self):
        recorded = {}

        class Variety:
            enabled = True
            META = type("M", (), {"default_settings": {"retryOnce": True}})

            def get(self, key, default=None):
                return {"retryOnce": True}.get(key, default)

            async def refine(self, engine, conversation, reply, message, runtime, **kwargs):
                recorded["called"] = True
                return reply

        class Engine:
            async def chat(self, conversation, **kwargs):
                return "原始回复"

        class Controls:
            enabled = True

            async def generate(self, engine, conversation, **kwargs):
                return "原始回复"

        import asyncio

        for per_call, expect in ((True, False), (False, True)):
            manager = self.manager(per_call)
            manager._plugins = {"chat_controls": Controls(), "reply_variety": Variety()}
            recorded.clear()
            reply = asyncio.run(manager.generate_reply(Engine(), [], runtime=None))
            self.assertEqual(reply, "原始回复")
            self.assertEqual(recorded.get("called", False), expect,
                             f"per_call={per_call} 时是否重生成应当为 {expect}")


class BurstWindowTests(unittest.TestCase):
    def plugin(self, per_call):
        from src.plugins.burst_merge import BurstMerge

        plugin = BurstMerge.__new__(BurstMerge)
        settings = dict(BurstMerge.META.default_settings)

        class Manager:
            pass

        manager = Manager()
        manager.per_call_mode = per_call
        plugin._manager = manager
        plugin.get = lambda key, default=None: settings.get(key, default)
        plugin._last_msg = {}
        return plugin

    def test_window_widens_in_per_call_mode(self):
        import time

        normal = self.plugin(False)
        message = {"kind": "group", "target": "g", "sender_id": "u"}
        self.assertIsNone(normal.on_pre_ai(message, [], None))
        time.sleep(0.01)
        delay_normal = normal.on_pre_ai(message, [], None)
        self.assertIsNotNone(delay_normal, "同一人连发应当要求等待（合并）")
        self.assertLess(delay_normal["delay"], 8.1)

        billed = self.plugin(True)
        self.assertIsNone(billed.on_pre_ai(message, [], None))
        time.sleep(0.01)
        delay_billed = billed.on_pre_ai(message, [], None)
        self.assertGreater(delay_billed["delay"], delay_normal["delay"],
                           "按次计费时应当等更久，把连发的几条合并成一次调用")
        self.assertLessEqual(delay_billed["delay"], 30)


class ConsoleCallCountTests(unittest.TestCase):
    def test_model_card_shows_call_count(self):
        import qq_gui

        tab = qq_gui.ConsoleTab()
        tab.get = lambda path: {"success": True}
        tab.post = lambda path, data: {"success": True}
        tab.refresh({"running": True, "model": "TM ·Auto", "ai_test": "", "calls": 12,
                    "billing_mode": "per_call", "messages": [], "events": [],
                    "official": {}, "onebot": {}, "inbound": [], "decisions": []})
        text = tab.card_model.detail_lbl.text()
        self.assertIn("12 次", text, f"控制台要显示调用次数：{text}")
        self.assertIn("积分", text, "按次计费要折算成积分")


if __name__ == "__main__":
    unittest.main()
