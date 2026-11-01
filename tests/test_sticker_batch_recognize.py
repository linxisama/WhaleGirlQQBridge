"""表情库「一键识别全部」的回归测试（真机需求 2026-10-17）。

用户原话："这个加一个一键识别所有表情的功能"。

要点：
- 库里几十张时，一张一张点「AI识别这张图」太费事；
- 识别一张 = 一次视觉模型调用，**按次计费的中转站每张扣一次钱** →
  必须先问清范围（只补空缺 / 全部重来），默认按钮落在"取消"；
- 识别要跑在后台线程，不能卡界面，并且能中途停止；
- 关窗时先让线程收尾，避免线程还在跑而控件已销毁（之前踩过 QThread 的坑）。
"""
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.gui import diagnostics  # noqa: E402
from src.gui.stickers import BatchRecognizeWorker, StickerLibraryDialog  # noqa: E402

APP = QApplication.instance() or QApplication([])

# 真实接口 /api/stickers 的返回形状：{items,total,page,pages}，**没有 success 字段**。
# 1.6.35 的 bug 就出在这里：代码要求 success 为真 → 第一页被判失败 → 返回空列表，
# 于是表格里有 16 张、点「一键识别全部」却提示图库里还没有表情。
PAGES = {
    1: {"pages": 2, "total": 4, "page": 1,
        "items": [{"id": "a", "label": "猫猫", "description": ""},
                  {"id": "b", "label": "狗狗", "description": "已有的说明"}]},
    2: {"pages": 2, "total": 4, "page": 2,
        "items": [{"id": "c", "label": "鲸鱼", "description": ""},
                  {"id": "d", "label": "兔兔", "description": "  "}]},
}


def make_dialog(post=None):
    diagnostics.install(pathlib.Path(tempfile.mkdtemp()), lambda title, message: None)
    calls = []

    def fake_get(path):
        page = int(path.rsplit("page=", 1)[-1])
        calls.append(page)
        return PAGES.get(page, {"success": True, "pages": 2, "items": []})

    def fake_post(path, data=None, timeout=30):
        calls.append(path)
        return post(path) if post else {"success": True, "message": "识别完成"}

    dialog = StickerLibraryDialog(pathlib.Path(tempfile.mkdtemp()), fake_get,
                                  fake_post, lambda path: {"success": True})
    return dialog, calls


class BatchButtonTests(unittest.TestCase):
    def test_button_exists_and_hidden_stop(self):
        dialog, _ = make_dialog()
        try:
            self.assertIn("一键识别", dialog.batch_button.text())
            self.assertTrue(dialog.stop_button.isHidden(), "没在识别时不显示停止按钮")
        finally:
            dialog.close()

    def test_all_entries_works_without_success_field(self):
        """列表接口没有 success 字段也必须能收全（1.6.35 的真机 bug）。"""
        dialog, _ = make_dialog()
        try:
            entries = dialog._all_entries()
            self.assertEqual(len(entries), 4,
                             f"接口不返回 success 时也要能读到条目：{entries}")
        finally:
            dialog.close()

    def test_all_entries_walks_every_page(self):
        dialog, calls = make_dialog()
        try:
            entries = dialog._all_entries()
            self.assertEqual(len(entries), 4, f"要把分页都收全：{entries}")
            self.assertEqual(sorted(set(calls)), [1, 2])
        finally:
            dialog.close()

    def test_empty_library_is_reported(self):
        diagnostics.install(pathlib.Path(tempfile.mkdtemp()), lambda title, message: None)
        dialog = StickerLibraryDialog(pathlib.Path(tempfile.mkdtemp()),
                                      lambda path: {"success": True, "items": []},
                                      lambda *a, **k: {"success": True},
                                      lambda path: {"success": True})
        try:
            dialog.batch_recognize()
            self.assertIn("还没有表情", dialog.notice.text())
        finally:
            dialog.close()


class BatchWorkerTests(unittest.TestCase):
    def test_counts_success_and_failure(self):
        def post(path, data=None, timeout=30):
            if "/c/" in path:      # 注意 "recognize" 里也有 c，必须按路径段判断
                raise RuntimeError("网络出错")
            if "/d/" in path:
                return {"success": False, "message": "识别失败"}
            return {"success": True}

        entries = [{"id": "a", "label": "猫猫"}, {"id": "c", "label": "鲸鱼"},
                   {"id": "d", "label": "兔兔"}]
        worker = BatchRecognizeWorker(post, entries)
        result = {}
        worker.completed.connect(lambda ok, failed, stopped: result.update(
            {"ok": ok, "failed": failed, "stopped": stopped}))
        worker.start()
        self.assertTrue(worker.wait(8000), "后台线程应当尽快结束")
        APP.processEvents()
        self.assertEqual(result, {"ok": 1, "failed": 2, "stopped": False},
                         f"成功/失败要数清楚：{result}")

    def test_stop_before_start_processes_nothing(self):
        calls = []

        def post(path, data=None, timeout=30):
            calls.append(path)
            return {"success": True}

        worker = BatchRecognizeWorker(post, [{"id": "a"}, {"id": "b"}])
        worker.stop()
        worker.start()
        worker.wait(5000)
        APP.processEvents()
        self.assertLessEqual(len(calls), 1, "请求停止后不该继续逐个识别")


class BatchFlowTests(unittest.TestCase):
    def test_start_batch_disables_button_and_shows_progress(self):
        dialog, calls = make_dialog()
        try:
            dialog._start_batch([{"id": "a", "label": "猫猫"}, {"id": "c", "label": "鲸鱼"}])
            self.assertFalse(dialog.batch_button.isEnabled(), "识别中要禁用按钮防重复点击")
            self.assertFalse(dialog.stop_button.isHidden(), "识别中要能停止")
            worker = dialog._batch_worker
            self.assertIsNotNone(worker)
            worker.wait(8000)
            APP.processEvents()
            self.assertTrue(dialog.batch_button.isEnabled(), "结束后要恢复按钮")
            self.assertIn("成功", dialog.notice.text(), f"要汇报结果：{dialog.notice.text()}")
            self.assertEqual(len([path for path in calls if str(path).startswith("stickers/")]), 2)
        finally:
            dialog.close()

    def test_close_waits_for_worker(self):
        dialog, _ = make_dialog()
        dialog._start_batch([{"id": "a", "label": "猫猫"}])
        dialog.close()          # 关窗要能安全收尾
        self.assertIsNone(getattr(dialog, "_batch_worker", None),
                          "关窗后不该还挂着运行中的线程引用")


if __name__ == "__main__":
    unittest.main()

class ChoiceDialogLayoutTests(unittest.TestCase):
    """多选一对话框的按钮不能被截断（真机截图：『别未识别的（7』）。

    QMessageBox 会按最长正文限制整体宽度，按钮文字过长就被省略号吃掉。
    修法是给按钮加按最长文字算出来的最小宽度；这里直接量布局。
    """

    def test_buttons_are_wide_enough_for_their_labels(self):
        from src.gui.dialogs import _choice_box

        options = [('只识别未识别的 7 张', 'missing'), ('全部重新识别 16 张', 'all'), ('取消', '')]
        box, buttons = _choice_box(None, '一键识别全部',
                                   '图库里共 16 张，其中 7 张还没有图片说明。',
                                   detail='每张都会调用一次视觉模型（按次计费时约等于张数积分）',
                                   options=options)
        try:
            box.show()
            APP.processEvents()
            for button in buttons:
                need = button.fontMetrics().horizontalAdvance(button.text())
                self.assertGreaterEqual(button.width(), need,
                                        f'按钮「{button.text()}」被截断：{button.width()} < {need}')
            self.assertEqual([value for value in buttons.values()], ['missing', 'all', ''])
        finally:
            box.close()

    def test_cancel_is_default(self):
        from src.gui.dialogs import _choice_box

        box, buttons = _choice_box(None, 't', 'x', options=[('确认花钱', 'ok'), ('取消', '')])
        try:
            cancel = next(button for button, value in buttons.items() if not value)
            self.assertIs(box.defaultButton(), cancel,
                          '默认按钮要落在取消上，避免一路回车就把钱花了')
        finally:
            box.close()

    def test_empty_options_returns_empty(self):
        from src.gui.dialogs import ask_choice

        self.assertEqual(ask_choice(None, 't', 'x', options=[]), '')


if __name__ == "__main__":
    unittest.main()
