"""plugin_core 内核桥接：目录扫描、消息归一化、发送与运行时接入。"""
import asyncio
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.plugin_core_bridge import (
    PluginCoreBridge,
    build_raw_message,
    media_to_reply_meta,
)
from src.qq_app import QQRuntime


ROOT = Path(__file__).resolve().parents[1]
HELLO = ROOT / "plugin_core" / "examples" / "hello"


class BuildMessageTests(unittest.TestCase):
    def test_group_text_media_and_mentions(self):
        raw = build_raw_message({
            "id": "m1", "kind": "group", "target": "g1", "text": "看这个",
            "sender_id": "u1", "sender_name": "小明", "sender_role": "admin",
            "at_ids": ["u2"], "reply_to": "m0",
            "media": [{"type": "image", "url": "http://x/a.png", "size": 3},
                      {"type": "face", "id": "14"}],
        })
        self.assertEqual(raw["session"], {"id": "g1", "kind": "group", "name": ""})
        self.assertEqual(raw["sender"]["role"], "admin")
        self.assertEqual(raw["message"]["text"], "看这个")
        self.assertEqual([s["type"] for s in raw["message"]["segments"]], ["text", "image", "face"])
        self.assertEqual(raw["message"]["mentions"], ["u2"])
        self.assertEqual(raw["message"]["reply_to"], "m0")

    def test_private_role_defaults_to_member(self):
        raw = build_raw_message({"kind": "private", "target": "u1", "text": "hi",
                                 "sender_id": "u1", "sender_name": "小明"})
        self.assertEqual(raw["session"]["id"], "u1")
        self.assertEqual(raw["sender"]["role"], "member")

    def test_media_to_reply_meta_skips_file(self):
        media = media_to_reply_meta([
            {"type": "image", "url": "http://x/a.png"},
            {"type": "voice", "url": "http://x/a.amr"},
            {"type": "face", "id": "1"},
            {"type": "file", "url": "http://x/a.txt", "name": "a.txt"},
        ])
        self.assertEqual([item["type"] for item in media], ["image", "record", "face"])
        self.assertEqual(media[1]["data"]["file"], "http://x/a.amr")
        self.assertEqual(media[2]["data"]["id"], "1")


class BridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pcb-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    async def test_empty_directory_stays_dormant(self):
        bridge = PluginCoreBridge(self.tmp)
        status = await bridge.start_async()
        self.assertFalse(status["available"])
        self.assertFalse(status["started"])
        self.assertIsNone(await asyncio.to_thread(bridge.handle, {"text": "hi"}))

    async def test_hello_plugin_claims_and_replies(self):
        shutil.copytree(HELLO, self.tmp / "plugins" / "hello")
        bridge = PluginCoreBridge(self.tmp)
        status = await bridge.start_async()
        self.assertTrue(status["available"])
        self.assertEqual([p["id"] for p in status["plugins"]], ["hello"])
        record = await asyncio.to_thread(bridge.handle, {
            "id": "m1", "kind": "private", "target": "u1", "text": "hi",
            "sender_id": "u1", "sender_name": "小明",
        })
        self.assertTrue(record["bids"])
        self.assertEqual(record["sent"][0]["conversation"], "p:u1")
        self.assertIn("hello", record["sent"][0]["text"])
        self.assertTrue(bridge.available)
        await bridge.close_async()
        self.assertFalse(bridge.available)

    async def test_reload_picks_up_new_plugin(self):
        bridge = PluginCoreBridge(self.tmp)
        await bridge.start_async()
        self.assertFalse(bridge.available)
        shutil.copytree(HELLO, self.tmp / "plugins" / "hello")
        status = await bridge.reload_async()
        self.assertTrue(status["available"])
        self.assertEqual(len(status["plugins"]), 1)
        await bridge.close_async()

    async def test_install_directory_then_uninstall(self):
        source = self.tmp / "source" / "hello"
        shutil.copytree(HELLO, source)
        bridge = PluginCoreBridge(self.tmp / "pc")
        status = bridge.install(str(source))
        self.assertTrue(status["installed"])
        self.assertEqual(status["plugin"]["id"], "hello")
        self.assertEqual([p["id"] for p in status["plugins"]], ["hello"])
        self.assertTrue((self.tmp / "pc" / "plugins" / "hello" / "plugin.json").is_file())
        record = bridge.handle({
            "id": "m1", "kind": "private", "target": "u1", "text": "hi",
            "sender_id": "u1", "sender_name": "小明",
        })
        self.assertTrue(record["bids"])
        removed = bridge.uninstall("hello")
        self.assertTrue(removed["uninstalled"])
        self.assertFalse(removed["available"])
        self.assertEqual(removed["plugins"], [])
        self.assertFalse((self.tmp / "pc" / "plugins" / "hello").exists())

    async def test_install_zip_with_single_top_folder(self):
        archive = self.tmp / "hello.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for path in HELLO.rglob("*"):
                if path.is_file():
                    zf.write(path, "hello/" + path.relative_to(HELLO).as_posix())
        bridge = PluginCoreBridge(self.tmp / "pc")
        status = bridge.install(str(archive))
        self.assertTrue(status["installed"])
        self.assertEqual([p["id"] for p in status["plugins"]], ["hello"])
        self.assertTrue(bridge.available)
        bridge.close()

    async def test_install_rejects_zip_traversal(self):
        archive = self.tmp / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../evil.txt", "x")
        bridge = PluginCoreBridge(self.tmp / "pc")
        with self.assertRaisesRegex(ValueError, "不安全路径"):
            bridge.install(str(archive))
        self.assertFalse((self.tmp / "evil.txt").exists())

    async def test_install_requires_overwrite(self):
        source = self.tmp / "source" / "hello"
        shutil.copytree(HELLO, source)
        bridge = PluginCoreBridge(self.tmp / "pc")
        bridge.install(str(source))
        with self.assertRaisesRegex(ValueError, "已安装"):
            bridge.install(str(source))
        status = bridge.install(str(source), overwrite=True)
        self.assertTrue(status["installed"])
        bridge.close()


class FakeDB:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def commit(self):
        pass


class FakeTransport:
    def __init__(self):
        self.sent = []

    async def send(self, message, reply, meta=None):
        self.sent.append((dict(message), reply, meta))
        return {"ok": True}


class CoreRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, record):
        class Core:
            available = True

            def handle(self, message):
                return record

        bot = SimpleNamespace(
            plugin_core=Core(),
            db=FakeDB(),
            notes=[],
            decisions=[],
            transport=FakeTransport(),
        )
        bot.note = lambda text: bot.notes.append(text)
        bot.decision = lambda message, text: bot.decisions.append(text)
        bot._transport_for = lambda message: bot.transport
        bot._run_plugin_core = QQRuntime._run_plugin_core.__get__(bot, type(bot))
        return bot

    async def test_sends_text_and_media_then_marks_sent(self):
        record = {
            "bids": [{"plugin": "hello"}],
            "sent": [
                {"conversation": "g:g1", "text": "第一句", "media": []},
                {"conversation": "g:g1", "text": "", "media": [{"type": "image", "url": "http://x/a.png"}]},
            ],
        }
        bot = self.make_bot(record)
        ok = await bot._run_plugin_core("k1", {"kind": "group", "target": "old", "text": "hi"}, "old")
        self.assertTrue(ok)
        self.assertEqual(len(bot.transport.sent), 2)
        first, second = bot.transport.sent
        self.assertEqual(first[0]["target"], "g1")
        self.assertEqual(first[0]["kind"], "group")
        self.assertEqual(first[1], "第一句")
        self.assertEqual(second[2]["media"][0]["type"], "image")
        sql, params = bot.db.calls[-1]
        self.assertIn("state='sent'", sql)
        self.assertEqual(params[1], "k1")
        self.assertTrue(bot.decisions)

    async def test_no_bid_falls_through(self):
        bot = self.make_bot({"bids": [], "sent": []})
        ok = await bot._run_plugin_core("k1", {"kind": "private", "target": "u1", "text": "hi"}, "u1")
        self.assertFalse(ok)
        self.assertEqual(bot.transport.sent, [])
        self.assertEqual(bot.db.calls, [])

    async def test_send_failure_marks_uncertain(self):
        class BrokenTransport(FakeTransport):
            async def send(self, message, reply, meta=None):
                raise RuntimeError("boom")

        record = {"bids": [{"plugin": "hello"}], "sent": [{"conversation": "p:u1", "text": "x", "media": []}]}
        bot = self.make_bot(record)
        bot.transport = BrokenTransport()
        ok = await bot._run_plugin_core("k1", {"kind": "private", "target": "u1", "text": "hi"}, "u1")
        self.assertTrue(ok)
        sql, params = bot.db.calls[-1]
        self.assertIn("state='uncertain'", sql)
        self.assertEqual(params[2], "k1")


if __name__ == "__main__":
    unittest.main()
