"""空回复、地址一致性、识图失败原因 —— 三处用户实测问题的回归测试。

背景（用户截图，1.6.8）：
1. QQ 里收到"刚才没能生成回复：模型返回了空内容"，控制台运行记录只有这一句，
   看不出为什么 —— 实测成因是推理模型把输出额度花在 reasoning_content 上；
2. 设置页「模型测速」六个模型全报 `HTTP 404：地址路径不对`，而同一页
   「测试连接」却拿到 `HTTP 200 · 10 个模型` —— 因为测速走的是**已保存**的旧地址；
3. 图片识别失败只给一句"请检查视觉模型、图片链接是否过期及 API 状态"，没有真原因。
"""
import asyncio
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx  # noqa: E402

from src.ai_engine import (EMPTY_RETRY_TOKENS, AIEngine, content_text,  # noqa: E402
                           empty_reply_reason, should_retry_empty)


class ContentTextTests(unittest.TestCase):
    """content 的各种形状都要能取到文本（以前 null 会 AttributeError）。"""

    def test_plain_string(self):
        self.assertEqual(content_text("  在的  "), "在的")

    def test_none_and_missing(self):
        self.assertEqual(content_text(None), "")
        self.assertEqual(content_text(""), "")

    def test_block_list(self):
        self.assertEqual(content_text([{"type": "text", "text": "你好"},
                                       {"content": "世界"}]), "你好\n世界")
        self.assertEqual(content_text(["甲", "乙"]), "甲\n乙")

    def test_other_types(self):
        self.assertEqual(content_text(123), "123")


class RetryDecisionTests(unittest.TestCase):
    def test_truncated_output_retries(self):
        self.assertTrue(should_retry_empty({"max_tokens": 500}, "length", ""))

    def test_reasoning_only_retries(self):
        self.assertTrue(should_retry_empty({"max_tokens": 500}, "stop", "我先想想…"))

    def test_plain_empty_does_not_retry(self):
        self.assertFalse(should_retry_empty({"max_tokens": 500}, "stop", ""))

    def test_already_large_budget_does_not_retry(self):
        self.assertFalse(should_retry_empty({"max_tokens": EMPTY_RETRY_TOKENS}, "length", "x"))

    def test_reasons_are_actionable(self):
        self.assertIn("长度上限", empty_reply_reason("length", "", {"max_tokens": 500}))
        self.assertIn("推理模型", empty_reply_reason("stop", "思考", {"max_tokens": 500}))
        self.assertIn("content_filter", empty_reply_reason("content_filter", "", {"max_tokens": 500}))


class ChatEmptyReplyTests(unittest.TestCase):
    """空回复：先加大上限重试一次；仍为空时给出可操作原因。"""

    def engine(self, responses):
        engine = AIEngine(api_url="https://relay.test/v1/chat/completions", api_key="k",
                          model="deepseek-v4-pro", timeout=5)
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content.decode("utf-8")))
            payload = responses[min(len(calls) - 1, len(responses) - 1)]
            return httpx.Response(200, json=payload)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # 引擎按事件循环缓存 client，所以要替换工厂方法，而不是塞 self._client
        patcher = mock.patch.object(AIEngine, '_get_client_for_current_loop', lambda self: client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        engine.calls = calls
        return engine

    def run_chat(self, engine, **kwargs):
        return asyncio.run(engine.chat([{"role": "user", "content": "在吗"}],
                                       system_prompt="你是鲸鱼娘", temperature=0.8, **kwargs))

    def test_truncated_then_success_retries_with_bigger_budget(self):
        engine = self.engine([
            {"choices": [{"message": {"content": "", "reasoning_content": "想了很久"},
                          "finish_reason": "length"}]},
            {"choices": [{"message": {"content": "在的，干嘛"}, "finish_reason": "stop"}]},
        ])
        reply = self.run_chat(engine, max_tokens=120)
        self.assertEqual(reply, "在的，干嘛")
        self.assertEqual(len(engine.calls), 2, "空回复应当只多试一次")
        self.assertEqual(engine.calls[0]["max_tokens"], 120)
        self.assertEqual(engine.calls[1]["max_tokens"], EMPTY_RETRY_TOKENS)
        self.assertEqual(engine.last_error, "")

    def test_reasoning_only_still_empty_reports_the_reason(self):
        engine = self.engine([
            {"choices": [{"message": {"content": None, "reasoning_content": "思考中"},
                          "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
        ])
        reply = self.run_chat(engine, max_tokens=200)
        self.assertEqual(reply, "")
        self.assertIn("推理模型", engine.last_error)
        self.assertEqual(len(engine.calls), 2)

    def test_plain_empty_does_not_retry(self):
        engine = self.engine([{"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}])
        self.assertEqual(self.run_chat(engine), "")
        self.assertEqual(len(engine.calls), 1, "普通空回复不该额外发请求")
        self.assertIn("空内容", engine.last_error)

    def test_content_blocks_are_understood(self):
        engine = self.engine([{"choices": [{"message": {
            "content": [{"type": "text", "text": "在的"}]}, "finish_reason": "stop"}]}])
        self.assertEqual(self.run_chat(engine), "在的")


class SpeedTestTargetTests(unittest.TestCase):
    """测速必须用界面里填的地址（与「测试连接」一致），并回报实际用的地址。"""

    def test_uses_the_given_url_and_reports_it(self):
        engine = AIEngine(api_url="https://old-saved.test/v1/chat/completions", api_key="old",
                          model="m1")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        patcher = mock.patch.object(AIEngine, '_get_client_for_current_loop', lambda self: client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        result = asyncio.run(engine.speed_test(["m1"], api_url="https://relay.test", api_key="new"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen["url"], "https://relay.test/v1/chat/completions",
                         "裸域名应当补成 /v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer new", "测速要用界面上填的密钥")
        self.assertEqual(result["api_url"], "https://relay.test/v1/chat/completions")
        self.assertEqual(engine.api_url, "https://old-saved.test/v1/chat/completions",
                         "测速不能改掉引擎自己的配置")

    def test_missing_key_is_refused_before_any_request(self):
        engine = AIEngine(api_url="https://relay.test/v1/chat/completions", api_key="", model="m1")
        result = asyncio.run(engine.speed_test(["m1"]))
        self.assertFalse(result["ok"])
        self.assertTrue(result["message"])


class ImageUnderstandingFailureTests(unittest.TestCase):
    """识图失败要把真实原因带出来，而不是只说"请检查……"。"""

    def test_failure_note_carries_the_engine_error(self):
        from src.plugins import image_understanding as module
        import inspect
        source = inspect.getsource(module)
        self.assertIn("last_error", source, "识图失败提示应当带上引擎的真实错误")
        self.assertIn("vision_error", source, "异常文本也要带上")
        self.assertIn("模型=", source, "提示里应当写清用的是哪个视觉模型")


if __name__ == "__main__":
    unittest.main()
