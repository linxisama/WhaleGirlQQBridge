"""forward_summary（聊天记录预览）的离线测试。

本插件只做**离线部分**：把合并转发的节点结构展开成可读文本并做预算截断。
摘要主路径里的图像识别与 OCR 当前**不可用**（本项目没有这两项能力），所以默认关闭。

审核修正记录（核心）：原稿有两处真 bug ——
① ``node_parts`` 把 ``text`` 写死成空串；② ``render_forward`` 另起一套逻辑只读
``node['content']``，而真实 OneBot 节点用 ``node['message']``，结果每行渲染成
「名字：」丢正文。下面 ``test_real_onebot_shape`` 就是钉住这两点的回归测试。
"""

import pathlib
import tempfile
import unittest

from src.plugins import PluginBase, PluginMeta
from src.plugins.forward_summary import (
    ForwardSummary,
    node_parts,
    nodes_of,
    render_forward,
    segment_text,
)


class FakeManager:
    def __init__(self, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(ForwardSummary.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class PluginDiscoveryTests(unittest.TestCase):
    def test_plugin_is_discoverable_by_plugin_manager(self):
        from src.plugins.manager import PluginManager

        self.assertTrue(issubclass(ForwardSummary, PluginBase),
                        "必须继承 src.plugins.PluginBase，不能自造一个")
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(pathlib.Path(tmp))
            self.assertIn("forward_summary", manager._plugins)
            self.assertTrue(manager._plugins["forward_summary"].enabled)

    def test_meta_is_complete(self):
        self.assertIsInstance(ForwardSummary.META, PluginMeta)
        self.assertTrue(ForwardSummary.META.enabled_by_default)
        self.assertTrue(ForwardSummary.META.default_settings["enabled"])
        schema = ForwardSummary.META.schema_dict()
        for key in ("enabled", "summaryChars", "maxNodes", "maxChars", "maxImages",
                    "adultFilter", "ocrFirst", "singleScreenshots", "maxOcrImages",
                    "maxOcrChars"):
            self.assertIn(key, schema)

    def test_documents_unavailable_ocr(self):
        description = ForwardSummary.META.description
        self.assertTrue("OCR" in description or "不可用" in description)


class NodesOfTests(unittest.TestCase):
    def test_top_level_messages(self):
        self.assertEqual(nodes_of({"messages": [1, 2]}), [1, 2])

    def test_nested_data_messages(self):
        self.assertEqual(nodes_of({"data": {"messages": [3]}}), [3])

    def test_non_list_and_none(self):
        for payload in (None, {}, {"messages": "no"}, {"data": {"messages": None}}, "x"):
            with self.subTest(payload=payload):
                self.assertEqual(nodes_of(payload), [])


class NodePartsTests(unittest.TestCase):
    def test_node_wrapper(self):
        parts = node_parts({"type": "node", "data": {"sender": {"card": "乙"},
                                                     "message": [{"type": "text", "data": {"text": "内容"}}]}})
        self.assertEqual(parts["name"], "乙")
        self.assertEqual(parts["text"], "内容")

    def test_plain_dict(self):
        parts = node_parts({"sender": {"nickname": "甲"},
                            "message": [{"type": "text", "data": {"text": "你好"}}]})
        self.assertEqual(parts["name"], "甲")
        self.assertEqual(parts["text"], "你好")

    def test_string_content(self):
        parts = node_parts({"nickname": "丙", "content": "纯文本"})
        self.assertEqual(parts["text"], "纯文本")

    def test_missing_sender(self):
        self.assertEqual(node_parts({"message": []})["name"], "某人")

    def test_name_newlines_replaced(self):
        parts = node_parts({"sender": {"nickname": "上\n下"}, "message": []})
        self.assertNotIn("\n", parts["name"])

    def test_garbage(self):
        self.assertEqual(node_parts(None), {"name": "某人", "text": ""})
        self.assertEqual(node_parts("x")["name"], "某人")


class SegmentTextTests(unittest.TestCase):
    def test_each_segment_type(self):
        cases = [
            ([{"type": "text", "data": {"text": "hi"}}], "hi"),
            ([{"type": "face", "data": {}}], "[表情]"),
            ([{"type": "image", "data": {}}], "[图片]"),
            ([{"type": "record", "data": {}}], "[语音]"),
            ([{"type": "video", "data": {}}], "[视频]"),
            ([{"type": "at", "data": {"qq": "123"}}], "[@123]"),
            ([{"type": "forward", "data": {}}], "[嵌套合并转发：未展开]"),
            ([{"type": "unknown", "data": {}}], "[unknown]"),
        ]
        for segments, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(segment_text(segments), expected)

    def test_nested_node_depth(self):
        inner = [{"type": "node", "data": {"message": [{"type": "text", "data": {"text": "深层"}}]}}]
        self.assertIn("内嵌消息", segment_text(inner, depth=0))
        self.assertIn("未展开", segment_text(inner, depth=2))

    def test_empty_and_garbage(self):
        self.assertEqual(segment_text([]), "")
        self.assertEqual(segment_text(None), "")
        self.assertEqual(segment_text("x"), "")
        self.assertEqual(segment_text([None, "x"]), "")


class RenderForwardTests(unittest.TestCase):
    NODES = [
        {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "第一句"}}]},
        {"type": "node", "data": {"sender": {"card": "乙"},
                                  "message": [{"type": "text", "data": {"text": "第二句"}},
                                              {"type": "image", "data": {}}]}},
    ]

    def test_real_onebot_shape(self):
        # 回归测试：早先只读 content，导致「甲：」丢正文
        result = render_forward(self.NODES, 10, 1000)
        self.assertIn("甲：第一句", result["text"])
        self.assertIn("乙：第二句[图片]", result["text"])
        self.assertEqual(result["nodes"], 2)
        self.assertFalse(result["partial"])

    def test_nodes_limit_marks_partial(self):
        result = render_forward(self.NODES, 1, 1000)
        self.assertTrue(result["partial"])
        self.assertEqual(result["nodes"], 1)
        self.assertIn("甲", result["text"])
        self.assertNotIn("乙", result["text"])

    def test_chars_limit_marks_partial(self):
        result = render_forward(self.NODES, 0, 3)
        self.assertTrue(result["partial"])

    def test_invalid_limits_do_not_raise(self):
        for nodes_limit, chars_limit in (("bad", -1), (None, None), (0, 0), (-5, "x")):
            with self.subTest(nodes=nodes_limit, chars=chars_limit):
                result = render_forward(self.NODES, nodes_limit, chars_limit)
                self.assertIn("甲", result["text"])

    def test_empty_input(self):
        result = render_forward([], 10, 100)
        self.assertEqual(result["text"], "")
        self.assertEqual(result["nodes"], 0)
        self.assertFalse(result["partial"])

    def test_garbage_input(self):
        result = render_forward(None, 10, 100)
        self.assertEqual(result["nodes"], 0)


class OnPreAiTests(unittest.TestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        return ForwardSummary(FakeManager(tmp.name, settings))

    def test_forward_with_nodes_returns_extra_system(self):
        plugin = self.make()
        message = {"media": [{"type": "forward", "nodes": [
            {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "内容"}}]}]}]}
        conversation = []
        hints = plugin.on_pre_ai(message, conversation, None)
        self.assertIn("甲：内容", conversation[0]['content'])
        self.assertEqual(conversation[0]['role'],'user')
        self.assertNotIn('甲：内容',hints['extra_system'])

    def test_forward_without_nodes_returns_none(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_pre_ai({"media": [{"type": "forward"}]}, [], None))

    def test_no_media_returns_none(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_pre_ai({"text": "普通消息"}, [], None))

    def test_disabled_returns_none(self):
        plugin = self.make(enabled=False)
        message = {"media": [{"type": "forward", "nodes": [{"nickname": "甲", "content": "x"}]}]}
        self.assertIsNone(plugin.on_pre_ai(message, [], None))

    def test_media_is_forward_list_returns_none(self):
        # media[0] 是 forward 段的**列表**时也不能崩
        plugin = self.make()
        self.assertIsNone(plugin.on_pre_ai({"media": [{"type": "forward", "nodes": "x"}]}, [], None))


if __name__ == "__main__":
    unittest.main()
