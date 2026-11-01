"""锁竞争：控制台"测试连接"不该把回复堵在 ai_lock 后面。

以前 `/api/test-ai` 持 `ai_lock` 调 `test_connection()` → 而它借道 `chat()`（共享客户端），
于是中转站慢的那几秒（最多 30 秒）全部转嫁成"回复排队"。现在探测用一次性临时客户端，
路由不再持锁。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai_engine import AIEngine  # noqa: E402
from src.qq_app import QQRuntime, create_app  # noqa: E402

try:
    from fastapi.testclient import TestClient

    HAS_CLIENT = True
except Exception:
    HAS_CLIENT = False


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n", encoding="utf-8")
    return root


class TestConnectionClientTests(unittest.TestCase):
    """test_connection 必须用自己的临时客户端，绝不碰共享客户端。"""

    def engine(self):
        return AIEngine(api_url="https://example.test/v1", api_key="k", model="m", timeout=5)

    def fake_response(self, *, status=200, payload=None, raise_status=False):
        response = mock.Mock()
        response.status_code = status
        response.json.return_value = payload if payload is not None else {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 1,
                      "prompt_cache_hit_tokens": 4},
        }
        response.text = json.dumps(payload or {})
        if raise_status:
            import httpx
            response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "bad", request=mock.Mock(), response=response)
        else:
            response.raise_for_status.return_value = None
        return response

    def run_test_connection(self, engine, client_factory):
        async def run():
            with mock.patch("src.ai_engine.httpx.AsyncClient", client_factory), \
                 mock.patch.object(engine, "_get_client",
                                   side_effect=AssertionError("不该用共享客户端")):
                return await engine.test_connection()

        return asyncio.run(run())

    def test_uses_a_temporary_client(self):
        engine = self.engine()
        created = {}

        def factory(**kwargs):
            created.update(kwargs)
            client = mock.AsyncMock()
            client.post.return_value = self.fake_response()
            client.__aenter__ = mock.AsyncMock(return_value=client)
            client.__aexit__ = mock.AsyncMock(return_value=False)
            return client

        ok, message = self.run_test_connection(engine, factory)
        self.assertTrue(ok, message)
        self.assertIn("连接成功", message)
        self.assertIn("s）", message, "要带上真实往返时间，方便判断快慢")
        self.assertIn("headers", created, "临时客户端要自带鉴权头")

    def test_reports_http_and_timeout_failures_in_plain_words(self):
        engine = self.engine()

        def factory_status(**kwargs):
            client = mock.AsyncMock()
            client.post.return_value = self.fake_response(status=403, raise_status=True)
            client.__aenter__ = mock.AsyncMock(return_value=client)
            client.__aexit__ = mock.AsyncMock(return_value=False)
            return client

        ok, message = self.run_test_connection(engine, factory_status)
        self.assertFalse(ok)
        self.assertTrue(message, "失败也要有中文原因")

        import httpx

        def factory_timeout(**kwargs):
            client = mock.AsyncMock()
            client.post.side_effect = httpx.TimeoutException("slow")
            client.__aenter__ = mock.AsyncMock(return_value=client)
            client.__aexit__ = mock.AsyncMock(return_value=False)
            return client

        ok, message = self.run_test_connection(engine, factory_timeout)
        self.assertFalse(ok)
        self.assertIn("超时", message)

    def test_usage_is_counted_for_the_probe(self):
        engine = self.engine()

        def factory(**kwargs):
            client = mock.AsyncMock()
            client.post.return_value = self.fake_response()
            client.__aenter__ = mock.AsyncMock(return_value=client)
            client.__aexit__ = mock.AsyncMock(return_value=False)
            return client

        self.run_test_connection(engine, factory)
        self.assertEqual(engine.usage.prompt_tokens, 9)
        self.assertEqual(engine.usage.cached_tokens, 4)


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class TestAiRouteLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "t.sqlite3")
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_route_does_not_acquire_ai_lock(self):
        entered = []

        class SpyLock:
            async def __aenter__(self):
                entered.append("entered")

            async def __aexit__(self, *args):
                return False

        with mock.patch.object(self.bot, "ai_lock", SpyLock()), \
             mock.patch.object(self.bot.ai, "test_connection",
                               new=mock.AsyncMock(return_value=(True, "连接成功（0.42s）: OK"))):
            response = self.client.post("/api/test-ai")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(entered, [], "测试连接不该再占 ai_lock")
        self.assertIn("0.42", response.json()["message"])

    def test_unconfigured_engine_is_refused_without_probing(self):
        with mock.patch.object(self.bot.ai, "test_connection", new=mock.AsyncMock()) as probe:
            self.bot.ai.api_key = ""
            response = self.client.post("/api/test-ai")
        self.assertFalse(response.json()["success"])
        probe.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
