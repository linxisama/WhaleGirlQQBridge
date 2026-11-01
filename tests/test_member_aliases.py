"""群友外号对照：命中外号才加几行，帮模型认人（移植自社区插件 member-aliases）。

来源：kondius.cn/qq-agent/plugin-market/ 的 ``member-aliases`` v1.0.0。
照搬的两个细节：短英文/数字外号只在 ASCII 边界上判断（"99" 不该命中 "1990"）；
中文或长度≥3 的外号直接包含判断（中文不用空格分词）。
"""
import asyncio
import os
import pathlib
import tempfile
import unittest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime  # noqa: E402
from src.plugins.member_aliases import (  # noqa: E402
    HEADER, build_note, clean_aliases, text_has_alias,
)

BUILTIN = "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n"


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    (root / "config.qq.yaml").write_text(yaml.safe_dump({
        "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
    }), encoding="utf-8")
    return root


class TextHasAliasTests(unittest.TestCase):
    def test_chinese_aliases_match_inside_sentences(self):
        self.assertTrue(text_has_alias("某某今天怎么没来", "某某"))
        self.assertFalse(text_has_alias("今天谁都没来", "某某"))

    def test_short_alnum_aliases_respect_ascii_boundaries(self):
        self.assertTrue(text_has_alias("99来了", "99"))
        self.assertTrue(text_has_alias("叫 99 的那个人", "99"))
        self.assertFalse(text_has_alias("1990年的老歌", "99"))
        self.assertFalse(text_has_alias("2990", "99"))

    def test_long_alnum_aliases_use_contains(self):
        self.assertTrue(text_has_alias("abc1234567 在吗", "abc123"))

    def test_empty_inputs(self):
        self.assertFalse(text_has_alias("", "99"))
        self.assertFalse(text_has_alias("在吗", ""))


class CleanAliasesTests(unittest.TestCase):
    def test_splits_dedupes_and_limits(self):
        self.assertEqual(clean_aliases("大王, 大王、小王；阿豆"), ["大王", "小王", "阿豆"])
        self.assertEqual(clean_aliases(["a", "a", "", "  ", "b"]), ["a", "b"])
        self.assertEqual(clean_aliases(["x" * 30]), [])
        self.assertEqual(len(clean_aliases([f"别名{i}" for i in range(30)])), 12)
        self.assertEqual(clean_aliases(None), [])


class BuildNoteTests(unittest.TestCase):
    def test_speaker_line_and_mention_line(self):
        table = {"111": ["大王", "老王"], "222": ["阿豆"]}
        note = build_note(table, sender="111", sender_name="大王本尊", text="阿豆在吗")
        self.assertTrue(note.startswith(HEADER))
        self.assertIn("正在说话的 大王本尊(QQ:111) 也叫：大王、老王", note)
        self.assertIn("有人提到「阿豆」= 阿豆(QQ:222)", note)
        self.assertEqual(note.count("·"), 2)

    def test_nothing_matched_adds_nothing(self):
        self.assertEqual(build_note({"111": ["大王"]}, sender="333", text="今天天气不错"), "")
        self.assertEqual(build_note({}, sender="111", text="大王"), "")

    def test_scan_mentions_can_be_switched_off(self):
        note = build_note({"222": ["阿豆"]}, sender="111", text="阿豆在吗", scan_mentions=False)
        self.assertEqual(note, "")

    def test_max_lines_caps_the_block(self):
        table = {str(100 + index): [f"外号{index}"] for index in range(20)}
        text = " ".join(f"外号{index}" for index in range(20))
        note = build_note(table, sender="999", text=text, max_lines=3)
        self.assertEqual(note.count("·"), 3)

    def test_speaker_is_not_also_listed_as_mentioned(self):
        note = build_note({"111": ["大王"]}, sender="111", sender_name="大王", text="大王在此")
        self.assertEqual(note.count("·"), 1)


class MemberAliasesPluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.plugin = self.bot.plugins._plugins["member_aliases"]
        self.bot.plugins.update_settings("member_aliases", {"enabled": True})
        self.bot.plugins.update_settings("pc_control", {"enabled": True, "ownerIds": "10001"})

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def message(self, text="你好", sender="20002", kind="group"):
        return {"id": "m1", "kind": kind, "target": "123", "sender_id": sender,
                "sender_name": "小鱼", "text": text}

    def note(self, text="你好", sender="20002"):
        hints = self.plugin.on_pre_ai(self.message(text, sender), [], self.bot) or {}
        return hints.get("volatile_system") or hints.get("extra_system") or ""

    def test_command_adds_and_lists_aliases(self):
        reply = self.plugin.on_ingest(self.message("外号 加 20002 大王", sender="10001", kind="private"),
                                      self.bot)
        self.assertIn("已记下", reply)
        self.assertEqual(self.plugin.table(), {"20002": ["大王"]})
        listing = self.plugin.on_ingest(self.message("外号", sender="10001", kind="private"), self.bot)
        self.assertIn("QQ:20002 → 大王", listing)

    def test_command_rejects_bad_input_and_non_admin(self):
        self.assertIn("只有管理员", self.plugin.on_ingest(
            self.message("外号 加 20002 大王", sender="99999", kind="private"), self.bot))
        self.assertIn("要按 QQ 号", self.plugin.on_ingest(
            self.message("外号 加 大王", sender="10001", kind="private"), self.bot))
        self.assertIn("用法", self.plugin.on_ingest(
            self.message("外号 乱写", sender="10001", kind="private"), self.bot))

    def test_command_remove(self):
        self.plugin.add_alias("20002", "大王")
        self.plugin.add_alias("20002", "老王")
        self.assertIn("已删掉", self.plugin.on_ingest(
            self.message("外号 删 20002 老王", sender="10001", kind="private"), self.bot))
        self.assertEqual(self.plugin.table(), {"20002": ["大王"]})
        self.assertIn("全部外号", self.plugin.on_ingest(
            self.message("外号 删 20002", sender="10001", kind="private"), self.bot))
        self.assertEqual(self.plugin.table(), {})

    def test_note_is_injected_only_on_hit(self):
        """规则①：有外号的人一说话就加一行（这是设计，不是"命中"）；
        规则②：消息里提到别人的外号才加行，都没命中就一个字不加。"""
        self.plugin.add_alias("20002", "大王")
        self.assertIn("大王", self.note("大王在吗"))
        self.assertIn("正在说话的", self.note("今天天气不错"), "说话人自己有外号就加一行")
        self.assertEqual(self.note("今天天气不错", sender="30003"), "",
                         "既不是有外号的人在说话、也没提到任何外号 → 一个字不加")

    def test_auto_learn_is_off_by_default(self):
        self.plugin.on_pre_ai(self.message("我叫大王"), [], self.bot)
        self.assertEqual(self.plugin.table(), {})
        self.bot.plugins.update_settings("member_aliases", {"autoLearn": True})
        self.plugin.on_pre_ai(self.message("我叫大王"), [], self.bot)
        self.assertEqual(self.plugin.table(), {"20002": ["大王"]})

    def test_disabled_plugin_adds_nothing(self):
        self.plugin.add_alias("20002", "大王")
        self.bot.plugins.update_settings("member_aliases", {"enabled": False})
        self.assertIsNone(self.plugin.on_pre_ai(self.message("大王在吗"), [], self.bot))

    def test_group_command_gets_no_reply(self):
        self.assertIsNone(self.plugin.on_ingest(self.message("外号", sender="10001"), self.bot))


if __name__ == "__main__":
    unittest.main()
