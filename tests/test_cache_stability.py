"""缓存稳定性：稳定说明在前、易变说明在后 + 真实缓存命中率（移植自社区插件 cache-stability）。

来源：kondius.cn/qq-agent/plugin-market/ 的 ``cache-stability`` v1.0.0。
原版把容易漂移的段落固定到系统提示词末尾、把易变块挪到稳定块之后；
本运行时对应的是"插件说明的排列顺序"+ 真实 usage 命中统计。
"""
import asyncio
import os
import pathlib
import tempfile
import unittest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai_engine import TokenUsage, cache_hit_tokens  # noqa: E402
from src.qq_app import QQRuntime  # noqa: E402
from src.plugins.cache_stability import hit_rate, prefix_fingerprint  # noqa: E402

BUILTIN = "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n"


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    (root / "config.qq.yaml").write_text(yaml.safe_dump({
        "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
    }), encoding="utf-8")
    return root


class UsageTests(unittest.TestCase):
    def test_reads_every_providers_cache_field(self):
        self.assertEqual(cache_hit_tokens({"prompt_cache_hit_tokens": 120}), 120)
        self.assertEqual(cache_hit_tokens({"prompt_tokens_details": {"cached_tokens": 64}}), 64)
        self.assertEqual(cache_hit_tokens({"cache_hit_tokens": 8}), 8)
        self.assertEqual(cache_hit_tokens({"prompt_tokens": 500}), 0)
        self.assertEqual(cache_hit_tokens(None), 0)
        self.assertEqual(cache_hit_tokens({"prompt_cache_hit_tokens": "abc"}), 0)

    def test_usage_accumulates_and_reports_rate(self):
        usage = TokenUsage()
        usage.add(prompt=1000, completion=100, cached=800)
        usage.add(prompt=1000, completion=100, cached=0)
        self.assertEqual(usage.cached_tokens, 800)
        self.assertEqual(usage.summary["cache_hit_rate"], 0.4)
        self.assertEqual(TokenUsage().cache_hit_rate, 0.0)

    def test_cached_cannot_exceed_prompt(self):
        usage = TokenUsage()
        usage.add(prompt=10, completion=1, cached=999)
        self.assertEqual(usage.cached_tokens, 10)

    def test_hit_rate_helper_guards_bad_numbers(self):
        self.assertEqual(hit_rate(50, 100), 0.5)
        self.assertEqual(hit_rate(0, 0), 0.0)
        self.assertEqual(hit_rate("x", 100), 0.0)
        self.assertEqual(hit_rate(200, 100), 1.0)


class CacheStabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["cache_stability"]
        self.bot.plugins.update_settings("cache_stability", {"enabled": True, "reportEvery": 1})
        self.bot.plugins.update_settings("emotion_state", {"enabled": True, "injectPrompt": True})

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def notes(self):
        return [str(v.get("text", "")) for v in getattr(self.bot, "events", [])]

    def message(self, text="随便聊聊"):
        return {"id": "m", "kind": "group", "target": "123", "sender_id": "9",
                "sender_name": "小鱼", "text": text}

    def test_fingerprint_changes_only_when_the_stable_part_changes(self):
        first = prefix_fingerprint("人设A")
        self.assertEqual(first, prefix_fingerprint("人设A"))
        self.assertNotEqual(first, prefix_fingerprint("人设B"))

    def test_volatile_notes_are_labelled_when_enabled(self):
        hints = self.bot.plugins.run_on_pre_ai(self.message(), [], self.bot)
        self.assertIn("心情", hints.get("volatile_system", ""))
        self.assertNotIn("心情", hints.get("extra_system", ""))

    def test_volatile_notes_fall_back_when_plugin_is_off(self):
        self.bot.plugins.update_settings("cache_stability", {"enabled": False})
        hints = self.bot.plugins.run_on_pre_ai(self.message(), [], self.bot)
        self.assertEqual(hints.get("volatile_system", ""), "")
        self.assertIn("心情", hints.get("extra_system", ""), "关掉后行为与未安装时一致")

    def test_stable_order_switch(self):
        self.bot.plugins.update_settings("cache_stability", {"stableOrder": False})
        hints = self.bot.plugins.run_on_pre_ai(self.message(), [], self.bot)
        self.assertEqual(hints.get("volatile_system", ""), "")
        self.assertIn("心情", hints.get("extra_system", ""))

    def test_prefix_note_is_recorded_and_change_is_reported(self):
        self.plugin.on_pre_ai(self.message(), [], self.bot)
        self.assertFalse(any("重新预热" in text for text in self.notes()))
        self.bot.style.save_persona({"name": "default2", "display_name": "默认2",
                                     "system_prompt": "你是另一个助手。"})
        self.bot.style.set_active("default2")
        self.plugin.on_pre_ai(self.message(), [], self.bot)
        self.assertTrue(any("重新预热" in text for text in self.notes()))

    def test_report_writes_the_real_hit_rate(self):
        self.bot.ai.usage.add(prompt=1000, completion=50, cached=900)
        self.plugin.on_sent(self.message(), "好", self.bot)
        self.assertTrue(any("缓存命中率" in text and "90%" in text for text in self.notes()),
                        self.notes())

    def test_disabled_plugin_does_not_report(self):
        self.bot.plugins.update_settings("cache_stability", {"enabled": False})
        self.bot.ai.usage.add(prompt=1000, completion=50, cached=900)
        self.plugin.on_sent(self.message(), "好", self.bot)
        self.assertFalse(any("缓存命中率" in text for text in self.notes()))

    def test_status_text_lists_state(self):
        text = self.plugin.status_text()
        self.assertIn("缓存稳定性", text)
        self.assertIn("稳定说明在前", text)


if __name__ == "__main__":
    unittest.main()
