"""人设提示词组装（prompt_text）与默认鲸鱼娘人设的离线测试。

原来只用 `system_prompt`，人设文件里的 `examples` 从没进过提示词；
`temperature` / `max_tokens` 也没被消费（用的是全局配置）。
这里既验证组装本身，也把默认人设的质量要求钉住，防止以后再被写成小作文。
"""

import pathlib
import tempfile
import unittest

import yaml

from src.style_manager import ChatStyle, StyleManager

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PERSONA_FILE = REPO_ROOT / "styles" / "tsundere_whale_girl.yaml"

# 回复规范里必须出现的要点（每条一个关键词，改了措辞就要同步改这里）
REQUIRED_RULES = ("最多 2 句", "60 个汉字", "一次只回一件事", "口语化",
                  "不要用", "不要复述", "思考过程", "不知道", "不承认自己是 AI")


class PromptTextTests(unittest.TestCase):
    def test_starts_with_system_prompt(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="你是猫娘。")
        self.assertTrue(style.prompt_text().startswith("你是猫娘。"))

    def test_contains_all_reply_rules(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="x")
        text = style.prompt_text()
        for rule in REQUIRED_RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, text)

    def test_temperature_can_rise_and_fall(self):
        """温度是随互动变化的关系档位，不是固定采样值。"""
        text = ChatStyle(name="a", display_name="A", description="",
                         system_prompt="x").prompt_text()
        self.assertIn("升温", text)
        self.assertIn("降温", text)
        self.assertIn("不是固定档位", text)

    def test_examples_are_rendered(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="x",
                          examples=[{"user": "你好", "assistant": "哼，干嘛"}])
        text = style.prompt_text()
        self.assertIn("不要照抄内容", text)
        self.assertIn("用户：你好", text)
        self.assertIn("你：哼，干嘛", text)

    def test_at_most_four_examples(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="x",
                          examples=[{"user": f"u{i}", "assistant": f"a{i}"} for i in range(6)])
        text = style.prompt_text()
        for i in range(4):
            self.assertIn(f"用户：u{i}", text)
        for i in range(4, 6):
            self.assertNotIn(f"用户：u{i}", text)

    def test_no_examples_section_when_empty(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="x")
        self.assertNotIn("不要照抄内容", style.prompt_text())
        style2 = ChatStyle(name="a", display_name="A", description="", system_prompt="x", examples=[])
        self.assertNotIn("不要照抄内容", style2.prompt_text())

    def test_incomplete_examples_are_skipped(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="x",
                          examples=[{"user": "", "assistant": "有空答"},
                                    {"user": "有空问", "assistant": ""},
                                    {"user": "好的", "assistant": "嗯"},
                                    {"user": 123, "assistant": None}])
        text = style.prompt_text()
        self.assertIn("用户：好的", text)
        self.assertNotIn("有空答", text)
        self.assertNotIn("有空问", text)

    def test_order_is_prompt_then_rules_then_examples(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="正文标记",
                          examples=[{"user": "u", "assistant": "a"}])
        text = style.prompt_text()
        self.assertLess(text.index("正文标记"), text.index("最多 2 句"))
        self.assertLess(text.index("最多 2 句"), text.index("不要照抄内容"))

    def test_bad_types_do_not_raise(self):
        cases = [
            ChatStyle(name="a", display_name="A", description="", system_prompt=None),
            ChatStyle(name="a", display_name="A", description="", system_prompt="x", examples="不是列表"),
            ChatStyle(name="a", display_name="A", description="", system_prompt="x", examples=[1, 2]),
            ChatStyle(name="a", display_name="A", description="", system_prompt="", examples=None),
        ]
        for style in cases:
            with self.subTest(style=style):
                self.assertIsInstance(style.prompt_text(), str)

    def test_empty_prompt_still_returns_rules(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="   ")
        self.assertIn("最多 2 句", style.prompt_text())


class DefaultPersonaFileTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PERSONA_FILE.is_file(), f"默认人设文件不存在：{PERSONA_FILE}")
        self.data = yaml.safe_load(PERSONA_FILE.read_text(encoding="utf-8"))

    def test_identity_fields(self):
        # name 有代码与既有配置依赖，不允许改
        self.assertEqual(self.data["name"], "tsundere_whale_girl")
        self.assertTrue(self.data.get("display_name"))
        self.assertTrue(self.data.get("description"))

    def test_prompt_is_substantial(self):
        self.assertGreater(len(self.data.get("system_prompt") or ""), 80)

    def test_numbers_in_range(self):
        self.assertTrue(0 <= float(self.data["temperature"]) <= 1.5)
        self.assertTrue(50 <= int(self.data["max_tokens"]) <= 1000)

    def test_examples_are_short_and_complete(self):
        examples = self.data.get("examples") or []
        self.assertGreaterEqual(len(examples), 5)
        for item in examples:
            with self.subTest(user=item.get("user")):
                self.assertTrue(str(item.get("user") or "").strip())
                assistant = str(item.get("assistant") or "").strip()
                self.assertTrue(assistant)
                self.assertLessEqual(len(assistant), 80, "示例回复太长，会被模型照抄成长回复")

    def test_examples_avoid_written_style_and_stage_directions(self):
        for item in self.data.get("examples") or []:
            assistant = str(item.get("assistant") or "")
            for word in ("首先", "其次", "综上所述"):
                self.assertNotIn(word, assistant)
            self.assertNotIn("（", assistant)
            self.assertNotIn("）", assistant)

    def test_no_bom(self):
        raw = PERSONA_FILE.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "人设文件不能带 BOM")


class PersonaManagerPromptTests(unittest.TestCase):
    def test_manager_builds_prompt_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "styles").mkdir()
            (root / "styles" / "p.yaml").write_text(
                "name: p\ndisplay_name: P\nsystem_prompt: 你好\ntemperature: 0.5\nmax_tokens: 100\n",
                encoding="utf-8")
            manager = StyleManager(str(root / "styles"), str(root / "styles" / "custom"))
            manager.load_all()
            self.assertIn("最多 2 句", manager.get().prompt_text())

    def test_switch_to_missing_name_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "styles").mkdir()
            manager = StyleManager(str(root / "styles"), str(root / "styles" / "custom"))
            manager.load_all()
            self.assertFalse(manager.set_active("nope"))


if __name__ == "__main__":
    unittest.main()
