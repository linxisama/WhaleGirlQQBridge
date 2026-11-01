"""出站管道：优先级顺序、短路、防回灌、权限跳过、异常隔离。"""

from __future__ import annotations

import unittest

from plugin_core.tests import support as s


class OutboundTest(s.KernelTestCase):
    def _talk(self, priority=20, text="原始正文"):
        s.write_plugin(self.plugins, "talk", s.main_source(text, priority), capabilities=[s.main_capability(priority)])

    def test_priority_order_is_ascending(self):
        self._talk()
        s.write_plugin(
            self.plugins, "mw-late", s.outbound_source("|late"), capabilities=[s.outbound_capability(20)]
        )
        s.write_plugin(
            self.plugins, "mw-early", s.outbound_source("|early"), capabilities=[s.outbound_capability(10)]
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        host.handle(self.message())
        self.assertEqual(transport.sent_texts(), ["原始正文|early|late"])
        self.assertEqual(host.outbound.passes, {"mw-early": 1, "mw-late": 1})

    def test_cancel_short_circuits_remaining_middleware(self):
        self._talk()
        s.write_plugin(
            self.plugins, "mw-cancel", s.OUTBOUND_CANCEL_SOURCE, capabilities=[s.outbound_capability(5, "cancel")]
        )
        s.write_plugin(
            self.plugins, "mw-late", s.outbound_source("|late"), capabilities=[s.outbound_capability(20)]
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        host.handle(self.message())

        self.assertEqual(transport.history, [])
        self.assertEqual(host.outbound.passes, {"mw-cancel": 1})
        cancelled = host.audit.select(event="outbound-cancelled")
        self.assertEqual([item["plugin"] for item in cancelled], ["mw-cancel"])

    def test_rewrite_is_not_fed_back(self):
        self._talk(text="正文")
        s.write_plugin(
            self.plugins, "mw-twice", s.outbound_source("X"), capabilities=[s.outbound_capability(10)]
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        host.handle(self.message())
        # 只跑一遍：若回灌就会变成 正文XX
        self.assertEqual(transport.sent_texts(), ["正文X"])
        self.assertEqual(host.outbound.passes.get("mw-twice"), 1)

    def test_middleware_without_send_permission_is_skipped(self):
        self._talk()
        s.write_plugin(
            self.plugins,
            "mw-blind",
            s.outbound_source("|blind"),
            permissions=["read_message"],
            capabilities=[s.outbound_capability(10)],
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        host.handle(self.message())
        self.assertEqual(transport.sent_texts(), ["原始正文"])
        denied = host.audit.select(event="outbound-denied")
        self.assertEqual([item["plugin"] for item in denied], ["mw-blind"])
        self.assertNotIn("mw-blind", host.outbound.passes)

    def test_middleware_exception_is_isolated(self):
        self._talk()
        s.write_plugin(
            self.plugins, "mw-bad", s.OUTBOUND_FAIL_SOURCE, capabilities=[s.outbound_capability(10)]
        )
        s.write_plugin(
            self.plugins, "mw-good", s.outbound_source("|good"), capabilities=[s.outbound_capability(20)]
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        host.handle(self.message())

        self.assertEqual(transport.sent_texts(), ["原始正文|good"])
        failed = host.audit.select(event="outbound-failed")
        self.assertEqual([item["plugin"] for item in failed], ["mw-bad"])
        self.assertIn("RuntimeError", failed[0]["detail"])

    def test_extra_output_goes_through_pipeline(self):
        s.write_plugin(
            self.plugins,
            "notify",
            "from plugin_core.sdk import Action\n\n\ndef setup(api) -> None:\n"
            "    def generate(ctx):\n"
            "        return [Action.send_text(ctx.session['key'], '通知正文')]\n\n"
            "    api.declare_inbound(lambda ctx: api.bid('extra', 60, generate=generate, reason='额外'))\n",
            capabilities=[{"stage": "inbound", "produces": "extra", "priority": 60}],
        )
        s.write_plugin(
            self.plugins, "mw", s.outbound_source("|mw"), capabilities=[s.outbound_capability(10)]
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        record = host.handle(self.message())

        self.assertEqual(record["winner"], None)
        self.assertEqual(record["extra"], ["notify"])
        self.assertEqual([item["text"] for item in record["sent"]], ["通知正文|mw"])
        self.assertEqual(host.outbound.passes, {"mw": 1})


if __name__ == "__main__":
    unittest.main()
