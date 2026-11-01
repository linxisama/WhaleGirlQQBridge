#!/usr/bin/env python3
"""鲸鱼娘记忆 MCP 服务：把本机机器人的记忆库接给本地 DSH 使用。

只跑在本机 stdio 上（DSH 的 dsh-mcp-client 会启动它），不监听端口、不上传。
DSH 侧注册后会出现这些工具：

- ``search_memory``     搜索手动记忆、结构化记忆、Agent 经验/偏好/技能
- ``remember``          写一条手动记忆（当前人设），机器人聊天时会读到
- ``save_preference``   写一条 Agent 长期偏好（与学习 Agent 共用）
- ``list_preferences``  列出长期偏好
- ``forget_memory``     删除一条手动记忆（按 id）
- ``memory_status``     返回记忆数量与文件位置

环境变量：
- ``WHALE_QQ_HOME``：机器人数据目录，默认 ``%LOCALAPPDATA%/WhaleGirlQQ``
- ``WHALE_QQ_DB``：SQLite 路径，默认 ``<home>/data/qq-only/messages.sqlite3``
- ``WHALE_AGENT_LEARNING``：学习库路径，默认 ``<home>/agent_learning.json``
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.chat_state import maintain_memories as _maintain_memories, memory_similarity
from src.unified_memory import UnifiedMemory

PROTOCOL_VERSION = '2025-06-18'
SERVER_NAME = 'whale_memory'
SERVER_VERSION = '1.0.0'


def home() -> Path:
    explicit = os.environ.get('WHALE_QQ_HOME')
    if explicit:
        return Path(explicit)
    local = os.environ.get('LOCALAPPDATA', '')
    return Path(local) / 'WhaleGirlQQ' if local else Path.cwd()


def db_path() -> Path:
    return Path(os.environ.get('WHALE_QQ_DB') or home() / 'data' / 'qq-only' / 'messages.sqlite3')


def learning_path() -> Path:
    return Path(os.environ.get('WHALE_AGENT_LEARNING') or home() / 'agent_learning.json')


def config_path() -> Path:
    return home() / 'config.qq.yaml'


def active_persona() -> str:
    """从配置里读当前人设名；读不到就返回空串（写记忆时再报错）。"""
    try:
        text = config_path().read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return ''
    in_style = False
    for line in text.splitlines():
        if re.match(r'^style:\s*$', line):
            in_style = True
            continue
        if in_style and re.match(r'^\S', line):
            break
        if in_style:
            match = re.match(r'^\s+active:\s*(\S+)\s*$', line)
            if match:
                return match.group(1).strip('\'"')
    return ''


def connect() -> sqlite3.Connection | None:
    path = db_path()
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def table_exists(conn, name: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def search_memory(query: str = '', limit: int = 20) -> dict:
    limit = max(1, min(50, int(limit or 20)))
    pattern = f"%{(query or '')[:100]}%"
    result = {'query': query, 'manual': [], 'structured': [], 'lessons': [],
              'preferences': [], 'skills': []}
    conn = connect()
    if conn is not None:
        try:
            if table_exists(conn, 'unified_memories'):
                memory = UnifiedMemory(conn)
                result['unified'] = memory.search(query, limit=limit, source='')
            if table_exists(conn, 'self_memories'):
                rows = conn.execute(
                    'SELECT id,persona,content FROM self_memories WHERE enabled=1 AND content LIKE ? '
                    'ORDER BY created DESC LIMIT 100', (pattern,)).fetchall()
                items = [dict(id=row['id'], persona=row['persona'], content=row['content'])
                         for row in rows]
                if query:
                    items.sort(key=lambda item: memory_similarity(query, item['content']), reverse=True)
                result['manual'] = items[:limit]
            if table_exists(conn, 'chat_memories'):
                rows = conn.execute(
                    "SELECT id,scope,subject,content,status,hits FROM chat_memories "
                    "WHERE content LIKE ? AND status<>'superseded' LIMIT 200", (pattern,)).fetchall()
                items = [dict(id=row['id'], scope=row['scope'], subject=row['subject'],
                              content=row['content'], status=row['status'], evidence=row['hits'],
                              score=round(memory_similarity(query, row['content']) if query else 0, 3))
                         for row in rows]
                items.sort(key=lambda item: (item['score'], item['status'] == 'confirmed',
                                             item['evidence'] or 0), reverse=True)
                result['structured'] = items[:limit]
        except sqlite3.Error as exc:
            result['database_error'] = str(exc)
        finally:
            conn.close()
    try:
        data = json.loads(learning_path().read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        data = {}
    if isinstance(data, dict):
        low = (query or '').lower()
        for lesson in data.get('lessons') or []:
            text = f"{lesson.get('intent', '')} {lesson.get('summary', '')}".lower()
            if not low or low in text:
                result['lessons'].append({'id': lesson.get('id'), 'intent': lesson.get('intent'),
                                          'summary': lesson.get('summary'),
                                          'ok': lesson.get('ok'), 'fail': lesson.get('fail'),
                                          'actions': lesson.get('actions')})
        for key, value in (data.get('preferences') or {}).items():
            text = f"{key} {value.get('value', '')}".lower()
            if not low or low in text:
                result['preferences'].append({'key': key, 'value': value.get('value')})
        for skill in data.get('skills') or []:
            text = f"{skill.get('name', '')} {skill.get('description', '')}".lower()
            if not low or low in text:
                result['skills'].append({'id': skill.get('id'), 'name': skill.get('name'),
                                         'description': skill.get('description'),
                                         'triggers': skill.get('triggers')})
    return result


def remember(text: str, persona: str = '') -> dict:
    value = str(text or '').strip()
    if not value:
        raise ValueError('记忆内容不能为空')
    if len(value) > 1000:
        raise ValueError('记忆内容最多 1000 字')
    persona = (persona or active_persona()).strip()
    if not persona:
        raise ValueError('读不到当前人设；请先在控制台选一个人设')
    conn = connect()
    if conn is None:
        raise ValueError('找不到机器人记忆库，请先启动一次机器人')
    try:
        if not table_exists(conn, 'self_memories'):
            conn.execute('CREATE TABLE IF NOT EXISTS self_memories('
                         'id TEXT PRIMARY KEY,persona TEXT,content TEXT,enabled INTEGER,created REAL)')
            conn.commit()
        existing = conn.execute('SELECT id FROM self_memories WHERE persona=? AND content=?',
                                (persona, value)).fetchone()
        if existing:
            return {'id': existing['id'], 'persona': persona, 'content': value,
                    'duplicate': True}
        identifier = uuid.uuid4().hex
        conn.execute('INSERT INTO self_memories VALUES(?,?,?,?,?)',
                     (identifier, persona, value, 1, time.time()))
        conn.commit()
        return {'id': identifier, 'persona': persona, 'content': value, 'duplicate': False}
    finally:
        conn.close()


def forget_memory(id: str) -> dict:
    identifier = str(id or '').strip()
    if not identifier:
        raise ValueError('需要记忆 id')
    conn = connect()
    if conn is None:
        raise ValueError('找不到机器人记忆库')
    try:
        if not table_exists(conn, 'self_memories'):
            return {'removed': 0}
        removed = conn.execute('DELETE FROM self_memories WHERE id=?', (identifier,)).rowcount
        conn.commit()
        return {'removed': int(removed or 0), 'id': identifier}
    finally:
        conn.close()


def load_learning() -> dict:
    try:
        data = json.loads(learning_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def save_learning(data: dict) -> None:
    path = learning_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temp, path)


def save_preference(key: str, value: str) -> dict:
    key, value = str(key or '').strip()[:40], str(value or '').strip()[:500]
    if not key or not value:
        raise ValueError('偏好需要 key 与 value')
    data = load_learning()
    preferences = data.get('preferences') if isinstance(data.get('preferences'), dict) else {}
    preferences[key] = {'value': value, 'note': 'DSH 写入', 'updated': time.time()}
    data['version'] = 1
    data['preferences'] = preferences
    data.setdefault('lessons', [])
    data.setdefault('skills', [])
    save_learning(data)
    return {'key': key, 'value': value}


def list_preferences() -> dict:
    data = load_learning()
    preferences = data.get('preferences') if isinstance(data.get('preferences'), dict) else {}
    return {'preferences': [{'key': key, **value} for key, value in
                            sorted(preferences.items(),
                                   key=lambda kv: float((kv[1] or {}).get('updated') or 0),
                                   reverse=True)]}


def status() -> dict:
    conn = connect()
    counts = {'manual': 0, 'structured': 0}
    if conn is not None:
        try:
            if table_exists(conn, 'self_memories'):
                counts['manual'] = conn.execute('SELECT count(*) FROM self_memories').fetchone()[0]
            if table_exists(conn, 'chat_memories'):
                counts['structured'] = conn.execute('SELECT count(*) FROM chat_memories').fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    data = load_learning()
    return {'persona': active_persona(), **counts,
            'lessons': len(data.get('lessons') or []),
            'preferences': len(data.get('preferences') or {}),
            'skills': len(data.get('skills') or []),
            'database': str(db_path()), 'learning': str(learning_path())}


def remember_unified(text: str, importance: int = 50, source: str = 'dsh') -> dict:
    conn = connect()
    if conn is None:
        raise ValueError('找不到机器人记忆库，请先启动一次机器人')
    try:
        item = UnifiedMemory(conn).remember(text, source=source, importance=importance,
                                            persona=active_persona())
        return {'ok': True, 'item': item}
    finally:
        conn.close()


def read_diary(day: str = '') -> dict:
    conn = connect()
    if conn is None:
        raise ValueError('找不到机器人记忆库，请先启动一次机器人')
    try:
        memory = UnifiedMemory(conn)
        return {'day': day, 'diary': memory.diary(day) if day else None,
                'items': memory.diaries(30)}
    finally:
        conn.close()


def write_diary(day: str, content: str) -> dict:
    conn = connect()
    if conn is None:
        raise ValueError('找不到机器人记忆库，请先启动一次机器人')
    try:
        return {'ok': True, 'diary': UnifiedMemory(conn).write_diary(day, content,
                {'source': 'dsh'})}
    finally:
        conn.close()


TOOLS = [
    {'name': 'search_memory',
     'description': '搜索本机机器人记忆：手动记忆、结构化记忆、Agent 经验/偏好/技能。query 为空时返回最近的。',
     'inputSchema': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'description': '关键词，可留空'},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 20}},
         'additionalProperties': False}},
    {'name': 'remember',
     'description': '写一条手动记忆（当前人设）；机器人聊天时会读到。重复内容不会重复写入。',
     'inputSchema': {'type': 'object', 'properties': {
         'text': {'type': 'string', 'description': '要记住的内容'},
         'persona': {'type': 'string', 'description': '人设名，留空用当前人设'}},
         'required': ['text'], 'additionalProperties': False}},
    {'name': 'save_preference',
     'description': '写一条 Agent 长期偏好（key/value），与学习 Agent 共用。',
     'inputSchema': {'type': 'object', 'properties': {
         'key': {'type': 'string'}, 'value': {'type': 'string'}},
         'required': ['key', 'value'], 'additionalProperties': False}},
    {'name': 'list_preferences',
     'description': '列出 Agent 长期偏好。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'forget_memory',
     'description': '按 id 删除一条手动记忆。',
     'inputSchema': {'type': 'object', 'properties': {
         'id': {'type': 'string'}}, 'required': ['id'], 'additionalProperties': False}},
    {'name': 'memory_status',
     'description': '返回记忆数量、当前人设与文件位置。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'maintain_memory',
     'description': '整理记忆库：合并近重复、清理被覆盖与过期候选。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'remember_unified',
     'description': '写入统一记忆区（QQ/桌面/DSH 共用，可遗忘）。',
     'inputSchema': {'type': 'object', 'properties': {
         'text': {'type': 'string'}, 'importance': {'type': 'integer', 'minimum': 0, 'maximum': 100}},
         'required': ['text'], 'additionalProperties': False}},
    {'name': 'read_diary',
     'description': '读取某天或最近 30 天的日记。',
     'inputSchema': {'type': 'object', 'properties': {
         'day': {'type': 'string', 'description': 'YYYY-MM-DD，留空读列表'}},
         'additionalProperties': False}},
    {'name': 'write_diary',
     'description': '写入一天日记（YYYY-MM-DD）。',
     'inputSchema': {'type': 'object', 'properties': {
         'day': {'type': 'string'}, 'content': {'type': 'string'}},
         'required': ['day', 'content'], 'additionalProperties': False}},
]

def maintain_memory() -> dict:
    conn = connect()
    if conn is None:
        raise ValueError('找不到机器人记忆库，请先启动一次机器人')
    try:
        removed = _maintain_memories(conn)
        return {**removed, 'status': status()}
    finally:
        conn.close()


HANDLERS = {'search_memory': search_memory, 'remember': remember,
            'save_preference': save_preference, 'list_preferences': list_preferences,
            'forget_memory': forget_memory, 'memory_status': status,
            'maintain_memory': maintain_memory,
            'remember_unified': remember_unified, 'read_diary': read_diary,
            'write_diary': write_diary}


def call_tool(name: str, arguments: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        raise ValueError(f'未知工具：{name}')
    arguments = arguments if isinstance(arguments, dict) else {}
    allowed = set(next((tool['inputSchema'].get('properties', {}) for tool in TOOLS
                        if tool['name'] == name), {}))
    extra = set(arguments) - allowed
    if extra:
        raise ValueError('不支持的参数：' + '、'.join(sorted(extra)))
    return handler(**arguments)


def respond(identifier, result=None, error=None):
    message = {'jsonrpc': '2.0', 'id': identifier}
    if error is not None:
        message['error'] = error
    else:
        message['result'] = result if result is not None else {}
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def handle(message: dict):
    method = message.get('method')
    identifier = message.get('id')
    params = message.get('params') or {}
    if method == 'initialize':
        requested = params.get('protocolVersion') if isinstance(params, dict) else None
        respond(identifier, {
            'protocolVersion': requested or PROTOCOL_VERSION,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': ('本机机器人记忆库。先用 search_memory 查已有记忆，'
                             '需要长期保留时用 remember 或 save_preference。'),
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
        name = params.get('name') if isinstance(params, dict) else ''
        arguments = params.get('arguments') if isinstance(params, dict) else {}
        try:
            result = call_tool(str(name or ''), arguments)
            respond(identifier, {'content': [{'type': 'text',
                                              'text': json.dumps(result, ensure_ascii=False)}],
                                 'isError': False})
        except ValueError as exc:
            respond(identifier, {'content': [{'type': 'text', 'text': str(exc)}],
                                 'isError': True})
        except Exception as exc:  # 兜底：工具错误不能打断 stdio 会话
            respond(identifier, {'content': [{'type': 'text',
                                              'text': f'{type(exc).__name__}: {exc}'}],
                                 'isError': True})
        return
    if identifier is not None:
        respond(identifier, error={'code': -32601, 'message': f'Unsupported method: {method}'})


def main() -> int:
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
