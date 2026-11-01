"""历史消息也吃同一份注入预算（系统提示 + 历史总封顶）。"""
import unittest

from src.prompt_budget import trim_history


class TrimHistoryTests(unittest.TestCase):
    def test_removes_oldest_until_budget_fits(self):
        messages = [
            {'role': 'user', 'content': 'a' * 100},
            {'role': 'assistant', 'content': 'b' * 100},
            {'role': 'user', 'content': 'c' * 100},
        ]
        kept, stats = trim_history(messages, 220, protect_last=1)
        self.assertEqual([item['content'] for item in kept], ['b' * 100, 'c' * 100])
        self.assertEqual(stats['removed_messages'], 1)
        self.assertEqual(stats['removed_chars'], 100)
        self.assertEqual(stats['before'], 300)
        self.assertEqual(stats['after'], 200)

    def test_keeps_current_turn_even_when_it_is_over_budget(self):
        messages = [
            {'role': 'user', 'content': '旧' * 50},
            {'role': 'user', 'content': '当前' * 100},
        ]
        kept, stats = trim_history(messages, 10, protect_last=1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['content'], '当前' * 100)
        self.assertEqual(stats['removed_messages'], 1)
        self.assertGreater(stats['after'], stats['limit'])

    def test_zero_budget_keeps_only_protected_tail(self):
        messages = [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}]
        kept, stats = trim_history(messages, 0)
        self.assertEqual(kept, [messages[-1]])
        self.assertEqual(stats['removed_messages'], 1)

    def test_non_string_content_is_counted_by_repr(self):
        messages = [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]},
            {'role': 'user', 'content': 'short'},
        ]
        kept, stats = trim_history(messages, 500)
        self.assertEqual(len(kept), 2)
        self.assertGreater(stats['before'], 0)
        self.assertEqual(stats['removed_messages'], 0)


if __name__ == '__main__':
    unittest.main()
