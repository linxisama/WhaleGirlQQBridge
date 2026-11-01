"""host-one 的原地修改回传契约（真实 node 进程）。

0.4 的 hook 允许直接改 ctx（before-llm-messages 往 messages 末尾加小抄）而不返回值。
宿主是独立进程，旧实现只读返回值，所以这类插件「跑得动但改不了宿主」。本组用例用
仓库自带的 runtime/qqagent-host 和临时扩展证明：原地改过的顶层字段会作为 delta 回到
Python，并沿 manager -> bridge 真正写回会话；完全没改时不会把整个 payload 当结果。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src import extension_host as host
from src.plugins.manager import PluginManager

REPO = Path(__file__).resolve().parents[1]
HOST_ROOT = REPO / 'runtime' / 'qqagent-host'
HOST_SCRIPT = HOST_ROOT / 'host-one.mjs'
NODE = shutil.which('node')
READY = bool(NODE and HOST_SCRIPT.is_file() and (HOST_ROOT / 'src' / 'plugin-loader.js').is_file())

MESSAGE = {'kind': 'group', 'target': '12345', 'sender_id': '999', 'sender_name': 'Alice',
           'text': '最近有什么热梗', 'message_id': 'm1', 'self_id': '10000'}

MUTATING_HOOK = """export const hooks = {
  'before-llm-messages'(ctx) {
    ctx.messages[ctx.messages.length - 1].content += '\\n\\n【hook-delta】测试小抄';
  }
};
"""

NOOP_HOOK = """export const hooks = {
  'before-llm-messages'(ctx) {
    return undefined;
  }
};
"""


class NoteRuntime:
    """manager 只用到 runtime.note。"""

    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(str(text))


@unittest.skipUnless(READY, '需要 node + 仓库自带的 runtime/qqagent-host')
class HostHookDeltaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ext_dir = self.root / 'plugins'
        self.ext_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_extension(self, source: str) -> Path:
        ext = self.ext_dir / 'hook-delta-fixture'
        ext.mkdir()
        manifest = {
            'id': 'hook-delta-fixture',
            'name': 'hook delta fixture',
            'version': '1.0.0',
            'apiVersion': 1,
            'entry': 'index.js',
            'permissions': [],
            'enabledByDefault': True,
            'capabilities': [],
        }
        (ext / 'plugin.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        (ext / 'index.js').write_text(source, encoding='utf-8')
        return ext

    def settings_for(self, ext: Path) -> dict:
        profile = {'extensionPath': str(ext), 'extensionId': 'hook-delta-fixture',
                   'hookName': 'before-llm-messages', 'timeout': 20}
        return {
            'enabled': True, 'hostScript': str(HOST_SCRIPT), 'nodeExe': NODE or 'node',
            'sourceDir': str(HOST_ROOT), 'extensionPath': '', 'catalogPath': '',
            'extensionsDir': str(self.ext_dir), 'autoDiscover': False, 'maxProfiles': 2,
            'provider': '', 'hookName': '', 'event': '', 'ownerOnly': False, 'ownerIds': '',
            'injectPrompt': False, 'timeout': 20, 'preAiBudgetMs': 0, 'toolAllowlist': '',
            'profiles': json.dumps([profile], ensure_ascii=False),
        }

    def run_hook(self, ext: Path, payload=None) -> dict:
        data_dir = self.root / 'hostdata'
        return asyncio.run(host.run_capability(
            node=NODE, host_script=str(HOST_SCRIPT), extension=str(ext), source_dir=str(HOST_ROOT),
            capability='hook', capability_arg='before-llm-messages',
            payload=payload or {'messages': [{'role': 'user', 'content': '最近有什么热梗'}]},
            timeout=20, data_dir=self.root, kind='hook', extension_id='hook-delta-fixture',
            extra_env={'QQ_AGENT_DATA_DIR': str(data_dir)}))

    def test_in_place_hook_rewrite_reaches_real_conversation(self):
        ext = self.write_extension(MUTATING_HOOK)
        manager = PluginManager(self.root)
        manager._settings['qqagent_bridge'] = self.settings_for(ext)
        runtime = NoteRuntime()
        conversation = [{'role': 'system', 'content': 'sys'},
                        {'role': 'user', 'content': '最近有什么热梗'}]

        asyncio.run(manager.run_on_pre_ai_async(dict(MESSAGE), conversation, runtime))

        self.assertEqual(len(conversation), 2)
        self.assertEqual(conversation[-1]['content'], '最近有什么热梗\n\n【hook-delta】测试小抄')
        self.assertFalse([n for n in runtime.notes if 'QQ Agent' in n], runtime.notes)

    def test_mutating_hook_returns_only_changed_fields(self):
        ext = self.write_extension(MUTATING_HOOK)
        result = self.run_hook(ext)
        self.assertTrue(result['ok'], result.get('error'))
        self.assertEqual(result['value'], {
            'messages': [{'role': 'user', 'content': '最近有什么热梗\n\n【hook-delta】测试小抄'}]
        })

    def test_hook_without_changes_returns_none(self):
        ext = self.write_extension(NOOP_HOOK)
        result = self.run_hook(ext)
        self.assertTrue(result['ok'], result.get('error'))
        self.assertIsNone(result['value'])
