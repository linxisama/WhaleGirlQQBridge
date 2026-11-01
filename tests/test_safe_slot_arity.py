"""`safe_slot` 包装不能改变槽的调用约定（真机 2026-10-16 事故）。

现象（用户截图）：表情包管理窗底部出现

    表情库 显示失败（已跳过这一块）：TypeError:
    StickerLibraryDialog.refresh() takes 1 positional argument but 2 were given

根因：PyQt 连信号时，若槽函数声明了固定参数，多出来的信号参数会被**自动丢弃**
（`clicked(bool)` 接 `def refresh(self)` 合法）。一旦用 ``*args`` 包装，PyQt 就会把
信号参数全传进来 → 包装函数把多出来的参数转发给原函数 → TypeError。

修法：`safe_slot` 包装时按原函数签名**裁掉多余的位置参数**，保持包装前后行为一致。
"""
import inspect
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton  # noqa: E402

from src.gui import diagnostics  # noqa: E402
from src.gui.diagnostics import safe_slot  # noqa: E402
from src.gui.stickers import StickerLibraryDialog  # noqa: E402

APP = QApplication.instance() or QApplication([])


class SafeSlotArityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        diagnostics.install(tempfile.mkdtemp(), lambda title, message: None)

    def test_zero_argument_slot_tolerates_signal_arguments(self):
        class Demo:
            def __init__(self):
                self.calls = []

            @safe_slot(label="演示")
            def refresh(self):
                self.calls.append("ok")

        demo = Demo()
        demo.refresh(False)          # clicked(bool)
        demo.refresh()               # 普通调用
        demo.refresh(False, "多余")   # 更多参数也不该炸
        self.assertEqual(demo.calls, ["ok", "ok", "ok"])

    def test_slot_with_parameters_still_receives_them(self):
        class Demo:
            def __init__(self):
                self.seen = []

            @safe_slot(label="演示")
            def populate(self, data):
                self.seen.append(data)

        demo = Demo()
        demo.populate({"a": 1})      # 正常参数
        demo.populate({"b": 2}, False)   # 多一个也要裁掉、第一个参数照常生效
        self.assertEqual(demo.seen, [{"a": 1}, {"b": 2}])

    def test_varargs_slot_is_not_trimmed(self):
        class Demo:
            def __init__(self):
                self.count = 0

            @safe_slot(label="演示")
            def refresh(self, *args):
                self.count = len(args)

        demo = Demo()
        demo.refresh(1, 2, 3)
        self.assertEqual(demo.count, 3, "原函数本来就能接任意参数时不该裁剪")

    def test_wrapped_signature_is_preserved(self):
        class Demo:
            @safe_slot(label="演示")
            def refresh(self):
                pass

        # functools.wraps 生效：inspect 看到的是原签名，Qt/PyQt 才能正确判断参数
        self.assertEqual(list(inspect.signature(Demo().refresh).parameters), [])

    def test_sticker_dialog_refresh_works_when_wired_to_clicked(self):
        """真机场景：按钮 clicked(bool) 直连 refresh，不能再报"显示失败"。"""
        dialog = StickerLibraryDialog(
            tempfile.mkdtemp(),
            lambda path: {"items": [{"id": "1", "label": "猫猫", "uses": 0, "thumbnail": ""}],
                          "pages": 1, "total": 1},
            lambda path, payload: {"success": True},
            lambda *args: None)
        button = QPushButton("刷新")
        button.clicked.connect(dialog.refresh)
        button.click()
        APP.processEvents()
        self.assertEqual(dialog.table.rowCount(), 1, "点击刷新应当正常填充表格")
        self.assertEqual(dialog.notice.text(), "",
                         "不该再出现「表情库 显示失败」的提示")


if __name__ == "__main__":
    unittest.main()
