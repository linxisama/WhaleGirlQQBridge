"""身份保护 + 控制台状态文案（真机 2026-10-17）。

截图里两处问题：
1. 消息列表状态显示英文 `error_replied`（state_map 缺这个键）；
2. 群里问"你是谁啊"，机器人回 **"我是WorkBuddy。"** —— 底层产品名顶替了人设身份。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.identity_guard import find_leaks, scrub  # noqa: E402

APP = QApplication.instance() or QApplication([])


class IdentityGuardTests(unittest.TestCase):
    def test_foreign_product_name_is_a_leak(self):
        self.assertTrue(find_leaks("我是WorkBuddy。"))
        self.assertTrue(find_leaks("我叫ChatGPT，你满意了吗"))

    def test_ai_self_description_is_a_leak(self):
        self.assertTrue(find_leaks("作为一个AI语言模型，我不能说。"))

    def test_persona_self_intro_is_not_a_leak(self):
        for text in ("我是老鲸，贴吧撕了十年的鲸鱼娘。",
                     "我是鲸鱼娘，不服开帖。",
                     "哟，问这个干嘛？我是老鲸。"):
            self.assertEqual(find_leaks(text), [], f"人设自我介绍不该误伤：{text}")

    def test_scrub_removes_only_the_leaking_sentence(self):
        cleaned, changed = scrub("我是WorkBuddy。今天天气不错。")
        self.assertTrue(changed)
        self.assertNotIn("WorkBuddy", cleaned)
        self.assertIn("今天天气不错", cleaned)

    def test_scrub_uses_persona_fallback_when_nothing_left(self):
        cleaned, changed = scrub("我是WorkBuddy。", fallback="我是老鲸。")
        self.assertTrue(changed)
        self.assertEqual(cleaned, "我是老鲸。")

    def test_clean_reply_is_untouched(self):
        cleaned, changed = scrub("别问了，我就是我。")
        self.assertFalse(changed)
        self.assertEqual(cleaned, "别问了，我就是我。")


class ConsoleStateLabelTests(unittest.TestCase):
    def test_error_replied_is_shown_in_chinese(self):
        import qq_gui

        source = open("qq_gui.py", encoding="utf-8").read()
        self.assertIn('"error_replied": "已回复（超时兜底）"', source,
                      "error_replied 必须有中文文案，否则控制台直接显示英文")
        # 后端写过的状态都要有文案
        for state in ("pending", "sending", "sent", "failed", "uncertain", "expired",
                      "error_replied", "feedback_sending", "paused", "blocked"):
            self.assertIn(f'"{state}"', source, f"{state} 缺少中文映射")


if __name__ == "__main__":
    unittest.main()
