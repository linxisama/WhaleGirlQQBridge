"""回复超时与超时重试（用户截图报的"没能生成回复"）。

现象：QQ 里收到「刚才没能生成回复，请稍后再试」，控制台运行记录写
「请求超时（15s）；已按设置不再重试」。

根因（自己引入的回归）：为了"3 秒回复"把聊天超时压到 15 秒，并且**关掉了超时重试** ——
中转站偶发变慢时，一次抖动就直接变成失败。正确做法是：超时值合理（默认 20 秒）+ 超时后
再试一次（第二次往往 1 秒就回来），同时让用户在设置页能改这两项。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx  # noqa: E402

from src.ai_engine import AIEngine  # noqa: E402
from src.config_manager import ConfigManager  # noqa: E402
from src.qq_app import QQRuntime, Settings, create_app  # noqa: E402

try:
    from fastapi.testclient import TestClient

    HAS_CLIENT = True
except Exception:
    HAS_CLIENT = False


def build_root(tmp, *, config_extra=""):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n"
        "  reply_timeout: 20\n  reply_timeout_retry: true\n" + config_extra, encoding="utf-8")
    return root


class TimeoutRetryEngineTests(unittest.TestCase):
    def engine(self, **kwargs):
        return AIEngine(api_url="https://example.test/v1", api_key="k", model="m",
                        timeout=5, max_retries=3, **kwargs)

    def client(self, engine, outcomes):
        """outcomes：每次调用的结果，"timeout" 或一段文本。"""
        calls = {"n": 0}

        def post(url, **kwargs):
            outcome = outcomes[min(calls["n"], len(outcomes) - 1)]
            calls["n"] += 1
            if outcome == "timeout":
                raise httpx.TimeoutException("slow")
            response = mock.Mock()
            response.status_code = 200
            response.json.return_value = {"choices": [{"message": {"content": outcome}}],
                                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            response.raise_for_status.return_value = None
            return response

        client = mock.AsyncMock()
        client.post = mock.AsyncMock(side_effect=post)
        return client, calls

    def run_chat(self, engine, client, **kwargs):
        async def run():
            with mock.patch.object(engine, "_get_client",
                                   new=mock.AsyncMock(return_value=client)), \
                 mock.patch("src.ai_engine.asyncio.sleep", new=mock.AsyncMock()) as sleep:
                reply = await engine.chat([{"role": "user", "content": "hi"}], **kwargs)
                return reply, sleep.await_count

        return asyncio.run(run())

    def test_timeout_then_success_returns_the_reply(self):
        engine = self.engine()
        client, calls = self.client(engine, ["timeout", "第二次就回来了"])
        reply, sleeps = self.run_chat(engine, client, timeout=15, retry_on_timeout=True)
        self.assertEqual(reply, "第二次就回来了")
        self.assertEqual(calls["n"], 2, "超时后应当再试一次")
        self.assertEqual(sleeps, 0, "超时重试不该再退避等待")

    def test_two_timeouts_give_up_with_a_clear_reason(self):
        engine = self.engine()
        client, calls = self.client(engine, ["timeout", "timeout", "timeout"])
        reply, sleeps = self.run_chat(engine, client, timeout=15, retry_on_timeout=True)
        self.assertEqual(reply, "")
        self.assertEqual(calls["n"], 2, "超时最多重试一次，不能拖成三次")
        self.assertEqual(sleeps, 0)
        self.assertIn("已重试一次仍未返回", engine.last_error)

    def test_retry_disabled_keeps_the_old_single_attempt(self):
        engine = self.engine()
        client, calls = self.client(engine, ["timeout", "never used"])
        reply, _ = self.run_chat(engine, client, timeout=15, retry_on_timeout=False)
        self.assertEqual(reply, "")
        self.assertEqual(calls["n"], 1)
        self.assertIn("超时后自动重试", engine.last_error)

    def test_engine_default_when_caller_does_not_specify(self):
        """构造参数决定默认：探测类调用不传 retry_on_timeout 时按构造值走。"""
        engine = self.engine(retry_on_timeout=True)
        client, calls = self.client(engine, ["timeout", "好"])
        reply, _ = self.run_chat(engine, client, timeout=15)
        self.assertEqual(reply, "好")
        self.assertEqual(calls["n"], 2)


class ConfigDefaultsTests(unittest.TestCase):
    def test_defaults_are_reasonable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.qq.yaml"
            config = ConfigManager(str(path))
            config.load("default")
            self.assertEqual(float(config.get("ai.reply_timeout")), 20.0)
            self.assertTrue(config.get("ai.reply_timeout_retry"))


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class TimeoutSettingsRouteTests(unittest.TestCase):
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

    def payload(self, **overrides):
        base = {"mode": "official", "api_url": "https://example.test/v1", "model": "test",
                "api_style": "auto", "reply_timeout": 25, "reply_timeout_retry": False}
        base.update(overrides)
        return base

    def test_settings_get_exposes_the_two_fields(self):
        data = self.client.get("/api/settings").json()
        self.assertEqual(float(data["reply_timeout"]), 20.0)
        self.assertTrue(data["reply_timeout_retry"])

    def test_settings_round_trip(self):
        response = self.client.post("/api/settings", json=self.payload())
        self.assertTrue(response.json()["success"], response.text)
        data = self.client.get("/api/settings").json()
        self.assertEqual(float(data["reply_timeout"]), 25.0)
        self.assertFalse(data["reply_timeout_retry"])
        saved = json.loads(json.dumps(self.bot.config.config))
        self.assertEqual(float(saved["ai"]["reply_timeout"]), 25.0)

    def test_dirty_values_are_clamped(self):
        self.client.post("/api/settings", json=self.payload(reply_timeout=0))
        self.assertGreaterEqual(float(self.bot.config.get("ai.reply_timeout")), 5.0)
        self.client.post("/api/settings", json=self.payload(reply_timeout=9999))
        self.assertLessEqual(float(self.bot.config.get("ai.reply_timeout")), 120.0)


class TimeoutFeedbackTests(unittest.TestCase):
    """失败时给 QQ 的那句话要说清"为什么"和"怎么办"。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "t.sqlite3")

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def feedback_for(self, last_error, retry=True):
        sent = []

        async def fake_send(message, text, meta=None):
            sent.append(text)
            return {"id": "1"}

        async def run():
            self.bot.ai.last_error = last_error
            self.bot.config.set("ai.reply_timeout_retry", retry)
            self.bot.official.send = fake_send
            self.bot._error_feedback_times.clear()
            self.bot.db.execute("INSERT INTO inbox(key,mode,account,room,payload,state,created)"
                                " VALUES('k',?,?,?,'{}','failed',?)",
                                (self.bot.mode, self.bot.account(), "group:1", time.time()))
            self.bot.db.commit()
            await self.bot._error_feedback("k", {"id": "m1", "kind": "group", "target": "1"},
                                          "group:1", "boom")

        asyncio.run(run())
        return sent[0] if sent else ""

    def test_timeout_feedback_does_not_publish_operator_settings(self):
        text = self.feedback_for("请求超时 (20s)")
        self.assertIn("超时", text)
        self.assertNotIn('模型', text)
        self.assertNotIn('20', text)
        self.assertNotIn('设置', text)

    def test_retry_setting_is_not_published_to_qq(self):
        text = self.feedback_for("请求超时 (20s)", retry=False)
        self.assertNotIn('重试', text)
        self.assertNotIn('关', text)

    def test_upstream_errors_are_local_diagnostics_only(self):
        text = self.feedback_for("HTTP 401：密钥无效")
        self.assertNotIn('401', text)
        self.assertNotIn('密钥', text)

    def test_empty_reason_keeps_the_generic_line(self):
        text = self.feedback_for("")
        self.assertIn("没能生成回复", text)


class FastProfileTimeoutTests(unittest.TestCase):
    """「快速回复」档位不该再把超时压到 15 秒、也不该关掉超时重试。"""

    @unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
    def test_profile_keeps_reasonable_timeout_and_enables_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, config_extra="")
            (root / "data" / "qq-only").mkdir(parents=True, exist_ok=True)
            (root / "data" / "qq-only" / "plugin_settings.json").write_text(
                json.dumps({"burst_merge": {"enabled": True, "capMs": 8000, "quietMs": 600}}),
                encoding="utf-8")
            bot = QQRuntime(root=root, config_path=root / "config.qq.yaml",
                            db_path=root / "t.sqlite3")
            bot.config.set("ai.reply_timeout", 45)
            bot.config.set("ai.reply_timeout_retry", False)
            bot.config.save_atomic()
            client = TestClient(create_app(bot))
            with client:
                response = client.post("/api/pc/speed-profile", json={"apply": True}).json()
            self.assertTrue(response["changed"])
            self.assertEqual(float(bot.config.get("ai.reply_timeout")), 20.0,
                             "只在明显过大（>30 秒）时才收，不该把合理值也压小")
            self.assertTrue(bot.config.get("ai.reply_timeout_retry"))
            self.assertIn("超时后自动重试", response["message"])
            try:
                bot.db.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
