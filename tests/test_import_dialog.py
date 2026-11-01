"""W11-4b-1 统一导入对话框用例（offscreen，不弹真窗口）。

后端一律换成假 api_post：这里验的是「schema 渲染得对不对」「请求体传得对不对」
「出错时不崩、也不假报成功」。
"""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from src.gui import import_dialog  # noqa: E402
from src.gui.import_dialog import ImportDialog, open_import  # noqa: E402

APP = QApplication.instance() or QApplication([])
NL = chr(10)
PACK = "C:/packs/demo.zip"


def payload(kind="plugin-py", **overrides):
    """后端 pack_import 的 19 键 schema（安装/预检同一份）。"""
    base = {
        "ok": True,
        "kind": kind,
        "id": "demo",
        "name": "演示包",
        "version": "1.0",
        "entry": "demo.py",
        "files": 3,
        "bytes": 2048,
        "landed": "",
        "target_dir": "C:/data/packs/demo",
        "tools": [{"name": "demo_tool"}],
        "hooks": [],
        "providers": [],
        "problems": [],
        "notices": [{"code": "not-enabled", "message": "装完还要在设置里打开才会被调用"}],
        "load": {"ok": False, "error": "", "code": "not-loaded"},
        "installed_but_disabled": False,
        "actions": [],
        "duration_ms": 12,
    }
    base.update(overrides)
    return base


class FakeApi:
    """假 api_post：记下每次 (suffix, body)，可按 suffix 回不同结果，也能慢、也能炸。"""

    def __init__(self, inspect=None, install=None, delay=0.0, raises=None):
        self.calls = []
        self.inspect_reply = payload() if inspect is None else inspect
        self.install_reply = (payload(landed="C:/data/packs/demo", duration_ms=40)
                              if install is None else install)
        self.delay = delay
        self.raises = raises

    def __call__(self, suffix, body):
        self.calls.append((str(suffix), dict(body or {})))
        if self.delay:
            time.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.inspect_reply if str(suffix).endswith("inspect") else self.install_reply

    def suffixes(self):
        return [suffix for suffix, _body in self.calls]


def wait_idle(dialog, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        APP.processEvents()
        if not dialog.busy:
            return True
        time.sleep(0.01)
    return False


def visible_text(widget) -> str:
    parts = [label.text() for label in widget.findChildren(QLabel) if not label.isHidden()]
    return NL.join(parts)


def has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(text))


class DialogCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.dialog = self.make_dialog()

    def make_dialog(self, api=None, path=PACK):
        dialog = ImportDialog(self.api if api is None else api)
        self.addCleanup(self.drop, dialog)
        if path:
            dialog.set_path(path)
        return dialog

    @staticmethod
    def drop(dialog):
        dialog.shutdown()
        dialog.deleteLater()

    def preflight(self, data):
        """直接走落地回调：纯渲染用例不必绕工作线程。"""
        self.dialog._inspected(data)

    def install(self, dialog=None):
        dialog = dialog or self.dialog
        self.assertTrue(dialog.install_package())
        self.assertTrue(wait_idle(dialog))

    def texts(self, dialog=None) -> str:
        return visible_text(dialog or self.dialog)


class KindLabelTest(DialogCase):
    def test_kind_labels_are_exactly_the_contract(self):
        self.assertEqual(import_dialog.KIND_LABELS, {
            "plugin-py": "Python 插件",
            "extension-js": "JS 扩展",
            "skill": "技能",
            "feature-pack": "功能包",
            "mcp-server": "MCP 服务",
            "mod-py": "目录 Mod",
            "unknown": "认不出来",
        })

    def _assert_render(self, kind, label):
        self.preflight(payload(kind=kind))
        self.assertEqual(self.dialog.badge.text(), label)
        self.assertIn(label, self.texts())
        self.assertTrue(self.dialog.warning.isHidden())
        self.assertTrue(self.dialog.btn_install.isEnabled())

    def test_plugin_py_label(self):
        self._assert_render("plugin-py", "Python 插件")

    def test_extension_js_label(self):
        self._assert_render("extension-js", "JS 扩展")

    def test_skill_label(self):
        self._assert_render("skill", "技能")

    def test_feature_pack_label(self):
        self._assert_render("feature-pack", "功能包")

    def test_mcp_server_label(self):
        self._assert_render("mcp-server", "MCP 服务")

    def test_unknown_kind_warns_and_blocks_install(self):
        self.preflight(payload(kind="unknown", ok=False, problems=[
            {"code": "unknown-family", "message": "看不出这是什么包", "hint": "确认是官方发布的包"}]))
        self.assertEqual(self.dialog.badge.text(), "认不出来")
        self.assertFalse(self.dialog.warning.isHidden())
        self.assertFalse(self.dialog.btn_install.isEnabled())


class InstallGateTest(DialogCase):
    def test_install_disabled_until_ok(self):
        self.assertFalse(self.dialog.btn_install.isEnabled())
        self.preflight(payload(ok=True))
        self.assertTrue(self.dialog.btn_install.isEnabled())

    def test_ok_false_shows_blocked_text(self):
        self.preflight(payload(ok=False, problems=[
            {"code": "b1-dialect", "message": "这个包用了不支持的写法", "hint": "换成标准包"}]))
        self.assertFalse(self.dialog.btn_install.isEnabled())
        self.assertIn("预检没通过，先按下面的提示处理", self.dialog.status.text())

    def test_install_refused_when_not_ok(self):
        self.preflight(payload(ok=False))
        self.assertFalse(self.dialog.install_package())
        self.assertEqual(self.api.calls, [])
        self.assertIn("预检没通过", self.dialog.status.text())


class ProblemRenderTest(DialogCase):
    def test_problems_message_and_hint_rendered(self):
        self.preflight(payload(ok=False, problems=[
            {"code": "zip-missing-entry", "message": "包里没有入口文件", "hint": "补齐 entry 字段"}]))
        texts = self.texts()
        self.assertIn("zip-missing-entry", texts)
        self.assertIn("包里没有入口文件", texts)
        self.assertIn("补齐 entry 字段", texts)

    def test_problem_without_hint_has_no_hint_row(self):
        self.preflight(payload(ok=False, problems=[{"code": "solo", "message": "只有一句说明"}]))
        self.assertEqual(len(self.dialog.problem_rows), 1)
        self.assertIsNone(self.dialog.problem_rows[0]["hint"])
        self.assertNotIn("→", self.texts())

    def test_problems_are_selectable_and_wrapped(self):
        self.preflight(payload(ok=False, problems=[
            {"code": "long", "message": "很长的说明" * 30, "hint": "很长的提示" * 30}]))
        message = self.dialog.problem_rows[0]["message"]
        self.assertTrue(message.wordWrap())
        self.assertTrue(message.textInteractionFlags()
                        & Qt.TextInteractionFlag.TextSelectableByMouse)
        self.assertTrue(self.dialog.scroll.widgetResizable())

    def test_summary_lists_counts_and_actions(self):
        self.preflight(payload(tools=[{"name": "a"}, {"name": "b"}], hooks=[{"name": "h"}],
                               providers=[{"name": "p"}, {"name": "q"}, {"name": "r"}],
                               actions=["install", "enable"]))
        summary = self.dialog.summary.text()
        self.assertIn("工具 2 个 / hook 1 个 / provider 3 个", summary)
        self.assertIn("install、enable", summary)
        self.assertIn("C:/data/packs/demo", summary)

    def test_notices_are_rendered(self):
        self.preflight(payload(notices=[{"code": "n1", "message": "装完要重启一次"}]))
        self.assertIn("装完要重启一次", self.texts())


class InstallFlowTest(DialogCase):
    def test_install_payload_carries_confirmed_kind_overwrite_enable(self):
        api = FakeApi(inspect=payload(kind="extension-js"))
        dialog = self.make_dialog(api)
        self.assertTrue(dialog.inspect_package())
        self.assertTrue(wait_idle(dialog))
        dialog.chk_overwrite.setChecked(True)
        dialog.enable_combo.setCurrentIndex(1)
        self.install(dialog)
        suffix, body = api.calls[-1]
        self.assertEqual(suffix, "import/install")
        self.assertIs(body["confirmed"], True)
        self.assertEqual(body["kind"], "extension-js")
        self.assertIs(body["overwrite"], True)
        self.assertIs(body["enable"], True)
        self.assertEqual(body["path"], PACK)

    def test_enable_tristate_maps_to_none_true_false(self):
        api = FakeApi(inspect=payload(kind="skill"))
        dialog = self.make_dialog(api)
        self.assertTrue(dialog.inspect_package())
        self.assertTrue(wait_idle(dialog))
        for index, expected in ((0, None), (1, True), (2, False)):
            with self.subTest(enable=expected):
                dialog.enable_combo.setCurrentIndex(index)
                self.install(dialog)
                self.assertIs(api.calls[-1][1]["enable"], expected)

    def test_install_success_shows_target_dir_and_landed(self):
        api = FakeApi(inspect=payload(kind="feature-pack"),
                      install=payload(kind="feature-pack", landed="C:/data/packs/demo",
                                      installed_but_disabled=False))
        dialog = self.make_dialog(api)
        self.assertTrue(dialog.inspect_package())
        self.assertTrue(wait_idle(dialog))
        self.install(dialog)
        texts = self.texts(dialog)
        self.assertIn("功能包", texts)
        self.assertIn("C:/data/packs/demo", texts)
        self.assertIn("已落盘", texts)
        self.assertIn("安装完成", dialog.status.text())
        self.assertTrue(dialog.installed)

    def test_installed_but_disabled_is_prominent(self):
        api = FakeApi(inspect=payload(kind="skill"),
                      install=payload(kind="skill", landed="C:/data/packs/demo",
                                      installed_but_disabled=True))
        dialog = self.make_dialog(api)
        self.assertTrue(dialog.inspect_package())
        self.assertTrue(wait_idle(dialog))
        self.install(dialog)
        self.assertIn("已安装但未生效", self.texts(dialog))
        self.assertTrue(dialog.disabled_hint.isVisibleTo(dialog))

    def test_progress_text_during_inspect_and_install(self):
        dialog = self.make_dialog(FakeApi(delay=0.2))
        self.assertTrue(dialog.inspect_package())
        self.assertEqual(dialog.status.text(), "正在预检 demo.zip…")
        self.assertFalse(dialog.btn_install.isEnabled())
        self.assertTrue(wait_idle(dialog))
        self.assertTrue(dialog.install_package())
        self.assertEqual(dialog.status.text(), "正在安装 demo.zip…")
        self.assertTrue(wait_idle(dialog))


class PickTest(DialogCase):
    def test_pick_folder_passes_selected_path(self):
        folder = "C:/packs/skill-dir"
        with mock.patch.object(import_dialog.QFileDialog, "getExistingDirectory",
                               return_value=folder):
            self.dialog.pick_folder()
        self.assertTrue(wait_idle(self.dialog))
        self.assertEqual(self.dialog.path_edit.text(), folder)
        self.assertEqual(self.api.calls[0][0], "import/inspect")
        self.assertEqual(self.api.calls[0][1]["path"], folder)

    def test_pick_file_passes_path_and_filter(self):
        target = "C:/packs/plugin.zip"
        with mock.patch.object(import_dialog.QFileDialog, "getOpenFileName",
                               return_value=(target, "")) as picker:
            self.dialog.pick_file()
        self.assertTrue(wait_idle(self.dialog))
        self.assertEqual(self.api.calls[0][1]["path"], target)
        used = picker.call_args.args[-1]
        for pattern in ("*.zip", "*.py", "*.js", "*.mjs", "*.json"):
            self.assertIn(pattern, used)

    def test_cancel_keeps_previous_path_and_sends_nothing(self):
        with mock.patch.object(import_dialog.QFileDialog, "getOpenFileName",
                               return_value=("", "")):
            self.dialog.pick_file()
        self.assertTrue(wait_idle(self.dialog))
        self.assertEqual(self.dialog.path_edit.text(), PACK)
        self.assertEqual(self.api.calls, [])


class FailureTest(DialogCase):
    def test_inspect_exception_shows_chinese_failure(self):
        dialog = self.make_dialog(FakeApi(raises=RuntimeError("连接被重置")))
        self.assertTrue(dialog.inspect_package())
        self.assertTrue(wait_idle(dialog))
        self.assertIn("导入接口请求失败", dialog.status.text())
        self.assertIn("连接被重置", dialog.status.text())
        self.assertFalse(dialog.btn_install.isEnabled())
        self.assertFalse(dialog.installed)

    def test_install_exception_shows_chinese_failure(self):
        api = FakeApi()
        dialog = self.make_dialog(api)
        self.assertTrue(dialog.inspect_package())
        self.assertTrue(wait_idle(dialog))
        api.raises = RuntimeError("后台没回应")
        self.assertTrue(dialog.install_package())
        self.assertTrue(wait_idle(dialog))
        self.assertIn("导入接口请求失败", dialog.status.text())
        self.assertIn("后台没回应", dialog.status.text())
        self.assertFalse(dialog.installed)

    def test_non_dict_reply_shows_failed_row(self):
        dialog = self.make_dialog(FakeApi(inspect="不是字典"))
        self.assertTrue(dialog.inspect_package())
        self.assertTrue(wait_idle(dialog))
        self.assertIn("预检没通过", dialog.status.text())
        self.assertEqual(len(dialog.problem_rows), 1)
        self.assertIn("request-failed", dialog.problem_rows[0]["code"].text())

    def test_api_post_none_shows_degraded_hint(self):
        dialog = ImportDialog(None)
        self.addCleanup(self.drop, dialog)
        dialog.set_path(PACK)
        self.assertFalse(dialog.inspect_package())
        self.assertEqual(dialog.status.text(), import_dialog.NOT_READY_TEXT)
        self.assertFalse(dialog.install_package())
        self.assertIn("导入接口还没接好，请重启控制台", dialog.status.text())

    def test_window_minimum_size_and_chinese_copy(self):
        self.assertGreaterEqual(self.dialog.minimumWidth(), 720)
        self.assertGreaterEqual(self.dialog.minimumHeight(), 560)
        self.assertEqual(self.dialog.windowTitle(), "导入包")
        for button in (self.dialog.btn_file, self.dialog.btn_folder,
                       self.dialog.btn_inspect, self.dialog.btn_install):
            self.assertTrue(has_chinese(button.text()), button.text())
        for index in range(self.dialog.enable_combo.count()):
            self.assertTrue(has_chinese(self.dialog.enable_combo.itemText(index)))


class OpenImportTest(unittest.TestCase):
    def drop(self, dialog):
        dialog.shutdown()
        dialog.deleteLater()

    def test_open_import_returns_visible_dialog(self):
        api = FakeApi()
        dialog = open_import(api_post=api)
        self.addCleanup(self.drop, dialog)
        self.assertIsInstance(dialog, ImportDialog)
        self.assertTrue(dialog.isVisible())
        dialog.set_path(PACK)
        self.assertTrue(dialog.inspect_package())
        self.assertTrue(wait_idle(dialog))
        self.assertEqual(api.calls[0][1]["path"], PACK)
        self.assertTrue(dialog.btn_install.isEnabled())

    def test_open_import_without_api_post_is_degraded(self):
        dialog = open_import()
        self.addCleanup(self.drop, dialog)
        self.assertTrue(dialog.isVisible())
        dialog.set_path(PACK)
        self.assertFalse(dialog.inspect_package())
        self.assertEqual(dialog.status.text(), import_dialog.NOT_READY_TEXT)


if __name__ == "__main__":
    unittest.main()
