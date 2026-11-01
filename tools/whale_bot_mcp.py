#!/usr/bin/env python3
"""鲸鱼娘控制台 MCP 服务：让本地 DSH 直接操作机器人。

仅本机：通过 ``WHALE_BOT_URL`` 调用控制台已有接口；没设就跟随控制台端口
（``WHALEGIRL_PORT``，非法值兜底 27499），见 ``src/bot_control_api.base_url()``。

工具白名单：
- 运行控制：状态、启停、插件开关、人设切换、发文字消息；
- 扩展市场：市场清单、市场安装（必须 confirm:true）、已装扩展、扩展启停、
  能力桥状态、skill 工具清单——这几个统一返回结构化 JSON（ok / error.code），
  后端端点还没实现时回 ``not_implemented``，不会让 MCP 进程抛栈退出。

不提供改密钥、改接入、执行代码或任意文件的能力。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import bot_control_api as api

PROTOCOL_VERSION = '2025-06-18'
SERVER_NAME = 'whale_bot'
SERVER_VERSION = '1.1.0'


def force_utf8_stdio() -> None:
    """MCP stdio 必须说 UTF-8。

    Windows 下 python 的管道默认走 ANSI 代码页（中文系统是 cp936），对端按 UTF-8
    解析 initialize / tools/list 就会失败——DSH 侧表现为 server unavailable。
    这里显式把两个方向都锁成 UTF-8，不依赖父进程是否设了 PYTHONUTF8。
    """
    for stream, errors in ((sys.stdin, 'replace'), (sys.stdout, 'strict')):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding='utf-8', errors=errors, newline='\n')
        except (ValueError, OSError):
            pass


TOOLS = [
    {'name': 'bot_status',
     'description': '查看机器人运行状态、接入连接、模型与人设；可选返回最近几条收发记录。',
     'inputSchema': {'type': 'object', 'properties': {
         'include_recent': {'type': 'boolean', 'default': False},
         'recent_limit': {'type': 'integer', 'minimum': 1, 'maximum': 30, 'default': 10}},
         'additionalProperties': False}},
    {'name': 'bot_start', 'description': '启动机器人服务（开始接收并回复 QQ 消息）。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'bot_stop', 'description': '停止机器人服务。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'bot_plugins', 'description': '列出插件及启用状态。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'bot_set_plugin', 'description': '开启或关闭一个插件。',
     'inputSchema': {'type': 'object', 'properties': {
         'id': {'type': 'string'}, 'enabled': {'type': 'boolean'}},
         'required': ['id', 'enabled'], 'additionalProperties': False}},
    {'name': 'bot_personas', 'description': '列出人设与当前人设。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'bot_activate_persona', 'description': '切换当前人设（失败会返回原因）。',
     'inputSchema': {'type': 'object', 'properties': {
         'name': {'type': 'string'}}, 'required': ['name'], 'additionalProperties': False}},
    {'name': 'bot_send_text',
     'description': '用机器人真实发送一条 QQ 文字消息（群或私聊）。会真的发出去，只在用户明确要求时调用。',
     'inputSchema': {'type': 'object', 'properties': {
         'target_type': {'type': 'string', 'enum': ['group', 'private']},
         'target_id': {'type': 'string', 'description': '群号或 QQ 号'},
         'text': {'type': 'string', 'description': '消息正文，最多 2000 字'}},
         'required': ['target_type', 'target_id', 'text'], 'additionalProperties': False}},
    {'name': 'bot_market_list',
     'description': '列出扩展市场条目（只读元数据）。可按分类或关键词过滤，limit 上限 50。',
     'inputSchema': {'type': 'object', 'properties': {
         'category': {'type': 'string', 'description': '分类标识或分类名（子串匹配）'},
         'keyword': {'type': 'string', 'description': '在 id/名称/简介里做子串匹配'},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 20},
         'refresh': {'type': 'boolean', 'default': False,
                     'description': 'true 时先联网刷新市场清单'}},
         'additionalProperties': False}},
    {'name': 'bot_market_install',
     'description': '安装一个市场扩展（按扩展 id）或本地 zip 扩展包。会真的下载/写入扩展目录，'
                        '必须显式传 confirm:true；不传只返回 confirmation_required，不产生任何改动。',
     'inputSchema': {'type': 'object', 'properties': {
         'extension_id': {'type': 'string', 'description': '市场条目 id'},
         'zip_path': {'type': 'string', 'description': '本地 .zip 扩展包的绝对路径'},
         'confirm': {'type': 'boolean', 'description': '必须为 true 才会真的安装'}},
         'required': ['confirm'], 'additionalProperties': False}},
    {'name': 'bot_extensions',
     'description': '列出已安装的扩展及其启用状态。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'bot_set_extension',
     'description': '启用或停用一个已安装的扩展。',
     'inputSchema': {'type': 'object', 'properties': {
         'id': {'type': 'string', 'description': '扩展 id'},
         'enabled': {'type': 'boolean'}},
         'required': ['id', 'enabled'], 'additionalProperties': False}},
    {'name': 'bot_bridge_status',
     'description': '能力桥只读状态：provider/hook 覆盖、缺哪些配置、最近调用统计（后端提供才有）。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'bot_skill_tools',
     'description': '列出 skill 提供的工具（技能工具清单）。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
]

HANDLERS = {
    'bot_status': api.status,
    'bot_start': api.start,
    'bot_stop': api.stop,
    'bot_plugins': api.plugins,
    'bot_set_plugin': lambda id, enabled: api.set_plugin(id, enabled),
    'bot_personas': api.personas,
    'bot_activate_persona': api.activate_persona,
    'bot_send_text': api.send_text,
    # 扩展市场 / 能力桥：结果一律是结构化 JSON（ok / error.code）
    'bot_market_list': api.market_list,
    'bot_market_install': api.install_market,
    'bot_extensions': api.extensions,
    'bot_set_extension': lambda id, enabled: api.set_extension(id, enabled),
    'bot_bridge_status': api.bridge_status,
    'bot_skill_tools': api.skill_tools,
}

# 这些工具的失败也走结构化载荷，不让 MCP 进程抛栈退出。
STRUCTURED_TOOLS = frozenset({
    'bot_market_list', 'bot_market_install', 'bot_extensions',
    'bot_set_extension', 'bot_bridge_status', 'bot_skill_tools',
})


def respond(identifier, result=None, error=None):
    message = {'jsonrpc': '2.0', 'id': identifier}
    if error is not None:
        message['error'] = error
    else:
        message['result'] = result if result is not None else {}
    payload = json.dumps(message, ensure_ascii=False)
    try:
        sys.stdout.write(payload + '\n')
    except UnicodeEncodeError:
        # 极端情况下 stdout 仍是 ANSI 代码页：退回纯 ASCII 也要让对端解析成功。
        sys.stdout.write(json.dumps(message, ensure_ascii=True) + '\n')
    sys.stdout.flush()


def structured_call(handler, arguments: dict) -> dict:
    """结构化工具的兜底：缺接口回 not_implemented，其余异常收敛成 error.code。"""
    try:
        payload = handler(**arguments)
    except api.ConfirmationRequired as exc:
        return api.structured_error('confirmation_required', str(exc))
    except api.BotUnavailable as exc:
        return api.structured_error('console_unavailable', str(exc))
    except (ValueError, TypeError) as exc:
        return api.structured_error('invalid_argument', str(exc))
    except Exception as exc:
        return api.structured_error('internal_error', f'{type(exc).__name__}: {exc}')
    if not isinstance(payload, dict):
        return api.structured_error('internal_error', '工具返回了非对象结果')
    return payload


def structured_result(payload: dict) -> dict:
    """结构化载荷按 MCP 成功结果返回：isError=False，正文是 JSON，ok/error 由正文表达。"""
    return {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
            'isError': False}

def handle(message: dict):
    method = message.get('method')
    identifier = message.get('id')
    params = message.get('params') if isinstance(message.get('params'), dict) else {}
    if method == 'initialize':
        respond(identifier, {
            'protocolVersion': params.get('protocolVersion') or PROTOCOL_VERSION,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': ('本机鲸鱼娘控制台。发消息前先确认用户意图；'
                             'bot_status 看清运行状态再操作，失败按返回原因说明。'),
        })
        return
    if method in ('notifications/initialized', 'notifications/cancelled'):
        return
    if method == 'ping':
        respond(identifier, {})
        return
    if method == 'tools/list':
        respond(identifier, {'tools': TOOLS})
        return
    if method == 'tools/call':
        name = str(params.get('name') or '')
        arguments = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
        handler = HANDLERS.get(name)
        if handler is None:
            respond(identifier, {'content': [{'type': 'text', 'text': f'未知工具：{name}'}],
                                 'isError': True})
            return
        allowed = set(next((tool['inputSchema'].get('properties', {}) for tool in TOOLS
                            if tool['name'] == name), {}))
        extra = set(arguments) - allowed
        if extra:
            respond(identifier, {'content': [{'type': 'text',
                                              'text': '不支持的参数：' + '、'.join(sorted(extra))}],
                                 'isError': True})
            return
        if name in STRUCTURED_TOOLS:
            respond(identifier, structured_result(structured_call(handler, arguments)))
            return
        try:
            result = handler(**arguments)
            respond(identifier, {'content': [{'type': 'text',
                                              'text': json.dumps(result, ensure_ascii=False)}],
                                 'isError': False})
        except (ValueError, api.BotUnavailable) as exc:
            respond(identifier, {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True})
        except Exception as exc:
            respond(identifier, {'content': [{'type': 'text',
                                              'text': f'{type(exc).__name__}: {exc}'}],
                                 'isError': True})
        return
    if identifier is not None:
        respond(identifier, error={'code': -32601, 'message': f'Unsupported method: {method}'})


def main() -> int:
    force_utf8_stdio()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            handle(message)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
