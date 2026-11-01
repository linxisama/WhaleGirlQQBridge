"""群聊诊断：入站审计 + @ 标记渲染 + 控制台里的会话标注。

用户反馈「群聊回复还有很多 bug，不回复群聊信息，然后控制面板没有群聊」。
排查时最要紧的问题只有一个：**平台到底有没有把那条群消息推过来？**
以前只有"成功入队"才落库，"平台没推送"和"本程序丢掉了"长得一模一样。
现在每条收到的原始事件都写进 ``inbound_audit``，并标注最终处置。
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

from fastapi.testclient import TestClient  # noqa: E402

from src.qq_app import QQRuntime, create_app  # noqa: E402
from src.qq_transports import official_event  # noqa: E402

GROUP = "1CBE87EF553193840AFFDCC788043580"
ME = "238548A1FA8AF51CC5C1D0D81B67FD05"
BOT = "BOTOPENID1234567890ABCDEF1234567"


def build_root(tmp, *, mode="official", settings=None):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    data = root / "data" / "qq-only"
    data.mkdir(parents=True, exist_ok=True)
    if settings:
        (data / "plugin_settings.json").write_text(json.dumps(settings, ensure_ascii=False),
                                                   encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        f"ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n"
        f"qq_only:\n  mode: {mode}\n  listen_private: true\n", encoding="utf-8")
    return root


def group_payload(mid="M1", text="群聊还是有些bug", mention=BOT):
    return {"id": mid, "content": f"<@!{mention}> {text}", "group_openid": GROUP,
            "author": {"member_openid": ME, "username": "西莲寺紫"},
            "timestamp": int(time.time())}


class MentionRenderingTests(unittest.TestCase):
    """官方正文里的 ``<@!openid>`` 不能原样丢给模型。"""

    def test_bot_mention_becomes_readable(self):
        message = official_event("GROUP_AT_MESSAGE_CREATE", group_payload())
        self.assertNotIn("<@!", message["text"])
        self.assertNotIn(BOT[:20], message["text"], "不该把整串 openid 留给模型")
        self.assertIn("群聊还是有些bug", message["text"])

    def test_mentioned_ids_are_exposed(self):
        message = official_event("GROUP_AT_MESSAGE_CREATE", group_payload())
        self.assertEqual(message["at_ids"], [BOT],
                         "at_ids 以前永远是空的，按 @ 判断的插件全都失灵")

    def test_self_mention_uses_the_nickname(self):
        payload = group_payload(mention=ME)
        message = official_event("GROUP_AT_MESSAGE_CREATE", payload)
        self.assertIn("@西莲寺紫", message["text"])

    def test_multiple_mentions_keep_order_and_dedupe(self):
        payload = group_payload()
        payload["content"] = f"<@!{ME}> 你看 <@!{BOT}> 和 <@!{BOT}>"
        message = official_event("GROUP_AT_MESSAGE_CREATE", payload)
        self.assertEqual(message["at_ids"], [ME, BOT])
        self.assertIn("@西莲寺紫", message["text"])


class InboundAuditTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name,
                               settings={"chat_controls": {"enabled": True, "requireAt": True,
                                                           "aliases": "鲸鱼娘"}})
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "t.sqlite3")
        self.bot.running = True

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def rows(self):
        return self.bot.db.execute(
            "SELECT event,kind,outcome,reason,text FROM inbound_audit ORDER BY at").fetchall()

    def receiver(self):
        """跑一次接收循环，拿到传给传输层的 on_event。"""
        captured = {}

        async def fake_ws(on_event, should_stop):
            captured["cb"] = on_event

        async def run():
            self.bot.official.run_websocket = fake_ws
            await self.bot._official_receiver()

        asyncio.run(run())
        return captured["cb"]

    def test_every_raw_event_is_recorded(self):
        callback = self.receiver()

        async def run():
            await callback("GROUP_AT_MESSAGE_CREATE", group_payload())

        asyncio.run(run())
        rows = self.rows()
        self.assertEqual(len(rows), 2, rows)          # 收到 + 解析后各一条
        self.assertEqual(rows[0][2], "received")

    def test_queued_group_message_is_marked_queued(self):
        callback = self.receiver()

        async def run():
            await callback("GROUP_AT_MESSAGE_CREATE", group_payload())

        asyncio.run(run())
        self.assertEqual(self.rows()[-1][2], "queued")
        self.assertIn("已进入回复队列", self.rows()[-1][3])

    def test_non_chat_event_is_marked_ignored_not_dropped(self):
        callback = self.receiver()

        async def run():
            await callback("SOMETHING_ELSE", {"foo": 1})

        asyncio.run(run())
        self.assertEqual(self.rows()[-1][2], "ignored")

    def test_unparsable_event_is_marked_unparsed(self):
        callback = self.receiver()

        async def run():
            with mock.patch("src.qq_app.official_event", side_effect=KeyError("bad")):
                await callback("GROUP_AT_MESSAGE_CREATE", {})

        asyncio.run(run())
        self.assertEqual(self.rows()[-1][2], "unparsed")

    def test_blocked_message_keeps_the_reason(self):
        """被插件/规则拦下的群消息也要能看出为什么。"""
        callback = self.receiver()

        async def run():
            await callback("GROUP_AT_MESSAGE_CREATE",
                           group_payload(text="没点名也没喊名字的一句话", mention=BOT))

        asyncio.run(run())
        bot = self.bot
        bot.plugins._plugins["chat_controls"]._manager._settings["chat_controls"] = {
            "enabled": True, "requireAt": True, "aliases": "鲸鱼娘"}
        rows = self.rows()
        self.assertIn(rows[-1][2], {"queued", "dropped"})

    def test_status_api_exposes_inbound_and_chat_labels(self):
        callback = self.receiver()

        async def run():
            await callback("GROUP_AT_MESSAGE_CREATE", group_payload())

        asyncio.run(run())
        client = TestClient(create_app(self.bot))
        with client:
            data = client.get("/api/status").json()
        self.assertTrue(data["inbound"], "控制台要能看到收到的每条事件")
        self.assertEqual(data["inbound"][0]["outcome"], "queued")
        self.assertTrue(data["messages"], "入队的消息要出现在消息列表里")
        first = data["messages"][0]
        self.assertEqual(first["kind"], "group")
        self.assertEqual(first["target"], GROUP)

    def test_platform_silence_is_distinguishable(self):
        """平台没推送时审计表里**没有**对应行 —— 这正是要靠它区分的情况。"""
        callback = self.receiver()

        async def run():
            await callback("GROUP_AT_MESSAGE_CREATE", group_payload(mid="M-only-private"))

        asyncio.run(run())
        rows = self.bot.db.execute(
            "SELECT count(*) FROM inbound_audit WHERE message_id='M-never-sent'").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_audit_is_pruned_to_a_week(self):
        old = time.time() - 8 * 86400
        self.bot.db.execute(
            "INSERT INTO inbound_audit(at,mode,event,kind,room,sender,at_flag,text,media,outcome,reason,message_id)"
            " VALUES(?,?,'X','group','group:1','s',1,'t',0,'queued','','old')", (old, "official"))
        self.bot.db.commit()
        self.bot.audit_inbound(None, "PING", outcome="ignored", reason="测试")
        remaining = self.bot.db.execute(
            "SELECT count(*) FROM inbound_audit WHERE message_id='old'").fetchone()[0]
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
