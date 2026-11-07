"""入站正文长度上限：单条长消息不能吃掉整个上下文（token 节流）。"""
import unittest

from src.image_input import MAX_MESSAGE_CHARS, message_text


class MessageTextCapTests(unittest.TestCase):
    def test_default_cap_is_applied(self):
        text = "长" * (MAX_MESSAGE_CHARS + 500)
        out = message_text({"text": text})
        self.assertLess(len(out), len(text), "超长正文必须被截断")
        self.assertTrue(out.startswith("长" * 10))
        self.assertIn("正文过长", out)
        self.assertIn("已截断 500 字", out)

    def test_short_text_is_untouched(self):
        self.assertEqual(message_text({"text": "你好"}), "你好")

    def test_exact_limit_is_untouched(self):
        text = "字" * MAX_MESSAGE_CHARS
        self.assertEqual(message_text({"text": text}), text, "刚好到上限不该加省略号")

    def test_limit_zero_disables_capping(self):
        text = "长" * (MAX_MESSAGE_CHARS + 500)
        self.assertEqual(message_text({"text": text}, limit=0), text)

    def test_explicit_limit_is_respected(self):
        out = message_text({"text": "字" * 100}, limit=20)
        self.assertTrue(out.startswith("字" * 20))
        self.assertIn("已截断 80 字", out)

    def test_vision_description_keeps_its_own_cap(self):
        out = message_text({"text": "看图", "vision_description": "画" * 5000,
                            "vision_status": "ok"})
        self.assertIn("图片识别参考", out)
        self.assertLess(len(out), 3500, "描述仍有 3000 字上限，不该被正文上限影响")


if __name__ == "__main__":
    unittest.main()
