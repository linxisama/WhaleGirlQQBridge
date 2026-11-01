"""唤醒词触发链路 + 一键导入导出 + 触发测试的回归测试。

真机背景（用户截图）：在「群聊上下文与回复控制」里把唤醒词设成 `老鲸`，
群里说"老鲸"却不触发。查出两个问题：

1. **判定分裂**：`chat_controls` 认为"唤醒词出现=点名"，`mention_gate` 只认 `@`，
   消息在前一道闸门就被丢掉，唤醒词没机会生效。
2. **匹配过严**：只做 `alias in text`（原样包含），全角/空格/标点/大小写都会漏；
   单字唤醒词被静默忽略，用户以为设置没生效。

本文件覆盖：统一判定（src/mention.py）、两个插件行为一致、以及
"一键导入/导出"（src/plugin_settings_io.py）的往返与校验。
"""
import json
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src import plugin_settings_io  # noqa: E402
from src.mention import addressed, explain, normalize, split_aliases  # noqa: E402


class AliasParsingTests(unittest.TestCase):
    def test_separators_and_dedupe(self):
        valid, ignored = split_aliases("鲸鱼娘,小鲸鱼、老鲸\n鲸鱼娘")
        self.assertEqual(valid, ["鲸鱼娘", "小鲸鱼", "老鲸"])
        self.assertEqual(ignored, [])

    def test_single_char_is_ignored_but_reported(self):
        valid, ignored = split_aliases("鲸")
        self.assertEqual(valid, [])
        self.assertEqual(ignored, ["鲸"])

    def test_space_inside_a_name_is_tolerated(self):
        # 用户手滑在昵称中间打了空格（"老 鲸"）应当仍被当成一个词
        self.assertEqual(split_aliases("老 鲸"), (["老鲸"], []))

    def test_two_names_separated_by_space_still_split(self):
        self.assertEqual(split_aliases("鲸鱼娘 小鲸鱼"), (["鲸鱼娘", "小鲸鱼"], []))


class AddressedTests(unittest.TestCase):
    def test_plain_alias_hit(self):
        self.assertEqual(addressed("老鲸在吗", "老鲸"), (True, "老鲸"))

    def test_punctuation_and_spaces_are_ignored(self):
        self.assertTrue(addressed("老 鲸！你在不？", "老鲸")[0])
        self.assertTrue(addressed("小鲸鱼，在", "老鲸,小鲸鱼")[0])

    def test_fullwidth_and_case(self):
        # 全角 → 半角、大小写归一之后应当命中（英文昵称很常见）
        self.assertTrue(addressed("ＬＡＯ鲸", "LAO鲸")[0])
        self.assertTrue(addressed("lao鲸 在吗", "LAO鲸")[0])

    def test_miss(self):
        self.assertEqual(addressed("随便说句话", "老鲸"), (False, "未命中"))

    def test_at_counts(self):
        self.assertEqual(addressed("随便说", "老鲸", at=True), (True, "@"))

    def test_explain_is_actionable(self):
        self.assertIn("命中唤醒词", explain("老鲸在吗", "老鲸"))
        text = explain("随便说", "鲸")
        self.assertIn("不会回复", text)
        self.assertIn("已忽略", text, "单字被忽略要说出来，否则用户以为设置没生效")

    def test_normalize(self):
        self.assertEqual(normalize(" 老 鲸！ "), "老鲸")


class PluginAgreementTests(unittest.TestCase):
    """两个插件对"点名"必须给出同一个答案（真机不触发的根因）。"""

    def test_mention_gate_respects_wake_words(self):
        from src.plugins.mention_gate import MentionGate

        gate = MentionGate.__new__(MentionGate)
        gate.get = lambda key, default=None: {
            "enabled": True, "mode": "only", "group_ids": "1CBE87EF553193840AFFDCC788043580",
        }.get(key, default)

        class Controls:
            def get(self, key, default=None):
                return "老鲸" if key == "aliases" else default

        class Manager:
            _plugins = {"chat_controls": Controls()}

        gate._manager = Manager()
        message = {"kind": "group", "target": "1CBE87EF553193840AFFDCC788043580", "text": "老鲸在吗", "at": False}
        self.assertFalse(gate.on_block(message, object()),
                         "唤醒词命中时 mention_gate 不该拦（以前会拦，导致唤醒词永不生效）")
        plain = {"kind": "group", "target": "1CBE87EF553193840AFFDCC788043580", "text": "随便聊聊", "at": False}
        self.assertTrue(gate.on_block(plain, object()), "没点名该拦")

    def test_chat_controls_triggers_on_wake_word(self):
        from src.plugins.chat_controls import ChatControls

        controls = ChatControls.__new__(ChatControls)
        settings = {"aliases": "老鲸", "requireAt": True, "interjectPercent": 0,
                    "perMinute": 10, "cooldownSeconds": 0}

        class DB:
            def execute(self, *args, **kwargs):
                class Cursor:
                    def fetchone(self):
                        return (0, None)
                return Cursor()

        class Runtime:
            mode = "onebot"
            db = DB()

            @staticmethod
            def account():
                return "self"

        class Manager:
            _plugins = {}

        controls._manager = Manager()
        controls.get = lambda key, default=None: settings.get(key, default)
        message = {"kind": "group", "target": "1CBE87EF553193840AFFDCC788043580", "text": "老鲸 在吗？", "at": False}
        self.assertTrue(controls.should_reply(message, Runtime()))
        miss = {"kind": "group", "target": "1CBE87EF553193840AFFDCC788043580", "text": "随便聊聊", "at": False}
        self.assertFalse(controls.should_reply(miss, Runtime()))


class SettingsIOTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "chat_controls-settings.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        settings = {"aliases": "老鲸,小鲸鱼", "requireAt": True, "perMinute": 10}
        plugin_settings_io.dump_settings(self.path, "chat_controls", settings)
        payload = json.loads(open(self.path, encoding="utf-8").read())
        self.assertEqual(payload["plugin"], "chat_controls")
        self.assertEqual(payload["version"], plugin_settings_io.FORMAT_VERSION)
        schema = {"aliases": {"type": "string"}, "requireAt": {"type": "bool"},
                  "perMinute": {"type": "int", "min": 1, "max": 60}}
        loaded, problems = plugin_settings_io.load_settings(self.path, schema, {})
        self.assertEqual(loaded["aliases"], "老鲸,小鲸鱼")
        self.assertIs(loaded["requireAt"], True)
        self.assertEqual(loaded["perMinute"], 10)
        self.assertEqual(problems, [])

    def test_unknown_key_is_reported(self):
        plugin_settings_io.dump_settings(self.path, "x", {"nope": 1, "aliases": "老鲸"})
        loaded, problems = plugin_settings_io.load_settings(
            self.path, {"aliases": {"type": "string"}}, {})
        self.assertEqual(loaded.get("aliases"), "老鲸")
        self.assertNotIn("nope", loaded)
        self.assertTrue(any("nope" in item for item in problems), problems)

    def test_bad_types_fall_back_and_are_reported(self):
        plugin_settings_io.dump_settings(
            self.path, "x", {"perMinute": "abc", "requireAt": "yes", "aliases": 123})
        schema = {"perMinute": {"type": "int", "min": 1, "max": 60},
                  "requireAt": {"type": "bool"}, "aliases": {"type": "string"}}
        loaded, problems = plugin_settings_io.load_settings(
            self.path, schema, {"perMinute": 10, "requireAt": True, "aliases": ""})
        self.assertEqual(loaded["perMinute"], 10, "非法数字回退默认值")
        self.assertIs(loaded["requireAt"], True, '"yes" 视为真')
        self.assertEqual(loaded["aliases"], "123", "数字也能当字符串读")
        self.assertTrue(any("perMinute" in item for item in problems), problems)

    def test_out_of_range_int_is_rejected(self):
        plugin_settings_io.dump_settings(self.path, "x", {"perMinute": 999})
        loaded, problems = plugin_settings_io.load_settings(
            self.path, {"perMinute": {"type": "int", "min": 1, "max": 60}}, {"perMinute": 10})
        self.assertEqual(loaded["perMinute"], 10)
        self.assertTrue(any("不能大于" in item for item in problems), problems)

    def test_broken_file_gives_a_readable_problem(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("这不是 JSON")
        loaded, problems = plugin_settings_io.load_settings(self.path, {}, {})
        self.assertEqual(loaded, {})
        self.assertTrue(problems and "读不出" in problems[0], problems)


if __name__ == "__main__":
    unittest.main()
