"""人设热加载 / system 提示词合并 / 关键词回复可见性（真机 2026-10-17）。

用户反馈："提示词完全没有用啊，关键词回复[也没用]"。

查下来三件事：
1. `StyleManager.load_all()` **只在启动时调用一次** —— 用户用编辑器改完人设 YAML，
   跑的还是内存里的旧人设，所以"改了提示词完全没变"；
2. `_build_payload` 无条件在消息前面插一条 system：如果对话里本来就有 system，
   就会出现**两条 system**，部分模型/中转站只吃第一条（表现为"人设提示词被吃掉"）；
3. `keyword_reply` 在用户配置里是 `enabled: false` + `rules: ""`：插件本身没问题
   （配上规则一发就回，见下面的端到端用例），但"配了却不回复"没有任何提示。
"""
import asyncio
import os
import pathlib
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai_engine import AIEngine  # noqa: E402
from src.keyword_rules import write_rule_rows  # noqa: E402
from src.qq_app import QQRuntime  # noqa: E402
from src.style_manager import StyleManager  # noqa: E402

USER_ROOT = pathlib.Path.home() / "AppData" / "Local" / "WhaleGirlQQ"
GROUP = "A9FC46C6BC722A1B8BAADC3F8B2D65AD"


class PersonaHotReloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        (root / "custom").mkdir()
        (root / "a.yaml").write_text("name: a\nsystem_prompt: 第一版提示词\n", encoding="utf-8")
        self.manager = StyleManager(str(root), str(root / "custom"), None, str(root / "img"))
        self.manager.load_all()
        self.manager.set_active("a")
        self.file = root / "a.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_change_means_no_reload(self):
        self.assertFalse(self.manager.reload_if_stale())

    def test_edited_file_is_reloaded_and_keeps_active_persona(self):
        time.sleep(0.02)
        self.file.write_text("name: a\nsystem_prompt: 第二版提示词（改过了）\n", encoding="utf-8")
        self.assertTrue(self.manager.reload_if_stale(), "文件改过就应当热加载")
        self.assertIn("第二版", self.manager.full_prompt(self.manager.get()))
        self.assertEqual(self.manager.get().name, "a", "热加载不能把当前人设换掉")

    def test_second_call_is_noop(self):
        time.sleep(0.02)
        self.file.write_text("name: a\nsystem_prompt: 第三版\n", encoding="utf-8")
        self.assertTrue(self.manager.reload_if_stale())
        self.assertFalse(self.manager.reload_if_stale(), "已经重载过就不再重复")


class SystemPromptMergeTests(unittest.TestCase):
    def engine(self):
        return AIEngine(api_url="https://x/v1/chat/completions", api_key="k", model="m")

    def test_no_duplicate_system_message(self):
        payload = self.engine()._build_payload(
            [{"role": "system", "content": "旧人设"}, {"role": "user", "content": "你好"}],
            "新人设", 0.7)
        roles = [item["role"] for item in payload["messages"]]
        self.assertEqual(roles.count("system"), 1, f"只应有一条 system：{roles}")
        self.assertEqual(payload["messages"][0]["content"], "新人设", "应当用最新的提示词覆盖")

    def test_system_is_inserted_when_missing(self):
        payload = self.engine()._build_payload([{"role": "user", "content": "你好"}], "人设", 0.7)
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "人设"})

    def test_plugin_system_messages_are_kept(self):
        payload = self.engine()._build_payload(
            [{"role": "system", "content": "旧"},
             {"role": "user", "content": "x"},
             {"role": "system", "content": "插件指令"}],
            "新人设", 0.7)
        texts = [item["content"] for item in payload["messages"] if item["role"] == "system"]
        self.assertIn("新人设", texts)
        self.assertIn("插件指令", texts, "插件按轮插入的 system 不能丢")


class KeywordReplyVisibilityTests(unittest.TestCase):
    """插件本身要能真正回复；配了却不生效要有明确提示。"""

    def make_bot(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: None)
        root = pathlib.Path(tmp)
        (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
        shutil.copytree(USER_ROOT / "styles", root / "styles", dirs_exist_ok=True)
        (root / "config.qq.yaml").write_text(
            "ai:\n  api_url: https://x/v1/chat/completions\n  api_key: k\n  model: m\n"
            "qq:\n  app_id: a\n  client_secret: s\nqq_only:\n  mode: official\n", encoding="utf-8")
        bot = QQRuntime(root=root, config_path=root / "config.qq.yaml",
                        db_path=root / "t.sqlite3")
        bot.running = True
        bot.official.authenticated = True
        self.addCleanup(bot.db.close)
        return bot

    def test_configured_keyword_replies(self):
        bot = self.make_bot()
        rules = write_rule_rows([("你好", "你好呀，找本鲸有事？")])
        bot.plugins.update_settings("keyword_reply", {"enabled": True, "rules": rules})
        sent = []

        async def fake_send(*args, **kwargs):
            sent.append(args[1] if len(args) > 1 else kwargs)
            return {"id": "1"}

        bot.official.send = fake_send

        async def main():
            bot.worker = asyncio.create_task(bot._work())
            bot.ingest({"id": "K1", "kind": "group", "target": GROUP, "text": "你好",
                        "sender_id": "S1", "sender_name": "西莲寺紫", "at": True,
                        "media": [], "at_ids": [], "notice": None}, "official")
            await asyncio.sleep(1.0)
        asyncio.run(main())
        bot.running = False
        self.assertEqual(sent, ["你好呀，找本鲸有事？"], f"关键词规则应当立刻回复：{sent}")

    def test_rules_but_disabled_gets_a_note(self):
        bot = self.make_bot()
        notes = []
        bot.note = lambda text: notes.append(text)
        bot.plugins.update_settings("keyword_reply",
                                    {"enabled": False, "rules": write_rule_rows([("你好", "在")])})
        bot.running = False          # start() 在 running=True 时直接返回
        asyncio.run(bot.start())
        bot.running = False
        joined = "\n".join(notes)
        self.assertIn("停用", joined, f"有规则但停用要说清楚：{notes}")
        self.assertIn("关键词", joined)

    def test_enabled_without_rules_gets_a_note(self):
        bot = self.make_bot()
        notes = []
        bot.note = lambda text: notes.append(text)
        bot.plugins.update_settings("keyword_reply", {"enabled": True, "rules": ""})
        bot.running = False
        asyncio.run(bot.start())
        bot.running = False
        joined = "\n".join(notes)
        self.assertIn("规则是空的", joined, f"开着但没规则也要说清楚：{notes}")


if __name__ == "__main__":
    unittest.main()
