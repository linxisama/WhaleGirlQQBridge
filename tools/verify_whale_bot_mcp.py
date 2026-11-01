#!/usr/bin/env python3
"""真实验证 whale_bot MCP server：stdio + JSON-RPC 三段证据。

对 tools/whale_bot_mcp.py 发 initialize → tools/list → tools/call(bot_status)，
另外核对两件 DSH 侧必须成立的事：未知方法回 -32601、stdout 是严格 UTF-8。

启动方式刻意与 DSH profile（cordis.patch.yml 里的 mcp-whale-bot）一致：同一个
解释器 / 同一个脚本路径 / 只设 WHALE_BOT_URL / 不设 PYTHONUTF8 或 PYTHONIOENCODING。
所以这里通过就等于 DSH 拿到的字节是对的；控制台没在跑时脚本不崩，只用退出码区分。

用法::

    python tools/verify_whale_bot_mcp.py
    python tools/verify_whale_bot_mcp.py --url http://127.0.0.1:27499
    python tools/verify_whale_bot_mcp.py --python C:/path/to/python.exe --json

退出码：0 全部通过；2 协议/工具清单/编码不符；3 控制台没在跑；4 子进程异常。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCRIPT = ROOT / 'tools' / 'whale_bot_mcp.py'
PROTOCOL_VERSION = '2025-06-18'
EXPECTED_SERVER = 'whale_bot'
BASE_TOOLS = ('bot_status', 'bot_start', 'bot_stop', 'bot_plugins', 'bot_set_plugin',
              'bot_personas', 'bot_activate_persona', 'bot_send_text')
NEW_TOOLS = ('bot_market_list', 'bot_market_install', 'bot_extensions',
             'bot_set_extension', 'bot_bridge_status', 'bot_skill_tools')
REQUIRED_TOOLS = BASE_TOOLS + NEW_TOOLS
MASKED_ENV = ('PYTHONIOENCODING', 'PYTHONUTF8', 'PYTHONLEGACYWINDOWSSTDIO')
NEWLINE = chr(10)
EXIT_OK = 0
EXIT_PROTOCOL = 2
EXIT_NO_CONSOLE = 3
EXIT_CRASH = 4


class McpSession:
    """拉起 whale_bot_mcp.py，按行为它写一行 JSON、读一行字节。"""

    def __init__(self, python: str, script: Path, url: str = '', timeout: float = 30.0):
        self.timeout = timeout
        self.decode_errors: list = []
        env = dict(os.environ)
        for key in MASKED_ENV:
            env.pop(key, None)
        if url:
            env['WHALE_BOT_URL'] = url
        self.command = [python, str(script)]
        self.proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        self.queue: queue.Queue = queue.Queue()
        self.stderr: list = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            for raw in self.proc.stdout:
                self.queue.put(raw)
        finally:
            self.queue.put(None)

    def _read_stderr(self):
        try:
            for raw in self.proc.stderr:
                self.stderr.append(raw.decode('utf-8', 'replace').rstrip())
        except Exception:
            pass

    def stderr_tail(self, limit: int = 5) -> str:
        return ' | '.join(self.stderr[-limit:])

    def send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False) + NEWLINE
        self.proc.stdin.write(line.encode('utf-8'))
        self.proc.stdin.flush()

    def notify(self, message: dict) -> None:
        self.send(message)

    def wait(self, identifier, timeout=None) -> dict:
        """等 id 匹配的响应；JSON 按严格 UTF-8 解，解不开记进 decode_errors。"""
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('等 id=' + str(identifier) + ' 的响应超时')
            try:
                raw = self.queue.get(timeout=left)
            except queue.Empty:
                raise TimeoutError('等 id=' + str(identifier) + ' 的响应超时') from None
            if raw is None:
                raise EOFError('MCP 子进程已退出（stderr: ' + (self.stderr_tail() or '无') + '）')
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                self.decode_errors.append(str(exc))
                continue
            text = text.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get('id') == identifier:
                return message

    def request(self, identifier, method: str, params=None, timeout=None) -> dict:
        message = {'jsonrpc': '2.0', 'id': identifier, 'method': method}
        if params is not None:
            message['params'] = params
        self.send(message)
        return self.wait(identifier, timeout)

    def close(self) -> int:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return int(self.proc.returncode if self.proc.returncode is not None else -1)


def schema_problems(tools) -> list:
    """工具 schema 合法性：name/description/inputSchema 齐全，required 不含未声明参数。"""
    problems = []
    seen = set()
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            problems.append('工具项不是对象')
            continue
        name = tool.get('name')
        if not isinstance(name, str) or not name.strip():
            problems.append('工具缺少 name')
            name = '?'
        elif name in seen:
            problems.append('工具名重复：' + name)
        else:
            seen.add(name)
        if not str(tool.get('description') or '').strip():
            problems.append(name + ' 缺少 description')
        schema = tool.get('inputSchema')
        if not isinstance(schema, dict):
            problems.append(name + ' 缺少 inputSchema')
            continue
        if schema.get('type') != 'object':
            problems.append(name + ' 的 inputSchema.type 不是 object')
        properties = schema.get('properties')
        if not isinstance(properties, dict):
            problems.append(name + ' 的 inputSchema.properties 不是对象')
            properties = {}
        required = schema.get('required') or []
        if not isinstance(required, list):
            problems.append(name + ' 的 required 不是数组')
        else:
            missing = [key for key in required if key not in properties]
            if missing:
                problems.append(name + ' 的 required 含未声明参数：' + '、'.join(missing))
    return problems


def summarize(text, limit: int = 400) -> str:
    text = '' if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + '…'


def resolved_console_label(url=None) -> str:
    """控制台那一行后面的半句：显式给了地址就照写，否则报运行期解析到的地址。"""
    if url:
        return str(url)
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from src.bot_control_api import base_url
        return base_url() + '（运行期解析：WHALEGIRL_PORT / 控制台端点文件 / 兜底端口）'
    except Exception:
        return '(运行期解析：WHALE_BOT_URL / WHALEGIRL_PORT / 控制台端点文件 / 兜底端口)'


def run(args) -> tuple:
    """跑完所有检查项，返回 (退出码, 检查项列表)。"""
    checks = []
    script = Path(args.script).resolve() if args.script else DEFAULT_SCRIPT

    def add(name, ok, detail=''):
        checks.append({'name': name, 'ok': bool(ok), 'detail': summarize(detail)})
        return bool(ok)

    if not script.is_file():
        add('找到 MCP 脚本', False, str(script) + ' 不存在')
        return EXIT_CRASH, checks
    add('找到 MCP 脚本', True, str(script))

    try:
        session = McpSession(args.python, script, args.url, args.timeout)
    except Exception as exc:
        add('拉起 MCP 子进程', False, type(exc).__name__ + ': ' + str(exc))
        return EXIT_CRASH, checks

    console_down = False
    returncode = -1
    try:
        add('拉起 MCP 子进程', True, ' '.join(session.command))
        init = session.request(1, 'initialize', {'protocolVersion': PROTOCOL_VERSION,
                                                'capabilities': {},
                                                'clientInfo': {'name': 'verify_whale_bot_mcp',
                                                               'version': '1.0.0'}})
        result = init.get('result') if isinstance(init.get('result'), dict) else {}
        server = result.get('serverInfo') if isinstance(result.get('serverInfo'), dict) else {}
        capabilities = result.get('capabilities') if isinstance(result.get('capabilities'), dict) else {}
        protocol = str(result.get('protocolVersion') or '')
        ok_init = (server.get('name') == EXPECTED_SERVER and protocol == PROTOCOL_VERSION
                   and 'tools' in capabilities)
        add('① initialize', ok_init,
            'serverInfo=' + str(server.get('name')) + '/' + str(server.get('version'))
            + ' protocolVersion=' + protocol + ' capabilities=' + '、'.join(sorted(capabilities)))
        session.notify({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

        listed = session.request(2, 'tools/list')
        result = listed.get('result') if isinstance(listed.get('result'), dict) else {}
        tools = result.get('tools') if isinstance(result.get('tools'), list) else []
        problems = schema_problems(tools)
        names = [str(tool.get('name')) for tool in tools if isinstance(tool, dict)]
        missing = [name for name in REQUIRED_TOOLS if name not in names]
        detail = str(len(tools)) + ' 个工具：' + '、'.join(names)
        if missing:
            detail += '；缺少：' + '、'.join(missing)
        if problems:
            detail += '；schema 问题：' + '；'.join(problems)
        add('② tools/list（含 schema 与 6 个新工具）', bool(tools) and not problems and not missing,
            detail)

        add('③ stdout 是严格 UTF-8（DSH 解析前提）', not session.decode_errors,
            '解码失败：' + '；'.join(session.decode_errors) if session.decode_errors
            else 'initialize 与 tools/list 两帧都是合法 UTF-8')

        unknown = session.request(3, 'no/such/method')
        error = unknown.get('error') if isinstance(unknown.get('error'), dict) else {}
        add('④ 未知方法回 -32601', error.get('code') == -32601,
            'error=' + json.dumps(error, ensure_ascii=False))

        call = session.request(4, 'tools/call', {'name': 'bot_status',
                                                 'arguments': {'include_recent': False}})
        call_result = call.get('result') if isinstance(call.get('result'), dict) else {}
        content = call_result.get('content') if isinstance(call_result.get('content'), list) else []
        text = str(content[0].get('text')) if content and isinstance(content[0], dict) else ''
        if call_result.get('isError'):
            console_down = '连不上机器人控制台' in text
            add('⑤ tools/call bot_status', False,
                ('控制台未运行：' if console_down else '工具返回错误：') + text)
        else:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                add('⑤ tools/call bot_status', 'running' in payload,
                    'running=' + str(payload.get('running')) + ' model=' + str(payload.get('model'))
                    + ' connection=' + json.dumps(payload.get('connection'), ensure_ascii=False))
            else:
                add('⑤ tools/call bot_status', False, '不是 JSON 对象：' + text)
    except (TimeoutError, EOFError) as exc:
        add('MCP 会话', False, type(exc).__name__ + ': ' + str(exc))
        return EXIT_CRASH, checks
    except Exception as exc:
        add('MCP 会话', False, type(exc).__name__ + ': ' + str(exc))
        return EXIT_CRASH, checks
    finally:
        returncode = session.close()

    registry = ''
    if session.stderr:
        registry = ' stderr=' + session.stderr_tail()
    add('⑥ 关闭 stdin 后子进程正常退出', returncode == 0,
        'returncode=' + str(returncode) + registry)

    if all(check['ok'] for check in checks):
        return EXIT_OK, checks
    protocol_ok = all(check['ok'] for check in checks[:-1])
    if console_down and protocol_ok:
        return EXIT_NO_CONSOLE, checks
    return EXIT_PROTOCOL, checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='验证 whale_bot MCP server 的 stdio/JSON-RPC 契约')
    parser.add_argument('--python', default=sys.executable, help='运行 MCP 的解释器')
    parser.add_argument('--script', default=str(DEFAULT_SCRIPT), help='MCP 脚本路径')
    parser.add_argument('--url', default=os.environ.get('WHALE_BOT_URL', ''),
                        help='控制台地址（默认不指定：跟着 WHALE_BOT_URL / WHALEGIRL_PORT / '
                             '控制台端点文件运行期解析）')
    parser.add_argument('--timeout', type=float, default=30.0, help='单条响应等待秒数')
    parser.add_argument('--json', action='store_true', help='输出机器可读 JSON')
    args = parser.parse_args(argv)

    code, checks = run(args)
    if args.json:
        print(json.dumps({'ok': code == EXIT_OK, 'exit_code': code, 'checks': checks},
                         ensure_ascii=False, indent=2))
        return code

    print('=== whale_bot MCP 验证（stdio + JSON-RPC，启动方式与 DSH profile 一致）===')
    print('脚本：' + str(Path(args.script).resolve() if args.script else DEFAULT_SCRIPT))
    print('解释器：' + args.python)
    print('控制台：' + resolved_console_label(args.url))
    for check in checks:
        print(('  [PASS] ' if check['ok'] else '  [FAIL] ') + check['name'])
        if check['detail']:
            print('         ' + check['detail'])
    verdict = {
        EXIT_OK: '结论：全部通过，DSH 侧可以直接用 mcp__whale_bot__* 工具。',
        EXIT_NO_CONSOLE: ('结论：MCP 协议、编码与工具清单都正常，只是控制台没在运行——'
                          '先打开鲸鱼娘控制台再跑一次。'),
        EXIT_PROTOCOL: '结论：MCP 协议/编码/工具清单不符，看上面的 FAIL 项。',
        EXIT_CRASH: '结论：MCP 子进程异常，看上面的 FAIL 项与 stderr。',
    }.get(code, '结论：未知退出码 ' + str(code))
    print(verdict)
    print('退出码：' + str(code))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
