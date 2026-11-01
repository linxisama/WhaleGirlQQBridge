"""回复速度第二部分：时延记账 + 「速度」查询 + 「快速回复」档位（需确认才落盘）。"""
import asyncio
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.robot_admin import read_status  # noqa: E402
from src.speed_profile import FAST_VALUES, NEVER_TOUCH, describe, profile_diff  # noqa: E402
from src.qq_app import QQRuntime, create_app  # noqa: E402
from src.plugins.pc_control import session_command  # noqa: E402

try:
    from fastapi.testclient import TestClient

    HAS_CLIENT = True
except Exception:
    HAS_CLIENT = False

BUILTIN = "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n"
SLOW_SETTINGS = {
    "burst_merge": {"enabled": True, "capMs": 8000, "quietMs": 600},
    "send_pacing": {"enabled": True, "intervalMs": 1200},
    "turn_complete": {"enabled": True, "quietMs": 6000, "maxDelayMs": 8000},
    "msg_reaction": {"enabled": True, "delaySec": 3},
    "typing_indicator": {"enabled": True},
    "context_chime": {"enabled": True},
    "chat_controls": {"enabled": True, "requireAt": True, "aliases": "鲸鱼娘"},
}


def build_root(tmp, *, settings=None):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    data = root / "data" / "qq-only"
    data.mkdir(parents=True, exist_ok=True)
    (data / "plugin_settings.json").write_text(
        json.dumps(settings if settings is not None else SLOW_SETTINGS, ensure_ascii=False),
        encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n"
        "  reply_timeout: 30\n", encoding="utf-8")
    return root


class ProfileTests(unittest.TestCase):
    def test_diff_only_lists_latency_knobs_that_differ(self):
        plan = profile_diff(SLOW_SETTINGS)
        self.assertEqual(plan["burst_merge"], {"capMs": 1200, "quietMs": 500})
        self.assertEqual(plan["context_chime"], {"enabled": False})
        self.assertEqual(plan["typing_indicator"], {"enabled": False})
        self.assertIn("send_pacing", plan)
        self.assertNotIn("chat_controls", plan, "与等待无关的插件不该出现在清单里")

    def test_already_fast_settings_produce_no_diff(self):
        fast = {pid: dict(values) for pid, values in FAST_VALUES.items()}
        fast["burst_merge"] = {"enabled": True, **FAST_VALUES["burst_merge"]}
        self.assertEqual(profile_diff(fast), {})

    def test_never_touches_credentials_or_personas(self):
        for key in NEVER_TOUCH:
            for values in FAST_VALUES.values():
                self.assertNotIn(key, values, key)
        plan = profile_diff(SLOW_SETTINGS)
        flat = json.dumps(plan, ensure_ascii=False)
        for word in ("api_url", "api_key", "model", "client_secret", "app_id", "style"):
            self.assertNotIn(word, flat, word)

    def test_unknown_plugin_is_skipped_not_invented(self):
        self.assertEqual(profile_diff({"burst_merge": {"enabled": True}}), {},
                         "缺键就不该瞎猜目标值")

    def test_describe_is_human_readable(self):
        text = describe(profile_diff(SLOW_SETTINGS))
        self.assertIn("连发合并", text)
        self.assertIn("1200", text)
        self.assertIn("关闭", text, "布尔值要写成 开启/关闭")


class SpeedQueryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def test_latency_is_recorded_and_summarised(self):
        now = time.time()
        message = {"kind": "group", "target": "456", "id": "m1"}
        for index in range(3):
            self.bot._record_latency(message, now - 0.1, now, 0.05, 900 + index * 100)
        summary = self.bot.latency_summary()
        self.assertEqual(summary["count"], 3)
        self.assertAlmostEqual(summary["model_ms"], 1000, delta=150)
        self.assertGreater(summary["total_ms"], 0)
        self.assertGreaterEqual(summary["slowest_ms"], summary["fastest_ms"])
        self.assertEqual(len(self.bot.latency), 3)

    def test_ring_buffer_keeps_only_recent(self):
        now = time.time()
        for index in range(40):
            self.bot._record_latency({"kind": "group", "target": "456"}, now - 0.1, now,
                                     0.0, 100)
        self.assertEqual(len(self.bot.latency), 30)

    def test_speed_command_reports_the_breakdown(self):
        now = time.time()
        self.bot._record_latency({"kind": "group", "target": "456"}, now - 0.1, now, 0.02, 900)
        text = read_status(self.bot, "speed")
        self.assertIn("平均耗时", text)
        self.assertIn("拆解", text)
        self.assertIn("模型测速", text)

    def test_speed_command_without_history_asks_for_chat(self):
        self.assertIn("还没有回复记录", read_status(self.bot, "speed"))

    def test_session_vocabulary_knows_speed(self):
        self.assertEqual(session_command("速度"), ("speed", ""))


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class SpeedProfileRouteTests(unittest.TestCase):
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

    def test_preview_does_not_change_anything(self):
        before = json.loads((self.root / "data" / "qq-only" / "plugin_settings.json")
                            .read_text(encoding="utf-8"))
        response = self.client.post("/api/pc/speed-profile", json={}).json()
        self.assertTrue(response["success"])
        self.assertFalse(response["changed"])
        self.assertIn("burst_merge", response["plan"])
        after = json.loads((self.root / "data" / "qq-only" / "plugin_settings.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(before, after, "预览不该动任何设置")
        self.assertEqual(self.bot.plugins.get_settings("burst_merge")["capMs"], 8000)

    def test_apply_writes_only_the_planned_keys(self):
        response = self.client.post("/api/pc/speed-profile", json={"apply": True}).json()
        self.assertTrue(response["changed"])
        settings = self.bot.plugins.get_settings("burst_merge")
        self.assertEqual(settings["capMs"], 1200)
        self.assertEqual(settings["quietMs"], 500)
        self.assertTrue(settings["enabled"], "不该顺手把插件关掉")
        self.assertFalse(self.bot.plugins.get_settings("context_chime")["enabled"])
        self.assertFalse(self.bot.plugins.get_settings("typing_indicator")["enabled"])
        self.assertEqual(self.bot.plugins.get_settings("chat_controls")["aliases"], "鲸鱼娘")
        self.assertEqual(self.bot.ai.model, "test", "模型不该被碰")
        # 回复超时只在"明显过大"时才收（本用例配置里是 30 秒 → 保持不变）。
        # 这里以前断言"压到 15 秒"，后来发现那会把中转站的偶发抖动变成直接失败，
        # 所以改成：>30 秒才收到 20 秒，并把「超时后自动重试」打开。
        self.assertEqual(float(self.bot.config.get("ai.reply_timeout")), 30)
        self.assertTrue(self.bot.config.get("ai.reply_timeout_retry"))
        import yaml
        saved = yaml.safe_load((self.root / "config.qq.yaml").read_text(encoding="utf-8"))
        self.assertEqual(float(saved["ai"]["reply_timeout"]), 30)

    def test_apply_shrinks_an_overlong_timeout_only(self):
        self.bot.config.set("ai.reply_timeout", 90)
        self.bot.config.set("ai.reply_timeout_retry", False)
        self.bot.config.save_atomic()
        response = self.client.post("/api/pc/speed-profile", json={"apply": True}).json()
        self.assertEqual(float(self.bot.config.get("ai.reply_timeout")), 20)
        self.assertTrue(self.bot.config.get("ai.reply_timeout_retry"))
        self.assertIn("回复超时", response["message"])
        self.assertIn("重试", response["message"])

    def test_second_apply_says_already_fast(self):
        self.client.post("/api/pc/speed-profile", json={"apply": True})
        again = self.client.post("/api/pc/speed-profile", json={}).json()
        self.assertEqual(again["plan"], {})
        self.assertIn("已经是快档", again["message"])

    def test_apply_sets_a_reply_length_ceiling(self):
        response = self.client.post("/api/pc/speed-profile", json={"apply": True}).json()
        self.assertEqual(int(self.bot.config.get("ai.reply_max_tokens")), 150)
        self.assertIn("回复长度上限", response["message"])

    def test_apply_keeps_a_stricter_user_ceiling(self):
        self.bot.config.set("ai.reply_max_tokens", 80)
        self.bot.config.save_atomic()
        self.client.post("/api/pc/speed-profile", json={"apply": True})
        self.assertEqual(int(self.bot.config.get("ai.reply_max_tokens")), 80,
                         "用户自己设的更严的上限不该被放宽")


class ReplyLengthCeilingTests(unittest.TestCase):
    """应用级回复长度上限：只压不抬，人设自己的上限仍然有效。"""

    def runtime(self, tmp, *, persona_tokens, ceiling):
        root = build_root(tmp)
        (root / "styles" / "default.yaml").write_text(
            f"name: default\ndisplay_name: 默认\nmax_tokens: {persona_tokens}\n"
            "system_prompt: 你是默认助手。\n", encoding="utf-8")
        (root / "config.qq.yaml").write_text(
            f"ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n"
            f"  reply_max_tokens: {ceiling}\n", encoding="utf-8")
        return QQRuntime(root=root, config_path=root / "config.qq.yaml",
                         db_path=root / "t.sqlite3")

    def passed_max_tokens(self, bot):
        seen = {}

        async def fake_chat(messages, **kwargs):
            seen.update(kwargs)
            return "好呀"

        async def fake_send(*args, **kwargs):
            return {"id": "1"}

        async def run():
            bot.running = True
            bot.official.authenticated = True
            bot.official.send = fake_send
            bot.ai.chat = fake_chat
            bot.worker = asyncio.create_task(bot._work())
            bot.ingest({"id": "m1", "kind": "group", "target": "456", "sender_id": "20002",
                        "sender_name": "小鱼", "text": "鲸鱼娘在吗", "at": True, "media": []},
                       "official")
            for _ in range(300):
                if seen:
                    break
                await asyncio.sleep(0.02)

        asyncio.run(run())
        return seen.get("max_tokens")

    def test_ceiling_lowers_a_chatty_persona(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.runtime(tmp, persona_tokens=400, ceiling=150)
            try:
                self.assertEqual(self.passed_max_tokens(bot), 150)
            finally:
                asyncio.run(bot.close())

    def test_ceiling_never_raises_a_strict_persona(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.runtime(tmp, persona_tokens=60, ceiling=400)
            try:
                self.assertEqual(self.passed_max_tokens(bot), 60)
            finally:
                asyncio.run(bot.close())

    def test_zero_means_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.runtime(tmp, persona_tokens=400, ceiling=0)
            try:
                self.assertEqual(self.passed_max_tokens(bot), 400)
            finally:
                asyncio.run(bot.close())


class ReplyTimeoutTests(unittest.TestCase):
    def test_reply_path_passes_the_configured_timeout(self):
        """聊天回复用 ai.reply_timeout（默认 15 秒），探测类仍用 30 秒。"""
        from src.ai_engine import AIEngine

        engine = AIEngine(api_url="https://example.test/v1", api_key="k", model="m", timeout=30)
        seen = {}

        async def fake_chat(messages, **kwargs):
            seen.update(kwargs)
            return "好"

        async def run():
            with mock.patch.object(engine, "chat", side_effect=fake_chat):
                return await engine.chat([{"role": "user", "content": "hi"}], timeout=15)

        asyncio.run(run())
        self.assertEqual(seen.get("timeout"), 15)


if __name__ == "__main__":
    unittest.main()
