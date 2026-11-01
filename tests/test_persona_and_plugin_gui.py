"""人设（自定义）与插件详细配置界面的离线测试。

背景：这两项是用户新提的需求 ——
1. 人设原本被硬锁定为 tsundere_whale_girl，且配置层会强制覆盖 active；
2. 插件只有"能开能关"，没有按 schema 的详细配置界面。

本文件覆盖后端部分（人设 CRUD API 用的 StyleManager 方法、解锁行为）。
GUI 部分（PersonaTab / PluginConfigDialog / PluginTab）在有无 PyQt6 时分别验证：
有 PyQt6 就真正实例化控件并检查生成结果，没有就跳过。
"""

import pathlib
import tempfile
import unittest
import unittest.mock

from src.style_manager import DEFAULT_PERSONA, ChatStyle, StyleManager


class ChatStyleDictTests(unittest.TestCase):
    def test_to_dict_round_trip_fields(self):
        style = ChatStyle(
            name="cat", display_name="猫娘", description="测试",
            system_prompt="你是猫娘", temperature=0.9, max_tokens=123,
            examples=[{"user": "hi", "assistant": "喵"}],
        )
        data = style.to_dict()
        self.assertEqual(data["name"], "cat")
        self.assertEqual(data["display_name"], "猫娘")
        self.assertEqual(data["temperature"], 0.9)
        self.assertEqual(data["max_tokens"], 123)
        self.assertEqual(data["examples"], [{"user": "hi", "assistant": "喵"}])

    def test_to_dict_copies_examples(self):
        style = ChatStyle(name="a", display_name="A", description="", system_prompt="",
                          examples=[{"user": "u", "assistant": "a"}])
        data = style.to_dict()
        data["examples"][0]["user"] = "changed"
        self.assertEqual(style.examples[0]["user"], "u")   # 不能把内部列表暴露出去


class PersonaManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        (self.dir / "styles").mkdir()
        # 造一个"内置"人设，验证内置不可覆盖/不可删
        (self.dir / "styles" / "builtin.yaml").write_text(
            "name: builtin\ndisplay_name: 内置\nsystem_prompt: hi\ntemperature: 0.5\nmax_tokens: 100\n",
            encoding="utf-8")
        self.manager = StyleManager(str(self.dir / "styles"), str(self.dir / "styles" / "custom"))
        self.manager.load_all()

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_persona_constant(self):
        self.assertEqual(DEFAULT_PERSONA, "tsundere_whale_girl")

    def test_save_persona_creates_custom_file(self):
        name = self.manager.save_persona({
            "name": "my_cat", "display_name": "我的猫娘", "system_prompt": "你是猫娘",
            "temperature": 0.8, "max_tokens": 200,
            "examples": [{"user": "hi", "assistant": "喵"}],
        })
        self.assertEqual(name, "my_cat")
        self.assertTrue((self.dir / "styles" / "custom" / "my_cat.yaml").is_file())
        saved = self.manager.get_for_edit("my_cat")
        self.assertEqual(saved.display_name, "我的猫娘")
        self.assertEqual(saved.temperature, 0.8)
        self.assertEqual(len(saved.examples), 1)

    def test_save_persona_rejects_bad_name(self):
        for bad in ("", "../evil", "a/b", "有中文", "x" * 64, "a b"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    self.manager.save_persona({"name": bad})

    def test_save_persona_rejects_builtin_overwrite(self):
        with self.assertRaises(ValueError):
            self.manager.save_persona({"name": "builtin", "system_prompt": "改掉"})

    def test_save_persona_clamps_numbers(self):
        self.manager.save_persona({"name": "hot", "temperature": 9.0, "max_tokens": 999999})
        saved = self.manager.get_for_edit("hot")
        self.assertLessEqual(saved.temperature, 2.0)
        self.assertLessEqual(saved.max_tokens, 8000)

    def test_save_persona_bad_numbers_raise(self):
        for field in ("temperature", "max_tokens"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.manager.save_persona({"name": "x1", field: "abc"})

    def test_rename_removes_old_file(self):
        self.manager.save_persona({"name": "old_name", "system_prompt": "a"})
        self.manager.save_persona({"name": "new_name", "original_name": "old_name",
                                   "system_prompt": "b"})
        self.assertFalse((self.dir / "styles" / "custom" / "old_name.yaml").exists())
        self.assertTrue((self.dir / "styles" / "custom" / "new_name.yaml").exists())
        self.assertIsNone(self.manager.get_for_edit("old_name"))
        self.assertIsNotNone(self.manager.get_for_edit("new_name"))

    def test_parse_examples_text(self):
        text = "用户：你好\n你：哼\n\n用户：在吗\n你：在的\n垃圾行"
        examples = StyleManager._parse_examples_text(text)
        self.assertEqual(examples, [{"user": "你好", "assistant": "哼"},
                                    {"user": "在吗", "assistant": "在的"}])

    def test_parse_examples_text_empty(self):
        self.assertEqual(StyleManager._parse_examples_text(""), [])
        self.assertEqual(StyleManager._parse_examples_text(None), [])
        self.assertEqual(StyleManager._parse_examples_text("你：没有上文"), [])

    def test_save_persona_accepts_examples_text(self):
        self.manager.save_persona({"name": "texty", "examples": "用户：a\n你：b"})
        self.assertEqual(self.manager.get_for_edit("texty").examples, [{"user": "a", "assistant": "b"}])

    def test_switch_persona_is_allowed(self):
        # 解锁后的关键行为：能切到自定义人设
        self.manager.save_persona({"name": "my_cat", "system_prompt": "x"})
        self.assertTrue(self.manager.set_active("my_cat"))
        self.assertEqual(self.manager.active_name, "my_cat")

    def test_switch_to_missing_persona_fails(self):
        self.assertFalse(self.manager.set_active("no_such_persona"))

    def test_default_persona_cannot_be_deleted(self):
        self.assertFalse(self.manager.remove_style(DEFAULT_PERSONA))

    def test_custom_persona_can_be_deleted(self):
        self.manager.save_persona({"name": "temp_one", "system_prompt": "x"})
        self.assertTrue(self.manager.remove_style("temp_one"))
        self.assertIsNone(self.manager.get_for_edit("temp_one"))


class ConfigLockTests(unittest.TestCase):
    """配置层不再把人设钉死（否则自定义人设一保存就被打回）。"""

    def test_enforce_style_lock_keeps_user_choice(self):
        from src.config_manager import ConfigManager

        manager = ConfigManager.__new__(ConfigManager)
        manager._config = {"style": {"active": "my_cat"}}
        manager._enforce_style_lock()
        self.assertEqual(manager._config["style"]["active"], "my_cat")
        self.assertFalse(manager._config["style"]["locked"])

    def test_enforce_style_lock_fills_default_when_missing(self):
        from src.config_manager import LOCKED_STYLE_NAME, ConfigManager

        manager = ConfigManager.__new__(ConfigManager)
        manager._config = {}
        manager._enforce_style_lock()
        self.assertEqual(manager._config["style"]["active"], LOCKED_STYLE_NAME)


class MissingPersonaFallbackTests(unittest.TestCase):
    def test_missing_locked_persona_does_not_crash(self):
        # 旧行为：锁定人设文件缺失会抛 RuntimeError，导致换部署树就打不开
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "styles").mkdir()
            manager = StyleManager(str(root / "styles"), str(root / "styles" / "custom"),
                                   DEFAULT_PERSONA)
            manager.load_all()      # 不该抛
            self.assertIn(manager.active_name, ("default", DEFAULT_PERSONA))


try:
    from PyQt6.QtWidgets import QApplication
    import qq_gui

    _APP = QApplication.instance() or QApplication([])
    HAS_QT = True
except Exception:      # 没有 PyQt6 时跳过 GUI 用例
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class PersonaTabTests(unittest.TestCase):
    def test_tab_builds_and_new_clears_form(self):
        tab = qq_gui.PersonaTab()
        tab._new()
        self.assertEqual(tab.ed_name.text(), "")
        self.assertEqual(tab.ed_prompt.toPlainText(), "")
        self.assertIn("新建", tab.notice.text())

    def test_payload_parses_examples_text(self):
        tab = qq_gui.PersonaTab()
        tab.ed_name.setText("demo")
        tab.ed_display.setText("演示")
        tab.ed_examples.setPlainText("用户：hi\n你：yo")
        payload = tab._payload(rename=False)
        self.assertEqual(payload["name"], "demo")
        self.assertEqual(payload["examples"], "用户：hi\n你：yo")   # 由后端宽松解析

    def test_save_without_name_warns(self):
        tab = qq_gui.PersonaTab()
        tab._new()
        tab._save(rename=False)
        self.assertIn("标识", tab.notice.text())

    def test_banned_words_field_exists_and_renders_lines(self):
        # 违禁词框：每行一个词，保存时原样交给后端解析
        tab = qq_gui.PersonaTab()
        self.assertTrue(hasattr(tab, "ed_banned"), "人设页要有违禁词输入框")
        self.assertTrue(hasattr(tab, "btn_banned"), "人设页要有保存违禁词的按钮")
        tab.ed_banned.setPlainText("违禁词A\nbadword")
        self.assertIn("违禁词A", tab.ed_banned.toPlainText())

    def test_banned_words_save_posts_text_and_reports(self):
        tab = qq_gui.PersonaTab()
        tab.ed_banned.setPlainText("违禁词A, badword")
        captured = {}

        def fake_post(path, data=None, timeout=10):
            captured["path"] = path
            captured["data"] = data
            return {"success": True, "words": ["违禁词A", "badword"], "message": "已保存 2 个违禁词"}

        with unittest.mock.patch.object(qq_gui, "api_post", side_effect=fake_post):
            tab._save_banned()
        self.assertEqual(captured["path"], "personas/banned-words")
        self.assertEqual(captured["data"], {"words": "违禁词A, badword"})
        self.assertIn("2", tab.notice.text())
        self.assertEqual(tab.ed_banned.toPlainText(), "违禁词A\nbadword", "后端规范化后回填")

    def test_banned_word_save_failure_pops_explanation(self):
        tab = qq_gui.PersonaTab()
        tab.ed_banned.setPlainText("违禁词A")
        with unittest.mock.patch.object(qq_gui, "api_post",
                                        return_value={"success": False, "message": "保存失败"}), \
             unittest.mock.patch.object(qq_gui, "tell") as told:
            tab._save_banned()
        told.assert_not_called()          # 普通失败只写在页内提示，不弹窗
        self.assertIn("保存失败", tab.notice.text())

    def test_persona_save_hit_banned_word_pops_dialog(self):
        tab = qq_gui.PersonaTab()
        tab.ed_name.setText("demo")
        with unittest.mock.patch.object(qq_gui, "api_post",
                                        return_value={"success": False,
                                                      "message": "人设命中违禁词：违禁词A。已拒绝保存"}), \
             unittest.mock.patch.object(qq_gui, "tell") as told:
            tab._save(rename=False)
        told.assert_called_once()
        args = told.call_args.args
        self.assertIn("违禁词", args[2] if len(args) > 2 else str(told.call_args.kwargs))


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class PluginConfigDialogTests(unittest.TestCase):
    def _plugin(self):
        return {
            "id": "demo_plugin", "name": "演示插件", "description": "说明",
            "enabled": False,
            "settings": {"flag": True, "count": 5, "mode": "b", "note": "hi"},
            "defaults": {"flag": False, "count": 1, "mode": "a", "note": ""},
            "schema": {
                "flag": {"type": "bool", "label": "开关"},
                "count": {"type": "int", "label": "数量", "min": 0, "max": 10},
                "mode": {"type": "select", "label": "模式", "values": ["a", "b"]},
                "note": {"type": "string", "label": "备注", "multiline": True},
            },
        }

    def test_widget_types_match_schema(self):
        dialog = qq_gui.PluginConfigDialog(self._plugin())
        self.assertIsInstance(dialog._widgets["flag"][0], qq_gui.QCheckBox)
        self.assertIsInstance(dialog._widgets["count"][0], qq_gui.QSpinBox)
        self.assertIsInstance(dialog._widgets["mode"][0], qq_gui.QComboBox)
        self.assertIsInstance(dialog._widgets["note"][0], qq_gui.QTextEdit)

    def test_values_reflect_current_settings(self):
        dialog = qq_gui.PluginConfigDialog(self._plugin())
        values = dialog.values()
        self.assertTrue(values["flag"])
        self.assertEqual(values["count"], 5)
        self.assertEqual(values["mode"], "b")
        self.assertEqual(values["note"], "hi")

    def test_reset_restores_defaults(self):
        dialog = qq_gui.PluginConfigDialog(self._plugin())
        dialog._reset()
        values = dialog.values()
        self.assertFalse(values["flag"])
        self.assertEqual(values["count"], 1)
        self.assertEqual(values["mode"], "a")

    def test_schema_key_fallback_for_legacy_shape(self):
        plugin = self._plugin()
        plugin["config_schema"] = plugin.pop("schema")
        plugin["default_settings"] = plugin.pop("defaults")
        dialog = qq_gui.PluginConfigDialog(plugin)
        self.assertEqual(len(dialog._widgets), 4)

    def test_plugin_without_schema_has_no_fields(self):
        dialog = qq_gui.PluginConfigDialog({"id": "x", "name": "x"})
        self.assertEqual(dialog._widgets, {})


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class PluginPayloadContractTests(unittest.TestCase):
    """保存/开关时发出去的 body 必须**只含合法设置字段**。

    回归测试：GUI 曾在 body 里多塞一个 ``plugin_id``，而后端
    ``PluginManager.update_settings`` 对不在 config_schema 里的键直接抛
    ``ValueError: <id>: unknown setting field 'plugin_id'`` ——
    结果是界面里**任何插件都保存不了、开关也点不动**（用户实测报过这个 bug）。
    插件 id 只在 URL 里，body 里不能有。
    """

    def _plugin(self):
        return {
            "id": "demo_plugin", "name": "演示插件", "description": "说明", "enabled": False,
            "settings": {"flag": True, "count": 5},
            "defaults": {"flag": False, "count": 1},
            "schema": {
                "flag": {"type": "bool", "label": "开关"},
                "count": {"type": "int", "label": "数量", "min": 0, "max": 10},
            },
        }

    def _capture(self):
        """替换 qq_gui.api_post，返回结果容器。"""
        captured = {}

        def fake_post(path, data=None, timeout=10):
            captured["path"] = path
            captured["data"] = data
            return {"success": True}

        original = qq_gui.api_post
        qq_gui.api_post = fake_post
        self.addCleanup(lambda: setattr(qq_gui, "api_post", original))
        return captured

    def test_save_body_contains_only_schema_keys(self):
        dialog = qq_gui.PluginConfigDialog(self._plugin())
        captured = self._capture()
        dialog._save()
        self.assertEqual(captured["path"], "plugins/demo_plugin")
        self.assertEqual(sorted(captured["data"]), ["count", "flag"])
        self.assertNotIn("plugin_id", captured["data"])

    def test_save_body_has_no_id_like_fields(self):
        dialog = qq_gui.PluginConfigDialog(self._plugin())
        captured = self._capture()
        dialog._save()
        for key in captured["data"]:
            self.assertNotIn("id", key.lower(), f"body 里不应出现标识类字段：{key}")

    def test_toggle_body_contains_only_enabled(self):
        tab = qq_gui.PluginTab()
        plugin = self._plugin()
        tab._rows["demo_plugin"] = {
            "frame": None,
            "toggle": qq_gui.QCheckBox(),
            "plugin": plugin,
        }
        captured = self._capture()
        tab._toggle("demo_plugin", True)
        self.assertEqual(captured["path"], "plugins/demo_plugin")
        self.assertEqual(captured["data"], {"enabled": True})

    def test_save_reports_backend_failure(self):
        # 后端返回失败时不能假装成功
        dialog = qq_gui.PluginConfigDialog(self._plugin())
        self._capture_failing()
        with unittest.mock.patch.object(qq_gui, "tell") as warned:
            dialog._save()
        self.assertTrue(warned.called)
        self.assertFalse(dialog.saved)

    def _capture_failing(self):
        original = qq_gui.api_post
        qq_gui.api_post = lambda path, data=None, timeout=10: {"success": False,
                                                              "message": "boom"}
        self.addCleanup(lambda: setattr(qq_gui, "api_post", original))


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class PluginTabTests(unittest.TestCase):
    def test_row_is_built_from_plugin_dict(self):
        tab = qq_gui.PluginTab()
        row = tab._make_row({"id": "p1", "name": "插件一", "category": "rhythm",
                             "description": "说明", "enabled": True,
                             "schema": {"a": {"type": "bool", "label": "A"}}})
        self.assertIsNotNone(row)
        self.assertEqual(row.findChildren(qq_gui.QCheckBox)[0].isChecked(), True)

    def test_row_without_schema_shows_no_config_button(self):
        tab = qq_gui.PluginTab()
        row = tab._make_row({"id": "p2", "name": "插件二", "description": ""})
        labels = [w.text() for w in row.findChildren(qq_gui.QLabel)]
        self.assertTrue(any("无可配置项" in text for text in labels))


if __name__ == "__main__":
    unittest.main()
