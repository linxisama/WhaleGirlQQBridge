import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

# 与其他 GUI 测试文件一致：无 PyQt6 时整体跳过。
# 之前这里是模块级直接 `from PyQt6...`，在无 PyQt6 的环境（.venv-qq）里会让
# 整个 discover 变成 FAILED，而且这 5 个用例根本不会被收集 —— 测试总数会
# 莫名其妙地比另一个环境少，很难看出原因。
try:
    from PyQt6.QtWidgets import QApplication
    import qq_gui as gui

    APP = QApplication.instance() or QApplication([])
    HAS_QT = True
except Exception:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "需要 PyQt6")


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tab = gui.PluginTab()
        self.plugins = [
            {'id':'sleep','name':'睡眠模式','description':'夜间停止回复','category':'rhythm','enabled':True,'settings':{'enabled':True}},
            {'id':'image','name':'图片助手','description':'管理图片','category':'media','enabled':False,'settings':{'enabled':False}},
            {'id':'other','name':'关键词回复','description':'根据关键词回复','category':'utility','enabled':True,'settings':{'enabled':True}},
        ]
        with patch.object(gui,'api_get',return_value={'plugins':copy.deepcopy(self.plugins)}): self.tab.refresh()
    def tearDown(self):
        self.tab.close(); self.tab.deleteLater(); APP.processEvents()
    def visible(self):
        return {k for k,v in self.tab._rows.items() if not v['frame'].isHidden()}
    def test_search_category_and_state_intersection(self):
        self.tab.search.setText('图片')
        self.assertEqual(self.visible(),{'image'})
        self.tab.state_filter.setCurrentIndex(1)
        self.assertEqual(self.visible(),set())
        self.assertFalse(self.tab.empty.isHidden())
        self.tab._clear_filters()
        self.tab.category_filter.setCurrentIndex(self.tab.category_filter.findData('rhythm'))
        self.assertEqual(self.visible(),{'sleep'})
    def test_failed_refresh_preserves_list_and_filter(self):
        self.tab.search.setText('sleep')
        with patch.object(gui,'api_get',return_value={}): self.tab.refresh()
        self.assertEqual(self.visible(),{'sleep'})
        self.assertIn('刷新失败',self.tab.notice.text())
    def test_empty_response_is_valid(self):
        with patch.object(gui,'api_get',return_value={'plugins':[]}): self.tab.refresh()
        self.assertEqual(self.tab._plugins,[])
        self.assertFalse(self.tab.empty.isHidden())
        self.assertIn('共 0 个',self.tab.summary.text())
    def test_toggle_failure_restores_previous_state(self):
        with patch.object(gui,'api_post',return_value={'success':False,'message':'offline'}) as post:
            self.tab._rows['sleep']['toggle'].setChecked(False)
        self.assertEqual(post.call_count,1)
        self.assertTrue(self.tab._rows['sleep']['toggle'].isChecked())
        self.assertTrue(self.tab._rows['sleep']['plugin']['enabled'])
    def test_success_updates_count_and_filtered_visibility(self):
        self.tab.state_filter.setCurrentIndex(2)
        with patch.object(gui,'api_post',return_value={'success':True}):
            self.tab._rows['image']['toggle'].setChecked(True)
        self.assertIn('已启用 3 个',self.tab.summary.text())
        self.assertEqual(self.visible(),set())
        self.assertTrue(self.tab._rows['image']['plugin']['settings']['enabled'])
    def test_refresh_preserves_selected_category(self):
        self.tab.category_filter.setCurrentIndex(self.tab.category_filter.findData('media'))
        with patch.object(gui,'api_get',return_value=copy.deepcopy(self.plugins)):self.tab.refresh()
        self.assertEqual(self.tab.category_filter.currentData(),'media')
        self.assertEqual(self.visible(),{'image'})

if __name__ == '__main__':unittest.main()
