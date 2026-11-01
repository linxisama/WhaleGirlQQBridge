"""一键登记三个桥（记忆 + 控制 + 工具）：临时 DSH home、幂等、只撤工具桥、--status 看得到第三条。"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml

from src import bot_control_api as control_api
from src import dsh_memory_bridge as bridge
from tools import setup_dsh_memory as setup

MEMORY = 'mcp-whale-memory'
CONTROL = 'mcp-whale-bot'
TOOLS = 'mcp-whale-tools'


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dsh = Path(self.tmp.name) / '.dsh'
        self.bot_home = Path(self.tmp.name) / 'bot'
        self.path = bridge.profile_path(self.dsh)

    def run_cli(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(['--dsh-home', str(self.dsh),
                               '--bot-home', str(self.bot_home), *extra])
        self.assertEqual(code, 0)
        return out.getvalue()

    def install(self, *extra):
        """跑安装，返回 (payload 列表, 关键行)。结果里混着一条提示行，只取 JSON 部分。"""
        text = self.run_cli(*extra)
        return json.loads(text[:text.rindex(']') + 1])

    def entries(self, path=None):
        target = path or self.path
        return yaml.safe_load(target.read_text(encoding='utf-8')) or []

    def rows(self, path=None):
        """展开 insert 块后的登记行（脚本怎么写块不作断言，只关心登记内容）。"""
        out = []
        for item in self.entries(path):
            nested = item.get('insert') if isinstance(item, dict) else None
            if isinstance(nested, list):
                out.extend(row for row in nested if isinstance(row, dict))
            elif isinstance(item, dict):
                out.append(item)
        return out

    def entry(self, entry_id, path=None):
        return next(row for row in self.rows(path) if row.get('id') == entry_id)

    def bare_ids(self, path=None):
        return [item['id'] for item in self.entries(path)
                if isinstance(item, dict) and 'insert' not in item]

    def ids(self, path=None):
        return [row['id'] for row in self.rows(path)]

    def backups(self):
        return sorted(self.path.parent.glob('cordis.patch.yml.bak-whale-memory-*'))


class DefaultRegisterTest(SetupCase):
    def test_one_command_registers_three_bridges(self):
        self.install()
        self.assertEqual(self.ids(), [MEMORY, CONTROL, TOOLS])
        tools = self.entry(TOOLS)['config']
        self.assertEqual(tools['serverName'], 'whale_tools')
        self.assertEqual(tools['transport'], 'stdio')
        self.assertEqual(tools['toolCallTimeoutMs'], 30000)
        self.assertEqual(Path(tools['args'][0]).name, 'whale_tools_mcp.py')
        self.assertEqual(tools['env']['WHALE_QQ_HOME'], str(self.bot_home))
        self.assertEqual(tools['env']['PYTHONUTF8'], '1')

    def test_install_writes_an_insert_block_not_bare_rows(self):
        """DSH 只认 - insert: 下的新增行，裸 - id: 会被静默丢弃。"""
        self.install()
        data = self.entries()
        self.assertEqual(len(data), 1)
        self.assertEqual(list(data[0]), ['insert'])
        self.assertEqual([row['id'] for row in data[0]['insert']], [MEMORY, CONTROL, TOOLS])
        self.assertEqual(self.bare_ids(), [])

    def test_install_output_reports_every_bridge(self):
        payload = self.install()
        self.assertEqual(len(payload), 1)
        result = payload[0]
        self.assertTrue(result['changed'])
        self.assertTrue(result['installed'])
        self.assertTrue(result['control_installed'])
        self.assertTrue(result['tools_installed'])
        self.assertEqual(result['serverName'], 'whale_memory')
        self.assertEqual(result['controlServerName'], 'whale_bot')
        self.assertEqual(result['toolsServerName'], 'whale_tools')

    def test_second_run_is_idempotent_and_leaves_no_backup(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: other\n  name: "@x/y"\n  config: {}\n', encoding='utf-8')
        self.install()
        self.assertEqual(len(self.backups()), 1)   # 三条共用一次备份
        first = self.path.read_text(encoding='utf-8')
        payload = self.install()
        self.assertFalse(payload[0]['changed'])
        self.assertIsNone(payload[0]['backup'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), first)
        self.assertEqual(len(self.backups()), 1)   # 第二次运行不留第二份备份

    def test_legacy_bare_rows_are_migrated(self):
        """用户 profile 里旧版本写下的裸行：重跑一次安装就搬进 insert 块。"""
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: mcp-whale-memory\n  name: "@deepseek-ai/dsh-mcp-client"\n'
                             '  config:\n    serverName: whale_memory\n'
                             '- id: mcp-whale-bot\n  name: "@deepseek-ai/dsh-mcp-client"\n'
                             '- id: mcp-whale-tools\n  name: "@deepseek-ai/dsh-mcp-client"\n',
                             encoding='utf-8')
        self.assertEqual(self.bare_ids(), [MEMORY, CONTROL, TOOLS])
        self.install()
        self.assertEqual(self.bare_ids(), [])
        data = self.entries()
        self.assertEqual(len(data), 1)
        self.assertEqual([row['id'] for row in data[0]['insert']], [MEMORY, CONTROL, TOOLS])
        self.assertEqual(self.ids(), [MEMORY, CONTROL, TOOLS])

    def test_existing_foreign_entry_is_kept(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: mcp-snowluma\n  name: "@x/y"\n  config: {}\n',
                             encoding='utf-8')
        self.install()
        self.assertEqual(self.ids(), ['mcp-snowluma', MEMORY, CONTROL, TOOLS])
        self.assertEqual(len(self.backups()), 1)


class OptOutTest(SetupCase):
    def test_no_tools_writes_only_two(self):
        self.install('--no-tools')
        self.assertEqual(self.ids(), [MEMORY, CONTROL])

    def test_bot_only_skips_tools(self):
        self.install('--bot-only')
        self.assertEqual(self.ids(), [CONTROL])

    def test_no_bot_still_registers_tools(self):
        self.install('--no-bot')
        self.assertEqual(self.ids(), [MEMORY, TOOLS])


class RemoveTest(SetupCase):
    def test_remove_tools_keeps_the_other_two(self):
        self.install()
        out = self.run_cli('--remove-tools')
        self.assertIn('"removed": true', out)
        self.assertEqual(self.ids(), [MEMORY, CONTROL])
        again = json.loads(self.run_cli('--remove-tools'))
        self.assertFalse(again['removed'])
        self.assertFalse(again['changed'])
        self.assertEqual(self.ids(), [MEMORY, CONTROL])

    def test_remove_tools_on_empty_profile_is_a_noop(self):
        payload = json.loads(self.run_cli('--remove-tools'))
        self.assertFalse(payload['removed'])
        self.assertFalse(self.path.exists())

    def test_remove_takes_all_three_and_keeps_foreign(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: other\n  name: "@x/y"\n  config: {}\n', encoding='utf-8')
        self.install()
        self.run_cli('--remove')
        self.assertEqual(self.ids(), ['other'])


class DiagnosticsTest(SetupCase):
    def test_status_shows_the_third_entry(self):
        self.install()
        status = json.loads(self.run_cli('--status'))
        self.assertTrue(status['installed'])
        self.assertTrue(status['control_installed'])
        self.assertTrue(status['tools_installed'])
        self.assertEqual(status['toolsServerName'], 'whale_tools')
        self.assertEqual(status['tools_timeout_ms'], 30000)
        self.assertEqual(Path(status['tools_script']).name, 'whale_tools_mcp.py')
        self.assertEqual(status['entry_count'], 3)
        self.assertEqual(status['insert_blocks'], 1)
        self.assertEqual(status['bare_entry_ids'], [])

    def test_status_sees_tools_go_away(self):
        self.install()
        self.run_cli('--remove-tools')
        status = json.loads(self.run_cli('--status'))
        self.assertTrue(status['installed'])
        self.assertFalse(status['tools_installed'])
        self.assertIsNone(status['toolsServerName'])
        self.assertEqual(status['entry_count'], 2)

    def test_status_of_empty_home_reports_all_false(self):
        status = json.loads(self.run_cli('--status'))
        self.assertFalse(status['installed'])
        self.assertFalse(status['control_installed'])
        self.assertFalse(status['tools_installed'])


class DryRunAndCompatTest(SetupCase):
    def test_dry_run_lists_three_entries_and_writes_nothing(self):
        payload = json.loads(self.run_cli('--dry-run'))
        self.assertEqual([item['id'] for item in payload['entries']], [MEMORY, CONTROL, TOOLS])
        self.assertFalse(self.path.exists())

    def test_dry_run_respects_no_tools(self):
        payload = json.loads(self.run_cli('--dry-run', '--no-tools'))
        self.assertEqual([item['id'] for item in payload['entries']], [MEMORY, CONTROL])

    def test_old_arguments_keep_their_meaning(self):
        self.install('--profile', 'web',
                     '--script', str(self.bot_home / 'memory.py'),
                     '--bot-script', str(self.bot_home / 'bot.py'),
                     '--bot-url', 'http://127.0.0.1:27500')
        web = bridge.profile_path(self.dsh, 'web')
        self.assertEqual(self.ids(web), [MEMORY, CONTROL, TOOLS])
        self.assertEqual(self.entry(MEMORY, web)['config']['args'],
                         [str(self.bot_home / 'memory.py')])
        self.assertEqual(self.entry(CONTROL, web)['config']['args'],
                         [str(self.bot_home / 'bot.py')])
        self.assertEqual(self.entry(CONTROL, web)['config']['env']['WHALE_BOT_URL'],
                         'http://127.0.0.1:27500')

    def test_help_documents_the_new_flags(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                setup.main(['--help'])
        self.assertEqual(caught.exception.code, 0)
        text = out.getvalue()
        self.assertIn('--remove-tools', text)
        self.assertIn('--no-tools', text)
        self.assertIn('--tools-script', text)


@contextmanager
def clean_env(**values):
    """只留本用例要设的端口变量，shell 里的同名变量先摘掉，端点文件也隔离。

    端点文件必须一起隔离：`_endpoint_url()` 会把「这台机器上真有一台控制台在跑」
    当成解析结果（端点文件里记的 pid 探活通过就用它的端口），于是开发机开着控制台时
    这几条「坏端口降级到 27499」的断言必挂 —— 用户看得见的假失败。把端点候选目录
    全部指到空临时目录，用例就只看它自己设的环境变量。
    """
    with tempfile.TemporaryDirectory() as sandbox_raw:
        sandbox = Path(sandbox_raw)
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(control_api, 'data_dir_candidates', lambda: [sandbox]):
            for key in ('WHALE_BOT_URL', 'WHALEGIRL_PORT', 'WHALE_QQ_DATA_DIR',
                        'WHALE_QQ_HOME', 'LOCALAPPDATA'):
                os.environ.pop(key, None)
            os.environ['WHALE_QQ_DATA_DIR'] = str(sandbox)
            os.environ['LOCALAPPDATA'] = str(sandbox)
            for key, value in values.items():
                os.environ[key] = str(value)
            yield


class BotUrlFollowTest(SetupCase):
    """登记不再把"当时算出来的地址"写死进 profile。

    写死 = 之后改控制台端口必失效（旧地址压过运行期解析）。默认留空，让 MCP
    子进程自己去读控制台端点文件；只有用户显式 --bot-url 才写。
    """

    def control_env(self, *extra, **env):
        with clean_env(**env):
            payload = json.loads(self.run_cli('--dry-run', *extra))
        entry = next(item for item in payload['entries'] if item['id'] == CONTROL)
        return entry['config']['env']

    def written_url(self, *extra, **env):
        return self.control_env(*extra, **env)['WHALE_BOT_URL']

    def test_default_leaves_the_url_empty_for_runtime_resolution(self):
        for env in ({}, {'WHALEGIRL_PORT': '28651'},
                    {'WHALE_BOT_URL': 'http://127.0.0.1:19999'}):
            with self.subTest(env=env):
                self.assertEqual(self.written_url(**env), '')

    def test_dry_run_shows_written_env_and_resolved_address_apart(self):
        with clean_env(WHALEGIRL_PORT=28651):
            payload = json.loads(self.run_cli('--dry-run'))
        self.assertEqual(payload['bot_url'], '')
        self.assertEqual(payload['resolved_bot_url'], 'http://127.0.0.1:28651')

    def test_bad_port_degrades_to_default_without_raising(self):
        for raw in ('abc', '0', '70000', ''):
            with self.subTest(raw=raw), clean_env(WHALEGIRL_PORT=raw):
                payload = json.loads(self.run_cli('--dry-run'))
                self.assertEqual(payload['resolved_bot_url'], 'http://127.0.0.1:27499')

    def test_explicit_flag_is_written_verbatim(self):
        self.assertEqual(self.written_url('--bot-url', 'http://127.0.0.1:27500',
                                          WHALEGIRL_PORT=28651),
                         'http://127.0.0.1:27500')

    def test_install_clears_a_leftover_url_from_an_old_registration(self):
        """老版本写死的地址必须能被重跑登记清掉，否则它会压过运行期解析。"""
        self.path.parent.mkdir(parents=True)
        self.path.write_text('- id: mcp-whale-bot\n  name: "@deepseek-ai/dsh-mcp-client"\n'
                             '  config:\n    serverName: whale_bot\n'
                             '    env:\n      WHALE_BOT_URL: http://127.0.0.1:27499\n',
                             encoding='utf-8')
        with clean_env():
            self.install()
        self.assertEqual(self.entry(CONTROL)['config']['env']['WHALE_BOT_URL'], '')

    def test_install_keeps_an_explicit_url(self):
        with clean_env():
            self.install('--bot-url', 'http://127.0.0.1:27500')
        self.assertEqual(self.entry(CONTROL)['config']['env']['WHALE_BOT_URL'],
                         'http://127.0.0.1:27500')

if __name__ == '__main__':
    unittest.main()
