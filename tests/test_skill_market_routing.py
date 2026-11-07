"""Market browsing, eligibility and installation must use the same runtime source."""
import json
import os
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import skill_market, skill_tools


class SkillMarketRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.default = self.root / 'default'
        self.default.mkdir()
        self.configured = self.root / 'configured'
        self.configured.mkdir()
        self.runtime = types.SimpleNamespace(
            data_root=self.root / 'runtime',
            config={'qq_only': {'skill_market_dir': str(self.configured)}})
        for patcher in (
            mock.patch.dict(os.environ, {'QQAGENT_SKILL_MARKET': '', 'SKILL_MARKET_DIR': ''}),
            mock.patch.object(skill_market, 'DEFAULT_MARKET_DIRS', (self.default,)),
            mock.patch.object(skill_market, '_repo_root', return_value=self.root / 'repo'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manifest = {'skills': [{
            'id': 'sample', 'tier': 'T1-ready',
            'tools': [{'name': 'sample__ping'}],
            'permissions': [], 'requires': [], 'hostImports': [],
        }]}

    def package(self, folder, *, blocked=False):
        # blocked 用**不存在的 npm 包**当对照组。原来用 node:net，但政策改过：
        # 出网/线程/vm 与 child_process 一样归 NODE_BUILTIN_ACCOUNTED（放行但记账），
        # 市场里 0 个包声明过 network，留成 GATED 等于永久阻断。npm 包缺失才是真阻断。
        path = folder / '中文名称-sample.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('sample/skill.json', json.dumps({'id': 'sample'}))
            archive.writestr('sample/index.js',
                             "require('no-such-package-xyz');\n"
                             'api.registerTool({name:"ping"}, () => "pong");' if blocked else
                             'api.registerTool({name:"ping"}, () => "pong");')
        return path

    def eligible(self, runtime=None, **kwargs):
        return skill_tools.eligible_skills(
            manifest=self.manifest, runtime=runtime or self.runtime, **kwargs)

    def test_configured_market_controls_eligibility(self):
        self.package(self.default, blocked=True)
        selected = self.package(self.configured)
        browsing = skill_market.list_market(runtime=self.runtime)
        result = self.eligible()
        self.assertEqual(Path(browsing['market']), self.configured)
        self.assertEqual(result['counts']['market'], str(self.configured))
        self.assertEqual(result['counts']['tools'], 1)
        self.assertEqual(result['eligible'][0]['source'], 'zip:' + str(selected))
        self.assertEqual(result['skipped'], [])

    def test_summary_uses_same_market_as_browsing(self):
        self.package(self.default, blocked=True)
        self.package(self.configured)
        path = skill_tools.data_dir(self.runtime) / skill_tools.MANIFEST_FILE
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.manifest), encoding='utf-8')
        result = skill_market.summary(runtime=self.runtime)
        self.assertEqual(result['marketDir'], str(self.configured))
        self.assertEqual(result['marketCount'], 1)
        self.assertEqual(result['eligible'], 1)
        self.assertEqual(result['blocked'], 0)

    def test_two_runtimes_do_not_share_configured_source(self):
        self.package(self.configured)
        second_market = self.root / 'second-market'
        second_market.mkdir()
        self.package(second_market, blocked=True)
        second = types.SimpleNamespace(
            data_root=self.root / 'second-runtime',
            config={'qq_only': {'skill_market_dir': str(second_market)}})
        for runtime, expected in ((self.runtime, 1), (second, 0), (self.runtime, 1)):
            result = self.eligible(runtime)
            self.assertEqual(result['counts']['tools'], expected)
            self.assertEqual(result['counts']['market'], runtime.config['qq_only']['skill_market_dir'])

    def test_empty_configured_directory_does_not_select_default_packages(self):
        self.package(self.default)
        result = self.eligible()
        self.assertEqual(result['counts']['market'], str(self.configured))
        self.assertEqual(result['eligible'], [])
        self.assertEqual(len(result['skipped']), 1)
        self.assertIn('zip', result['skipped'][0]['reason'])
        self.assertEqual(list(self.configured.iterdir()), [])

    def test_explicit_market_overrides_runtime_config(self):
        self.package(self.configured, blocked=True)
        selected = self.package(self.default)
        self.assertEqual(self.eligible(market=self.default)['counts']['tools'], 1)
        self.assertEqual(skill_tools.market_zip('sample', market=self.default,
                                               runtime=self.runtime), selected)

    def test_environment_priority_is_shared_with_market_browser(self):
        self.package(self.default, blocked=True)
        selected = self.package(self.configured)
        # Legacy environment takes precedence over runtime config; canonical wins over legacy.
        with mock.patch.dict(os.environ, {'SKILL_MARKET_DIR': str(self.default)}):
            self.assertEqual(self.eligible()['counts']['tools'], 0)
            with mock.patch.dict(os.environ, {'QQAGENT_SKILL_MARKET': str(self.configured)}):
                self.assertEqual(skill_tools.market_zip('sample', runtime=self.runtime), selected)
                self.assertEqual(self.eligible()['counts']['tools'], 1)
                self.assertEqual(skill_tools.market_dir(self.runtime),
                                 skill_market.market_dir(runtime=self.runtime))

    def test_install_uses_runtime_market_and_runtime_data_root(self):
        self.package(self.default, blocked=True)
        selected = self.package(self.configured)
        self.assertEqual(skill_tools.market_zip('sample', runtime=self.runtime), selected)
        result = skill_tools.install_skill('sample', runtime=self.runtime)
        target = skill_tools.extensions_dir(self.runtime) / 'sample'
        self.assertTrue(result['ok'])
        self.assertEqual(Path(result['dir']), target)
        self.assertIn('pong', (target / 'index.js').read_text(encoding='utf-8'))
        self.assertFalse((self.configured / 'sample').exists())

    def test_explicit_zip_and_target_keep_precedence(self):
        self.package(self.configured, blocked=True)
        selected = self.package(self.default)
        target = self.root / 'explicit-extensions'
        result = skill_tools.install_skill('sample', selected, target, runtime=self.runtime)
        self.assertEqual(Path(result['dir']), target / 'sample')
        self.assertIn('pong', (target / 'sample/index.js').read_text(encoding='utf-8'))
        self.assertFalse(skill_tools.extensions_dir(self.runtime).exists())

    def test_no_argument_call_remains_supported(self):
        selected = self.package(self.default)
        self.assertEqual(skill_tools.market_dir(), self.default)
        self.assertEqual(skill_tools.market_zip('sample'), selected)

    def test_no_market_does_not_create_configured_directory(self):
        self.default.rmdir()  # Empty test fixture only.
        missing = self.root / 'missing-market'
        self.runtime.config['qq_only']['skill_market_dir'] = str(missing)
        self.assertIsNone(skill_tools.market_dir(self.runtime))
        self.assertEqual(self.eligible()['counts']['market'], '')
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
