"""「关于作者」页（QQ 控制台）的离线回归测试。

用户要求：在控制台新加一页「关于作者」——作者 林夕、头像用他给的图、QQ 2019117802、
交流群「水产养殖交流群」群号 948578157，特别感谢群友、狐栗、稔穗与「你携秋水揽星河」，
作者署名旁写「sana世界第一」，声明「未经作者允许，禁止贩卖、转卖」，并去掉「关于本程序」。

三件容易在打包后才坏的事钉在这里：头像和群二维码必须随包发出去（走 BUNDLE）、
页面上真的显示了这些信息、关于本程序的介绍不再回来。
"""
import os
import unittest
from pathlib import Path

try:
    from PyQt6.QtWidgets import QApplication, QLabel
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def texts_of(widget) -> str:
    return "\n".join(label.text() or "" for label in widget.findChildren(QLabel))


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class AboutPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        import qq_gui
        cls.gui = qq_gui

    def setUp(self):
        self.tab = self.gui.AboutTab()

    def test_page_shows_author_group_thanks_and_notice(self):
        shown = texts_of(self.tab)
        self.assertIn(self.gui.AUTHOR_NAME, shown)
        self.assertIn(self.gui.AUTHOR_QQ, shown)
        self.assertIn(self.gui.AUTHOR_TAGLINE, shown)
        self.assertIn(self.gui.GROUP_NAME, shown)
        self.assertIn(self.gui.GROUP_QQ, shown)
        self.assertIn("水产养殖群聊的所有群友", shown)
        self.assertIn("狐栗", shown)
        self.assertIn("稔穗", shown)
        self.assertIn("你携秋水揽星河", shown)
        self.assertIn("未经作者允许，禁止贩卖、转卖", shown)


    def test_update_card_has_repo_and_one_click_controls(self):
        shown = texts_of(self.tab)
        self.assertIn("自动更新", shown)
        self.assertEqual(self.tab.update_repo.objectName(), "about_update_repo")
        self.assertTrue(self.tab.update_auto.isChecked())
        self.assertTrue(self.tab.btn_update_save.isEnabled())
        self.assertTrue(self.tab.btn_update_check.isEnabled())
        self.assertFalse(self.tab.btn_update_apply.isEnabled(),
                         "没检查到新版本前不该让用户点一键更新")
        self.assertIn(self.gui.APP_VERSION, self.tab.update_state.text())

    def test_wording_is_the_corrected_one(self):
        """用户更正过用词：是「转卖」，不是「专卖」。"""
        self.assertIn("转卖", self.gui.COPYRIGHT_NOTICE)
        self.assertNotIn("专卖", self.gui.COPYRIGHT_NOTICE)

    def test_avatar_is_bundled_and_loads_as_a_circle(self):
        path = self.gui.author_avatar_path()
        self.assertTrue(path.is_file(), f"头像资源缺失：{path}")
        self.assertTrue(str(path).replace("\\", "/").endswith("assets/author/linxi.jpg"))
        pixmap = self.tab.avatar.pixmap()
        self.assertFalse(pixmap.isNull(), "头像没加载出来")
        self.assertEqual((pixmap.width(), pixmap.height()), (92, 92))
        image = pixmap.toImage()
        for x, y in ((0, 0), (image.width() - 1, 0), (0, image.height() - 1),
                     (image.width() - 1, image.height() - 1)):
            self.assertEqual(image.pixelColor(x, y).alpha(), 0, f"({x},{y}) 不是透明的圆角")

    def test_group_qr_is_bundled_and_loads(self):
        path = self.gui.author_group_qr_path()
        self.assertTrue(path.is_file(), f"群二维码资源缺失：{path}")
        self.assertTrue(str(path).replace("\\", "/").endswith("assets/author/group_qr.png"))
        pixmap = self.tab.group_qr.pixmap()
        self.assertFalse(pixmap.isNull(), "群二维码没加载出来")
        self.assertGreater(pixmap.width(), 100)

    def test_program_intro_is_removed(self):
        self.assertNotIn("关于本程序", texts_of(self.tab))

    def test_copy_buttons_put_things_on_the_clipboard(self):
        self.tab._copy_qq()
        self.assertEqual(QApplication.clipboard().text(), self.gui.AUTHOR_QQ)
        self.assertIn(self.gui.AUTHOR_QQ, self.tab.copy_hint.text())
        self.tab._copy_version()
        copied = QApplication.clipboard().text()
        self.assertIn("WhaleGirlQQ", copied)
        self.assertIn(self.gui.AUTHOR_NAME, copied)

    def test_page_does_not_depend_on_online_content(self):
        shown = texts_of(self.tab)
        for forbidden in ("http://", "https://"):
            self.assertNotIn(forbidden, shown)

    def test_mentions_official_group_platform_limits(self):
        """官方 QQ 只推送 @ 机器人的群消息 —— 这件事要在使用须知里说清。"""
        self.assertIn("只推送", texts_of(self.tab))

    def test_missing_avatar_degrades_to_the_name(self):
        tab = self.gui.AboutTab()
        original = self.gui.author_avatar_path
        try:
            self.gui.author_avatar_path = lambda: Path("Z:/definitely/not/here.jpg")
            tab._load_avatar()
        finally:
            self.gui.author_avatar_path = original
        self.assertTrue(tab.avatar.pixmap().isNull() or tab.avatar.text() == self.gui.AUTHOR_NAME)


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class AboutTabIsRegisteredTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        import qq_gui
        cls.gui = qq_gui

    def test_tab_bar_has_the_about_page_last(self):
        window = self.gui.MainWindow()
        try:
            titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
            self.assertIn("关于作者", titles)
            self.assertEqual(titles[-1], "关于作者", "关于页应放在最后一页")
            self.assertIs(window.tabs.widget(titles.index("关于作者")), window.about_tab)
        finally:
            window.close()
            window.deleteLater()
            _APP.processEvents()


if __name__ == "__main__":
    unittest.main()
