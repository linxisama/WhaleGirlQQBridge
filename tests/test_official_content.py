"""官方消息段的解析测试（表情标记 / 段数组 / 媒体占位）。

背景（用户实测报的 bug）：机器人在官方接口下把收到的表情看成"一串看不懂的符号"。
根因在 ``official_event``：
- ``content`` 被直接 ``str()``，如果是段数组就会变成
  ``<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">`` 这种 repr 天书；
- ``media`` 写死为空列表，图片/语音段全被丢掉；
- 于是 face-decode 插件也救不了它 —— 插件只处理 ``[表情N]`` 形态，够不到这种标记。

现在解析在传输层完成：段数组与纯文本标记两种形态都译成可读文本，
并把媒体段保留进 ``media``。
"""

import base64
import unittest

from src.qq_transports import (
    FACE_MARKUP_RE,
    face_name,
    official_content,
    official_event,
    translate_face_markup,
)


def markup(face_id, face_type=6, text=""):
    ext = base64.b64encode(f'{{"text":"{text}"}}'.encode()).decode()
    return f'<faceType={face_type},faceId="{face_id}",ext="{ext}">'


class FaceMarkupTests(unittest.TestCase):
    def test_real_markup_from_user_report(self):
        # 用户截图里那条真实标记
        self.assertEqual(translate_face_markup('<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'),
                         "[表情:惊讶]")

    def test_known_ids_translate(self):
        for face_id, name in ((0, "惊讶"), (6, "害羞"), (14, "微笑")):
            with self.subTest(face_id=face_id):
                self.assertEqual(translate_face_markup(markup(face_id)), f"[表情:{name}]")

    def test_unknown_id_kept_as_number(self):
        # 宁可不译不许编
        self.assertEqual(translate_face_markup(markup(99999)), "[表情99999]")

    def test_markup_without_ext(self):
        self.assertEqual(translate_face_markup('<faceType=1,faceId="14">'), "[表情:微笑]")

    def test_surrounding_text_preserved(self):
        text = f"笑死{markup(14)}了"
        self.assertEqual(translate_face_markup(text), "笑死[表情:微笑]了")

    def test_multiple_markups(self):
        text = f"{markup(0)}和{markup(99999)}"
        self.assertEqual(translate_face_markup(text), "[表情:惊讶]和[表情99999]")

    def test_ext_text_appended(self):
        raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0Ijoi5ZOI5ZOIIn0=">'   # {"text":"哈哈"}
        self.assertIn("哈哈", translate_face_markup(raw))

    def test_plain_text_untouched(self):
        for text in ("普通消息", "", None, "no face here"):
            with self.subTest(text=text):
                expected = "" if text is None else text
                self.assertEqual(translate_face_markup(text), expected)

    def test_idempotent_on_translated(self):
        once = translate_face_markup(markup(14))
        self.assertEqual(translate_face_markup(once), once)

    def test_regex_matches_both_ext_forms(self):
        self.assertIsNotNone(FACE_MARKUP_RE.search(markup(0)))
        self.assertIsNotNone(FACE_MARKUP_RE.search('<faceType=1,faceId="5">'))

    def test_face_name_helper(self):
        self.assertEqual(face_name(14), "微笑")
        self.assertEqual(face_name("14"), "微笑")      # 字符串 id 也要认
        self.assertEqual(face_name(99999), "")
        self.assertEqual(face_name(None), "")


class OfficialContentTests(unittest.TestCase):
    def test_segment_array(self):
        text, media = official_content([
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "face", "data": {"id": 6}},
            {"type": "image", "data": {"url": "https://x/a.jpg", "width": 200}},
            {"type": "voice", "data": {"url": "https://x/b.silk"}},
        ])
        self.assertEqual(text, "看这个[表情:害羞][图片][语音]")
        self.assertEqual([m["type"] for m in media], ["face", "image", "record"])
        self.assertEqual(media[1]["url"], "https://x/a.jpg")

    def test_image_url_thumbnail_suffix_stripped(self):
        _, media = official_content([{"type": "image", "data": {"url": "https://x/a.jpg!100"}}])
        self.assertEqual(media[0]["url"], "https://x/a.jpg")

    def test_image_signature_query_is_preserved(self):
        url = "https://example.test/a.jpg!100?signature=one!two&expires=123"
        _, media = official_content([{"type": "image", "data": {"url": url}}])
        self.assertEqual(media[0]["url"], "https://example.test/a.jpg?signature=one!two&expires=123")

    def test_inline_image_file_survives_official_segment(self):
        value = "base64://iVBORw0KGgo="
        _, media = official_content([{"type": "image", "data": {"file": value}}])
        self.assertEqual(media[0]["file"], value)

    def test_video_and_file_placeholders(self):
        text, media = official_content([
            {"type": "video", "data": {"url": "https://x/v.mp4"}},
            {"type": "file", "data": {"file_name": "a.zip"}},
        ])
        self.assertEqual(text, "[视频][file]")
        self.assertEqual(media[1]["file_name"], "a.zip")

    def test_at_segment(self):
        text, _ = official_content([{"type": "at", "data": {"qq": "12345"}}])
        self.assertEqual(text, "[@12345]")

    def test_unknown_segment_is_labelled_not_swallowed(self):
        text, _ = official_content([{"type": "markdown", "data": {"x": 1}}])
        self.assertEqual(text, "[markdown]")

    def test_string_content_translates_markup(self):
        text, media = official_content(markup(0))
        self.assertEqual(text, "[表情:惊讶]")
        self.assertEqual(media, [])

    def test_plain_string_unchanged(self):
        self.assertEqual(official_content("普通消息"), ("普通消息", []))

    def test_garbage_content_does_not_raise(self):
        for value in (None, 42, {"type": "text"}, [None, "x", 3], []):
            with self.subTest(value=value):
                text, media = official_content(value)
                self.assertIsInstance(text, str)
                self.assertIsInstance(media, list)

    def test_no_repr_leak(self):
        # 关键回归：段数组绝不能被 str() 成 <faceType=...> 天书
        text, _ = official_content([
            {"type": "face", "data": {"id": 0}},
            {"type": "image", "data": {"url": "https://x/a.jpg"}},
        ])
        self.assertNotIn("faceType", text)
        self.assertNotIn("{", text)


class OfficialEventTests(unittest.TestCase):
    def _data(self, content):
        return {"id": "M1", "group_openid": "G1", "content": content,
                "author": {"member_openid": "U1", "username": "主人"}}

    def test_markup_content_end_to_end(self):
        message = official_event("GROUP_AT_MESSAGE_CREATE", self._data(markup(0)))
        self.assertEqual(message["text"], "[表情:惊讶]")
        self.assertEqual(message["sender_id"], "U1")
        self.assertTrue(message["at"])

    def test_segment_array_end_to_end(self):
        message = official_event("GROUP_AT_MESSAGE_CREATE", self._data([
            {"type": "text", "data": {"text": "你好"}},
            {"type": "image", "data": {"url": "https://x/a.jpg"}},
        ]))
        self.assertEqual(message["text"], "你好[图片]")
        self.assertEqual([m["type"] for m in message["media"]], ["image"])

    def test_c2c_uses_author_openid(self):
        data = {"id": "M2", "content": "私聊", "author": {"user_openid": "U2"}}
        message = official_event("C2C_MESSAGE_CREATE", data)
        self.assertEqual(message["kind"], "private")
        self.assertEqual(message["target"], "U2")

    def test_image_attachments_without_standard_mime_private_and_group(self):
        for event in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            for extra in ({"filename": "a.PNG"}, {"content_type": "application/octet-stream"},
                          {"type": "image", "url": "//example.test/opaque"},
                          {"content_type": "IMAGE/JPEG"}):
                with self.subTest(event=event, extra=extra):
                    data = self._data("")
                    data["author"]["user_openid"] = "U1"
                    data["attachments"] = [dict(url="//example.test/photo.jpg?signature=one!two", **extra)] if "url" not in extra else [extra]
                    message = official_event(event, data)
                    self.assertEqual(message["text"], "[图片]")
                    self.assertEqual(len(message["media"]), 1)
                    self.assertTrue(message["media"][0]["url"].startswith("https://"))
                    if "url" not in extra:
                        self.assertIn("signature=one!two", message["media"][0]["url"])

    def test_non_image_attachments_are_not_promoted_to_images(self):
        data = self._data("")
        data["attachments"] = [{"content_type": "application/pdf", "filename": "misleading.png"},
                               {"url": "https://example.test/file.pdf"},
                               {"content_type": "audio/silk", "url": "https://example.test/audio.png"}]
        message = official_event("GROUP_AT_MESSAGE_CREATE", data)
        self.assertFalse(any(m['type'] == 'image' for m in message['media']))
        self.assertEqual(len(message['media']), 1)
        self.assertEqual(message['media'][0]['type'], 'record')
        self.assertEqual(message['media'][0]['url'], 'https://example.test/audio.png')
        self.assertEqual(message['text'], '[语音]')

    def test_non_message_event_returns_none(self):
        for event in ("SOMETHING_ELSE", "GROUP_ADD_ROBOT", None, ""):
            with self.subTest(event=event):
                self.assertIsNone(official_event(event, self._data("x")))

    def test_missing_fields_return_none(self):
        self.assertIsNone(official_event("GROUP_AT_MESSAGE_CREATE",
                                         {"content": "x", "group_openid": "G"}))
        self.assertIsNone(official_event("GROUP_AT_MESSAGE_CREATE", None))


if __name__ == "__main__":
    unittest.main()
