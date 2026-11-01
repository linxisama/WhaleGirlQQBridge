"""兼容视图（messages / bot_messages）与两个既有插件的离线测试。

背景：库里只有 `inbox` 一张表，但 `proactive_chat` 与 `silence_ping` 按
`messages` / `bot_messages` 查询收发时间。表不存在 → 查询抛异常 → 被插件的
`except` 吞掉 → **空闲判断永远返回 None**，插件开着却从不生效（也不报错）。

修法：在 `inbox` 上建两张视图，一次修好所有同类查询。这里既验证视图本身，
也验证两个插件通过视图能真的拿到时间。
"""

import json
import pathlib
import sqlite3
import tempfile
import time
import unittest

import yaml

from src.qq_app import QQRuntime


def make_runtime(tmp: pathlib.Path) -> QQRuntime:
    config = tmp / "test.yaml"
    config.write_text(yaml.safe_dump({
        "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "m"},
        "qq": {"app_id": "1", "client_secret": "s"},
    }), encoding="utf-8")
    return QQRuntime(config_path=config, db_path=tmp / "test.sqlite")


def insert_inbox(bot, *, key, payload, state="sent", reply="", created=None):
    bot.db.execute(
        "INSERT INTO inbox(key,mode,account,room,payload,state,reply,created) VALUES(?,?,?,?,?,?,?,?)",
        (key, "onebot", "self", payload.get("room", "group:1"),
         json.dumps(payload, ensure_ascii=False), state, reply,
         time.time() if created is None else created),
    )
    bot.db.commit()


def close_runtime(bot) -> None:
    """关掉 runtime 与 SQLite 句柄。

    Windows 下只要句柄还在，临时目录就删不掉（WinError 32）；所以显式关连接，
    并容忍已经关过的情况。
    """
    try:
        bot.db.close()
    except Exception:
        pass
    try:
        bot.official.http = None
    except Exception:
        pass


class ViewExistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = pathlib.Path(self._tmp.name)
        self.bot = make_runtime(self.tmp)

    def tearDown(self):
        close_runtime(self.bot)
        self._tmp.cleanup()

    def test_views_exist(self):
        names = {row[0] for row in self.bot.db.execute(
            "SELECT name FROM sqlite_master WHERE type='view'")}
        self.assertIn("messages", names)
        self.assertIn("bot_messages", names)

    def test_messages_view_exposes_inbound_fields(self):
        insert_inbox(self.bot, key="k1", created=1000.0, payload={
            "id": "m1", "kind": "group", "target": 12345, "sender_id": "7",
            "sender_name": "小明", "text": "你好", "room": "group:12345"})
        row = self.bot.db.execute(
            "SELECT id, kind, target, sender_id, sender_name, text, time FROM messages"
        ).fetchone()
        self.assertEqual(row[0], "m1")
        self.assertEqual(row[1], "group")
        self.assertEqual(row[2], "12345")      # 统一成文本，便于与插件传入的字符串比较
        self.assertEqual(row[3], "7")
        self.assertEqual(row[4], "小明")
        self.assertEqual(row[5], "你好")
        self.assertEqual(row[6], 1000.0)

    def test_notice_events_are_excluded_from_messages(self):
        insert_inbox(self.bot, key="poke1", payload={
            "id": "p1", "kind": "group", "target": "1", "text": "",
            "notice": "poke", "room": "group:1"})
        insert_inbox(self.bot, key="k2", payload={
            "id": "m2", "kind": "group", "target": "1", "text": "正常消息", "room": "group:1"})
        rows = list(self.bot.db.execute("SELECT id FROM messages"))
        self.assertEqual([r[0] for r in rows], ["m2"])

    def test_bot_messages_view_only_lists_replies(self):
        insert_inbox(self.bot, key="k1", reply="机器人说的话", payload={
            "id": "m1", "kind": "group", "target": "1", "text": "用户的话", "room": "group:1"})
        insert_inbox(self.bot, key="k2", reply="", payload={
            "id": "m2", "kind": "group", "target": "1", "text": "还没回", "room": "group:1"})
        rows = list(self.bot.db.execute("SELECT id, text FROM bot_messages"))
        self.assertEqual([(r[0], r[1]) for r in rows], [("k1", "机器人说的话"), ("k2", "")][:1] + [])

    def test_max_time_query_works_for_group(self):
        # 这是 proactive_chat 原本的查询：以前会因表不存在抛异常
        insert_inbox(self.bot, key="k1", created=500.0, payload={
            "id": "m1", "kind": "group", "target": "12345", "text": "a", "room": "group:12345"})
        insert_inbox(self.bot, key="k2", created=900.0, payload={
            "id": "m2", "kind": "group", "target": "12345", "text": "b", "room": "group:12345"})
        value = self.bot.db.execute(
            "SELECT MAX(time) FROM messages WHERE target=? AND kind='group'", ("12345",)
        ).fetchone()[0]
        self.assertEqual(value, 900.0)

    def test_max_time_query_works_for_private_sender(self):
        # 这是 silence_ping 原本的查询
        insert_inbox(self.bot, key="k1", created=700.0, payload={
            "id": "m1", "kind": "private", "target": "1", "text": "hi", "sender_id": "7",
            "room": "private:1"})
        value = self.bot.db.execute(
            "SELECT MAX(time) FROM messages WHERE sender_id=? AND kind='private'", ("7",)
        ).fetchone()[0]
        self.assertEqual(value, 700.0)

    def test_bot_messages_query_works_for_private_target(self):
        insert_inbox(self.bot, key="k1", created=800.0, reply="在的", payload={
            "id": "m1", "kind": "private", "target": "7", "text": "hi", "room": "private:7"})
        value = self.bot.db.execute(
            "SELECT MAX(time) FROM bot_messages WHERE target=? AND kind='private'", ("7",)
        ).fetchone()[0]
        self.assertEqual(value, 800.0)

    def test_views_survive_reopen(self):
        insert_inbox(self.bot, key="k1", created=1234.0, payload={
            "id": "m1", "kind": "group", "target": "1", "text": "a", "room": "group:1"})
        close_runtime(self.bot)
        reopened = QQRuntime(config_path=self.tmp / "test.yaml", db_path=self.tmp / "test.sqlite")
        try:
            value = reopened.db.execute("SELECT MAX(time) FROM messages").fetchone()[0]
            self.assertEqual(value, 1234.0)
        finally:
            close_runtime(reopened)


class PluginIntegrationTests(unittest.TestCase):
    """两个既有插件要能通过视图真的拿到时间（修之前它们永远拿 None）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = pathlib.Path(self._tmp.name)
        self.bot = make_runtime(self.tmp)

    def tearDown(self):
        close_runtime(self.bot)
        self._tmp.cleanup()

    def test_proactive_chat_gets_last_group_message_time(self):
        from src.plugins.proactive_chat import ProactiveChat

        insert_inbox(self.bot, key="k1", created=2000.0, payload={
            "id": "m1", "kind": "group", "target": "999", "text": "a", "room": "group:999"})
        plugin = ProactiveChat.__new__(ProactiveChat)
        self.bot.config.set('qq_only.mode','onebot');self.bot.onebot.self_id='self'
        value = ProactiveChat._last_group_message_time(plugin, self.bot, "999")
        self.assertEqual(value, 2000.0)

    def test_silence_ping_gets_last_dm_and_bot_time(self):
        from src.plugins.silence_ping import SilencePing

        insert_inbox(self.bot, key="k1", created=3000.0, reply="在的", payload={
            "id": "m1", "kind": "private", "target": "7", "text": "hi", "sender_id": "7",
            "room": "private:7"})
        self.bot.config.set("qq_only.mode","onebot");self.bot.onebot.self_id="self"
        plugin = SilencePing.__new__(SilencePing)
        self.assertEqual(SilencePing._last_dm_time(plugin, self.bot, "7"), 3000.0)
        self.assertEqual(SilencePing._last_bot_dm_time(plugin, self.bot, "7"), 3000.0)

    def test_missing_history_returns_none_not_exception(self):
        from src.plugins.proactive_chat import ProactiveChat

        plugin = ProactiveChat.__new__(ProactiveChat)
        self.assertIsNone(ProactiveChat._last_group_message_time(plugin, self.bot, "no-such-group"))


if __name__ == "__main__":
    unittest.main()
