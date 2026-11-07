"""上下文提取：历史媒体描述压缩与整段预算的接线（token 节流第 5 项）。"""
import json
import unittest

from src.chat_state import build_history, turn_text

FP = "fp-1"


def _row(message, reply=""):
    message.setdefault("_persona_key", FP)
    return (json.dumps(message, ensure_ascii=False), reply)


def _msg(**extra):
    base = {"kind": "group", "target": "1", "sender_id": "7", "text": "看看这张"}
    base.update(extra)
    return base


class HistoryMediaBudgetTests(unittest.TestCase):
    def test_long_vision_description_is_compressed_in_history(self):
        message = _msg(vision_description="画面描述" * 400, vision_status="ok")
        stats = {}
        full = build_history([_row(dict(message), "嗯")], FP, max_chars=20000)
        shrunk = build_history([_row(dict(message), "嗯")], FP, max_chars=20000,
                               media_chars=200, stats=stats)
        self.assertEqual(len(full), 2)
        self.assertGreater(len(full[0]["content"]), len(shrunk[0]["content"]))
        self.assertIn("历史媒体描述已压缩", shrunk[0]["content"])
        self.assertGreater(stats.get("_context_saved_chars", 0), 0)

    def test_current_turn_keeps_full_description(self):
        """压缩只作用于历史；本轮要看到完整描述，否则等于没识别。"""
        message = _msg(vision_description="画面描述" * 400, vision_status="ok")
        self.assertIn("画面描述" * 400, turn_text(message))

    def test_short_description_is_left_alone(self):
        message = _msg(vision_description="一只猫", vision_status="ok")
        shrunk = build_history([_row(dict(message), "嗯")], FP, max_chars=20000, media_chars=200)
        self.assertNotIn("历史媒体描述已压缩", shrunk[0]["content"])

    def test_media_budget_none_keeps_original_behaviour(self):
        message = _msg(vision_description="画面描述" * 400, vision_status="ok")
        plain = build_history([_row(dict(message), "嗯")], FP, max_chars=20000)
        self.assertNotIn("历史媒体描述已压缩", plain[0]["content"])

    def test_history_budget_drops_whole_turns(self):
        """预算不够时整轮丢弃，不把一轮切成半截。"""
        rows = [_row(_msg(text="第%d句" % i + "。" * 50), "回%d" % i) for i in range(6)]
        kept = build_history(rows, FP, max_chars=300)
        self.assertTrue(len(kept) < 12, "预算 300 装不下 6 轮")
        for item in kept:
            self.assertIn(item["role"], ("user", "assistant"))
            self.assertTrue(item["content"])


if __name__ == "__main__":
    unittest.main()
