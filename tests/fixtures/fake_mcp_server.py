"""极简 stdio MCP server fixture：手写 JSON-RPC 2.0，只给 tests/test_mcp_client.py 用。

手写而不是引 SDK：venv 里没有 mcp SDK，也不想为一颗假服务器装依赖。
开关：--exit-immediately（握手前就退出）、--handshake-sleep N（拖住 initialize）、
--ignore-eof（stdin 关了也不退，用来验证客户端强杀整棵进程树）。
"""
from __future__ import annotations

import json
import os
import sys
import time

SLOW_SECONDS = 3.0
PID_FILE_ENV = 'MCP_FAKE_PID_FILE'

TOOLS = [
    {'name': 'echo_cn', 'description': '中文回显',
     'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}},
    {'name': 'slow', 'description': f'睡 {SLOW_SECONDS:g} 秒再回',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'boom', 'description': '返回 isError:true',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'emit_bad_json', 'description': '先写一行坏 JSON 再正常回包',
     'inputSchema': {'type': 'object', 'properties': {}}},
]


def write(message: dict) -> None:
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()


def write_raw(line: str) -> None:
    sys.stdout.buffer.write((line + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()


def result(request_id, payload: dict) -> dict:
    return {'jsonrpc': '2.0', 'id': request_id, 'result': payload}


def text_payload(text: str, is_error: bool = False) -> dict:
    return {'content': [{'type': 'text', 'text': text}], 'isError': is_error}


def handle(message: dict) -> None:
    method = message.get('method')
    request_id = message.get('id')
    params = message.get('params') or {}
    if method == 'initialize':
        write(result(request_id, {
            'protocolVersion': params.get('protocolVersion') or '2025-06-18',
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'fake-mcp', 'version': '0.1'},
        }))
    elif method == 'notifications/initialized':
        return
    elif method == 'tools/list':
        write(result(request_id, {'tools': TOOLS}))
    elif method == 'tools/call':
        name = params.get('name')
        arguments = params.get('arguments') or {}
        if name == 'echo_cn':
            write({'jsonrpc': '2.0', 'method': 'notifications/message',
                   'params': {'level': 'info', 'message': '回显之前先发一条日志'}})
            text = str(arguments.get('text') or '默认中文')
            write(result(request_id, text_payload('回显：' + text)))
        elif name == 'slow':
            time.sleep(SLOW_SECONDS)
            write(result(request_id, text_payload('慢工具完成')))
        elif name == 'boom':
            write(result(request_id, text_payload('故意的失败：boom', True)))
        elif name == 'emit_bad_json':
            write_raw('### 这不是一行 JSON ###')
            write(result(request_id, text_payload('坏行之后仍然回包')))
        else:
            write({'jsonrpc': '2.0', 'id': request_id,
                   'error': {'code': -32602, 'message': '未知工具 ' + str(name)}})
    elif request_id is not None:
        write({'jsonrpc': '2.0', 'id': request_id,
               'error': {'code': -32601, 'message': '未实现的方法 ' + str(method)}})


def main(argv) -> int:
    if '--exit-immediately' in argv:
        sys.stderr.write('fake-mcp: 收到 --exit-immediately，握手前退出\n')
        sys.stderr.flush()
        return 3
    handshake_sleep = 0.0
    for index, item in enumerate(argv):
        if item == '--handshake-sleep' and index + 1 < len(argv):
            handshake_sleep = float(argv[index + 1])
    pid_file = os.environ.get(PID_FILE_ENV)
    if pid_file:
        with open(pid_file, 'w', encoding='utf-8') as stream:
            stream.write(str(os.getpid()))
    first = True
    for raw in sys.stdin.buffer:
        line = raw.decode('utf-8', 'replace').strip()
        if not line:
            continue
        if first and handshake_sleep:
            time.sleep(handshake_sleep)
        first = False
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            handle(message)
    if '--ignore-eof' in argv:
        time.sleep(30)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
