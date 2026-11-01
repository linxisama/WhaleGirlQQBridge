"""QQ 端"卡死"的后台自愈：接收/观察/定时任务死了必须自己活过来。

真实取证（用户机器）：`logs/startup.log` 里最后一条处理记录停在某个时间点之后，
数据库里再没有任何新消息（既没排队也没失败）—— 典型的"接收路径断了但没人管"。
原来 `_health` 只重启**回复循环**，`official_ws` / `observer` / `plugin_ticker`
一旦结束就再也没人拉起来。
"""
import asyncio
import os
import pathlib
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n", encoding="utf-8")
    return root


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass
        self._tmp.cleanup()

    def test_dead_receiver_is_revived(self):
        async def run():
            self.bot.running = True
            self.bot.config.set("qq_only.mode", "official")
            dead = asyncio.create_task(asyncio.sleep(0))
            await dead                                  # 确保它真结束了
            self.bot.official_ws = dead
            revived = self.bot._revive_task("official_ws", "_official_receiver",
                                            "官方消息接收任务已恢复（之前中断过）")
            started = self.bot.official_ws
            self.bot.running = False
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
            return revived, started, [item["text"] for item in self.bot.events]

        revived, started, events = asyncio.run(run())
        self.assertTrue(revived, "接收任务死了却没被重启")
        self.assertTrue(any("已恢复" in text for text in events), events)

    def test_live_task_is_left_alone(self):
        async def run():
            self.bot.running = True
            self.bot.config.set("qq_only.mode", "official")

            async def alive():
                await asyncio.sleep(3600)

            task = asyncio.create_task(alive())
            self.bot.official_ws = task
            try:
                return self.bot._revive_task("official_ws", "_official_receiver", "重启")
            finally:
                self.bot.running = False
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertFalse(asyncio.run(run()), "活着的任务不该被重启")

    def test_no_revive_when_stopped_by_user(self):
        async def run():
            self.bot.running = False
            self.bot.config.set("qq_only.mode", "official")
            self.bot.official_ws = None
            return self.bot._revive_task("official_ws", "_official_receiver", "重启")

        self.assertFalse(asyncio.run(run()), "用户自己停了就不该自动拉起")

    def test_official_receiver_is_not_created_in_onebot_mode(self):
        async def run():
            self.bot.running = True
            self.bot.config.set("qq_only.mode", "onebot")
            self.bot.official_ws = None
            return self.bot._revive_task("official_ws", "_official_receiver", "重启")

        self.assertFalse(asyncio.run(run()))

    def test_observer_is_revived(self):
        async def run():
            self.bot.running = True
            dead = asyncio.create_task(asyncio.sleep(0))
            await dead
            self.bot.observer = dead
            ok = self.bot._revive_task("observer", "_observe", "后台观察任务已恢复")
            started = self.bot.observer
            self.bot.running = False
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
            return ok, [item["text"] for item in self.bot.events]

        ok, events = asyncio.run(run())
        self.assertTrue(ok, events)


class ReceiverCallbackTests(unittest.TestCase):
    """接收回调必须自己吞掉"这条消息被拒"，否则会把 WebSocket 拖去重连。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass
        self._tmp.cleanup()

    def grab_on_event(self):
        """跑一次 _official_receiver，把传给传输层的 on_event 抓出来。"""
        captured = {}
        message = {"id": "m1", "kind": "group", "target": "456", "sender_id": "20002",
                   "sender_name": "小鱼", "text": "鲸鱼娘在吗", "at": True, "media": []}

        async def fake_run_websocket(on_event, should_stop):
            captured["on_event"] = on_event

        async def run():
            self.bot.running = True
            self.bot.official.run_websocket = fake_run_websocket
            with mock.patch("src.qq_app.official_event", return_value=dict(message)):
                await self.bot._official_receiver()

        asyncio.run(run())
        return captured["on_event"]

    async def fire(self, on_event, **extra):
        """用一条能解析的官方事件调用接收回调（official_event 在回调执行期间保持被替换）。"""
        message = {"id": "m1", "kind": "group", "target": "456", "sender_id": "20002",
                   "sender_name": "小鱼", "text": "鲸鱼娘在吗", "at": True, "media": []}
        with mock.patch("src.qq_app.official_event", return_value=dict(message)):
            await on_event("GROUP_AT_MESSAGE_CREATE", {})

    def test_ingest_refusal_does_not_escape(self):
        from fastapi import HTTPException

        on_event = self.grab_on_event()

        def refuse(message, mode):                 # ingest 是同步方法，这里必须同步抛
            raise HTTPException(503, "待处理消息已满，请稍后重试")

        async def run():
            with mock.patch.object(self.bot, "ingest", side_effect=refuse):
                await self.fire(on_event)          # 不抛异常才算通过

        asyncio.run(run())
        self.assertTrue(any("没有入队" in item["text"] for item in self.bot.events),
                        f"应当记一条事件日志：{self.bot.events[-2:]}")

    def test_plugin_crash_in_ingest_does_not_escape(self):
        on_event = self.grab_on_event()

        def boom(message, mode):
            raise RuntimeError("插件炸了")

        async def run():
            with mock.patch.object(self.bot, "ingest", side_effect=boom):
                await self.fire(on_event)

        asyncio.run(run())
        self.assertTrue(any("接收继续" in item["text"] for item in self.bot.events))

    def test_unparsable_event_is_skipped(self):
        on_event = self.grab_on_event()

        async def run():
            with mock.patch("src.qq_app.official_event", side_effect=KeyError("bad")):
                await on_event("WEIRD_EVENT", {})

        asyncio.run(run())
        self.assertTrue(any("无法解析" in item["text"] for item in self.bot.events))

    def test_healthy_message_still_reaches_ingest(self):
        """别把"兜住异常"改成"什么都不做"：正常消息必须照旧入队。"""
        on_event = self.grab_on_event()
        seen = []

        def record(message, mode):
            seen.append((message.get("text"), mode))

        async def run():
            with mock.patch.object(self.bot, "ingest", side_effect=record):
                await self.fire(on_event)

        asyncio.run(run())
        self.assertEqual(seen, [("鲸鱼娘在吗", "official")])


if __name__ == "__main__":
    unittest.main()
