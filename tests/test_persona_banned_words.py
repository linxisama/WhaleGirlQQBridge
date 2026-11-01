"""人设违禁词：命中就拒绝落盘 + 暂停自动回复，并且程序始终能正常启动。

用户的要求原文：人设上设置违禁词，触犯就暂停程序并不保存当前配置，
然后要保证退出后还能正常进入程序。这里把四件事都钉住：

1. 命中违禁词 → 抛 ``BannedWordError``，**旧文件、旧内存状态一个都不动**；
2. 走 HTTP 路由保存时 → 400 + ``reply_paused=True``，且**不写盘**；
3. 已经存在的人设（先存后加词）不许切到前台；
4. 出过这些事之后，程序**照样能启动**：暂停只留在内存里，人设文件损坏也不挡启动。
"""
import os
import pathlib
import tempfile
import unittest

import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from fastapi.testclient import TestClient

    from src.qq_app import QQRuntime, create_app

    HAS_CLIENT = True
except Exception:
    HAS_CLIENT = False

from src.style_manager import BannedWordError, StyleManager  # noqa: E402

BUILTIN = """name: tsundere_whale_girl
display_name: 傲娇鲸鱼娘
description: 默认人设
system_prompt: 你是傲娇鲸鱼娘。
temperature: 0.7
max_tokens: 500
"""

CLEAN_PERSONA = {
    "name": "my_persona",
    "display_name": "自定义人设",
    "description": "测试用",
    "system_prompt": "你是测试人设，说话简短。",
}


def build_root(tmp: str) -> pathlib.Path:
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "tsundere_whale_girl.yaml").write_text(BUILTIN, encoding="utf-8")
    return root


class BannedWordParsingTests(unittest.TestCase):
    """词表解析：列表、换行、逗号、分号都认，去空去重。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.manager = StyleManager(str(build_root(self._tmp.name) / "styles"),
                                    str(build_root(self._tmp.name) / "styles" / "custom"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_text_input_splits_on_newline_comma_semicolon(self):
        self.assertEqual(self.manager.set_banned_words("甲\n乙, 丙；丁"), ["甲", "乙", "丙", "丁"])

    def test_list_input_is_cleaned_and_deduplicated(self):
        self.assertEqual(self.manager.set_banned_words([" 甲 ", "", "甲", "乙"]), ["甲", "乙"])

    def test_deduplication_ignores_case(self):
        self.assertEqual(self.manager.set_banned_words(["Bad", "bad", "BAD"]), ["Bad"])

    def test_overlong_word_is_dropped_and_cap_is_200(self):
        words = self.manager.set_banned_words(["x" * 41] + [f"w{i}" for i in range(260)])
        self.assertEqual(len(words), 200)
        self.assertNotIn("x" * 41, words)

    def test_violations_are_case_insensitive_and_ordered(self):
        self.manager.set_banned_words(["开挂", "Bad"])
        self.assertEqual(self.manager.violations("这游戏BAD开挂了吧"), ["开挂", "Bad"])

    def test_empty_word_list_never_matches(self):
        self.manager.set_banned_words([])
        self.assertEqual(self.manager.violations("随便什么内容"), [])


class PersonaSaveGuardTests(unittest.TestCase):
    """保存守卫：命中违禁词不落盘，正常内容原子写盘。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.custom = self.root / "styles" / "custom"
        self.manager = StyleManager(str(self.root / "styles"), str(self.custom))
        self.manager.load_all()
        self.manager.set_banned_words(["违禁词A", "badword"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_banned_word_in_prompt_is_rejected_without_writing(self):
        with self.assertRaises(BannedWordError) as caught:
            self.manager.save_persona(dict(CLEAN_PERSONA, system_prompt="这里含 违禁词A 哦"))
        self.assertIn("违禁词A", caught.exception.words)
        self.assertFalse((self.custom / "my_persona.yaml").exists(), "命中违禁词不许写文件")
        self.assertNotIn("my_persona", self.manager._styles, "内存状态也不许变")

    def test_rejection_keeps_previous_file_content(self):
        self.manager.save_persona(dict(CLEAN_PERSONA))
        path = self.custom / "my_persona.yaml"
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(BannedWordError):
            self.manager.save_persona(dict(CLEAN_PERSONA, system_prompt="BADWORD 出现"))
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_banned_word_in_example_or_display_name_is_caught(self):
        with self.assertRaises(BannedWordError):
            self.manager.save_persona(dict(CLEAN_PERSONA, examples=[{"user": "hi", "assistant": "违禁词A"}]))
        with self.assertRaises(BannedWordError):
            self.manager.save_persona(dict(CLEAN_PERSONA, display_name="违禁词A 小号"))

    def test_clean_persona_is_written_atomically(self):
        name = self.manager.save_persona(dict(CLEAN_PERSONA))
        self.assertEqual(name, "my_persona")
        path = self.custom / "my_persona.yaml"
        self.assertTrue(path.is_file())
        self.assertFalse((self.custom / "my_persona.yaml.tmp").exists(), "临时文件必须被替换掉")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["name"], "my_persona")

    def test_set_active_refuses_persona_that_hits_banned_words(self):
        # 先在没有词表时存下来，再设置词表 —— 这条路径挡的正是"先存后加词"
        self.manager.set_active("tsundere_whale_girl")
        self.manager.set_banned_words([])
        self.manager.save_persona(dict(CLEAN_PERSONA, system_prompt="里面藏着 badword"))
        self.manager.set_banned_words(["badword"])
        self.assertFalse(self.manager.set_active("my_persona"))
        self.assertEqual(self.manager.active_name, "tsundere_whale_girl")
        self.assertEqual(self.manager.blocked_persona, "my_persona")

    def test_set_active_allows_clean_persona(self):
        self.manager.save_persona(dict(CLEAN_PERSONA))
        self.assertTrue(self.manager.set_active("my_persona"))
        self.assertEqual(self.manager.blocked_persona, "")


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class PersonaBannedWordRouteTests(unittest.TestCase):
    """真实路由：保存被拒 + 暂停回复 + 重启后一切正常。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.config = self.root / "config.qq.yaml"
        self.config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake-key", "model": "test-model"},
            "qq": {"app_id": "123", "client_secret": "fake-secret"},
        }), encoding="utf-8")
        self._runtimes = []
        self._clients = []
        self.bot = self._runtime()
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()
        self._clients.append(self.client)

    def _runtime(self):
        runtime = QQRuntime(root=self.root, config_path=self.config,
                            db_path=self.root / "test.sqlite")
        self._runtimes.append(runtime)
        return runtime

    def _restart(self):
        """模拟"退出程序后重新进入"：新开一个运行时和客户端。"""
        runtime = self._runtime()
        client = TestClient(create_app(runtime))
        client.__enter__()
        self._clients.append(client)
        return runtime, client

    def tearDown(self):
        for client in self._clients:
            try:
                client.__exit__(None, None, None)
            except Exception:
                pass
        for runtime in self._runtimes:
            try:
                runtime.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def _set_words(self, text):
        return self.client.post("/api/personas/banned-words", json={"words": text})

    def test_banned_words_round_trip_and_persist(self):
        response = self._set_words("违禁词A\nbadword")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["words"], ["违禁词A", "badword"])
        self.assertEqual(self.client.get("/api/personas/banned-words").json()["words"],
                         ["违禁词A", "badword"])
        saved = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["style"]["banned_words"], ["违禁词A", "badword"])

    def test_save_with_banned_word_returns_400_pauses_and_writes_nothing(self):
        self._set_words("违禁词A")
        response = self.client.post("/api/personas", json=dict(
            CLEAN_PERSONA, name="bad_one", system_prompt="你是 违禁词A 变的"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("违禁词", response.json()["detail"])
        self.assertFalse((self.root / "styles" / "custom" / "bad_one.yaml").exists())
        self.assertTrue(self.bot.reply_paused, "命中违禁词必须暂停自动回复")
        self.assertIn("违禁词", self.bot.banned_note)
        self.assertIn("违禁词", self.client.get("/api/status").json()["banned_note"])

    def test_activate_violating_persona_is_refused_and_pauses(self):
        self.client.post("/api/personas", json=dict(CLEAN_PERSONA))
        # 人设里已经有"测试人设"这几个字，把它加成违禁词即可复现"先存后加词"
        self._set_words("测试人设")
        response = self.client.post("/api/personas/my_persona/activate")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.bot.reply_paused)
        self.assertNotEqual(self.client.get("/api/personas").json()["active"], "my_persona")

    def test_clean_save_still_works_and_persona_is_listed(self):
        self._set_words("违禁词A")
        response = self.client.post("/api/personas", json=dict(CLEAN_PERSONA))
        self.assertEqual(response.status_code, 200)
        names = [p["name"] for p in self.client.get("/api/personas").json()["personas"]]
        self.assertIn("my_persona", names)

    def test_restart_after_violation_is_clean(self):
        """出过违禁词之后，重新打开程序必须一切正常（暂停不落盘）。"""
        self._set_words("违禁词A")
        self.client.post("/api/personas", json=dict(CLEAN_PERSONA, name="bad_one",
                                                    system_prompt="违禁词A"))
        self.assertTrue(self.bot.reply_paused)

        restarted, client = self._restart()   # 模拟退出后重新进入程序
        self.assertFalse(restarted.reply_paused, "暂停只应留在内存里，重启后不该继续暂停")
        self.assertEqual(restarted.banned_note, "")
        self.assertTrue(restarted.style.banned_words, "违禁词表本身要保留")
        self.assertEqual(client.get("/api/personas").status_code, 200)
        self.assertEqual(client.get("/api/status").status_code, 200)

    def test_corrupt_persona_file_does_not_block_startup(self):
        (self.root / "styles" / "custom" / "broken.yaml").write_text(
            "name: [这不是合法的人设\n", encoding="utf-8")
        _restarted, client = self._restart()
        response = client.get("/api/personas")
        self.assertEqual(response.status_code, 200)
        names = [p["name"] for p in response.json()["personas"]]
        self.assertIn("tsundere_whale_girl", names, "坏文件不该带走其它人设")

    def test_startup_pauses_once_when_active_persona_hits_words(self):
        """已经开始用的人设后来被加进违禁词：启动就暂停，但程序照常打开。"""
        self.client.post("/api/personas", json=dict(CLEAN_PERSONA, name="risky",
                                                    system_prompt="里面写着 badword"))
        self.client.post("/api/personas/risky/activate")
        self._set_words("badword")

        restarted, client = self._restart()
        self.assertTrue(restarted.reply_paused)
        self.assertIn("badword", restarted.banned_note)
        self.assertEqual(client.get("/api/personas").status_code, 200)


if __name__ == "__main__":
    unittest.main()
