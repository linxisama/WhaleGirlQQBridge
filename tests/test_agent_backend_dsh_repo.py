"""安装版（冻结态）的 DSH 联动：仓库根要能指过去，python 不许是 GUI exe。

冻结产物里 _internal 只有 web 静态资源（实测 src 下 0 个 .py/.pyc），src.* 全在
PyInstaller 归档里 —— 三个 MCP 桥 import 不到 src.chat_state，所以安装版必须靠
「源码仓库根」指到真实仓库；同时 sys.executable 是 WhaleGirlQQBridge.exe，那不是
解释器，绝不能写进 MCP 登记。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

import httpx

from src import agent_backend_api as api
from src.qq_app import QQRuntime, create_app
from tests.test_persona_files import build_root


def make_repo(parent):
    """假仓库：tools 下三个桥 + 命令行工具都在，dsh_repo_root() 就认它。"""
    repo = os.path.join(parent, 'repo')
    tools = os.path.join(repo, 'tools')
    os.makedirs(tools, exist_ok=True)
    for name in list(api.DSH_SCRIPT_FILES.values()) + ['setup_dsh_memory.py']:
        with open(os.path.join(tools, name), 'w', encoding='utf-8') as handle:
            handle.write('# stub\n')
    return repo


def frozen():
    """模拟 PyInstaller 产物：真实现场是 sys.frozen = True。"""
    return mock.patch.object(sys, 'frozen', True, create=True)


class DshFrozenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api._reset_for_tests()
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = os.path.join(self.temp.name, 'dsh')
        self.keys = ('DSH_HOME', 'WHALE_QQ_HOME', api.DSH_REPO_ENV)
        self.saved = {key: os.environ.get(key) for key in self.keys}
        os.environ['DSH_HOME'] = self.home
        for key in ('WHALE_QQ_HOME', api.DSH_REPO_ENV):
            os.environ.pop(key, None)
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'test.sqlite')
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        api._reset_for_tests()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        await self.bot.close()
        self.temp.cleanup()

    async def status(self, **params):
        return await self.client.get('/api/agent/backends/dsh/status', params=params)

    async def test_frozen_without_repo_asks_for_source(self):
        with frozen():
            response = await self.status(profile='desktop')
        self.assertEqual(400, response.status_code, response.text)
        body = response.json()
        self.assertEqual('dsh_needs_source', body['code'])
        self.assertIn('源码仓库根', body['message'])
        self.assertEqual(api.DSH_REPO_ENV, body['repo_env'])
        self.assertEqual('unset', body['repo_root_source'])

    async def test_frozen_never_offers_the_gui_exe_as_python(self):
        # 冻结态 sys.executable 是 WhaleGirlQQBridge.exe：找不到真解释器就要老实说没有。
        empty = os.path.join(self.temp.name, 'no-venv-repo')
        os.makedirs(empty, exist_ok=True)
        with frozen(), mock.patch.object(api.shutil, 'which', lambda name: None):
            self.assertTrue(api.frozen_mode())
            self.assertEqual('', api.dsh_python(empty))
        store = os.path.join('C:', 'Users', 'x', 'AppData', 'Local', 'Microsoft', 'WindowsApps',
                             'python.exe')
        with frozen(), mock.patch.object(api.shutil, 'which', lambda name: store):
            self.assertEqual('', api.dsh_python(empty), '商店桩子只会弹窗口，不能写进登记')
        real = os.path.join(self.temp.name, 'Python312', 'python.exe')
        with frozen(), mock.patch.object(api.shutil, 'which', lambda name: real):
            self.assertEqual(real, api.dsh_python(empty))

    async def test_frozen_prefers_repo_venv_python(self):
        repo = make_repo(self.temp.name)
        venv = os.path.join(repo, '.venv-qq', 'Scripts')
        os.makedirs(venv, exist_ok=True)
        python = os.path.join(venv, 'python.exe')
        with open(python, 'w', encoding='utf-8') as handle:
            handle.write('stub\n')
        with frozen():
            self.assertEqual(python, api.dsh_python(repo))

    async def test_env_repo_resolves_scripts_and_command(self):
        repo = make_repo(self.temp.name)
        os.environ[api.DSH_REPO_ENV] = repo
        with frozen():
            body = (await self.status(profile='desktop')).json()
            self.assertTrue(body['ok'], body)
            self.assertEqual('env', body['repo_root_source'])
        self.assertEqual(os.path.abspath(repo), os.path.abspath(body['repo_root']))
        self.assertEqual(os.path.join(repo, 'tools', 'whale_memory_mcp.py'),
                         body['scripts']['memory'])
        self.assertEqual(os.path.join(repo, 'tools', 'setup_dsh_memory.py').replace('/', os.sep),
                         os.path.join(repo, 'tools', 'setup_dsh_memory.py'))
        self.assertIn(os.path.join(repo, 'tools', 'setup_dsh_memory.py'), body['command'])
        self.assertNotIn('_internal', body['command'])

    async def test_typed_repo_root_is_remembered_for_the_next_status(self):
        repo = make_repo(self.temp.name)
        with frozen():
            first = (await self.status(profile='desktop', repo_root=repo)).json()
            self.assertTrue(first['ok'], first)
            self.assertEqual('override', first['repo_root_source'])
            second = (await self.status(profile='desktop')).json()
        self.assertTrue(second['ok'], second)
        self.assertEqual(os.path.abspath(repo), os.path.abspath(second['repo_root']),
                         '填过一次的仓库根要记住，别每次让用户重填')
        self.assertEqual('saved', second['repo_root_source'], '第二次是「上次记住的」')
        config = os.path.join(str(self.root), api.DSH_REPO_CONFIG)
        self.assertTrue(os.path.isfile(config), config)

    async def test_typo_repo_root_is_not_remembered(self):
        with frozen():
            bad = (await self.status(profile='desktop', repo_root='Z:/definitely/missing')).json()
        self.assertEqual('dsh_needs_source', bad['code'])
        self.assertFalse(os.path.isfile(os.path.join(str(self.root), api.DSH_REPO_CONFIG)),
                         '指错路不该把配置写脏')

    async def test_source_mode_still_uses_its_own_repo(self):
        body = (await self.status(profile='desktop')).json()
        self.assertTrue(body['ok'], body)
        self.assertEqual('repo', body['repo_root_source'])
        self.assertEqual(str(api.repo_root()), body['repo_root'])
        self.assertEqual(str(sys.executable), body['python'])
        self.assertIn('tools', body['scripts']['memory'])


if __name__ == '__main__':
    unittest.main()
