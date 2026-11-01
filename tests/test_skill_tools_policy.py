"""Skill 工具注册期闸门政策（W8a）单元测试。

本机 venv 没装 pytest，用 unittest 写（pytest 也能收集）。W8a 三道闸的口径都钉在这里：
默认 tier 加进 T2-web-fetch、node:child_process 从「要包声明 process_spawn」改成「放行但
记账」、fetch 视为宿主已提供；同时钉住没动的部分（T3/T5 仍不放行、T4/T6 结构性不可用、
NODE_BUILTIN_GATED 仍要声明权限名）以及账本字段（注册审计的 released）。
"""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src import skill_tools, tool_loop


SKILL_TEMPLATE = {'name': '示例', 'version': '1.0.0'}
SPAWN_SOURCE = ("const { execSync } = require('node:child_process');\n"
                "api.registerTool({ name: 'ping' }, () => ({ out: execSync('echo hi') }));")
FETCH_SOURCE = ("api.registerTool({ name: 'web' }, () =>"
                " api.fetch('https://example.com/data.json'));")
PLAIN_SOURCE = 'api.registerTool({ name: "ping" }, () => ({ ok: true }));'


def write_zip(path, files):
    with zipfile.ZipFile(str(path), 'w') as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


def skill_zip(path, skill_id, source=PLAIN_SOURCE):
    return write_zip(path, {
        '%s/skill.json' % skill_id: json.dumps(dict(SKILL_TEMPLATE, id=skill_id),
                                               ensure_ascii=False),
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
                 'tools': [{'name': name, 'description': '', 'hasSchema': True}
                           for name in tools],
                 'permissions': [], 'requires': [], 'hostImports': []}, **extra)


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.ext = self.tmp / 'data' / 'qq-only' / 'skill_extensions'
        self.runtime = FakeRuntime(self.tmp)

    def install(self, skill_id, source=None):
        archive = skill_zip(self.tmp / ('%s.zip' % skill_id), skill_id, source or PLAIN_SOURCE)
        return skill_tools.install_skill(skill_id, str(archive), str(self.ext))

    def fake_host(self):
        """造一份假的宿主环境（node/host-one.mjs/0.4 源码树），只骗过存在性检查。"""
        host = self.tmp / 'fake-host-one.mjs'
        host.write_text('// fake', encoding='utf-8')
        source = self.tmp / 'fake-qq-agent-run'
        (source / 'src').mkdir(parents=True, exist_ok=True)
        (source / 'src' / 'plugin-loader.js').write_text('// fake', encoding='utf-8')
        return {'node': 'node', 'host_script': str(host), 'source_dir': str(source)}

    def audit_rows(self):
        path = self.tmp / 'data' / 'qq-only' / 'skill-tools-audit.jsonl'
        return [json.loads(line) for line in
                path.read_text(encoding='utf-8').splitlines() if line.strip()]


# ── 政策常量与判定 ──────────────────────────────────────────────────────────
class TierPolicyTest(TempCase):
    def test_default_tiers_open_t2_and_nothing_else(self):
        self.assertEqual(skill_tools.DEFAULT_TIERS, ('T1-ready', 'T2-web-fetch'))
        for tier in ('T3-extra-api', 'T4-host-source', 'T5-depends-other', 'T6-disabled'):
            self.assertNotIn(tier, skill_tools.DEFAULT_TIERS)

    def test_child_process_is_accounted_not_gated(self):
        self.assertIn('node:child_process', skill_tools.NODE_BUILTIN_ACCOUNTED)
        self.assertNotIn('node:child_process', skill_tools.NODE_BUILTIN_GATED)
        self.assertNotIn('node:child_process', skill_tools.GATED_BY_PERMISSION)
        # 声明与否都放行：市场里没有任何包声明 process_spawn，不该再要它。
        self.assertEqual(skill_tools.blocked_capabilities(['node:child_process']), set())
        self.assertEqual(skill_tools.blocked_capabilities(['node:child_process'],
                                                          ['process_spawn']), set())

    def test_fetch_is_host_provided(self):
        self.assertIn('fetch', skill_tools.HOST_PROVIDED_CTX)
        self.assertIn('fetch', skill_tools.HOST_SUPPORTED_CTX)
        self.assertNotIn('fetch', skill_tools.GATED_BY_PERMISSION)
        self.assertEqual(skill_tools.blocked_capabilities(['fetch']), set())
        self.assertEqual(skill_tools.blocked_capabilities(['fetch'], ['web_fetch']), set())

    def test_released_accounted_covers_exactly_the_two_gates(self):
        self.assertEqual(set(skill_tools.RELEASED_ACCOUNTED),
                         {'node:child_process', 'fetch'})
        self.assertFalse(skill_tools.RELEASED_ACCOUNTED & set(skill_tools.NODE_BUILTIN_GATED))

    def test_gated_builtin_still_needs_its_permission(self):
        # 闸没死：GATED 档仍然要包自己声明权限名。
        self.assertEqual(skill_tools.blocked_capabilities(['node:net']), {'node:net'})
        self.assertEqual(skill_tools.blocked_capabilities(['node:net'], ['network']), set())
        self.assertEqual(skill_tools.blocked_capabilities(['node:vm'], ['network']),
                         {'node:vm'})

    def test_other_blockers_behave_as_before(self):
        # 未声明 web_fetch 的包：其它 ctx 成员的判定与改动前逐字一致。
        self.assertEqual(skill_tools.blocked_capabilities(['onebot', 'store']),
                         {'onebot', 'store'})
        # fetch 不再新增阻断，也不会顺带解锁同一包里别的缺失成员。
        self.assertEqual(skill_tools.blocked_capabilities(['fetch', 'onebot']), {'onebot'})
        self.assertIn('onebot', skill_tools.HOST_MISSING_CONTEXT)
        self.assertNotIn('fetch', skill_tools.HOST_MISSING_CONTEXT)

    def test_structural_and_undecided_tiers(self):
        self.assertEqual(set(skill_tools.BLOCKED_TIERS),
                         {'T4-host-source', 'T6-disabled'})
        for tier in ('T4-host-source', 'T6-disabled'):
            self.assertIn('结构性不可用', skill_tools.BLOCKED_TIERS[tier])
        self.assertFalse(set(skill_tools.BLOCKED_TIERS) & set(skill_tools.DEFAULT_TIERS))


# ── eligible_skills 端到端 ──────────────────────────────────────────────────
class EligiblePolicyTest(TempCase):
    def setUp(self):
        super().setUp()
        self.manifest = manifest_of(
            skill_entry('clean', tools=['clean__ping']),
            skill_entry('web', tier='T2-web-fetch', tools=['web__ping']),
            skill_entry('extra-api', tier='T3-extra-api', tools=['extra-api__ping']),
            skill_entry('host-source', tier='T4-host-source', tools=['host-source__ping']),
            skill_entry('depends-other', tier='T5-depends-other', tools=['depends-other__ping']),
            skill_entry('broken', tier='T6-disabled', tools=['broken__ping']),
        )
        for skill in self.manifest['skills']:
            self.install(skill['id'])

    def run_eligible(self, allow=None):
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            return skill_tools.eligible_skills(allow=allow, manifest=self.manifest,
                                               runtime=self.runtime)

    def test_default_opens_t1_and_t2_only(self):
        result = self.run_eligible()
        self.assertEqual(sorted(item['id'] for item in result['eligible']),
                         ['clean', 'web'])
        self.assertEqual(result['counts']['tools'], 2)
        reasons = {item['id']: item['reason'] for item in result['skipped']}
        self.assertIn('tier T3-extra-api', reasons['extra-api'])
        self.assertIn('tier T5-depends-other', reasons['depends-other'])
        self.assertIn('结构性不可用', reasons['host-source'])
        self.assertIn('结构性不可用', reasons['broken'])

    def test_allow_still_never_opens_t4_t6(self):
        # allow 是额外放行（覆盖 tier 与 ctx 闸，W8a 没改这个口径），但 T4/T6 是结构性
        # 不可用，allow 也不行。
        result = self.run_eligible(allow=['extra-api', 'depends-other',
                                          'host-source', 'broken'])
        self.assertEqual(sorted(item['id'] for item in result['eligible']),
                         ['clean', 'depends-other', 'extra-api', 'web'])
        reasons = {item['id']: item['reason'] for item in result['skipped']}
        self.assertEqual(sorted(reasons), ['broken', 'host-source'])
        self.assertIn('结构性不可用', reasons['host-source'])
        self.assertIn('结构性不可用', reasons['broken'])


class ReleasedPlanTest(TempCase):
    def setUp(self):
        super().setUp()
        self.manifest = manifest_of(
            skill_entry('spawner', tools=['spawner__ping']),
            skill_entry('fetcher', tools=['fetcher__web']),
            skill_entry('plain', tools=['plain__ping']),
        )
        self.install('spawner', SPAWN_SOURCE)
        self.install('fetcher', FETCH_SOURCE)
        self.install('plain', PLAIN_SOURCE)

    def plan(self):
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            return skill_tools.eligible_skills(manifest=self.manifest, runtime=self.runtime)

    def test_eligible_records_released_members(self):
        result = self.plan()
        self.assertEqual(sorted(item['id'] for item in result['eligible']),
                         ['fetcher', 'plain', 'spawner'])
        released = {item['id']: item['released'] for item in result['eligible']}
        self.assertEqual(released['spawner'], ['node:child_process'])
        self.assertEqual(released['fetcher'], ['fetch'])
        self.assertEqual(released['plain'], [])


# ── 注册期账本 ──────────────────────────────────────────────────────────────
class RegisterAuditTest(TempCase):
    def setUp(self):
        super().setUp()
        self.env = self.fake_host()
        self.install('spawner', SPAWN_SOURCE)
        self.install('web', FETCH_SOURCE)
        self.install('plain', PLAIN_SOURCE)

    def register(self, manifest):
        options = dict(manifest=manifest, runtime=self.runtime, **self.env)
        with mock.patch.object(skill_tools, 'market_dir', lambda: None):
            return skill_tools.register_skills(**options)

    def test_register_audit_marks_released_gate(self):
        result = self.register(manifest_of(
            skill_entry('spawner', tools=['spawner__ping'])))
        self.assertEqual(result['failed'], [])
        self.assertEqual(result['registered'][0]['released'], ['node:child_process'])
        rows = self.audit_rows()
        register_rows = [row for row in rows if row.get('phase') == 'register']
        self.assertEqual(len(register_rows), 1)
        row = register_rows[0]
        self.assertEqual(row['skill'], 'spawner')
        self.assertEqual(row['tier'], 'T1-ready')
        self.assertEqual(row['released'], ['node:child_process'])
        self.assertTrue(row['ok'])
        summary = [item for item in rows if item.get('phase') == 'register-summary'][-1]
        self.assertEqual(summary['released'], {'spawner': ['node:child_process']})
        # 账本按 skill id 认包：可与 qqagent_bridge_calls.jsonl 的 extension 列对齐；
        # source 记的是安装位置。
        self.assertTrue(row['source'].startswith('installed:'))
        self.assertEqual(row['tools'], ['spawner__ping'])
        self.assertEqual(sorted(tool_loop.registry_for(self.runtime).names()),
                         ['spawner__ping'])

    def test_plain_skill_has_no_released_field(self):
        result = self.register(manifest_of(skill_entry('plain', tools=['plain__ping'])))
        self.assertEqual(result['registered'][0]['released'], [])
        rows = self.audit_rows()
        register_rows = [row for row in rows if row.get('phase') == 'register']
        self.assertEqual(len(register_rows), 1)
        self.assertNotIn('released', register_rows[0])
        self.assertEqual(register_rows[0]['tier'], 'T1-ready')
        summary = [item for item in rows if item.get('phase') == 'register-summary'][-1]
        self.assertNotIn('released', summary)

    def test_register_counts_with_t2_default(self):
        result = self.register(manifest_of(
            skill_entry('spawner', tools=['spawner__ping']),
            skill_entry('web', tier='T2-web-fetch', tools=['web__ping']),
            skill_entry('host-source', tier='T4-host-source', tools=['host-source__ping'])))
        self.assertEqual(result['failed'], [])
        self.assertEqual(sorted(item['id'] for item in result['registered']),
                         ['spawner', 'web'])
        self.assertEqual(result['counts']['registered'], 2)
        self.assertEqual(result['counts']['registeredTools'], 2)
        self.assertEqual(result['counts']['skipped'], 1)
        summary = [row for row in self.audit_rows()
                   if row.get('phase') == 'register-summary'][-1]
        self.assertEqual(summary['released'],
                         {'spawner': ['node:child_process'], 'web': ['fetch']})
        self.assertEqual(summary['counts']['registeredTools'], 2)


if __name__ == '__main__':
    unittest.main()
