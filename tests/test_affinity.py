"""群友好感度：自动加分/衰减/等级/排行（移植自社区插件 affinity）。

来源：kondius.cn/qq-agent/plugin-market/ 的 ``affinity`` v1.1.0。
原版靠 LLM 工具查分数；本运行时没有工具调用，改成"把当前说话人的真实分数注入提示词
（易变说明）+ 确定性口令"，规则本身（加分、每日上限、衰减、主人满级、等级门槛）照搬。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import time
import unittest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402
from src.plugins.affinity import (  # noqa: E402
    DAY, DEFAULT_THRESHOLDS, LEVEL_TITLES, apply_decay, day_string, level_of, new_record,
    parse_thresholds, rank_of, top_members, touch_member,
)

BUILTIN = "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n"
NOW = time.time()


def settings(**overrides):
    base = {'perMessage': 1, 'atBotBonus': 3, 'replyBonus': 2, 'dailyCap': 20,
            'decayAfterDays': 7, 'decayPerDay': 1, 'ownerScore': 9999,
            'levelThresholds': '50,200,500,1000', 'announceLevelUp': True}
    base.update(overrides)
    return base


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    (root / "config.qq.yaml").write_text(yaml.safe_dump({
        "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
    }), encoding="utf-8")
    return root


class ThresholdTests(unittest.TestCase):
    def test_parses_and_validates(self):
        self.assertEqual(parse_thresholds("50,200,500,1000"), DEFAULT_THRESHOLDS)
        self.assertEqual(parse_thresholds("10，20、30 40"), (10, 20, 30, 40))
        self.assertEqual(parse_thresholds("100,50,200,500"), DEFAULT_THRESHOLDS, "必须递增")
        self.assertEqual(parse_thresholds("1,2,3"), DEFAULT_THRESHOLDS, "必须四个")
        self.assertEqual(parse_thresholds("a,b,c,d"), DEFAULT_THRESHOLDS)
        self.assertEqual(parse_thresholds(None), DEFAULT_THRESHOLDS)

    def test_level_titles_by_threshold(self):
        self.assertEqual(level_of(0)[1], '陌生')
        self.assertEqual(level_of(49)[1], '陌生')
        self.assertEqual(level_of(50)[1], '眼熟')
        self.assertEqual(level_of(199)[1], '眼熟')
        self.assertEqual(level_of(200)[1], '熟人')
        self.assertEqual(level_of(500)[1], '老友')
        self.assertEqual(level_of(1000)[1], '挚友')
        self.assertEqual(level_of(999999)[0], len(LEVEL_TITLES))

    def test_day_string_is_beijing_time(self):
        self.assertEqual(day_string(0), '1970-01-01')
        self.assertEqual(len(day_string(NOW)), 10)


class ScoringTests(unittest.TestCase):
    def test_message_and_bonuses(self):
        rec = new_record(NOW)
        self.assertEqual(touch_member(rec, NOW, settings()), 1)
        self.assertEqual(touch_member(rec, NOW, settings(), at_bot=True), 4)
        self.assertEqual(touch_member(rec, NOW, settings(), at_bot=True, reply_to_bot=True), 6)
        self.assertEqual(rec['score'], 11)
        self.assertEqual(rec['msgs'], 3)

    def test_daily_cap_blocks_farming(self):
        rec = new_record(NOW)
        for _ in range(50):
            touch_member(rec, NOW, settings(dailyCap=5))
        self.assertEqual(rec['score'], 5, "每天最多加 dailyCap 分")
        tomorrow = NOW + DAY
        touch_member(rec, tomorrow, settings(dailyCap=5))
        self.assertEqual(rec['score'], 6, "跨天后额度重置")

    def test_owner_is_pinned_and_never_decays(self):
        rec = new_record(NOW)
        touch_member(rec, NOW, settings(), owner=True)
        self.assertEqual(rec['score'], 9999)
        self.assertEqual(rec['level'], len(LEVEL_TITLES))
        self.assertTrue(rec['pinned'])
        self.assertEqual(apply_decay(rec, NOW + 30 * DAY, settings()), 0)

    def test_level_up_is_queued_for_announcement(self):
        rec = new_record(NOW)
        for _ in range(60):
            touch_member(rec, NOW, settings(dailyCap=100))
        self.assertEqual(rec['level'], 2)
        self.assertTrue(rec['pending'], "升级要排队给模型提一句")
        self.assertEqual(rec['pending'][-1]['title'], '眼熟')

    def test_announce_can_be_disabled(self):
        rec = new_record(NOW)
        for _ in range(60):
            touch_member(rec, NOW, settings(dailyCap=100, announceLevelUp=False))
        self.assertEqual(rec['pending'], [])

    def test_decay_after_idle_days_and_not_below_zero(self):
        rec = new_record(NOW)
        for _ in range(60):
            touch_member(rec, NOW, settings(dailyCap=100))
        score = rec['score']
        apply_decay(rec, NOW + 10 * DAY, settings())
        self.assertEqual(rec['score'], score - 3, "超过 7 天后每天扣 1（10 天 → 扣 3）")
        apply_decay(rec, NOW + 10 * DAY, settings())
        self.assertEqual(rec['score'], score - 3, "同一段时间不重复扣")
        apply_decay(rec, NOW + 400 * DAY, settings(decayPerDay=50))
        self.assertEqual(rec['score'], 0, "不会扣成负数")

    def test_rank_and_top(self):
        members = {}
        for index, score in enumerate([10, 300, 100]):
            rec = new_record(NOW)
            rec['score'] = score
            rec['name'] = f'群友{index}'
            members[f'u{index}'] = rec
        # 分数 300/100/10 → u1 第 1、u2 第 2、u0 第 3
        self.assertEqual(rank_of(members, 'u1'), 1)
        self.assertEqual(rank_of(members, 'u2'), 2)
        self.assertEqual(rank_of(members, 'u0'), 3)
        self.assertEqual(rank_of(members, 'nobody'), 0)
        self.assertEqual([key for key, _ in top_members(members, 2)], ['u1', 'u2'])


class AffinityPluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["affinity"]
        self.bot.plugins.update_settings("affinity", {"enabled": True, "ownerQq": "10001"})
        self.bot.plugins.update_settings("chat_controls", {"enabled": True, "aliases": "鲸鱼娘"})

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def message(self, text="你好", sender="20002", **extra):
        data = {"id": f"m{time.time()}", "kind": "group", "target": "123",
                "sender_id": sender, "sender_name": "小鱼", "text": text}
        data.update(extra)
        return data

    def notes(self):
        return [str(v.get("text", "")) for v in getattr(self.bot, "events", [])]

    def hints_note(self, message):
        """取提示词说明：装了「缓存稳定性」时进 volatile_system，否则进 extra_system。"""
        hints = self.plugin.on_pre_ai(message, [], self.bot) or {}
        return hints.get("volatile_system") or hints.get("extra_system") or ""

    def test_scoring_through_ingest_and_prompt_note(self):
        self.plugin.on_ingest(self.message("在吗"), self.bot)
        self.plugin.on_ingest(self.message("鲸鱼娘在吗", at=True), self.bot)
        scope = "group:123"
        rec = self.plugin.data()["members"][scope]["20002"]
        self.assertEqual(rec["score"], 1 + 4, "发言 1 分 + @ 机器人 3 分")
        note = self.hints_note(self.message("再来一句"))
        self.assertIn("【好感度】", note)
        self.assertIn("分", note)

    def test_reply_bonus_within_window(self):
        self.plugin.on_sent(self.message(), "好", self.bot)
        self.plugin.on_ingest(self.message("接着说"), self.bot)
        rec = self.plugin.data()["members"]["group:123"]["20002"]
        self.assertEqual(rec["score"], 1 + 2, "刚回过话 → 接话加分")

    def test_owner_is_max_level_and_independent(self):
        self.plugin.on_ingest(self.message("主人的话", sender="10001"), self.bot)
        rec = self.plugin.data()["members"]["group:123"]["10001"]
        self.assertEqual(rec["score"], 9999)
        self.assertEqual(rec["level"], len(LEVEL_TITLES))

    def test_backfill_does_not_double_count(self):
        for index in range(5):
            payload = {"id": f"old{index}", "kind": "group", "target": "123",
                       "sender_id": "30003", "sender_name": "阿豆", "text": "旧消息"}
            self.bot.db.execute(
                "INSERT INTO inbox(key,mode,account,room,payload,state,created)"
                " VALUES(?,?,?,?,?,'sent',?)",
                (f"bf{index}", self.bot.mode, self.bot.account(), "group:123",
                 json.dumps(payload, ensure_ascii=False), time.time() - 100 + index))
        self.bot.db.commit()
        self.plugin.on_ingest(self.message("新消息"), self.bot)
        first = self.plugin.data()["members"]["group:123"]["30003"]["score"]
        self.assertGreaterEqual(first, 5)
        self.plugin.on_ingest(self.message("又来"), self.bot)
        second = self.plugin.data()["members"]["group:123"]["30003"]["score"]
        self.assertEqual(second, first, "补扫过的消息不该再计一次")

    def test_backfill_is_throttled_per_room(self):
        """刷屏时不该每条消息都扫一次库：默认同一会话 30 秒最多补扫一次。"""
        calls = []
        original = self.plugin.backfill

        def spy(message, runtime, since=0.0):
            calls.append(message.get("id"))
            return original(message, runtime, since=since)

        self.plugin.backfill = spy
        try:
            self.plugin.on_ingest(self.message("第一句"), self.bot)
            self.plugin.on_ingest(self.message("第二句"), self.bot)
            self.plugin.on_ingest(self.message("第三句"), self.bot)
            self.assertEqual(len(calls), 1, "窗口内只补扫一次")
            self.bot.plugins.update_settings("affinity", {"backfillSeconds": 0})
            self.plugin.on_ingest(self.message("第四句"), self.bot)
            self.assertEqual(len(calls), 2, "把节流关掉就每次都扫")
        finally:
            self.plugin.backfill = original

    def test_scoring_still_happens_while_backfill_is_throttled(self):
        self.plugin.on_ingest(self.message("第一句"), self.bot)
        self.plugin.on_ingest(self.message("第二句"), self.bot)
        rec = self.plugin.data()["members"]["group:123"]["20002"]
        self.assertEqual(rec["score"], 2, "节流只影响补扫，不影响实时计分")

    def test_command_self_and_rank(self):
        self.plugin.on_ingest(self.message("聊两句"), self.bot)
        reply = self.plugin.on_ingest(self.message("好感度"), self.bot)
        self.assertIn("你的好感度", reply)
        reply = self.plugin.on_ingest(self.message("好感度 排行"), self.bot)
        self.assertIn("好感度排行", reply)

    def test_reset_requires_owner(self):
        self.plugin.on_ingest(self.message("你好"), self.bot)
        reply = self.plugin.on_ingest(self.message("好感度 重置"), self.bot)
        self.assertIn("只有主人", reply)
        reply = self.plugin.on_ingest(self.message("好感度 重置", sender="10001"), self.bot)
        self.assertIn("清零", reply)

    def test_tune_only_owner_and_only_known_keys(self):
        reply = self.plugin.on_ingest(self.message("好感度 设置 perMessage=2"), self.bot)
        self.assertIn("只有主人", reply)
        reply = self.plugin.on_ingest(
            self.message("好感度 设置 perMessage=2,dailyCap=50,nope=1", sender="10001"), self.bot)
        self.assertIn("perMessage=2", reply)
        self.assertIn("dailyCap=50", reply)
        self.assertIn("没认出来：nope", reply)
        self.assertEqual(self.plugin.get("perMessage"), 2)
        listing = self.plugin.on_ingest(self.message("好感度 设定", sender="10001"), self.bot)
        self.assertIn("perMessage=2", listing)

    def test_level_up_is_mentioned_once(self):
        self.bot.plugins.update_settings("affinity", {"dailyCap": 100})
        for _ in range(60):
            self.plugin.on_ingest(self.message("刷一刷"), self.bot)
        first = self.hints_note(self.message("在吗"))
        self.assertIn("刚刚升到", first)
        second = self.hints_note(self.message("在吗"))
        self.assertNotIn("刚刚升到", second, "提过一次就不再重复")

    def test_group_scores_feed_affinity_proactive(self):
        self.plugin.on_ingest(self.message("聊两句"), self.bot)
        self.assertEqual(self.plugin.chat_scores().get("group:123"), 1)
        proactive = self.bot.plugins._plugins["affinity_proactive"]
        self.assertEqual(proactive.targets().get("group:123"), 1.0,
                         "主动发言插件应直接吃实时分数，不用手填")

    def test_disabled_plugin_does_nothing(self):
        self.bot.plugins.update_settings("affinity", {"enabled": False})
        self.assertIsNone(self.plugin.on_ingest(self.message("你好"), self.bot))
        self.assertIsNone(self.plugin.on_pre_ai(self.message("你好"), [], self.bot))
        self.assertEqual(self.plugin.data()["members"], {})

    def test_command_is_ignored_in_private_when_not_addressed(self):
        self.assertIsNone(self.plugin.on_ingest(self.message("随便说说"), self.bot))


if __name__ == "__main__":
    unittest.main()
