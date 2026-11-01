"""省 token：历史裁剪、单条截断、自适应回复额度。"""
import unittest

from src.ai_engine import AIEngine


def _engine() -> AIEngine:
    return AIEngine('https://example.com/v1', 'k', max_tokens=500,
                    context_budget_tokens=800, history_max_messages=8,
                    adaptive_max_tokens=True)


class TokenSavingTests(unittest.TestCase):
    def test_history_trimmed_under_budget(self):
        engine = _engine()
        messages = [{'role': 'system', 'content': '你是鲸鱼娘'}]
        messages += [{'role': 'user', 'content': '旧消息' * 50} for _ in range(30)]
        messages.append({'role': 'user', 'content': '在吗'})
        trimmed = engine._trim_messages(messages)
        self.assertLess(len(trimmed), len(messages))
        self.assertEqual(trimmed[-1]['content'], '在吗', '最后一条用户消息必须保留')
        self.assertGreater(engine.trimmed_tokens, 0)
        total = sum(engine.estimate_tokens(item['content']) for item in trimmed)
        self.assertLessEqual(total, engine.context_budget_tokens + 200)

    def test_single_long_message_truncated(self):
        engine = AIEngine('https://example.com/v1', 'k', max_tokens=500,
                          context_budget_tokens=100000)
        trimmed = engine._trim_messages([
            {'role': 'user', 'content': '长' * 5000},
            {'role': 'user', 'content': 'hi'}])
        long_content = trimmed[0]['content']
        self.assertLess(len(long_content), 1300)
        self.assertTrue(long_content.endswith('…'))

    def test_adaptive_max_tokens_for_short_question(self):
        engine = _engine()
        payload = engine._build_payload([{'role': 'user', 'content': '在吗'}], '', None)
        self.assertLess(payload['max_tokens'], 200)
        long_payload = engine._build_payload([{'role': 'user', 'content': '问题' * 500}], '', None)
        self.assertGreater(long_payload['max_tokens'], payload['max_tokens'])

    def test_explicit_max_tokens_not_overridden(self):
        engine = _engine()
        payload = engine._build_payload([{'role': 'user', 'content': 'hi'}], '', None,
                                        max_tokens=400)
        self.assertEqual(payload['max_tokens'], 400)


if __name__ == '__main__':
    unittest.main()


class UrlIdempotencyTests(unittest.TestCase):
    """保存过的完整地址再归一化一次，不能越滚越长。"""

    def test_duplicated_endpoint_collapses(self):
        from src.ai_engine import AIEngine
        messy = ('https://workbuddy.tokenmao.cn/v1/messages/chat/completions'
                 '/messages/chat/completions')
        self.assertEqual(AIEngine._normalize_api_url(messy, 'openai'),
                         'https://workbuddy.tokenmao.cn/v1/chat/completions')

    def test_full_and_base_url_agree(self):
        from src.ai_engine import AIEngine
        base = AIEngine._normalize_api_url('https://x.cn/v1', 'openai')
        self.assertEqual(AIEngine._normalize_api_url(base, 'openai'), base)

    def test_anthropic_messages_not_doubled(self):
        from src.ai_engine import AIEngine
        url = 'https://api.anthropic.com/v1/messages'
        self.assertEqual(AIEngine._normalize_api_url(url, 'anthropic'), url)
