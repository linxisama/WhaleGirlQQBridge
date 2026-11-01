"""speech_to_text_qq（语音转文字）的离线测试。

本插件**默认关闭**：转写依赖协议端（NapCat/SnowLuma）的 `fetch_ptt_text` 接口，
是否支持尚未验证，所以按"照实实现但标注不可用"处理 —— 转写失败只显示占位，
绝不猜测内容。

审核修正记录（核心）：原稿有三处硬伤 ——
① docstring 少一个结束引号，导致整个文件无法编译；
② `PluginMeta` 用了 `name=`/`version=`/`author=`/`settings=`/`schema=` 这些不存在的参数，
   `config_schema` 还被写成 list、`FieldSchema(key=...)` 也没有 `key` 参数；
③ 调了基类没有的 `self.get_setting()` / `self.get_settings()`。
下面第一条测试就是钉住"能被真 PluginManager 发现"的硬门槛。
"""

import pathlib
import tempfile
import unittest

from src.plugins import PluginBase, PluginMeta
from src.plugins.speech_to_text_qq import (
    SpeechToTextQQ,
    find_records,
    parse_budget,
    record_label,
    should_transcribe,
)


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(SpeechToTextQQ.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class FakeOneBot:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    async def call(self, action, params=None):
        self.calls.append((action, params))
        if self.raises:
            raise self.raises
        return self.result


class FakeRuntime:
    def __init__(self, mode="onebot", onebot=None):
        self.mode = mode
        self.onebot = onebot


class PluginDiscoveryTests(unittest.TestCase):
    def test_plugin_is_discoverable_by_plugin_manager(self):
        from src.plugins.manager import PluginManager

        self.assertTrue(issubclass(SpeechToTextQQ, PluginBase),
                        "必须继承 src.plugins.PluginBase，不能自造一个")
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(pathlib.Path(tmp))
            self.assertIn("speech_to_text_qq", manager._plugins)
            self.assertFalse(manager._plugins["speech_to_text_qq"].enabled)

    def test_meta_is_complete_and_disabled_by_default(self):
        self.assertIsInstance(SpeechToTextQQ.META, PluginMeta)
        self.assertEqual(SpeechToTextQQ.META.id, "speech_to_text_qq")
        self.assertFalse(SpeechToTextQQ.META.enabled_by_default)
        self.assertFalse(SpeechToTextQQ.META.default_settings["enabled"])
        schema = SpeechToTextQQ.META.schema_dict()
        for key in ("enabled", "budgetMs", "timeoutMs", "debugLog"):
            self.assertIn(key, schema)

    def test_meta_has_no_bogus_keys(self):
        # 原稿给 PluginMeta 传了 version/author/settings/schema，这些字段不存在
        self.assertFalse(hasattr(SpeechToTextQQ, "enabled_by_default"))
        self.assertFalse(hasattr(SpeechToTextQQ, "config_schema"))


class PureFunctionTests(unittest.TestCase):
    def test_find_records(self):
        media = [{"type": "record", "file": "a.amr"},
                 {"type": "image", "file": "b.jpg"},
                 {"type": "record", "url": "http://x/c.amr"}]
        found = find_records(media)
        self.assertEqual(len(found), 2)
        self.assertEqual([item["type"] for item in found], ["record", "record"])

    def test_find_records_returns_copies(self):
        media = [{"type": "record", "file": "a.amr"}]
        found = find_records(media)
        found[0]["file"] = "changed"
        self.assertEqual(media[0]["file"], "a.amr")

    def test_find_records_garbage(self):
        for media in (None, "x", {}, [None, "x", 3]):
            with self.subTest(media=media):
                self.assertEqual(find_records(media), [])

    def test_record_label(self):
        self.assertEqual(record_label({"text": "你好"}), "[语音:你好]")
        self.assertEqual(record_label({}), "[语音]")
        self.assertEqual(record_label(None), "[语音]")
        self.assertEqual(record_label({"text": ""}), "[语音]")

    def test_parse_budget(self):
        self.assertEqual(parse_budget(3000, 8000), 3000)
        self.assertEqual(parse_budget("3000", 8000), 3000)
        self.assertEqual(parse_budget(3000.7, 8000), 3000)
        self.assertEqual(parse_budget(0, 8000), 8000)
        self.assertEqual(parse_budget(-5, 8000), 8000)
        self.assertEqual(parse_budget("abc", 8000), 8000)
        self.assertEqual(parse_budget(None, 8000), 8000)
        self.assertEqual(parse_budget(True, 8000), 8000)      # bool 不算数字

    def test_parse_budget_caps_at_60000(self):
        self.assertEqual(parse_budget(99999, 8000), 60000)

    def test_should_transcribe(self):
        item = {"file": "a.amr"}
        self.assertTrue(should_transcribe(item, True, True))
        self.assertTrue(should_transcribe({"url": "http://x"}, True, True))
        self.assertFalse(should_transcribe(item, True, False))     # 未启用
        self.assertFalse(should_transcribe(item, False, True))     # 协议端不支持
        self.assertFalse(should_transcribe({"nofile": 1}, True, True))
        self.assertFalse(should_transcribe(None, True, True))


class TranscribeTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SpeechToTextQQ(FakeManager(tmp.name, settings))

    async def test_returns_string_result(self):
        plugin = self.make()
        onebot = FakeOneBot(result="你好呀")
        self.assertEqual(await plugin.transcribe(FakeRuntime(onebot=onebot), {"file": "a.amr"}),
                         "你好呀")
        self.assertEqual(onebot.calls[0][0], "fetch_ptt_text")

    async def test_parses_dict_wrappers(self):
        for payload, expected in (({"text": "甲"}, "甲"),
                                  ({"data": {"text": "乙"}}, "乙"),
                                  ({"message": "丙"}, "丙")):
            with self.subTest(payload=payload):
                plugin = self.make()
                self.assertEqual(
                    await plugin.transcribe(FakeRuntime(onebot=FakeOneBot(result=payload)),
                                           {"file": "a.amr"}),
                    expected)

    async def test_unparsable_result_returns_none(self):
        for payload in (None, 42, {}, {"data": {}}, {"text": 5}):
            with self.subTest(payload=payload):
                plugin = self.make()
                self.assertIsNone(
                    await plugin.transcribe(FakeRuntime(onebot=FakeOneBot(result=payload)),
                                           {"file": "a.amr"}))

    async def test_exception_is_swallowed(self):
        plugin = self.make()
        runtime = FakeRuntime(onebot=FakeOneBot(raises=RuntimeError("协议端不支持")))
        self.assertIsNone(await plugin.transcribe(runtime, {"file": "a.amr"}))

    async def test_official_mode_does_not_call(self):
        plugin = self.make()
        onebot = FakeOneBot(result="x")
        self.assertIsNone(await plugin.transcribe(FakeRuntime(mode="official", onebot=onebot),
                                                  {"file": "a.amr"}))
        self.assertEqual(onebot.calls, [])

    async def test_missing_endpoint_returns_none(self):
        plugin = self.make()
        self.assertIsNone(await plugin.transcribe(FakeRuntime(onebot=None), {"file": "a.amr"}))

    async def test_missing_file_returns_none(self):
        plugin = self.make()
        onebot = FakeOneBot(result="x")
        self.assertIsNone(await plugin.transcribe(FakeRuntime(onebot=onebot), {}))
        self.assertEqual(onebot.calls, [])

    async def test_timeout_setting_is_passed(self):
        plugin = self.make(timeoutMs=1234)
        onebot = FakeOneBot(result="x")
        await plugin.transcribe(FakeRuntime(onebot=onebot), {"file": "a.amr"})
        self.assertEqual(onebot.calls[0][1]["timeout"], 1234)


class OnPreAiTests(unittest.TestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        return SpeechToTextQQ(FakeManager(tmp.name, settings))

    def test_annotates_records_when_enabled(self):
        plugin = self.make()
        hints = plugin.on_pre_ai({"media": [{"type": "record", "file": "a.amr"}]}, [], None)
        self.assertIn("[语音]", hints["extra_system"])
        self.assertIn("1", hints["extra_system"])

    def test_disabled_returns_none(self):
        plugin = self.make(enabled=False)
        self.assertIsNone(plugin.on_pre_ai({"media": [{"type": "record"}]}, [], None))

    def test_no_records_returns_none(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_pre_ai({"media": [{"type": "image"}]}, [], None))
        self.assertIsNone(plugin.on_pre_ai({"text": "普通消息"}, [], None))
        self.assertIsNone(plugin.on_pre_ai(None, [], None))


if __name__ == "__main__":
    unittest.main()
