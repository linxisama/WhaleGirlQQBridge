"""外部后端 × DSH 联动：/api/agent/backends/dsh/* 三个端点 + 面板「DSH 联动」区块。

两件事必须成立：端点写出来的 yaml 与它回读的 entry_count 一致；面板的按钮一次都不许
在 UI 线程里打网络（用带延迟的假端点来证明）。
"""
import asyncio
import os
import tempfile
import time
import unittest
from urllib.parse import parse_qs

import httpx
import yaml
from fastapi import FastAPI

from src import agent_backend_api as api
from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from src.gui.agent_panel import (DSH_DEFAULT_PROFILE, DSH_LINK_PATH, DSH_STATUS_PATH,
                                 DSH_UNLINK_PATH, AgentBackendPanel, dsh_status_path)

APP = QApplication.instance() or QApplication([])


# ── 后端端点 ────────────────────────────────────────────────────────────────

def insert_rows(path) -> list:
    """cordis.patch.yml 里所有 insert 块下的行（DSH 实际会展开成一条条登记的那些）。"""
    with open(path, 'r', encoding='utf-8') as handle:
        data = yaml.safe_load(handle) or []
    rows = []
    for item in data if isinstance(data, list) else []:
        block = item.get('insert') if isinstance(item, dict) else None
        if isinstance(block, list):
            rows.extend(row for row in block if isinstance(row, dict))
    return rows


def insert_server_names(path) -> list:
    """insert 块里每一行的 config.serverName（这才是 DSH 认的 MCP server 名）。"""
    names = []
    for row in insert_rows(path):
        config = row.get('config')
        if isinstance(config, dict) and config.get('serverName'):
            names.append(str(config['serverName']))
    return names


class DshLinkApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api._reset_for_tests()
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 假 ~/.dsh：link 之前连 profiles 目录都没有，测的就是「从无到有」。
        self.home = os.path.join(self.temp.name, 'dsh')
        self._old_home = os.environ.get('DSH_HOME')
        self._old_bot = os.environ.get('WHALE_QQ_HOME')
        os.environ['DSH_HOME'] = self.home
        os.environ.pop('WHALE_QQ_HOME', None)
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await asyncio.to_thread(api.shutdown_all)
        api._reset_for_tests()
        for key, value in (('DSH_HOME', self._old_home), ('WHALE_QQ_HOME', self._old_bot)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        await self.bot.close()
        await asyncio.sleep(.1)
        self.temp.cleanup()

    def patch_file(self, profile='desktop'):
        return os.path.join(self.home, 'profiles', profile, 'cordis.patch.yml')

    async def dsh_status(self, profile=''):
        return await self.client.get('/api/agent/backends/dsh/status',
                                     params={'profile': profile} if profile else None)

    async def dsh_link(self, profile='desktop'):
        return await self.client.post('/api/agent/backends/dsh/link', json={'profile': profile})

    async def dsh_unlink(self, profile='desktop'):
        return await self.client.post('/api/agent/backends/dsh/unlink', json={'profile': profile})

    # ── 只读状态 ────────────────────────────────────────────────────────────

    async def test_status_on_untouched_home_is_empty_and_writes_nothing(self):
        response = await self.dsh_status()
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual('status', body['action'])
        self.assertEqual('desktop', body['profile'])       # 空值按默认 profile
        self.assertFalse(body['tools_installed'])
        self.assertEqual(0, body['entry_count'])
        self.assertEqual([], body['profiles'])
        self.assertEqual(self.home, body['dsh_home'])
        self.assertFalse(os.path.exists(self.patch_file()), 'status 是只读的，不许碰文件')

    async def test_link_writes_three_entries_under_insert_block(self):
        response = await self.dsh_link()
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body['ok'], body)
        self.assertTrue(body['changed'])
        self.assertTrue(body['tools_installed'])
        self.assertEqual(3, body['entry_count'])
        self.assertEqual(1, body['insert_blocks'])
        self.assertEqual([], body['bare_entry_ids'], '新增行必须写在 insert 块下')
        self.assertFalse(body['profile_existed'])
        self.assertIn('profile desktop 目录', body['warning'])
        self.assertIn('会话', body['hint'])
        # 回读的 yaml 必须与 entry_count 对得上（三条都在 insert 块里）。
        names = insert_server_names(self.patch_file())
        self.assertEqual(['whale_memory', 'whale_bot', 'whale_tools'], names)
        self.assertEqual(body['entry_count'], len(names))
        self.assertEqual([{'whale_memory', 'whale_bot', 'whale_tools'}],
                         [set(entry['serverName'] for entry in body['entries'])])
        self.assertTrue(all(entry['installed'] for entry in body['entries']))

    async def test_status_carries_current_commands_for_staleness(self):
        # 空 profile 也要带上「现在该用的」解释器/脚本；dsh_auto_connect 靠它判断
        # 登记里的命令是不是还指向同一份仓库。
        body = (await self.dsh_status()).json()
        self.assertTrue(body['python'])
        self.assertEqual({'memory', 'bot', 'tools'}, set(body['scripts']))
        api.dsh_memory_bridge.install_all(
            self.home, 'C:/old/python.exe', 'old_memory.py', 'C:/account/bot',
            'old_bot.py', '', profile='desktop', tools_script='old_tools.py')
        stale = (await self.dsh_status()).json()
        self.assertFalse(api.dsh_commands_current(stale),
                         '登记还指着旧仓库时必须判 stale，否则永远不重写')

    async def test_link_keeps_existing_whale_qq_home(self):
        # 安装版的控制台数据根是 %LOCALAPPDATA%\WhaleGirlQQ，真实 QQ 数据却在网关账号目录；
        # 自动接入重写登记时必须沿用旧的 WHALE_QQ_HOME，不能把它改成控制台根。
        api.dsh_memory_bridge.install_all(
            self.home, 'C:/py/python.exe', 'mem.py', 'C:/account/bot',
            'bot.py', '', profile='desktop', tools_script='tools.py')
        body = (await self.dsh_link()).json()
        self.assertTrue(body['ok'], body)
        self.assertEqual('C:/account/bot', body['bot_home'])
        homes = {}
        for row in insert_rows(self.patch_file()):
            if row.get('id') in ('mcp-whale-memory', 'mcp-whale-tools'):
                homes[str(row['id'])] = row['config']['env'].get('WHALE_QQ_HOME')
        self.assertEqual({'mcp-whale-memory': 'C:/account/bot',
                          'mcp-whale-tools': 'C:/account/bot'}, homes)

    async def test_command_is_exactly_equivalent_to_what_was_written(self):
        body = (await self.dsh_link()).json()
        expected = api.dsh_command('desktop', 'link', home=body['dsh_home'],
                                   bot_home=body['bot_home'], python=body['python'],
                                   scripts=body['scripts'])
        self.assertEqual(expected, body['command'], '面板显示的命令必须就是这次真实用的那套参数')
        self.assertIn('setup_dsh_memory.py', body['command'])
        self.assertIn('--profile desktop', body['command'])
        self.assertIn('--dsh-home ' + body['dsh_home'], body['command'])
        self.assertNotIn('--remove', body['command'])

    async def test_link_is_idempotent(self):
        # 先放一个别人的条目：第一次接入会留备份，第二次幂等既不该改文件也不该再备份。
        os.makedirs(os.path.dirname(self.patch_file()), exist_ok=True)
        with open(self.patch_file(), 'w', encoding='utf-8') as handle:
            handle.write('- insert:\n  - id: someone-else\n    name: x\n')
        first = (await self.dsh_link()).json()
        self.assertTrue(first['changed'])
        folder = os.path.dirname(self.patch_file())
        backups_first = sorted(name for name in os.listdir(folder)
                               if '.bak-whale-memory-' in name)
        self.assertEqual(1, len(backups_first), backups_first)
        second = (await self.dsh_link()).json()
        self.assertTrue(second['ok'])
        self.assertFalse(second['changed'], '重复接入不该再写一遍')
        self.assertEqual(4, second['entry_count'], '别人的条目也算条目')
        rows = insert_rows(self.patch_file())
        self.assertEqual(second['entry_count'], len(rows), 'entry_count 必须等于 yaml 里的行数')
        self.assertEqual(['someone-else', 'mcp-whale-memory', 'mcp-whale-bot', 'mcp-whale-tools'],
                         [str(row.get('id')) for row in rows])
        self.assertEqual(['whale_memory', 'whale_bot', 'whale_tools'],
                         insert_server_names(self.patch_file()))
        backups_second = sorted(name for name in os.listdir(folder)
                                if '.bak-whale-memory-' in name)
        self.assertEqual(backups_first, backups_second, '幂等不该再造备份')

    async def test_unlink_clears_entries_and_is_idempotent(self):
        await self.dsh_link()
        response = await self.dsh_unlink()
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['changed'])
        self.assertTrue(body['removed'])
        self.assertFalse(body['tools_installed'])
        self.assertEqual(0, body['entry_count'])
        self.assertEqual([], insert_server_names(self.patch_file()))
        again = (await self.dsh_unlink()).json()
        self.assertTrue(again['ok'])
        self.assertFalse(again['changed'], '再撤销一次不该改动文件')
        self.assertFalse(again['removed'])

    async def test_status_after_link_reports_installed(self):
        await self.dsh_link()
        body = (await self.dsh_status()).json()
        self.assertTrue(body['tools_installed'])
        self.assertEqual(3, body['entry_count'])
        self.assertEqual(1, body['insert_blocks'])
        self.assertEqual(['desktop'], body['profiles'])
        self.assertTrue(all(entry['installed'] for entry in body['entries']))

    async def test_profiles_lists_only_real_profile_dirs(self):
        for name, files in (('desktop', ('cordis.yml',)),
                            ('web', ('cordis.patch.yml',)),
                            ('node_modules', ()),
                            ('broken', ('other.txt',))):
            folder = os.path.join(self.home, 'profiles', name)
            os.makedirs(folder, exist_ok=True)
            for filename in files:
                with open(os.path.join(folder, filename), 'w', encoding='utf-8') as handle:
                    handle.write('[]\n')
        body = (await self.dsh_status()).json()
        self.assertEqual(['desktop', 'web'], body['profiles'],
                         '没有 cordis 配置的目录不算 profile')

    async def test_link_to_named_profile_keeps_profiles_apart(self):
        self.assertEqual(200, (await self.dsh_link('web-desktop')).status_code)
        desktop = (await self.dsh_status('desktop')).json()
        web = (await self.dsh_status('web-desktop')).json()
        self.assertFalse(desktop['tools_installed'])
        self.assertEqual(0, desktop['entry_count'])
        self.assertTrue(web['tools_installed'])
        self.assertEqual(['web-desktop'], web['profiles'])
        self.assertIn('--profile web-desktop', web['command'])
        self.assertTrue(os.path.isfile(self.patch_file('web-desktop')))
        self.assertFalse(os.path.exists(self.patch_file('desktop')))

    async def test_unsafe_profile_name_is_rejected(self):
        for name in ('../evil', 'a/b', '..', 'name with space'):
            response = await self.dsh_link(name)
            self.assertEqual(400, response.status_code, (name, response.text))
            body = response.json()
            self.assertFalse(body['ok'])
            self.assertEqual('bad_profile', body['code'])
            self.assertIn('profile', body['message'])
        self.assertFalse(os.path.exists(os.path.join(self.temp.name, 'evil')),
                         '非法 profile 名不许造出目录')

    async def test_broken_patch_file_returns_chinese_code_not_traceback(self):
        os.makedirs(os.path.dirname(self.patch_file()), exist_ok=True)
        with open(self.patch_file(), 'w', encoding='utf-8') as handle:
            handle.write('just a string, not a patch list\n')
        status = await self.dsh_status()
        body = status.json()
        self.assertFalse(body['ok'])
        self.assertEqual('bad_dsh_profile', body['code'])
        self.assertTrue(body['message'])
        with open(self.patch_file(), 'r', encoding='utf-8') as handle:
            self.assertEqual('just a string, not a patch list\n', handle.read(),
                             '读坏了也要原样留着，不许被端点改掉')

    async def test_endpoints_refuse_non_local_callers(self):
        app = FastAPI()
        api.install_agent_backend_routes(app)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=('10.1.2.3', 50000)),
                base_url='http://127.0.0.1') as client:
            for method, path in (('GET', '/api/agent/backends/dsh/status'),
                                 ('POST', '/api/agent/backends/dsh/link'),
                                 ('POST', '/api/agent/backends/dsh/unlink')):
                response = await client.request(method, path, json={})
                self.assertEqual(403, response.status_code, (path, response.text))
                self.assertEqual('auth_denied', response.json()['code'])
        self.assertFalse(os.path.exists(self.patch_file()), '被拒的请求不许写文件')

    async def test_dsh_routes_are_registered_before_item_path(self):
        paths = [route.path for route in self.app.routes if getattr(route, 'path', '')]
        status_path = '/api/agent/backends/dsh/status'
        item_status = '/api/agent/backends/{backend_id}/status'
        self.assertIn(status_path, paths)
        self.assertLess(paths.index(status_path), paths.index(item_status),
                        'dsh/status 必须先注册，否则会被当成 backend_id')


# ── 面板 ────────────────────────────────────────────────────────────────────

def panel_list_payload():
    return {'ok': True, 'backends': [], 'data_dir': 'C:/tmp/data', 'data_dir_ready': True,
            'profile_file': 'C:/tmp/data/agent_backends.json'}


def dsh_payload(action='status', profile='desktop', profiles=('desktop',), installed=False,
                count=0, changed=None, removed=None, entries=None, warning=''):
    data = {'ok': True, 'action': action, 'profile': profile, 'dsh_home': 'C:/tmp/dsh',
            'path': 'C:/tmp/dsh/profiles/%s/cordis.patch.yml' % profile,
            'python': 'C:/py/python.exe', 'bot_home': 'C:/tmp/data',
            'scripts': {'memory': 'C:/repo/tools/whale_memory_mcp.py',
                        'bot': 'C:/repo/tools/whale_bot_mcp.py',
                        'tools': 'C:/repo/tools/whale_tools_mcp.py'},
            'profiles': list(profiles), 'tools_installed': installed,
            'entry_count': count, 'insert_blocks': 1 if installed else 0,
            'bare_entry_ids': [], 'entries': entries or [],
            'command': 'C:/py/python.exe C:/repo/tools/setup_dsh_memory.py %s--profile %s'
                       % ('' if action == 'link' else '--%s ' % (
                           'remove' if action == 'unlink' else 'status'), profile),
            'hint': '要新开一个 DSH 会话才看得到这些工具'}
    if changed is not None:
        data['changed'] = changed
        data['backup'] = 'C:/tmp/dsh/profiles/%s/cordis.patch.yml.bak-x' % profile
    if removed is not None:
        data['removed'] = removed
    if warning:
        data['warning'] = warning
    return data


class DshFakeApi:
    """端点替身：按路径分流，带延迟用来证明按钮走的是后台线程。"""

    def __init__(self, status_body=None, link_body=None, unlink_body=None, delay=0.0):
        self.status_body = status_body if status_body is not None else dsh_payload()
        self.link_body = link_body if link_body is not None else dsh_payload(
            'link', installed=True, count=3, changed=True)
        self.unlink_body = unlink_body if unlink_body is not None else dsh_payload(
            'unlink', installed=False, count=0, changed=True, removed=True)
        self.delay = delay
        self.calls = []

    def get(self, path, timeout=None):
        self.calls.append(('GET', path, None))
        if self.delay:
            time.sleep(self.delay)
        if not isinstance(self.status_body, dict):
            return self.status_body
        body = dict(self.status_body)
        if 'profile=' in path:
            query = path.split('?', 1)[1] if '?' in path else path
            body['profile'] = (parse_qs(query).get('profile') or [''])[0]
            body['repo_root'] = (parse_qs(query).get('repo_root') or [''])[0]
        return body

    def post(self, path, data=None, timeout=None):
        self.calls.append(('POST', path, data))
        if self.delay:
            time.sleep(self.delay)
        if path.endswith('/unlink'):
            body = self.unlink_body
        elif path.endswith('/link'):
            body = self.link_body
        else:
            body = self.status_body
        return dict(body) if isinstance(body, dict) else body

    def delete(self, path, timeout=None):
        self.calls.append(('DELETE', path, None))
        return {'ok': True}

    def paths(self):
        return [(method, path) for method, path, _data in self.calls]


def pump(until=None, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        APP.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    APP.processEvents()
    return until is None or bool(until())


class DshPanelCase(unittest.TestCase):
    def setUp(self):
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            try:
                panel.stop_polling()
            except Exception:
                pass
        pump(timeout=0.05)

    def make(self, **kwargs):
        api = DshFakeApi(**kwargs)
        panel = AgentBackendPanel(api.get, api.post, api.delete)
        self.panels.append(panel)
        pump(until=lambda: bool(api.calls))
        pump(until=lambda: not panel.requests.busy)
        return api, panel


class TestDshPanel(DshPanelCase):
    def test_default_profile_is_desktop_and_no_dsh_call_before_showing(self):
        api, panel = self.make()
        self.assertEqual([DSH_DEFAULT_PROFILE], panel.dsh_profiles_list())
        self.assertEqual('desktop', panel.dsh_profile_name())
        self.assertEqual([('GET', 'agent/backends')], api.paths(),
                         '面板没显示时不许自己去点 DSH 端点')
        self.assertEqual('', panel.dsh_command_text())

    def test_link_button_posts_profile_and_shows_copyable_command(self):
        body = dsh_payload('link', installed=True, count=3, changed=True)
        api, panel = self.make(link_body=body)
        panel.btn_dsh_link.click()
        self.assertTrue(pump(until=lambda: ('POST', panel_link_path()) in api.paths(),
                            timeout=3.0), api.paths())
        self.assertIn(('POST', DSH_LINK_PATH, {'profile': 'desktop'}), api.calls)
        pump(until=lambda: not panel.requests.busy)
        self.assertEqual(body['command'], panel.dsh_command_text())
        self.assertTrue(panel.dsh_command.isReadOnly(), '命令框只读，靠选中复制')
        text = panel.dsh_feedback_text()
        self.assertIn('已把 DSH 接上控制台', text)
        self.assertIn('条目总数 3', text)
        self.assertIn(body['hint'], text)

    def test_link_click_returns_before_network_finishes(self):
        api, panel = self.make(delay=0.4)
        started = time.time()
        panel.btn_dsh_link.click()
        elapsed = time.time() - started
        self.assertLess(elapsed, 0.2, '按钮必须立刻返回：网络在后台线程里跑')
        self.assertTrue(panel.requests.busy, '后台线程应该还在跑')
        self.assertIn('正在把三条 MCP 条目写进 DSH', panel.dsh_feedback_text())
        pump(until=lambda: any(method == 'POST' for method, _path in api.paths()), timeout=5.0)
        pump(until=lambda: not panel.requests.busy, timeout=5.0)
        self.assertIn('已把 DSH 接上控制台', panel.dsh_feedback_text())

    def test_idempotent_link_says_nothing_changed(self):
        body = dsh_payload('link', installed=True, count=3, changed=False)
        api, panel = self.make(link_body=body)
        panel.btn_dsh_link.click()
        pump(until=lambda: any(method == 'POST' for method, _path in api.paths()))
        pump(until=lambda: not panel.requests.busy)
        self.assertIn('没有改动文件', panel.dsh_feedback_text())
        self.assertIn('幂等', panel.dsh_feedback_text())

    def test_unlink_button_shows_removed_state(self):
        api, panel = self.make()
        panel.btn_dsh_unlink.click()
        pump(until=lambda: any(method == 'POST' for method, _path in api.paths()))
        pump(until=lambda: not panel.requests.busy)
        self.assertIn(('POST', DSH_UNLINK_PATH, {'profile': 'desktop'}), api.calls)
        self.assertIn('已撤销', panel.dsh_feedback_text())

    def test_status_button_fills_profile_combo_from_real_dirs(self):
        body = dsh_payload('status', profiles=('desktop', 'web-desktop', 'web'),
                           installed=True, count=3)
        api, panel = self.make(status_body=body)
        panel.btn_dsh_status.click()
        pump(until=lambda: any(method == 'GET' and 'dsh/status' in path
                               for method, path in api.paths()))
        pump(until=lambda: not panel.requests.busy)
        self.assertEqual(['desktop', 'web-desktop', 'web'], panel.dsh_profiles_list())
        self.assertEqual('desktop', panel.dsh_profile_name())
        self.assertIn(('GET', dsh_status_path('desktop'), None), api.calls)
        self.assertIn('条目总数 3', panel.dsh_feedback_text())

    def test_status_keeps_selected_profile_when_it_still_exists(self):
        api, panel = self.make(status_body=dsh_payload(profiles=('desktop', 'web')))
        panel.set_dsh_profiles(['desktop', 'web'])
        panel.dsh_profile.setCurrentIndex(panel.dsh_profile.findText('web'))
        self.assertEqual('web', panel.dsh_profile_name())
        panel.btn_dsh_status.click()
        pump(until=lambda: any('profile=web' in path for _method, path in api.paths()))
        pump(until=lambda: not panel.requests.busy)
        self.assertIn(('GET', dsh_status_path('web'), None), api.calls)
        self.assertEqual('web', panel.dsh_profile_name(), '刷新不该把用户选的 profile 换掉')

    def test_error_payload_becomes_code_plus_chinese_reason(self):
        bad = {'ok': False, 'code': 'dsh_scripts_missing',
               'message': '仓库里缺 MCP 入口脚本，登记会指向不存在的文件：C:/repo/tools/x.py'}
        api, panel = self.make(link_body=bad)
        panel.btn_dsh_link.click()
        pump(until=lambda: any(method == 'POST' for method, _path in api.paths()))
        pump(until=lambda: not panel.requests.busy)
        text = panel.dsh_feedback_text()
        self.assertTrue(text.startswith('[dsh_scripts_missing] '), text)
        self.assertIn('仓库里缺 MCP 入口脚本', text)
        self.assertEqual('', panel.dsh_command_text(), '失败时不写命令框（原来就没有）')

    def test_offline_payload_is_reported_not_raised(self):
        api, panel = self.make(link_body=None)
        api.link_body = None
        panel.btn_dsh_link.click()
        pump(until=lambda: any(method == 'POST' for method, _path in api.paths()))
        pump(until=lambda: not panel.requests.busy)
        self.assertTrue(panel.dsh_feedback_text().startswith('[offline]'),
                        panel.dsh_feedback_text())

    def test_warning_and_bare_hint_are_visible(self):
        body = dsh_payload('link', installed=True, count=3, changed=True,
                           warning='DSH 里原来没有 profile desktop 目录，已按 DSH 的目录结构新建')
        body['bare_entry_ids'] = ['mcp-whale-memory']
        api, panel = self.make(link_body=body)
        panel.btn_dsh_link.click()
        pump(until=lambda: any(method == 'POST' for method, _path in api.paths()))
        pump(until=lambda: not panel.requests.busy)
        text = panel.dsh_feedback_text()
        self.assertIn('原来没有 profile desktop 目录', text)
        self.assertIn('裸行登记', text)
        self.assertIn('mcp-whale-memory', text)

    def test_auto_status_runs_once_and_fills_combo(self):
        api, panel = self.make(status_body=dsh_payload(profiles=('desktop', 'web')))
        self.assertTrue(panel.auto_dsh_status())
        pump(until=lambda: any('dsh/status' in path for _method, path in api.paths()))
        pump(until=lambda: not panel.requests.busy)
        self.assertEqual(['desktop', 'web'], panel.dsh_profiles_list())
        self.assertFalse(panel.auto_dsh_status(), '自动状态只跑一次，别每次切页都打一遍')
        self.assertFalse(panel.auto_dsh_status())

    def test_other_buttons_keep_working_with_dsh_block_present(self):
        api, panel = self.make()
        state = panel.button_state()
        self.assertEqual({'probe', 'start', 'stop', 'open', 'delete'}, set(state))
        self.assertTrue(panel.btn_dsh_link.isEnabled())
        self.assertTrue(panel.btn_dsh_unlink.isEnabled())
        self.assertTrue(panel.btn_dsh_status.isEnabled())
        self.assertEqual('agent_dsh_command', panel.dsh_command.objectName())


class TestDshBundledPanel(DshPanelCase):
    """安装版不再要求源码仓库根；回音必须落在看得见的地方。"""

    def test_feedback_sits_above_the_copyable_command(self):
        api, panel = self.make()
        self.assertLess(panel.dsh_feedback.y(), panel.dsh_command.y(),
                        '回音要排在命令框之前，不然 1302x763 下写完就滚出可视区')

    def test_link_never_sends_repo_root(self):
        api, panel = self.make()
        panel.btn_dsh_link.click()
        self.assertTrue(pump(until=lambda: not panel.requests.busy))
        panel.btn_dsh_link.click()
        self.assertTrue(pump(until=lambda: sum(1 for call in api.calls
                                              if call[1] == DSH_LINK_PATH) == 2), api.calls)
        calls = [call for call in api.calls if call[1] == DSH_LINK_PATH]
        self.assertEqual([('POST', DSH_LINK_PATH, {'profile': 'desktop'})] * 2, calls,
                         '安装版自带 MCP 宿主，请求体不该再带源码仓库根')

    def test_status_has_no_repo_query_and_shows_bundled_hint(self):
        body = dsh_payload(profiles=('desktop',), installed=True, count=3)
        body.update({'mcp_mode': 'bundled', 'command': '', 'repo_root': '',
                     'mcp_command': 'C:/Program Files/WhaleGirlQQBridge/WhaleGirlQQBridge.exe'})
        api, panel = self.make(status_body=body)
        panel.btn_dsh_status.click()
        self.assertTrue(pump(until=lambda: not panel.requests.busy))
        self.assertFalse(any('repo_root=' in path for _method, path, _body in api.calls), api.calls)
        self.assertIn('安装版已内置 MCP 宿主', panel.dsh_command_text())
        self.assertIn('MCP 模式：安装版内置宿主', panel.dsh_feedback_text())
        self.assertIn('条目总数 3', panel.dsh_feedback_text())

    def test_repo_widget_is_gone(self):
        api, panel = self.make()
        self.assertFalse(hasattr(panel, 'dsh_repo'))
        self.assertEqual('', panel.dsh_repo_root())
        panel.set_dsh_repo('D:\\ignored')
        self.assertEqual('', panel.dsh_repo_root())

    def test_click_scrolls_the_feedback_into_view(self):
        api, panel = self.make()
        panel.resize(1302, 763)
        panel.show()
        pump(timeout=0.2)
        panel.btn_dsh_link.click()
        self.assertTrue(pump(until=lambda: not panel.requests.busy))
        viewport = panel.scroll.viewport()
        top = panel.dsh_feedback.mapTo(viewport, panel.dsh_feedback.rect().topLeft()).y()
        self.assertGreaterEqual(top, 0, '回音被滚到了视口上方')
        self.assertLess(top, viewport.height(), '回音在视口外——用户就是看不到反应')
        panel.hide()

def panel_link_path():
    return DSH_LINK_PATH


if __name__ == '__main__':
    unittest.main()
