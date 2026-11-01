"""平台能力自检：按钮交互这类"平台开没开通"的事，要让用户在控制台里一眼看到。

背景（2026-10-15 实测）：给用户账号的官方 WS Identify 加上 INTERACTION intent（1<<26）后，
平台回 **op=9 INVALID_SESSION** —— 说明该账号没开通按钮交互能力，QQ 里点按钮/键盘
不会有任何事件到达机器人。程序无法绕过平台限制，但可以**证明**并说清楚，
避免用户以为是程序坏了。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient  # noqa: E402

from src.qq_app import QQRuntime, create_app  # noqa: E402
from src.qq_transports import OfficialQQ  # noqa: E402


def build_root(tmp, *, mode="official"):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n"
        f"qq_only:\n  mode: {mode}\n", encoding="utf-8")
    return root


class IntentProbeTests(unittest.TestCase):
    """probe_intents：连一次 WS，看这套 intents 平台收不收。"""

    def transport(self):
        qq = OfficialQQ("appid", "secret")
        qq.authorize = mock.AsyncMock(return_value="token")
        return qq

    def fake_websockets(self, scripted):
        """scripted：每次连接要回的顺序消息（dict 会被 json 化）。"""
        calls = {"n": 0}

        class FakeWS:
            def __init__(self, replies):
                self.replies = list(replies)

            async def recv(self):
                return json.dumps(self.replies.pop(0))

            async def send(self, raw):
                calls.setdefault("sent", []).append(json.loads(raw))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        def connect(url, **kwargs):
            index = min(calls["n"], len(scripted) - 1)
            calls["n"] += 1
            return FakeWS(scripted[index])

        module = mock.Mock()
        module.connect = connect
        return module, calls

    def test_base_intents_accepted_and_interaction_rejected(self):
        qq = self.transport()
        module, calls = self.fake_websockets([
            [{"op": 10, "d": {"heartbeat_interval": 40000}}, {"op": 0, "t": "READY"}],
            [{"op": 10, "d": {"heartbeat_interval": 40000}}, {"op": 9, "d": False}],
        ])
        with mock.patch.dict("sys.modules", {"websockets": module}):
            result = asyncio.run(qq.probe_intents(OfficialQQ.INTERACTION_INTENT))
        self.assertTrue(result["ok"], result)
        self.assertIs(result["interaction"], False, result)
        self.assertIn("平台拒绝", result["results"][1]["detail"])
        sent = calls["sent"]
        self.assertEqual(sent[0]["d"]["intents"], OfficialQQ.INTENTS)
        self.assertTrue(sent[1]["d"]["intents"] & OfficialQQ.INTERACTION_INTENT)

    def test_interaction_available_case(self):
        qq = self.transport()
        module, _ = self.fake_websockets([
            [{"op": 10, "d": {}}, {"op": 0, "t": "READY"}],
            [{"op": 10, "d": {}}, {"op": 0, "t": "READY"}],
        ])
        with mock.patch.dict("sys.modules", {"websockets": module}):
            result = asyncio.run(qq.probe_intents(OfficialQQ.INTERACTION_INTENT))
        self.assertTrue(result["ok"])
        self.assertIs(result["interaction"], True)


class CapabilityRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "t.sqlite3")
        self.bot.official.app_id = "1905714695"
        self.bot.official.secret = "s" * 32
        self.bot.official.probe_intents = mock.AsyncMock(return_value={
            "ok": True,
            "interaction": False,
            "results": [{"label": "当前（群@ + 单聊）", "intents": 1, "ok": True, "detail": ""},
                        {"label": "加上按钮交互", "intents": 2, "ok": False,
                         "detail": "平台拒绝了这个 intents（会话无效）"}]})
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_refuses_while_the_bot_is_running(self):
        self.bot.running = True
        try:
            response = self.client.post("/api/platform/capabilities", json={})
            self.assertEqual(response.status_code, 400)
            self.assertIn("先停止", response.json()["detail"])
        finally:
            self.bot.running = False
        self.bot.official.probe_intents.assert_not_awaited()

    def test_explains_missing_interaction_capability(self):
        response = self.client.post("/api/platform/capabilities", json={})
        body = response.json()
        self.assertTrue(body["success"])
        self.assertIs(body["interaction"], False)
        self.assertIn("按钮交互", body["message"])
        self.assertIn("指令菜单", body["message"])
        self.assertIn("❌", body["message"])

    def test_refuses_in_onebot_mode(self):
        self.bot.config.set("qq_only.mode", "onebot")
        response = self.client.post("/api/platform/capabilities", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("官方 QQ", response.json()["detail"])

    def test_refuses_without_credentials(self):
        self.bot.official.secret = ""
        response = self.client.post("/api/platform/capabilities", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("AppSecret", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
