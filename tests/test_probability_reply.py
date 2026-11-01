"""probability_reply（戳一戳概率回应）的离线测试。

核心审核修正的真问题：它原来只检查文本里有没有「戳了戳」，而本项目传输层
（`qq_transports.onebot_notice`）给的是**语义化**的
``{"notice": "poke", "text": "", "notice_data": {...}}`` ——
所以这个插件在本项目里**永远不会触发**。现在两种形式都认。

另外冷却改为按「谁戳的」（`notice_data.poker`）计算：否则同一人换个会话标识就能绕过。
"""

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from src.plugins.probability_reply import ProbabilityReply


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(ProbabilityReply.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


def poke_notice(poker="7", poked="999", group="12345"):
    """传输层产出的拍一拍结构（见 qq_transports.onebot_notice）。"""
    return {
        "id": "t:poke:999:7",
        "kind": "group",
        "target": group,
        "text": "",
        "sender": poker,
        "at": False,
        "notice": "poke",
        "notice_data": {"poked": poked, "poker": poker, "group": group},
    }


class PokeDetectionTests(unittest.TestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        return ProbabilityReply(FakeManager(tmp.name, settings))

    def test_semantic_notice_is_recognised(self):
        # 这是本项目的真实形式，修之前它返回 False
        self.assertTrue(self.make()._is_poke(poke_notice()))

    def test_legacy_text_form_still_recognised(self):
        plugin = self.make()
        self.assertTrue(plugin._is_poke({"text": "小明戳了戳你"}))
        self.assertTrue(plugin._is_poke({"text": "[poke] 有人戳你"}))
        self.assertTrue(plugin._is_poke({"text": "", "poke_type": 1}))

    def test_normal_message_is_not_poke(self):
        plugin = self.make()
        self.assertFalse(plugin._is_poke({"text": "你好", "notice_data": {}}))
        self.assertFalse(plugin._is_poke(None))
        self.assertFalse(plugin._is_poke({}))

    def test_other_notices_are_not_poke(self):
        plugin = self.make()
        self.assertFalse(plugin._is_poke({"notice": "recall", "text": ""}))
        self.assertFalse(plugin._is_poke({"notice": "member_in", "text": ""}))


class PokerIdTests(unittest.TestCase):
    def test_uses_notice_data_poker(self):
        self.assertEqual(ProbabilityReply._poker_id(poke_notice(poker="42")), "42")

    def test_falls_back_to_sender_id(self):
        self.assertEqual(ProbabilityReply._poker_id({"sender_id": "9"}), "9")

    def test_blank_poker_falls_back(self):
        message = {"sender_id": "9", "notice_data": {"poker": ""}}
        self.assertEqual(ProbabilityReply._poker_id(message), "9")

    def test_garbage(self):
        self.assertEqual(ProbabilityReply._poker_id({}), "")
        self.assertEqual(ProbabilityReply._poker_id({"notice_data": "x", "sender_id": "3"}), "3")


class BehaviourTests(unittest.TestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        return ProbabilityReply(FakeManager(tmp.name, settings))

    def test_disabled_returns_none(self):
        plugin = self.make(enabled=False)
        self.assertIsNone(plugin.on_ingest(poke_notice(), None))

    def test_ignore_pokes_returns_empty(self):
        plugin = self.make(ignorePokes=True)
        self.assertEqual(plugin.on_ingest(poke_notice(), None), "")

    def test_reply_percent_100_lets_ai_handle(self):
        plugin = self.make(replyPercent=100, pokeBackPercent=0, cooldownSec=0)
        self.assertIsNone(plugin.on_ingest(poke_notice(), None))

    def test_reply_percent_0_pokeback_100_pokes_back(self):
        plugin = self.make(replyPercent=0, pokeBackPercent=100, cooldownSec=0)
        self.assertEqual(plugin.on_ingest(poke_notice(), None), "[poke_back]")

    def test_both_zero_ignores(self):
        plugin = self.make(replyPercent=0, pokeBackPercent=0, cooldownSec=0)
        self.assertEqual(plugin.on_ingest(poke_notice(), None), "")

    def test_cooldown_blocks_second_poke(self):
        plugin = self.make(replyPercent=100, cooldownSec=3600)
        plugin.on_ingest(poke_notice(), None)
        self.assertEqual(plugin.on_ingest(poke_notice(), None), "")

    def test_cooldown_is_per_poker(self):
        plugin = self.make(replyPercent=100, cooldownSec=3600)
        plugin.on_ingest(poke_notice(poker="7"), None)
        # 换个人戳：不该被前一个人的冷却挡住
        self.assertIsNone(plugin.on_ingest(poke_notice(poker="8"), None))

    def test_burst_detection_replies(self):
        # burstEnabled 时前几次仍会走概率分支（这里设成必定反戳），
        # 攒够 burstCount 的那一次改回"别戳了"
        plugin = self.make(burstEnabled=True, burstCount=3, burstWindowSec=60,
                           cooldownSec=0, replyPercent=0, pokeBackPercent=100)
        self.assertEqual(plugin.on_ingest(poke_notice(), None), "[poke_back]")
        self.assertEqual(plugin.on_ingest(poke_notice(), None), "[poke_back]")
        self.assertEqual(plugin.on_ingest(poke_notice(), None), "好了好了别戳了！")

    def test_burst_disabled_never_bursts(self):
        plugin = self.make(burstEnabled=False, replyPercent=0, pokeBackPercent=0, cooldownSec=0)
        for _ in range(10):
            self.assertEqual(plugin.on_ingest(poke_notice(), None), "")

    def test_non_poke_message_ignored(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest({"text": "你好"}, None))

    def test_description_documents_the_fix(self):
        self.assertIn("notice", ProbabilityReply.META.description)


if __name__ == "__main__":
    unittest.main()
