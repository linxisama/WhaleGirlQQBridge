#!/usr/bin/env python3
"""本地电脑控制 MCP：只给 DSH 主人私聊使用。

边界：
- 不提供任意 shell；只提供打开路径/网址、键鼠、截图、窗口、剪贴板；
- 打开路径必须存在，网址只允许 http/https；
- 坐标限制在当前虚拟屏幕内；
- 不购物、不付款、不登录账号，由调用方自己决定做什么。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from src import local_control

PROTOCOL_VERSION = '2025-06-18'
SERVER_NAME = 'pc_control'
SERVER_VERSION = '1.0.0'

TOOLS = [
    {'name': 'pc_status',
     'description': '查看当前屏幕尺寸、前台窗口标题和进程名。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'pc_screenshot',
     'description': '截取当前整个虚拟屏幕，并把 PNG 直接返回给模型查看。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'pc_open_path',
     'description': '用系统默认程序打开一个已存在的本地路径（文件或文件夹）。',
     'inputSchema': {'type': 'object', 'properties': {
         'path': {'type': 'string', 'description': '绝对路径'}},
         'required': ['path'], 'additionalProperties': False}},
    {'name': 'pc_open_url',
     'description': '用默认浏览器打开一个 http/https 网址。',
     'inputSchema': {'type': 'object', 'properties': {
         'url': {'type': 'string', 'description': 'http:// 或 https://'}},
         'required': ['url'], 'additionalProperties': False}},
    {'name': 'pc_type_text',
     'description': '向当前前台窗口输入文字（SendKeys）。只用于主人明确要求的操作。',
     'inputSchema': {'type': 'object', 'properties': {
         'text': {'type': 'string', 'description': '要输入的文字，最多 2000 字'}},
         'required': ['text'], 'additionalProperties': False}},
    {'name': 'pc_hotkey',
     'description': '向当前前台窗口发送快捷键，例如 ctrl+s、alt+tab、win+r。',
     'inputSchema': {'type': 'object', 'properties': {
         'keys': {'type': 'string', 'description': '例如 ctrl+s / alt+tab / enter'}},
         'required': ['keys'], 'additionalProperties': False}},
    {'name': 'pc_move',
     'description': '移动鼠标到屏幕坐标。',
     'inputSchema': {'type': 'object', 'properties': {
         'x': {'type': 'integer'}, 'y': {'type': 'integer'}},
         'required': ['x', 'y'], 'additionalProperties': False}},
    {'name': 'pc_click',
     'description': '在屏幕坐标点击鼠标。',
     'inputSchema': {'type': 'object', 'properties': {
         'x': {'type': 'integer'}, 'y': {'type': 'integer'},
         'button': {'type': 'string', 'enum': ['left', 'right', 'middle'], 'default': 'left'},
         'clicks': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'default': 1}},
         'required': ['x', 'y'], 'additionalProperties': False}},
    {'name': 'pc_active_window',
     'description': '返回当前前台窗口标题和进程名。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'pc_clipboard_read',
     'description': '读取当前剪贴板文本（最多 20000 字）。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'pc_clipboard_write',
     'description': '写入剪贴板文本（最多 20000 字）。',
     'inputSchema': {'type': 'object', 'properties': {
         'text': {'type': 'string'}},
         'required': ['text'], 'additionalProperties': False}},
]

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _screen_size():
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def _active_window():
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ''
    try:
        import psutil
        name = psutil.Process(pid.value).name()
    except Exception:
        pass
    return {'title': title, 'pid': int(pid.value), 'process': name}


def _clipboard_read():
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        raise RuntimeError('无法打开剪贴板')
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ''
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ''
        try:
            return ctypes.wstring_at(ptr)[:20000]
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _clipboard_write(text):
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    data = str(text)[:20000]
    if not user32.OpenClipboard(None):
        raise RuntimeError('无法打开剪贴板')
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(data)
        size = ctypes.sizeof(buf)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise RuntimeError('剪贴板分配失败')
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, buf, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise RuntimeError('写入剪贴板失败')
    finally:
        user32.CloseClipboard()


def _click(x, y, button='left', clicks=1):
    w, h = _screen_size()
    x, y = int(x), int(y)
    if not (0 <= x < w and 0 <= y < h):
        raise ValueError('坐标超出屏幕范围')
    user32.SetCursorPos(x, y)
    flags = {'left': (0x0002, 0x0004), 'right': (0x0008, 0x0010), 'middle': (0x0020, 0x0040)}
    down, up = flags.get(button, flags['left'])
    for _ in range(max(1, min(3, int(clicks)))):
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.08)


def _screenshot():
    path = local_control.screenshot()
    data = base64.b64encode(Path(path).read_bytes()).decode('ascii')
    return path, data


def respond(identifier, result=None, error=None):
    message = {'jsonrpc': '2.0', 'id': identifier}
    if error is not None:
        message['error'] = error
    else:
        message['result'] = result if result is not None else {}
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def tool_result(text, image_data=None):
    content = [{'type': 'text', 'text': str(text)}]
    if image_data:
        content.append({'type': 'image', 'data': image_data, 'mimeType': 'image/png'})
    return {'content': content, 'isError': False}


def call_tool(name, args):
    if name == 'pc_status':
        w, h = _screen_size()
        return tool_result(json.dumps({'screen': [w, h], **(_active_window())}, ensure_ascii=False))
    if name == 'pc_screenshot':
        path, data = _screenshot()
        return tool_result('截图已保存：' + path, data)
    if name == 'pc_open_path':
        local_control.open_path(args['path'])
        return tool_result('已请求系统打开：' + str(args['path']))
    if name == 'pc_open_url':
        local_control.open_url(args['url'])
        return tool_result('已请求浏览器打开：' + str(args['url']))
    if name == 'pc_type_text':
        text = str(args.get('text') or '')
        if len(text) > 2000:
            raise ValueError('输入文字最多 2000 字')
        local_control.type_text(text)
        return tool_result('已向当前前台窗口输入 %d 字' % len(text))
    if name == 'pc_hotkey':
        local_control.hotkey(str(args.get('keys') or ''))
        return tool_result('已发送快捷键：' + str(args.get('keys')))
    if name == 'pc_move':
        w, h = _screen_size(); x, y = int(args['x']), int(args['y'])
        if not (0 <= x < w and 0 <= y < h):
            raise ValueError('坐标超出屏幕范围')
        user32.SetCursorPos(x, y)
        return tool_result(f'鼠标已移动到 {x},{y}')
    if name == 'pc_click':
        _click(args['x'], args['y'], args.get('button', 'left'), args.get('clicks', 1))
        return tool_result('已点击：%s,%s' % (args['x'], args['y']))
    if name == 'pc_active_window':
        return tool_result(json.dumps(_active_window(), ensure_ascii=False))
    if name == 'pc_clipboard_read':
        return tool_result(_clipboard_read())
    if name == 'pc_clipboard_write':
        _clipboard_write(str(args.get('text') or ''))
        return tool_result('剪贴板已写入')
    raise ValueError('未知工具：' + name)


def handle(message):
    method = message.get('method')
    identifier = message.get('id')
    params = message.get('params') if isinstance(message.get('params'), dict) else {}
    if method == 'initialize':
        respond(identifier, {
            'protocolVersion': params.get('protocolVersion') or PROTOCOL_VERSION,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': '本机电脑控制只给 DSH 主人私聊使用；群聊不应看到这些工具。',
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
        args = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
        allowed = set(next((tool['inputSchema'].get('properties', {}) for tool in TOOLS
                            if tool['name'] == name), {}))
        extra = set(args) - allowed
        if extra:
            respond(identifier, {'content': [{'type': 'text', 'text': '不支持的参数：' + '、'.join(sorted(extra))}], 'isError': True})
            return
        try:
            respond(identifier, call_tool(name, args))
        except Exception as exc:
            respond(identifier, {'content': [{'type': 'text', 'text': f'{type(exc).__name__}: {exc}'}], 'isError': True})
        return
    if identifier is not None:
        respond(identifier, error={'code': -32601, 'message': f'Unsupported method: {method}'})


def main():
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


if __name__ == '__main__':
    main()
