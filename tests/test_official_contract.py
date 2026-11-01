"""官方 QQ 接口契约测试：断言我们真正发出的请求符合平台要求。

背景（用户要求"保证 QQ 端功能完整，测试完所有的问题后再打包"）：官方模式是用户实际在用的路径，
但此前只有"能发出去"的粗测。这里用 httpx.MockTransport 把**出站请求**逐项钉住：

- 鉴权：bots.qq.com/app/getAppAccessToken、令牌复用、401 强制换令牌后重试一次；
- 文本：POST /v2/groups|users/{openid}/messages，msg_type=0、带 msg_id 与 msg_seq；
- 图片：先 POST …/files（file_type=1、srv_send_msg=False）再以 msg_type=7 + media.file_info 发送；
- 错误：QQ 在 body 里给的错误码要翻成人话（额度/风控/过期/限流），不能再是裸 HTTP 500；
- 主动消息：不带 msg_id/msg_seq，配额不足时给出可读原因；
- 分句与长文本：递增 msg_seq，最多 5 条（QQ 上限），内容不丢；
- 入站事件：群@、单聊、附件图片、未知事件、缺字段。
"""
import asyncio
import base64
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx  # noqa: E402

from src.qq_app import (  # noqa: E402
    OFFICIAL_TEXT_LIMIT, QQRuntime, _send_with_pacing, _split_official_text,
)
from src.qq_transports import (  # noqa: E402
    OFFICIAL_EVENTS, OfficialQQ, QQSendError, describe_qq_error, official_event,
)

PNG_DATA_URL = ("data:image/png;base64,"
                + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 40).decode())


def message(kind="group", target="G1", mid="M1", seq=None):
    item = {"id": mid, "kind": kind, "target": target, "text": "鲸鱼娘在吗", "at": True, "media": []}
    if seq:
        item["_passive_seq"] = seq
    return item


class OfficialTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            url = str(request.url)
            if "getAppAccessToken" in url:
                return httpx.Response(200, json={"access_token": f"tok-{len(self.calls)}",
                                                 "expires_in": 7200})
            if url.endswith("/files"):
                return httpx.Response(200, json={"file_info": "FILEINFO", "ttl": 300})
            return httpx.Response(200, json={"id": "msg-1", "timestamp": int(time.time())})

        self.transport = httpx.MockTransport(handler)
        self.qq = OfficialQQ("appid-1", "secret-1")
        self.qq.http = httpx.AsyncClient(transport=self.transport, timeout=5)

    def tearDown(self):
        asyncio.run(self.qq.http.aclose())

    def body(self, index=-1):
        return json.loads(self.calls[index].content.decode())

    def auth_header(self, index=-1):
        return self.calls[index].headers.get("Authorization")

    # ── 鉴权 ────────────────────────────────────────────────────────────────
    def test_authorize_posts_app_credentials_and_reuses_token(self):
        async def run():
            first = await self.qq.authorize()
            second = await self.qq.authorize()
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first, second, "令牌没过期就该复用")
        self.assertIn("getAppAccessToken", str(self.calls[0].url))
        self.assertEqual(self.body(0), {"appId": "appid-1", "clientSecret": "secret-1"})

    def test_authorize_without_credentials_explains_itself(self):
        qq = OfficialQQ("", "")
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(qq.authorize())
        self.assertIn("AppID", str(ctx.exception))
        asyncio.run(qq.http.aclose())

    # ── 文本 ────────────────────────────────────────────────────────────────
    def test_group_text_reply_shape(self):
        asyncio.run(self.qq.send(message(), "在的呀"))
        request = self.calls[-1]
        self.assertEqual(request.method, "POST")
        self.assertTrue(str(request.url).endswith("/v2/groups/G1/messages"))
        self.assertTrue(str(self.auth_header()).startswith("QQBot "))
        self.assertEqual(request.headers.get("X-Union-Appid"), "appid-1")
        self.assertEqual(self.body(), {"content": "在的呀", "msg_type": 0,
                                       "msg_id": "M1", "msg_seq": 1})

    def test_private_text_reply_uses_users_path(self):
        asyncio.run(self.qq.send(message(kind="private", target="U1"), "在的呀"))
        self.assertTrue(str(self.calls[-1].url).endswith("/v2/users/U1/messages"))

    def test_passive_sequence_is_honoured_and_bounded(self):
        asyncio.run(self.qq.send(message(seq=3), "第三条"))
        self.assertEqual(self.body()["msg_seq"], 3)
        with self.assertRaises(QQSendError) as ctx:
            asyncio.run(self.qq.send(message(seq=6), "第六条"))
        self.assertIn("最多 5 条", str(ctx.exception))

    def test_unsupported_session_type_is_refused(self):
        with self.assertRaises(ValueError):
            asyncio.run(self.qq.send({"id": "M1", "kind": "channel", "target": "C1"}, "hi"))

    # ── 图片 ────────────────────────────────────────────────────────────────
    def test_image_reply_uploads_then_sends_media(self):
        meta = {"media": [{"type": "image", "data": {"url": PNG_DATA_URL}}]}
        asyncio.run(self.qq.send(message(), "给你一张", meta))
        upload, send = self.calls[-2], self.calls[-1]
        self.assertTrue(str(upload.url).endswith("/v2/groups/G1/files"))
        self.assertEqual(json.loads(upload.content.decode())["file_type"], 1)
        self.assertFalse(json.loads(upload.content.decode())["srv_send_msg"])
        self.assertIn("file_data", json.loads(upload.content.decode()))
        self.assertEqual(self.body(), {"content": "给你一张", "msg_type": 7, "msg_id": "M1",
                                       "msg_seq": 1, "media": {"file_info": "FILEINFO"}})

    def test_image_only_reply_sends_a_space_as_content(self):
        meta = {"media": [{"type": "image", "data": {"url": PNG_DATA_URL}}]}
        asyncio.run(self.qq.send(message(), "   ", meta))
        self.assertEqual(self.body()["content"], " ")
        self.assertEqual(self.body()["msg_type"], 7)

    def test_http_url_image_uses_url_field(self):
        meta = {"media": [{"type": "image", "data": {"url": "https://x.test/a.png"}}]}
        asyncio.run(self.qq.send(message(), "图", meta))
        self.assertEqual(json.loads(self.calls[-2].content.decode())["url"], "https://x.test/a.png")

    def test_two_images_are_refused_with_reason(self):
        meta = {"media": [{"type": "image", "data": {"url": "https://x.test/a.png"}},
                          {"type": "image", "data": {"url": "https://x.test/b.png"}}]}
        with self.assertRaises(QQSendError) as ctx:
            asyncio.run(self.qq.send(message(), "两张", meta))
        self.assertIn("一张", str(ctx.exception))

    def test_bad_image_source_is_refused(self):
        meta = {"media": [{"type": "image", "data": {"url": "file:///etc/passwd"}}]}
        with self.assertRaises(QQSendError) as ctx:
            asyncio.run(self.qq.send(message(), "图", meta))
        self.assertIn("不可用", str(ctx.exception))

    def test_upload_without_file_info_is_reported(self):
        def handler(request):
            self.calls.append(request)
            if "getAppAccessToken" in str(request.url):
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
            return httpx.Response(200, json={})

        self.qq.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
        meta = {"media": [{"type": "image", "data": {"url": "https://x.test/a.png"}}]}
        with self.assertRaises(QQSendError) as ctx:
            asyncio.run(self.qq.send(message(), "图", meta))
        self.assertIn("file_info", str(ctx.exception))

    # ── 错误翻译 ────────────────────────────────────────────────────────────
    def test_qq_error_codes_become_actionable_chinese(self):
        cases = {11244: "额度", 11253: "频繁", 11278: "过期", 11279: "5 次", 30404: "msg_seq",
                 40034: "权限"}
        for code, keyword in cases.items():
            text = describe_qq_error(400, {"code": code, "message": "raw"})
            self.assertIn(keyword, text, f"{code} → {text}")
        self.assertIn("限流", describe_qq_error(429, None))
        self.assertIn("鉴权", describe_qq_error(401, None))
        self.assertIn("权限", describe_qq_error(403, None))

    def test_send_raises_readable_error_from_body(self):
        def handler(request):
            self.calls.append(request)
            if "getAppAccessToken" in str(request.url):
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
            return httpx.Response(500, json={"code": 11244, "message": "no quota"})

        self.qq.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
        with self.assertRaises(QQSendError) as ctx:
            asyncio.run(self.qq.send(message(), "在吗"))
        self.assertIn("主动消息额度不足", str(ctx.exception))
        self.assertEqual(ctx.exception.code, 11244)
        self.assertEqual(ctx.exception.status, 500)

    def test_401_is_retried_once_with_a_fresh_token(self):
        seen = {"sent": 0}

        def handler(request):
            self.calls.append(request)
            if "getAppAccessToken" in str(request.url):
                return httpx.Response(200, json={"access_token": f"tok-{len(self.calls)}",
                                                 "expires_in": 7200})
            seen["sent"] += 1
            if seen["sent"] == 1:
                return httpx.Response(401, json={"code": 11244, "message": "token expired"})
            return httpx.Response(200, json={"id": "ok"})

        self.qq.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
        asyncio.run(self.qq.send(message(), "重试成功"))
        self.assertEqual(seen["sent"], 2, "401 后应当换令牌重试一次")
        self.assertEqual(self.qq.last_error, "")

    # ── 主动消息 ────────────────────────────────────────────────────────────
    def test_active_message_omits_reply_fields(self):
        asyncio.run(self.qq.send_active("group", "G1", "大家好呀"))
        body = self.body()
        self.assertNotIn("msg_id", body)
        self.assertNotIn("msg_seq", body)
        self.assertEqual(body["msg_type"], 0)
        self.assertTrue(str(self.calls[-1].url).endswith("/v2/groups/G1/messages"))

    def test_active_message_with_image(self):
        asyncio.run(self.qq.send_active("private", "U1", "给你",
                                        {"media": [{"type": "image",
                                                    "data": {"url": PNG_DATA_URL}}]}))
        body = self.body()
        self.assertEqual(body["msg_type"], 7)
        self.assertNotIn("msg_id", body)
        self.assertEqual(body["media"], {"file_info": "FILEINFO"})


class OfficialEventTests(unittest.TestCase):
    def test_group_at_message(self):
        msg = official_event("GROUP_AT_MESSAGE_CREATE", {
            "id": "M1", "content": " 鲸鱼娘在吗 ", "group_openid": "G1",
            "author": {"member_openid": "U1", "username": "小鱼"}})
        self.assertEqual(msg["kind"], "group")
        self.assertEqual(msg["target"], "G1")
        self.assertEqual(msg["sender_id"], "U1")
        self.assertIn("鲸鱼娘", msg["text"])
        self.assertTrue(msg["at"])

    def test_c2c_message(self):
        msg = official_event("C2C_MESSAGE_CREATE", {
            "id": "M2", "content": "在吗", "author": {"user_openid": "U2"}})
        self.assertEqual(msg["kind"], "private")
        self.assertEqual(msg["target"], "U2")

    def test_image_attachments_become_media(self):
        msg = official_event("GROUP_AT_MESSAGE_CREATE", {
            "id": "M3", "content": "", "group_openid": "G1",
            "author": {"member_openid": "U1"},
            "attachments": [{"content_type": "image/png", "url": "//gchat.qpic.cn/a.png",
                             "filename": "a.png", "width": 10, "height": 20, "size": 100}]})
        self.assertEqual(len(msg["media"]), 1)
        self.assertTrue(msg["media"][0]["url"].startswith("https://"))
        self.assertIn("[图片]", msg["text"], "纯图片消息也要给模型一个占位")

    def test_unknown_event_and_missing_fields_are_ignored(self):
        self.assertIsNone(official_event("MESSAGE_CREATE", {"id": "x"}))
        self.assertIsNone(official_event("GROUP_AT_MESSAGE_CREATE", {"content": "hi"}))
        self.assertIsNone(official_event("GROUP_AT_MESSAGE_CREATE", None))

    def test_intents_cover_the_events_we_handle(self):
        """接收的事件类型必须都在订阅的 intent 里，否则"插件没反应"会被当成 bug。"""
        self.assertIn("GROUP_AT_MESSAGE_CREATE", OFFICIAL_EVENTS)
        self.assertIn("C2C_MESSAGE_CREATE", OFFICIAL_EVENTS)
        intents = OfficialQQ.INTENTS
        self.assertTrue(intents & (1 << 25), "群@消息 intent")
        self.assertTrue(intents & (1 << 30), "单聊消息 intent")


class OfficialSplitTests(unittest.TestCase):
    def test_short_text_is_one_piece(self):
        self.assertEqual(_split_official_text(["你好呀。"]), ["你好呀。"])

    def test_long_text_is_split_on_sentence_and_nothing_is_lost(self):
        long_text = "第一句。" * 400
        pieces = _split_official_text([long_text])
        self.assertGreater(len(pieces), 1)
        self.assertLessEqual(max(len(piece) for piece in pieces), OFFICIAL_TEXT_LIMIT)
        self.assertEqual("".join(pieces), long_text, "切分不能丢内容")

    def test_more_than_five_pieces_are_merged_into_five(self):
        pieces = _split_official_text(["一段。" * 300])
        self.assertLessEqual(len(pieces), 5, "QQ 一条来信最多 5 条被动回复")

    def test_empty_input_gives_empty_list(self):
        self.assertEqual(_split_official_text([]), [])
        self.assertEqual(_split_official_text(["", "  "]), [])


class OfficialPacingTests(unittest.TestCase):
    """官方模式下 send_pacing 的 \\x01PACE\\x01 标记要变成递增 msg_seq 的多条被动回复。"""

    def setUp(self):
        self.sent = []

        class FakeOfficial(OfficialQQ):
            pass

        self.qq = FakeOfficial("appid", "secret")

        async def fake_send(message, text, meta=None):
            self.sent.append((int(message.get("_passive_seq") or 1), text))
            return {"id": str(len(self.sent))}

        self.qq.send = fake_send

    def test_pacing_markers_produce_increasing_sequence(self):
        reply = "第一句。\x01PACE:200\x01第二句。\x01PACE:200\x01第三句。"
        asyncio.run(_send_with_pacing(self.qq, message(), reply))
        self.assertEqual([seq for seq, _ in self.sent], [1, 2, 3])
        self.assertEqual([text for _, text in self.sent],
                         ["第一句。", "第二句。", "第三句。"])

    def test_sequence_never_exceeds_five(self):
        reply = "。".join([f"第{index}句" for index in range(9)]) + "。" 
        reply = "\x01PACE:10\x01".join(f"第{index}句。" for index in range(9))
        asyncio.run(_send_with_pacing(self.qq, message(), reply))
        self.assertLessEqual(len(self.sent), 5)
        self.assertTrue(all(1 <= seq <= 5 for seq, _ in self.sent))

    def test_start_sequence_is_respected(self):
        asyncio.run(_send_with_pacing(self.qq, message(seq=3), "甲。\x01PACE:10\x01乙。"))
        self.assertEqual([seq for seq, _ in self.sent], [3, 4])

    def test_plain_reply_is_a_single_message(self):
        asyncio.run(_send_with_pacing(self.qq, message(), "就一句。"))
        self.assertEqual(self.sent, [(1, "就一句。")])


class OfficialOutboxTests(unittest.TestCase):
    """官方模式的主动消息：以前 _drain_outbox 直接 return，插件永远发不出去。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
        (root / "styles" / "default.yaml").write_text(
            "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
        (root / "config.qq.yaml").write_text(
            "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n"
            "qq_only:\n  mode: official\n  listen_private: true\n", encoding="utf-8")
        self.bot = QQRuntime(root=root, config_path=root / "config.qq.yaml",
                             db_path=root / "t.sqlite3")

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def queue(self, text="主动打个招呼"):
        from src.chat_state import ChatState
        self.bot.db.execute(
            "INSERT INTO chat_outbox(key,mode,account,kind,target,text,state,created)"
            " VALUES(?,?,?,?,?,?,'pending',?)",
            ("k1", "official", self.bot.account(), "group", "G1", text, time.time()))
        self.bot.db.commit()

    def test_active_message_is_sent_on_official(self):
        sent = []

        async def fake_active(kind, target, text, meta=None):
            sent.append((kind, target, text))
            return {"id": "active"}

        async def run():
            self.bot.official.authenticated = True
            self.bot.official.send_active = fake_active
            with mock.patch.object(self.bot, "can_send_proactive", return_value=True):
                self.queue()
                await self.bot._drain_outbox()

        asyncio.run(run())
        self.assertEqual(sent, [("group", "G1", "主动打个招呼")])
        state = self.bot.db.execute("SELECT state FROM chat_outbox WHERE key='k1'").fetchone()[0]
        self.assertEqual(state, "sent")

    def test_quota_error_is_recorded_in_words(self):
        async def fake_active(kind, target, text, meta=None):
            raise QQSendError("QQ 拒绝了这次发送（11244）：主动消息额度不足", status=500, code=11244)

        async def run():
            self.bot.official.authenticated = True
            self.bot.official.send_active = fake_active
            with mock.patch.object(self.bot, "can_send_proactive", return_value=True):
                self.queue()
                await self.bot._drain_outbox()

        asyncio.run(run())
        row = self.bot.db.execute("SELECT state,error FROM chat_outbox WHERE key='k1'").fetchone()
        self.assertEqual(row[0], "uncertain")
        self.assertIn("额度不足", row[1])
        self.assertTrue(any("主动消息未发出" in item["text"] for item in self.bot.events))

    def test_paused_bot_does_not_send(self):
        sent = []

        async def fake_active(*args, **kwargs):
            sent.append(args)
            return {}

        async def run():
            self.bot.official.authenticated = True
            self.bot.official.send_active = fake_active
            self.bot.reply_paused = True
            self.queue()
            await self.bot._drain_outbox()

        asyncio.run(run())
        self.assertEqual(sent, [])


class OutboundHygieneTests(unittest.TestCase):
    """发给 QQ 的正文不能带"入站渲染占位符"（模型爱照抄，用户看到的是方括号字）。"""

    def test_placeholders_are_removed_and_spacing_tidied(self):
        from src.text_hygiene import tidy_outbound
        cases = {
            "晚安 [表情:睡觉]": "晚安",
            "看这个 [图片] 好看吗": "看这个 好看吗",
            "[表情:微笑]": "",
            "好呀 [表情14]": "好呀",
            "收到 [动图:猫猫]": "收到",
            "哈哈 [卡片消息] 笑死": "哈哈 笑死",
            "真的吗 [语音]？": "真的吗？",
            "晚安 [表情:睡觉] 。": "晚安。",
        }
        for raw, want in cases.items():
            self.assertEqual(tidy_outbound(raw), want, raw)

    def test_normal_text_is_untouched(self):
        from src.text_hygiene import tidy_outbound
        for text in ("普通的一句回复。", "带[方括号]但不是占位", "1+1=2", "（笑）好呀～"):
            self.assertEqual(tidy_outbound(text), text)

    def test_tidying_runs_before_sending(self):
        sent = []

        class FakeOfficial(OfficialQQ):
            pass

        qq = FakeOfficial("appid", "secret")

        async def fake_send(message, text, meta=None):
            sent.append(text)
            return {}

        qq.send = fake_send
        asyncio.run(_send_with_pacing(qq, message(), "晚安 [表情:睡觉]"))
        self.assertEqual(sent, ["晚安"])

    def test_meta_text_is_tidied_too(self):
        sent = []

        class FakeOfficial(OfficialQQ):
            pass

        qq = FakeOfficial("appid", "secret")

        async def fake_send(message, text, meta=None):
            sent.append(text)
            return {}

        qq.send = fake_send
        asyncio.run(_send_with_pacing(qq, message(), "ignored",
                                      {"text": "看图 [图片]", "media": []}))
        self.assertEqual(sent, ["看图"])


if __name__ == "__main__":
    unittest.main()
