"""外部工具侧跟随控制台端口：登记默认值与运行期解析同一口径，文档字符串不写死地址。

控制台端口支持 --port > WHALEGIRL_PORT > qq.console_port > 27499 之后，
MCP 侧任何一处写死 127.0.0.1:27499 都会让工具调用打到没人监听的端口。
"""
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src import bot_control_api  # noqa: E402
from tools import setup_dsh_memory as setup  # noqa: E402

BOT_MCP = REPO / 'tools' / 'whale_bot_mcp.py'
TOOLS_MCP = REPO / 'tools' / 'whale_tools_mcp.py'

ENVS = ({},
        {'WHALEGIRL_PORT': '28651'},
        {'WHALE_BOT_URL': 'http://127.0.0.1:19999'})


@contextmanager
def clean_env(**values):
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in ('WHALE_BOT_URL', 'WHALEGIRL_PORT'):
            os.environ.pop(key, None)
        for key, value in values.items():
            os.environ[key] = str(value)
        yield


def docstring(path):
    return path.read_text(encoding='utf-8').split('"""')[1]


class RegistrationFollowsPortTest(unittest.TestCase):
    def test_installer_default_matches_runtime_base_url(self):
        """登记侧和运行期是同一套口径，改端口时两边不会各说各话。"""
        for env in ENVS:
            with self.subTest(env=env), clean_env(**env):
                self.assertEqual(setup.default_bot_url(), bot_control_api.base_url())

    def test_bad_port_degrades_identically_on_both_sides(self):
        for raw in ('abc', '0', '70000', ''):
            with self.subTest(raw=raw), clean_env(WHALEGIRL_PORT=raw):
                self.assertEqual(setup.default_bot_url(), bot_control_api.base_url())


class DocstringTest(unittest.TestCase):
    def test_console_bridge_docstring_has_no_pinned_address(self):
        head = docstring(BOT_MCP)
        self.assertNotIn('127.0.0.1:27499', head)
        self.assertIn('WHALE_BOT_URL', head)
        self.assertIn('WHALEGIRL_PORT', head)

    def test_tools_bridge_never_talks_to_the_console(self):
        """工具桥只走 stdio + 宿主脚本，压根没有控制台地址这一项。"""
        head = docstring(TOOLS_MCP)
        self.assertNotIn('127.0.0.1:', head)
        self.assertNotIn('WHALEGIRL_PORT', head)


if __name__ == '__main__':
    unittest.main()
