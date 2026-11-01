"""W6b：设置页「Agent 工具环」三键 + 主窗口入口收敛的离线回归（需要 PyQt6）。

两件事值得锁死：
1. ai.agent_tools / ai.agent_tools_max_rounds / ai.agent_tools_timeout 必须能从界面改，
   而且真的落进 config.yaml（读回文件验证，不信控件自己的缓存）；脏值不许抛崩。
2. 与「Agent 平台新方向」无关的侧栏入口收起来就行，页面对象和可达性都不能丢。

所有落盘目标都在临时目录：ini 里 agent_settings.set_config_path 把默认路径整体挪走，
绝不碰仓库里的 config.qq.yaml。
"""
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

try:
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:                   # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = None


def _app():
    # 必须持有模块级引用：QApplication 被回收后，再构造 QWidget 会直接终止进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _flush_deletes() -> None:
    """把排队的 deleteLater 立刻执行掉（没有事件循环时也要生效）。"""
    app = _app()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _write_cfg(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class _GuiCase(unittest.TestCase):
    """窗口构造的公共脚手架（每个用例一个新窗口，配置全在临时目录）。"""

    @classmethod
    def setUpClass(cls):
        _app()
        import qq_gui
        from src.gui import agent_settings
        cls.gui = qq_gui
        cls.agent_settings = agent_settings
        cls._root_tmp = tempfile.TemporaryDirectory(prefix="w6b-gui-")
        cls.root = Path(cls._root_tmp.name)
        # 整个模块里 AgentToolsGroup 的默认落盘目标挪到临时目录（构造函数也会读它）
        cls.sandbox_cfg = _write_cfg(cls.root / "config.qq.yaml", "ai:\n  api_url: \"\"\n")
        agent_settings.set_config_path(cls.sandbox_cfg)
        cls._real_cfg = Path(qq_gui.ROOT) / "config.qq.yaml"
        cls._real_digest = (hashlib.sha256(cls._real_cfg.read_bytes()).hexdigest()
                            if cls._real_cfg.exists() else "")

    @classmethod
    def tearDownClass(cls):
        cls.agent_settings.set_config_path(None)
        _flush_deletes()
        cls._root_tmp.cleanup()

    def setUp(self):
        backend = patch.object(self.gui.MainWindow, "_start_backend")
        backend.start()
        self.addCleanup(backend.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="w6b-win-")
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _write_cfg(Path(self.tmp.name) / "config.qq.yaml",
                              "ai:\n  api_url: \"\"\n")
        original = self.gui.GUI_CFG
        self.gui.GUI_CFG = Path(self.tmp.name) / "gui_config.json"
        self.addCleanup(lambda: setattr(self.gui, "GUI_CFG", original))

    def make_window(self):
        window = self.gui.MainWindow()
        self.addCleanup(self._dispose, window)
        return window

    def _dispose(self, window):
        """真把窗口拆掉，不是只排个 deleteLater。

        只调 deleteLater 而不转事件循环，窗口在本模块里会一直活着：它的定时器
        （工作台端口探活、状态轮询、看门狗）会在**后面的**用例里继续触发，
        实测把 test_desktop_reliability 的两个可靠性用例拖成假失败。
        """
        try:
            window._quitting = True
            for timer in (getattr(window, "_timer", None),
                          getattr(getattr(window, "pc_tab", None), "timer", None),
                          getattr(getattr(window, "workbench_tab", None), "_status_timer", None),
                          getattr(getattr(window, "hang_watchdog", None), "_timer", None),
                          getattr(window, "_hang_notice_timer", None)):
                if timer is not None:
                    timer.stop()
            window.hide()
            window.deleteLater()
            _flush_deletes()
        except RuntimeError:        # 对应的 C++ 对象已经不在了
            pass

    def new_group(self, path=None):
        group = self.agent_settings.AgentToolsGroup(config_path=path or self.cfg)
        self.addCleanup(group.deleteLater)
        return group


class AgentToolsControlsTests(_GuiCase):
    """三键控件：默认值、落盘、夹取、脏值、缺失文件。"""

    def test_settings_tab_has_the_three_controls_wired(self):
        window = self.make_window()
        tab = window.settings_tab
        self.assertTrue(hasattr(tab, "agent_tools"), "设置页要挂上 Agent 工具环区块")
        group = tab.agent_tools
        self.assertEqual(group.chk_enabled.objectName(), "agentToolsEnabled")
        self.assertEqual(group.sp_rounds.objectName(), "agentToolsMaxRounds")
        self.assertEqual(group.sp_timeout.objectName(), "agentToolsTimeout")
        # 范围就是契约里的 1..5 与 1..60
        self.assertEqual((group.sp_rounds.minimum(), group.sp_rounds.maximum()), (1, 5))
        self.assertEqual((group.sp_timeout.minimum(), group.sp_timeout.maximum()), (1, 60))
        # 文案要讲清「开了之后 agent 会自己调工具」
        self.assertIn("自己调工具", group.hint.text())
        self.assertIn("默认关闭", group.hint.text())

    def test_defaults_are_off_three_and_twenty(self):
        group = self.new_group()
        self.assertFalse(group.chk_enabled.isChecked())
        self.assertEqual(group.sp_rounds.value(), 3)
        self.assertEqual(group.sp_timeout.value(), 20)
        self.assertEqual(group.values(),
                         {"enabled": False, "max_rounds": 3, "timeout": 20})

    def test_edits_are_persisted_into_config_yaml(self):
        window = self.make_window()
        tab = window.settings_tab
        tab.agent_tools.set_config_path(self.cfg)
        tab.agent_tools.chk_enabled.setChecked(True)
        tab.agent_tools.sp_rounds.setValue(4)
        tab.agent_tools.sp_timeout.setValue(45)
        # 走真实保存路径：SettingsTab.collect() 里委托 agent_tools.collect()
        tab.collect()
        saved = yaml.safe_load(self.cfg.read_text(encoding="utf-8"))
        self.assertIs(saved["ai"]["agent_tools"], True)
        self.assertEqual(saved["ai"]["agent_tools_max_rounds"], 4)
        self.assertEqual(saved["ai"]["agent_tools_timeout"], 45)
        # 关掉重开：新对象从磁盘读回来必须是刚才的值（证明真落盘）
        reopened = self.new_group()
        self.assertTrue(reopened.chk_enabled.isChecked())
        self.assertEqual(reopened.sp_rounds.value(), 4)
        self.assertEqual(reopened.sp_timeout.value(), 45)

    def test_out_of_range_values_in_file_are_clamped(self):
        _write_cfg(self.cfg,
                   "ai:\n  agent_tools: \"yes\"\n  agent_tools_max_rounds: 99\n"
                   "  agent_tools_timeout: 0\n")
        group = self.new_group()
        self.assertTrue(group.chk_enabled.isChecked(), "\"yes\" 要当打开")
        self.assertEqual(group.sp_rounds.value(), 5, "99 轮夹到上限 5")
        self.assertEqual(group.sp_timeout.value(), 1, "0 秒夹到下限 1")

    def test_garbage_values_fall_back_to_defaults(self):
        _write_cfg(self.cfg,
                   "ai:\n  agent_tools: \"maybe\"\n  agent_tools_max_rounds: abc\n"
                   "  agent_tools_timeout: [1, 2]\n")
        group = self.new_group()
        self.assertFalse(group.chk_enabled.isChecked())
        self.assertEqual((group.sp_rounds.value(), group.sp_timeout.value()), (3, 20))

    def test_spinboxes_refuse_values_out_of_range(self):
        group = self.new_group()
        group.sp_rounds.setValue(99)
        group.sp_timeout.setValue(-5)
        self.assertEqual(group.sp_rounds.value(), 5)
        self.assertEqual(group.sp_timeout.value(), 1)
        result = group.save()
        self.assertTrue(result["ok"], result)
        saved = yaml.safe_load(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(saved["ai"]["agent_tools_max_rounds"], 5)
        self.assertEqual(saved["ai"]["agent_tools_timeout"], 1)

    def test_saving_never_creates_a_missing_config_file(self):
        missing = Path(self.tmp.name) / "nope.yaml"
        result = self.agent_settings.write_values({"enabled": True}, missing)
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])
        self.assertFalse(missing.exists(), "不能替用户凭空造一份配置")

    def test_real_config_file_is_untouched_by_these_tests(self):
        self.make_window()
        if not self._real_cfg.exists():
            self.skipTest("仓库里没有 config.qq.yaml（临时工作区），跳过指纹比对")
        digest = hashlib.sha256(self._real_cfg.read_bytes()).hexdigest()
        self.assertEqual(digest, self._real_digest, "测试不许写仓库里的真配置")


class NavConvergenceTests(_GuiCase):
    """入口收敛：页面与可达性在；W9b 起不再靠「更多面板」弹出菜单收敛。"""

    def visible_nav_texts(self, window):
        items = window.nav_list
        return [items.item(row).text() for row in range(items.count())]

    def test_w9b_pages_are_direct_sidebar_entries(self):
        window = self.make_window()
        visible = self.visible_nav_texts(window)
        # W9b：不再收进弹出菜单，四个页面直接占侧栏入口
        self.assertEqual(self.gui.HIDDEN_NAV_ENTRIES, (), "不再隐藏任何页面")
        for name in ("机器人管理", "人设", "插件", "外观"):
            self.assertIn(name, visible, name + " 必须直接出现在侧栏")
        # 核心入口一个都不能少
        for name in ("对话", "智能中心", "控制台", "对话与记忆", "设置", "外部后端", "关于作者"):
            self.assertIn(name, visible)

    def test_hidden_pages_still_exist_in_the_tab_widget(self):
        window = self.make_window()
        titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        for name in ("机器人管理", "人设", "插件", "外观"):
            self.assertIn(name, titles, name + " 页面对象要留着")
        # 页面对象也还在（不是被删掉）
        self.assertIsNotNone(window.persona_tab)
        self.assertIsNotNone(window.plugin_tab)
        self.assertIsNotNone(window.appearance_tab)
        self.assertIsNotNone(window.pc_tab)

    def test_w9b_no_more_panel_row_and_every_page_reachable(self):
        """W9b：侧栏不再有「更多面板」行，四个页面各自可直达。"""
        window = self.make_window()
        more_rows = [row for row, target in window._nav_rows if target == "more"]
        self.assertEqual(more_rows, [], "不再有收起来的面板，也就不该有「更多面板」入口")
        self.assertNotIn("更多面板", self.visible_nav_texts(window))
        for name in ("机器人管理", "人设", "插件", "外观"):
            window._assistant_navigate(name)
            self.assertEqual(window.tabs.tabText(window.tabs.currentIndex()), name)

    def test_about_author_page_is_still_last(self):
        window = self.make_window()
        titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        self.assertIn("关于作者", titles)
        self.assertEqual(titles[-1], "关于作者", "关于作者必须仍是最后一页")

    def test_nav_rows_only_point_at_real_pages(self):
        window = self.make_window()
        count = window.tabs.count()
        for row, target in window._nav_rows:
            self.assertLess(row, window.nav_list.count())
            if target is None:
                continue
            self.assertIsInstance(target, int)
            self.assertTrue(0 <= target < count)

    def test_window_still_builds_quickly_offscreen(self):
        started = time.perf_counter()
        window = self.make_window()
        elapsed = time.perf_counter() - started
        self.assertIsNotNone(window.settings_tab)
        self.assertLess(elapsed, 60.0, "offscreen 构造耗时 %.1fs 超过上限" % elapsed)
