"""『扩展管理』对话框冒烟测试（offscreen，不连后端：run_api_async 被替换成记录器）。"""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import qq_gui  # noqa: E402

# 必须持有模块级引用：QApplication 被回收后再构造任何 QWidget 会直接结束进程。
_APP = QApplication.instance() or QApplication([])


def _items():
    """每条用例都拿到全新 dict：_saved() 会就地改这些条目，共享会串味。"""
    return [
        {"id": "ext-a", "name": "扩展甲", "layer": "catalog", "tier": "T1-ready",
         "enabled": True, "toolCount": 2, "hookCount": 1, "blockers": [],
         "path": "D:/x/ext-a.zip"},
        {"id": "ext-b", "name": "扩展乙", "layer": "installed", "tier": "T6-disabled",
         "enabled": False, "toolCount": 0, "hookCount": 0, "blockers": ["zip-missing"],
         "path": ""},
    ]

SUMMARY = {"total": 2, "enabled": 1, "layers": {"catalog": 1, "installed": 1},
           "enabledToolCount": 2, "error": "", "stale": False}


def _drain_dialog(dialog):
    """关窗 + deleteLater + 排空事件队列：否则对话框里的 0ms 定时器会掉进后续用例。"""
    dialog.close()
    dialog.deleteLater()
    QApplication.processEvents()


class ExtensionsDialogTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_run(widget, path, payload=None, *, timeout=30, callback=None, busy=""):
            self.calls.append({"path": path, "payload": payload, "timeout": timeout,
                               "callback": callback});

        self._patch = mock.patch.object(qq_gui, "run_api_async", side_effect=fake_run)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.items = _items()
        self.dialog = qq_gui.ExtensionManagerDialog()
        self.addCleanup(self._drain)

    def _drain(self):
        """关窗并把 deleteLater / 0ms 单发定时器跑完。

        构造函数里排了 QTimer.singleShot(0, self._initial_load)；不排空事件队列就退出用例，
        这些定时器会在别的模块的用例里触发，落到那时生效的 run_api_async 替身上（曾让
        tests/test_skills_gui 的「事件循环前不发请求」变成 20 条 extensions 调用）。
        """
        self.dialog.close()
        self.dialog.deleteLater()
        QApplication.processEvents()

    def _load(self, items=None, summary=None):
        self.dialog._loaded({"ok": True, "success": True,
                             "items": items if items is not None else self.items,
                             "summary": summary if summary is not None else SUMMARY})

    def test_constructs_offscreen(self):
        self.assertEqual(self.dialog.windowTitle(), "扩展管理")
        self.assertEqual(self.dialog.table.columnCount(), 7)
        headers = [self.dialog.table.horizontalHeaderItem(col).text()
                   for col in range(self.dialog.table.columnCount())]
        self.assertEqual(headers, ["启用", "ID", "名称", "层级", "工具数", "钩子", "阻塞原因"])
        self.assertEqual(self.dialog.table.rowCount(), 0)
        self.assertEqual([self.dialog.layer.itemData(index) for index in range(
            self.dialog.layer.count())], ["", "installed", "catalog", "builtin"])
        self.assertEqual(self.dialog.btn_refresh.text(), "刷新")
        self.assertEqual(self.dialog.btn_log.text(), "打开调用日志")
        self.assertEqual(self.dialog.btn_batch_on.text(), "批量启用")
        self.assertEqual(self.dialog.btn_batch_off.text(), "批量停用")

    def test_catalog_layer_hint_explains_why_nothing_is_enabled(self):
        text = self.dialog.layer_hint.text()
        self.assertIn("市场清单", text)
        self.assertIn("不能启用", text)
        self.assertIn("插件市场", text)

    def test_initial_load_goes_through_async_api(self):
        _APP.processEvents()          # 触发 QTimer.singleShot(0, self._initial_load)
        paths = [call["path"] for call in self.calls]
        self.assertEqual(paths[:2], ["extensions", "extensions/status"])
        self.assertEqual(self.calls[0]["payload"], {"refresh": False})

    def test_render_fills_table_and_summary(self):
        self._load()
        self.assertEqual(self.dialog.table.rowCount(), 2)
        first = self.dialog.table.item(0, 0)
        self.assertEqual(first.data(Qt.ItemDataRole.UserRole), "ext-a")
        self.assertEqual(first.checkState(), Qt.CheckState.Checked)
        self.assertEqual(self.dialog.table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(self.dialog.table.item(0, 1).text(), "ext-a")
        self.assertEqual(self.dialog.table.item(0, 4).text(), "2")
        self.assertEqual(self.dialog.table.item(1, 6).text(), "zip-missing")
        self.assertEqual(self.dialog.table.item(0, 6).text(), "—")
        self.assertIn("共 2 个扩展", self.dialog.notice.text())
        self.assertIn("已启用 1 个", self.dialog.notice.text())
        self.assertEqual(self.dialog.status.text(), "显示 2 / 2 个扩展")

    def test_search_and_layer_filters(self):
        self._load()
        self.dialog.search.setText("乙")
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertEqual(self.dialog.table.item(0, 1).text(), "ext-b")
        self.dialog.search.setText("")
        self.dialog.layer.setCurrentIndex(2)          # catalog
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertEqual(self.dialog.table.item(0, 1).text(), "ext-a")
        self.dialog.layer.setCurrentIndex(3)          # builtin：一条都没有
        self.assertEqual(self.dialog.table.rowCount(), 0)
        self.assertIn("显示 0 / 2", self.dialog.status.text())

    def test_toggle_sends_set_enabled_and_updates_state(self):
        self._load()
        self.dialog.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
        sent = [call for call in self.calls if call["path"] == "extensions/set-enabled"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["payload"], {"id": "ext-b", "enabled": True})
        sent[0]["callback"]({"ok": True, "success": True,
                            "item": {"id": "ext-b", "enabled": True}})
        self.assertEqual(self.dialog.table.item(1, 0).checkState(), Qt.CheckState.Checked)
        self.assertIn("已启用 ext-b", self.dialog.notice.text())

    def test_toggle_failure_reverts_after_render(self):
        self._load()
        self.dialog.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
        sent = [call for call in self.calls if call["path"] == "extensions/set-enabled"]
        sent[0]["callback"]({"ok": False, "message": "磁盘满"})
        self.assertEqual(self.dialog.table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
        self.assertIn("磁盘满", self.dialog.notice.text())

    def test_batch_enable_uses_filtered_rows_after_confirm(self):
        self._load()
        with mock.patch.object(qq_gui, "ask_confirm", return_value=True) as confirm:
            self.dialog._batch(True)
        self.assertTrue(confirm.called)
        sent = [call for call in self.calls if call["path"] == "extensions/batch-enabled"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["payload"], {"ids": ["ext-a", "ext-b"], "enabled": True})
        sent[0]["callback"]({"ok": True, "success": True, "count": 2, "enabled": True})
        self.assertIn("已启用 2 个扩展", self.dialog.notice.text())

    def test_batch_cancel_sends_nothing(self):
        self._load()
        with mock.patch.object(qq_gui, "ask_confirm", return_value=False):
            self.dialog._batch(False)
        self.assertEqual([call for call in self.calls
                          if call["path"] == "extensions/batch-enabled"], [])

    def test_status_line_when_bridge_enabled(self):
        self.dialog._status_loaded({"ok": True, "available": True, "loaded": True,
                                   "enabled_extensions": ["ext-a"], "calls": 7,
                                   "tool_failures": 1, "log_path": "D:/x/calls.jsonl",
                                   "capabilities": [{"deadInHost": True}]})
        text = self.dialog.bridge_state.text()
        self.assertIn("能力桥：已启用", text)
        self.assertIn("调用 7 次", text)
        self.assertIn("失败 1 次", text)
        self.assertIn("宿主不支持的能力 1 项", text)
        self.assertEqual(self.dialog._log_path, "D:/x/calls.jsonl")

    def test_status_line_when_bridge_missing_or_disabled(self):
        self.dialog._status_loaded({"ok": True, "available": False, "loaded": False,
                                   "message": "QQ Agent 能力桥未加载"})
        self.assertIn("未加载", self.dialog.bridge_state.text())
        self.dialog._status_loaded({"ok": True, "available": False, "loaded": True,
                                   "message": "能力桥已加载但未启用"})
        self.assertIn("已加载但未启用", self.dialog.bridge_state.text())

    def test_open_log_reports_missing_path(self):
        self.dialog._open_log()
        self.assertIn("先点「刷新」", self.dialog.notice.text())
        self.dialog._status_loaded({"log_path": "D:/nope/calls.jsonl"})
        self.dialog._open_log()
        self.assertIn("还没有调用记录", self.dialog.notice.text())

    def test_plugin_tab_exposes_entry_point(self):
        self.assertTrue(callable(getattr(qq_gui.PluginTab, "_open_extensions", None)))


if __name__ == "__main__":
    unittest.main()


class InstallNoticesTests(unittest.TestCase):
    """plugin_installer 的 notices 是非阻断提示（m00213）：要显示，但不能拦安装。"""

    def setUp(self):
        self.dialog = qq_gui.PluginInstallDialog()
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.local_path.setText("D:/x/demo.zip")
        self.dialog.preview.setPlainText("原有源码")
        self.dialog.btn_install.setEnabled(False)

    def test_notices_are_shown_but_do_not_block_install(self):
        self.dialog._check_done({"ok": True, "plugin_id": "demo", "kind": "archive",
                                 "resolved_url": "D:/x/demo.zip", "bytes": 2048, "lines": 12,
                                 "notices": ["plugin.json 缺少必填字段 name", "缺少 version"]})
        text = self.dialog.result.text()
        self.assertIn("提示（不影响安装）", text)
        self.assertIn("plugin.json 缺少必填字段 name", text)
        self.assertIn("缺少 version", text)
        self.assertNotIn("不允许安装", text)
        self.assertTrue(self.dialog.btn_install.isEnabled())
        self.assertEqual(self.dialog._inspection["plugin_id"], "demo")
        # 后端这次没给 preview：不能把原有源码清空
        self.assertEqual(self.dialog.preview.toPlainText(), "原有源码")

    def test_missing_notices_key_still_passes(self):
        self.dialog._check_done({"ok": True, "plugin_id": "demo", "resolved_url": "D:/x/demo.zip"})
        text = self.dialog.result.text()
        self.assertIn("检查通过，可以安装", text)
        self.assertNotIn("提示（不影响安装）", text)
        self.assertTrue(self.dialog.btn_install.isEnabled())

    def test_problems_still_block_and_clear_preview(self):
        self.dialog._check_done({"ok": True, "problems": ["包含 exec( 调用"]})
        self.assertIn("不允许安装", self.dialog.result.text())
        self.assertFalse(self.dialog.btn_install.isEnabled())
        self.assertEqual(self.dialog.preview.toPlainText(), "")
        self.assertIsNone(self.dialog._inspection)

    def test_preview_is_replaced_when_backend_sends_it(self):
        self.dialog._check_done({"ok": True, "plugin_id": "demo", "resolved_url": "D:/x/demo.zip",
                                 "preview": "# 新源码", "notices": ["小提示"]})
        self.assertEqual(self.dialog.preview.toPlainText(), "# 新源码")
        self.assertIn("小提示", self.dialog.result.text())

    def test_batch_notices_are_listed_without_failing_the_item(self):
        self.dialog._check_batch_done({"items": [
            {"ok": True, "path": "D:/x/a.zip", "plugin_id": "a",
             "notices": ["plugin.json 缺少必填字段 version"]},
            {"ok": False, "path": "D:/x/b.zip", "plugin_id": "b", "error": "不是插件包"},
        ]})
        text = self.dialog.result.text()
        self.assertIn("提示：plugin.json 缺少必填字段 version", text)
        self.assertIn("1. a（通过）", text)
        self.assertIn("2. b（未通过）：不是插件包", text)
        self.assertTrue(self.dialog.btn_install.isEnabled())

class MarketInstallEnableTests(unittest.TestCase):
    """市场对话框：安装即启用是默认动作，取消勾选才回到旧行为。"""

    def setUp(self):
        self.calls = []

        def fake_run(widget, path, payload=None, *, timeout=30, callback=None, busy=""):
            self.calls.append({"path": path, "payload": payload, "callback": callback})

        self._patch = mock.patch.object(qq_gui, "run_api_async", side_effect=fake_run)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.dialog = qq_gui.MarketDialog()
        self.addCleanup(_drain_dialog, self.dialog)
        self.dialog._items = [{"id": "ext-a", "name": "扩展甲", "categoryName": "工具",
                               "version": "1.0.0", "downloads": 3,
                               "downloadUrl": "https://example.com/ext-a.zip"}]
        self.dialog._fill(self.dialog._items)

    def _install_payload(self):
        calls = [call for call in self.calls if call["path"] == "qqagent/market/install"]
        self.assertTrue(calls, "没有发出安装请求")
        return calls[-1]["payload"]

    def test_enable_checkbox_defaults_on_and_request_carries_it(self):
        self.assertIn("启用", self.dialog.chk_enable.text())
        self.assertTrue(self.dialog.chk_enable.isChecked(), "主动点安装说明意图明确，默认勾选")
        self.dialog.table.selectRow(0)
        self.dialog._install()
        self.assertEqual(self._install_payload(), {"id": "ext-a", "enable": True})

    def test_unchecked_falls_back_to_old_disabled_behaviour(self):
        self.dialog.chk_enable.setChecked(False)
        self.dialog.table.selectRow(0)
        self.dialog._install()
        self.assertEqual(self._install_payload(), {"id": "ext-a", "enable": False})

    def test_reinstall_checkbox_sends_force(self):
        self.assertFalse(self.dialog.chk_force.isChecked(), "默认沿用已装目录，不覆盖")
        self.dialog.chk_force.setChecked(True)
        self.dialog.table.selectRow(0)
        self.dialog._install()
        self.assertEqual(self._install_payload(),
                         {"id": "ext-a", "enable": True, "force": True})

    def test_install_failure_shows_fastapi_detail(self):
        self.dialog._installed({"detail": "扩展已存在：D:/x/plugins/ext-a（exists）"})
        text = self.dialog.notice.text()
        self.assertIn("扩展已存在", text)
        self.assertNotEqual(text, "安装失败")

    def test_installed_notice_shows_backend_reason_when_not_enabled(self):
        self.dialog._installed({"success": True,
                                "message": "已安装 ext-a，但未启用：T6-disabled（阻塞原因：zip-missing）",
                                "extension": {"path": "D:/x/ext-a"}})
        text = self.dialog.notice.text()
        self.assertIn("未启用", text)
        self.assertIn("T6-disabled", text)
        self.assertIn("D:/x/ext-a", text)

    def test_installed_notice_shows_enabled_path(self):
        self.dialog._installed({"success": True, "message": "已安装 ext-a 并在能力桥启用",
                                "extension": {"path": "D:/x/ext-a"}})
        self.assertIn("并在能力桥启用", self.dialog.notice.text())

    def test_bridge_status_keeps_sync_notice_after_refresh(self):
        # 同步完成后会顺手续读状态；不能把"已写入能力桥配置"盖成一直转圈的提示。
        self.dialog._refresh_bridge("已写入能力桥配置（2 个扩展）")
        call = [item for item in self.calls
                if item["path"] == "qqagent/bridge/status"][-1]
        call["callback"]({"success": True, "loaded": True, "enabled": True,
                          "ready": True, "profiles": 2, "missing": []})
        self.assertEqual(self.dialog.notice.text(), "已写入能力桥配置（2 个扩展）")

    def test_bridge_sync_keeps_install_notice(self):
        self.dialog._installed({"success": True, "message": "已安装 ext-a 并在能力桥启用",
                                "extension": {"path": "D:/x/ext-a"}})
        sync = [item for item in self.calls
                if item["path"] == "qqagent/bridge/sync"][-1]
        sync["callback"]({"success": True, "catalog": {"count": 3}, "missing": []})
        status = [item for item in self.calls
                  if item["path"] == "qqagent/bridge/status"][-1]
        status["callback"]({"success": True, "loaded": True, "enabled": True,
                            "ready": True, "profiles": 3, "missing": []})
        text = self.dialog.notice.text()
        self.assertIn("已安装 ext-a 并在能力桥启用", text)
        self.assertIn("D:/x/ext-a", text)
        self.assertIn("已写入能力桥配置（3 个扩展）", text)

    def test_link_import_sends_url_and_confirmation(self):
        with mock.patch.object(qq_gui.QInputDialog, "getText",
                               return_value=(" https://example.com/ext.zip ", True)):
            self.dialog._import_url()
        call = [item for item in self.calls if item["path"] == "extensions/install-url"][-1]
        self.assertEqual(call["payload"]["url"], "https://example.com/ext.zip")
        self.assertEqual(call["payload"]["confirmed"], True)
        self.assertEqual(call["payload"]["enable"], True)

    def test_local_zip_import_sends_path_and_confirmation(self):
        with mock.patch.object(qq_gui.QFileDialog, "getOpenFileName",
                               return_value=("D:/tmp/ext.zip", "插件包 (*.zip)")):
            self.dialog._import_zip()
        call = [item for item in self.calls if item["path"] == "extensions/install-local"][-1]
        self.assertEqual(call["payload"]["zipPath"], "D:/tmp/ext.zip")
        self.assertEqual(call["payload"]["confirmed"], True)
        self.assertEqual(call["payload"]["enable"], True)

    def test_open_market_web_button_is_gone(self):
        self.assertFalse(hasattr(self.dialog, "btn_open"))
        self.assertTrue(hasattr(self.dialog, "btn_import_url"))
        self.assertTrue(hasattr(self.dialog, "btn_import_zip"))

