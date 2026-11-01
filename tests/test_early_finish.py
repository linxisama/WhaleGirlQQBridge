"""提前收尾（early_finish）：JS 插件 early-finish 的规则移植 + 两处"省一次调用"的接线。

判定规则逐条照搬 ``lib/policy.js``：没发出去不收尾、有工具失败不收尾、预告式结尾不收尾、
预告要交东西却没交不收尾；其余情况才允许省掉这一轮的可选模型调用。
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402
from src.plugins.early_finish import EarlyFinish, decide_turn_end, digest_intent  # noqa: E402


class DecideTurnEndTests(unittest.TestCase):
    """规则矩阵：任何一条不满足都必须"不收尾"。"""

    def test_not_delivered_never_ends(self):
        self.assertEqual(decide_turn_end(delivered=False, text="你好呀"),
                         (False, '本批没有发送动作'))

    def test_clean_delivery_ends(self):
        should_end, reason = decide_turn_end(delivered=True, text="晚安，早点睡。")
        self.assertTrue(should_end)
        self.assertIn("已交付完毕", reason)

    def test_tool_failure_keeps_the_loop(self):
        should_end, reason = decide_turn_end(delivered=True, text="晚安。", tool_failed=True)
        self.assertFalse(should_end)
        self.assertIn("工具失败", reason)

    def test_preview_tail_keeps_the_loop(self):
        for text in ("我去看看", "等下告诉你", "待会儿说", "回头再告诉你", "我查一下"):
            should_end, reason = decide_turn_end(delivered=True, text=text)
            self.assertFalse(should_end, text)
            self.assertIn("预告", reason)

    def test_promise_without_asset_keeps_the_loop(self):
        should_end, reason = decide_turn_end(delivered=True, text="给你发张图", sent_asset=False)
        self.assertFalse(should_end)
        self.assertIn("没交", reason)

    def test_promise_with_asset_ends(self):
        should_end, _ = decide_turn_end(delivered=True, text="给你发张图", sent_asset=True)
        self.assertTrue(should_end)

    def test_asset_text_without_promise_ends(self):
        should_end, _ = decide_turn_end(delivered=True, text="喏，拿去。", sent_asset=True)
        self.assertTrue(should_end)


class DigestIntentTests(unittest.TestCase):
    def test_takes_segment_after_the_marker(self):
        text = "想了一下，她刚才说要去睡了。\n回法：那就道个晚安，别再追问了，明天再聊。"
        self.assertIn("那就道个晚安", digest_intent(text))

    def test_falls_back_to_last_meaningful_lines(self):
        text = "第一行是一句完整的话。\n第二行也是完整的一句话。\n短\n"
        self.assertIn("第二行", digest_intent(text))

    def test_empty_input_returns_empty(self):
        self.assertEqual(digest_intent(""), "")
        self.assertEqual(digest_intent(None), "")


class EarlyFinishPluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
        # 配一个"看起来已配置"的模型：接话判定的守卫要在"有没有模型"之后才轮到，
        # 这里不配的话判定会先返回"没有可用的模型配置"，测不到提前收尾那一段。
        (self.root / "config.qq.yaml").write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake-key", "model": "test-model"},
        }), encoding="utf-8")
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["early_finish"]
        self.bot.plugins.update_settings("early_finish", {"enabled": True})

    def tearDown(self):
        # 用 QQRuntime.close() 收尾：它会把 AI 客户端和 sqlite 一起关掉，
        # 只关 db 的话文件句柄还留着，临时目录删不掉（Windows 会报 WinError 32）。
        import asyncio
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def delivered(self, reply="晚安，早点睡。", media=None, failed=False, room="group:1"):
        message = {"id": "m1", "kind": "group", "target": room, "sender_id": "9", "text": "睡了吗"}
        if media:
            message["_reply_meta"] = {"media": [{"type": "image"}]}
        if failed:
            message["_error_feedback"] = True
        self.plugin.on_sent(message, reply, self.bot)
        return message

    def test_disabled_plugin_never_settles(self):
        self.bot.plugins.update_settings("early_finish", {"enabled": False})
        self.delivered()
        self.assertFalse(self.plugin.settled(self.bot, {"target": "group:1"}))

    def test_clean_reply_settles_and_allows_skipping(self):
        self.delivered()
        self.assertTrue(self.plugin.settled(self.bot, {"target": "group:1"}))
        self.assertFalse(self.plugin.allow_extra(self.bot, {"target": "group:1"}, "judge"))
        self.assertFalse(self.plugin.allow_extra(self.bot, {"target": "group:1"}, "reaction"))

    def test_dry_run_only_logs(self):
        self.bot.plugins.update_settings("early_finish", {"dryRun": True})
        self.delivered()
        self.assertFalse(self.plugin.settled(self.bot, {"target": "group:1"}))
        notes = [str(v.get("text", "")) for v in getattr(self.bot, "events", [])]
        self.assertTrue(any("试运行" in text for text in notes), "试运行要写一条日志")

    def test_per_switch_off_keeps_that_call(self):
        self.bot.plugins.update_settings("early_finish", {"skipJudge": False})
        self.delivered()
        self.assertTrue(self.plugin.allow_extra(self.bot, {"target": "group:1"}, "judge"))
        self.assertFalse(self.plugin.allow_extra(self.bot, {"target": "group:1"}, "reaction"))

    def test_failed_or_preview_reply_never_settles(self):
        self.delivered(failed=True)
        self.assertFalse(self.plugin.settled(self.bot, {"target": "group:1"}))
        self.delivered(reply="我先去看看")
        self.assertFalse(self.plugin.settled(self.bot, {"target": "group:1"}))
        self.delivered(reply="给你发张图")
        self.assertFalse(self.plugin.settled(self.bot, {"target": "group:1"}))
        self.delivered(reply="给你发张图", media=True)
        self.assertTrue(self.plugin.settled(self.bot, {"target": "group:1"}))

    def test_other_room_is_not_settled(self):
        self.delivered()
        self.assertFalse(self.plugin.settled(self.bot, {"target": "group:2"}))

    def test_stale_delivery_is_ignored(self):
        self.delivered()
        self.plugin._last["at"] -= 10_000
        self.assertFalse(self.plugin.settled(self.bot, {"target": "group:1"}))

    def test_context_chime_skips_its_judge_call(self):
        """接话判定：已交付完毕时不再花那次模型调用。"""
        import asyncio

        chime = self.bot.plugins._plugins["context_chime"]
        self.bot.plugins.update_settings("context_chime", {"enabled": True})
        self.delivered()
        message = {"id": "m2", "kind": "group", "target": "group:1", "sender_id": "9", "text": "在吗"}
        with mock.patch.object(self.bot.ai, "chat", new=mock.AsyncMock()) as chat:
            result = asyncio.run(chime._ask_judge_async(self.bot, message, [], "在吗"))
        chat.assert_not_awaited()
        self.assertIn("提前收尾", str(result.get("skipped")))
        self.assertTrue(result.get("block"))

    def test_context_chime_still_judges_when_not_settled(self):
        import asyncio

        chime = self.bot.plugins._plugins["context_chime"]
        self.bot.plugins.update_settings("context_chime", {"enabled": True})
        self.delivered(reply="我先去看看")            # 预告式结尾 → 不该省这次调用
        message = {"id": "m3", "kind": "group", "target": "group:1", "sender_id": "9", "text": "在吗"}
        with mock.patch.object(self.bot.ai, "chat",
                               new=mock.AsyncMock(return_value='{"reply":1,"confidence":90,"why":"该接"}')) as chat:
            result = asyncio.run(chime._ask_judge_async(self.bot, message, [], "在吗"))
        chat.assert_awaited()
        self.assertTrue(result.get("respond"))

    def test_direct_mention_is_never_skipped_by_the_gate(self):
        """@ 机器人的消息在判定钩子之前就返回了，永远不会被提前收尾挡住。"""
        import asyncio

        chime = self.bot.plugins._plugins["context_chime"]
        self.bot.plugins.update_settings("context_chime", {"enabled": True})
        self.delivered()
        message = {"id": "m4", "kind": "group", "target": "group:1", "sender_id": "9",
                   "text": "鲸鱼娘在吗", "at": True}
        self.assertIsNone(asyncio.run(chime.on_pre_ai_async(message, [], self.bot)))


if __name__ == "__main__":
    unittest.main()
