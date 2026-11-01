"""单个坏插件不能拖垮启动：导入/构造/post_load/超时都跳过，其它插件照常加载。"""
import json
import pathlib
import tempfile
import unittest

from src.plugins.manager import PluginManager

GOOD = '''
from . import PluginBase, PluginMeta
class Good(PluginBase):
    META = PluginMeta(id="good_iso", name="好插件", category="utility", description="x")
    def on_prepare(self, message, runtime): return None
'''
BAD_IMPORT = 'raise RuntimeError("import boom")\n'
BAD_INIT = '''
from . import PluginBase, PluginMeta
class Bad(PluginBase):
    META = PluginMeta(id="bad_init", name="坏构造", category="utility", description="x")
    def __init__(self, manager): raise RuntimeError("init boom")
'''
BAD_POST = '''
from . import PluginBase, PluginMeta
class Bad(PluginBase):
    META = PluginMeta(id="bad_post", name="坏post", category="utility", description="x")
    def post_load(self): raise RuntimeError("post boom")
'''
HANG = '''
import time
time.sleep(30)
from . import PluginBase, PluginMeta
class Hang(PluginBase):
    META = PluginMeta(id="hang_iso", name="挂起", category="utility")
'''


class PluginIsolationTests(unittest.TestCase):
    def test_bad_plugins_are_skipped_and_good_plugin_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            plugins = root / 'custom_plugins'
            plugins.mkdir()
            for name, code in (('good_iso', GOOD), ('bad_import', BAD_IMPORT),
                               ('bad_init', BAD_INIT), ('bad_post', BAD_POST)):
                (plugins / f'{name}.py').write_text(code, encoding='utf-8')
            (root / 'custom_plugins.json').write_text(json.dumps({'plugins': [
                'good_iso', 'bad_import', 'bad_init', 'bad_post']}), encoding='utf-8')
            manager = PluginManager(root)
            self.assertIn('good_iso', manager._plugins)
            for bad in ('bad_import', 'bad_init', 'bad_post'):
                self.assertNotIn(bad, manager._plugins)
            problems = manager.take_load_problems()
            joined = '\n'.join(problems)
            self.assertIn('bad_import', joined)
            self.assertIn('bad_init', joined)
            self.assertIn('bad_post', joined)
            self.assertNotIn('good_iso', joined)

    def test_hung_import_is_skipped_by_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            plugins = root / 'custom_plugins'; plugins.mkdir()
            (plugins / 'hang_iso.py').write_text(HANG, encoding='utf-8')
            (root / 'custom_plugins.json').write_text(json.dumps({'plugins': ['hang_iso']}), encoding='utf-8')
            manager = PluginManager(root)
            self.assertNotIn('hang_iso', manager._plugins)
            self.assertIn('hang_iso', '\n'.join(manager.take_load_problems()))


if __name__ == '__main__':
    unittest.main()
