"""权限校验：未声明的动作被拒、写进审计、不抛给用户、进程不崩。"""

from __future__ import annotations

import unittest

from plugin_core.tests import support as s

SEND_TEXT_SOURCE = '''from plugin_core.sdk import Action


def setup(api) -> None:
    def generate(ctx):
        return [Action.send_text(ctx.session["key"], "越权正文")]

    api.declare_inbound(lambda ctx: api.bid("main", 50, generate=generate, reason="越权"))
'''

SEND_MEDIA_SOURCE = '''from plugin_core.sdk import Action


def setup(api) -> None:
    def generate(ctx):
        return [Action.send_media(ctx.session["key"], "https://example.invalid/a.png")]

    api.declare_inbound(lambda ctx: api.bid("main", 50, generate=generate, reason="越权媒体"))
'''

STORE_SOURCE = '''from plugin_core.sdk import Action


def setup(api) -> None:
    def generate(ctx):
        ctx.store.set("secret", 1)
        return [Action.noop()]

    api.declare_inbound(lambda ctx: api.bid("main", 50, generate=generate, reason="越权写存储"))
'''

FETCH_SOURCE = '''from plugin_core.sdk import Action


def setup(api) -> None:
    def generate(ctx):
        ctx.fetch("https://example.invalid/data")
        return [Action.noop()]

    api.declare_inbound(lambda ctx: api.bid("main", 50, generate=generate, reason="越权联网"))
'''


class PermissionTest(s.KernelTestCase):
    def test_send_text_without_permission_is_denied(self):
        s.write_plugin(
            self.plugins, "thief", SEND_TEXT_SOURCE, permissions=["read_message"], capabilities=[s.main_capability(50)]
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)

        self.assertTrue(host.get_plugin("thief").api.can("read_message"))
        self.assertFalse(host.get_plugin("thief").api.can("send_message"))

        record = host.handle(self.message())

        self.assertEqual(record["sent"], [])
        self.assertEqual(transport.history, [])
        self.assertTrue(host.started)
        denied = host.audit.select(event="denied")
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["plugin"], "thief")
        self.assertEqual(denied[0]["action"], "send_text")
        self.assertEqual(denied[0]["permission"], "send_message")
        self.assertEqual(denied[0]["code"], "denied")
        self.assertEqual(host.gate.denied, 1)

    def test_send_media_requires_send_permission(self):
        s.write_plugin(
            self.plugins, "media", SEND_MEDIA_SOURCE, permissions=["read_message"], capabilities=[s.main_capability(50)]
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        host.handle(self.message())
        self.assertEqual(transport.history, [])
        denied = host.audit.select(event="denied")
        self.assertEqual([item["action"] for item in denied], ["send_media"])

    def test_store_write_requires_store_permission(self):
        s.write_plugin(
            self.plugins,
            "nokv",
            STORE_SOURCE,
            permissions=["read_message", "send_message"],
            capabilities=[s.main_capability(50)],
        )
        host = self.build_host(default_persona=False)
        record = host.handle(self.message())

        self.assertEqual(record["sent"], [])
        denied = host.audit.select(event="denied")
        self.assertEqual([item["permission"] for item in denied], ["store"])
        failed = host.audit.select(event="hook-failed")
        self.assertEqual([item["plugin"] for item in failed], ["nokv"])
        self.assertIn("PermissionDenied", failed[0]["detail"])
        self.assertEqual(host.store.kv_data("nokv"), {})

    def test_network_without_permission_is_denied(self):
        s.write_plugin(
            self.plugins,
            "netless",
            FETCH_SOURCE,
            permissions=["read_message", "send_message"],
            capabilities=[s.main_capability(50)],
        )
        host = self.build_host(default_persona=False)
        host.handle(self.message())
        denied = host.audit.select(event="denied")
        self.assertEqual([item["permission"] for item in denied], ["network"])
        failed = host.audit.select(event="hook-failed")
        self.assertIn("PermissionDenied", failed[0]["detail"])

    def test_declared_permissions_are_granted(self):
        s.write_plugin(
            self.plugins, "typed", s.main_source("合规正文", 50), capabilities=[s.main_capability(50)]
        )
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        host.handle(self.message())
        self.assertEqual(transport.sent_texts(), ["合规正文"])
        self.assertEqual(host.audit.select(event="denied"), [])

    def test_permission_denial_does_not_kill_host(self):
        s.write_plugin(
            self.plugins, "thief", SEND_TEXT_SOURCE, permissions=["read_message"], capabilities=[s.main_capability(50)]
        )
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 10), capabilities=[s.main_capability(10)])
        transport = s.MockTransport()
        host = self.build_host(transport=transport)
        record = host.handle(self.message())
        # 越权插件胜出但不发，主体被优先规则挡下——进程照旧活着，下一轮还能工作
        self.assertTrue(host.started)
        self.assertEqual(record["sent"], [])
        record2 = host.handle(self.message())
        self.assertIsNotNone(record2["message_id"])
        self.assertEqual(len(host.audit.select(event="arbitration")), 2)


if __name__ == "__main__":
    unittest.main()
