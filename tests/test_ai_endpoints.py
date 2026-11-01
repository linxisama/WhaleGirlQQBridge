"""接口地址解析 / 模型列表 / 连接性测试的离线回归测试。

背景（真实日志）：
- 用户把服务商的**显示名**（"NVIDIA: Nemotron 3 Ultra"、"Kimi K2.7 Code"）填进模型名，
  上游一律 400/404；
- 控制台只会报 "HTTP 403：接口权限不足"，用户不知道是密钥分组没有这个模型；
- baseurl 写法五花八门（裸域名、/v1、/compatible-mode/v1、/api/paas/v4、one-api 的
  http://127.0.0.1:3000 ...），补全规则一旦写死就会重复追加版本段。

这里用 unittest.mock 假掉 httpx：全部离线，不碰真实网络，也不允许任何密钥出现在
返回字符串里。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.ai_engine as ai_engine  # noqa: E402
from src.ai_engine import AIEngine  # noqa: E402

KEY = "sk-test-1234567890abcdefghij"


def response(status: int = 200, payload=None, text: str | None = None, method: str = "GET") -> httpx.Response:
    """造一个真的 httpx.Response（raise_for_status / json / text 都会照常工作）。"""
    request = httpx.Request(method, "https://upstream.test/probe")
    if payload is not None:
        return httpx.Response(status, json=payload, request=request)
    return httpx.Response(status, text=text if text is not None else "", request=request)


def fake_async_client(get_result=None, post_result=None):
    """假的 httpx.AsyncClient 工厂：支持 async with，记录构造参数和每次请求。"""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def do_get(url, **kwargs):
        if isinstance(get_result, Exception):
            raise get_result
        return get_result

    async def do_post(url, **kwargs):
        if isinstance(post_result, Exception):
            raise post_result
        return post_result

    client.get = AsyncMock(side_effect=do_get)
    client.post = AsyncMock(side_effect=do_post)
    factory = MagicMock(return_value=client)
    return factory, client


class UrlShapesTest(unittest.TestCase):
    """(1) 用户可能填的各种 baseurl 写法，都要落到正确的对话接口地址。"""

    SHAPES = [
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.moonshot.cn/v1", "https://api.moonshot.cn/v1/chat/completions"),
        ("https://api.moonshot.cn/v1/", "https://api.moonshot.cn/v1/chat/completions"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/chat/completions"),
        ("https://openrouter.ai/api/v1/chat/completions",
         "https://openrouter.ai/api/v1/chat/completions"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1",
         "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
        ("https://open.bigmodel.cn/api/paas/v4",
         "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
        ("https://ark.cn-beijing.volces.com/api/v3",
         "https://ark.cn-beijing.volces.com/api/v3/chat/completions"),
        ("http://127.0.0.1:3000", "http://127.0.0.1:3000/v1/chat/completions"),
        ("https://host/chat/completions", "https://host/chat/completions"),
        ("https://host/completions", "https://host/completions"),
    ]

    ANTHROPIC_SHAPES = [
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.moonshot.cn/anthropic", "https://api.moonshot.cn/anthropic/v1/messages"),
        ("https://host/v1", "https://host/v1/messages"),
        ("https://host/v1/messages", "https://host/v1/messages"),
    ]

    def test_openai_shapes(self):
        for raw, expected in self.SHAPES:
            with self.subTest(url=raw):
                self.assertEqual(AIEngine._normalize_api_url(raw, "openai"), expected)

    def test_anthropic_shapes(self):
        for raw, expected in self.ANTHROPIC_SHAPES:
            with self.subTest(url=raw):
                self.assertEqual(AIEngine._normalize_api_url(raw, "anthropic"), expected)

    def test_anthropic_style_is_auto_detected(self):
        for raw in ("https://api.anthropic.com", "https://host/v1/messages",
                    "https://api.moonshot.cn/anthropic", "https://host/anthropic/v1"):
            with self.subTest(url=raw):
                self.assertEqual(AIEngine._detect_api_style(raw), "anthropic")
                self.assertIn("/messages", AIEngine._normalize_api_url(
                    raw, AIEngine._detect_api_style(raw)))

    def test_whitespace_query_fragment_and_slashes(self):
        self.assertEqual(AIEngine._normalize_api_url("  https://api.deepseek.com/v1/?a=1#b  "),
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(AIEngine._normalize_api_url("https://api.deepseek.com///"),
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(AIEngine._normalize_api_url("   "), "")

    def test_never_double_appends_and_keeps_version_suffix(self):
        for raw in [raw for raw, _ in self.SHAPES] + [raw for raw, _ in self.ANTHROPIC_SHAPES]:
            style = AIEngine._detect_api_style(raw)
            once = AIEngine._normalize_api_url(raw, style)
            self.assertEqual(AIEngine._normalize_api_url(once, style), once, raw)
            suffix = "/messages" if style == "anthropic" else "/chat/completions"
            # /completions 或 /chat/completions 这种"用户已经写到末段"的地址本来就该原样返回
            already = (raw.rstrip("/").lower().endswith("/completions")
                       and not raw.rstrip("/").lower().endswith("/chat/completions"))
            self.assertEqual(once.lower().count(suffix), 0 if already else 1, f"{raw} -> {once}")
            # 版本段只允许出现一次，且必须和用户填的一致
            for segment in ("/compatible-mode/v1", "/api/paas/v4", "/api/v3", "/api/v1", "/v1"):
                self.assertLessEqual(once.count(segment), 1, f"{raw} -> {once}")

    def test_engine_populates_full_endpoint(self):
        engine = AIEngine("https://api.moonshot.cn/v1", KEY, model="kimi-k2")
        self.assertEqual(engine.api_url, "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(engine.api_style, "openai")

    def test_suggested_base_url_only_for_bare_hosts(self):
        cases = {
            "https://api.deepseek.com": "https://api.deepseek.com/v1",
            "openrouter.ai": "https://openrouter.ai/api/v1",         # 没写协议也认得出来
            "https://openrouter.ai": "https://openrouter.ai/api/v1",
            "https://open.bigmodel.cn": "https://open.bigmodel.cn/api/paas/v4",
            "http://127.0.0.1:3000": "http://127.0.0.1:3000/v1",      # one-api / new-api
            "https://some.proxy": "https://some.proxy/v1",
            "https://api.deepseek.com/v1": "",                        # 已经填了路径就不再提示
            "https://host/api/paas/v4/": "",
            "": "",
        }
        for raw, expected in cases.items():
            with self.subTest(url=raw):
                self.assertEqual(AIEngine.suggested_base_url(raw), expected)


class ModelsUrlTest(unittest.TestCase):
    """(2) 由对话接口推导模型列表地址。"""

    def test_openai_models_url(self):
        self.assertEqual(AIEngine._models_url("https://api.deepseek.com/v1/chat/completions", "openai"),
                         "https://api.deepseek.com/v1/models")
        self.assertEqual(AIEngine._models_url("https://host/v1/completions", "openai"),
                         "https://host/v1/models")
        self.assertEqual(AIEngine._models_url("https://openrouter.ai/api/v1", "openai"),
                         "https://openrouter.ai/api/v1/models")

    def test_anthropic_models_url(self):
        self.assertEqual(AIEngine._models_url("https://api.anthropic.com/v1/messages", "anthropic"),
                         "https://api.anthropic.com/v1/models")
        self.assertEqual(AIEngine._models_url("https://api.moonshot.cn/anthropic", "anthropic"),
                         "https://api.moonshot.cn/anthropic/v1/models")


class ListModelsTest(unittest.IsolatedAsyncioTestCase):
    """(2) list_models：解析三种返回形状 + 错误映射，任何情况都不抛异常。"""

    async def list_with(self, get_result):
        engine = AIEngine("https://api.moonshot.cn/v1", KEY, model="kimi-k2")
        factory, client = fake_async_client(get_result=get_result)
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.list_models()
        return result, factory, client

    async def test_openai_shape_is_sorted_and_counted(self):
        result, factory, _ = await self.list_with(
            response(200, {"object": "list", "data": [{"id": "z-model"}, {"id": "a-model"}]}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], ["a-model", "z-model"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["url"], "https://api.moonshot.cn/v1/models")
        self.assertIn("2", result["message"])
        self.assertEqual(factory.call_args.kwargs["headers"]["Authorization"], f"Bearer {KEY}")

    async def test_anthropic_shape_uses_x_api_key(self):
        engine = AIEngine("https://api.anthropic.com", KEY, model="claude-3-5-sonnet",
                          api_style="anthropic")
        factory, _ = fake_async_client(get_result=response(200, {"data": [{"id": "claude-3-5-sonnet"}]}))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.list_models()
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://api.anthropic.com/v1/models")
        headers = factory.call_args.kwargs["headers"]
        self.assertEqual(headers["x-api-key"], KEY)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("Authorization", headers)

    async def test_bare_list_and_models_key_are_tolerated(self):
        result, _, _ = await self.list_with(response(200, ["m1", {"id": "m2"}, {"name": "m3"}, {}]))
        self.assertEqual(result["models"], ["m1", "m2", "m3"])
        result, _, _ = await self.list_with(response(200, {"models": [{"id": "x"}]}))
        self.assertEqual(result["models"], ["x"])

    async def test_is_capped_at_500_ids_and_deduplicated(self):
        payload = {"data": [{"id": f"m{i:04d}"} for i in range(600)] + [{"id": "m0000"}]}
        result, _, _ = await self.list_with(response(200, payload))
        self.assertEqual(result["count"], 500)
        self.assertEqual(len(set(result["models"])), 500)

    async def test_error_mapping_mentions_what_to_do(self):
        cases = {
            400: "请求参数",
            401: "密钥无效",
            403: "权限",
            404: "地址路径不对",
            429: "限流",
            500: "服务商故障",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                result, _, _ = await self.list_with(
                    response(status, {"error": {"message": "upstream says no"}}))
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertIn(expected, result["message"])
                self.assertIn("upstream says no", result["message"])
                self.assertEqual(result["models"], [])

    async def test_timeout_and_network_error_and_non_json(self):
        result, _, _ = await self.list_with(httpx.TimeoutException("slow"))
        self.assertIn("超时", result["message"])
        self.assertFalse(result["ok"])

        result, _, _ = await self.list_with(httpx.ConnectError("no route"))
        self.assertIn("网络连接失败", result["message"])

        result, _, _ = await self.list_with(response(200, text="<html>welcome to our site</html>"))
        self.assertFalse(result["ok"])
        self.assertIn("不是 JSON", result["message"])

    async def test_blank_key_or_url_returns_hint_without_any_request(self):
        engine = AIEngine("https://api.moonshot.cn/v1", "")
        factory, _ = fake_async_client(get_result=response(200, {"data": []}))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.list_models()
        self.assertFalse(result["ok"])
        self.assertIn("密钥", result["message"])

        engine = AIEngine("", "")
        factory, _ = fake_async_client(get_result=response(200, {"data": []}))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.list_models()
        self.assertIn("API 地址", result["message"])
        factory.assert_not_called()

    async def test_bare_host_comes_back_with_a_suggested_base_url(self):
        engine = AIEngine("https://api.deepseek.com", KEY, model="deepseek-chat")
        factory, _ = fake_async_client(get_result=response(404, {"error": {"message": "not found"}}))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.list_models(api_url="https://api.deepseek.com")
        self.assertEqual(result["suggested_base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(result["url"], "https://api.deepseek.com/v1/models")
        # 已经带路径的地址不再提示
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.list_models(api_url="https://api.deepseek.com/v1")
        self.assertEqual(result["suggested_base_url"], "")

    async def test_api_key_never_appears_in_result(self):
        leaking = response(403, {"error": {"message": f"invalid api key {KEY} for this group"}})
        result, _, _ = await self.list_with(leaking)
        self.assertNotIn(KEY, json.dumps(result, ensure_ascii=False))
        self.assertIn("sk-***", result["detail"])


class DiagnoseTest(unittest.IsolatedAsyncioTestCase):
    """(3) diagnose：形状、中文结论、模型不在列表时的明确提示。"""

    async def diagnose_with(self, get_result, post_result, **kwargs):
        engine = AIEngine("https://api.moonshot.cn/v1", KEY, model="kimi-k2")
        factory, client = fake_async_client(get_result=get_result, post_result=post_result)
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.diagnose(**kwargs)
        return result, factory, client

    async def test_success_shape_and_endpoints(self):
        result, factory, client = await self.diagnose_with(
            response(200, {"data": [{"id": "kimi-k2"}, {"id": "other"}]}),
            response(200, {"choices": [{"message": {"content": "OK"}}]}, method="POST"),
            model="kimi-k2",
        )
        for field in ("ok", "url", "models_url", "api_style", "style_source", "model", "model_listed",
                      "models_ok", "models_status", "models_count", "models_sample", "chat_ok",
                      "chat_status", "latency_ms", "reply", "message", "problem"):
            self.assertIn(field, result)
        self.assertTrue(result["ok"])
        self.assertTrue(result["chat_ok"])
        self.assertTrue(result["model_listed"])
        self.assertEqual(result["problem"], "")
        self.assertEqual(result["url"], "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(result["models_url"], "https://api.moonshot.cn/v1/models")
        self.assertEqual(result["api_style"], "openai")
        self.assertEqual(result["style_source"], "auto")
        self.assertEqual(result["models_sample"], ["kimi-k2", "other"])
        self.assertEqual(result["chat_status"], 200)
        self.assertEqual(result["reply"], "OK")
        self.assertIsInstance(result["latency_ms"], int)
        # 模型列表走 GET /models，真实调用走 POST 到补全后的对话地址
        self.assertEqual(client.get.call_args.args[0], "https://api.moonshot.cn/v1/models")
        self.assertEqual(client.post.call_args.args[0], "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(client.post.call_args.kwargs["json"]["max_tokens"], 8)
        self.assertEqual(client.post.call_args.kwargs["json"]["model"], "kimi-k2")

    async def test_model_not_listed_says_it_explicitly(self):
        result, _, _ = await self.diagnose_with(
            response(200, {"data": [{"id": "good-model"}]}),
            response(404, {"error": {"message": "model not found"}}, method="POST"),
            model="Kimi K2.7 Code",
        )
        self.assertFalse(result["ok"])
        self.assertIs(result["model_listed"], False)
        self.assertIn("HTTP 404", result["problem"])
        self.assertIn("地址路径不对", result["problem"])
        self.assertIn("该模型不在密钥可用的模型列表里，这就是 403/404 的常见原因，请从列表中选择。",
                      result["problem"])
        self.assertEqual(result["message"], result["problem"])

    async def test_forbidden_model_list_leaves_model_listed_unknown(self):
        # 列表接口本身被 403 拒绝时不能瞎猜"模型不在列表里"，只能说清权限问题
        result, _, _ = await self.diagnose_with(
            response(403, {"error": {"message": "no permission for this group"}}),
            response(403, {"error": {"message": "no permission"}}, method="POST"),
            model="Kimi K2.7 Code",
        )
        self.assertIsNone(result["model_listed"])
        self.assertIn("HTTP 403", result["problem"])
        self.assertIn("权限", result["problem"])
        self.assertIn("换 key", result["problem"])

    async def test_model_listed_false_with_working_chat_still_warns(self):
        result, _, _ = await self.diagnose_with(
            response(200, {"data": [{"id": "a"}]}),
            response(200, {"choices": [{"message": {"content": "hello"}}]}, method="POST"),
            model="not-in-list",
        )
        self.assertTrue(result["ok"])
        self.assertIs(result["model_listed"], False)
        self.assertIn("该模型不在密钥可用的模型列表里", result["problem"])

    async def test_timeout_and_non_json_chat_are_reported(self):
        result, _, _ = await self.diagnose_with(
            response(200, {"data": [{"id": "m"}]}),
            httpx.TimeoutException("slow"),
            model="m",
        )
        self.assertFalse(result["ok"])
        self.assertIn("超时", result["problem"])
        self.assertIsNone(result["latency_ms"])

        result, _, _ = await self.diagnose_with(
            response(200, {"data": [{"id": "m"}]}),
            response(200, text="<html>not an api</html>", method="POST"),
            model="m",
        )
        self.assertFalse(result["ok"])
        self.assertIn("不是 JSON", result["problem"])

    async def test_blank_config_and_blank_model_are_explained(self):
        engine = AIEngine("", "")
        result = await engine.diagnose(api_url="", api_key="", model="m")
        self.assertIn("地址", result["problem"])

        engine = AIEngine("https://api.moonshot.cn/v1", KEY, model="")
        factory, _ = fake_async_client(get_result=response(200, {"data": [{"id": "m"}]}))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.diagnose(api_url="https://api.moonshot.cn/v1", model="")
        self.assertFalse(result["ok"])
        self.assertIn("模型名称", result["problem"])

    async def test_configured_model_is_used_when_none_given(self):
        engine = AIEngine("https://api.moonshot.cn/v1", KEY, model="kimi-k2")
        factory, client = fake_async_client(
            get_result=response(200, {"data": [{"id": "kimi-k2"}]}),
            post_result=response(200, {"choices": [{"message": {"content": "OK"}}]}, method="POST"))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.diagnose()
        self.assertEqual(result["model"], "kimi-k2")
        self.assertEqual(client.post.call_args.kwargs["json"]["model"], "kimi-k2")

    async def test_no_api_key_in_any_string(self):
        result, _, _ = await self.diagnose_with(
            response(401, {"error": {"message": f"bad key {KEY}"}}),
            response(401, {"error": {"message": f"bad key {KEY}"}}, method="POST"),
            model="m",
        )
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(KEY, dumped)
        self.assertIn("sk-***", dumped)
        self.assertIn("密钥无效", result["problem"])


class ChatRetryTest(unittest.IsolatedAsyncioTestCase):
    """(3) chat() 对 401/403/404 只尝试一次；限流/服务端故障才重试。"""

    def engine_with(self, status: int, max_retries: int = 3, model: str = "Kimi K2.7 Code"):
        engine = AIEngine("https://api.moonshot.cn/v1", KEY, model=model,
                          max_retries=max_retries)
        payload = {"error": {"message": f"upstream rejected key {KEY}"}}
        post = AsyncMock(return_value=response(status, payload, method="POST"))
        # chat() 只用到 client.post，给一个最小替身即可
        engine._get_client = AsyncMock(return_value=SimpleNamespace(post=post))
        return engine, post

    async def test_401_and_403_and_404_are_not_retried(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                engine, post = self.engine_with(status)
                with patch.object(ai_engine.asyncio, "sleep", AsyncMock()) as sleeper:
                    reply = await engine.chat([{"role": "user", "content": "hi"}])
                self.assertEqual(reply, "")
                self.assertEqual(post.await_count, 1)
                self.assertFalse(sleeper.await_count)
                self.assertIn(f"HTTP {status}", engine.last_error)
                self.assertNotIn(KEY, engine.last_error)
                await engine.close()

    async def test_403_tells_the_user_what_to_change(self):
        engine, _ = self.engine_with(403)
        await engine.chat([{"role": "user", "content": "hi"}])
        self.assertIn("权限", engine.last_error)
        self.assertIn("换 key", engine.last_error)
        self.assertIn("upstream rejected key sk-***", engine.last_error)

    async def test_403_last_error_suggests_the_real_id(self):
        engine, _ = self.engine_with(403, model="TM DeepSeek V4.1")
        engine._known_models = ["TM ·DeepSeek-V4.1", "gpt-4o"]
        await engine.chat([{"role": "user", "content": "hi"}])
        self.assertIn("HTTP 403", engine.last_error)
        self.assertIn("模型名称不在可用列表里，最接近的是：TM ·DeepSeek-V4.1", engine.last_error)
        self.assertNotIn(KEY, engine.last_error)
        await engine.close()

    async def test_403_without_a_known_list_keeps_the_plain_message(self):
        engine, _ = self.engine_with(403, model="TM DeepSeek V4.1")
        await engine.chat([{"role": "user", "content": "hi"}])
        self.assertNotIn("最接近的是", engine.last_error)
        await engine.close()

    async def test_429_and_5xx_are_retried(self):
        for status in (429, 503):
            with self.subTest(status=status):
                engine, post = self.engine_with(status, max_retries=3)
                with patch.object(ai_engine.asyncio, "sleep", AsyncMock()) as sleeper:
                    reply = await engine.chat([{"role": "user", "content": "hi"}])
                self.assertEqual(reply, "")
                self.assertEqual(post.await_count, 3)
                self.assertEqual(sleeper.await_count, 2)
                self.assertIn(f"HTTP {status}", engine.last_error)
                await engine.close()


class ModelSuggestionTest(unittest.TestCase):
    """模型名填成显示名（"TM DeepSeek V4.1" vs "TM ·DeepSeek-V4.1"）时要能给出真实 id。"""

    AVAILABLE = ["TM ·DeepSeek-V4.1", "kimi-k2-0905-preview", "gpt-4o"]

    def test_exact_id_needs_no_suggestion(self):
        self.assertEqual(AIEngine._suggest_models("gpt-4o", self.AVAILABLE), ["gpt-4o"])

    def test_display_name_normalizes_to_the_real_id(self):
        # 大小写、空格和 ·/-/_ 都被归一化掉，直接命中真实 id
        self.assertEqual(AIEngine._suggest_models("TM DeepSeek V4.1", self.AVAILABLE),
                         ["TM ·DeepSeek-V4.1"])
        self.assertEqual(AIEngine._suggest_models("kimi k2.7 code", ["kimi-k2.7-code"]),
                         ["kimi-k2.7-code"])

    def test_near_miss_uses_fuzzy_match(self):
        self.assertEqual(AIEngine._suggest_models("TM DeepSeek V4.2", self.AVAILABLE),
                         ["TM ·DeepSeek-V4.1"])

    def test_suggestions_are_capped_and_empty_when_nothing_is_close(self):
        self.assertLessEqual(len(AIEngine._suggest_models("deepseek", [f"deepseek-v{i}" for i in range(9)])), 3)
        self.assertEqual(AIEngine._suggest_models("NVIDIA: Nemotron 3 Ultra", ["gpt-4o"]), [])
        self.assertEqual(AIEngine._suggest_models("", self.AVAILABLE), [])
        self.assertEqual(AIEngine._suggest_models("gpt-4o", []), [])

    def test_normalize_drops_punctuation_and_case(self):
        self.assertEqual(AIEngine._normalize_model_id("TM ·DeepSeek-V4.1"), "tmdeepseekv41")
        self.assertEqual(AIEngine._normalize_model_id("  Kimi_K2.7/Code "), "kimik27code")


class DiagnoseSuggestionTest(unittest.IsolatedAsyncioTestCase):
    """diagnose 把建议写进 problem 和 suggested_models。"""

    async def diagnose_with(self, model, models):
        engine = AIEngine("https://workbuddy.tokenmao.cn/v1", KEY, model=model)
        factory, _ = fake_async_client(
            get_result=response(200, {"data": [{"id": m} for m in models]}),
            post_result=response(403, {"error": {"message": "无权使用该模型"}}, method="POST"))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            return await engine.diagnose(api_url="https://workbuddy.tokenmao.cn/v1", model=model)

    async def test_display_name_gets_the_real_id(self):
        result = await self.diagnose_with("TM DeepSeek V4.1", ["TM ·DeepSeek-V4.1"])
        self.assertIs(result["model_listed"], False)
        self.assertEqual(result["suggested_models"], ["TM ·DeepSeek-V4.1"])
        self.assertIn("模型名称不在可用列表里，最接近的是：TM ·DeepSeek-V4.1", result["problem"])
        self.assertIn("HTTP 403", result["problem"])
        self.assertIn("请从列表中选择", result["problem"])

    async def test_near_miss_is_suggested(self):
        result = await self.diagnose_with("TM DeepSeek V4.2", ["TM ·DeepSeek-V4.1"])
        self.assertEqual(result["suggested_models"], ["TM ·DeepSeek-V4.1"])
        self.assertIn("最接近的是：TM ·DeepSeek-V4.1", result["problem"])

    async def test_no_close_match_keeps_the_generic_sentence(self):
        result = await self.diagnose_with("NVIDIA: Nemotron 3 Ultra", ["gpt-4o", "gpt-4o-mini"])
        self.assertEqual(result["suggested_models"], [])
        self.assertIn("该模型不在密钥可用的模型列表里，这就是 403/404 的常见原因，请从列表中选择。",
                      result["problem"])

    async def test_working_model_has_no_suggestion(self):
        engine = AIEngine("https://workbuddy.tokenmao.cn/v1", KEY, model="ok-model")
        factory, _ = fake_async_client(
            get_result=response(200, {"data": [{"id": "ok-model"}]}),
            post_result=response(200, {"choices": [{"message": {"content": "OK"}}]}, method="POST"))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.diagnose(api_url="https://workbuddy.tokenmao.cn/v1", model="ok-model")
        self.assertEqual(result["suggested_models"], [])
        self.assertEqual(result["problem"], "")

    async def test_cached_list_still_helps_when_models_endpoint_is_forbidden(self):
        engine = AIEngine("https://workbuddy.tokenmao.cn/v1", KEY, model="TM DeepSeek V4.1")
        engine._known_models = ["TM ·DeepSeek-V4.1"]      # 上一次成功拉到过列表
        factory, _ = fake_async_client(
            get_result=response(403, {"error": {"message": "模型列表不开放"}}),
            post_result=response(403, {"error": {"message": "无权使用该模型"}}, method="POST"))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            result = await engine.diagnose(api_url="https://workbuddy.tokenmao.cn/v1",
                                           model="TM DeepSeek V4.1")
        self.assertIsNone(result["model_listed"])
        self.assertEqual(result["suggested_models"], ["TM ·DeepSeek-V4.1"])
        self.assertIn("最接近的是：TM ·DeepSeek-V4.1", result["problem"])


class RuntimeFixture(unittest.TestCase):
    """起一个隔离的 QQRuntime：配置与 sqlite 都在临时目录里，不碰真实 data/。"""

    def setUp(self):
        import yaml
        from src.qq_app import QQRuntime

        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.path = Path(self.tmp.name)
        config = self.path / "test.yaml"
        config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://api.moonshot.cn/v1", "api_key": KEY, "model": "kimi-k2"},
            "qq": {"app_id": "123", "client_secret": "fake-secret"},
        }), encoding="utf-8")
        self.bot = QQRuntime(config_path=config, db_path=self.path / "test.sqlite")

    def tearDown(self):
        # Windows 上 sqlite 文件还被连着时删不掉临时目录；先关连接再清理
        try:
            self.bot.db.close()
        except Exception:
            pass
        self.tmp.cleanup()


class AiRoutesTest(RuntimeFixture):
    """/api/ai/models 与 /api/ai/diagnose：只读探测，绝不写配置。"""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from src.qq_app import create_app

        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_models_route_uses_saved_key_and_writes_nothing(self):
        before = self.bot.config.config_path.read_bytes()
        factory, _ = fake_async_client(get_result=response(200, {"data": [{"id": "b"}, {"id": "a"}]}))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            res = self.client.post("/api/ai/models", json={"api_url": "", "api_key": "", "api_style": "auto"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["models"], ["a", "b"])
        self.assertEqual(data["url"], "https://api.moonshot.cn/v1/models")
        self.assertEqual(factory.call_args.kwargs["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(self.bot.ai.model, "kimi-k2")            # 探测不改模型
        self.assertEqual(self.bot.config.config_path.read_bytes(), before)

    def test_models_route_forwards_explicit_url_and_style(self):
        factory, _ = fake_async_client(get_result=response(200, {"data": [{"id": "claude-3-5-sonnet"}]}))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            data = self.client.post("/api/ai/models", json={
                "api_url": "https://api.anthropic.com", "api_key": KEY, "api_style": "anthropic"}).json()
        self.assertEqual(data["url"], "https://api.anthropic.com/v1/models")
        self.assertEqual(factory.call_args.kwargs["headers"]["x-api-key"], KEY)

    def test_diagnose_route_returns_chinese_problem_without_key(self):
        factory, client = fake_async_client(
            get_result=response(200, {"data": [{"id": "good-model"}]}),
            post_result=response(403, {"error": {"message": f"no access for {KEY}"}}, method="POST"))
        with patch("src.ai_engine.httpx.AsyncClient", factory):
            res = self.client.post("/api/ai/diagnose", json={"model": "Kimi K2.7 Code"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["ok"])
        self.assertIs(data["model_listed"], False)
        self.assertIn("HTTP 403", data["problem"])
        self.assertIn("该模型不在密钥可用的模型列表里", data["problem"])
        dumped = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(KEY, dumped)
        self.assertEqual(client.post.call_args.args[0], "https://api.moonshot.cn/v1/chat/completions")

    def test_settings_route_still_rejects_example_hosts_and_blank_model(self):
        res = self.client.post("/api/settings", json={
            "api_url": "https://api.example.com/v1", "model": "新模型", "app_id": "123"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("示例域名", res.json()["detail"])
        res = self.client.post("/api/settings", json={
            "api_url": "https://api.moonshot.cn/v1", "model": "   ", "app_id": "123"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("模型名称不能为空", res.json()["detail"])

    def test_new_baseurl_shapes_can_be_saved(self):
        for url in ("https://api.deepseek.com", "https://openrouter.ai/api/v1",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1", "http://127.0.0.1:3000",
                    "https://open.bigmodel.cn/api/paas/v4"):
            with self.subTest(url=url):
                res = self.client.post("/api/settings", json={
                    "api_url": url, "model": "some-model", "app_id": "123"})
                self.assertEqual(res.status_code, 200, res.text)
                self.assertTrue(res.json()["success"])
        # 保存后引擎里的地址是补全过的对话地址，配置里存的仍是用户原文
        self.assertEqual(self.bot.ai.api_url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(self.bot.config.get("ai.api_url"), "https://open.bigmodel.cn/api/paas/v4")


class FailureNoticeTest(RuntimeFixture):
    """事件日志里的 AI 失败提示：区分配置错误，并且一分钟只记一条。"""

    def test_permission_error_is_classified_and_throttled(self):
        error = "HTTP 403：密钥没有该模型或该分组的权限（中转站常见：换 key 或换分组/模型）"
        for _ in range(5):
            self.bot.note_failure(error, ai=True)
        texts = [event["text"] for event in self.bot.events]
        self.assertEqual(len(texts), 1)                      # 同一错误不再刷屏
        self.assertIn("AI 配置问题", texts[0])
        self.assertIn("HTTP 403", texts[0])
        self.assertIn("换 key", texts[0])

    def test_different_errors_are_still_recorded(self):
        self.bot.note_failure("HTTP 401：密钥无效或未授权", ai=True)
        self.bot.note_failure("HTTP 500：服务商故障或暂时不可用（5xx），稍后重试", ai=True)
        self.assertEqual(len(self.bot.events), 2)

    def test_qq_side_error_is_not_labelled_as_ai_problem(self):
        self.bot.note_failure("HTTP 403：权限或 IP 白名单未配置", ai=False)
        self.assertNotIn("AI 配置问题", self.bot.events[-1]["text"])
        self.assertIn("IP 白名单", self.bot.events[-1]["text"])


if __name__ == "__main__":
    unittest.main()
