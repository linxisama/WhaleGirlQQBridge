"""人设关键词：每个人设各自一套触发词，命中就用那个人设回这一条。

用户要求："每个人设有每个人设的关键词。在人设内容地方添加一个关键词"。
这里钉住三件事：
- 关键词随人设保存/加载（YAML 往返、接口往返、界面字段）；
- 命中规则可预测（当前人设优先、长词优先、锁定/违禁词人设不参与）；
- 真回复路径上换的是**这一条**的人设提示词，不动"当前人设"。
"""
import asyncio
import os
import pathlib
import tempfile
import unittest
from unittest import mock
from unittest.mock import AsyncMock

import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime, create_app  # noqa: E402
from src.style_manager import MAX_KEYWORDS, ChatStyle, StyleManager, parse_keywords  # noqa: E402

try:
    from fastapi.testclient import TestClient

    HAS_CLIENT = True
except Exception:
    HAS_CLIENT = False

try:
    from PyQt6.QtWidgets import QApplication

    import qq_gui

    HAS_QT = True
except Exception:
    HAS_QT = False

BUILTIN = """name: default
display_name: 默认
description: 通用
system_prompt: 你是默认助手。
"""


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    return root


class ParseKeywordsTests(unittest.TestCase):
    def test_accepts_common_separators(self):
        self.assertEqual(parse_keywords("鲸鱼娘, 小鲸鱼；蓝鲸酱、小深\n出来"),
                         ["鲸鱼娘", "小鲸鱼", "蓝鲸酱", "小深", "出来"])

    def test_accepts_list_and_dedupes(self):
        self.assertEqual(parse_keywords(["鲸鱼娘", "鲸鱼娘", " 小鲸鱼 "]), ["鲸鱼娘", "小鲸鱼"])

    def test_drops_too_short_and_too_long(self):
        self.assertEqual(parse_keywords(["鲸", "好", "鲸鱼娘", "x" * 40]), ["鲸鱼娘"])

    def test_caps_count(self):
        words = parse_keywords([f"关键词{i:03d}" for i in range(80)])
        self.assertEqual(len(words), MAX_KEYWORDS)

    def test_bad_types_do_not_raise(self):
        for value in (None, 42, {"a": 1}, object()):
            self.assertEqual(parse_keywords(value), [])


class StyleManagerKeywordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self.tmp.name)
        self.manager = StyleManager(styles_dir=self.root / "styles",
                                    custom_dir=self.root / "styles" / "custom")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_reload_round_trip(self):
        self.manager.save_persona({"name": "whale", "display_name": "鲸鱼娘", "system_prompt": "傲娇",
                                   "keywords": "鲸鱼娘, 小鲸鱼"})
        self.assertEqual(self.manager.get_for_edit("whale").keywords, ["鲸鱼娘", "小鲸鱼"])
        raw = yaml.safe_load((self.root / "styles" / "custom" / "whale.yaml").read_text(encoding="utf-8"))
        self.assertEqual(raw["keywords"], ["鲸鱼娘", "小鲸鱼"])
        self.manager.load_all()                    # 重新加载（等价重启后读取）
        self.assertEqual(self.manager.get_for_edit("whale").keywords, ["鲸鱼娘", "小鲸鱼"])

    def test_to_dict_and_listing_expose_keywords(self):
        self.manager.save_persona({"name": "whale", "system_prompt": "傲娇", "keywords": ["鲸鱼娘"]})
        self.assertEqual(self.manager.get_for_edit("whale").to_dict()["keywords"], ["鲸鱼娘"])
        listed = {item["name"]: item for item in self.manager.list_styles()}
        self.assertEqual(listed["whale"]["keywords"], ["鲸鱼娘"])

    def test_keywords_participate_in_banned_word_scan(self):
        self.manager.set_banned_words(["坏词"])
        with self.assertRaises(Exception) as ctx:
            self.manager.save_persona({"name": "bad", "system_prompt": "正常", "keywords": ["坏词"]})
        self.assertIn("坏词", str(ctx.exception))

    def test_match_prefers_active_persona(self):
        self.manager.save_persona({"name": "a", "display_name": "A", "system_prompt": "甲",
                                   "keywords": "鲸鱼娘"})
        self.manager.save_persona({"name": "b", "display_name": "B", "system_prompt": "乙",
                                   "keywords": "鲸鱼娘"})
        self.manager.set_active("b")
        self.assertEqual(self.manager.match_keywords("鲸鱼娘在吗").name, "b",
                         "当前人设自己也命中时不该换人设")

    def test_match_longest_keyword_wins(self):
        self.manager.save_persona({"name": "short", "system_prompt": "甲", "keywords": "鲸鱼娘"})
        self.manager.save_persona({"name": "long", "system_prompt": "乙", "keywords": "鲸鱼娘二号"})
        self.assertEqual(self.manager.match_keywords("鲸鱼娘二号出来").name, "long")
        self.assertEqual(self.manager.match_keywords("鲸鱼娘出来").name, "short")

    def test_no_match_or_empty_text_returns_none(self):
        self.manager.save_persona({"name": "whale", "system_prompt": "傲娇", "keywords": "鲸鱼娘"})
        self.assertIsNone(self.manager.match_keywords("今天天气不错"))
        self.assertIsNone(self.manager.match_keywords(""))
        self.assertIsNone(self.manager.match_keywords(None))

    def test_locked_persona_disables_switching_by_keyword(self):
        self.manager.save_persona({"name": "whale", "system_prompt": "傲娇", "keywords": "鲸鱼娘"})
        self.manager._locked_name = "default"
        self.assertIsNone(self.manager.match_keywords("鲸鱼娘"))

    def test_persona_with_banned_word_is_not_summoned(self):
        self.manager.save_persona({"name": "whale", "system_prompt": "傲娇", "keywords": "鲸鱼娘"})
        self.manager.set_banned_words(["傲娇"])          # 事后加的违禁词
        self.assertIsNone(self.manager.match_keywords("鲸鱼娘"), "违规人设不许被关键词叫到前台")


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class PersonaKeywordRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self.tmp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / "test.sqlite")
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        try:
            self.bot.db.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_save_and_read_back_keywords(self):
        response = self.client.post("/api/personas", json={
            "name": "whale", "display_name": "鲸鱼娘", "system_prompt": "傲娇",
            "keywords": "鲸鱼娘, 小鲸鱼"})
        self.assertEqual(response.status_code, 200, response.text)
        detail = self.client.get("/api/personas/whale").json()
        self.assertEqual(detail["keywords"], ["鲸鱼娘", "小鲸鱼"])
        listed = self.client.get("/api/personas").json()
        entry = next(item for item in listed["personas"] if item["name"] == "whale")
        self.assertEqual(entry["keywords"], ["鲸鱼娘", "小鲸鱼"])

    def test_keyword_hit_chooses_persona_for_that_reply_only(self):
        """真回复路径：命中就换这一条的提示词，且不改"当前人设"。"""
        from tests.test_chat_features import message

        self.client.post("/api/personas", json={
            "name": "whale", "display_name": "鲸鱼娘", "system_prompt": "你是傲娇鲸鱼娘。",
            "keywords": "鲸鱼娘"})
        self.bot.style.set_active("default")
        self.bot.running = True
        self.bot.official.authenticated = True
        self.bot.official.send = AsyncMock()
        self.bot.ai.chat = AsyncMock(return_value="哼，找我干嘛啦")

        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.bot.ingest(message(mid="k1", text="鲸鱼娘在吗"), "official")
            for _ in range(100):
                if self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='sent'").fetchone()[0]:
                    break
                await asyncio.sleep(0.05)

        asyncio.run(run())
        prompt = self.bot.ai.chat.call_args.kwargs.get("system_prompt", "")
        self.assertIn("傲娇鲸鱼娘", prompt, "命中的那条要用被叫到的人设")
        self.assertEqual(self.bot.style.active_name, "default", "只是这一条，不改当前人设")
        notes = [str(v.get("text", "")) for v in getattr(self.bot, "events", [])]
        self.assertTrue(any("人设关键词" in text for text in notes), "控制台要留一句说明")


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class PersonaKeywordGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_field_exists_in_persona_form(self):
        tab = qq_gui.PersonaTab()
        self.assertTrue(hasattr(tab, "ed_keywords"), "人设内容里要有「关键词」")
        tab._new()
        self.assertEqual(tab.ed_keywords.text(), "")
        self.assertIn("关键词", tab.ed_keywords.placeholderText())

    def test_payload_carries_keywords(self):
        tab = qq_gui.PersonaTab()
        tab.ed_name.setText("whale")
        tab.ed_keywords.setText("鲸鱼娘, 小鲸鱼")
        self.assertEqual(tab._payload(rename=False)["keywords"], "鲸鱼娘, 小鲸鱼")

    def test_detail_load_shows_keywords(self):
        tab = qq_gui.PersonaTab()
        with mock.patch.object(qq_gui, "api_get",
                               return_value={"name": "whale", "display_name": "鲸鱼娘",
                                             "description": "", "system_prompt": "傲娇",
                                             "keywords": ["鲸鱼娘", "小鲸鱼"]}):
            tab._load_detail("whale")
        self.assertEqual(tab.ed_keywords.text(), "鲸鱼娘, 小鲸鱼")


if __name__ == "__main__":
    unittest.main()
