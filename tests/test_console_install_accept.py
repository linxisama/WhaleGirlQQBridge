"""控制台安装验收脚本的守卫测试：抽层形状 + 层级规则与源码一致 + 真 HTTP 安装一例。

真 HTTP 那一例是进程内 uvicorn + httpx 真 socket（同 tools/accept_console_install.py），
只装 1 个 P1 插件和 1 个 T4 skill，不跑 512 个市场包。
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / 'tools' / 'accept_console_install.py'
SWEEP = ROOT / '.tmp' / 'inv' / 'sweep-report.json'
PLUGIN_TOOLS = ROOT / '.tmp' / 'inv' / 'plugin-tools.json'
SKILL_TOOLS = ROOT / '.tmp' / 'inv' / 'skill-tools.json'


def load_script():
    spec = importlib.util.spec_from_file_location('accept_console_install', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACCEPT = load_script()


def have_inventory() -> bool:
    return all(path.is_file() for path in (SWEEP, PLUGIN_TOOLS, SKILL_TOOLS))


class PlanShapeTests(unittest.TestCase):
    """抽样形状（读清单，不联网、不起应用）。"""

    def setUp(self):
        if not have_inventory():
            self.skipTest('缺 .tmp/inv 清单，先跑清点工具')

    def test_plugin_plan_counts(self):
        cases, notes = ACCEPT.build_plugin_cases(0, 1525)
        self.assertEqual(notes, [])
        self.assertEqual(len(cases), 61)
        counts = {}
        for case in cases:
            counts[case['class']] = counts.get(case['class'], 0) + 1
        # P3 多出来的 1 个是边界样例 bili-autopilot（本身也是 P3-host-source）。
        self.assertEqual(counts, {'P1-usable': 20, 'P3-host-source': 31, 'P2-web-fetch': 3,
                                 'P4-depends-other': 3, 'P6-declarative': 3,
                                 'P5-electron': 1})
        ids = [case['id'] for case in cases]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertIn('long-image-reader', ids)
        self.assertIn('bili-autopilot', ids)
        boundary = [case for case in cases if case['boundary']]
        self.assertEqual(len(boundary), 2)
        # 已知边界的探针都是坏的，脚本必须带着"预期失败"的预期去跑，而不是当成功。
        self.assertTrue(all(case['expect_probe'] is False for case in boundary))

    def test_skill_plan_counts_and_zips(self):
        cases, notes = ACCEPT.build_skill_cases(0, 1525)
        self.assertEqual(len(cases), 20)
        self.assertEqual(notes, [])
        classes = {}
        for case in cases:
            classes[case['class']] = classes.get(case['class'], 0) + 1
            self.assertTrue(Path(case['zip']).is_file(), case['zip'])
        self.assertIn('wiki-query', [case['id'] for case in cases])
        self.assertGreaterEqual(classes.get('T4-host-source', 0), 3)
        self.assertGreaterEqual(classes.get('T6-disabled', 0), 1)

    def test_limit_shrinks_each_class(self):
        cases, _notes = ACCEPT.build_plugin_cases(1, 7)
        self.assertEqual(len(cases), 7)   # 5 类各 1 个 + 2 个边界
        self.assertEqual(len([c for c in cases if c['boundary']]), 2)


class RuleConsistencyTests(unittest.TestCase):
    """脚本里的层级口径必须和被测源码一致，源码改了这里先红。"""

    def test_blocked_tiers_match_source(self):
        from src import skill_market
        from src import qq_app
        self.assertEqual(tuple(ACCEPT.BLOCKED_TIERS), tuple(qq_app._EXTENSION_BLOCKED_TIERS))
        self.assertEqual(tuple(ACCEPT.BLOCKED_TIERS), tuple(skill_market.BLOCKED_TIERS))

    def test_tier_map_covers_inventory(self):
        if not have_inventory():
            self.skipTest('缺 .tmp/inv 清单')
        tiers = {str(item.get('tier')) for item in
                 json.loads(PLUGIN_TOOLS.read_text(encoding='utf-8')).get('plugins') or []}
        self.assertTrue(tiers)
        self.assertLessEqual(tiers, set(ACCEPT.TIER_MAP))
        # P1/P3/P6 是唯一允许集，不许被 TIER_ALSO_OK 放水。
        self.assertFalse(set(ACCEPT.TIER_ALSO_OK) & {'P1-usable', 'P3-host-source',
                                                     'P6-declarative'})


class LiveInstallTests(unittest.IsolatedAsyncioTestCase):
    """真起 FastAPI、真下载真解压的一幕：1 个 P1 插件 + 1 个 T4 skill。"""

    @classmethod
    def setUpClass(cls):
        if not have_inventory():
            raise unittest.SkipTest('缺 .tmp/inv 清单')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / 'data-root'
        cls.root.mkdir(parents=True)
        snapshot = cls.root / 'data' / 'qq-only'
        snapshot.mkdir(parents=True)
        (snapshot / 'skill-tools.json').write_text(
            SKILL_TOOLS.read_text(encoding='utf-8'), encoding='utf-8')
        cls.sweep = {str(item['id']): item for item in
                     json.loads(SWEEP.read_text(encoding='utf-8'))['items']}
        cls.skill_zips = ACCEPT._market_zip_index(
            Path(r'D:\聊天机器人\插件市场\Skill市场'))
        repo_data = ROOT / 'data' / 'qq-only'
        cls.repo_before = ACCEPT._snapshot(repo_data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        args = type('Args', (), {'timeout': 90.0, 'short': 20.0, 'install_timeout': 120.0,
                                 'retries': 1, 'limit': 1, 'seed': 1525})()
        self.args = args
        self.console = ACCEPT.Console(self.root, args.timeout)
        await self.console.start()

    async def asyncTearDown(self):
        await self.console.stop()

    async def test_host_endpoint_is_real(self):
        status, body, ms = await self.console.get('/api/extensions/host', timeout=20)
        self.assertEqual(status, 200)
        self.assertTrue(body.get('success'))
        # 首次询问时宿主根还没准备，这不是失败，是"按需准备"。
        self.assertIn('root', body)
        self.assertIn('prepared', body)
        self.assertIsInstance(ms, int)

    async def test_install_one_plugin_and_one_skill(self):
        plugin_cases, _notes = ACCEPT.build_plugin_cases(1, 1525)
        plugin = next(case for case in plugin_cases if case['class'] == 'P1-usable')
        record = await ACCEPT._plugin_case(self.console, plugin, self.args)
        if record.get('failureClass') == 'network':
            self.skipTest('市场 CDN 不可达：%s' % '；'.join(record['failures']))
        self.assertTrue(record['ok'], record['failures'])
        self.assertEqual(record['tier'], 'T1-ready')
        self.assertTrue(record['enabled'])
        self.assertTrue((Path(record['hostRoot']) / 'plugins' / plugin['id']).is_dir())
        self.assertTrue(record['hostGet']['ok'])
        self.assertEqual(record['probeOk'], plugin['expect_probe'])

        skill_cases, _notes = ACCEPT.build_skill_cases(1, 1525)
        skill = next(case for case in skill_cases if case['class'] == 'T4-host-source')
        record = await ACCEPT._skill_case(self.console, skill, self.args)
        self.assertTrue(record['ok'], record['failures'])
        self.assertEqual(record['tier'], 'T4-host-source')
        self.assertFalse(record['enabled'])
        self.assertIn('未启用', record['note'])
        self.assertTrue(record['installedListing']['found'])

        # 硬性要求：绝不动仓库与用户的真实数据目录。
        self.assertEqual(ACCEPT._snapshot(ROOT / 'data' / 'qq-only'), self.repo_before)


if __name__ == '__main__':
    unittest.main()
