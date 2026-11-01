"""群友花费统计：区间解析 + 按触发人汇总 + 报表（移植自社区插件 group-cost-report）。

来源：kondius.cn/qq-agent/plugin-market/ 的 ``group-cost-report`` v1.0.0。
核心差异：原版靠模型调工具发报告，我们是**规则触发、立即回复、零 token**；
原版复用它们的模型价目表，我们提供可选单价（默认只报 token 不折算金额）。
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

from src.plugins.group_cost_report import (  # noqa: E402
    DAY, build_report, cost_of, fmt_tokens, parse_range, usage_of,
)
from src.qq_app import QQRuntime  # noqa: E402

BUILTIN = "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n"
# 2026-10-15 12:00 北京时间
NOW = time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, -1)) - 8 * 3600


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    (root / "config.qq.yaml").write_text(yaml.safe_dump({
        "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
    }), encoding="utf-8")
    return root


def row(sender="20002", name="小鱼", prompt=1000, completion=200, cached=0):
    return json.dumps({"sender_id": sender, "sender_name": name,
                       "_usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                  "cached_tokens": cached, "total_tokens": prompt + completion}},
                      ensure_ascii=False)


class ParseRangeTests(unittest.TestCase):
    def test_named_ranges(self):
        self.assertEqual(parse_range("金额统计", now=NOW)["key"], "today")
        self.assertEqual(parse_range("金额统计 昨天", now=NOW)["key"], "yesterday")
        self.assertEqual(parse_range("金额统计 本周", now=NOW)["key"], "thisweek")
        self.assertEqual(parse_range("金额统计 上月", now=NOW)["key"], "lastmonth")
        self.assertEqual(parse_range("金额统计 最近7天", now=NOW)["key"], "7d")
        self.assertEqual(parse_range("金额统计 最近 12 天", now=NOW)["key"], "12d")
        self.assertEqual(parse_range("金额统计 全部", now=NOW)["key"], "all")

    def test_explicit_dates(self):
        span = parse_range("金额统计 9月1日到9月30日", now=NOW)
        self.assertEqual(span["key"], "range")
        self.assertEqual(span["label"], "09-01 到 09-30")
        self.assertAlmostEqual(span["end"] - span["start"], 30 * DAY - 1, delta=2)

    def test_full_dates_and_default_fallback(self):
        span = parse_range("统计 2026-09-01 到 2026-09-03", now=NOW)
        self.assertEqual(span["label"], "09-01 到 09-03")
        self.assertEqual(parse_range("乱写的", default_key="lastweek", now=NOW)["key"], "lastweek")
        self.assertEqual(parse_range("", default_key="不存在", now=NOW)["key"], "today")

    def test_week_boundaries_start_on_monday(self):
        span = parse_range("本周", now=NOW)
        self.assertEqual(time.strftime('%m-%d', time.localtime(span["start"] + 8 * 3600)), "10-12")


class FormattingTests(unittest.TestCase):
    def test_tokens_and_cost(self):
        self.assertEqual(fmt_tokens(999), "999")
        self.assertEqual(fmt_tokens(1500), "1.5k")
        self.assertEqual(fmt_tokens(2500000), "2.50M")
        self.assertEqual(cost_of(1000000, 0, 2.0, 8.0), 2.0)
        self.assertEqual(cost_of(0, 1000000, 2.0, 8.0), 8.0)
        self.assertEqual(cost_of(1000, 1000, 0, 0), 0.0)

    def test_usage_of_ignores_rows_without_usage(self):
        self.assertEqual(usage_of("{}"), {})
        self.assertEqual(usage_of("not json"), {})
        self.assertEqual(usage_of(json.dumps({"sender_id": "1"}))["prompt"] if usage_of(
            json.dumps({"sender_id": "1"})) else {}, {})


class ReportTests(unittest.TestCase):
    def test_report_groups_by_trigger_person(self):
        rows = [(row("20002", "小鱼", 1000, 200), 0), (row("20002", "小鱼", 500, 100), 0),
                (row("30003", "阿豆", 2000, 300), 0), (json.dumps({"sender_id": "9"}), 0)]
        text = build_report(rows, label="10-15 到 10-15", top_n=8)
        self.assertIn("本群花费统计（10-15 到 10-15）", text)
        self.assertIn("合计 3 次运行", text)
        self.assertIn("1. 阿豆：1 次 · 2.3k token", text)
        self.assertIn("2. 小鱼：2 次 · 1.8k token", text)
        self.assertIn("口径", text)

    def test_report_merges_the_tail_and_hides_money_without_price(self):
        import re as _re
        rows = [(row(f"u{index}", f"群友{index}", 100 * (index + 1), 0), 0) for index in range(12)]
        text = build_report(rows, label="最近7天", top_n=3)
        listed = _re.findall(r'^\d+\. 群友\d+：', text, _re.M)
        self.assertEqual(len(listed), 3, text)
        self.assertIn("其余 9 人合计", text)
        self.assertNotIn("¥", text, "没填单价就不该出现金额")

    def test_report_with_price_shows_money(self):
        rows = [(row("20002", "小鱼", 1000000, 1000000), 0)]
        text = build_report(rows, label="今天", price_in=2.0, price_out=8.0)
        self.assertIn("¥10.0000", text)

    def test_empty_range_is_explained(self):
        text = build_report([], label="今天")
        self.assertIn("没有可统计的运行记录", text)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["group_cost_report"]
        self.bot.plugins.update_settings("group_cost_report", {"enabled": True})

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def insert(self, payload, created=None):
        self.bot.db.execute(
            "INSERT INTO inbox(key,mode,account,room,payload,state,created)"
            " VALUES(?,?,?,?,?,'sent',?)",
            (f"k{time.time_ns()}", self.bot.mode, self.bot.account(), "group:123", payload,
             float(created if created is not None else time.time())))
        self.bot.db.commit()

    def message(self, text="金额统计", at=True):
        return {"id": "m1", "kind": "group", "target": "123", "sender_id": "20002",
                "sender_name": "小鱼", "text": text, "at": at}

    def test_reply_is_built_without_any_model_call(self):
        self.insert(row("20002", "小鱼", 1200, 300))
        reply = self.plugin.on_ingest(self.message("金额统计 今天"), self.bot)
        self.assertIn("本群花费统计", reply)
        self.assertIn("小鱼", reply)

    def test_requires_at_unless_configured(self):
        self.insert(row("20002", "小鱼"))
        self.assertIsNone(self.plugin.on_ingest(self.message("金额统计", at=False), self.bot))
        self.bot.plugins.update_settings("group_cost_report", {"allowWithoutAt": True})
        self.assertIsNotNone(self.plugin.on_ingest(self.message("金额统计", at=False), self.bot))

    def test_private_and_disabled_and_other_text_are_ignored(self):
        message = self.message("金额统计")
        message["kind"] = "private"
        self.assertIsNone(self.plugin.on_ingest(message, self.bot))
        self.assertIsNone(self.plugin.on_ingest(self.message("今天天气不错"), self.bot))
        self.bot.plugins.update_settings("group_cost_report", {"enabled": False})
        self.assertIsNone(self.plugin.on_ingest(self.message("金额统计"), self.bot))

    def test_range_filters_by_time(self):
        self.insert(row("20002", "小鱼", 1000, 0), created=time.time() - 3 * DAY)
        self.insert(row("30003", "阿豆", 2000, 0), created=time.time() - 60)
        today = self.plugin.on_ingest(self.message("金额统计"), self.bot)
        self.assertIn("阿豆", today)
        self.assertNotIn("小鱼", today)
        week = self.plugin.on_ingest(self.message("金额统计 最近7天"), self.bot)
        self.assertIn("小鱼", week)
        self.assertIn("阿豆", week)

    def test_reply_records_usage_for_the_trigger_person(self):
        """真回复路径：生成之后把本轮用量写回该条消息（报表的数据来源）。"""
        from tests.test_chat_features import message as chat_message

        self.bot.running = True
        self.bot.official.authenticated = True
        self.bot.official.send = unittest.mock.AsyncMock()
        self.bot.ai.chat = unittest.mock.AsyncMock(return_value="好")

        async def fake_chat(*args, **kwargs):
            self.bot.ai.usage.add(prompt=1234, completion=56, cached=1000)
            return "好"

        self.bot.ai.chat = unittest.mock.AsyncMock(side_effect=fake_chat)

        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.bot.ingest(chat_message(mid="cost1", text="在吗"), "official")
            for _ in range(120):
                row = self.bot.db.execute(
                    "SELECT payload FROM inbox WHERE state='sent' LIMIT 1").fetchone()
                if row:
                    return row[0]
                await asyncio.sleep(0.05)
            return ""

        payload = asyncio.run(run())
        self.assertIn("_usage", payload)
        usage = usage_of(payload)
        self.assertEqual(usage["prompt"], 1234)
        self.assertEqual(usage["completion"], 56)
        self.assertEqual(usage["cached"], 1000)


if __name__ == "__main__":
    unittest.main()
