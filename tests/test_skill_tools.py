"""Skill 工具接入（skill_tools）单元测试。

本机 venv 没装 pytest，用 unittest 写（pytest 也能收集）。覆盖：zip 安全解包（路径穿越/
软链/条目与总量上限）、清单损坏降级、运行时全名注册键（含前缀≠清单 id 的真实用例）、
T4/T6 结构性禁用、handler 超时与错误映射、单 skill 失败隔离、ctx 静态扫描（含可选链）、
审计脱敏与轮转。
"""
import asyncio
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src import extension_host, skill_tools, tool_loop


SKILL_TEMPLATE = {'name': '示例', 'version': '1.0.0'}


def write_zip(path, files, extra=None):
    """造一个 zip；files 是 相对路径 → 文本。extra 可以是 ZipInfo 列表。"""
    with zipfile.ZipFile(str(path), 'w') as archive:
        for name, text in files.items():
            archive.writestr(name, text)
        for info in extra or []:
            archive.writestr(info, 'x')
    return path


def skill_zip(path, skill_id, source='api.registerTool({ name: "ping" }, () => ({ ok: true }));'):
    return write_zip(path, {
        '%s/skill.json' % skill_id: json.dumps(dict(SKILL_TEMPLATE, id=skill_id), ensure_ascii=False),
        '%s/index.js' % skill_id: source,
    })


class FakeRuntime(object):
    """够 tool_loop.registry_for / skill_tools.data_dir 用的最小 runtime。"""

    def __init__(self, root):
        self.data_root = str(root)
        self.config = {}

    @property
    def registry(self):
        return tool_loop.registry_for(self)


def manifest_of(*skills):
    return {'skills': list(skills), 'source': 'test', 'error': '', 'count': len(skills)}


def skill_entry(ident, tier='T1-ready', tools=(), **extra):
    return dict({'id': ident, 'name': ident, 'tier': tier,
                 'tools': [{'name': name, 'description': '', 'hasSchema': True} for name in tools],
                 'permissions': [], 'requires': [], 'hostImports': []}, **extra)


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.ext = self.tmp / 'data' / 'qq-only' / 'skill_extensions'
        self.runtime = FakeRuntime(self.tmp)

    def install(self, skill_id, source=None):
        archive = skill_zip(self.tmp / ('%s.zip' % skill_id), skill_id, source or
                            'api.registerTool({ name: "ping" }, () => ({ ok: true }));')
        return skill_tools.install_skill(skill_id, str(archive), str(self.ext))

    def fake_host(self):
        """造一份假的宿主环境（node/host-one.mjs/0.4 源码树），只骗过存在性检查。"""
        host = self.tmp / 'fake-host-one.mjs'
        host.write_text('// fake', encoding='utf-8')
        source = self.tmp / 'fake-qq-agent-run'
        (source / 'src').mkdir(parents=True, exist_ok=True)
        (source / 'src' / 'plugin-loader.js').write_text('// fake', encoding='utf-8')
        return {'node': 'node', 'host_script': str(host), 'source_dir': str(source)}


# ── 解包安全 ─────────────────────────────────────────────────────────────────
class InstallSafetyTest(TempCase):
    def test_traversal_entry_rejected(self):
        for bad in ('../evil.js', '..\\evil.js', 'a/../../evil.js', '/abs/evil.js',
                    'C:/windows/evil.js'):
            archive = write_zip(self.tmp / 'bad.zip', {
                'skill.json': json.dumps({'id': 'bad'}),
                'index.js': 'x',
                bad: 'pwned',
            })
            with self.assertRaises(skill_tools.SkillToolsError):
                skill_tools.install_skill('bad', str(archive), str(self.ext))
            self.assertFalse((self.ext / 'evil.js').exists())
            self.assertFalse((self.tmp / 'evil.js').exists())

    def test_symlink_entry_rejected(self):
        info = zipfile.ZipInfo('evil-link')
        info.external_attr = (0o120777 << 16)
        archive = write_zip(self.tmp / 'link.zip', {'skill.json': json.dumps({'id': 'link'})}, [info])
        with self.assertRaises(skill_tools.SkillToolsError) as caught:
            skill_tools.install_skill('link', str(archive), str(self.ext))
        self.assertIn('软链', str(caught.exception))

    def test_entry_and_size_limits(self):
        many = {'skill.json': json.dumps({'id': 'many'})}
        many.update({'f%d.js' % index: 'x' for index in range(3)})
        archive = write_zip(self.tmp / 'many.zip', many)
        with mock.patch.object(skill_tools, 'MAX_ENTRIES', 2):
            with self.assertRaises(skill_tools.SkillInstallError) as caught:
                skill_tools.install_skill('many', str(archive), str(self.ext))
        self.assertIn('条目过多', str(caught.exception))
        with mock.patch.object(skill_tools, 'MAX_TOTAL_BYTES', 4):
            with self.assertRaises(skill_tools.SkillInstallError) as caught:
                skill_tools.install_skill('many', str(archive), str(self.ext))
        self.assertIn('解压后过大', str(caught.exception))

    def test_bad_id_and_missing_zip(self):
        archive = skill_zip(self.tmp / 'ok.zip', 'ok')
        for bad_id in ('../ok', '', 'ok/..', 'ok so'):
            with self.assertRaises(skill_tools.SkillInstallError):
                skill_tools.install_skill(bad_id, str(archive), str(self.ext))
        with self.assertRaises(skill_tools.SkillInstallError) as caught:
            skill_tools.install_skill('ok', str(self.tmp / 'nope.zip'), str(self.ext))
        self.assertIn('找不到 zip', str(caught.exception))

    def test_install_lands_package_root(self):
        info = skill_tools.install_skill('ok', str(skill_zip(self.tmp / 'ok.zip', 'ok')),
                                        str(self.ext))
        self.assertTrue(info['ok'])
        self.assertEqual(Path(info['dir']), self.ext / 'ok')
        self.assertTrue((self.ext / 'ok' / 'index.js').is_file())
        self.assertTrue(info['manifestIdMatches'])
        self.assertEqual(info['files'], 2)


# ── 清单 ────────────────────────────────────────────────────────────────────
class ManifestTest(TempCase):
    def test_corrupt_json_degrades_without_raising(self):
        path = self.tmp / 'broken.json'
        path.write_text('{"skills": [', encoding='utf-8')
        loaded = skill_tools.load_manifest(str(path))
        self.assertEqual(loaded['count'], 0)
        self.assertEqual(loaded['skills'], [])
        self.assertIn('不是有效的清单 JSON', loaded['error'])

    def test_top_level_not_list(self):
        path = self.tmp / 'wrong.json'
        path.write_text('{"skills": {"a": 1}}', encoding='utf-8')
        loaded = skill_tools.load_manifest(str(path))
        self.assertEqual(loaded['skills'], [])
        self.assertIn('skills 数组', loaded['error'])

    def test_explicit_missing_path_raises(self):
        with self.assertRaises(skill_tools.SkillToolsError) as caught:
            skill_tools.load_manifest(str(self.tmp / 'nope.json'))
        self.assertIn('清单文件不存在', str(caught.exception))

    def test_fallback_to_builtin_manifest(self):
        # _manifest_candidates 现在带 runtime（临时 data_root 要能读到自己的清单）
        with mock.patch.object(skill_tools, '_manifest_candidates',
                               lambda path=None, runtime=None: [self.tmp / 'missing.json']):
            loaded = skill_tools.load_manifest()
        self.assertEqual(loaded['source'], 'builtin')
        self.assertEqual(loaded['count'], len(skill_tools.BUILTIN_MANIFEST['skills']))
        self.assertIn('回退内置最小清单', loaded['error'])

    def test_mojibake_repaired(self):
        path = self.tmp / 'm.json'
        text = '查询碧蓝幻想 wiki'
        broken = text.encode('utf-8').decode('latin-1')
        path.write_text(json.dumps({'skills': [{'id': 'x', 'name': broken, 'tier': 'T1-ready',
                                               'summary': broken,
                                               'tools': [{'name': 'x__y', 'description': broken}]}]},
                                   ensure_ascii=False), encoding='utf-8')
        loaded = skill_tools.load_manifest(str(path))
        self.assertEqual(loaded['skills'][0]['name'], text)
        self.assertEqual(loaded['skills'][0]['tools'][0]['description'], text)


# ── ctx 静态扫描 ─────────────────────────────────────────────────────────────
class CtxScanTest(TempCase):
    def test_optional_chain_destructure_bracket_and_modules(self):
        folder = self.tmp / 'scan'
        folder.mkdir()
        (folder / 'index.js').write_text('\n'.join([
            "const onebot = ctx?.onebot;",
            "const chatKey = ctx?.chatKey || '';",
            'const { store, memory } = ctx;',
            "ctx['panel'].open();",
            "api.config();",
            "require('node:fs');",
            "require('left-pad');",
        ]), encoding='utf-8')
        usage = skill_tools.ctx_api_usage(str(folder))
        for token in ('onebot', 'chatKey', 'store', 'memory', 'panel', 'config',
                      'node:fs', 'npm:left-pad'):
            self.assertIn(token, usage)
        blocked = skill_tools.blocked_capabilities(usage)
        self.assertIn('onebot', blocked)
        # T1 now provides chatKey from the task-local ingress turn. Callback
        # objects remain blocked; an absent turn is checked at invocation time.
        self.assertNotIn('chatKey', blocked)
        # 产品行为变化：node:fs 从「缺失」提到 NODE_BUILTIN_SAFE。宿主机本身就是 Node，
        # import() 一跑就能自己 import('node:fs')，注册期挡它挡不住任何东西，只是误判。
        self.assertNotIn('node:fs', blocked)
        self.assertNotIn('config', blocked)

    def test_gated_builtin_blocks_until_its_permission_is_declared(self):
        # GATED 档必须自己声明权限名。真实名单已清空（net/vm/worker_threads 都归了
        # ACCOUNTED），所以这里注入一个假 token 验证机制本身没死。
        original = skill_tools.GATED_BY_PERMISSION
        try:
            skill_tools.GATED_BY_PERMISSION = dict(original, **{'node:fake-net': 'network'})
            self.assertEqual(skill_tools.blocked_capabilities(['node:fake-net']),
                             {'node:fake-net'})
            # 闸没死：声明了 GATED 映射里的权限名就放行。
            self.assertEqual(skill_tools.blocked_capabilities(['node:fake-net'], ['network']),
                             set())
            self.assertEqual(skill_tools.blocked_capabilities(['node:fake-net'], ['NETWORK ']),
                             set())
            # 不相干的权限名不算数。
            self.assertEqual(skill_tools.blocked_capabilities(['node:fake-net'], ['web_fetch']),
                             {'node:fake-net'})
        finally:
            skill_tools.GATED_BY_PERMISSION = original

    def test_safe_and_gated_partition_the_node_builtins(self):
        self.assertFalse(skill_tools.NODE_BUILTIN_SAFE & set(skill_tools.NODE_BUILTIN_GATED))
        self.assertIn('node:fs', skill_tools.NODE_BUILTIN_SAFE)
        self.assertIn('node:fs/promises', skill_tools.NODE_BUILTIN_SAFE)
        self.assertIn('node:path', skill_tools.NODE_BUILTIN_SAFE)
        # 产品行为变化（W8b）：出网/线程/vm 也改归 ACCOUNTED。真实市场 379 个 zip 里
        # 声明过 network/worker_threads/vm 的是 0 个，留成 GATED 等于永久阻断；宿主本身
        # 就是 Node，注册期挡不住代码自己 import()，只挡得住想用它的包。
        self.assertEqual(skill_tools.NODE_BUILTIN_GATED, {})
        self.assertIn('node:net', skill_tools.NODE_BUILTIN_ACCOUNTED)
        # 产品行为变化（W8a）：node:child_process 不再按权限名闸住，改登记为「放行但记账」
        # ——市场上 379 个 zip 里声明 process_spawn 的是 0 个，要求声明等于永久阻断。
        self.assertIn('node:child_process', skill_tools.NODE_BUILTIN_ACCOUNTED)
        self.assertNotIn('node:child_process', skill_tools.NODE_BUILTIN_GATED)
        # 裸内置子路径归一成 node: 前缀，不能被记成 npm 包。
        folder = self.tmp / 'fsp'
        folder.mkdir()
        (folder / 'index.js').write_text("require('fs/promises');", encoding='utf-8')
        usage = skill_tools.ctx_api_usage(str(folder))
        self.assertIn('node:fs/promises', usage)
        self.assertEqual(skill_tools.blocked_capabilities(usage), set())

    def test_host_supported_members_pass(self):
        folder = self.tmp / 'clean'
        folder.mkdir()
        (folder / 'index.js').write_text('api.registerTool({name:"a"}, () => api.config());\n'
                                         'api.log("x"); api.utils.sleep(1);', encoding='utf-8')
        usage = skill_tools.ctx_api_usage(str(folder))
        self.assertEqual(skill_tools.blocked_capabilities(usage), set())

    def test_relative_import_closure_is_scanned(self):
        folder = self.tmp / 'closure'
        folder.mkdir()
        (folder / 'index.js').write_text("const helper = require('./lib/helper');", encoding='utf-8')
        (folder / 'lib').mkdir()
        (folder / 'lib' / 'helper.js').write_text('module.exports = ctx.sender.send;', encoding='utf-8')
        usage = skill_tools.ctx_api_usage(str(folder))
        self.assertIn('sender', usage)

    def test_missing_dir_raises(self):
        with self.assertRaises(skill_tools.SkillToolsError) as caught:
            skill_tools.ctx_api_usage(str(self.tmp / 'nope'))
        self.assertIn('目录不存在', str(caught.exception))


# ── 可注册清单 ───────────────────────────────────────────────────────────────
class EligibleTest(TempCase):
    def setUp(self):
        super().setUp()
        self.manifest = manifest_of(
            skill_entry('clean', tools=['clean__ping']),
            skill_entry('needs-ctx', tools=['needs-ctx__ping']),
            skill_entry('needs-web', tier='T2-web-fetch', tools=['needs-web__ping']),
            skill_entry('host-source', tier='T4-host-source', tools=['host-source__ping']),
            skill_entry('broken', tier='T6-disabled', tools=['broken__ping']),
            skill_entry('extra-api', tier='T3-extra-api', tools=['extra-api__ping']),
        )
        self.install('clean')
        self.install('needs-ctx', 'const onebot = ctx?.onebot; api.registerTool({name:"p"}, () => 1)')
        self.install('needs-web', 'api.registerTool({name:"p"}, () => 1)')
        self.install('host-source')
        self.install('broken')
        self.install('extra-api')

    def run_eligible(self, allow=None):
        with mock.patch.object(skill_tools, 'market_dir', lambda runtime=None: None):
            return skill_tools.eligible_skills(allow=allow, manifest=self.manifest,
                                               runtime=self.runtime)

    def test_default_keeps_t1_and_t2(self):
        # 产品行为变化（Q1=C）：ctx 对象成员不再拦包，只记进 plan['blocked'] 供审计；
        # 真缺的 node:/npm: 仍由 blocked_capabilities 判。needs-ctx 因此进默认清单。
        result = self.run_eligible()
        self.assertEqual(sorted(item['id'] for item in result['eligible']),
                         ['clean', 'needs-ctx', 'needs-web'])
        self.assertEqual(result['counts']['tools'], 3)
        plans = {item['id']: item for item in result['eligible']}
        self.assertEqual(plans['needs-ctx']['blocked'], ['onebot'])
        self.assertEqual(plans['needs-ctx']['released'], [])
        reasons = {item['id']: item['reason'] for item in result['skipped']}
        self.assertIn('结构性不可用', reasons['host-source'])
        self.assertIn('结构性不可用', reasons['broken'])
        self.assertIn('tier T3-extra-api', reasons['extra-api'])
        # T2 不再出现在 skipped 里（这里断言它没被别的闸顺手带出来）。
        self.assertNotIn('needs-web', reasons)

    def test_allow_opens_tier_and_ctx_but_never_t4_t6(self):
        result = self.run_eligible(allow=['needs-web', 'needs-ctx', 'host-source', 'broken'])
        self.assertEqual(sorted(item['id'] for item in result['eligible']),
                         ['clean', 'needs-ctx', 'needs-web'])
        reasons = {item['id']: item['reason'] for item in result['skipped']}
        self.assertIn('结构性不可用', reasons['host-source'])
        self.assertIn('结构性不可用', reasons['broken'])
        self.assertEqual(sorted(item['id'] for item in result['skipped']),
                         ['broken', 'extra-api', 'host-source'])

    def test_requires_and_host_imports_block(self):
        manifest = manifest_of(skill_entry('with-req', tools=['with-req__p'], requires=['onebot']),
                               skill_entry('with-import', tools=['with-import__p'],
                                           hostImports=['../../src/tools.js']))
        self.install('with-req')
        self.install('with-import')
        with mock.patch.object(skill_tools, 'market_dir', lambda runtime=None: None):
            result = skill_tools.eligible_skills(manifest=manifest, runtime=self.runtime)
        reasons = {item['id']: item['reason'] for item in result['skipped']}
        self.assertIn('requires', reasons['with-req'])
        # 有源码可扫时不再按清单的 hostImports 判死：判据是源码本身（见 _skill_plan 注释）。
        self.assertIn('with-import', [item['id'] for item in result['eligible']])

    def test_host_imports_block_when_no_source_to_scan(self):
        manifest = manifest_of(skill_entry('no-source', tools=['no-source__p'],
                                           hostImports=['../../src/tools.js']))
        with mock.patch.object(skill_tools, 'market_dir', lambda runtime=None: None):
            result = skill_tools.eligible_skills(manifest=manifest, runtime=self.runtime)
        reasons = {item['id']: item['reason'] for item in result['skipped']}
        self.assertIn('宿主源码模块', reasons['no-source'])

    def test_tool_name_without_prefix_is_skipped(self):
        manifest = manifest_of(skill_entry('flat', tools=['ping']))
        self.install('flat')
        with mock.patch.object(skill_tools, 'market_dir', lambda runtime=None: None):
            result = skill_tools.eligible_skills(manifest=manifest, runtime=self.runtime)
        self.assertEqual(result['eligible'], [])
        self.assertIn('不是运行时全名', result['skipped'][0]['reason'])


# ── 注册 ────────────────────────────────────────────────────────────────────
MISMATCH_SKILLS = [
    ('global-short-memory-lookup', ['global-short-memory-look__short_memory_query']),
    ('reply-meme_1.2.0', ['reply-meme_1_2_0__find_meme', 'reply-meme_1_2_0__send_meme']),
    ('qq-agent-image-studio-7f3c', ['qq-agent-image-studio-7f__draw',
                                    'qq-agent-image-studio-7f__view_last_generated_image']),
    ('csgo-5e', ['csgo-5e__team_roster']),
]


class RegisterTest(TempCase):
    def setUp(self):
        super().setUp()
        self.env = self.fake_host()
        self.manifest = manifest_of(*[skill_entry(ident, tools=names)
                                      for ident, names in MISMATCH_SKILLS])
        for ident, _names in MISMATCH_SKILLS:
            self.install(ident)

    def register(self, **kwargs):
        options = dict(manifest=self.manifest, runtime=self.runtime, **self.env)
        options.update(kwargs)
        with mock.patch.object(skill_tools, 'market_dir', lambda runtime=None: None):
            return skill_tools.register_skills(**options)

    def test_prefix_mismatch_registers_runtime_full_names(self):
        result = self.register()
        self.assertEqual(result['failed'], [])
        expected = sorted(name for _ident, names in MISMATCH_SKILLS for name in names)
        self.assertEqual(sorted(result['registered'][0]['tools'] +
                                result['registered'][1]['tools'] +
                                result['registered'][2]['tools'] +
                                result['registered'][3]['tools']), expected)
        names = sorted(tool_loop.registry_for(self.runtime).names())
        self.assertEqual(names, expected)
        self.assertEqual(result['counts']['registered'], 4)
        self.assertEqual(result['counts']['registeredTools'], 6)

    def test_tool_spec_keeps_short_raw_name(self):
        self.register()
        spec = tool_loop.registry_for(self.runtime).get('csgo-5e__team_roster')
        self.assertIsNotNone(spec)
        self.assertEqual(spec.source, 'csgo-5e')
        self.assertEqual(spec.raw_name, 'team_roster')
        self.assertEqual(spec.parameters, {'type': 'object', 'properties': {}})
        self.assertTrue(callable(spec.handler))

    def test_allow_is_whitelist(self):
        result = self.register(allow=['csgo-5e'])
        self.assertEqual([item['id'] for item in result['registered']], ['csgo-5e'])
        self.assertIn('不在 allow 白名单里',
                      [item['reason'] for item in result['skipped'] if item['id'] == 'reply-meme_1.2.0'][0])
        self.assertEqual(sorted(tool_loop.registry_for(self.runtime).names()),
                         ['csgo-5e__team_roster'])

    def test_limit_truncates(self):
        result = self.register(limit=1)
        self.assertEqual(len(result['registered']), 1)
        self.assertTrue(any('limit=1 截断' in item['reason'] for item in result['skipped']))

    def test_missing_package_skipped_not_failed(self):
        manifest = manifest_of(skill_entry('not-installed', tools=['not-installed__ping']))
        result = self.register(manifest=manifest)
        self.assertEqual(result['registered'], [])
        self.assertEqual(result['failed'], [])
        self.assertIn('扫不到源码', result['skipped'][0]['reason'])

    def test_package_under_other_root_is_skipped(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        result = self.register(extensions_root=str(elsewhere))
        self.assertEqual(result['registered'], [])
        self.assertEqual(result['failed'], [])
        self.assertTrue(all('没有安装目录' in item['reason'] for item in result['skipped']))

    def test_env_missing_skips_everything(self):
        result = self.register(host_script=str(self.tmp / 'nope.mjs'), source_dir=str(self.tmp))
        self.assertEqual(result['registered'], [])
        self.assertEqual(result['failed'], [])
        self.assertEqual(len(result['skipped']), 4)
        self.assertIn('host-one.mjs', result['counts']['envProblem'])

    def test_one_skill_failure_does_not_block_others(self):
        real = tool_loop.register_tools

        def flaky(runtime, source, tools, handler=None):
            if source == 'csgo-5e':
                raise RuntimeError('坏掉的 skill')
            return real(runtime, source, tools, handler=handler)

        with mock.patch.object(tool_loop, 'register_tools', flaky):
            result = self.register()
        self.assertEqual([item['id'] for item in result['registered']],
                         ['global-short-memory-lookup', 'reply-meme_1.2.0',
                          'qq-agent-image-studio-7f3c'])
        self.assertEqual(len(result['failed']), 1)
        self.assertEqual(result['failed'][0]['id'], 'csgo-5e')
        self.assertIn('坏掉的 skill', result['failed'][0]['error'])
        self.assertNotIn('csgo-5e__team_roster', tool_loop.registry_for(self.runtime).names())

    def test_register_twice_is_idempotent(self):
        self.register()
        before = sorted(tool_loop.registry_for(self.runtime).names())
        second = self.register()
        self.assertEqual(sorted(tool_loop.registry_for(self.runtime).names()), before)
        self.assertEqual(before, sorted(name for _ident, names in MISMATCH_SKILLS for name in names))
        self.assertEqual(second['counts']['registeredTools'], 6)
        self.assertEqual(second['failed'], [])

    def test_audit_is_written_and_redacted(self):
        self.register()
        path = self.tmp / 'data' / 'qq-only' / 'skill-tools-audit.jsonl'
        self.assertTrue(path.is_file())
        lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(item['phase'] == 'register-summary' for item in lines))
        skill_tools._audit(self.runtime, {'phase': 'probe', 'apiKey': 'sk-secret-1',
                                          'nested': {'token': 'tk-2', 'plain': 'ok'}})
        raw = path.read_text(encoding='utf-8')
        self.assertNotIn('sk-secret-1', raw)
        self.assertNotIn('tk-2', raw)
        probe = json.loads(raw.splitlines()[-1])
        self.assertEqual(probe['apiKey'], '***')
        self.assertEqual(probe['nested']['token'], '***')
        self.assertEqual(probe['nested']['plain'], 'ok')

    def test_audit_rotates_over_limit(self):
        path = self.tmp / 'data' / 'qq-only' / 'skill-tools-audit.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x' * 32, encoding='utf-8')
        with mock.patch.object(skill_tools, 'AUDIT_MAX_BYTES', 16):
            skill_tools._audit(self.runtime, {'phase': 'rotate'})
        self.assertTrue(path.with_name('skill-tools-audit.previous.jsonl').is_file())
        self.assertIn('rotate', path.read_text(encoding='utf-8'))


# ── handler ─────────────────────────────────────────────────────────────────
WIKI_SOURCE = """api.registerTool({
  id: 'wiki_query',
  name: '查 GBF 灰机 wiki',
  description: '查询《碧蓝幻想》灰机中文 wiki',
  parameters: {
    type: 'object',
    properties: {
      query: { type: 'string', description: '要查的名字' },
      kind: { type: 'string', enum: ['character', 'weapon'] },
      section: { type: 'string' },
    },
    required: ['query'],
  },
  async execute(ctx, args) { return { ok: true }; },
});
"""


class RegisterSchemaTest(TempCase):
    """注册进工具环的 parameters 必须来自源码（WP5c 修的就是这里）。"""

    def setUp(self):
        super().setUp()
        self.env = self.fake_host()

    def register(self, manifest, **kwargs):
        options = dict(manifest=manifest, runtime=self.runtime, **self.env)
        options.update(kwargs)
        with mock.patch.object(skill_tools, 'market_dir', lambda runtime=None: None):
            return skill_tools.register_skills(**options)

    def test_parameters_extracted_from_source(self):
        self.install('wiki-query', source=WIKI_SOURCE)
        result = self.register(manifest_of(skill_entry('wiki-query', tools=['wiki-query__wiki_query'])))
        self.assertEqual(result['failed'], [])
        self.assertEqual(result['counts']['registeredTools'], 1)
        spec = tool_loop.registry_for(self.runtime).get('wiki-query__wiki_query')
        self.assertIsNotNone(spec)
        self.assertEqual(sorted(spec.parameters['properties']), ['kind', 'query', 'section'])
        self.assertEqual(spec.parameters['required'], ['query'])
        self.assertEqual(spec.parameters['properties']['kind']['enum'], ['character', 'weapon'])

    def test_missing_schema_falls_back_to_empty_object(self):
        self.install('plain')
        result = self.register(manifest_of(skill_entry('plain', tools=['plain__ping'])))
        self.assertEqual(result['failed'], [])
        spec = tool_loop.registry_for(self.runtime).get('plain__ping')
        self.assertEqual(spec.parameters, {'type': 'object', 'properties': {}})

    def test_broken_source_still_registers(self):
        self.install('broken', source='api.registerTool({ id: "broken_ping", parameters: {')
        result = self.register(manifest_of(skill_entry('broken', tools=['broken__broken_ping'])))
        self.assertEqual(result['failed'], [])
        spec = tool_loop.registry_for(self.runtime).get('broken__broken_ping')
        self.assertEqual(spec.parameters, {'type': 'object', 'properties': {}})

    def test_schema_lookup_is_idempotent(self):
        self.install('wiki-query', source=WIKI_SOURCE)
        manifest = manifest_of(skill_entry('wiki-query', tools=['wiki-query__wiki_query']))
        self.register(manifest)
        first = tool_loop.registry_for(self.runtime).get('wiki-query__wiki_query').parameters
        self.register(manifest)
        second = tool_loop.registry_for(self.runtime).get('wiki-query__wiki_query').parameters
        self.assertEqual(first, second)
        self.assertEqual(sorted(second['properties']), ['kind', 'query', 'section'])


class HandlerTest(TempCase):
    def build(self, timeout=5.0, mapping=None, allowed=True):
        return skill_tools.build_tool_handler(
            node='node', host_script=str(self.tmp / 'h.mjs'), source_dir=str(self.tmp),
            extension=str(self.tmp / 'ext'), extension_id='demo',
            mapping=mapping or {'ping': 'demo__ping'}, timeout=timeout,
            data_dir=str(self.tmp))

    def test_success_passes_tool_id_and_arguments(self):
        seen = {}

        async def fake_call_tool(**kwargs):
            seen.update(kwargs)
            return {'ok': True, 'value': {'content': 'pong'}, 'tool': kwargs['tool_id']}

        handler = self.build()
        with mock.patch.object(extension_host, 'call_tool', fake_call_tool):
            value = asyncio.run(handler('ping', {'text': 'hi'}))
        self.assertEqual(value, {'ok': True, 'tool': 'demo__ping',
                                 'result': {'content': 'pong'}})
        self.assertEqual(seen['tool_id'], 'demo__ping')
        self.assertEqual(seen['arguments'], {'text': 'hi'})
        self.assertEqual(seen['allowed_ids'], ['demo__ping'])
        self.assertEqual(seen['extension_id'], 'demo')

    def test_full_name_is_accepted_too(self):
        async def fake_call_tool(**kwargs):
            return {'ok': True, 'value': 1, 'tool': kwargs['tool_id']}

        handler = self.build()
        with mock.patch.object(extension_host, 'call_tool', fake_call_tool):
            value = asyncio.run(handler('demo__ping', {}))
        self.assertEqual(value['tool'], 'demo__ping')
        self.assertTrue(value['ok'])

    def test_timeout_returns_structured_error(self):
        async def slow_call_tool(**kwargs):
            await asyncio.sleep(5)
            return {'ok': True, 'value': {}}

        handler = self.build(timeout=0.2)
        with mock.patch.object(extension_host, 'call_tool', slow_call_tool):
            value = asyncio.run(handler('ping', {}))
        self.assertFalse(value['ok'])
        self.assertEqual(value['error']['code'], 'timeout')
        self.assertIn('工具超时', value['error']['message'])

    def test_error_mapping(self):
        cases = [('timeout after 15s（已强杀子进程）', 'timeout'),
                 ('tool 不在该扩展的白名单里', 'not-allowed'),
                 ('tool id 不属于这个扩展', 'not-owned'),
                 ('no-json (rc=1)', 'extension-error')]
        for message, code in cases:
            async def fake_call_tool(**kwargs):
                return {'ok': False, 'error': message, 'tool': kwargs['tool_id']}

            handler = self.build()
            with mock.patch.object(extension_host, 'call_tool', fake_call_tool):
                value = asyncio.run(handler('ping', {}))
            self.assertEqual(value['error']['code'], code)
            self.assertEqual(value['error']['message'], message)

    def test_handler_exception_is_structured(self):
        async def boom_call_tool(**kwargs):
            raise OSError('宿主进程起不来')

        handler = self.build()
        with mock.patch.object(extension_host, 'call_tool', boom_call_tool):
            value = asyncio.run(handler('ping', {}))
        self.assertFalse(value['ok'])
        self.assertEqual(value['error']['code'], 'handler-error')
        self.assertIn('宿主进程起不来', value['error']['message'])

    def test_non_dict_arguments_become_empty_payload(self):
        seen = {}

        async def fake_call_tool(**kwargs):
            seen.update(kwargs)
            return {'ok': True, 'value': {}, 'tool': kwargs['tool_id']}

        handler = self.build()
        with mock.patch.object(extension_host, 'call_tool', fake_call_tool):
            asyncio.run(handler('ping', 'not-a-dict'))
        self.assertEqual(seen['arguments'], {})


# ── 真实市场（有就测，没有就跳） ──────────────────────────────────────────────
class MarketTest(TempCase):
    def setUp(self):
        super().setUp()
        self.market = skill_tools.market_dir()

    def test_market_zip_resolves_by_id(self):
        if not self.market:
            self.skipTest('本机没有 Skill 市场目录')
        path = skill_tools.market_zip('wiki-query')
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).is_file())
        self.assertIsNone(skill_tools.market_zip('no-such-skill-xyz'))

    def test_install_real_zip_without_zip_path(self):
        if not self.market:
            self.skipTest('本机没有 Skill 市场目录')
        info = skill_tools.install_skill('wiki-query', target_dir=str(self.ext))
        self.assertTrue(info['ok'])
        self.assertTrue(info['manifestIdMatches'])
        self.assertTrue((self.ext / 'wiki-query' / 'index.js').is_file())


if __name__ == '__main__':
    unittest.main()
