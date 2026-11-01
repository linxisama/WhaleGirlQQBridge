"""聊天侧安全边界：QQ 消息只能管理机器人本身，不能操作宿主电脑。

这不是"文档承诺"，而是可执行约束：
- 聊天插件、管理员模块里不允许出现执行系统命令/控制桌面的调用；
- 管理员动作白名单只有暂停/恢复/重连，其它动作在入口就被拒绝；
- 聊天模型本身没有工具调用能力（只生成文本）；工具能力只能由 src/tool_loop.py 从外部
  显式注入，且默认关闭、默认只允许主人触发、接不上时必须回落到纯文本路径；
- 控制电脑的 MCP 服务是独立进程，只由本机 DSH 启动，机器人运行时不加载它。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = [
    r'os\.system\s*\(', r'os\.popen\s*\(', r'subprocess\.(?:run|Popen|call|check_output|check_call)',
    r'ctypes\.(?:windll|WinDLL|CDLL)', r'winreg\.', r'pyautogui\.', r'shutil\.rmtree\s*\(',
    r'win32api\.', r'win32con\.', r'taskkill\s', r'powershell(?:\.exe)?\s', r'shutdown\s*/s',
    r'\.terminate\s*\(\s*\)\s*#\s*host', r'\beval\s*\(', r'\bexec\s*\(',
]
SCAN = [
    'src/plugins/pc_control.py', 'src/plugins/__init__.py', 'src/robot_admin.py',
    'src/admin_session.py', 'src/pc_service.py', 'src/pc_backend.py',
] + [f'src/plugins/{path.name}' for path in
     sorted((Path(__file__).resolve().parents[1] / 'src' / 'plugins').glob('*.py'))
     if path.name != 'qqagent_bridge.py']  # 该能力桥默认关闭、仅主人，单独安全测试


class ChatHostBoundaryTests(unittest.TestCase):
    def test_chat_admin_code_has_no_host_control_calls(self):
        problems = []
        for relative in SCAN:
            path = ROOT / relative
            if not path.is_file():
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            for pattern in FORBIDDEN:
                if re.search(pattern, text):
                    problems.append(f'{relative} 命中 {pattern}')
        self.assertEqual(problems, [], '聊天侧出现系统控制调用：' + '；'.join(problems))

    def test_admin_actions_are_robot_only(self):
        from src import robot_admin
        self.assertEqual(robot_admin.READ_ACTIONS,
                         {'status', 'connections', 'queue', 'logs', 'personas', 'plugins', 'speed'})
        self.assertEqual(robot_admin.WRITE_ACTIONS, {'pause', 'resume', 'reconnect'})

    def test_console_backend_rejects_host_actions(self):
        from src.robot_admin import ConsoleBackend

        class Dummy:
            running = False

        backend = ConsoleBackend(Dummy())
        for action in ('run_program', 'screenshot', 'shutdown', 'volume', 'shell'):
            with self.assertRaises(ValueError) as ctx:
                import asyncio
                asyncio.run(backend.execute(action))
            self.assertIn('不能操作宿主机器', str(ctx.exception))

    def test_pc_service_rejects_host_actions(self):
        from src.pc_service import PcService

        class Dummy:
            db = None

            def execute(self, *args, **kwargs):
                return None

        service = PcService.__new__(PcService)
        service.runtime = Dummy()
        with self.assertRaises(ValueError):
            service.request({}, 'shutdown', '')

    def test_chat_command_for_host_stays_help(self):
        from src.plugins.pc_control import parse_command
        self.assertEqual(parse_command('电脑 关机'), ('help', ''))
        self.assertEqual(parse_command('机器人 状态'), ('status', ''))
        self.assertEqual(parse_command('运行 calc'), None)

    def test_pc_control_is_off_and_says_no_host_control(self):
        from src.plugins.pc_control import PcControl
        self.assertFalse(PcControl.META.enabled_by_default)
        self.assertIn('不操作宿主机器', PcControl.META.description)

    def test_chat_cannot_toggle_custom_plugins(self):
        from src.plugins.pc_control import PcControl, SAFE_CHAT_PLUGINS

        class Meta:
            name = '自定义插件'

        class Plugin:
            META = Meta()

        class Plugins:
            _plugins = {'custom_x': Plugin()}

        class Runtime:
            plugins = Plugins()

        control = PcControl.__new__(PcControl)
        self.assertNotIn('custom_x', SAFE_CHAT_PLUGINS)
        message = control._plugins(Runtime(), '开 custom_x')
        self.assertIn('不开放聊天侧开关', message)

    def test_chat_model_has_no_tool_calling_by_default(self):
        """"聊天模型默认不具备工具能力"——由四组门控证据守住，而不是"源码里不许出现 tools"。

        WP2 之后 ai_engine 多了 tools 透传形参与 tool_calls 解析，旧断言（源码里不许出现
        tools/tool_calls）字面上必然失败，但它守的安全意图仍然有效，只是契约变成了：
        引擎自己不会开工具、也不会执行工具；工具环默认关闭、默认只认主人、接不上必须回落。
        下面每条对应一个门，任何一条被改成"默认放开"都会在这里红。
        """
        from src import tool_loop

        # 门 1：默认关闭 + 默认只允许主人（读不到配置时的真实默认值）。
        defaults = tool_loop.loop_settings({})
        self.assertFalse(defaults.enabled, '工具环默认必须关闭，不允许默认具备工具能力')
        self.assertTrue(defaults.owner_only, '工具环默认只允许主人触发')
        # 权限门是这一个配置键，改成默认放开会被上面一条抓住。
        self.assertFalse(
            tool_loop.loop_settings({'ai': {'tool_loop_owner_only': False}}).owner_only,
            'ai.tool_loop_owner_only 必须真的控制主人限定')
        # 非主人调用 should_use 必须 False：由 tests/test_tool_loop.py:629
        # ShouldUseTests.test_stranger_is_blocked_by_default 覆盖，此处不重复造模型。

        # 门 2：ai_engine 只透传，不会自己开工具、不会执行工具。
        text = (ROOT / 'src' / 'ai_engine.py').read_text(encoding='utf-8')
        self.assertIsNone(
            re.search(r'^\s*(?:from|import)\s+.*\btool_loop\b', text, re.MULTILINE),
            'ai_engine 不应自己导入工具环（工具表只能从外部注入）')
        self.assertIsNone(
            re.search(r'run_on_before_tool|run_on_after_tool|call_tool\s*\(', text),
            'ai_engine 不应执行工具或派发工具钩子')
        self.assertRegex(
            text, r'tools\s*:\s*list\s*\|\s*None\s*=\s*None',
            'tools 必须以"默认 None"的形参透传：调用方不给工具，引擎就没有工具')

        # 门 3：主链路接线必须有回落——工具环不接管这一轮时原样走原来的回复路径。
        wiring = (ROOT / 'src' / 'qq_app.py').read_text(encoding='utf-8')
        self.assertEqual(wiring.count('tool_loop.should_use('), 1,
                         '工具环只允许有一个受门控的入口')
        start = wiring.find('tool_loop.should_use(')
        self.assertGreater(start, 0, 'qq_app 主链路里找不到工具环的门（should_use）')
        window = wiring[start:start + 1400]
        self.assertRegex(
            window,
            r'if\s+reply\s+is\s+None:\s*\n\s+reply\s*=\s*await\s+self\.plugins\.generate_reply\(',
            '工具环必须用 "if reply is None:" 回落到原来的 generate_reply 路径')
        self.assertLess(window.index('respond_with_tools'), window.index('if reply is None:'),
                        '回落判定必须排在工具环调用之后')

    def test_mcp_control_server_is_separate_from_bot_runtime(self):
        # 机器人运行时不导入控制 MCP；它只由 DSH 以独立进程启动。
        for relative in ('src/qq_app.py', 'src/console_agent.py', 'src/plugins/manager.py'):
            text = (ROOT / relative).read_text(encoding='utf-8')
            self.assertNotIn('whale_bot_mcp', text, relative)
            self.assertNotIn('bot_control_api', text, relative)


if __name__ == '__main__':
    unittest.main()
