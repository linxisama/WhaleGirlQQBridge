"""官方 QQ 的引用消息 / 聊天记录 / 卡片：入站解析 + 出站引用回复。

用户反馈（2026-10-17）：群里引用一条消息再 @ 机器人，"这个无法回复引用信息啊"。
核对官方文档后确认原因：引用(103)/聊天记录(102)/卡片(3) 的正文都在
``msg_elements`` / ``ark_data`` 里，而我们只读了顶层 ``content`` —— 于是被 @ 的那条
变成空正文，机器人只能瞎答。

同时官方支持出站引用：``message_reference: {message_id: <REFIDX>}``，
REFIDX 来自入站事件 ``message_scene.ext`` 的 ``msg_idx``。
"""
import asyncio
import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx  # noqa: E402

from src.qq_elements import render_elements, scene_refs  # noqa: E402
from src.qq_transports import OfficialQQ, official_event  # noqa: E402

GROUP = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
AUTHOR = {"id": "D4E5F6", "member_openid": "D4E5F6", "member_role": "member", "username": "七"}


def event(**extra):
    data = {"id": "ROBOT1.0_msg", "author": dict(AUTHOR), "content": " ",
            "group_openid": GROUP, "timestamp": "2026-10-17T19:30:00+08:00"}
    data.update(extra)
    return official_event("GROUP_AT_MESSAGE_CREATE", data)


class QuoteParsingTests(unittest.TestCase):
    def test_quote_message_103_keeps_quoted_content(self):
        message = event(message_type=103,
                        msg_elements=[{"content": "=== 消息 1 ===\n[消息内容] 今天的学习计划已完成"}],
                        message_scene={"source": "default",
                                       "ext": ["msg_idx=REFIDX_abc==", "auth_token=t",
                                               "ref_msg_idx=TMP_xxx"]})
        self.assertIn("今天的学习计划已完成", message["text"], "被引用的内容必须进正文")
        self.assertTrue(message["text"].startswith("[引用]"))
        self.assertTrue(message.get("_is_quote"))
        self.assertEqual(message.get("_quote_ref"), "REFIDX_abc==",
                         "出站引用要用 REFIDX（msg_idx），不是消息 id")

    def test_chat_record_102_is_marked(self):
        message = event(message_type=102,
                        msg_elements=[{"content": "=== 消息 1 ===\n[消息内容] 哈哈哈\n\n"
                                                 "=== 消息 2 ===\n[消息内容] 笑死"}])
        self.assertIn("聊天记录", message["text"])
        self.assertIn("笑死", message["text"])
        self.assertTrue(message.get("_is_chat_record"))

    def test_card_3_renders_title_desc_and_link(self):
        message = event(message_type=3, ark_data={
            "prompt": "[QQ小程序]哔哩哔哩", "ark_type": "miniapp", "ark_name": "小程序",
            "fields": {"title": "【鲸鱼娘】手书", "desc": "播放 12.3万",
                       "jump_url": "https://b23.tv/xyz"}})
        self.assertIn("卡片", message["text"])
        self.assertIn("【鲸鱼娘】手书", message["text"])
        self.assertIn("播放 12.3万", message["text"])
        self.assertTrue(message.get("_is_card"))

    def test_nested_elements_are_walked(self):
        message = event(message_type=103, msg_elements=[{
            "content": "这是回复",
            "msg_elements": [{"content": "被引用的原话"}]}])
        self.assertIn("被引用的原话", message["text"],
                      f"嵌套元素也要读出来：{message['text']}")

    def test_long_chat_record_is_clipped(self):
        blob = "啊" * 5000
        message = event(message_type=102, msg_elements=[{"content": blob}])
        self.assertLess(len(message["text"]), 2000, "长聊天记录要截断，别撑爆提示词")
        self.assertIn("已省略", message["text"])

    def test_quoted_image_is_not_this_message_media(self):
        message = event(message_type=103, msg_elements=[{
            "content": "看这张",
            "attachments": [{"content_type": "image/png", "url": "https://x/q.png"}]}])
        self.assertEqual([m for m in message["media"]], [],
                         "被引用的图片不该当成本条消息的图片（否则会去识图）")

    def test_scene_refs(self):
        self.assertEqual(scene_refs({"message_scene": {"ext": ["msg_idx=REFIDX_1"]}}),
                         {"msg_idx": "REFIDX_1", "is_quote": False, "ref_msg_idx": ""})
        self.assertTrue(scene_refs({"message_scene": {"ext": ["ref_msg_idx=T"]}})["is_quote"])
        self.assertEqual(scene_refs({})["msg_idx"], "")

    def test_plain_text_message_unaffected(self):
        message = event(content="在吗")
        self.assertEqual(message["text"], "在吗")
        self.assertNotIn("_quote_ref", message)


class OutboundReferenceTests(unittest.TestCase):
    def transport(self):
        qq = OfficialQQ("appid", "secret", sandbox=False)
        qq.token, qq.expires = "T", 10 ** 12
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"id": "ok"})

        qq.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return qq, captured

    def test_reply_quotes_when_ref_present(self):
        qq, captured = self.transport()
        asyncio.run(qq.send({"kind": "group", "target": GROUP, "id": "M1",
                             "_quote_ref": "REFIDX_abc==", "_quote_reply": True}, "回应你引用的那条"))
        self.assertEqual(captured["body"]["message_reference"], {"message_id": "REFIDX_abc=="})

    def test_plain_reply_has_no_reference(self):
        qq, captured = self.transport()
        asyncio.run(qq.send({"kind": "group", "target": GROUP, "id": "M2"}, "普通回复"))
        self.assertNotIn("message_reference", captured["body"])

    def test_blank_reference_is_ignored(self):
        qq, captured = self.transport()
        asyncio.run(qq.send({"kind": "group", "target": GROUP, "id": "M3",
                             "_quote_ref": "   "}, "回复"))
        self.assertNotIn("message_reference", captured["body"])


class RenderElementsUnitTests(unittest.TestCase):
    def test_depth_limit_does_not_loop(self):
        deep = {"content": "第一层", "msg_elements": []}
        node = deep
        for index in range(8):
            child = {"content": f"第{index}层", "msg_elements": []}
            node["msg_elements"].append(child)
            node = child
        text, _media, _marks = render_elements([deep])
        self.assertIn("第一层", text)


if __name__ == "__main__":
    unittest.main()
