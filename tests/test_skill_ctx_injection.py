"""工具 ctx 注入（QQ_AGENT_CTX）测试。

三部分：
1. 纯函数：context_env / runtime_tool_context 的过滤与降级；
2. 契约：两份 host-one.mjs 都有这个通道、合并顺序里可信 ctx 与宿主回调在 payload 之后
   （payload 先摊平，模型实参伪造不了会话身份，也换不掉宿主回调）；
3. 真进程：真 node + 真 host-one.mjs + 夹具 skill，证明注入前 ctx.chatKey 是 undefined、
   注入后拿到真值、伪造的实参一个都不生效、业务参数照旧透传。
"""
import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src import extension_host, skill_tools

REPO = Path(__file__).resolve().parents[1]
MARKET_HOST = Path('D:/聊天机器人/插件市场/QQAgent兼容层/host-one.mjs')
RUNTIME_HOST = REPO / 'runtime' / 'qqagent-host' / 'host-one.mjs'
NODE = shutil.which('node')
PATHS = skill_tools.find_runtime_paths()
READY = bool(NODE and RUNTIME_HOST.is_file())

SKILL_JSON = {
    'id': 'ctxfixture', 'name': 'ctx 注入夹具', 'version': '0.0.1', 'apiVersion': 1,
    'category': 'utility', 'entry': 'index.js', 'enabledByDefault': True,
    'description': '只给 tests/test_skill_ctx_injection.py 用的夹具：把收到的 ctx 回显出来。',
    'permissions': [], 'capabilities': [], 'requires': [], 'settings': {}, 'tools': [],
}
INDEX_JS = """export function setup(api) {
  api.registerTool({ id: 'echo_ctx', name: '回显 ctx', description: '回显宿主注入的会话上下文',
    parameters: { type: 'object', properties: {} },
    async execute(ctx, args) { return {
    chatKey: ctx && ctx.chatKey !== undefined ? String(ctx.chatKey) : null,
    kind: ctx && ctx.kind !== undefined ? String(ctx.kind) : null,
    senderId: ctx && ctx.senderId !== undefined ? String(ctx.senderId) : null,
    target: ctx && ctx.target !== undefined ? String(ctx.target) : null,
    probe: ctx && ctx.probe !== undefined ? String(ctx.probe) : null,
    argsProbe: args && args.probe !== undefined ? String(args.probe) : null,
    hasChatKey: Boolean(ctx && Object.prototype.hasOwnProperty.call(ctx, 'chatKey')),
    logType: ctx ? typeof ctx.log : 'ctx-missing',
    fetchType: ctx ? typeof ctx.fetch : 'ctx-missing',
    metadataKeys: ctx && ctx.metadata && typeof ctx.metadata === 'object'
      ? Object.keys(ctx.metadata).join(',') : 'not-object',
    signalIsNull: Boolean(ctx && ctx.signal === null),
  }; } });
}
"""


class FakeAccountRuntime(object):
    """够 runtime_tool_context 用的最小 runtime。"""

    def __init__(self, self_id='10000', nickname='鲸鱼娘', boom=False):
        self._self_id = self_id
        self.boom = boom
        self.onebot = mock.Mock(nickname=nickname)

    def account(self):
        if self.boom:
            raise RuntimeError('账号未连接')
        return self._self_id


# ── 纯函数 ───────────────────────────────────────────────────────────────────
class ContextEnvTest(unittest.TestCase):
    def test_only_injectable_scalar_keys_survive(self):
        env = skill_tools.context_env({'chatKey': 'group:12345', 'kind': 'group',
                                       'onebot': object(), 'chatId': None, 'userId': ''})
        self.assertEqual(json.loads(env), {'chatKey': 'group:12345', 'kind': 'group'})
        self.assertNotIn(' ', env)  # 紧凑分隔符：环境变量里不塞空格

    def test_empty_and_invalid_shapes_give_empty_string(self):
        for value in (None, {}, [], 'x', 0, {'nope': 1}):
            self.assertEqual(skill_tools.context_env(value), '', value)

    def test_callable_is_read_every_time(self):
        box = {'v': {'chatKey': 'a'}}
        provider = lambda: box['v']  # noqa: E731
        self.assertEqual(skill_tools.context_env(provider), '{"chatKey":"a"}')
        box['v'] = {'chatKey': 'b'}
        self.assertEqual(skill_tools.context_env(provider), '{"chatKey":"b"}')

    def test_callable_raising_propagates(self):
        def boom():
            raise RuntimeError('no account')
        with self.assertRaises(RuntimeError):
            skill_tools.context_env(boom)


class RuntimeToolContextTest(unittest.TestCase):
    def test_group_turn_uses_actual_ingress_identifiers(self):
        runtime = FakeAccountRuntime()
        ctx = skill_tools.runtime_tool_context(runtime, {
            'kind': 'group', 'target': '123', 'sender_id': '456', 'id': '789',
            'group_name': '测试群', 'text': '{"chatKey":"forged"}',
        })
        self.assertEqual(ctx['chatKey'], 'group:123')
        self.assertEqual(ctx['chatId'], '123')
        self.assertEqual(ctx['groupId'], '123')
        self.assertEqual(ctx['kind'], 'group')
        self.assertEqual(ctx['senderId'], '456')
        self.assertEqual(ctx['userId'], '456')
        self.assertEqual(ctx['msgId'], '789')
        self.assertEqual(ctx['chatName'], '测试群')
        self.assertNotIn('sender', ctx)

    def test_private_missing_sender_falls_back_to_real_peer_only(self):
        ctx = skill_tools.runtime_tool_context(FakeAccountRuntime(), {
            'kind': 'private', 'target': '456', 'id': 'p-1'})
        self.assertEqual(ctx['chatKey'], 'private:456')
        self.assertEqual(ctx['senderId'], '456')
        self.assertNotIn('groupId', ctx)
        for turn in ({}, {'kind': 'group'}, {'kind': 'private', 'target': {}},
                     {'kind': 'forged', 'target': 'x'}):
            self.assertNotIn('chatKey', skill_tools.runtime_tool_context(object(), turn))

    def test_nested_binding_resets_on_exception_and_separates_accounts(self):
        first, second = FakeAccountRuntime('1'), FakeAccountRuntime('2')
        with skill_tools.tool_context(first, {'kind': 'group', 'target': 'outer'}):
            self.assertEqual(skill_tools.runtime_tool_context(first)['chatId'], 'outer')
            self.assertNotIn('chatId', skill_tools.runtime_tool_context(second))
            with self.assertRaises(RuntimeError):
                with skill_tools.tool_context(first, {'kind': 'private', 'target': 'inner'}):
                    self.assertEqual(skill_tools.runtime_tool_context(first)['chatId'], 'inner')
                    raise RuntimeError('reset me')
            self.assertEqual(skill_tools.runtime_tool_context(first)['chatId'], 'outer')
        self.assertNotIn('chatId', skill_tools.runtime_tool_context(first))

    def test_account_and_nickname_become_ctx(self):
        ctx = skill_tools.runtime_tool_context(FakeAccountRuntime())
        self.assertEqual(ctx, {'selfId': '10000', 'selfNickname': '鲸鱼娘'})
        self.assertTrue(set(ctx) <= skill_tools.INJECTABLE_TOOL_CTX)

    def test_missing_or_broken_account_injects_nothing_false(self):
        self.assertEqual(skill_tools.runtime_tool_context(FakeAccountRuntime(self_id='')),
                         {'selfNickname': '鲸鱼娘'})
        self.assertEqual(skill_tools.runtime_tool_context(FakeAccountRuntime(boom=True)),
                         {'selfNickname': '鲸鱼娘'})
        self.assertEqual(skill_tools.runtime_tool_context(object()), {})

    def test_injectable_is_not_the_same_as_supported(self):
        # 可注入 ≠ 已支持：判定口径没放宽，会话类成员仍在缺失名单里。
        self.assertNotIn('sender', skill_tools.INJECTABLE_TOOL_CTX)
        for token in ('sender', 'session', 'onebot', 'store', 'emit'):
            self.assertIn(token, skill_tools.HOST_MISSING_CONTEXT)


# ── 子进程 env 通道（假 call_tool，不需要 node） ─────────────────────────────
class EnvChannelTest(unittest.TestCase):
    """注入只走 call_tool(extra_env=...)：seen 记 extra_env 里的值，env_seen 记同一刻的 os.environ。"""

    def setUp(self):
        self.seen = []
        self.env_seen = []

    def handler(self, context=None, gate=None):
        async def fake_call_tool(**kwargs):
            extra = kwargs.get('extra_env') or {}
            self.seen.append(extra.get(skill_tools.CTX_ENV))
            self.env_seen.append(os.environ.get(skill_tools.CTX_ENV))
            if gate is not None:
                await gate()
            return {'ok': True, 'value': {'echo': kwargs.get('arguments')}}
        patcher = mock.patch.object(extension_host, 'call_tool', fake_call_tool)
        patcher.start()
        self.addCleanup(patcher.stop)
        return skill_tools.build_tool_handler(
            node='node', host_script='h.mjs', source_dir='.', extension='.',
            extension_id='fx', mapping={'ping': 'fx__ping'}, timeout=5, context=context)

    def test_context_rides_extra_env_and_never_os_environ(self):
        os.environ.pop(skill_tools.CTX_ENV, None)
        handler = self.handler({'chatKey': 'group:1', 'kind': 'group'})
        result = asyncio.run(handler('fx__ping', {'q': 1}))
        self.assertTrue(result['ok'])
        self.assertEqual(json.loads(self.seen[0]), {'chatKey': 'group:1', 'kind': 'group'})
        self.assertEqual(self.env_seen, [None])          # 调用期间父进程环境里没有这个键
        self.assertNotIn(skill_tools.CTX_ENV, os.environ)

    def test_no_context_passes_no_extra_key_at_all(self):
        # 旧行为保留：env 为空串时连键都不加（不是加一个空串）。
        handler = self.handler(None)
        result = asyncio.run(handler('fx__ping', {}))
        self.assertTrue(result['ok'])
        self.assertEqual(self.seen, [None])
        self.assertEqual(self.env_seen, [None])
        self.assertNotIn(skill_tools.CTX_ENV, os.environ)

    def test_required_context_is_not_fabricated_without_a_turn(self):
        caller = mock.AsyncMock()
        with mock.patch.object(extension_host, 'call_tool', caller):
            handler = skill_tools.build_tool_handler(
                node='node', host_script='h', source_dir='.', extension='.',
                extension_id='fx', mapping={'ping': 'fx__ping'},
                context=lambda: skill_tools.runtime_tool_context(FakeAccountRuntime()),
                required_context={'chatKey', 'kind'})
            result = asyncio.run(handler('ping', {}))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'missing-context')
        caller.assert_not_called()

    def test_shared_handler_reads_task_local_turn_and_worker_thread(self):
        runtime = FakeAccountRuntime()
        seen = []
        gate = asyncio.Event()

        async def fake_call_tool(**kwargs):
            seen.append(json.loads(kwargs['extra_env'][skill_tools.CTX_ENV]))
            if len(seen) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 2)
            return {'ok': True, 'value': 'done'}

        handler = skill_tools.build_tool_handler(
            node='node', host_script='h', source_dir='.', extension='.',
            extension_id='fx', mapping={'ping': 'fx__ping'},
            context=lambda: skill_tools.runtime_tool_context(runtime),
            required_context={'chatKey', 'kind'})

        async def invoke(target):
            with skill_tools.tool_context(runtime, {'kind': 'group', 'target': target}):
                threaded = await asyncio.to_thread(skill_tools.runtime_tool_context, runtime)
                self.assertEqual(threaded['chatId'], target)
                result = await handler('ping', {})
                self.assertTrue(result['ok'])

        async def run():
            await asyncio.gather(invoke('1'), invoke('2'))
        with mock.patch.object(extension_host, 'call_tool', fake_call_tool):
            asyncio.run(run())
        self.assertEqual({item['chatKey'] for item in seen}, {'group:1', 'group:2'})
        self.assertNotIn('chatId', skill_tools.runtime_tool_context(runtime))

    def test_cancelled_turn_restores_context(self):
        runtime = FakeAccountRuntime()
        async def run():
            with self.assertRaises(asyncio.CancelledError):
                with skill_tools.tool_context(runtime, {'kind': 'group', 'target': '1'}):
                    raise asyncio.CancelledError()
            self.assertNotIn('chatKey', skill_tools.runtime_tool_context(runtime))
        asyncio.run(run())

    def test_parent_env_value_is_not_touched(self):
        # 改前：注入期间覆盖 os.environ 再还原；改后：父进程那份从头到尾没被碰过。
        os.environ[skill_tools.CTX_ENV] = '{"chatKey":"outer"}'
        self.addCleanup(os.environ.pop, skill_tools.CTX_ENV, None)
        handler = self.handler({'chatKey': 'inner'})
        asyncio.run(handler('fx__ping', {}))
        self.assertEqual(json.loads(self.seen[0]), {'chatKey': 'inner'})
        self.assertEqual(self.env_seen[0], '{"chatKey":"outer"}')
        self.assertEqual(os.environ[skill_tools.CTX_ENV], '{"chatKey":"outer"}')

    def test_concurrent_calls_see_their_own_context_without_serialising(self):
        # 三个并发调用必须同时在路上：gate 要等三个都进场才放行。
        # 旧实现按事件循环加锁串行，第一个调用会在这里等到 2s 超时 -> ok=False。
        os.environ.pop(skill_tools.CTX_ENV, None)
        in_flight = {'n': 0}
        all_in = asyncio.Event()

        async def gate():
            in_flight['n'] += 1
            if in_flight['n'] >= 3:
                all_in.set()
            await asyncio.wait_for(all_in.wait(), 2)

        handlers = [self.handler({'chatKey': 'k%d' % index}, gate=gate)
                    for index in range(3)]

        async def run():
            return await asyncio.gather(*[item('fx__ping', {}) for item in handlers])
        started = time.perf_counter()
        results = asyncio.run(run())
        elapsed = time.perf_counter() - started
        self.assertTrue(all(item['ok'] for item in results), results)
        self.assertEqual(in_flight['n'], 3)                       # 三个调用真的重叠过
        self.assertEqual(sorted(json.loads(text)['chatKey'] for text in self.seen),
                         ['k0', 'k1', 'k2'])                       # 各看各的 ctx
        self.assertEqual(set(self.env_seen), {None})               # 父进程环境始终干净
        self.assertLess(elapsed, 2.0, elapsed)                     # 没有排队等锁


# ── extension_host.extra_env 契约（假子进程，不需要 node） ───────────────────
class ExtraEnvContractTest(unittest.TestCase):
    """extra_env 只拼进子进程 env；键值转 str；坏形状当场 ValueError。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / 'src'
        self.source.mkdir()
        self.host = self.root / 'host-one.mjs'
        self.host.write_text('// stub', encoding='utf-8')
        self.extension = self.root / 'ext'
        self.extension.mkdir()
        os.environ.pop(skill_tools.CTX_ENV, None)

    def run_capture(self, **overrides):
        """跑一次 run_capability，返回它交给子进程的 env（假 create_subprocess_exec）。"""
        box = {}

        class FakeProc(object):
            pid = 4321
            returncode = 0

            async def communicate(self):
                return b'__WHALE_JSON__{"ok": true, "tool_result": {"v": 1}}', b''

        async def fake_exec(*argv, **kwargs):
            box['env'] = kwargs.get('env')
            return FakeProc()

        with mock.patch.object(extension_host.asyncio, 'create_subprocess_exec', fake_exec):
            run = asyncio.run(extension_host.run_capability(
                node='node', host_script=str(self.host), extension=str(self.extension),
                source_dir=str(self.source), capability='tool', capability_arg='x__t',
                payload={}, timeout=5, data_dir=self.root / 'data', kind='tool',
                extension_id='x', **overrides))
        self.assertTrue(run['ok'], run)
        return box['env']

    def test_extra_env_reaches_the_child_and_not_os_environ(self):
        env = self.run_capture(extra_env={'QQ_AGENT_CTX': '{"chatKey":"c1"}'})
        self.assertEqual(env['QQ_AGENT_CTX'], '{"chatKey":"c1"}')
        self.assertEqual(env['QQ_AGENT_SOURCE'], str(self.source))
        self.assertNotIn('QQ_AGENT_CTX', os.environ)

    def test_default_is_unchanged_child_env(self):
        env = self.run_capture()
        self.assertNotIn('QQ_AGENT_CTX', env)
        self.assertEqual(env['QQ_AGENT_SOURCE'], str(self.source))

    def test_values_are_coerced_to_str(self):
        env = self.run_capture(extra_env={'QQ_AGENT_CTX': 5, 'X_FLAG': True})
        self.assertEqual(env['QQ_AGENT_CTX'], '5')
        self.assertEqual(env['X_FLAG'], 'True')

    def test_bad_shapes_raise_value_error(self):
        for bad in ('x', ['a'], 1, 0.5):
            self.assertRaises(ValueError, extension_host._coerce_extra_env, bad)
        # 传进公开入口也一样当场抛（校验在起进程之前）
        with self.assertRaises(ValueError):
            asyncio.run(extension_host.call_tool(
                node='node', host_script=str(self.host), extension=str(self.extension),
                source_dir=str(self.source), tool_id='x__t', extra_env='not-a-dict'))

    def test_source_override_is_refused(self):
        with self.assertRaises(ValueError):
            extension_host._coerce_extra_env({'QQ_AGENT_SOURCE': 'elsewhere'})


# ── 宿主脚本契约 ─────────────────────────────────────────────────────────────
class HostScriptChannelTest(unittest.TestCase):
    def test_both_copies_carry_the_channel(self):
        copies = [RUNTIME_HOST]
        if MARKET_HOST.is_file():
            copies.append(MARKET_HOST)
        else:
            self.skipTest('市场兼容层副本不存在')
        bodies = [path.read_text(encoding='utf-8') for path in copies]
        self.assertEqual(bodies[0], bodies[1])  # 两份副本必须逐字相同
        for text in bodies:
            self.assertIn('process.env.QQ_AGENT_CTX', text)
            self.assertIn('function hostContext()', text)
            # 解析失败必须安全降级
            self.assertIn('catch { return {}; }', text)
            # 合并顺序（P1-2）：payload 先摊平，环境 ctx 与宿主回调在后覆盖，
            # 所以模型实参伪造的 chatKey 会被环境里的真值压掉。
            ctx_line = [line for line in text.splitlines() if 'const toolCtx =' in line][0]
            # hostContext is read once before availability and execution.
            context_source = '...hostContext()' if '...hostContext()' in ctx_line else '...context'
            if context_source == '...context':
                self.assertIn('const context = hostContext();', text)
            self.assertLess(ctx_line.index('...payload'), ctx_line.index(context_source))


# ── 真进程 ───────────────────────────────────────────────────────────────────
@unittest.skipUnless(READY, '需要 node + host-one.mjs + 0.4 运行副本')
class RealProcessInjectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Do not write plugins-host-* into the read-only original 0.4 baseline.
        self.source = self.root / 'isolated-host'
        shutil.copytree(REPO / 'runtime/qqagent-host', self.source)
        self.extension = self.root / 'ctxfixture'
        self.extension.mkdir()
        (self.extension / 'skill.json').write_text(
            json.dumps(SKILL_JSON, ensure_ascii=False), encoding='utf-8')
        (self.extension / 'index.js').write_text(INDEX_JS, encoding='utf-8')
        self.tool_id = 'ctxfixture__echo_ctx'
        os.environ.pop(skill_tools.CTX_ENV, None)

    def build(self, context=None):
        return skill_tools.build_tool_handler(
            node=PATHS['node'] if 'node' in PATHS else (NODE or 'node'),
            host_script=str(self.source / 'host-one.mjs'), source_dir=str(self.source),
            extension=str(self.extension), extension_id='ctxfixture',
            mapping={'echo_ctx': self.tool_id}, timeout=30,
            data_dir=self.root / 'data', context=context)

    def call(self, context=None, arguments=None):
        handler = self.build(context)
        return asyncio.run(handler(self.tool_id, arguments or {}))

    def test_injected_context_reaches_the_tool(self):
        result = self.call({'chatKey': 'group:12345', 'kind': 'group', 'senderId': '999'})
        self.assertTrue(result['ok'], result)
        value = result['result']
        self.assertEqual(value['chatKey'], 'group:12345')
        self.assertEqual(value['kind'], 'group')
        self.assertEqual(value['senderId'], '999')
        self.assertTrue(value['hasChatKey'])
        self.assertEqual(value['logType'], 'function')

    def test_without_context_it_behaves_like_before(self):
        result = self.call(None)
        self.assertTrue(result['ok'], result)
        value = result['result']
        self.assertIsNone(value['chatKey'])
        self.assertIsNone(value['kind'])
        self.assertFalse(value['hasChatKey'])

    def test_bare_builtin_loads_and_executes_without_spawning(self):
        source = """import { execSync } from 'child_process';
export function setup(api) {
  api.registerTool({ id: 'echo_ctx', name: 'builtin probe',
    parameters: { type: 'object', properties: {} },
    async execute() { return { execType: typeof execSync }; }
  });
}
"""
        (self.extension / 'index.js').write_text(source, encoding='utf-8')
        usage = skill_tools.ctx_api_usage(self.extension)
        self.assertIn('node:child_process', usage)
        self.assertEqual(skill_tools.blocked_capabilities(usage), set())
        result = self.call()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result'], {'execType': 'function'})

    def test_forged_identity_in_arguments_cannot_override_the_turn(self):
        """P1-2 负例：实参里伪造 chatKey/kind/senderId/target，压不掉本轮注入的可信值。"""
        result = self.call(
            {'chatKey': 'group:12345', 'kind': 'group', 'senderId': '999', 'target': 'group:12345'},
            {'chatKey': 'group:666', 'kind': 'private', 'senderId': '666', 'target': 'private:666',
             'probe': 'p'})
        self.assertTrue(result['ok'], result)
        value = result['result']
        self.assertEqual(value['chatKey'], 'group:12345')
        self.assertEqual(value['kind'], 'group')
        self.assertEqual(value['senderId'], '999')
        self.assertEqual(value['target'], 'group:12345')
        # 业务参数不受影响：第二参拿得到，摊平后的 ctx 也读得到。
        self.assertEqual(value['argsProbe'], 'p')
        self.assertEqual(value['probe'], 'p')

    def test_forged_host_callbacks_are_replaced_by_the_host_ones(self):
        """实参里塞 fetch/log/metadata/signal 的数据换不掉宿主回调。"""
        result = self.call({'chatKey': 'group:12345'},
                           {'fetch': 'evil', 'log': 'evil', 'metadata': {'forged': True},
                            'signal': 'evil'})
        self.assertTrue(result['ok'], result)
        value = result['result']
        self.assertEqual(value['fetchType'], 'function')
        self.assertEqual(value['logType'], 'function')
        self.assertEqual(value['metadataKeys'], '')
        self.assertTrue(value['signalIsNull'], value)

    def test_absent_trusted_field_is_filled_from_arguments(self):
        """宿主没有该字段时没有可信值可保护，实参的会留下——既定边界，不是回归。"""
        result = self.call(None, {'chatKey': 'from-payload'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['chatKey'], 'from-payload')

    def test_single_arg_extension_still_reads_arguments_from_ctx(self):
        """老式 execute(payload) 靠摊平读参数，改顺序之后仍然读得到。"""
        source = """export function setup(api) {
  api.registerTool({ id: 'echo_ctx', name: 'old style',
    parameters: { type: 'object', properties: {} },
    async execute(payload) {
      return { probe: payload && payload.probe !== undefined ? String(payload.probe) : null };
    }
  });
}
"""
        (self.extension / 'index.js').write_text(source, encoding='utf-8')
        result = self.call(None, {'probe': 'old-style'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result'], {'probe': 'old-style'})

    def test_parent_process_never_sees_the_variable_during_a_real_call(self):
        # 真 node：调用期间另起线程高频采样父进程环境，一次都不该出现这个键。
        samples = []
        stop = threading.Event()

        def probe():
            while not stop.is_set():
                samples.append(os.environ.get(skill_tools.CTX_ENV))
                time.sleep(0.002)

        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        try:
            result = self.call({'chatKey': 'group:777'})
        finally:
            stop.set()
            thread.join(5)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['chatKey'], 'group:777')  # 子进程真收到了
        self.assertGreater(len(samples), 3, len(samples))            # 采样确实覆盖了调用期
        self.assertEqual(set(samples), {None})
        self.assertNotIn(skill_tools.CTX_ENV, os.environ)

    def test_real_call_tool_keeps_the_value_in_the_child_only(self):
        run = asyncio.run(extension_host.call_tool(
            node=PATHS['node'] if 'node' in PATHS else (NODE or 'node'),
            host_script=str(self.source / 'host-one.mjs'), source_dir=str(self.source),
            extension=str(self.extension), tool_id=self.tool_id, arguments={},
            timeout=30, data_dir=self.root / 'data', extension_id='ctxfixture',
            extra_env={skill_tools.CTX_ENV: '{"chatKey":"direct"}'}))
        self.assertTrue(run['ok'], run)
        self.assertEqual(run['value']['chatKey'], 'direct')          # 真子进程读到了
        self.assertFalse(run['value']['hasChatKey'] is None)
        self.assertNotIn(skill_tools.CTX_ENV, os.environ)


if __name__ == '__main__':
    unittest.main()
