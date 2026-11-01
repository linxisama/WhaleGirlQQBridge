"""电脑控制页：工作空间面板 / 网址收藏夹 / 旧属性名兼容的 offscreen 测试。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QGroupBox, QLabel, QPushButton,
)

from src.gui import pc_workspace  # noqa: E402

_APP = QApplication.instance() or QApplication([])

OLD_ATTRS = ("path_edit", "url_edit", "type_edit", "hotkey_edit", "x_edit", "y_edit", "pc_info")
OLD_METHODS = ("pick_path", "open_path", "open_url", "type_text", "send_hotkey", "click",
               "screenshot", "copy_screenshot_path", "read_clipboard", "write_clipboard",
               "open_notepad")


class WorkspaceCase(unittest.TestCase):
    """每个用例一个临时数据根，绝不碰仓库里的 data/。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.root = pc_workspace.workspace_root(self.data_dir)
        (self.root / "笔记.txt").write_text("hello", encoding="utf-8")
        (self.root / "大文件.bin").write_bytes(b"x" * 2048)
        (self.root / "子目录").mkdir()
        (self.root / "子目录" / "里面.txt").write_text("inner", encoding="utf-8")
        (self.root / "空目录").mkdir()

    def tearDown(self):
        self._tmp.cleanup()


class TestWorkspaceFiles(WorkspaceCase):
    def test_list_root_dirs_first(self):
        result = pc_workspace.list_entries(data_dir=self.data_dir)
        self.assertTrue(result["ok"])
        names = [entry["name"] for entry in result["entries"]]
        self.assertEqual(set(names), {"笔记.txt", "大文件.bin", "子目录", "空目录"})
        kinds = [entry["type"] for entry in result["entries"]]
        self.assertEqual(kinds[:2], ["dir", "dir"], "文件夹要排在文件前面")
        self.assertEqual(kinds[2:], ["file", "file"])
        self.assertEqual(result["relpath"], "")
        sizes = {entry["name"]: entry["size"] for entry in result["entries"]}
        self.assertEqual(sizes["大文件.bin"], 2048)
        self.assertEqual(sizes["子目录"], 0)
        self.assertTrue(all(entry["mtime"] > 0 for entry in result["entries"]))

    def test_enter_subdir(self):
        result = pc_workspace.list_entries("子目录", data_dir=self.data_dir)
        self.assertTrue(result["ok"])
        self.assertEqual(result["relpath"], "子目录")
        self.assertEqual([entry["name"] for entry in result["entries"]], ["里面.txt"])
        self.assertEqual(result["entries"][0]["relpath"], "子目录/里面.txt")

    def test_traversal_rejected(self):
        for bad in ("../../", "../", "子目录/../../"):
            with self.subTest(relpath=bad):
                result = pc_workspace.list_entries(bad, data_dir=self.data_dir)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "denied")
                self.assertIn("..", result["error"]["message"])

    def test_make_folder_ok(self):
        result = pc_workspace.make_folder("新文件夹", data_dir=self.data_dir)
        self.assertTrue(result["ok"], result)
        self.assertTrue((self.root / "新文件夹").is_dir())
        again = pc_workspace.make_folder("新文件夹", data_dir=self.data_dir)
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"]["code"], "exists")

    def test_make_folder_illegal_chars_rejected(self):
        bad_names = ("", "   ", ".", "..", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b',
                     "a<b", "a>b", "a|b", "a\tb", "a\x01b", "尾巴.")
        for name in bad_names:
            with self.subTest(name=name):
                result = pc_workspace.make_folder(name, data_dir=self.data_dir)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid")
                self.assertTrue(result["error"]["message"])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         sorted(["笔记.txt", "大文件.bin", "子目录", "空目录"]))

    def test_delete_file(self):
        result = pc_workspace.delete_entry("笔记.txt", data_dir=self.data_dir)
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.root / "笔记.txt").exists())

    def test_delete_traversal_rejected(self):
        result = pc_workspace.delete_entry("../../笔记.txt", data_dir=self.data_dir)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "denied")
        self.assertTrue((self.root / "笔记.txt").exists(), "越界删除不许动到文件")

    def test_delete_root_rejected(self):
        result = pc_workspace.delete_entry("", data_dir=self.data_dir)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "denied")

    def test_delete_non_empty_dir_needs_recursive(self):
        blocked = pc_workspace.delete_entry("子目录", data_dir=self.data_dir)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "not_empty")
        self.assertTrue((self.root / "子目录" / "里面.txt").exists())
        done = pc_workspace.delete_entry("子目录", recursive=True, data_dir=self.data_dir)
        self.assertTrue(done["ok"], done)
        self.assertFalse((self.root / "子目录").exists())


class TestWorkspacePanel(WorkspaceCase):
    def make_panel(self):
        return pc_workspace.WorkspacePanel(data_dir=self.data_dir)

    def test_rows_and_root_label(self):
        panel = self.make_panel()
        self.assertEqual(panel.table.rowCount(), 4)
        self.assertIn(str(self.root), panel.root_label.text())
        self.assertIn("当前位置：" + str(self.root), panel.status.text())
        self.assertEqual(panel.table.columnCount(), 3)
        self.assertEqual([panel.table.horizontalHeaderItem(i).text() for i in range(3)],
                         ["名称", "大小", "修改时间"])

    def test_double_click_enters_subdir_and_goes_up(self):
        panel = self.make_panel()
        index = panel.table.model().index(0, 0)
        panel._on_double_click(index)
        self.assertEqual(panel.relpath, "子目录")
        self.assertEqual(panel.table.rowCount(), 1)
        self.assertIn(str(self.root / "子目录"), panel.status.text())
        panel.go_up()
        self.assertEqual(panel.relpath, "")
        self.assertEqual(panel.table.rowCount(), 4)

    def test_limit_note(self):
        many = self.root / "很多"
        many.mkdir()
        for i in range(pc_workspace.MAX_ENTRIES + 5):
            (many / ("f%03d.txt" % i)).write_text("", encoding="utf-8")
        panel = self.make_panel()
        panel._load("很多")
        self.assertEqual(panel.table.rowCount(), pc_workspace.MAX_ENTRIES)
        self.assertIn("仅显示前 500 项", panel.status.text())


class TestBookmarks(WorkspaceCase):
    def test_defaults_are_three_buttons(self):
        bar = pc_workspace.BookmarkBar(path=self.data_dir / "pc_bookmarks.json")
        self.assertEqual([button.text() for button in bar.buttons], ["B站", "微博", "QQ邮箱"])
        self.assertEqual(bar.add_button.text(), "＋ 添加")
        self.assertEqual([item["url"] for item in bar.items],
                         ["https://www.bilibili.com", "https://weibo.com", "https://mail.qq.com"])
        self.assertIn("https://weibo.com", bar.buttons[1].toolTip())

    def test_save_load_roundtrip_and_remove(self):
        target = self.data_dir / "pc_bookmarks.json"
        bar = pc_workspace.BookmarkBar(path=target)
        bar.items.append({"name": "知乎", "url": "https://www.zhihu.com"})
        bar._persist()
        stored = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(stored), 4)
        again = pc_workspace.BookmarkBar(path=target)
        self.assertEqual([button.text() for button in again.buttons],
                         ["B站", "微博", "QQ邮箱", "知乎"])
        self.assertTrue(again.remove_bookmark(3))
        self.assertEqual([item["name"] for item in pc_workspace.load_bookmarks(target)],
                         ["B站", "微博", "QQ邮箱"])

    def test_broken_file_falls_back_to_defaults(self):
        target = self.data_dir / "pc_bookmarks.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{ 不是 json", encoding="utf-8")
        self.assertEqual([item["name"] for item in pc_workspace.load_bookmarks(target)],
                         ["B站", "微博", "QQ邮箱"])
        bar = pc_workspace.BookmarkBar(path=target)
        self.assertEqual(len(bar.buttons), 3)


class TestWorkbenchPage(unittest.TestCase):
    def test_old_attributes_and_new_panels_exist(self):
        from src.gui import workbench

        tab = workbench.WorkbenchTab()
        try:
            tab._status_timer.stop()
            missing = [name for name in OLD_ATTRS + OLD_METHODS if not hasattr(tab, name)]
            self.assertEqual(missing, [], "旧属性/方法名必须全部还在")
            self.assertTrue(tab.path_edit.isHidden() or not tab.path_edit.isVisible())
            self.assertTrue(tab.url_edit.isHidden() or not tab.url_edit.isVisible())
            self.assertIsInstance(tab.pc_workspace, pc_workspace.WorkspacePanel)
            self.assertIsInstance(tab.bookmark_bar, pc_workspace.BookmarkBar)
            self.assertEqual(len(tab.bookmark_bar.buttons), 3)
            page = tab._pc_page()
            titles = [box.title() for box in page.findChildren(QGroupBox)]
            for title in ("本地工作空间", "常用网址", "输入文字", "快捷键", "鼠标", "动作"):
                self.assertIn(title, titles)
            labels = [label.text() for label in page.findChildren(QLabel)]
            self.assertIn("输入到当前窗口",
                          [button.text() for button in page.findChildren(QPushButton)])
            self.assertTrue(any("执行前请先点一下目标窗口" in text for text in labels))
            self.assertTrue(any("多个键用 + 连接" in text for text in labels))
            self.assertTrue(any("左上角是 0,0" in text for text in labels))
        finally:
            tab.deleteLater()


if __name__ == "__main__":
    unittest.main()
