"""『从本地 zip 安装…』的 GUI 流测试（offscreen；run_api_async 换成记录器，不连后端）。

覆盖三件事：预检失败只弹错、不发安装；确认框取消就不发安装；确认后 payload 受约束，
装完把后端 message 显示出来并重拉清单。
"""
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import qq_gui  # noqa: E402

# 必须持有模块级引用：QApplication 被回收后再构造任何 QWidget 会直接结束进程。
_APP = QApplication.instance() or QApplication([])

REPORT_OK = {"success": True, "ok": True, "id": "wp20-demo", "name": "演示包",
             "version": "1.0.0", "tier": "T1-ready", "blockers": [], "problems": [],
             "files": 2, "bytes": 2048, "existing": False, "note": ""}

MESSAGE = "已安装 wp20-demo 并在能力桥启用，宿主已加载，探测到 1 个工具"


class LocalZipGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wp20-gui-"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.zip_path = self.root / "wp20-demo.zip"
        with zipfile.ZipFile(str(self.zip_path), "w") as archive:
            archive.writestr("wp20-demo/plugin.json", '{"id": "wp20-demo"}')
        self.calls = []

        def fake_run(widget, path, payload=None, *, timeout=30, callback=None, busy=""):
            self.calls.append({"path": path, "payload": payload, "timeout": timeout,
                               "callback": callback})

        for patch in (mock.patch.object(qq_gui, "run_api_async", side_effect=fake_run),
                      mock.patch.object(qq_gui, "tell"),
                      mock.patch.object(qq_gui, "ask_confirm", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.tell = qq_gui.tell
        self.ask_confirm = qq_gui.ask_confirm

        self.dialog = qq_gui.ExtensionManagerDialog()
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.close)

    # ── 工具 ────────────────────────────────────────────────────────────
    def _paths(self):
        return [call["path"] for call in self.calls]

    def _first(self, path):
        for call in self.calls:
            if call["path"] == path:
                return call
        self.fail("没有发过 %s 请求，实际发出：%s" % (path, self._paths()))

    def _last(self, path):
        found = [call for call in self.calls if call["path"] == path]
        if not found:
            self.fail("没有发过 %s 请求，实际发出：%s" % (path, self._paths()))
        return found[-1]

    def _pick(self):
        return mock.patch.object(qq_gui.QFileDialog, "getOpenFileName",
                                 return_value=(str(self.zip_path), "扩展包 (*.zip)"))

    def _import(self):
        with self._pick():
            self.dialog._import_local_zip()

    # ── k) 预检失败：弹错误框 + 不发安装 ─────────────────────────────────
    def test_inspect_failure_shows_error_and_skips_install(self):
        self._import()
        inspect_call = self._first("extensions/inspect-local")
        self.assertEqual(inspect_call["payload"], {"zipPath": str(self.zip_path)})
        self.assertEqual(inspect_call["timeout"], 60)
        inspect_call["callback"]({"ok": False, "problems": ["压缩包不是合法 zip"],
                                  "message": "这个包不能装：压缩包不是合法 zip"})
        self.assertTrue(self.dialog.notice.text().startswith("预检没通过："))
        self.assertIn("压缩包不是合法 zip", self.dialog.notice.text())
        self.tell.assert_called_once()
        args, kwargs = self.tell.call_args
        self.assertEqual(args[1], "本地扩展包不能安装")
        self.assertIn("压缩包不是合法 zip", args[2])
        self.assertEqual(kwargs.get("kind"), "error")
        self.ask_confirm.assert_not_called()
        self.assertEqual(self._paths().count("extensions/install-local"), 0)

    def test_no_file_selected_installs_nothing(self):
        with mock.patch.object(qq_gui.QFileDialog, "getOpenFileName", return_value=("", "")):
            self.dialog._import_local_zip()
        self.assertEqual(self.dialog.notice.text(), "没有选择文件，什么都没装")
        self.assertEqual(self._paths().count("extensions/inspect-local"), 0)

    # ── 确认框：取消 → 不发安装 ─────────────────────────────────────────
    def test_confirm_cancel_skips_install(self):
        self.ask_confirm.return_value = False
        self._import()
        self._first("extensions/inspect-local")["callback"](dict(REPORT_OK))
        self.ask_confirm.assert_called_once()
        self.assertEqual(self.dialog.notice.text(), "已取消，什么都没装")
        self.assertEqual(self._paths().count("extensions/install-local"), 0)

    def test_confirm_detail_shows_tier_blockers_and_existing_hint(self):
        report = dict(REPORT_OK, tier="T4-host-source", blockers=["host-source"],
                      problems=["提示性的小问题"], existing=True)
        self._import()
        self._first("extensions/inspect-local")["callback"](report)
        args, kwargs = self.ask_confirm.call_args
        self.assertEqual(args[1], "确认安装本地扩展")
        detail = kwargs.get("detail") or args[3]
        self.assertIn("层级：T4-host-source", detail)
        self.assertIn("阻塞原因：host-source", detail)
        self.assertIn("提示：提示性的小问题", detail)
        self.assertIn("宿主根里已有同 id", detail)
        self.assertEqual(kwargs.get("ok_text"), "安装")

    # ── 确认后：payload 受约束 + 装完重拉清单 ───────────────────────────
    def test_confirm_yes_sends_guarded_payload(self):
        self.ask_confirm.return_value = True
        self.dialog.chk_enable.setChecked(True)
        self._import()
        self._first("extensions/inspect-local")["callback"](dict(REPORT_OK))
        call = self._first("extensions/install-local")
        self.assertEqual(call["payload"], {"zipPath": str(self.zip_path), "confirmed": True,
                                           "force": False, "enable": True})
        self.assertEqual(call["timeout"], 180)
        self.dialog.chk_enable.setChecked(False)
        self._import()
        self.calls[-1]["callback"](dict(REPORT_OK))
        self.assertIs(self._last("extensions/install-local")["payload"]["enable"], False)

    def test_install_success_updates_notice_and_reloads(self):
        with mock.patch.object(self.dialog, "_load_extensions") as reloaded:
            self.dialog._local_installed({"ok": True, "success": True, "message": MESSAGE})
        self.assertEqual(self.dialog.notice.text(), MESSAGE)
        reloaded.assert_called_once_with(refresh=True)

    def test_install_failure_updates_notice_and_skips_reload(self):
        with mock.patch.object(self.dialog, "_load_extensions") as reloaded:
            self.dialog._local_installed({"ok": False, "success": False,
                                          "message": "扩展包没装进宿主运行时：这个扩展已经装过"})
        self.assertTrue(self.dialog.notice.text().startswith("本地安装失败："))
        self.assertIn("已经装过", self.dialog.notice.text())
        reloaded.assert_not_called()


if __name__ == "__main__":
    unittest.main()
