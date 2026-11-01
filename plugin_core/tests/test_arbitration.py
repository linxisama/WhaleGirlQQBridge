"""仲裁规则：优先级、声明序、silent 同场、attach 串行与失败隔离。"""

from __future__ import annotations

import unittest

from plugin_core.tests import support as s


class ArbitrationTest(s.KernelTestCase):
    def test_priority_order_and_drop_reason(self):
        s.write_plugin(self.plugins, "low", s.main_source("低", 20), capabilities=[s.main_capability(20)])
        s.write_plugin(self.plugins, "high", s.main_source("高", 90), capabilities=[s.main_capability(90)])
        s.write_plugin(self.plugins, "mid", s.main_source("中", 50), capabilities=[s.main_capability(50)])
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        record = host.handle(self.message())

        self.assertEqual(
            {key: record["winner"][key] for key in ("plugin", "produces", "priority")},
            {"plugin": "high", "produces": "main", "priority": 90},
        )
        self.assertEqual(record["winner"]["reason"], "测试主体")
        self.assertEqual(record["generated"], ["high"])
        self.assertEqual([item["text"] for item in record["sent"]], ["高"])
        by_plugin = {item["plugin"]: item for item in record["dropped"]}
        self.assertEqual(set(by_plugin), {"low", "mid"})
        for plugin_id in ("low", "mid"):
            self.assertEqual(by_plugin[plugin_id]["reason"], "priority-low")
            self.assertEqual(by_plugin[plugin_id]["winner_priority"], 90)
        self.assertEqual(by_plugin["mid"]["priority"], 50)
        self.assertEqual(by_plugin["mid"]["label"], "优先级低")
        self.assertEqual(len(transport.history), 1)

    def test_manifest_declaration_order_wins_tie(self):
        # aaa 把 main 写在第二个 capability（下标 1），zzz 写在第一个（下标 0）
        s.write_plugin(
            self.plugins,
            "aaa",
            s.main_source("aaa", 50),
            capabilities=[
                {"stage": "inbound", "produces": "attach", "priority": 70, "requires_main": True},
                s.main_capability(50),
            ],
        )
        s.write_plugin(self.plugins, "zzz", s.main_source("zzz", 50), capabilities=[s.main_capability(50)])
        host = self.build_host()
        record = host.handle(self.message())
        self.assertEqual(record["winner"]["plugin"], "zzz")
        self.assertEqual([item["text"] for item in record["sent"]], ["zzz"])

    def test_declaration_order_beats_id_and_load_order(self):
        """契约 PLUGIN-CONTRACT.md:139 / DECISIONS.md D7：同优先级先比 capabilities 声明序，再按 id 兜底。

        两个 main 同 priority=50，且目录名/加载序（= id 字典序）与声明序相反：
        aaa-main 把 main 声明在 capabilities 下标 1，zzz-main 声明在下标 0。
        契约要求 zzz-main 胜出；若把声明序错当成目录名/加载序，胜者会变成 aaa-main。
        """
        s.write_plugin(
            self.plugins,
            "aaa-main",
            s.main_source("aaa", 50),
            capabilities=[
                {"stage": "inbound", "produces": "attach", "priority": 70, "requires_main": True},
                s.main_capability(50),
            ],
        )
        s.write_plugin(self.plugins, "zzz-main", s.main_source("zzz", 50), capabilities=[s.main_capability(50)])
        transport = s.MockTransport()
        host = self.build_host(transport=transport)

        entries, _ = host.arbiter._collect(self.message())
        mains = {entry.plugin.id: entry for entry in entries if entry.bid.produces == "main"}
        self.assertEqual(set(mains), {"aaa-main", "zzz-main"})
        for plugin_id in ("aaa-main", "zzz-main"):
            entry = mains[plugin_id]
            print(
                "  同优先级参战：id=%r order=%d cap_index=%d（目录名/加载序 aaa-main -> zzz-main）"
                % (entry.plugin.id, entry.order, entry.cap_index)
            )
        self.assertGreater(mains["zzz-main"].plugin.id, mains["aaa-main"].plugin.id, "前提：字典序与声明序相反")
        self.assertEqual(mains["aaa-main"].cap_index, 1)
        self.assertEqual(mains["zzz-main"].cap_index, 0)

        record = host.handle(self.message())
        self.assertEqual(record["winner"]["plugin"], "zzz-main")
        self.assertEqual(record["winner"]["priority"], 50)
        self.assertEqual([item["text"] for item in record["sent"]], ["zzz"])
        self.assertEqual(transport.sent_texts(), ["zzz"])

    def test_id_breaks_remaining_tie(self):
        for plugin_id in ("beta", "alpha"):
            s.write_plugin(self.plugins, plugin_id, s.main_source(plugin_id, 50), capabilities=[s.main_capability(50)])
        host = self.build_host()
        record = host.handle(self.message())
        self.assertEqual(record["winner"]["plugin"], "alpha")

    def test_silent_beats_main_and_sends_nothing(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 80), capabilities=[s.main_capability(80)])
        s.write_plugin(self.plugins, "hush", s.silent_source(90), capabilities=[s.silent_capability(90)])
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        record = host.handle(self.message())

        self.assertIsNone(record["winner"])
        self.assertEqual(
            {key: record["silent_winner"][key] for key in ("plugin", "produces", "priority")},
            {"plugin": "hush", "produces": "silent", "priority": 90},
        )
        self.assertEqual(record["generated"], [])
        self.assertEqual(record["sent"], [])
        self.assertEqual(transport.history, [])
        dropped = {item["plugin"]: item for item in record["dropped"]}
        self.assertEqual(dropped["talk"]["reason"], "silent-over-main")
        self.assertEqual(dropped["talk"]["winner_priority"], 90)
        self.assertEqual(dropped["talk"]["label"], "沉默优先")

    def test_silent_lower_priority_loses_to_main(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 80), capabilities=[s.main_capability(80)])
        s.write_plugin(self.plugins, "hush", s.silent_source(10), capabilities=[s.silent_capability(10)])
        host = self.build_host()
        record = host.handle(self.message())
        self.assertEqual(record["winner"]["plugin"], "talk")
        self.assertIsNone(record["silent_winner"])
        dropped = {item["plugin"]: item for item in record["dropped"]}
        self.assertEqual(dropped["hush"]["reason"], "priority-low")

    def test_attach_runs_after_main_in_priority_order(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 20), capabilities=[s.main_capability(20)])
        s.write_plugin(self.plugins, "attach-low", s.attach_source("附加低", 30), capabilities=[s.attach_capability(30)])
        s.write_plugin(self.plugins, "attach-high", s.attach_source("附加高", 70), capabilities=[s.attach_capability(70)])
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        record = host.handle(self.message())

        self.assertEqual(record["winner"]["plugin"], "talk")
        self.assertEqual(record["attached"], ["attach-high", "attach-low"])
        self.assertEqual([item["text"] for item in record["sent"]], ["主体", "附加高", "附加低"])
        self.assertEqual(transport.sent_texts(), ["主体", "附加高", "附加低"])

    def test_attach_failure_does_not_break_main(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 20), capabilities=[s.main_capability(20)])
        s.write_plugin(self.plugins, "bad", s.failing_attach_source(90), capabilities=[s.attach_capability(90)])
        s.write_plugin(self.plugins, "ok", s.attach_source("附加", 10), capabilities=[s.attach_capability(10)])
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        record = host.handle(self.message())

        self.assertEqual([item["text"] for item in record["sent"]], ["主体", "附加"])
        self.assertEqual(record["attached"], ["bad", "ok"])
        failed = host.audit.select(event="attach-failed")
        self.assertEqual([item["plugin"] for item in failed], ["bad"])
        self.assertIn("RuntimeError", failed[0]["detail"])
        self.assertEqual(host.get_plugin("talk").enabled, True)

    def test_attach_without_main_is_dropped(self):
        s.write_plugin(self.plugins, "only-attach", s.attach_source("附加", 70), capabilities=[s.attach_capability(70)])
        transport = s.MockTransport()
        host = self.build_host(transport=transport, default_persona=False)
        record = host.handle(self.message())

        self.assertIsNone(record["winner"])
        self.assertEqual(record["sent"], [])
        self.assertEqual(transport.history, [])
        self.assertEqual(record["dropped"][0]["reason"], "requires-main")
        self.assertEqual(record["dropped"][0]["label"], "缺少主体")

    def test_no_bid_writes_record_and_sends_nothing(self):
        s.write_plugin(
            self.plugins,
            "quiet",
            "def setup(api) -> None:\n    api.declare_inbound(lambda ctx: None)\n",
            capabilities=[s.main_capability(50)],
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport, default_persona=False)
        record = host.handle(self.message())

        self.assertEqual(record["event"], "arbitration")
        self.assertEqual(record["bids"], [])
        self.assertEqual(record["sent"], [])
        self.assertEqual(record["dropped"][0]["reason"], "no-trigger")
        self.assertEqual(record["dropped"][0]["label"], "未命中触发条件")
        self.assertEqual(transport.history, [])
        written = host.audit.select(event="arbitration")
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["message_id"], record["message_id"])

    def test_plugin_without_read_permission_never_bids(self):
        s.write_plugin(
            self.plugins,
            "blind",
            s.main_source("不该出现", 99),
            permissions=["send_message"],
            capabilities=[s.main_capability(99)],
        )
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 10), capabilities=[s.main_capability(10)])
        host = self.build_host(default_persona=False)
        record = host.handle(self.message())

        self.assertEqual(record["winner"]["plugin"], "talk")
        self.assertEqual([item["text"] for item in record["sent"]], ["主体"])
        skipped = host.audit.select(event="bid-skipped")
        self.assertEqual([item["plugin"] for item in skipped], ["blind"])
        self.assertEqual(skipped[0]["reason"], "no-read-permission")


if __name__ == "__main__":
    unittest.main()
