"""工具前后钩子：插件可拦截、改写参数、改写结果；异常不影响其它插件。"""
import asyncio
import json
import pathlib
import tempfile
import unittest

from src.plugins.manager import PluginManager

PLUGIN = '''
from . import PluginBase, PluginMeta
class ToolHooks(PluginBase):
    META = PluginMeta(id="tool_hooks", name="工具钩子", category="utility", description="x")
    def on_before_tool(self, tool, arguments, runtime):
        if tool == 'blocked': return {'block': True, 'reason': 'no'}
        arguments['x'] = 2
        return {'arguments': arguments}
    def on_after_tool(self, tool, arguments, result, runtime):
        return {'y': 3}
'''


class ToolHookTests(unittest.TestCase):
    def test_before_after(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder); plugins = root / 'custom_plugins'; plugins.mkdir()
            (plugins / 'tool_hooks.py').write_text(PLUGIN, encoding='utf-8')
            (root / 'custom_plugins.json').write_text(json.dumps({'plugins': ['tool_hooks']}), encoding='utf-8')
            manager = PluginManager(root)
            before = asyncio.run(manager.run_on_before_tool('t', {'x': 1}, None))
            self.assertFalse(before['block'])
            self.assertEqual(before['arguments']['x'], 2)
            blocked = asyncio.run(manager.run_on_before_tool('blocked', {}, None))
            self.assertTrue(blocked['block'])
            after = asyncio.run(manager.run_on_after_tool('t', {'x': 2}, {'z': 1}, None))
            self.assertEqual(after, {'y': 3})


if __name__ == '__main__':
    unittest.main()
