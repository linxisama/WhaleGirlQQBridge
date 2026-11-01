"""人设图片（非必填）：上传、自我描述、清除，以及"导出带走图片 / 导入还原图片"。

用户要求："人设可以再加一个人设图片的功能（但是非必填），让 ai 理解自己是什么样子的"。
做法：图片复制进运行数据目录（不依赖原文件），上传时用视觉模型生成一句**第一人称**的
自我描述，存进人设；回复时把描述拼进系统提示词（不额外发图，零额外 token）。
"""
import asyncio
import base64
import os
import pathlib
import struct
import tempfile
import time
import unittest
import zlib
from unittest import mock

import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.qq_app import QQRuntime, create_app  # noqa: E402
from src.style_manager import (  # noqa: E402
    MAX_PERSONA_IMAGE_BYTES, StyleManager, image_kind,
)

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

BUILTIN = "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n"


def png_bytes(width=2, height=2, color=(120, 180, 255)) -> bytes:
    """手工拼一张最小 PNG，测试里不依赖 Pillow。"""
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(BUILTIN, encoding="utf-8")
    return root


class ImageKindTests(unittest.TestCase):
    def test_detects_real_bitmaps_only(self):
        self.assertEqual(image_kind(png_bytes()), "png")
        self.assertEqual(image_kind(b"\xff\xd8\xff\xe0abc"), "jpeg")
        self.assertEqual(image_kind(b"GIF89a...."), "gif")
        self.assertEqual(image_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")
        self.assertEqual(image_kind(b"<svg xmlns=...>"), "")
        self.assertEqual(image_kind(b"PK\x03\x04"), "")


class StyleManagerImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self.tmp.name)
        self.manager = StyleManager(styles_dir=self.root / "styles",
                                    custom_dir=self.root / "styles" / "custom",
                                    image_dir=self.root / "data" / "persona_images")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_uses_magic_bytes_and_keeps_one_file(self):
        name = self.manager.save_image("whale", png_bytes())
        self.assertEqual(name, "whale.png")
        self.assertTrue((self.root / "data" / "persona_images" / "whale.png").is_file())
        again = self.manager.save_image("whale", b"\xff\xd8\xff\xe0jpegdata")
        self.assertEqual(again, "whale.jpg")
        leftovers = sorted(p.name for p in (self.root / "data" / "persona_images").iterdir())
        self.assertEqual(leftovers, ["whale.jpg"], "同一个人设只留一张图")

    def test_rejects_oversized_and_non_image(self):
        with self.assertRaises(ValueError):
            self.manager.save_image("whale", b"x" * (MAX_PERSONA_IMAGE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.manager.save_image("whale", b"<svg></svg>")

    def test_image_path_refuses_path_tricks(self):
        self.manager.save_image("whale", png_bytes())
        self.assertIsNotNone(self.manager.image_path("whale.png"))
        for bad in ("../whale.png", "sub/whale.png", "whale.png.exe", "", None, "missing.png"):
            self.assertIsNone(self.manager.image_path(bad), bad)

    def test_round_trip_and_missing_file_is_dropped(self):
        filename = self.manager.save_image("whale", png_bytes())
        self.manager.save_persona({"name": "whale", "system_prompt": "傲娇", "image": filename,
                                   "image_note": "银白长发、蓝眼睛"})
        manager = StyleManager(styles_dir=self.root / "styles",
                               custom_dir=self.root / "styles" / "custom",
                               image_dir=self.root / "data" / "persona_images")
        manager.load_all()
        style = manager.get_for_edit("whale")
        self.assertEqual(style.image, "whale.png")
        self.assertEqual(style.image_note, "银白长发、蓝眼睛")
        self.assertIn("你的样子", manager.appearance_text(style))

        (self.root / "data" / "persona_images" / "whale.png").unlink()
        manager.save_persona(manager.get_for_edit("whale").to_dict())
        self.assertEqual(manager.get_for_edit("whale").image, "", "图片没了就不该再引用它")

    def test_appearance_text_is_empty_by_default(self):
        self.manager.save_persona({"name": "whale", "system_prompt": "傲娇"})
        self.assertEqual(self.manager.appearance_text(self.manager.get_for_edit("whale")), "")
        self.assertEqual(self.manager.appearance_text(None), "")

    def test_image_note_participates_in_banned_words(self):
        self.manager.set_banned_words(["坏词"])
        with self.assertRaises(Exception):
            self.manager.save_persona({"name": "whale", "system_prompt": "傲娇",
                                       "image_note": "她的名字是坏词"})


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class PersonaImageRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self.tmp.name)
        (self.root / "config.qq.yaml").write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "test"},
        }), encoding="utf-8")
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "test.sqlite")
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()
        self.client.post("/api/personas", json={"name": "whale", "display_name": "鲸鱼娘",
                                                "system_prompt": "你是傲娇鲸鱼娘。"})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        try:
            self.bot.db.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def upload(self, raw=None, name="whale"):
        return self.client.post("/api/personas/image", json={
            "name": name, "filename": "pic.png",
            "content_base64": base64.b64encode(raw if raw is not None else png_bytes()).decode()})

    def test_upload_generates_self_description_with_vision(self):
        with mock.patch.object(self.bot.ai, "describe_images",
                               new=mock.AsyncMock(return_value="银白色长发，蓝眼睛，白色连帽衫，看起来十六七岁。")) as vision:
            response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["described"])
        self.assertEqual(body["image"], "whale.png")
        self.assertIn("银白色长发", body["image_note"])
        # 提示词要是"描述她自己"，不是通用的"图里有什么"
        self.assertIn("第一人称", vision.call_args.kwargs["prompt"])
        detail = self.client.get("/api/personas/whale").json()
        self.assertEqual(detail["image"], "whale.png")
        self.assertIn("银白色长发", detail["image_note"])

    def test_upload_without_vision_still_saves_image(self):
        with mock.patch.object(self.bot.ai, "describe_images",
                               new=mock.AsyncMock(side_effect=RuntimeError("模型不支持图片"))):
            response = self.upload()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["described"])
        self.assertEqual(body["image"], "whale.png")
        self.assertIn("没能识别", body["message"])
        self.assertEqual(self.client.get("/api/personas/whale").json()["image_note"], "")

    def test_save_builtin_picture_without_waiting_for_vision_and_reload_it(self):
        builtin = self.root / 'styles/default.yaml'
        before = builtin.read_bytes()
        with mock.patch.object(self.bot.ai, 'describe_images', new=mock.AsyncMock()) as vision:
            body = self.client.post('/api/personas/image', json={
                'name':'default', 'describe':False,
                'content_base64':base64.b64encode(png_bytes()).decode()}).json()
        self.assertTrue(body['success'])
        vision.assert_not_awaited()
        self.assertEqual(builtin.read_bytes(), before)
        self.client.post('/api/personas/image/note', json={'name':'default','image_note':'蓝色短发'})
        manager = StyleManager(styles_dir=self.root/'styles', custom_dir=self.root/'styles/custom',
                               image_dir=self.bot.style.image_dir)
        manager.load_all()
        self.assertEqual(manager.get_for_edit('default').image, 'default.png')
        self.assertEqual(manager.get_for_edit('default').image_note, '蓝色短发')

    def test_replacing_picture_clears_old_description_even_when_vision_is_disabled(self):
        for color in ((120,180,255), (255,80,60)):
            raw = png_bytes(color=color)
            response = self.client.post('/api/personas/image', json={
                'name':'whale', 'describe':False,'content_base64':base64.b64encode(raw).decode()})
            self.assertTrue(response.json()['success'])
            self.assertEqual(self.client.get('/api/personas/whale/image').content, raw)
            self.assertEqual(self.client.get('/api/personas/whale').json()['image_note'], '')
            self.client.post('/api/personas/image/note', json={'name':'whale','image_note':'旧图描述'})

    def test_failed_persona_write_rolls_back_picture_and_memory(self):
        self.client.post('/api/personas/image', json={'name':'whale','describe':False,
            'content_base64':base64.b64encode(png_bytes()).decode()})
        before=self.client.get('/api/personas/whale/image').content
        with mock.patch.object(self.bot.style, 'save_persona', side_effect=OSError('disk full')):
            response=self.client.post('/api/personas/image',json={'name':'whale','describe':False,
                'content_base64':base64.b64encode(png_bytes(color=(200,0,0))).decode()})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.client.get('/api/personas/whale/image').content,before)

    def test_manual_description_is_not_overwritten_by_a_late_ai_result(self):
        self.client.post('/api/personas/image',json={'name':'whale','describe':False,
            'content_base64':base64.b64encode(png_bytes()).decode()})
        async def late(*args,**kwargs):
            style=self.bot.style.get_for_edit('whale')
            self.bot.style.save_persona(dict(style.to_dict(),image_note='手动保存的新描述'))
            return '过期的自动描述'
        with mock.patch.object(self.bot.ai,'describe_images',new=mock.AsyncMock(side_effect=late)):
            body=self.client.post('/api/personas/image/describe',json={'name':'whale'}).json()
        self.assertFalse(body['success'])
        self.assertEqual(self.client.get('/api/personas/whale').json()['image_note'],'手动保存的新描述')

    def test_vision_text_hitting_banned_words_is_dropped(self):
        self.client.post("/api/personas/banned-words", json={"words": "坏词"})
        with mock.patch.object(self.bot.ai, "describe_images",
                               new=mock.AsyncMock(return_value="她身上写着坏词")):
            body = self.upload().json()
        self.assertEqual(body["image"], "whale.png")
        self.assertEqual(body["image_note"], "", "自动生成的违规描述丢掉，但不惩罚用户")
        self.assertFalse(self.bot.reply_paused)

    def test_image_file_is_served_and_can_be_removed(self):
        self.upload()
        response = self.client.get("/api/personas/whale/image")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG"))
        removed = self.client.post("/api/personas/image/remove", json={"name": "whale"})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get("/api/personas/whale/image").status_code, 404)
        detail = self.client.get("/api/personas/whale").json()
        self.assertEqual((detail["image"], detail["image_note"]), ("", ""))

    def test_upload_rejects_bad_input(self):
        self.assertEqual(self.upload(raw=b"<svg/>").status_code, 400)
        self.assertEqual(self.upload(raw=b"x" * (MAX_PERSONA_IMAGE_BYTES + 10)).status_code, 400)
        self.assertEqual(self.client.post("/api/personas/image",
                                          json={"name": "whale", "content_base64": ""}).status_code, 400)
        self.assertEqual(self.client.post("/api/personas/image",
                                          json={"name": "nobody", "content_base64": "eA=="}).status_code, 400)

    def test_re_describe_updates_the_note(self):
        self.upload()
        with mock.patch.object(self.bot.ai, "describe_images",
                               new=mock.AsyncMock(return_value="蓝色短发，戴鲸鱼发卡。")):
            response = self.client.post("/api/personas/image/describe", json={"name": "whale"})
        self.assertTrue(response.json()["success"])
        self.assertIn("鲸鱼发卡", self.client.get("/api/personas/whale").json()["image_note"])

    def test_export_embeds_image_and_import_restores_it(self):
        self.upload()
        exported = self.client.post("/api/personas/export", json={
            "name": "whale", "display_name": "鲸鱼娘", "system_prompt": "你是傲娇鲸鱼娘。",
            "image": "whale.png", "image_note": "银白长发"}).json()
        self.assertTrue(exported["image_embedded"])
        data = yaml.safe_load(exported["content"])
        self.assertIn("image_data", data)
        self.assertEqual(base64.b64decode(data["image_data"]), png_bytes())

        self.client.post("/api/personas/image/remove", json={"name": "whale"})
        self.assertEqual(self.client.get("/api/personas/whale/image").status_code, 404)

        response = self.client.post("/api/personas/import", json={
            "filename": "persona-whale.yaml", "content": exported["content"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("形象图", response.json()["message"])
        # 原来的 whale 还在（只清了图），所以这次会改名成 whale-2 —— 图片要跟着它一起进来
        imported = response.json()["name"]
        self.assertNotEqual(imported, "whale")
        self.assertEqual(self.client.get(f"/api/personas/{imported}/image").status_code, 200)
        self.assertEqual(self.client.get(f"/api/personas/{imported}").json()["image_note"], "银白长发")

    def test_import_referencing_a_missing_image_only_takes_text(self):
        response = self.client.post("/api/personas/import", json={
            "filename": "x.yaml",
            "content": "name: other\ndisplay_name: 别人\nsystem_prompt: 你好\nimage: other.png\nimage_note: 白头\n"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("不在这台机器上", response.json()["message"])
        self.assertEqual(self.client.get("/api/personas/other").json()["image"], "")

    def test_reply_prompt_carries_the_self_description(self):
        """真回复路径：系统提示词里要带上"你的样子"。"""
        from tests.test_chat_features import message

        self.upload()
        # 回复用的是"当前人设"，所以要先把它设为当前
        self.assertEqual(self.client.post("/api/personas/whale/activate").status_code, 200)
        self.bot.running = True
        self.bot.official.authenticated = True
        self.bot.official.send = mock.AsyncMock()
        self.bot.ai.chat = mock.AsyncMock(return_value="我呀，白头发蓝眼睛。")

        async def run():
            self.bot.worker = asyncio.create_task(self.bot._work())
            self.bot.ingest(message(mid="img1", text="你长什么样？"), "official")
            for _ in range(100):
                if self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='sent'").fetchone()[0]:
                    break
                await asyncio.sleep(0.05)

        with mock.patch.object(self.bot.ai, "describe_images",
                               new=mock.AsyncMock(return_value="银白色长发，蓝眼睛。")):
            self.upload()
        asyncio.run(run())
        prompt = self.bot.ai.chat.call_args.kwargs.get("system_prompt", "")
        self.assertIn("你的样子", prompt)
        self.assertIn("银白色长发", prompt)


class FullPromptTests(unittest.TestCase):
    """"你的样子"必须出现在所有真正发言的地方，不能只在"用户先开口"时才有。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self.tmp.name)
        self.manager = StyleManager(styles_dir=self.root / "styles",
                                    custom_dir=self.root / "styles" / "custom",
                                    image_dir=self.root / "data" / "persona_images")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_prompt_appends_look_only_when_present(self):
        self.manager.save_persona({"name": "whale", "system_prompt": "你是傲娇鲸鱼娘。"})
        plain = self.manager.full_prompt(self.manager.get_for_edit("whale"))
        self.assertEqual(plain, self.manager.get_for_edit("whale").prompt_text())
        self.manager.save_persona({"name": "whale", "system_prompt": "你是傲娇鲸鱼娘。",
                                   "image_note": "银白长发、蓝眼睛"})
        styled = self.manager.full_prompt(self.manager.get_for_edit("whale"))
        self.assertTrue(styled.startswith(plain))
        self.assertIn("你的样子", styled)
        self.assertIn("银白长发", styled)

    def test_fingerprint_ignores_the_look(self):
        """换张形象图不该切断历史：人设指纹只看 prompt_text()。"""
        from src.chat_state import persona_key

        self.manager.save_persona({"name": "whale", "system_prompt": "你是傲娇鲸鱼娘。"})
        before = persona_key(self.manager.get_for_edit("whale").prompt_text())
        self.manager.save_persona({"name": "whale", "system_prompt": "你是傲娇鲸鱼娘。",
                                   "image_note": "银白长发"})
        self.assertEqual(before, persona_key(self.manager.get_for_edit("whale").prompt_text()))

    def test_every_speaking_plugin_uses_full_prompt(self):
        """源码级契约：主动发言的插件都要用 full_prompt，否则它不知道自己长什么样。"""
        import pathlib as _pathlib

        plugins = _pathlib.Path(__file__).resolve().parents[1] / "src" / "plugins"
        for name in ("proactive_chat.py", "icebreaker.py", "silence_ping.py"):
            source = (plugins / name).read_text(encoding="utf-8")
            self.assertIn("full_prompt", source, f"{name} 应该用 full_prompt()")
            self.assertNotIn(".get().prompt_text()", source, f"{name} 不该直接用 prompt_text()")
        affinity = (plugins / "affinity_proactive.py").read_text(encoding="utf-8")
        self.assertIn("appearance_text", affinity, "affinity_proactive 也要带上你的样子")


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
def wait_api(widget, timeout: float = 5.0) -> bool:
    """等后台接口任务跑完（离屏模式下需要手动转事件循环）。

    这些按钮现在都在 QThread 里调接口（同步调用会把窗口卡成 Windows 的
    AppHangTransient，也就是用户报的"卡死"），所以断言前必须先等回调。
    超时会打出每个任务的真实状态，便于定位（这类"只在全量跑时失败"的问题最怕没线索）。
    """
    from PyQt6.QtWidgets import QApplication
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if not getattr(widget, "_api_tasks", []):
            QApplication.processEvents()
            return True
        time.sleep(0.01)
    for task in getattr(widget, "_api_tasks", []):
        print(f"[wait_api 超时] path={task.path} running={task.isRunning()} "
              f"finished={task.isFinished()} payload={task.payload}")
    dispatcher = getattr(widget, "_api_dispatcher", None)
    if dispatcher is not None:
        print(f"[wait_api 超时] 派发器里还挂着 {len(dispatcher._calls)} 个回调")
    return False


class PersonaImageGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_row_exists_and_starts_empty(self):
        tab = qq_gui.PersonaTab()
        for attr in ("image_preview", "btn_image_upload", "btn_image_note",
                     "btn_image_clear", "ed_image_note"):
            self.assertTrue(hasattr(tab, attr), attr)
        tab._new()
        self.assertEqual(tab.image_preview.text(), "未设置")
        self.assertEqual(tab.ed_image_note.text(), "")

    def test_payload_carries_image_fields(self):
        tab = qq_gui.PersonaTab()
        tab.ed_name.setText("whale")
        tab._pending_image = "whale.png"
        tab.ed_image_note.setText("银白长发")
        payload = tab._payload(rename=False)
        self.assertEqual(payload["image"], "whale.png")
        self.assertEqual(payload["image_note"], "银白长发")

    def test_persona_save_button_stays_visible_above_the_scroll_area(self):
        tab=qq_gui.PersonaTab()
        tab.resize(800,500)
        tab.show()
        self.app.processEvents()
        tab.scroll.verticalScrollBar().setValue(tab.scroll.verticalScrollBar().maximum())
        self.assertFalse(tab.btn_save.visibleRegion().isEmpty())
        self.assertTrue(hasattr(tab,'btn_image_save'))
        self.assertLess(tab.btn_save.mapTo(tab,qq_gui.QPoint()).y(),tab.scroll.y())
        tab.close()

    def test_preview_failure_never_clears_the_saved_image_reference(self):
        tab=qq_gui.PersonaTab();tab._current='whale';tab._pending_image='whale.png'
        with mock.patch.object(qq_gui,'local_request',side_effect=OSError('offline')):
            tab._refresh_image_preview()
        self.assertEqual(tab._pending_image,'whale.png')

    def test_upload_posts_base64_and_shows_note(self):
        source = pathlib.Path(tempfile.mkdtemp()) / "pic.png"
        source.write_bytes(png_bytes())
        tab = qq_gui.PersonaTab()
        tab._current = "whale"
        with mock.patch.object(qq_gui, "api_post",
                               return_value={"success": True, "image": "whale.png",
                                             "image_note": "银白长发", "message": "已按形象图生成自我描述"}) as post, \
             mock.patch.object(qq_gui.file_dialogs, "open_file", return_value=(str(source), "")), \
             mock.patch.object(tab, "_refresh_image_preview") as refresh:
            tab._upload_persona_image()
            self.assertTrue(wait_api(tab), "后台任务没在 5 秒内跑完")   # 走后台线程，避免主线程卡死
        path, payload = post.call_args.args
        self.assertEqual(path, "personas/image")
        self.assertEqual(payload["name"], "whale")
        self.assertFalse(payload['describe'])
        self.assertEqual(base64.b64decode(payload["content_base64"]), png_bytes())
        self.assertEqual(tab.ed_image_note.text(), "银白长发")
        refresh.assert_called_once()
        self.assertIn("已按形象图", tab.notice.text())

    def test_upload_without_saved_persona_is_refused(self):
        tab = qq_gui.PersonaTab()
        with mock.patch.object(qq_gui, "api_post") as post:
            tab._upload_persona_image()
        post.assert_not_called()
        self.assertIn("先保存人设", tab.notice.text())

    def test_oversized_file_is_refused_before_upload(self):
        source = pathlib.Path(tempfile.mkdtemp()) / "big.png"
        source.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        tab = qq_gui.PersonaTab()
        tab._current = "whale"
        with mock.patch.object(qq_gui, "api_post") as post, \
             mock.patch.object(qq_gui, "tell") as told, \
             mock.patch.object(qq_gui.file_dialogs, "open_file", return_value=(str(source), "")):
            tab._upload_persona_image()
        post.assert_not_called()
        told.assert_called_once()          # 真要弹窗时必须 patch 掉：离屏模式下 exec() 会一直等
        self.assertIn("太大", tab.notice.text())

    def test_clear_asks_first(self):
        tab = qq_gui.PersonaTab()
        tab._current = "whale"
        tab._pending_image = "whale.png"
        with mock.patch.object(qq_gui, "ask_confirm", return_value=False) as confirm, \
             mock.patch.object(qq_gui, "api_post") as post:
            tab._clear_persona_image()
        confirm.assert_called_once()
        post.assert_not_called()

    def test_detail_load_shows_note_and_image_state(self):
        tab = qq_gui.PersonaTab()
        with mock.patch.object(qq_gui, "api_get",
                               return_value={"name": "whale", "display_name": "鲸鱼娘",
                                             "description": "", "system_prompt": "傲娇",
                                             "image": "whale.png", "image_note": "银白长发"}), \
             mock.patch.object(tab, "_refresh_image_preview"):
            tab._load_detail("whale")
        self.assertEqual(tab.ed_image_note.text(), "银白长发")
        self.assertEqual(tab._pending_image, "whale.png")


if __name__ == "__main__":
    unittest.main()
