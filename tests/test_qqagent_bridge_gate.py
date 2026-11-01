"""WP1d：停用一个扩展，工具面与钩子面必须行为一致。

三条用例分别钉住：已登记且停用 → 不派发 / 不起进程 / 不写账本；未登记的手写 profile
→ 照旧派发（防止"升级后手写配置静默失效"回归）；已登记且启用 → 照常派发。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import extension_host as host
from src import extension_registry as ext_registry
from src.plugins.manager import PluginManager

REPO = Path(__file__).resolve().parents[1]
HOST_SCRIPT = Path('D:/聊天机器人/插件市场/QQAgent兼容层/host-one.mjs')
SOURCE_DIR = REPO / '.tmp' / 'qq-agent-run'
FIXTURE = REPO / 'tests' / 'fixtures' / 'qqagent_bridge_fixture'
NODE = shutil.which('node')
READY = bool(NODE and HOST_SCRIPT.is_file() and SOURCE_DIR.is_dir() and FIXTURE.is_dir())

IDENT = 'qqagent_bridge_fixture'
MESSAGE = {'kind': 'group', 'target': '12345', 'sender_id': '999', 'text': '你好',
           'message_id': 'm1', 'self_id': '10000'}


class NoteRuntime:
    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(str(text))


def hook_profile(**over):
    item = {'extensionPath': str(FIXTURE), 'extensionId': IDENT, 'hookName': 'onMessage',
            'timeout': 30, 'enabled': True}
    item.update(over)
    return item


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = self.manager._plugins['qqagent_bridge']
        self.runtime = NoteRuntime()
        self.configure([hook_profile()])

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, profiles, **extra):
        base = {'enabled': True, 'hostScript': str(HOST_SCRIPT), 'nodeExe': NODE or 'node',
                'sourceDir': str(SOURCE_DIR), 'extensionPath': '', 'catalogPath': '',
                'extensionsDir': '', 'autoDiscover': False, 'maxProfiles': 2, 'provider': '',
                'hookName': '', 'event': '', 'ownerOnly': False, 'ownerIds': '',
                'injectPrompt': False, 'timeout': 30, 'preAiBudgetMs': 0, 'toolAllowlist': '',
                'profiles': json.dumps(profiles, ensure_ascii=False)}
        base.update(extra)
        self.manager._settings['qqagent_bridge'] = base
        return self.plugin

    def rows(self):
        return host.read_log(self.root)

    def dispatch(self):
        asyncio.run(self.plugin.on_prepare(dict(MESSAGE), self.runtime))


class DisabledExtensionTests(GateBase):
    def test_registered_disabled_hook_is_not_dispatched(self):
        ext_registry.set_enabled(IDENT, False, self.root)
        self.assertTrue(ext_registry.is_known(IDENT, self.root))
        self.assertFalse(ext_registry.is_enabled(IDENT, self.root))
        self.assertTrue(self.plugin._registry_disabled(IDENT))
        self.assertEqual(self.plugin._event_profiles(dict(MESSAGE), 'on_prepare'), [])

        before = len(self.rows())
        with mock.patch('asyncio.create_subprocess_exec',
                        side_effect=AssertionError('停用的扩展不得起进程')) as spy:
            self.dispatch()
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(len(self.rows()), before)
        self.assertFalse([n for n in self.runtime.notes if 'QQ Agent' in n], self.runtime.notes)


class UnregisteredExtensionTests(GateBase):
    def test_unregistered_manual_profile_still_dispatches(self):
        self.configure([hook_profile(extensionId='wp1d-handmade')])
        self.assertFalse(ext_registry.is_known('wp1d-handmade', self.root))
        self.assertFalse(self.plugin._registry_disabled('wp1d-handmade'))
        chosen = self.plugin._event_profiles(dict(MESSAGE), 'on_prepare')
        self.assertEqual([item['extensionId'] for item in chosen], ['wp1d-handmade'])

    @unittest.skipUnless(READY, '需要 node + host-one.mjs + qq-agent-run 运行副本')
    def test_unregistered_manual_profile_really_runs(self):
        self.configure([hook_profile(extensionId='wp1d-handmade')])
        before = len(self.rows())
        self.dispatch()
        rows = self.rows()
        self.assertEqual(len(rows), before + 1, rows[-1:])
        self.assertTrue(rows[-1]['ok'], rows[-1])
        self.assertEqual(rows[-1]['extension'], 'wp1d-handmade')


class EnabledExtensionTests(GateBase):
    def test_registered_enabled_hook_is_dispatched(self):
        ext_registry.set_enabled(IDENT, True, self.root)
        self.assertFalse(self.plugin._registry_disabled(IDENT))
        self.assertEqual(len(self.plugin._event_profiles(dict(MESSAGE), 'on_prepare')), 1)

    @unittest.skipUnless(READY, '需要 node + host-one.mjs + qq-agent-run 运行副本')
    def test_registered_enabled_hook_really_runs(self):
        ext_registry.set_enabled(IDENT, True, self.root)
        before = len(self.rows())
        self.dispatch()
        rows = self.rows()
        self.assertEqual(len(rows), before + 1, rows[-1:])
        self.assertTrue(rows[-1]['ok'], rows[-1])
        self.assertEqual(rows[-1]['extension'], IDENT)
        self.assertEqual(rows[-1]['path'], 'async')


class ParityTests(GateBase):
    """停用后，工具面与钩子面用同一套判断，不出现"一边禁一边放"。"""

    def test_tool_and_hook_paths_agree_when_disabled(self):
        ext_registry.set_enabled(IDENT, False, self.root)
        self.assertEqual(self.plugin.list_tools(), [])
        self.assertEqual(self.plugin._event_profiles(dict(MESSAGE), 'on_prepare'), [])
        self.assertTrue(self.plugin._registry_disabled(IDENT))


if __name__ == '__main__':
    unittest.main()
