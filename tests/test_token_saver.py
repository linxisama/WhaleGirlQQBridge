"""Token 节流：历史滑窗 + 一行摘要 + 截断过长历史（移植自社区插件 token-saver）。

来源：kondius.cn/qq-agent/plugin-market/ 的 ``token-saver`` v1.0.0。
原版三件事里"工具描述瘦身"在本运行时不存在（没有 function calling），
③"截断过长工具结果"换成"截断过长的单条历史内容"。
规则细节照搬，尤其是"省不到字符就退化成只删不留摘要"这条。
"""
import asyncio
import os
import pathlib
import tempfile
import unittest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402
from src.plugins.token_saver import (  # noqa: E402
    DIGEST_OMIT, DIGEST_PREFIX, clamp, text_of, trim_history, truncate_history_content,
)

BUILTIN = "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n"


def conversation(count, *, size=20, roles=None):
    messages = []
    for index in range(count):
        role = (roles or [])[index] if roles and index < len(roles) else (
            "assistant" if index % 2 else "user")
        messages.append({"role": role, "content": f"第{index}条" + "字" * size})
    return messages


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    (root / "config.qq.yaml").write_text(yaml.safe_dump({
        "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
    }), encoding="utf-8")
    return root


class HelperTests(unittest.TestCase):
    def test_text_of_handles_both_shapes(self):
        self.assertEqual(text_of({"content": "abc"}), "abc")
        self.assertEqual(text_of({"content": [{"type": "text", "text": "a"},
                                              {"type": "image", "url": "x"},
                                              {"type": "text", "text": "b"}]}), "a\nb")
        self.assertEqual(text_of({}), "")
        self.assertEqual(text_of("not a message"), "")

    def test_clamp_falls_back_on_bad_input(self):
        self.assertEqual(clamp("7", 4, 60, 14), 7)
        self.assertEqual(clamp("abc", 4, 60, 14), 14)
        self.assertEqual(clamp(None, 4, 60, 14), 14)
        self.assertEqual(clamp(999, 4, 60, 14), 60)


class TrimHistoryTests(unittest.TestCase):
    def test_window_keeps_recent_and_inserts_one_line_digest(self):
        # 每条要够长，摘要才比原文省（原版也是这个前提：省不到就退化成只删）
        messages = conversation(30, size=80)
        saved, what = trim_history(messages, mode="window", keep=6, budget=600)
        self.assertGreater(saved, 0)
        self.assertIn("1 行摘要", what)
        self.assertEqual(len(messages), 7, "最近 6 条 + 1 条摘要")
        self.assertEqual(messages[-1]["content"], conversation(30, size=80)[-1]["content"])
        self.assertEqual(messages[0]["role"], "user")
        self.assertTrue(messages[0]["content"].startswith(DIGEST_PREFIX))
        self.assertIn("群友:", messages[0]["content"])
        self.assertIn("机器人:", messages[0]["content"])
        self.assertLessEqual(len(messages[0]["content"]), 601, "摘要有上限（截断时会多一个省略号）")

    def test_aggressive_only_counts(self):
        messages = conversation(30, size=80)
        saved, what = trim_history(messages, mode="aggressive", keep=6, budget=600)
        self.assertGreater(saved, 0)
        self.assertEqual(len(messages), 7, "最近 6 条 + 1 条'省略了 N 条'的摘要")
        self.assertTrue(messages[0]["content"].startswith("【较早对话已压缩】省略了 24 条"))

    def test_window_digest_survives_when_the_tail_is_long(self):
        """真机回归：保留的最近消息很长时，一行摘要也必须照常插入。

        旧实现把「保留的最近 N 条」也算进 after，于是 ``before - after`` 常常是负数
        （head 比 tail 短）→ 默认档「滑窗 + 一行摘要」静默退化成「只删不留摘要」。
        用户实测的默认配置就是 keepRecentMessages=14，tail 天然更长，属常态。
        """
        messages = conversation(20, size=500)          # 每条 500 字，tail 14 条远长于 head
        saved, what = trim_history(messages, mode="window", keep=14, budget=800)
        self.assertGreater(saved, 0, "省下字符数必须为正，否则会退化")
        self.assertIn("1 行摘要", what, f"应当插入摘要而不是只删：{what}")
        self.assertTrue(messages[0]["content"].startswith(DIGEST_PREFIX),
                        "第一条应当是摘要（保留了最近 14 条）")
        self.assertEqual(len(messages), 15, "最近 14 条 + 1 条摘要")
        # 省下的字符数应当等于「被裁掉的部分 − 摘要」，而不是把保留部分也算进去
        self.assertLess(saved, 20 * 500, "不能把保留的最近消息算成省下的字符数")

    def test_aggressive_still_counts_only_removed_messages(self):
        messages = conversation(20, size=500)
        saved, what = trim_history(messages, mode="aggressive", keep=14, budget=800)
        self.assertGreater(saved, 0)
        self.assertTrue(messages[0]["content"].startswith("【较早对话已压缩】省略了 6 条"),
                        f"aggressive 档只留一句计数摘要：{messages[0]['content'][:40]}")

    def test_short_history_is_left_alone(self):
        messages = conversation(7)
        self.assertEqual(trim_history(messages, mode="window", keep=6, budget=600), (0, ""))
        self.assertEqual(len(messages), 7)

    def test_off_or_unknown_mode_does_nothing(self):
        for mode in ("off", "whatever", ""):
            messages = conversation(30)
            self.assertEqual(trim_history(messages, mode=mode, keep=6, budget=600), (0, ""))
            self.assertEqual(len(messages), 30)

    def test_falls_back_to_dropping_when_digest_costs_more(self):
        """摘要预算很大 + 原文极短时，插摘要反而更费 → 只删不留摘要（原版同款退化路径）。"""
        messages = [{"role": "user", "content": "嗯嗯"} for _ in range(40)]
        saved, what = trim_history(messages, mode="window", keep=4, budget=4000)
        self.assertGreater(saved, 0, "至少要能把条数减下来")
        self.assertIn("没插摘要", what)
        self.assertEqual(len(messages), 4)
        self.assertFalse(any(str(m["content"]).startswith(DIGEST_PREFIX) for m in messages))

    def test_too_short_history_is_never_touched(self):
        messages = [{"role": "user", "content": "嗯"} for _ in range(5)]
        self.assertEqual(trim_history(messages, mode="window", keep=4, budget=100), (0, ""))
        self.assertEqual(len(messages), 5)

    def test_system_and_tool_roles_are_protected(self):
        messages = ([{"role": "system", "content": "系统提示"}] + conversation(20)
                    + [{"role": "tool", "content": "工具结果"}])
        trim_history(messages, mode="window", keep=4, budget=600)
        roles = [m["role"] for m in messages]
        self.assertIn("system", roles)
        self.assertIn("tool", roles)
        self.assertEqual(roles[0], "system")

    def test_bad_input_is_safe(self):
        self.assertEqual(trim_history("nope", mode="window", keep=4, budget=600), (0, ""))
        self.assertEqual(trim_history([{"role": "user"}], mode="window", keep=1, budget=100), (0, ""))


class TruncateTests(unittest.TestCase):
    def test_truncates_long_history_but_not_the_current_turn(self):
        messages = [{"role": "user", "content": "长" * 5000},
                    {"role": "assistant", "content": "短"},
                    {"role": "user", "content": "当前这轮" + "长" * 5000}]
        saved = truncate_history_content(messages, 1000)
        self.assertGreater(saved, 0)
        self.assertIn("内容过长，已省略", messages[0]["content"])
        self.assertLessEqual(len(messages[0]["content"]), 1000 + 40)
        self.assertTrue(messages[-1]["content"].endswith("长" * 5000), "当前这轮永不截断")

    def test_zero_limit_disables_truncation(self):
        messages = [{"role": "user", "content": "长" * 100}]
        self.assertEqual(truncate_history_content(messages, 0), 0)
        self.assertEqual(len(messages[0]["content"]), 100)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["token_saver"]
        self.bot.plugins.update_settings("token_saver", {"enabled": True, "verbose": True})

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

    def test_hook_compresses_and_logs(self):
        messages = conversation(40, size=80)
        self.plugin.on_pre_ai({"id": "m", "text": "hi"}, messages, self.bot)
        self.assertEqual(len(messages), self.plugin.get("keepRecentMessages") + 1)
        self.assertTrue(any("Token 节流" in text for text in self.notes()))

    def test_disabled_plugin_leaves_history_alone(self):
        self.bot.plugins.update_settings("token_saver", {"enabled": False})
        messages = conversation(40)
        self.assertIsNone(self.plugin.on_pre_ai({"id": "m"}, messages, self.bot))
        self.assertEqual(len(messages), 40)

    def test_history_mode_off_only_truncates_long_items(self):
        self.bot.plugins.update_settings("token_saver", {"historyMode": "off"})
        messages = [{"role": "user", "content": "长" * 9000}] + conversation(30)
        self.plugin.on_pre_ai({"id": "m"}, messages, self.bot)
        self.assertIn("内容过长", messages[0]["content"])
        self.assertEqual(len(messages), 31, "mode=off 时不动条数")

    def test_reply_path_runs_through_the_plugin_manager(self):
        """走真实的插件管理器（回复路径用的就是它）：历史被压缩、摘要进了这一轮的请求。"""
        messages = conversation(40, size=80)

        async def run():
            return await self.bot.plugins.run_on_pre_ai_async({"id": "m", "text": "现在呢"},
                                                              messages, self.bot)

        hints = asyncio.run(run())
        self.assertEqual(len(messages), self.plugin.get("keepRecentMessages") + 1)
        self.assertTrue(any(str(m.get("content", "")).startswith(DIGEST_PREFIX) for m in messages),
                        "压缩后的历史里应有一行摘要")
        self.assertEqual(hints.get("max_tokens", 0), 0, "Token 节流不参与输出封顶")
        self.assertTrue(any("Token 节流" in text for text in self.notes()))


if __name__ == "__main__":
    unittest.main()
