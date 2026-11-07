"""宿主工具 ctx 的 fetch 按 permissions 注入（W8b）。

口径与 `src/plugin-loader.js:124-126` 一致：只有清单里声明了 `web_fetch` 的扩展，
工具 ctx 上才拿到真的 `globalThis.fetch`；其余情况 `ctx.fetch` 这个名字仍然在，
但调用必 reject，且消息要说人话。清单没有 permissions 段、permissions 为 null /
字符串 / 对象等非数组形状，一律按「未声明」处理，且不能崩。

分两部分：
1. 真进程：真 node + 真 host-one.mjs + 临时扩展 + 本机 127.0.0.1 临时 HTTP 服务；
2. 契约：两份 host-one.mjs 逐字节一致，且闸门还在（防止只打一份补丁或日后回退）。
"""
import asyncio
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src import extension_host

REPO = Path(__file__).resolve().parents[1]
RUNTIME_HOST = REPO / 'runtime' / 'qqagent-host' / 'host-one.mjs'
MARKET_HOST = Path('D:/聊天机器人/插件市场/QQAgent兼容层/host-one.mjs')
NODE = shutil.which('node')

BODY_PREFIX = 'W8B-FETCH-OK'

# 夹具：把 ctx.fetch 的行为整个回显出来，断言只读返回值。
INDEX_JS = """export function setup(api) {
  api.registerTool({
    id: 'probe', name: 'probe', description: '取证：回显 ctx.fetch 的行为',
    parameters: { type: 'object', properties: { url: { type: 'string' }, mode: { type: 'string' } } },
    async execute(ctx, args) {
      const info = {
        fetchType: typeof ctx.fetch,
        isGlobalFetch: typeof ctx.fetch === 'function'
          && String(ctx.fetch) === String(globalThis.fetch),
        signalValue: ctx.signal === null ? 'null' : String(ctx.signal),
        logType: typeof ctx.log,
        metadataType: ctx.metadata && typeof ctx.metadata === 'object' ? 'object' : String(typeof ctx.metadata),
        payloadEcho: ctx.probe !== undefined ? String(ctx.probe) : null,
        rejected: false,
      };
      const url = (args && args.url) || ctx.url || '';
      try {
        const target = (args && args.mode) === 'request' ? new Request(url) : url;
        const response = await ctx.fetch(target);
        info.status = response.status;
        info.isResponse = response instanceof Response;
        info.text = await response.text();
      } catch (error) {
        info.rejected = true;
        info.isError = error instanceof Error;
        info.message = String((error && error.message) || error);
      }
      return info;
    }
  });
}
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
        body = ('%s %s' % (BODY_PREFIX, self.path)).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(NODE and RUNTIME_HOST.is_file(), '需要 node + runtime host-one.mjs')
class HostFetchCtxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # 不往只读的 0.4 基线里写 plugins-host-*，先复制成隔离副本。
        self.source = self.root / 'isolated-host'
        shutil.copytree(REPO / 'runtime/qqagent-host', self.source)
        self.data = self.root / 'data'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d/hello' % self.server.server_address[1]

        def stop():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)

        self.addCleanup(stop)

    # ── 夹具 ────────────────────────────────────────────────────────────────
    def package(self, ident='fetchfixture', *, permissions=None, drop_permissions=False):
        folder = self.source / 'plugins' / ident
        folder.mkdir(parents=True)
        meta = {'id': ident, 'name': ident, 'version': '1.0.0', 'apiVersion': 1,
                'entry': 'index.js', 'enabledByDefault': True,
                'requires': [], 'capabilities': []}
        if not drop_permissions:
            meta['permissions'] = [] if permissions is None else permissions
        (folder / 'plugin.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        (folder / 'index.js').write_text(INDEX_JS, encoding='utf-8')
        return folder

    def run_probe(self, folder, **arguments):
        payload = {'url': self.url}
        payload.update(arguments)
        run = asyncio.run(extension_host.run_capability(
            node=NODE, host_script=str(self.source / 'host-one.mjs'),
            source_dir=str(self.source), extension=str(folder),
            extension_id=folder.name, capability='tool',
            capability_arg='%s__probe' % folder.name, payload=payload, kind='tool',
            data_dir=self.data, timeout=60,
            extra_env={'QQ_AGENT_DATA_DIR': str(self.root / 'node-data')}))
        self.assertTrue(run['ok'], run)
        return run['value']

    # ── 声明了 web_fetch ────────────────────────────────────────────────────
    def test_declared_permission_gets_real_fetch(self):
        value = self.run_probe(self.package(permissions=['web_fetch']))
        self.assertFalse(value['rejected'], value)
        self.assertEqual(value['fetchType'], 'function')
        self.assertTrue(value['isGlobalFetch'], value)   # 就是 globalThis.fetch 本身
        self.assertTrue(value['isResponse'], value)      # Response 语义没被包坏
        self.assertEqual(value['status'], 200)
        self.assertIn(BODY_PREFIX, value['text'])
        self.assertIn('/hello', value['text'])

    def test_request_object_is_accepted_by_the_passthrough_fetch(self):
        value = self.run_probe(self.package(permissions=['web_fetch']), mode='request')
        self.assertFalse(value['rejected'], value)
        self.assertEqual(value['status'], 200)
        self.assertIn(BODY_PREFIX, value['text'])

    def test_existing_ctx_members_and_payload_order_survive(self):
        value = self.run_probe(self.package(permissions=['web_fetch']), probe='payload-wins')
        self.assertEqual(value['signalValue'], 'null')
        self.assertEqual(value['logType'], 'function')
        self.assertEqual(value['metadataType'], 'object')
        self.assertEqual(value['payloadEcho'], 'payload-wins')

    # ── 未声明：桩必须 reject 且消息是人话 ──────────────────────────────────
    def assert_stub_rejects(self, permissions=None, drop_permissions=False, ident='fetchfixture'):
        value = self.run_probe(self.package(ident, permissions=permissions,
                                            drop_permissions=drop_permissions))
        self.assertEqual(value['fetchType'], 'function', value)  # 名字在，但只是桩
        self.assertFalse(value['isGlobalFetch'], value)
        self.assertTrue(value['rejected'], value)
        self.assertTrue(value['isError'], value)                 # 真 Error，不是字符串
        self.assertIn('web_fetch', value['message'])
        self.assertIn('未声明', value['message'])
        self.assertIn('无法出网', value['message'])
        return value

    def test_undeclared_permission_gets_rejecting_stub(self):
        self.assert_stub_rejects(permissions=[])

    def test_manifest_without_permissions_section_does_not_crash(self):
        self.assert_stub_rejects(drop_permissions=True)

    def test_non_array_permissions_are_treated_as_undeclared(self):
        for index, permissions in enumerate((None, 'web_fetch', {'web_fetch': True}, 42, True)):
            with self.subTest(permissions=permissions):
                # 每轮换 id：同名的扩展目录不能重复创建。
                self.assert_stub_rejects(permissions=permissions, ident='fx%02d' % index)


class HostFetchContractTest(unittest.TestCase):
    """两份副本必须逐字节一致，且权限闸门不能只出现在其中一份。"""

    def copies(self):
        if not MARKET_HOST.is_file():
            self.skipTest('插件市场副本不存在（%s），跳过' % MARKET_HOST)
        return [RUNTIME_HOST, MARKET_HOST]

    def test_both_copies_are_byte_identical(self):
        paths = self.copies()
        digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        self.assertEqual(digests[0], digests[1], '两份 host-one.mjs 内容不一致：%s' % (paths,))

    def test_both_copies_carry_the_permission_gate(self):
        for path in self.copies():
            text = path.read_text(encoding='utf-8')
            self.assertIn('function toolFetch(manifest)', text, path)
            self.assertIn("includes('web_fetch')", text, path)
            self.assertIn('未声明 web_fetch 权限', text, path)
            # P1-2 之后 payload 先摊平，可信 ctx 与宿主回调在后覆盖；fetch 也在覆盖侧，
            # 模型实参换不掉它。
            line = [item for item in text.splitlines() if 'const toolCtx =' in item][0]
            self.assertIn('fetch: toolFetch(', line)
            self.assertLess(line.index('...payload'), line.index('...context'))
            self.assertLess(line.index('...context'), line.index('fetch: toolFetch('))


if __name__ == '__main__':
    unittest.main()
