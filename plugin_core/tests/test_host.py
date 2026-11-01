"""Host：启动冻结、运行期增删、整批重载、归一化白名单、会话落盘。"""

from __future__ import annotations

import json
import unittest
from types import MappingProxyType

from plugin_core.api import FreezeViolation, KernelError
from plugin_core.host import DEFAULT_SYSTEM_PROMPT, Host, normalize_message
from plugin_core.mock import MockModel, MockSource, MockTransport
from plugin_core.tests import support as s


class HostTest(s.KernelTestCase):
    def test_start_freezes_and_refuses_add_plugin(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 50), capabilities=[s.main_capability(50)])
        host = self.build_host()
        self.assertTrue(host.frozen)

        extra = s.write_plugin(self.plugins, "late", s.main_source("迟到", 60), capabilities=[s.main_capability(60)])
        with self.assertRaises(FreezeViolation):
            host.add_plugin(extra)
        self.assertNotIn("late", host.plugins)
        frozen_events = host.audit.select(event="frozen")
        self.assertEqual([item["moment"] for item in frozen_events], ["start", "add-plugin"])
        self.assertEqual(frozen_events[1]["code"], "frozen")

    def test_snapshot_is_immutable(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 50), capabilities=[s.main_capability(50)])
        host = self.build_host()
        snapshot = host.freeze_snapshot()
        self.assertIsInstance(snapshot, tuple)
        self.assertEqual(len(snapshot), 1)
        self.assertIsInstance(snapshot[0], MappingProxyType)
        with self.assertRaises(TypeError):
            snapshot[0]["name"] = "改不动"
        self.assertEqual(snapshot[0]["id"], "talk")
        self.assertIsInstance(snapshot[0]["permissions"], tuple)

    def test_reload_all_picks_up_new_plugin(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 20), capabilities=[s.main_capability(20)])
        transport = MockTransport()
        host = self.build_host(transport=transport)
        self.assertEqual([item["id"] for item in host.freeze_snapshot()], ["talk"])

        s.write_plugin(self.plugins, "second", s.main_source("第二", 60), capabilities=[s.main_capability(60)])
        snapshot = host.reload_all()
        self.assertEqual([item["id"] for item in snapshot], ["second", "talk"])
        record = host.handle(self.message())
        self.assertEqual(record["winner"]["plugin"], "second")
        self.assertEqual(transport.sent_texts(), ["第二"])

    def test_api_version_must_match_exactly(self):
        s.write_plugin(
            self.plugins,
            "future",
            s.main_source("未来", 99),
            api_version=999,
            capabilities=[s.main_capability(99)],
        )
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 20), capabilities=[s.main_capability(20)])
        host = self.build_host()
        self.assertEqual([item["id"] for item in host.freeze_snapshot()], ["talk"])
        self.assertEqual([item["code"] for item in host.refused], ["api-version-mismatch"])
        self.assertIn("999 != 1", host.refused[0]["detail"])

    def test_uninstall_removes_plugin_and_snapshot_entry(self):
        s.write_plugin(self.plugins, "one", s.main_source("一", 20), capabilities=[s.main_capability(20)])
        s.write_plugin(self.plugins, "two", s.main_source("二", 60), capabilities=[s.main_capability(60)])
        host = self.build_host()
        host.uninstall("two")
        self.assertEqual([item["id"] for item in host.freeze_snapshot()], ["one"])
        self.assertEqual([item["id"] for item in host.freeze_snapshot()], [item["id"] for item in host.freeze_snapshot()])

    def test_handle_before_start_is_rejected(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 20), capabilities=[s.main_capability(20)])
        host = Host(self.plugins, self.data, MockTransport(), MockModel("回复"))
        with self.assertRaises(KernelError):
            host.handle(self.message())

    def test_default_persona_when_no_plugin_answers(self):
        model = MockModel("默认人格回复")
        transport = MockTransport()
        host = self.build_host(transport=transport, model=model)
        record = host.handle(self.message("你好"))
        self.assertEqual(transport.sent_texts(), ["默认人格回复"])
        self.assertEqual(record["fallback"], "default-persona")
        prompt = model.last_prompt()
        self.assertEqual(prompt[0]["content"], DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(prompt[-1]["content"], "你好")

    def test_session_survives_restart(self):
        s.write_plugin(self.plugins, "talk", s.main_source("主体", 20), capabilities=[s.main_capability(20)])
        host = self.build_host()
        host.handle(self.message("第一句"))
        path = self.data / "session.json"
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["turns"], 1)

        host2 = self.build_host()
        self.assertTrue(host2.session_loaded)
        prompts = host2.build_prompt(normalize_message(MockSource().message("第二句")))
        self.assertEqual([item["role"] for item in prompts], ["system", "user", "assistant", "user"])


class NormalizeTest(unittest.TestCase):
    def test_upstream_fields_are_dropped(self):
        raw = MockSource().message("正文")
        raw["source"] = "上游私有字段"
        raw["raw_seq"] = 123456
        normalized = normalize_message(raw)
        self.assertEqual(set(normalized), {"session", "sender", "message"})
        self.assertNotIn("source", json.dumps(normalized, ensure_ascii=False))
        self.assertNotIn("raw_seq", json.dumps(normalized, ensure_ascii=False))
        self.assertEqual(normalized["session"]["key"], "g:100")
        self.assertEqual(normalized["message"]["text"], "正文")

    def test_unknown_segment_fields_are_dropped(self):
        raw = MockSource().message("图片")
        raw["message"]["segments"] = [{"type": "image", "url": "https://example.invalid/a.png", "extra": "丢弃"}]
        normalized = normalize_message(raw)
        segment = normalized["message"]["segments"][0]
        self.assertEqual(segment["type"], "image")
        self.assertNotIn("extra", segment)

    def test_missing_session_is_rejected(self):
        with self.assertRaises(KernelError):
            normalize_message({"message": {"id": "m", "text": "x"}, "sender": {"id": "u"}})

    def test_missing_message_is_rejected(self):
        with self.assertRaises(KernelError):
            normalize_message({"session": {"id": "1"}, "sender": {"id": "u"}})


if __name__ == "__main__":
    unittest.main()
