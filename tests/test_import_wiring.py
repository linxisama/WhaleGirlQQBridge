"""W11-4b-2 统一导入接线用例（offscreen，不弹真窗口）。

验的是接线本身：open_import_dialog 按新对话框的契约收尾（shutdown），绝不碰 .requests；
四个入口按钮找得到、点得动；真装上了才让所在页刷新。后端一律换成替身，不发真请求。
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

import qq_gui  # noqa: E402

APP = QApplication.instance() or QApplication([])
GUI_SOURCE = REPO_ROOT / "qq_gui.py"
ENTRY_TEXT = "导入包…"
ANY_TEXT = "导入任意包…"
API = object()          # api_post 哨兵：既证明传进去了，又保证不会真的发请求


class FakeDialog:
    """ImportDialog 替身：记生命周期调用，并记下任何越界属性访问。"""

    instances = []

    def __init__(self, api_post=None, parent=None):
        self.api_post = api_post
        self.parent = parent
        self.installed = False
        self.calls = []
        self.touched = []
        FakeDialog.instances.append(self)

    def exec(self):
        self.calls.append("exec")
        return 0

    def shutdown(self):
        self.calls.append("shutdown")
        return True

    def deleteLater(self):
        self.calls.append("deleteLater")

    def __getattr__(self, name):
        # 只记名字再抛：旧接线里的 .requests 会在这里留下证据。
        self.__dict__.setdefault("touched", []).append(name)
        raise AttributeError(name)


class InstalledDialog(FakeDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.installed = True


class BoomExecDialog(FakeDialog):
    def exec(self):
        raise RuntimeError("窗口没弹出来")


class InstalledBoomShutdownDialog(InstalledDialog):
    def shutdown(self):
        raise RuntimeError("收尾时炸了")


class FakePage(QWidget):
    """插件安装窗的上层页替身：只提供该页现有的刷新方法 refresh。"""

    def __init__(self):
        super().__init__()
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


class ImportDialogBase(unittest.TestCase):
    """统一把 import_dialog.ImportDialog 换成替身，并挡住所有真实后端调用。"""

    def setUp(self):
        FakeDialog.instances.clear()
        self.api_post = mock.patch.object(qq_gui, "api_post", API)
        self.api_get = mock.patch.object(qq_gui, "api_get", API)
        self.api_delete = mock.patch.object(qq_gui, "api_delete", API)
        self.api_async = mock.patch.object(qq_gui, "run_api_async", mock.Mock())
        self.dialog_patch = mock.patch("src.gui.import_dialog.ImportDialog", FakeDialog)
        for patcher in (self.api_post, self.api_get, self.api_delete,
                        self.api_async, self.dialog_patch):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_dialog(self, cls):
        patcher = mock.patch("src.gui.import_dialog.ImportDialog", cls)
        patcher.start()
        self.addCleanup(patcher.stop)

    def last_dialog(self):
        return FakeDialog.instances[-1]


class OpenImportDialogTests(ImportDialogBase):
    """open_import_dialog：返回"真装上了吗" + 用新契约收尾。"""

    def test_returns_true_when_dialog_installed(self):
        self.use_dialog(InstalledDialog)
        self.assertTrue(qq_gui.open_import_dialog())

    def test_returns_false_when_nothing_installed(self):
        self.assertFalse(qq_gui.open_import_dialog())

    def test_calls_shutdown_and_never_touches_requests(self):
        self.use_dialog(InstalledDialog)
        self.assertTrue(qq_gui.open_import_dialog())
        dialog = self.last_dialog()
        self.assertIn("exec", dialog.calls)
        self.assertIn("shutdown", dialog.calls)
        self.assertNotIn("requests", dialog.touched)
        self.assertEqual([name for name in dialog.touched if not name.startswith("_")], [])

    def test_passes_module_api_post_and_parent(self):
        parent = QWidget()
        qq_gui.open_import_dialog(parent)
        dialog = self.last_dialog()
        self.assertIs(dialog.api_post, API)
        self.assertIs(dialog.parent, parent)

    def test_dialog_exception_returns_false_without_crashing(self):
        self.use_dialog(BoomExecDialog)
        self.assertFalse(qq_gui.open_import_dialog())
        self.assertIn("shutdown", self.last_dialog().calls)

    def test_shutdown_failure_keeps_the_result(self):
        self.use_dialog(InstalledBoomShutdownDialog)
        self.assertTrue(qq_gui.open_import_dialog())

    def test_source_has_no_dialog_requests(self):
        source = GUI_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("dialog.requests", source)
        function = source.split("def open_import_dialog")[1].split("class ExtensionManagerDialog")[0]
        self.assertNotIn("requests.close()", function)
        self.assertIn("dialog.shutdown", function)


class EntryButtonTests(ImportDialogBase):
    """入口按钮：找得到、文案对、点得动；真装上了才刷新所在页。"""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.opener = mock.Mock(side_effect=lambda parent=None: self.calls.append(parent) or False)
        patcher = mock.patch.object(qq_gui, "open_import_dialog", self.opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def click(self, button):
        self.assertTrue(button.isEnabled(), "入口按钮必须是可点的")
        button.click()

    def test_extension_manager_entry(self):
        dialog = qq_gui.ExtensionManagerDialog()
        self.assertEqual(dialog.btn_import_package.text(), ENTRY_TEXT)
        self.click(dialog.btn_import_package)
        self.assertEqual(self.opener.call_count, 1)

    def test_skill_market_entry(self):
        dialog = qq_gui.SkillMarketDialog()
        self.assertEqual(dialog.btn_import_package.text(), ENTRY_TEXT)
        self.click(dialog.btn_import_package)
        self.assertEqual(self.opener.call_count, 1)

    def test_plugin_tab_entry(self):
        page = qq_gui.PluginTab()
        self.assertEqual(page.btn_import_any.text(), ANY_TEXT)
        self.click(page.btn_import_any)
        self.assertEqual(self.opener.call_count, 1)

    def test_plugin_install_dialog_entry(self):
        page = FakePage()                       # 留着引用：父窗被回收会带走子窗的控件
        dialog = qq_gui.PluginInstallDialog(page)
        self.assertEqual(dialog.btn_import_any.text(), ANY_TEXT)
        self.click(dialog.btn_import_any)
        self.assertEqual(self.opener.call_count, 1)

    def test_old_pack_button_survives_untouched(self):
        page = qq_gui.PluginTab()
        self.assertEqual(page.btn_pack.text(), "导入功能包")
        self.assertTrue(page.btn_pack.isEnabled())

    def test_plugin_tab_refreshes_only_when_installed(self):
        page = qq_gui.PluginTab()
        with mock.patch.object(qq_gui.PluginTab, "refresh") as refresh:
            page.btn_import_any.click()
            self.assertEqual(refresh.call_count, 0)
            self.opener.side_effect = lambda parent=None: True
            page.btn_import_any.click()
            self.assertEqual(refresh.call_count, 1)

    def test_skill_market_refreshes_when_installed(self):
        dialog = qq_gui.SkillMarketDialog()
        with mock.patch.object(qq_gui.SkillMarketDialog, "refresh_all") as refresh:
            self.opener.side_effect = lambda parent=None: True
            dialog.btn_import_package.click()
            self.assertEqual(refresh.call_count, 1)

    def test_extension_manager_refreshes_when_installed(self):
        dialog = qq_gui.ExtensionManagerDialog()
        with mock.patch.object(qq_gui.ExtensionManagerDialog, "_refresh") as refresh:
            self.opener.side_effect = lambda parent=None: True
            dialog.btn_import_package.click()
            self.assertEqual(refresh.call_count, 1)

    def test_plugin_install_dialog_refreshes_parent_page(self):
        page = FakePage()
        dialog = qq_gui.PluginInstallDialog(page)
        self.opener.side_effect = lambda parent=None: True
        dialog.btn_import_any.click()
        self.assertEqual(page.refreshed, 1)


if __name__ == "__main__":
    unittest.main()
