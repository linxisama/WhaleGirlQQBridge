"""技能市场 GUI（SkillMarketDialog）—— 表格渲染、层级颜色、错误码、安装后自动刷新。

真机约束（见 wp12c 报告）：PyQt6 只装在 .venv-build，跑法
.venv-build/Scripts/python.exe -m unittest tests.test_skills_gui -v，
环境变量 QT_QPA_PLATFORM=offscreen。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

import qq_gui as gui

SCRATCH = Path(__file__).resolve().parents[1] / ".tmp" / "wp12c-scratch"
TIERS = ["T1", "T2", "T3", "T4", "T6"]


def market_item(index, tier=None):
    return {"id": "skill-%03d" % index,
            "name": "技能 %03d" % index,
            "version": "1.0.%d" % index,
            "description": "第 %d 个技能包的说明" % index,
            "file": "skills/skill-%03d.zip" % index,
            "bytes": 1024 + index,
            "installed": index % 7 == 0,
            "tier": tier or TIERS[index % len(TIERS)],
            "tools": ["tool_a", "tool_b"]}


def market_payload(count=379, broken=0):
    items = [market_item(index) for index in range(count)]
    return {"ok": True, "count": len(items), "broken": broken,
            "market": "D:/skill-market", "items": items}


def installed_payload():
    return {"ok": True, "count": 2, "root": "D:/skill-root", "items": [
        {"id": "skill-000", "name": "技能 000", "version": "1.0.0", "tier": "T1",
         "tools": ["tool_a", "tool_b"], "enabled": True, "known": True, "blocked": False},
        {"id": "skill-003", "name": "技能 003", "version": "1.0.3", "tier": "T4",
         "tools": [], "enabled": False, "known": False, "blocked": "tier"},
    ]}


def summary_payload(**overrides):
    data = {"ok": True, "installed": 5, "marketCount": 379, "eligible": 228,
            "blocked": 151, "marketDir": "D:/skill-market", "error": None}
    data.update(overrides)
    return data


def default_routes():
    return {"skills/market": market_payload(),
            "skills/installed": installed_payload(),
            "skills/summary": summary_payload()}


def load_ui_fonts() -> str:
    """offscreen 平台没有系统字体，所有字都画成方块：挂一个系统字体再截图才是能看的。

    真机上这是无副作用的（Qt 本来就用系统字体），失败也不影响任何断言。
    """
    from PyQt6.QtGui import QFont, QFontDatabase
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if not path.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 9))
            return families[0]
    return ""


def _close_dialog(dialog):
    """关窗 + deleteLater + 排空事件队列：对话框构造时排的 0ms 定时器不许漏进后续用例。"""
    dialog.close()
    dialog.deleteLater()
    QApplication.processEvents()


class FakeApi:
    """替身 run_api_async：立刻回放预设结果，并记下每次调用的 path/payload。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, widget, path, payload=None, *, timeout=30, callback=None, busy=""):
        self.calls.append({"path": path, "payload": dict(payload or {}), "timeout": timeout})
        answer = self.routes.get(path)
        if callable(answer):
            answer = answer(payload or {})
        if callback is not None and answer is not None:
            callback(answer)
        return None

    def paths(self):
        return [call["path"] for call in self.calls]


class SkillHelperTests(unittest.TestCase):
    """纯函数：层级归一化/配色、工具数、错误码 → 中文。"""

    def test_tier_normalization_and_colors(self):
        self.assertEqual(gui.skill_tier("t1"), "T1")
        self.assertEqual(gui.skill_tier(3), "T3")
        self.assertEqual(gui.skill_tier(" T6 "), "T6")
        self.assertEqual(gui.skill_tier(None), "")
        self.assertEqual(gui.skill_tier("weird"), "WEIRD")

        self.assertEqual(gui.skill_tier_color("T1"), "#1E8E3E")
        self.assertEqual(gui.skill_tier_color("t2"), "#B26A00")
        self.assertEqual(gui.skill_tier_color("T4"), gui.MUTED)
        self.assertEqual(gui.skill_tier_color("T6"), "#C0392B")
        self.assertEqual(gui.skill_tier_color(None), gui.MUTED)
        self.assertNotEqual(gui.skill_tier_color("T1"), gui.skill_tier_color("T4"))

        self.assertTrue(gui.skill_is_registerable("T3"))
        self.assertFalse(gui.skill_is_registerable("T4"))
        self.assertFalse(gui.skill_is_registerable(None))

    def test_tool_count_accepts_list_or_number(self):
        self.assertEqual(gui.skill_count(["a", "b"]), 2)
        self.assertEqual(gui.skill_count(7), 7)
        self.assertEqual(gui.skill_count(None), 0)
        self.assertEqual(gui.skill_count("bad"), 0)

    def test_error_codes_map_to_chinese(self):
        cases = {"exists": "已安装，如需覆盖请勾选覆盖安装",
                 "unsafe-path": "包内路径不安全，已拒绝",
                 "no-manifest": "包内缺少 skill.json/plugin.json",
                 "bad-id": "ID 非法",
                 "missing": "未安装"}
        for code, text in cases.items():
            data = {"ok": False, "error": {"code": code, "message": "raw " + code}}
            self.assertEqual(gui.skill_error_text(data), text)
        self.assertEqual(gui.skill_error_text(
            {"ok": False, "error": {"code": "unknown-code", "message": "后端原话"}}), "后端原话")
        self.assertEqual(gui.skill_error_text(
            {"success": False, "message": "当前后台尚未连接，请稍等"}), "当前后台尚未连接，请稍等")
        self.assertEqual(gui.skill_error_text({"ok": False}), "接口没有返回原因")


class SkillMarketDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        load_ui_fonts()

    def setUp(self):
        self.routes = default_routes()
        self.api = FakeApi(self.routes)
        patcher = patch.object(gui, "run_api_async", self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def dialog(self):
        dialog = gui.SkillMarketDialog()
        self.addCleanup(_close_dialog, dialog)
        dialog.refresh_all()
        self.app.processEvents()
        return dialog

    def test_market_table_renders_first_rows_of_379(self):
        dialog = self.dialog()
        self.assertEqual(dialog.market_table.rowCount(), 379)
        self.assertEqual(dialog.market_table.horizontalHeaderItem(0).text(), "已装")
        self.assertEqual(dialog.market_table.horizontalHeaderItem(4).text(), "层级(T1..T6)")
        self.assertEqual([dialog.market_table.item(0, column).text() for column in range(7)],
                         ["已装", "skill-000", "技能 000", "1.0.0", "T1", "2", "第 0 个技能包的说明"])
        self.assertEqual(dialog.market_table.item(1, 0).text(), "—")
        self.assertEqual(dialog.market_table.item(7, 0).text(), "已装")
        self.assertEqual(dialog.market_table.item(3, 4).foreground().color().name().upper(),
                         gui.MUTED.upper())
        self.assertEqual(dialog.market_table.item(4, 4).foreground().color().name().upper(),
                         "#C0392B")
        self.assertIn("显示 379 / 379", dialog.status.text())
        self.assertGreater(dialog.market_table.columnWidth(6), 80)

    def test_installed_table_states_and_tab_label(self):
        dialog = self.dialog()
        self.assertEqual(dialog.installed_table.rowCount(), 2)
        self.assertEqual(dialog.tabs.tabText(1), "已安装（2）")
        self.assertEqual([dialog.installed_table.item(0, column).text() for column in range(7)],
                         ["是", "skill-000", "技能 000", "1.0.0", "T1", "2", "已注册 · 已启用"])
        self.assertEqual(dialog.installed_table.item(1, 6).text(), "门禁阻挡（tier） · 未注册 · 已停用")

    def test_only_registerable_filter_and_search(self):
        dialog = self.dialog()
        dialog.only_registerable.setChecked(True)
        self.app.processEvents()
        self.assertEqual(dialog.market_table.rowCount(), 228)
        tiers = {dialog.market_table.item(row, 4).text()
                 for row in range(dialog.market_table.rowCount())}
        self.assertEqual(tiers, {"T1", "T2", "T3"})
        dialog.only_registerable.setChecked(False)
        dialog.search.setText("skill-377")
        self.app.processEvents()
        self.assertEqual(dialog.market_table.rowCount(), 1)
        self.assertEqual(dialog.market_table.item(0, 1).text(), "skill-377")
        dialog.search.setText("说明")
        self.app.processEvents()
        self.assertEqual(dialog.market_table.rowCount(), 379)

    def test_summary_status_bar_shows_counts_and_market_dir(self):
        dialog = self.dialog()
        text = dialog.summary.text()
        for expected in ("已装 5", "市场 379", "可注册 228", "被挡 151", "D:/skill-market"):
            self.assertIn(expected, text)
        self.routes["skills/summary"] = summary_payload(eligible=None, blocked=None,
                                                        error="market.zip 缺清单")
        dialog.refresh_all()
        self.app.processEvents()
        text = dialog.summary.text()
        self.assertIn("可注册 —", text)
        self.assertIn("被挡 —", text)
        self.assertIn("扫描告警：market.zip 缺清单", text)

    def test_install_sends_contract_payload_and_refreshes_both_tables(self):
        dialog = self.dialog()
        dialog.market_table.setCurrentCell(0, 1)
        self.app.processEvents()
        self.routes["skills/install"] = {"ok": True, "id": "skill-000", "files": 3, "bytes": 4096,
                                         "replaced": False, "tier": "T1", "tools": ["tool_a"],
                                         "enabled": True, "note": "已安装并注册到工具环",
                                         "error": None}
        self.api.calls.clear()
        dialog._install()
        install = [call for call in self.api.calls if call["path"] == "skills/install"]
        self.assertEqual(len(install), 1)
        self.assertEqual(install[0]["payload"],
                         {"file": "skills/skill-000.zip", "force": False, "enable": True})
        paths = self.api.paths()
        self.assertIn("skills/market", paths)
        self.assertIn("skills/installed", paths)
        self.assertIn("skills/summary", paths)
        self.assertEqual(dialog.notice.text(), "已安装并注册到工具环")

    def test_install_payload_follows_checkboxes(self):
        dialog = self.dialog()
        dialog.market_table.setCurrentCell(4, 1)
        dialog.chk_enable.setChecked(False)
        dialog.chk_force.setChecked(True)
        self.routes["skills/install"] = {"ok": True, "id": "skill-004", "files": 1,
                                         "enabled": False, "note": "已安装，层级受限未注册"}
        self.api.calls.clear()
        dialog._install()
        install = [call for call in self.api.calls if call["path"] == "skills/install"][0]
        self.assertEqual(install["payload"],
                         {"file": "skills/skill-004.zip", "force": True, "enable": False})
        self.assertEqual(dialog.notice.text(), "已安装，层级受限未注册")

    def test_install_failure_shows_mapped_reason_and_keeps_tables(self):
        dialog = self.dialog()
        dialog.market_table.setCurrentCell(0, 1)
        self.routes["skills/install"] = {"ok": False, "id": "skill-000",
                                         "error": {"code": "exists", "message": "file exists"}}
        dialog._install()
        self.assertEqual(dialog.notice.text(), "安装失败：已安装，如需覆盖请勾选覆盖安装")
        self.assertEqual(dialog.market_table.rowCount(), 379)

    def test_uninstall_needs_confirmation_and_sends_id(self):
        dialog = self.dialog()
        dialog.market_table.setCurrentCell(0, 1)
        self.routes["skills/uninstall"] = {"ok": True, "id": "skill-000", "removed": 3, "error": None}
        with patch.object(gui, "ask_confirm", return_value=False):
            dialog._uninstall()
        self.assertNotIn("skills/uninstall", self.api.paths())
        with patch.object(gui, "ask_confirm", return_value=True):
            self.api.calls.clear()
            dialog._uninstall()
        uninstall = [call for call in self.api.calls if call["path"] == "skills/uninstall"]
        self.assertEqual(uninstall[0]["payload"], {"id": "skill-000"})
        self.assertIn("skills/installed", self.api.paths())
        self.assertEqual(dialog.notice.text(), "已卸载 skill-000（删除 3 项）")

    def test_uninstall_from_installed_tab_uses_that_row(self):
        dialog = self.dialog()
        dialog.tabs.setCurrentIndex(1)
        dialog.installed_table.setCurrentCell(1, 1)
        self.routes["skills/uninstall"] = {"ok": True, "id": "skill-003", "removed": 2}
        with patch.object(gui, "ask_confirm", return_value=True):
            self.api.calls.clear()
            dialog._uninstall()
        uninstall = [call for call in self.api.calls if call["path"] == "skills/uninstall"]
        self.assertEqual(uninstall[0]["payload"], {"id": "skill-003"})

    def test_rescan_reports_elapsed_and_refreshes(self):
        dialog = self.dialog()
        self.routes["skills/rescan"] = {"ok": True, "skills": 379, "elapsedMs": 37, "error": None}
        self.api.calls.clear()
        dialog._rescan()
        self.assertIn("skills/rescan", self.api.paths())
        self.assertEqual(dialog.notice.text(), "已重建技能清单：379 个技能，耗时 37 ms")
        self.assertIn("skills/market", self.api.paths())
        self.assertEqual(dialog.btn_rescan.isEnabled(), True)

    def test_rescan_failure_reports_backend_message(self):
        dialog = self.dialog()
        self.routes["skills/rescan"] = {"ok": False,
                                        "error": {"code": "io", "message": "清单重建失败：权限不足"}}
        dialog._rescan()
        self.assertEqual(dialog.notice.text(), "重建清单失败：清单重建失败：权限不足")

    def test_endpoint_unreachable_degrades_gracefully(self):
        failure = {"success": False,
                   "message": "当前后台尚未连接，请稍等；若持续出现，请重新打开控制台。"}
        self.routes.update({"skills/market": failure, "skills/installed": failure,
                            "skills/summary": failure})
        dialog = self.dialog()
        self.assertTrue(dialog.status.text().startswith("技能市场读取失败："))
        self.assertIn("当前后台尚未连接", dialog.status.text())
        self.assertEqual(dialog.market_table.rowCount(), 0)
        self.assertEqual(dialog.installed_table.rowCount(), 0)
        self.assertTrue(dialog.summary.text().startswith("技能汇总读取失败："))
        self.assertEqual(dialog.tabs.tabText(1), "已安装（读取失败）")
        self.assertFalse(dialog.btn_install.isEnabled())
        self.assertFalse(dialog.btn_uninstall.isEnabled())

    def test_dialog_does_not_call_api_until_event_loop_runs(self):
        dialog = gui.SkillMarketDialog()
        self.addCleanup(_close_dialog, dialog)
        self.assertEqual(self.api.calls, [])
        self.app.processEvents()
        self.assertEqual(sorted(self.api.paths()),
                         ["skills/installed", "skills/market", "skills/summary"])

    def test_skill_market_button_visible_in_main_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.addCleanup(patch.stopall)
            patch.object(gui, "AUTO_BACKEND", False).start()
            patch.object(gui.UiHangWatchdog, "start").start()
            window = gui.MainWindow(config_path=Path(tmp) / "gui.json")
            self.addCleanup(window.deleteLater)
            window._timer.stop()
            window.pc_tab.timer.stop()
            window.tabs.setCurrentWidget(window.plugin_tab)
            window.show()
            self.app.processEvents()
            button = window.findChild(QPushButton, "btn_skill_market")
            self.assertIsNotNone(button, "插件页应当有「技能市场」入口按钮")
            self.assertEqual(button.text(), "技能市场")
            self.assertTrue(button.isVisible())
            self.assertTrue(button.isEnabled())
            self.assertIn("T1/T2/T3", button.toolTip())
            self.assertIs(window.plugin_tab.btn_skill_market, button)
            window._quitting = True
            window.close()
            self.app.processEvents()

    def test_dialog_grab_writes_screenshot(self):
        dialog = self.dialog()
        dialog.resize(1080, 720)
        dialog.show()
        self.app.processEvents()
        SCRATCH.mkdir(parents=True, exist_ok=True)
        target = SCRATCH / "skills-dialog.png"
        pixmap = dialog.grab()
        self.assertTrue(pixmap.save(str(target)), "grab() 应当能存成 PNG")
        self.assertEqual((pixmap.width(), pixmap.height()), (1080, 720))
        self.assertGreater(target.stat().st_size, 20000)   # 字体挂上后才画得出字
        self.assertEqual(dialog.market_table.rowCount(), 379)
        self.assertFalse(dialog.btn_install.isEnabled())


if __name__ == "__main__":
    unittest.main()
