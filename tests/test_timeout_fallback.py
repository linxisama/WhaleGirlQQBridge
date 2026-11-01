"""超时兜底的三处修正（真机 2026-10-17）。

用户截图：机器人回复"刚才那条回复超时了，你可以稍后再发一次。"，而且这条兜底提示
还被**引用**进了对方的引用框。库里对应的一行是：

    state=error_replied  error='请求超时 (10s)；已重试一次仍未返回'

三件事：
1. 重试时把单次超时放宽（1.8× 或 +8 秒，上限 120 秒）—— 中转站首包慢时，
   用同样的 10 秒重试几乎必然再超；
2. 兜底提示**不引用**用户那条消息（`_no_quote`）；
3. 超时时在**控制台**给一条可操作提示（建议把「回复超时」调到 25 秒以上）。
"""
import asyncio
import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx  # noqa: E402

from src.ai_engine import AIEngine  # noqa: E402
from src.qq_transports import OfficialQQ  # noqa: E402


class _TimeoutClient:
    """假客户端：每次 post 都抛 ReadTimeout，并记下单次超时。"""

    def __init__(self):
        self.timeouts = []

    async def post(self, url, **kwargs):
        timeout = kwargs.get("timeout")
        read = getattr(timeout, "read", timeout)
        self.timeouts.append(float(read or 0))
        raise httpx.ReadTimeout("slow")

    async def get(self, url, **kwargs):        # 探测类请求需要
        raise httpx.ReadTimeout("slow")

    async def aclose(self):
        return None


class TimeoutEscalationTests(unittest.TestCase):
    def engine(self):
        engine = AIEngine(api_url="https://example.test/v1/chat/completions",
                          api_key="k", model="m")
        engine.http = _TimeoutClient()

        async def use_fake_client():
            return engine.http          # 绕开真实建连，直接给假客户端

        engine._get_client = use_fake_client
        return engine

    def test_retry_uses_a_longer_timeout(self):
        engine = self.engine()
        reply = asyncio.run(engine.chat([{"role": "user", "content": "在吗"}],
                                        timeout=10, retry_on_timeout=True))
        self.assertEqual(reply, "")
        self.assertIn("超时", engine.last_error)
        seen = engine.http.timeouts
        self.assertEqual(len(seen), 2, f"应当重试一次；实际请求 {len(seen)} 次：{seen}")
        self.assertAlmostEqual(seen[0], 10, places=1)
        self.assertGreater(seen[1], seen[0], f"重试的超时要比第一次长：{seen}")
        self.assertAlmostEqual(seen[1], 18, places=1, msg="10 秒 → 1.8 倍 = 18 秒")

    def test_no_retry_when_disabled(self):
        engine = self.engine()
        asyncio.run(engine.chat([{"role": "user", "content": "在吗"}],
                                timeout=10, retry_on_timeout=False))
        self.assertEqual(len(engine.http.timeouts), 1)
        self.assertIn("不再重试", engine.last_error)

    def test_escalation_is_capped(self):
        engine = self.engine()
        asyncio.run(engine.chat([{"role": "user", "content": "在吗"}],
                                timeout=100, retry_on_timeout=True))
        seen = engine.http.timeouts
        self.assertLessEqual(seen[1], 120, f"放宽也要有上限：{seen}")


class FallbackNoQuoteTests(unittest.TestCase):
    def test_fallback_reply_is_sent_without_reference(self):
        transport = OfficialQQ("appid", "secret", sandbox=False)
        transport.token, transport.expires = "T", 10 ** 12
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"id": "ok"})

        transport.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        message = {"kind": "group", "target": "G1", "id": "M1",
                   "_quote_ref": "REFIDX_abc==", "_no_quote": True,
                   "_error_feedback": True}
        asyncio.run(transport.send(message, "刚才那条回复超时了，你可以稍后再发一次。"))
        self.assertNotIn("message_reference", captured["body"],
                         "兜底提示不该引用用户那条消息")


if __name__ == "__main__":
    unittest.main()
