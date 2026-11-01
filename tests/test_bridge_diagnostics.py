"""WP15b：三处诊断缺陷的回归 —— 注释骗排序 / provider 只写不读 / no-json 丢 JS 错误。

缺陷一、二只用临时目录与打桩，不依赖本机是否装了宿主；缺陷三要起真 node
（找不到 node 时跳过，不算失败）。每个用例都钉住"改前必红"的那一点：

1. 注释里写 `import('../../src/config.js')` 不该再拿到排序优先位，真引用仍要命中；
2. `_remember()` 写下的 provider 记录要真的被审计读出来（decision 的 providers
   字段 / 提权 note 的后缀）；
3. 宿主崩掉时 no-json 文案里必须留着原始 JS 异常那一行，且 stderr 为空时文案不变。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import extension_host as host
from src.plugins import qqagent_bridge as bridge

NODE = shutil.which('node')
NEED_NODE = unittest.skipUnless(NODE, '需要 node 才能复现 no-json')

# 只在注释和普通字符串里"引用宿主"：改前 `'../../' in text` 会命中，改后不该命中。
COMMENT_ONLY_SOURCE = (
    "// await import('../../src/config.js') 以前这么引，现在改成包内相对路径了" + chr(10)
    + "/* require('../../src/config.js') 也注释掉了 */" + chr(10)
    + "const LEGACY_NOTE = '../../src/config.js';" + chr(10)
    + 'module.exports = { setup() {} };' + chr(10))

# 真引用宿主源码：动态 import + 静态 from 从句，改后都要命中。
REAL_IMPORT_SOURCE = (
    "const config = await import('../../src/config.js');" + chr(10)
    + "import helper from '../../src/qqagent_host.js';" + chr(10)
    + 'module.exports = { config, helper };' + chr(10))


def write_extension(folder: Path, ident='demo-ext', source=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'plugin.json').write_text(json.dumps({'id': ident, 'name': ident}, ensure_ascii=False),
                                        encoding='utf-8')
    (folder / 'index.js').write_text(source or 'module.exports = {};' + chr(10), encoding='utf-8')
    return folder


class HostImportsTests(unittest.TestCase):
    """缺陷一：host_imports 不能再被注释/普通字符串里的 ../../ 骗到。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.extdir = Path(self.tmp.name) / 'extensions'

    def scan(self, source):
        write_extension(self.extdir / 'demo', 'demo-ext', source)
        items = bridge.scan_extension_dirs(self.extdir)
        self.assertEqual([item['id'] for item in items], ['demo-ext'])
        return items[0]['host_imports']

    def test_comment_and_string_mentions_do_not_earn_priority(self):
        self.assertEqual(self.scan(COMMENT_ONLY_SOURCE), [])

    def test_real_dynamic_import_still_counts(self):
        self.assertEqual(self.scan(REAL_IMPORT_SOURCE), ['../../'])

    def test_fallback_without_mask_still_ignores_comments(self):
        """遮蔽模块不可用时退回"只去注释、字符串保留"，同样不许注释骗到优先位。"""
        with mock.patch.object(bridge, '_schema_module', return_value=None):
            self.assertEqual(self.scan(COMMENT_ONLY_SOURCE), [])
            self.assertEqual(self.scan(REAL_IMPORT_SOURCE), ['../../'])


class ProviderAuditTests(unittest.TestCase):
    """缺陷二：_remember() 写的 provider 记录必须有人读（审计里看得见）。"""

    def setUp(self):
        # 只用 _remember 本身，不跑 __init__（那会去读设置、铺目录）。
        self.plugin = object.__new__(bridge.QqAgentBridge)
        self.message = {'kind': 'group', 'target': '12345', 'sender_id': '999'}

    def remember_gate(self, value=None):
        self.plugin._remember(self.message,
                              {'ok': True, 'value': value or {'block': False}, 'timed_out': False},
                              {'provider': 'message.gate'})
        return self.message

    def test_remember_writes_readable_note(self):
        message = self.remember_gate()
        self.assertEqual(message['_qqagent_provider_note'],
                         'message.gate=ok({"block": false})')

    def test_decision_records_providers_field(self):
        from src.qq_app import QQRuntime  # 放在用例里：桥级用例不依赖整个控制台

        bot = object.__new__(QQRuntime)
        bot.decisions = []
        bot._audit_outcome = lambda message, reason: None
        self.remember_gate()
        QQRuntime.decision(bot, self.message,
                           '被扩展 demo-ext 的 message.gate 挡下，未回复：fixture-block')
        entry = bot.decisions[-1]
        self.assertIn('挡下', entry['reason'])
        self.assertEqual(entry['providers'], 'message.gate=ok({"block": false})')

    def test_privilege_note_carries_provider_digest(self):
        from src.qq_app import QQRuntime

        plugin = self.plugin

        def privilege_allow(message, runtime=None):  # 桥的 privilege_allow 是同步入口
            plugin._remember(message,
                             {'ok': True, 'value': {'allow': True}, 'timed_out': False},
                             {'provider': 'message.privilege-check'})
            return {'allow': True, 'reason': 'fixture-allow', 'extensionId': 'demo-ext'}

        notes = []
        bot = object.__new__(QQRuntime)
        bot._qqagent = lambda: mock.Mock(privilege_allow=privilege_allow)
        bot.account = lambda: '10001'
        bot.note = notes.append
        self.assertTrue(QQRuntime._qqagent_privilege(bot, 'group', '12345', '999', '999'))
        self.assertIn('提成了主人', notes[-1])
        self.assertIn('message.privilege-check=ok({"allow": true})', notes[-1])


class NoJsonStderrTests(unittest.IsolatedAsyncioTestCase):
    """缺陷三：no-json 必须把原始 JS 异常文本带出来，stderr 空时文案不变。"""

    async def run_with_host(self, host_source):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ext = write_extension(root / 'ext', 'probe-ext')
            source_dir = root / 'src'
            source_dir.mkdir()
            script = root / 'host-probe.mjs'
            script.write_text(host_source, encoding='utf-8')
            result = await host.run_capability(node=NODE or 'node', host_script=str(script),
                                               extension=str(ext), source_dir=str(source_dir),
                                               capability='message.gate', payload={},
                                               timeout=30, data_dir=str(root), kind='provider')
            return result, host.read_log(data_dir=str(root))

    @NEED_NODE
    async def test_no_json_keeps_original_js_error_line(self):
        result, rows = await self.run_with_host(
            "throw new Error('boom-from-host 宿主自己崩了');" + chr(10))
        self.assertFalse(result['ok'], result)
        self.assertIn('no-json (rc=1)', result['error'])
        # 改前这里只剩 "m/loader:647:26 + 堆栈尾巴"，Error: 那行被截掉了。
        self.assertIn('Error: boom-from-host 宿主自己崩了', result['error'])
        self.assertIn('Error: boom-from-host 宿主自己崩了', result['stderr_tail'])
        self.assertGreaterEqual(len(result['stderr_tail']), len(result['stderr']))
        self.assertIn('Error: boom-from-host', rows[-1]['stderr_tail'])

    @NEED_NODE
    async def test_no_json_with_empty_stderr_keeps_old_wording(self):
        result, rows = await self.run_with_host('process.exit(3);' + chr(10))
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['error'], 'no-json (rc=3)')
        self.assertEqual(result['stderr'], '')
        self.assertEqual(result['stderr_tail'], '')
        self.assertNotIn('stderr_tail', rows[-1])


if __name__ == '__main__':
    unittest.main()
