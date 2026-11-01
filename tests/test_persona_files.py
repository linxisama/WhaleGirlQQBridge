"""人设的持久化与"保存/导入文件"。

用户反馈两件事：
1. 换过人设，重启后又回到原来那个（不记录上一次的选择）；
2. 想要"保存人设"和"导入人设"，并且能直接选文件。

这里把两件事都钉住：``style.active`` 写进配置并在下次启动时生效；导出/导入走同一个
规整函数，重名自动改名、不覆盖任何人设，违禁词照样拒绝导入并暂停。
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime, create_app  # noqa: E402
from src.style_manager import BannedWordError, StyleManager  # noqa: E402

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

PERSONA_YAML = """name: whale
display_name: 傲娇鲸鱼娘
description: 群里撒娇用
system_prompt: 你是傲娇鲸鱼娘。
temperature: 0.9
max_tokens: 320
keywords:
  - 鲸鱼娘
  - 小鲸鱼
examples:
  - user: 你好
    assistant: 哼，找我干嘛啦
"""


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    return root


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class ActivePersonaSurvivesRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self.tmp.name)
        self.config = self.root / "config.qq.yaml"
        self.config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
            "qq": {"app_id": "app", "client_secret": "secret"},
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def boot(self):
        bot = QQRuntime(root=self.root, config_path=self.config, db_path=self.root / "test.sqlite")
        client = TestClient(create_app(bot))
        client.__enter__()
        return bot, client

    def stop(self, bot, client):
        client.__exit__(None, None, None)
        try:
            bot.db.close()
        except Exception:
            pass

    def test_switching_persona_is_remembered_after_restart(self):
        bot, client = self.boot()
        self.assertEqual(client.post("/api/personas", json={
            "name": "whale", "display_name": "傲娇鲸鱼娘", "system_prompt": "你是傲娇鲸鱼娘。",
            "keywords": "鲸鱼娘"}).status_code, 200)
        self.assertEqual(client.post("/api/personas/whale/activate").status_code, 200)
        self.assertEqual(bot.style.active_name, "whale")
        self.stop(bot, client)

        saved = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["style"]["active"], "whale", "切换要写进配置，否则重启就丢")

        bot2, client2 = self.boot()                      # 等价"重启程序"
        try:
            self.assertEqual(bot2.style.active_name, "whale", "重启后应继续用上次选的人设")
            listed = client2.get("/api/personas").json()
            self.assertEqual(listed["active"], "whale")
        finally:
            self.stop(bot2, client2)

    def test_missing_persona_in_config_falls_back_instead_of_crashing(self):
        self.config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
            "qq": {"app_id": "app", "client_secret": "secret"},
            "style": {"active": "deleted-persona", "banned_words": []},
        }), encoding="utf-8")
        bot, client = self.boot()
        try:
            self.assertIn(bot.style.active_name, bot.style._styles)
            listed = client.get("/api/personas").json()      # 这个接口不返回 success 字段
            self.assertIn(listed["active"], {item["name"] for item in listed["personas"]})
        finally:
            self.stop(bot, client)


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class PersonaExportImportRouteTests(unittest.TestCase):
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

    def export(self, **overrides):
        payload = {"name": "whale", "display_name": "傲娇鲸鱼娘", "description": "群里撒娇用",
                   "system_prompt": "你是傲娇鲸鱼娘。", "temperature": 0.9, "max_tokens": 320,
                   "keywords": "鲸鱼娘, 小鲸鱼",
                   "examples": "用户：你好\n你：哼，找我干嘛啦"}
        payload.update(overrides)
        return self.client.post("/api/personas/export", json=payload)

    def test_export_returns_yaml_of_the_current_form(self):
        response = self.export()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["filename"], "persona-whale.yaml")
        data = yaml.safe_load(body["content"])
        self.assertEqual(data["name"], "whale")
        self.assertEqual(data["keywords"], ["鲸鱼娘", "小鲸鱼"])
        self.assertEqual(data["examples"], [{"user": "你好", "assistant": "哼，找我干嘛啦"}])
        self.assertEqual(data["max_tokens"], 320)

    def test_export_reports_banned_words_without_writing(self):
        self.client.post("/api/personas/banned-words", json={"words": "坏词"})
        response = self.export(system_prompt="这里有坏词")
        self.assertEqual(response.status_code, 400)
        self.assertIn("违禁词", response.json()["detail"])
        self.assertEqual(list((self.root / "styles" / "custom").glob("*.yaml")), [])

    def test_import_round_trip_keeps_content(self):
        exported = self.export().json()["content"]
        response = self.client.post("/api/personas/import",
                                    json={"filename": "persona-whale.yaml", "content": exported})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "whale")
        detail = self.client.get("/api/personas/whale").json()
        self.assertEqual(detail["display_name"], "傲娇鲸鱼娘")
        self.assertEqual(detail["keywords"], ["鲸鱼娘", "小鲸鱼"])
        self.assertEqual(detail["examples"], [{"user": "你好", "assistant": "哼，找我干嘛啦"}])

    def test_import_renames_instead_of_overwriting(self):
        self.client.post("/api/personas/import", json={"filename": "a.yaml", "content": PERSONA_YAML})
        again = self.client.post("/api/personas/import", json={"filename": "a.yaml", "content": PERSONA_YAML})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["name"], "whale-2")
        self.assertEqual(again.json()["renamed_from"], "whale")
        self.assertEqual(self.client.get("/api/personas/whale").json()["display_name"], "傲娇鲸鱼娘")

    def test_import_cannot_shadow_a_builtin_persona(self):
        response = self.client.post("/api/personas/import", json={
            "filename": "x.yaml",
            "content": "name: default\ndisplay_name: 假的默认\nsystem_prompt: 我是假的\n"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "default-2")
        self.assertEqual(self.client.get("/api/personas/default").json()["display_name"], "默认")

    def test_import_uses_filename_when_name_missing(self):
        response = self.client.post("/api/personas/import", json={
            "filename": "my-persona.yaml", "content": "display_name: 无名人设\nsystem_prompt: 你好\n"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "my-persona")

    def test_import_rejects_broken_or_unusable_files(self):
        cases = [
            ("不是 yaml: [", "YAML"),
            ("just a string", "不是一个人设"),
            ("name: x\n", "人设本体"),
            ("", "空的"),
            ("name: x\nsystem_prompt: 你好\n", ""),
        ]
        for content, expected in cases:
            response = self.client.post("/api/personas/import",
                                        json={"filename": "f.yaml", "content": content})
            if expected:
                self.assertEqual(response.status_code, 400, content)
                self.assertIn(expected, response.json()["detail"], content)

    def test_import_of_oversized_file_is_rejected(self):
        blob = "name: big\nsystem_prompt: " + "字" * (300 * 1024)
        response = self.client.post("/api/personas/import", json={"filename": "big.yaml", "content": blob})
        self.assertEqual(response.status_code, 400)
        self.assertIn("太大", response.json()["detail"])

    def test_import_hits_banned_words_and_pauses(self):
        self.client.post("/api/personas/banned-words", json={"words": "坏词"})
        response = self.client.post("/api/personas/import", json={
            "filename": "bad.yaml",
            "content": "name: bad\nsystem_prompt: 我是坏词人设\n"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("违禁词", response.json()["detail"])
        self.assertTrue(self.bot.reply_paused, "导入命中违禁词也要暂停自动回复")
        self.assertFalse((self.root / "styles" / "custom" / "bad.yaml").exists())

    def test_import_ignores_original_name_in_file(self):
        """文件里的 original_name 不能用来删掉别的自定义人设。"""
        self.client.post("/api/personas", json={"name": "victim", "system_prompt": "原来的人设"})
        response = self.client.post("/api/personas/import", json={
            "filename": "t.yaml",
            "content": "name: fresh\noriginal_name: victim\nsystem_prompt: 新来的\n"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.client.get("/api/personas/victim").json()["system_prompt"] == "原来的人设")


class FreeNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self.tmp.name)
        self.manager = StyleManager(styles_dir=self.root / "styles",
                                    custom_dir=self.root / "styles" / "custom")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sanitizes_and_avoids_taken_names(self):
        self.assertEqual(self.manager.free_name("default"), "default-2")
        slug = self.manager.free_name("我的 老婆!")
        self.assertRegex(slug, r"^[A-Za-z0-9_\-]{1,48}$")
        self.assertTrue(slug.startswith("persona-"), "中文名生成稳定 slug，展示名另存")
        self.manager.save_persona({"name": "whale", "system_prompt": "x"})
        self.assertEqual(self.manager.free_name("whale"), "whale-2")

    def test_render_persona_does_not_touch_memory_or_disk(self):
        text = self.manager.render_persona({"name": "draft", "system_prompt": "草稿",
                                            "keywords": "草, 稿"})
        self.assertIn("draft", text)
        self.assertNotIn("draft", self.manager._styles)
        self.assertEqual(list((self.root / "styles" / "custom").glob("*.yaml")), [])

    def test_banned_word_error_type_is_used_for_export(self):
        self.manager.set_banned_words(["坏词"])
        with self.assertRaises(BannedWordError):
            self.manager.render_persona({"name": "x", "system_prompt": "坏词"})


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class PersonaFileGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_buttons_exist(self):
        tab = qq_gui.PersonaTab()
        for attr in ("btn_export", "btn_import"):
            self.assertTrue(hasattr(tab, attr), attr)

    def test_export_writes_file_from_backend_content(self):
        tab = qq_gui.PersonaTab()
        tab.ed_name.setText("whale")
        tab.ed_keywords.setText("鲸鱼娘")
        target = pathlib.Path(tempfile.mkdtemp()) / "out.yaml"
        with mock.patch.object(qq_gui, "api_post",
                               return_value={"success": True, "filename": "persona-whale.yaml",
                                             "content": "name: whale\n"}) as post, \
             mock.patch.object(qq_gui.file_dialogs, "save_file", return_value=(str(target), "")):
            tab._export_persona()
        self.assertEqual(post.call_args.args[0], "personas/export")
        self.assertEqual(target.read_text(encoding="utf-8"), "name: whale\n")
        self.assertIn("已导出", tab.notice.text())

    def test_export_cancel_writes_nothing(self):
        tab = qq_gui.PersonaTab()
        tab.ed_name.setText("whale")
        with mock.patch.object(qq_gui, "api_post",
                               return_value={"success": True, "content": "x"}), \
             mock.patch.object(qq_gui.file_dialogs, "save_file", return_value=("", "")):
            tab._export_persona()
        self.assertEqual(tab.notice.text(), "")

    def test_import_uploads_file_content_and_reloads(self):
        source = pathlib.Path(tempfile.mkdtemp()) / "given.yaml"
        source.write_text("name: whale\nsystem_prompt: 你好\n", encoding="utf-8")
        tab = qq_gui.PersonaTab()
        with mock.patch.object(qq_gui, "api_post",
                               return_value={"success": True, "name": "whale", "message": "已导入人设「whale」"}) as post, \
             mock.patch.object(qq_gui.file_dialogs, "open_file", return_value=(str(source), "")), \
             mock.patch.object(tab, "refresh") as refresh, \
             mock.patch.object(tab, "_load_detail") as load:
            tab._import_persona()
        self.assertEqual(post.call_args.args[0], "personas/import")
        self.assertEqual(post.call_args.args[1]["filename"], "given.yaml")
        self.assertIn("system_prompt", post.call_args.args[1]["content"])
        refresh.assert_called_once()
        load.assert_called_once_with("whale")
        self.assertIn("已导入", tab.notice.text())

    def test_import_failure_is_explained(self):
        source = pathlib.Path(tempfile.mkdtemp()) / "bad.yaml"
        source.write_text("nope", encoding="utf-8")
        tab = qq_gui.PersonaTab()
        with mock.patch.object(qq_gui, "api_post", return_value={"success": False, "message": "文件不是有效的人设"}), \
             mock.patch.object(qq_gui.file_dialogs, "open_file", return_value=(str(source), "")), \
             mock.patch.object(qq_gui, "tell") as told:
            tab._import_persona()
        told.assert_called_once()
        self.assertIn("文件不是有效的人设", tab.notice.text())


if __name__ == "__main__":
    unittest.main()
