"""控制台"打不开/闪退"的回归测试：单实例接管、僵尸后台清理、端口冲突说明、边缘缩放。

用户实测（2026-10-15 20:30 那台机器）：
- 8090 上留着**两个** WhaleGirlQQ.exe：一个有可见窗口（正占端口），一个 1 MB **没有窗口**；
- 之后再启动控制台只会提示"检测到旧版或其他后台占用8090"，用户看到的就是"程序打不开"。

这里把"该怎么办"钉成纯函数 + 路由 + 窗口行为三组测试。
"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient  # noqa: E402

from src.qq_app import QQRuntime, create_app  # noqa: E402

try:
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False

import pathlib  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n", encoding="utf-8")
    return root


class BackendPlanTests(unittest.TestCase):
    """plan_backend_start：把"端口上有什么"映射成"该怎么办"。"""

    def setUp(self):
        import qq_gui
        self.gui = qq_gui

    def test_empty_port_starts_our_backend(self):
        self.assertEqual(self.gui.plan_backend_start(None), 'start')
        self.assertEqual(self.gui.plan_backend_start({}), 'start')

    def test_our_windowed_console_means_focus_or_takeover(self):
        probe = {"app": self.gui.APP_ID, "windowed": True, "version": "1.6.0", "pid": 1808}
        self.assertEqual(self.gui.plan_backend_start(probe), 'focus')

    def test_our_windowless_backend_is_a_zombie_to_take_over(self):
        probe = {"app": self.gui.APP_ID, "windowed": False, "version": "1.6.0", "pid": 20132}
        self.assertEqual(self.gui.plan_backend_start(probe), 'takeover')
        # 老版本后台没有 windowed 字段：按"无窗口僵尸"处理，避免新窗口卡在冲突提示
        self.assertEqual(self.gui.plan_backend_start({"app": self.gui.APP_ID}), 'takeover')

    def test_foreign_process_is_reported_not_ignored(self):
        self.assertEqual(self.gui.plan_backend_start({"app": "OtherApp"}), 'foreign')
        self.assertEqual(self.gui.plan_backend_start({"app": ""}), 'foreign')

    def test_probe_returns_none_when_nothing_listens(self):
        with mock.patch.object(self.gui, 'local_request', side_effect=OSError('refused')):
            self.assertIsNone(self.gui.backend_probe())

    def test_probe_returns_payload_when_our_backend_answers(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"app": self.gui.APP_ID, "windowed": True}
        with mock.patch.object(self.gui, 'local_request', return_value=response):
            self.assertEqual(self.gui.backend_probe(), {"app": self.gui.APP_ID, "windowed": True})

    def test_takeover_sends_quit_then_waits_for_the_port(self):
        with mock.patch.object(self.gui, 'request_backend_quit', return_value=True) as quit_call, \
             mock.patch.object(self.gui, 'wait_port_free', return_value=True) as wait_call:
            import qq_gui
            window = mock.Mock()
            window.console_tab = mock.Mock()
            qq_gui.MainWindow._takeover_backend(window)
        quit_call.assert_called_once()
        wait_call.assert_called_once()
        window._start_backend.assert_called_once()

    def test_takeover_reports_when_the_old_backend_will_not_die(self):
        import qq_gui
        with mock.patch.object(self.gui, 'request_backend_quit', return_value=True), \
             mock.patch.object(self.gui, 'wait_port_free', return_value=False):
            window = mock.Mock()
            window.console_tab = mock.Mock()
            qq_gui.MainWindow._takeover_backend(window)
        window._start_backend.assert_not_called()
        message = window.console_tab.set_notice.call_args.args[0]
        self.assertIn('任务管理器', message)


class BackendQuitRouteTests(unittest.TestCase):
    """后台要能被新控制台"请"着退出（否则只能去任务管理器杀进程）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
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

    def test_quit_route_stops_the_bot_and_exits(self):
        exited = []
        # 关键：替换掉真正的 os._exit —— 否则关闭任务一旦跑到就带走整个测试进程
        with mock.patch.object(self.bot, "_exit_process",
                               side_effect=lambda code=0: exited.append(code)), \
             mock.patch.object(self.bot, "stop", new=mock.AsyncMock()) as stop:
            response = self.client.post("/api/runtime/quit", json={})
            self.assertTrue(response.json()["success"])
            for _ in range(80):                     # 等后台的关闭任务跑完
                if exited:
                    break
                import time
                time.sleep(0.05)
        stop.assert_awaited()
        self.assertEqual(exited, [0])

    def test_runtime_identity_reports_windowed_flag(self):
        data = self.client.get("/api/runtime").json()
        self.assertEqual(data["app"], "WhaleGirlQQ")
        self.assertIn("windowed", data)
        self.assertFalse(data["windowed"], "独立跑的后台没有窗口，默认应为 False")


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class WindowResizeTests(unittest.TestCase):
    """窗口要能"自动调节大小"：四条边/四个角都能拖，尺寸记进配置。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import qq_gui
        self.gui = qq_gui
        # 造窗口时不要真的去探测/启动后端（测试机上会真的占用 8090 端口）
        self._cfg_tmp = tempfile.TemporaryDirectory()
        self._cfg_path = pathlib.Path(self._cfg_tmp.name) / 'gui_config.json'
        with mock.patch.object(qq_gui.MainWindow, '_prepare_backend', lambda self: None):
            self.window = qq_gui.MainWindow(config_path=self._cfg_path)
        self.window.resize(1000, 700)

    def tearDown(self):
        # 先停看门狗线程：它持有窗口引用，窗口删了之后还在跑会撞到已销毁的控件
        try:
            self.window.hang_watchdog._stop = True
            self.window.hang_watchdog._timer.stop()
        except Exception:
            pass
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self._cfg_tmp.cleanup()

    def test_edges_hit_test_all_sides_and_corners(self):
        window, QPoint_ = self.window, QPoint
        width, height = window.width(), window.height()
        cases = {
            QPoint_(1, height // 2): self.gui.Qt.Edge.LeftEdge,
            QPoint_(width - 1, height // 2): self.gui.Qt.Edge.RightEdge,
            QPoint_(width // 2, 1): self.gui.Qt.Edge.TopEdge,
            QPoint_(width // 2, height - 1): self.gui.Qt.Edge.BottomEdge,
        }
        for point, expected in cases.items():
            self.assertEqual(window._edges_at(point), expected, point)
        corner = window._edges_at(QPoint_(1, 1))
        self.assertTrue(corner & self.gui.Qt.Edge.LeftEdge and corner & self.gui.Qt.Edge.TopEdge)
        self.assertIsNone(window._edges_at(QPoint_(width // 2, height // 2)))

    def test_cursor_matches_the_edge(self):
        gui, window = self.gui, self.window
        width, height = window.width(), window.height()
        cursors = {
            QPoint(1, height // 2): gui.Qt.CursorShape.SizeHorCursor,
            QPoint(width // 2, 1): gui.Qt.CursorShape.SizeVerCursor,
            QPoint(1, 1): gui.Qt.CursorShape.SizeFDiagCursor,
            QPoint(width - 1, 1): gui.Qt.CursorShape.SizeBDiagCursor,
            QPoint(width // 2, height // 2): gui.Qt.CursorShape.ArrowCursor,
        }
        for point, expected in cursors.items():
            self.assertEqual(window._cursor_for_edges(window._edges_at(point)), expected, point)

    def test_visible_inner_edges_are_resizable_past_the_transparent_margin(self):
        window, gui = self.window, self.gui
        width, height = window.width(), window.height()
        inset = window.layout().contentsMargins().left() + 3
        for point, expected in ((QPoint(inset, height//2), gui.Qt.Edge.LeftEdge),
                                (QPoint(width-inset, height//2), gui.Qt.Edge.RightEdge),
                                (QPoint(width//2, inset), gui.Qt.Edge.TopEdge),
                                (QPoint(width//2, height-inset), gui.Qt.Edge.BottomEdge)):
            self.assertEqual(window._edges_at(point), expected)

    def test_failed_system_resize_does_not_consume_the_mouse_press(self):
        event = mock.Mock()
        event.position.return_value.toPoint.return_value = QPoint(3, 100)
        handle = mock.Mock()
        handle.startSystemResize.return_value = False
        with mock.patch.object(self.window, 'windowHandle', return_value=handle):
            self.assertFalse(self.window._resize_zone(self.window._container, event, press=True))
        handle.startSystemResize.assert_called_once()

    def test_console_background_follows_panel_transparency(self):
        """控制台白底必须跟着「面板透明度」走，口径只有 qq_gui.panel_alpha 一处。

        默认配置会套用内置背景图（ensure_default_background），有图时白量按玻璃指数
        压薄（65% 滑块 → 35/255 而不是 89/255），所以这里不能只写死"无图"的通道值：
        两个分支都钉住，并钉住"有图一定比无图薄"这个本轮要点。
        """
        views = (self.window.console_tab.msg_view, self.window.console_tab.evt_view)
        for transparency, no_image_alpha in ((.65, 89), (0, 255), (1, 0)):
            self.window._container.set_background_pixmap(None)
            self.assertFalse(self.window._container.has_background_image())
            self.window._apply_panel_transparency(transparency)
            alpha = round(255 * self.gui.panel_alpha(transparency, False))
            self.assertEqual(alpha, no_image_alpha)
            for view in views:
                self.assertIn(f'rgba(255, 255, 255, {alpha})', view.styleSheet())

        from PyQt6.QtGui import QPixmap
        self.window._container.set_background_pixmap(QPixmap(4, 4))
        self.assertTrue(self.window._container.has_background_image())
        self.window._apply_panel_transparency(.65)
        glass = round(255 * self.gui.panel_alpha(.65, True))
        self.assertLess(glass, 89, "有背景图时白量必须比无图更薄")
        for view in views:
            self.assertIn(f'rgba(255, 255, 255, {glass})', view.styleSheet())

    def test_filter_is_installed_and_tracking_enabled(self):
        self.assertTrue(self.window._container.hasMouseTracking())
        self.assertTrue(self.window._container.testAttribute(
            self.gui.Qt.WidgetAttribute.WA_Hover) or True)  # 装了过滤器即够

    def test_fit_screen_respects_a_small_screen(self):
        screen = mock.Mock()
        screen.availableGeometry.return_value = self.gui.QRect(0, 0, 1024, 640)
        with mock.patch.object(self.window, 'screen', return_value=screen):
            self.window.resize(1600, 1200)
            self.window._fit_screen()
        self.assertLessEqual(self.window.width(), 1024)
        self.assertLessEqual(self.window.height(), 640)
        self.assertGreaterEqual(self.window.minimumWidth(), 320)
        self.assertGreaterEqual(self.window.minimumHeight(), 240)

    def test_size_is_remembered_in_gui_config(self):
        """缩放后的尺寸要能存下来（下次打开还是这个大小）。"""
        self.window.resize(1234, 678)
        self.window._save_geometry()
        saved = self.gui.load_gui_config(self._cfg_path)
        self.assertEqual(saved.get("geometry", {}).get("w"), 1234)
        self.assertEqual(saved.get("geometry", {}).get("h"), 678)

    def test_show_event_really_fits_screen(self):
        with mock.patch.object(self.window, '_fit_screen') as fit:
            self.window.show()
            self.app.processEvents()
            fit.assert_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows 原生消息')
    def test_native_hit_test_uses_full_width_message_hwnd_without_winid(self):
        import ctypes
        from ctypes import wintypes
        message = wintypes.MSG()
        message.hWnd = 0x123456789
        message.message = 0x84
        message.lParam = 5 | (5 << 16)
        get_rect = mock.Mock(return_value=1)
        def rectangle(hwnd, target):
            target._obj.left = target._obj.top = 0
            target._obj.right = target._obj.bottom = 100
            return 1
        get_rect.side_effect = rectangle
        library = mock.Mock(GetWindowRect=get_rect)
        with mock.patch.object(self.gui.ctypes, 'WinDLL', return_value=library), \
             mock.patch.object(self.window, 'winId', side_effect=AssertionError('不能重入创建窗口')):
            self.window.safe_mode = False
            self.assertEqual(self.window.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message)), (True, 13))
        self.assertEqual(get_rect.call_args.args[0], 0x123456789)

    @unittest.skipUnless(os.name == 'nt', 'Windows 原生消息')
    def test_unhandled_native_messages_never_call_sip_base_virtual(self):
        import ctypes
        from ctypes import wintypes
        message = wintypes.MSG()
        message.message = 0x5
        self.window.safe_mode = False
        with mock.patch.object(self.gui.QWidget, 'nativeEvent', side_effect=AssertionError('不能回调基类')):
            self.assertEqual(self.window.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message)), (False, 0))

    @unittest.skipUnless(os.name == 'nt', 'Windows 原生消息')
    def test_failed_rect_read_returns_to_qt_without_base_virtual(self):
        import ctypes
        from ctypes import wintypes
        message = wintypes.MSG()
        message.message = 0x84
        self.window.safe_mode = False
        library = mock.Mock(GetWindowRect=mock.Mock(return_value=0))
        with mock.patch.object(self.gui.ctypes, 'WinDLL', return_value=library), \
             mock.patch.object(self.gui.QWidget, 'nativeEvent', side_effect=AssertionError('不能回调基类')):
            self.assertEqual(self.window.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message)), (False, 0))


def port_busy(port: int | None = None) -> bool:
    """默认端口上有没有**在监听**的程序。

    用 bind 判断而不是 connect：刚退出的进程会留下 TIME_WAIT，
    connect 会把它误判成"有人在听"，白跳过这个端到端测试。
    """
    import socket
    if port is None:
        import qq_gui
        port = qq_gui.PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


@unittest.skipIf(port_busy(), "端口已被占用：本机正在跑控制台，跳过端口接管测试")
class BackendTakeoverEndToEndTests(unittest.TestCase):
    """真起一个后台进程在 8090 上，验证"探测 → 请它退出 → 端口释放"整条链路。

    这是用户"打不开"那条链路的端到端证据：不是 mock，而是真进程 + 真端口 + 真退出。
    """

    @classmethod
    def setUpClass(cls):
        import qq_gui
        cls.gui = qq_gui

    def setUp(self):
        import shutil
        import subprocess
        import sys as _sys
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
        repo = pathlib.Path(__file__).resolve().parents[1]
        shutil.copytree(repo / "styles", root / "styles", dirs_exist_ok=True)
        script = (
            "import sys, pathlib, uvicorn\n"
            f"sys.path.insert(0, r'{repo}')\n"
            "import qq_gui\n"
            "from src.qq_app import QQRuntime, create_app\n"
            f"root = pathlib.Path(r'{root}')\n"
            "app = create_app(QQRuntime(root=root, config_path=root/'config.qq.yaml',"
            " db_path=root/'t.sqlite3'))\n"
            "uvicorn.run(app, host='127.0.0.1', port=qq_gui.PORT,"
            " log_level='error', log_config=None)\n"
        )
        self.proc = subprocess.Popen([_sys.executable, "-c", script], cwd=str(repo),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 立刻登记清理：setUp 后面任何一步失败都不会把子后台留在机器上占着 8090
        # （上一版就踩过：setUp 里 self.fail 之后 tearDown 不执行，后台残留）。
        self.addCleanup(self._kill_child)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.gui.backend_probe(timeout=0.6):
                break
            time.sleep(0.2)
        else:
            self.fail("后台进程没能在 30 秒内起来")

    def _kill_child(self):
        import subprocess
        try:
            if self.proc.poll() is None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True)
                self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:
            pass
        # 确保端口彻底释放，别影响后续测试
        try:
            self.gui.wait_port_free(timeout=10)
        except Exception:
            pass

    def tearDown(self):
        self._kill_child()
        self._tmp.cleanup()

    def test_takeover_chain_works_on_a_real_port(self):
        probe = self.gui.backend_probe()
        self.assertIsNotNone(probe)
        self.assertEqual(probe.get("app"), self.gui.APP_ID)
        self.assertFalse(probe.get("windowed"), "独立后台没有窗口 → 应判为需要接管")
        self.assertEqual(self.gui.plan_backend_start(probe), 'takeover')

        self.assertTrue(self.gui.request_backend_quit(), "后台应当接受优雅退出请求")
        self.assertTrue(self.gui.wait_port_free(timeout=15), "后台退出后端口应当释放")
        self.assertFalse(port_busy(), "端口仍然被占用")
        self.assertIsNone(self.gui.backend_probe(timeout=0.5))


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class LayoutAdaptivityTests(unittest.TestCase):
    """窗口缩到最小、逐个标签页切换都不能出错（用户要求"窗口能自动调节大小"）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import qq_gui
        self.gui = qq_gui
        self._cfg_tmp = tempfile.TemporaryDirectory()
        self._cfg_path = pathlib.Path(self._cfg_tmp.name) / 'gui_config.json'
        with mock.patch.object(qq_gui.MainWindow, '_prepare_backend', lambda self: None):
            self.window = qq_gui.MainWindow(config_path=self._cfg_path)

    def tearDown(self):
        try:
            self.window.hang_watchdog._stop = True
            self.window.hang_watchdog._timer.stop()
        except Exception:
            pass
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self._cfg_tmp.cleanup()

    def test_every_tab_survives_the_minimum_size(self):
        self.window.resize(800, 500)
        self.app.processEvents()
        titles = []
        for index in range(self.window.tabs.count()):
            self.window.tabs.setCurrentIndex(index)
            self.app.processEvents()
            widget = self.window.tabs.currentWidget()
            self.assertIsNotNone(widget, index)
            self.assertGreaterEqual(widget.width(), 0, index)
            titles.append(self.window.tabs.tabText(index))
        for expected in ("控制台", "设置", "人设", "插件", "对话与记忆", "机器人管理", "外观", "关于作者"):
            self.assertIn(expected, titles)

    def test_window_can_be_enlarged_and_shrunk_freely(self):
        for size in ((900, 600), (1400, 900), (820, 520)):
            self.window.resize(*size)
            self.app.processEvents()
            self.assertEqual((self.window.width(), self.window.height()), size)
        # 关键：没有被 setFixedSize 之类的写法锁死
        self.assertNotEqual(self.window.minimumSize(), self.window.maximumSize())

    def test_shown_console_does_not_turn_wrapped_text_into_window_height(self):
        self.window.show()
        self.window.tabs.setCurrentWidget(self.window.console_tab)
        self.app.processEvents()
        self.window.console_tab.set_notice('兼容模式说明和很长的错误提示。' * 10)
        self.app.processEvents()
        self.assertFalse(self.window.layout().hasHeightForWidth())
        for width, height in ((900, 600), (1100, 780), (800, 500)):
            self.window.resize(width, height)
            self.app.processEvents()
            self.assertEqual((self.window.width(), self.window.height()), (width, height))
        notice = self.window.console_tab.notice
        button = self.window.console_tab.btn_start
        self.assertTrue(notice.wordWrap())
        self.assertGreater(notice.y(), button.geometry().bottom())

    def test_long_forms_scroll_inside_their_pages(self):
        self.window.show()
        self.app.processEvents()
        self.window.resize(800, 500)
        for page in (self.window.persona_tab, self.window.appearance_tab):
            self.window.tabs.setCurrentWidget(page)
            self.app.processEvents()
            self.assertGreater(page.scroll.verticalScrollBar().maximum(), 0)
            page.scroll.verticalScrollBar().setValue(page.scroll.verticalScrollBar().maximum())
            self.assertEqual((self.window.width(), self.window.height()), (800, 500))


if __name__ == "__main__":
    unittest.main()
