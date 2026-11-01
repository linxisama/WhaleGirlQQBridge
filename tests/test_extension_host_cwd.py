"""子进程受控工作目录：扩展里的相对路径落在本次调用独占目录，不写进仓库根。

三条不变量：
1. 每次 run_capability 起子进程都给一个本次调用独占的 cwd；成功、失败、超时、
   被强杀、被取消都在 finally 里删掉。
2. 建目录失败不炸：回落成不传 cwd（子进程继承父进程目录，行为同改动前），原因进
   返回值 cwd_error。
3. run_capability / list_tools / call_tool 的公开签名与返回结构对既有调用方兼容，
   环境注入规则（QQ_AGENT_SOURCE / QQ_AGENT_HOST_TIMEOUT_MS / 残留键不继承）不变。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import extension_host

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
REPO_PROBE = REPO / 'data' / 'probe.txt'

# 扩展里的相对路径就是缺陷现场：fs.writeFileSync('data/probe.txt') 以前落在仓库根。
WRITE_BODY = """
import fs from 'node:fs';
import path from 'node:path';
export function setup(api){
  api.registerTool({id:'write', name:'write', execute(){
    const before = fs.existsSync('data/probe.txt');
    fs.mkdirSync('data', {recursive:true});
    fs.writeFileSync('data/probe.txt','x');
    return {cwd: process.cwd(), abs: path.resolve('data/probe.txt'),
            content: fs.readFileSync('data/probe.txt','utf8'), before};
  }});
}
"""

HANG_BODY = """
import fs from 'node:fs';
export function setup(api){
  api.registerTool({id:'hang', name:'hang', execute(){
    fs.mkdirSync('data', {recursive:true});
    fs.writeFileSync('data/hang.txt','x');
    return new Promise(()=>{});
  }});
}
"""

OK_JSON = b'__WHALE_JSON__{"ok": true, "tool_result": {"v": 1}}'

# 改动前就有的返回字段（兼容性基线）：只比对字段名，不比对值。
RUN_FIELDS = {'ok', 'kind', 'capability', 'argument', 'extension', 'value', 'raw',
              'error', 'timed_out', 'duration_ms', 'pid', 'stderr', 'stderr_tail'}
RUN_NEW_FIELDS = {'cwd', 'cwd_error'}  # cwd 总出现；cwd_error 只在建目录失败时出现
LIST_FIELDS = {'ok', 'tools', 'error', 'extension', 'duration_ms'}
CALL_FIELDS = {'ok', 'value', 'tool', 'extension', 'duration_ms', 'error', 'raw'}


def _same(left, right) -> bool:
    """Windows 上 realpath/normcase 之后再比，避免短名/符号链接误判。"""
    norm = lambda value: os.path.normcase(os.path.realpath(str(value)))
    return norm(left) == norm(right)


@unittest.skipUnless(NODE, 'Node runtime required')
class RealNodeCallCwdTests(unittest.TestCase):
    """真 node 端到端：扩展写相对路径，看它到底落在哪。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = self.root / 'host'
        shutil.copytree(REPO / 'runtime/qqagent-host', self.host)
        self.data = self.root / 'data'

    def package(self, ident, body):
        folder = self.host / 'plugins' / ident
        folder.mkdir(parents=True)
        meta = {'id': ident, 'name': ident, 'version': '1.0.0', 'apiVersion': 1,
                'entry': 'index.js', 'enabledByDefault': True, 'requires': [],
                'capabilities': []}
        (folder / 'plugin.json').write_text(json.dumps(meta), encoding='utf-8')
        (folder / 'index.js').write_text(body, encoding='utf-8')
        return folder

    def run_tool(self, folder, tool, timeout=20, **overrides):
        kwargs = dict(node=NODE, host_script=str(self.host / 'host-one.mjs'),
                      source_dir=str(self.host), extension=str(folder),
                      extension_id=folder.name, capability='tool', capability_arg=tool,
                      payload={}, kind='tool', data_dir=self.data, timeout=timeout)
        kwargs.update(overrides)
        return asyncio.run(extension_host.run_capability(**kwargs))

    def residue(self):
        root = extension_host.call_cwd_root(self.data)
        return sorted(item.name for item in root.iterdir()) if root.is_dir() else []

    def test_relative_write_lands_in_call_cwd_and_repo_root_stays_clean(self):
        folder = self.package('fixture-cwd', WRITE_BODY)
        result = self.run_tool(folder, 'fixture-cwd__write')
        self.assertTrue(result['ok'], result)
        value = result['value']
        cwd = Path(result['cwd'])
        self.assertEqual(set(result) - RUN_FIELDS - {'cwd_error'}, {'cwd'})
        self.assertTrue(_same(cwd.parent, extension_host.call_cwd_root(self.data)), cwd)
        self.assertEqual(cwd.name.split('-')[0], str(os.getpid()))
        self.assertTrue(_same(value['cwd'], cwd), value)
        self.assertTrue(_same(value['abs'], cwd / 'data' / 'probe.txt'), value)
        self.assertEqual(value['content'], 'x')
        self.assertFalse(value['before'])
        self.assertFalse(REPO_PROBE.exists(), '仓库根被写了：%s' % REPO_PROBE)
        self.assertFalse(cwd.exists(), '本次调用目录没删掉：%s' % cwd)
        self.assertEqual(self.residue(), [])

    def test_each_call_gets_a_fresh_directory(self):
        folder = self.package('fixture-fresh', WRITE_BODY)
        first = self.run_tool(folder, 'fixture-fresh__write')
        second = self.run_tool(folder, 'fixture-fresh__write')
        self.assertTrue(first['ok'], first)
        self.assertTrue(second['ok'], second)
        self.assertNotEqual(first['cwd'], second['cwd'])
        self.assertFalse(Path(first['cwd']).exists(), first['cwd'])
        self.assertFalse(Path(second['cwd']).exists(), second['cwd'])
        # 上一次写的文件不会漏到下一次调用的目录里
        self.assertFalse(second['value']['before'])

    def test_timeout_still_removes_call_cwd(self):
        folder = self.package('fixture-hang', HANG_BODY)
        result = self.run_tool(folder, 'fixture-hang__hang', timeout=3)
        self.assertFalse(result['ok'], result)
        self.assertTrue(result['timed_out'], result)
        self.assertTrue(result['cwd'], result)
        self.assertFalse(Path(result['cwd']).exists(), result['cwd'])
        self.assertEqual(self.residue(), [])

    def test_list_tools_and_call_tool_leave_no_residue(self):
        folder = self.package('fixture-face', WRITE_BODY)
        listed = asyncio.run(extension_host.list_tools(
            node=NODE, host_script=str(self.host / 'host-one.mjs'),
            source_dir=str(self.host), extension=str(folder),
            extension_id='fixture-face', data_dir=self.data, timeout=20))
        self.assertTrue(listed['ok'], listed)
        self.assertEqual(set(listed), LIST_FIELDS)
        self.assertEqual(self.residue(), [])
        called = asyncio.run(extension_host.call_tool(
            node=NODE, host_script=str(self.host / 'host-one.mjs'),
            source_dir=str(self.host), extension=str(folder),
            tool_id='fixture-face__write', arguments={},
            extension_id='fixture-face', data_dir=self.data, timeout=20))
        self.assertTrue(called['ok'], called)
        self.assertEqual(set(called), CALL_FIELDS)
        self.assertEqual(self.residue(), [])


class CallCwdFallbackTests(unittest.TestCase):
    """假 create_subprocess_exec：不需要 node，专测回落、清理与返回结构。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'host'
        (self.source / 'src').mkdir(parents=True)
        self.script = self.source / 'host-one.mjs'
        self.script.write_text('// x', encoding='utf-8')
        self.ext = self.root / 'ext'
        self.ext.mkdir()
        (self.ext / 'index.js').write_text('// x', encoding='utf-8')
        self.data = self.root / 'data'

    def run_with_fake(self, *, data_dir='default', spawn=None, payload=OK_JSON, **overrides):
        seen = []

        class FakeProc(object):
            pid = 4321
            returncode = 0

            async def communicate(self):
                return payload, b''

            def kill(self):
                pass

            async def wait(self):
                return 0

        async def fake_exec(*argv, **kwargs):
            cwd = kwargs.get('cwd')
            seen.append({'argv': argv, 'kwargs': kwargs, 'cwd': cwd,
                         'existed': bool(cwd) and os.path.isdir(str(cwd))})
            return spawn() if spawn else FakeProc()

        kwargs = dict(node='node', host_script=str(self.script), extension=str(self.ext),
                      source_dir=str(self.source), capability='tool',
                      capability_arg='x__t', payload={}, kind='tool', timeout=5,
                      data_dir=self.data if data_dir == 'default' else data_dir,
                      extension_id='x')
        kwargs.update(overrides)
        with mock.patch.object(extension_host.asyncio, 'create_subprocess_exec', fake_exec), \
                mock.patch.object(extension_host, 'log_path',
                                  lambda data_dir=None: self.root / 'calls.jsonl'):
            run = asyncio.run(extension_host.run_capability(**kwargs))
        return run, seen

    def fake_exec_for(self, proc, payload=OK_JSON):
        async def fake_exec(*argv, **kwargs):
            return proc
        return fake_exec

    def test_qq_only_data_dir_does_not_nest_twice(self):
        only = self.data / 'qq-only'
        self.assertEqual(extension_host.call_cwd_root(only), only / 'extension_cwd')

    def test_missing_data_dir_falls_back_to_system_temp(self):
        run, seen = self.run_with_fake(data_dir=None)
        self.assertTrue(run['ok'], run)
        cwd = Path(run['cwd'])
        self.assertTrue(_same(cwd.parent,
                              Path(tempfile.gettempdir()) / 'qq-only' / 'extension_cwd'), cwd)
        self.assertEqual(cwd.name.split('-')[0], str(os.getpid()))
        self.assertTrue(seen[0]['existed'], '起子进程时目录必须已经存在')
        self.assertTrue(_same(seen[0]['cwd'], cwd), seen[0]['cwd'])
        self.assertFalse(cwd.exists(), cwd)

    def test_creation_failure_falls_back_to_inherited_cwd(self):
        blocker = self.root / 'blocker'
        blocker.write_text('not a directory', encoding='utf-8')
        run, seen = self.run_with_fake(data_dir=blocker)
        self.assertTrue(run['ok'], run)
        self.assertEqual(run['cwd'], '')
        self.assertTrue(run.get('cwd_error'), run)
        self.assertIsNone(seen[0]['kwargs'].get('cwd'), '回落时必须不传 cwd')
        self.assertEqual(run['value'], {'v': 1})
        self.assertTrue(RUN_FIELDS <= set(run), sorted(set(run)))

    def test_spawn_error_still_removes_directory(self):
        def boom():
            raise OSError('spawn-failed')

        run, seen = self.run_with_fake(spawn=boom)
        self.assertFalse(run['ok'], run)
        self.assertIn('OSError', run['error'])
        self.assertTrue(seen[0]['existed'])
        self.assertFalse(Path(seen[0]['cwd']).exists(), seen[0]['cwd'])

    def test_timeout_path_still_removes_directory(self):
        class HangProc(object):
            """真卡住：第一次 communicate 永不返回，超时后被 kill 才放行。"""

            pid = 4321
            returncode = None

            def __init__(self):
                self.calls = 0
                self.killed = False

            async def communicate(self):
                self.calls += 1
                if self.calls == 1:
                    await asyncio.sleep(3600)
                return b'', b''

            def kill(self):
                self.killed = True

            async def wait(self):
                return 0

        proc = HangProc()
        run, seen = self.run_with_fake(spawn=lambda: proc, timeout=1)
        self.assertFalse(run['ok'], run)
        self.assertTrue(run['timed_out'], run)
        self.assertTrue(proc.killed, '超时后必须强杀子进程')
        self.assertIn('timeout after 1s', run['error'])
        self.assertFalse(Path(seen[0]['cwd']).exists(), seen[0]['cwd'])

    def test_cancelled_call_still_removes_directory(self):
        class StuckProc(object):
            pid = 4321
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            def kill(self):
                pass

            async def wait(self):
                return 0

        seen = []

        async def fake_exec(*argv, **kwargs):
            seen.append(kwargs.get('cwd'))
            return StuckProc()

        async def scenario():
            task = asyncio.create_task(extension_host.run_capability(
                node='node', host_script=str(self.script), extension=str(self.ext),
                source_dir=str(self.source), capability='tool', capability_arg='x__t',
                payload={}, kind='tool', timeout=30, data_dir=self.data,
                extension_id='x'))
            for _ in range(200):
                if seen:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with mock.patch.object(extension_host.asyncio, 'create_subprocess_exec', fake_exec), \
                mock.patch.object(extension_host, 'log_path',
                                  lambda data_dir=None: self.root / 'calls.jsonl'):
            asyncio.run(scenario())
        self.assertTrue(seen and seen[0], seen)
        self.assertFalse(Path(seen[0]).exists(), seen[0])

    def test_env_and_timeout_rules_are_unchanged(self):
        run, seen = self.run_with_fake()
        env = seen[0]['kwargs']['env']
        self.assertTrue(run['ok'], run)
        self.assertEqual(env['QQ_AGENT_SOURCE'], str(self.source))
        self.assertEqual(env['QQ_AGENT_HOST_TIMEOUT_MS'], str(max(100, int(5 * 1000) - 250)))
        self.assertIsInstance(json.loads(env['QQ_AGENT_DEPENDENCY_IDS']), list)
        self.assertNotIn('QQ_AGENT_CTX', env)

    def test_public_shapes_match_existing_callers(self):
        run, _seen = self.run_with_fake()
        self.assertTrue(RUN_FIELDS <= set(run), sorted(set(run) - RUN_FIELDS))
        self.assertTrue(set(run) - RUN_FIELDS <= RUN_NEW_FIELDS, sorted(set(run) - RUN_FIELDS))

        payload = b'__WHALE_JSON__{"ok": true, "extension": {"id": "x", "tools": ["x__t"]}, "tools": [{"id": "x__t", "name": "t"}]}'

        class FakeProc(object):
            pid = 1
            returncode = 0

            async def communicate(self):
                return payload, b''

        async def fake_exec(*argv, **kwargs):
            return FakeProc()

        with mock.patch.object(extension_host.asyncio, 'create_subprocess_exec', fake_exec):
            listed = asyncio.run(extension_host.list_tools(
                node='node', host_script=str(self.script), extension=str(self.ext),
                source_dir=str(self.source), extension_id='x', data_dir=self.data, timeout=5))
            called = asyncio.run(extension_host.call_tool(
                node='node', host_script=str(self.script), extension=str(self.ext),
                source_dir=str(self.source), tool_id='x__t', arguments={},
                extension_id='x', data_dir=self.data, timeout=5))
        self.assertEqual(set(listed), LIST_FIELDS)
        self.assertEqual(set(called), CALL_FIELDS)
        self.assertEqual([item['id'] for item in listed['tools']], ['x__t'])
        self.assertEqual(called['tool'], 'x__t')


if __name__ == '__main__':
    unittest.main()
