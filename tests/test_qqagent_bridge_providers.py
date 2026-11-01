"""WP14：三个「宿主永不调用」的 provider 接上真实消费点后的契约与回归。

message.gate / message.privilege-check / message.identity 以前在本仓库只写不读
（桥的 bridge_status 把它们标成 DEAD_INHOST）。现在它们分别接到 qq_app 的三个
决策点：这轮回不回 / 这人算不算主人 / 机器人是谁。这个文件钉住三件事：

1. 桥关闭、没装扩展、扩展被停用、扩展报错或超时 → 行为与接入前完全一致；
2. 真实 node 派发 → 返回值怎么被消费（方向性是硬指标：gate 与 identity fail-open，
   privilege fail-closed 且默认整条不派发）；
3. qq_app 主链路 → 门禁挡下这一轮真的不回复、审计留痕；提权默认关闭时陌生人
   仍然不是主人。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src import extension_host as host
from src import extension_registry as ext_registry
from src.plugins.manager import PluginManager
from src.plugins.qqagent_bridge import gate_verdict

REPO = Path(__file__).resolve().parents[1]
HOST_SCRIPT = Path('D:/聊天机器人/插件市场/QQAgent兼容层/host-one.mjs')
SOURCE_DIR = REPO / '.tmp' / 'qq-agent-run'
NODE = shutil.which('node')
READY = bool(NODE and HOST_SCRIPT.is_file() and SOURCE_DIR.is_dir())
NEED_NODE = unittest.skipUnless(READY, '需要 node + host-one.mjs + qq-agent-run 运行副本')

MESSAGE = {'kind': 'group', 'target': '12345', 'sender_id': '999', 'text': '你好',
           'message_id': 'm1', 'self_id': '10000'}

GATE_BLOCK = "async () => ({ block: true, reason: 'fixture-gate-block' })"
GATE_SKIP = "async () => ({ skip: true, reason: 'fixture-gate-skip' })"
GATE_BOOM = "async () => { throw new Error('fixture-gate-boom'); }"
GATE_SLOW = ("async () => { await new Promise((r) => setTimeout(r, 30000));"
             " return { block: true, reason: 'too-late' }; }")
IDENTITY = ("async () => ({ selfId: '20002', nickname: '鲸鱼娘', persona: '小鲸',"
            " resolveUserId: 'u-1', listMembers: ['a'] })")
IDENTITY_BOOM = "async () => { throw new Error('fixture-identity-boom'); }"
PRIVILEGE_ALLOW = ("async (ctx) => ({ allow: true, reason: 'fixture-allow', code: 'ok',"
                   " kind: ctx && ctx.kind, chatId: ctx && ctx.chatId })")
PRIVILEGE_DENY = "async () => ({ allow: false, reason: 'fixture-deny' })"
PRIVILEGE_BOOM = "async () => { throw new Error('fixture-privilege-boom'); }"


class NoteRuntime:
    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(str(text))

    def text(self):
        return ' | '.join(self.notes)


def write_extension(base, ident, providers) -> Path:
    """现场写一个只声明指定 provider 的扩展（不联网、不写文件）。

    现成夹具 tests/fixtures/qqagent_bridge_fixture 不含这三个 provider，而 tests/ 下
    的既有文件不能改，所以真实 node 派发用的扩展在临时目录里现造。
    """
    ext = Path(base) / ident
    ext.mkdir(parents=True, exist_ok=True)
    (ext / 'plugin.json').write_text(json.dumps({
        'id': ident, 'name': ident, 'version': '0.0.1', 'apiVersion': 1,
        'category': 'utility', 'entry': 'index.js', 'enabledByDefault': True,
        'description': 'WP14 临时扩展：只回固定值。',
        'capabilities': list(providers), 'requires': [], 'settings': {}, 'tools': [],
    }, ensure_ascii=False), encoding='utf-8')
    body = ',\n'.join("  '%s': %s" % (name, fn) for name, fn in providers.items())
    (ext / 'index.js').write_text('export const providers = {\n%s\n};\n' % body,
                                  encoding='utf-8')
    return ext


class ProviderBase(unittest.TestCase):
    """桥级夹具：真实 PluginManager + 手写 profile（与 tests/test_qqagent_bridge_gate.py 同构）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = self.manager._plugins['qqagent_bridge']
        self.runtime = NoteRuntime()
        self.ext = write_extension(self.root, 'wp14-ext', {})

    def tearDown(self):
        self.tmp.cleanup()

    def profile(self, provider, ident='wp14-ext', ext=None, timeout=5, **over):
        item = {'extensionPath': str(ext or self.ext), 'extensionId': ident,
                'provider': provider, 'timeout': timeout, 'enabled': True}
        item.update(over)
        return item

    def configure(self, profiles, **extra):
        base = {'enabled': True, 'hostScript': str(HOST_SCRIPT), 'nodeExe': NODE or 'node',
                'sourceDir': str(SOURCE_DIR), 'extensionPath': '', 'catalogPath': '',
                'extensionsDir': '', 'autoDiscover': False, 'maxProfiles': 2, 'provider': '',
                'hookName': '', 'event': '', 'ownerOnly': False, 'ownerIds': '',
                'injectPrompt': False, 'timeout': 30, 'preAiBudgetMs': 3000,
                'privilegeEscalation': False, 'toolAllowlist': '',
                'profiles': json.dumps(profiles, ensure_ascii=False)}
        base.update(extra)
        self.manager._settings['qqagent_bridge'] = base
        return self.plugin

    def rows(self):
        return host.read_log(self.root)

    def no_spawn(self):
        return mock.patch('asyncio.create_subprocess_exec',
                          side_effect=AssertionError('这条路径不该起扩展进程'))


class GateTests(ProviderBase):
    def test_verdict_recognises_block_and_market_skip(self):
        self.assertEqual(gate_verdict({'block': True, 'reason': 'x'}), 'x')
        self.assertEqual(gate_verdict({'skip': True}), 'skip')
        self.assertEqual(gate_verdict({'skip': True, 'reason': '  市场原因  '}), '市场原因')
        # 噪声不能把消息挡掉（fail-open 方向）：只认严格的 JSON true。
        for noise in (None, 'block', {'block': 'true'}, {'skip': 'no'}, {'block': 1}, []):
            self.assertIsNone(gate_verdict(noise), noise)

    def test_bridge_disabled_passes_without_spawning(self):
        self.configure([self.profile('message.gate')], enabled=False)
        self.ext = write_extension(self.root, 'wp14-ext', {'message.gate': GATE_BLOCK})
        with self.no_spawn():
            verdict = asyncio.run(self.plugin.message_gate(dict(MESSAGE), self.runtime))
        self.assertEqual(verdict['block'], False)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.plugin._provider_profiles(dict(MESSAGE), 'message.gate'), [])

    def test_no_extension_passes(self):
        self.configure([])
        with self.no_spawn():
            verdict = asyncio.run(self.plugin.message_gate(dict(MESSAGE), self.runtime))
        self.assertEqual(verdict['block'], False)
        self.assertEqual(self.rows(), [])

    def test_zero_budget_passes_without_spawning(self):
        self.configure([self.profile('message.gate')], preAiBudgetMs=0)
        with self.no_spawn():
            verdict = asyncio.run(self.plugin.message_gate(dict(MESSAGE), self.runtime))
        self.assertEqual(verdict['block'], False)
        self.assertEqual(self.rows(), [])

    def test_registered_disabled_extension_is_not_dispatched(self):
        self.configure([self.profile('message.gate')])
        ext_registry.set_enabled('wp14-ext', False, self.root)
        self.assertTrue(self.plugin._registry_disabled('wp14-ext'))
        with self.no_spawn():
            verdict = asyncio.run(self.plugin.message_gate(dict(MESSAGE), self.runtime))
        self.assertEqual(verdict['block'], False)
        self.assertEqual(self.rows(), [])

    @NEED_NODE
    def test_extension_block_stops_the_turn(self):
        self.ext = write_extension(self.root, 'wp14-ext', {'message.gate': GATE_BLOCK})
        self.configure([self.profile('message.gate')])
        verdict = asyncio.run(self.plugin.message_gate(dict(MESSAGE), self.runtime))
        self.assertEqual(verdict['block'], True, verdict)
        self.assertEqual(verdict['reason'], 'fixture-gate-block')
        self.assertEqual(verdict['extensionId'], 'wp14-ext')
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0]['ok'], rows[0])
        self.assertEqual(rows[0]['capability'], 'message.gate')

    @NEED_NODE
    def test_extension_skip_key_also_blocks(self):
        # 市场里的 mention-skip 用的是 skip，不是 block；两个键都要认。
        self.ext = write_extension(self.root, 'wp14-ext', {'message.gate': GATE_SKIP})
        self.configure([self.profile('message.gate')])
        verdict = asyncio.run(self.plugin.message_gate(dict(MESSAGE), self.runtime))
        self.assertEqual(verdict['block'], True, verdict)
        self.assertEqual(verdict['reason'], 'fixture-gate-skip')

    @NEED_NODE
    def test_extension_error_fails_open(self):
        self.ext = write_extension(self.root, 'wp14-ext', {'message.gate': GATE_BOOM})
        self.configure([self.profile('message.gate')])
        verdict = asyncio.run(self.plugin.message_gate(dict(MESSAGE), self.runtime))
        self.assertEqual(verdict['block'], False, verdict)
        # 扩展抛错时 host 拿不到 JSON，桥把错误写进 note 但不拦消息（fail-open）。
        self.assertIn('QQ Agent message.gate 调用失败', self.runtime.text())
        self.assertEqual(len(self.rows()), 1, self.rows())

    @NEED_NODE
    def test_slow_extension_times_out_and_passes(self):
        self.ext = write_extension(self.root, 'wp14-ext', {'message.gate': GATE_SLOW})
        self.configure([self.profile('message.gate', timeout=1)], preAiBudgetMs=1200)
        started = time.monotonic()
        verdict = asyncio.run(self.plugin.message_gate(dict(MESSAGE), self.runtime))
        elapsed = time.monotonic() - started
        self.assertEqual(verdict['block'], False, verdict)
        self.assertLess(elapsed, 6.0, '超时必须被预算/超时掐断，不能拖住回复')
        self.assertEqual(len(self.rows()), 1, self.rows())


class IdentityTests(ProviderBase):
    def test_bridge_disabled_returns_no_override(self):
        self.configure([self.profile('message.identity')], enabled=False)
        with self.no_spawn():
            found = asyncio.run(self.plugin.message_identity(dict(MESSAGE), self.runtime))
        self.assertEqual(found, {})

    def test_zero_budget_returns_no_override(self):
        self.configure([self.profile('message.identity')], preAiBudgetMs=0)
        with self.no_spawn():
            found = asyncio.run(self.plugin.message_identity(dict(MESSAGE), self.runtime))
        self.assertEqual(found, {})

    @NEED_NODE
    def test_identity_takes_only_scalar_keys(self):
        # 市场里的 qq-identity 返回的是函数表；函数/未知键当覆盖值会打出 <function ...>。
        self.ext = write_extension(self.root, 'wp14-ext', {'message.identity': IDENTITY})
        self.configure([self.profile('message.identity')])
        found = asyncio.run(self.plugin.message_identity(dict(MESSAGE), self.runtime))
        self.assertEqual(found, {'selfId': '20002', 'nickname': '鲸鱼娘', 'persona': '小鲸'})

    @NEED_NODE
    def test_identity_error_falls_back(self):
        self.ext = write_extension(self.root, 'wp14-ext', {'message.identity': IDENTITY_BOOM})
        self.configure([self.profile('message.identity')])
        found = asyncio.run(self.plugin.message_identity(dict(MESSAGE), self.runtime))
        self.assertEqual(found, {})
        self.assertIn('QQ Agent message.identity 调用失败', self.runtime.text())


class PrivilegeTests(ProviderBase):
    def test_setting_off_never_dispatches(self):
        """默认关闭：陌生人不会因为装了扩展就被当主人，而且一次扩展都不调。"""
        self.ext = write_extension(self.root, 'wp14-ext',
                                   {'message.privilege-check': PRIVILEGE_ALLOW})
        self.configure([self.profile('message.privilege-check')])
        with self.no_spawn():
            verdict = self.plugin.privilege_allow(dict(MESSAGE), self.runtime)
        self.assertEqual(verdict['allow'], False, verdict)
        self.assertEqual(self.rows(), [])

    def test_zero_budget_denies(self):
        self.configure([self.profile('message.privilege-check')], preAiBudgetMs=0,
                       privilegeEscalation=True)
        with self.no_spawn():
            verdict = self.plugin.privilege_allow(dict(MESSAGE), self.runtime)
        self.assertEqual(verdict['allow'], False, verdict)

    @NEED_NODE
    def test_setting_on_allows_escalation(self):
        self.ext = write_extension(self.root, 'wp14-ext',
                                   {'message.privilege-check': PRIVILEGE_ALLOW})
        self.configure([self.profile('message.privilege-check')], privilegeEscalation=True)
        verdict = self.plugin.privilege_allow(dict(MESSAGE), self.runtime)
        self.assertEqual(verdict['allow'], True, verdict)
        self.assertEqual(verdict['reason'], 'fixture-allow')
        self.assertEqual(verdict['extensionId'], 'wp14-ext')
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['capability'], 'message.privilege-check')

    @NEED_NODE
    def test_setting_on_but_extension_error_denies(self):
        self.ext = write_extension(self.root, 'wp14-ext',
                                   {'message.privilege-check': PRIVILEGE_BOOM})
        self.configure([self.profile('message.privilege-check')], privilegeEscalation=True)
        verdict = self.plugin.privilege_allow(dict(MESSAGE), self.runtime)
        self.assertEqual(verdict['allow'], False, verdict)
        self.assertIn('QQ Agent message.privilege-check 调用失败', self.runtime.text())

    @NEED_NODE
    def test_extension_saying_false_denies(self):
        self.ext = write_extension(self.root, 'wp14-ext',
                                   {'message.privilege-check': PRIVILEGE_DENY})
        self.configure([self.profile('message.privilege-check')], privilegeEscalation=True)
        verdict = self.plugin.privilege_allow(dict(MESSAGE), self.runtime)
        self.assertEqual(verdict['allow'], False, verdict)


# ── qq_app 主链路：三个消费点真的接在决策位置上 ──────────────────────────────
from src.qq_app import QQRuntime  # noqa: E402  （放在文件后半段：桥级用例不依赖整个控制台）
from src.qq_transports import onebot_message  # noqa: E402

BRIDGE_KEYS = ('enabled', 'hostScript', 'nodeExe', 'sourceDir', 'extensionPath', 'catalogPath',
               'extensionsDir', 'autoDiscover', 'maxProfiles', 'provider', 'hookName', 'event',
               'ownerOnly', 'ownerIds', 'injectPrompt', 'timeout', 'preAiBudgetMs',
               'privilegeEscalation', 'toolAllowlist', 'profiles')


class RuntimeBase(unittest.IsolatedAsyncioTestCase):
    ACTIVE = ('qqagent_bridge',)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'config.qq.yaml').write_text(
            'ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: stub\n'
            'qq:\n  app_id: fake\n  client_secret: fake\n', encoding='utf-8')
        self.bot = QQRuntime(root=self.root)
        for pid in self.bot.plugins._settings:
            self.bot.plugins._settings[pid]['enabled'] = pid in self.ACTIVE
        self.bridge = self.bot.plugins._plugins['qqagent_bridge']
        self.sent = []
        self.conversations = []
        self.systems = []

        async def generate(messages, **kwargs):
            self.conversations.append(json.loads(json.dumps(messages)))
            # 人设/身份约束走 system_prompt 参数，不在 messages 里（见 qq_app._work 的 ai.chat 调用）
            self.systems.append(str(kwargs.get('system_prompt') or ''))
            return '这是完整回答。'

        async def send(message, text, meta=None):
            self.sent.append((dict(message), text, meta))
            return {'id': 'out%d' % len(self.sent)}

        self.bot.ai.chat = mock.AsyncMock(side_effect=generate)
        self.bot.official.send = mock.AsyncMock(side_effect=send)
        self.bot.onebot.send = mock.AsyncMock(side_effect=send)
        self.bot.onebot.online = True
        self.bot.onebot.self_id = '10001'
        self.bot.config.set('qq_only.mode', 'onebot')
        self.worker = None

    async def asyncTearDown(self):
        self.bot.running = False
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.bot.close()
        self.tmp.cleanup()

    def configure_bridge(self, profiles, **extra):
        settings = {key: self.bot.plugins._settings['qqagent_bridge'].get(key)
                    for key in BRIDGE_KEYS}
        settings.update({'enabled': True, 'hostScript': str(HOST_SCRIPT),
                         'nodeExe': NODE or 'node', 'sourceDir': str(SOURCE_DIR),
                         'ownerOnly': False, 'preAiBudgetMs': 3000, 'timeout': 30,
                         'privilegeEscalation': False,
                         'profiles': json.dumps(profiles, ensure_ascii=False)})
        settings.update(extra)
        self.bot.plugins._settings['qqagent_bridge'] = settings
        return self.bridge

    def bridge_profile(self, provider, ext, timeout=5):
        return {'extensionPath': str(ext), 'extensionId': 'wp14-ext',
                'provider': provider, 'timeout': timeout, 'enabled': True}

    def private(self, mid, text='你好'):
        return onebot_message({'post_type': 'message', 'message_type': 'private',
                               'message_id': mid, 'user_id': 30003,
                               'message': [{'type': 'text', 'data': {'text': text}}]}, '10001')

    async def settle(self, timeout=10, states=('sent', 'partial', 'blocked', 'error')):
        """等主链路把这一条定型；返回 (state, inbox 行)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.bot.db.execute('SELECT state,error FROM inbox').fetchall()
            if rows and all(str(row[0]) in states for row in rows):
                return str(rows[-1][0]), rows
            await asyncio.sleep(0.02)
        self.fail('主链路未在超时内定型：'
                  + repr(self.bot.db.execute('SELECT state,error FROM inbox').fetchall())
                  + ' events=' + repr(self.bot.events[-6:]))

    def start(self):
        self.bot.running = True
        self.worker = asyncio.create_task(self.bot._work())

    def prompt_text(self):
        return '\n'.join(self.systems + [str(turn.get('content') or '')
                                          for convo in self.conversations for turn in convo
                                          if isinstance(turn, dict)])

    def decision_text(self):
        return ' | '.join(str(item.get('reason') or '') for item in self.bot.decisions)


class GateInRuntimeTests(RuntimeBase):
    async def test_reply_unchanged_when_bridge_is_off(self):
        """桥关闭（默认 enabled=False）：这条消息照旧得到回复，一次扩展都不起。"""
        self.bot.plugins._settings['qqagent_bridge']['enabled'] = False
        with mock.patch('asyncio.create_subprocess_exec',
                        side_effect=AssertionError('桥关闭时不该起扩展进程')):
            self.start()
            self.bot.ingest(self.private('wp14-off'), self.bot.mode)
            state, _ = await self.settle()
        self.assertEqual(state, 'sent')
        self.assertEqual(len(self.sent), 1)
        self.assertIn('完整回答', self.sent[0][1])
        self.assertEqual([n for n in self.bot.events if 'QQ Agent' in str(n.get('text'))], [])

    @NEED_NODE
    async def test_real_extension_gate_block_stops_the_reply(self):
        ext = write_extension(self.root, 'wp14-ext', {'message.gate': GATE_BLOCK})
        self.configure_bridge([self.bridge_profile('message.gate', ext)])
        self.start()
        self.bot.ingest(self.private('wp14-blocked'), self.bot.mode)
        state, _ = await self.settle()
        self.assertEqual(state, 'blocked')
        self.assertEqual(self.sent, [], '被门禁挡下的一轮不得发任何消息')
        self.bot.ai.chat.assert_not_awaited()
        reason = self.decision_text()
        self.assertIn('fixture-gate-block', reason, self.bot.decisions)
        self.assertIn('wp14-ext', reason, self.bot.decisions)

    async def test_gate_wrapper_exception_fails_open(self):
        boom = mock.Mock()
        boom.message_gate = mock.AsyncMock(side_effect=RuntimeError('wp14-gate-boom'))
        with mock.patch.object(self.bot, '_qqagent', lambda: boom):
            self.start()
            self.bot.ingest(self.private('wp14-gate-boom'), self.bot.mode)
            state, _ = await self.settle()
        self.assertEqual(state, 'sent')
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(any('QQ Agent message.gate 跳过' in str(item.get('text'))
                            for item in self.bot.events), self.bot.events[-6:])


class IdentityInRuntimeTests(RuntimeBase):
    @NEED_NODE
    async def test_real_extension_identity_override_reaches_prompt(self):
        ext = write_extension(self.root, 'wp14-ext', {'message.identity': IDENTITY})
        self.configure_bridge([self.bridge_profile('message.identity', ext)])
        self.start()
        self.bot.ingest(self.private('wp14-identity'), self.bot.mode)
        state, _ = await self.settle()
        self.assertEqual(state, 'sent')
        prompt = self.prompt_text()
        self.assertIn('群友平时叫你「鲸鱼娘」', prompt)

    async def test_identity_wrapper_exception_keeps_original_identity(self):
        boom = mock.Mock()
        boom.message_identity = mock.AsyncMock(side_effect=RuntimeError('wp14-id-boom'))
        with mock.patch.object(self.bot, '_qqagent', lambda: boom):
            self.start()
            self.bot.ingest(self.private('wp14-id-boom'), self.bot.mode)
            state, _ = await self.settle()
        self.assertEqual(state, 'sent')
        self.assertNotIn('群友平时叫你', self.prompt_text())


class PrivilegeInRuntimeTests(RuntimeBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # 严格私聊白名单 + 已绑定主人：陌生人本来进不来，提权才有可观测的差别。
        self.bot.config.set('qq_only.private_whitelist_strict', True)
        (self.root / 'agent_admin.json').write_text(
            json.dumps({'admin': '9001'}), encoding='utf-8')
        self.assertEqual(self.bot.owner_ids(), {'9001'})

    @NEED_NODE
    async def test_default_off_keeps_stranger_outside_owners(self):
        ext = write_extension(self.root, 'wp14-ext',
                              {'message.privilege-check': PRIVILEGE_ALLOW})
        self.configure_bridge([self.bridge_profile('message.privilege-check', ext)])
        with mock.patch('asyncio.create_subprocess_exec',
                        side_effect=AssertionError('提权设置默认关闭时不该起扩展进程')):
            allowed = self.bot.conversation_allowed('private', '30003', '30003')
        self.assertFalse(allowed, '扩展说是主人也不能凭空造出主人权限（默认关闭）')
        self.assertEqual(host.read_log(self.root), [])

    @NEED_NODE
    async def test_setting_on_lets_extension_promote(self):
        ext = write_extension(self.root, 'wp14-ext',
                              {'message.privilege-check': PRIVILEGE_ALLOW})
        self.configure_bridge([self.bridge_profile('message.privilege-check', ext)],
                              privilegeEscalation=True)
        allowed = self.bot.conversation_allowed('private', '30003', '30003')
        self.assertTrue(allowed, '显式打开提权后扩展的 allow=true 才生效')
        self.assertTrue(any('提成了主人' in str(item.get('text')) for item in self.bot.events),
                        self.bot.events[-6:])

    async def test_setting_on_but_extension_error_stays_denied(self):
        ext = write_extension(self.root, 'wp14-ext',
                              {'message.privilege-check': PRIVILEGE_BOOM})
        self.configure_bridge([self.bridge_profile('message.privilege-check', ext)],
                              privilegeEscalation=True)
        allowed = self.bot.conversation_allowed('private', '30003', '30003')
        self.assertFalse(allowed, '扩展报错时保持原判定（fail-closed）')

    async def test_privilege_wrapper_exception_stays_denied(self):
        boom = mock.Mock()
        boom.privilege_allow = mock.Mock(side_effect=RuntimeError('wp14-priv-boom'))
        with mock.patch.object(self.bot, '_qqagent', lambda: boom):
            allowed = self.bot.conversation_allowed('private', '30003', '30003')
        self.assertFalse(allowed)

    @NEED_NODE
    async def test_owner_verdict_is_never_downgraded(self):
        ext = write_extension(self.root, 'wp14-ext',
                              {'message.privilege-check': PRIVILEGE_DENY})
        self.configure_bridge([self.bridge_profile('message.privilege-check', ext)],
                              privilegeEscalation=True)
        with mock.patch('asyncio.create_subprocess_exec',
                        side_effect=AssertionError('主人判定不需要问扩展')):
            allowed = self.bot.conversation_allowed('private', '9001', '9001')
        self.assertTrue(allowed, '既有主人判定保持原样（扩展只用于提权这一个方向）')


if __name__ == '__main__':
    unittest.main()
