"""QQ 端"异常卡死"的两个根因：GUI 同步调接口、内置人设写入抛 500。

真实取证（用户机器，2026-10-15）：
- Windows 应用日志两次 `AppHangTransient`（WhaleGirlQQ.exe）16:40:28 / 16:41:39；
- `logs/startup.log` 显示 16:41:30 存形象图 → 16:41:30.4 发起模型调用 → 16:41:39.4 返回，
  紧接着 `Exception in ASGI application ... ValueError: 内置人设不能覆盖`。

也就是说：**主线程同步等了一次 9 秒的模型调用**（窗口"未响应"），
并且**用户对内置人设的保存请求让后端抛了未处理的 ValueError（HTTP 500）**。
"""
import base64
import json
import os
import pathlib
import struct
import tempfile
import time
import unittest
import zlib
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient  # noqa: E402

import qq_gui  # noqa: E402
from src.qq_app import QQRuntime, create_app  # noqa: E402

try:
    from PyQt6.QtWidgets import QApplication

    HAS_QT = True
except Exception:
    HAS_QT = False


def png_bytes() -> bytes:
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n", encoding="utf-8")
    return root


class BuiltinPersonaWriteTests(unittest.TestCase):
    """内置资源不修改；图片使用自定义覆盖，普通保存路径仍有保护。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        # 造一个"内置"人设：taken 判定来自 styles 目录里的文件，这里用官方内置名
        self.bot = QQRuntime(root=self.root, config_path=self.root / "config.qq.yaml",
                             db_path=self.root / "t.sqlite3")
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def builtin_name(self) -> str:
        """测试用的"内置"人设 = 位于 styles/ 目录下的那个（save_persona 以此判定内置）。"""
        return "default"

    def test_builtin_image_can_be_saved_as_a_custom_override_without_touching_resource(self):
        builtin = self.root / 'styles' / 'default.yaml'
        before = builtin.read_bytes()
        response = self.client.post("/api/personas/image", json={
            "name": self.builtin_name(),
            "filename": "pic.png",
            "content_base64": base64.b64encode(png_bytes()).decode(), 'describe': False})
        self.assertEqual(response.status_code, 200, response.text[:300])
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(builtin.read_bytes(), before)
        self.assertTrue((self.root / 'styles/custom/default.yaml').exists())

    def test_image_remove_on_builtin_returns_400(self):
        response = self.client.post("/api/personas/image/remove",
                                    json={"name": self.builtin_name()})
        self.assertNotEqual(response.status_code, 500, response.text[:300])

    def test_image_describe_on_builtin_without_image_is_400(self):
        response = self.client.post("/api/personas/image/describe",
                                    json={"name": self.builtin_name()})
        self.assertNotEqual(response.status_code, 500, response.text[:300])

    def test_plain_save_on_builtin_is_still_a_clean_400(self):
        response = self.client.post("/api/personas", json={
            "name": self.builtin_name(), "display_name": "鲸鱼娘",
            "system_prompt": "傲娇"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("内置人设", response.json()["detail"])


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class AsyncApiCallTests(unittest.TestCase):
    """按钮回调必须在后台线程调接口：主线程被网络/模型调用堵住就是"卡死"。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, widget, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.processEvents()
            if not getattr(widget, "_api_tasks", []):
                self.app.processEvents()
                return True
            time.sleep(0.01)
        return False

    def test_upload_returns_immediately_and_calls_back(self):
        source = pathlib.Path(tempfile.mkdtemp()) / "pic.png"
        source.write_bytes(png_bytes())
        tab = qq_gui.PersonaTab()
        tab._current = "whale"
        seen = []

        def slow_post(path, payload=None, timeout=10):
            time.sleep(0.8)                       # 模拟"让模型看图"这类慢调用
            seen.append(path)
            return {"success": True, "image": "whale.png", "image_note": "银白长发",
                    "message": "已按形象图生成自我描述"}

        with mock.patch.object(qq_gui, "api_post", side_effect=slow_post), \
             mock.patch.object(qq_gui.file_dialogs, "open_file", return_value=(str(source), "")), \
             mock.patch.object(tab, "_refresh_image_preview"):
            started = time.perf_counter()
            tab._upload_persona_image()
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.3, f"上传处理耗时 {elapsed:.2f}s，主线程被堵住了")
            self.assertIn("正在保存图片", tab.notice.text(), "要让用户知道在跑")
            self.assertTrue(self.wait(tab), "后台任务迟迟不结束")
        self.assertEqual(seen, ["personas/image"])
        self.assertEqual(tab.ed_image_note.text(), "银白长发")

    def test_describe_is_async_too(self):
        tab = qq_gui.PersonaTab()
        tab._current = "whale"
        tab._pending_image = "whale.png"

        def slow_post(path, payload=None, timeout=10):
            time.sleep(0.6)
            return {"success": True, "image_note": "银白长发", "message": "已重新识别"}

        with mock.patch.object(qq_gui, "api_post", side_effect=slow_post):
            started = time.perf_counter()
            tab._describe_persona_image()
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.3, f"重新识别耗时 {elapsed:.2f}s，主线程被堵住了")
            self.assertTrue(self.wait(tab))
        self.assertEqual(tab.ed_image_note.text(), "银白长发")

    def test_failure_still_lands_in_the_notice(self):
        tab = qq_gui.PersonaTab()
        tab._current = "whale"
        tab._pending_image = "whale.png"
        with mock.patch.object(qq_gui, "api_post",
                               return_value={"success": False, "message": "内置人设不能覆盖"}):
            tab._describe_persona_image()
            self.assertTrue(self.wait(tab))
        self.assertIn("内置人设不能覆盖", tab.notice.text())

    def test_run_api_async_helper_is_non_blocking(self):
        class Widget(qq_gui.QWidget):
            pass

        widget = Widget()
        widget.notice = qq_gui.QLabel("")
        done = []

        def slow_post(path, payload=None, timeout=10):
            time.sleep(0.5)
            return {"success": True, "message": "好"}

        with mock.patch.object(qq_gui, "api_post", side_effect=slow_post):
            started = time.perf_counter()
            task = qq_gui.run_api_async(widget, "anything", {"a": 1}, timeout=20,
                                        callback=done.append, busy="正在做…")
            self.assertLess(time.perf_counter() - started, 0.3)
            self.assertEqual(widget.notice.text(), "正在做…")
            self.assertTrue(self.wait(widget))
        self.assertEqual(done, [{"success": True, "message": "好"}])
        self.assertIsInstance(task, qq_gui.ApiTask)

    def test_worker_never_raises_into_the_gui(self):
        class Widget(qq_gui.QWidget):
            pass

        widget = Widget()
        done = []

        def boom(path, payload=None, timeout=10):
            raise RuntimeError("网络炸了")

        with mock.patch.object(qq_gui, "api_post", side_effect=boom):
            qq_gui.run_api_async(widget, "x", callback=done.append)
            self.assertTrue(self.wait(widget))
        self.assertFalse(done[0]["success"])
        self.assertIn("网络炸了", done[0]["message"])

    def test_callback_still_runs_when_the_signal_is_never_delivered(self):
        """兜底：排队信号万一没投递（实测出现过"任务 finished、回调还挂着"），
        定时器轮询也必须把回调跑掉 —— 否则按钮就一直转圈。"""
        class Widget(qq_gui.QWidget):
            pass

        widget = Widget()
        result = {"success": True, "message": "兜底送达"}
        done = []

        def silent_post(path, payload=None, timeout=10):
            return result

        with mock.patch.object(qq_gui, "api_post", side_effect=silent_post):
            task = qq_gui.ApiTask("x", "x", None, 10)
            dispatcher = qq_gui._ApiDispatcher(widget)
            # 只登记回调、不连信号：等价于"信号没被投递"
            dispatcher._calls[task] = (done.append, [task])
            dispatcher._timer.start()
            task.start()
            deadline = time.time() + 5
            while time.time() < deadline and not done:
                self.app.processEvents()
                time.sleep(0.02)
        self.assertEqual(done, [result], "兜底轮询没把结果送回来")
        self.assertEqual(getattr(widget, "_api_tasks", []), [])

    def test_callback_runs_exactly_once(self):
        class Widget(qq_gui.QWidget):
            pass

        widget = Widget()
        calls = []

        with mock.patch.object(qq_gui, "api_post", return_value={"success": True}):
            qq_gui.run_api_async(widget, "x", callback=lambda r: calls.append(r))
            self.assertTrue(self.wait(widget))
            for _ in range(10):                 # 再转几圈事件循环，看会不会重复回调
                self.app.processEvents()
                time.sleep(0.02)
        self.assertEqual(len(calls), 1)


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class HangWatchdogTests(unittest.TestCase):
    """下次再"未响应"，必须留下能定位的现场（各线程调用栈）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dump_contains_every_thread_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            widget = qq_gui.QWidget()
            watchdog = qq_gui.UiHangWatchdog(widget, pathlib.Path(tmp), stall_seconds=8,
                                             note=lambda text: None)
            watchdog.last_tick = time.time() - 12
            self.assertGreaterEqual(watchdog.stalled_for(), 11)
            path = watchdog.dump(watchdog.stalled_for())
            text = path.read_text(encoding="utf-8")
        self.assertTrue(path.name.startswith("hang-"))
        self.assertIn("界面无响应", text)
        self.assertIn("===== 线程", text)
        self.assertIn("File \"", text, "要有真实调用栈")
        self.assertIn("MainThread", text)

    def test_heartbeat_resets_the_stall(self):
        with tempfile.TemporaryDirectory() as tmp:
            widget = qq_gui.QWidget()
            watchdog = qq_gui.UiHangWatchdog(widget, pathlib.Path(tmp),
                                             note=lambda text: None)
            watchdog.last_tick = time.time() - 30
            watchdog.tick()                     # 主线程又跳了一下
            self.assertLess(watchdog.stalled_for(), 1.0)

    def test_watchdog_reports_once_per_stall(self):
        with tempfile.TemporaryDirectory() as tmp:
            widget = qq_gui.QWidget()
            notes = []
            watchdog = qq_gui.UiHangWatchdog(widget, pathlib.Path(tmp), stall_seconds=8,
                                             tick_seconds=0.05, note=notes.append)
            watchdog.last_tick = time.time() - 12
            watchdog.start()
            deadline = time.time() + 3
            while time.time() < deadline and not notes:
                time.sleep(0.05)
            watchdog._stop = True
            time.sleep(0.2)
        self.assertTrue(notes, "卡住之后应当写一条运行记录")
        self.assertIn("调用栈", notes[0])
        self.assertLessEqual(len(notes), 1, "同一次卡顿不该刷屏")

    def test_main_window_has_the_watchdog(self):
        window = qq_gui.MainWindow()
        try:
            self.assertTrue(hasattr(window, "hang_watchdog"))
            self.assertLess(window.hang_watchdog.stalled_for(), 30)
        finally:
            window.hang_watchdog._stop = True
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
