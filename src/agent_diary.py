"""AI 日记：让模型按当天的聊天与记忆写一篇第一人称日记，每天存一个 txt。

不是管理员手写，也不是统计模板：没有模型、当天没内容或生成失败时明确报错，
由调用方决定是否重试，绝不写一篇假日记。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
from pathlib import Path

DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MIN_CHARS = 80
MAX_CHARS = 12000
MAX_MESSAGES = 80
MAX_MESSAGE_CHARS = 160
MAX_MEMORIES = 12
STATES = ('sent', 'partial', 'pending', 'sending', 'verified', 'handled', 'done')
SYSTEM_PROMPT = (
    '你在写自己的日记。只用第一人称，写今天真实发生的事和你的感受；'
    '不要编造聊天里没有的事，不要写成分析报告或流水账；'
    '不要输出标题、日期、Markdown 标题或代码块，只输出正文。'
)


def today():
    return datetime.date.today().isoformat()


def normalize_day(day=None):
    day = str(day or '').strip()[:10]
    if not day:
        return today()
    if not DAY_RE.fullmatch(day):
        raise ValueError('日期需要 YYYY-MM-DD')
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        raise ValueError('日期不是有效的日历日期') from None
    return day


def diary_dir_for(bot):
    for attr in ('diary_dir',):
        value = getattr(bot, attr, None)
        if value:
            return Path(value)
    memory = getattr(bot, 'unified_memory', None)
    value = getattr(memory, 'diary_dir', None) if memory is not None else None
    if value:
        return Path(value)
    legacy = getattr(bot, 'diary_path', None)
    if legacy:
        return Path(legacy).parent / '日记'
    return Path(getattr(bot, 'root', '.') or '.') / 'data' / 'qq-only' / '日记'


def diary_file(bot, day=None):
    return diary_dir_for(bot) / (normalize_day(day) + '.txt')


def has_day(bot, day=None):
    try:
        path = diary_file(bot, day)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def list_days(bot, limit=90):
    folder = diary_dir_for(bot)
    items = []
    try:
        files = list(folder.glob('????-??-??.txt'))
    except OSError:
        files = []
    for path in files:
        if not DAY_RE.fullmatch(path.stem):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({'day': path.stem, 'path': str(path), 'bytes': stat.st_size,
                      'mtime': stat.st_mtime})
    items.sort(key=lambda item: item['day'], reverse=True)
    return items[:max(1, min(365, int(limit or 90)))]


def _day_bounds(day):
    date = datetime.date.fromisoformat(day)
    start = datetime.datetime.combine(date, datetime.time.min)
    return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()


def _owner_ids(bot):
    value = getattr(bot, 'owner_ids', None)
    if callable(value):
        try:
            return {str(x) for x in (value() or []) if str(x)}
        except Exception:
            return set()
    return {str(x) for x in (value or []) if str(x)} if value else set()


def _account(bot):
    value = getattr(bot, 'account', '')
    if callable(value):
        try:
            return str(value() or '')
        except Exception:
            return ''
    return str(value or '')


def _payload(row):
    try:
        data = json.loads(row or '{}')
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def gather_messages(bot, day, limit=MAX_MESSAGES):
    day = normalize_day(day)
    start, end = _day_bounds(day)
    db = getattr(bot, 'db', None)
    if db is None:
        return []
    mode = str(getattr(bot, 'mode', '') or '')
    account = _account(bot)
    where = ['created>=?', 'created<?', 'state IN (' + ','.join('?' * len(STATES)) + ')']
    params = [start, end, *STATES]
    if mode:
        where.append('mode=?')
        params.append(mode)
    if account:
        where.append('account=?')
        params.append(account)
    try:
        rows = db.execute(
            'SELECT room,payload FROM inbox WHERE ' + ' AND '.join(where) +
            ' ORDER BY created', params).fetchall()
    except Exception:
        return []
    owners = _owner_ids(bot)
    messages = []
    for room, raw in rows:
        data = _payload(raw)
        if data.get('notice'):
            continue
        text = str(data.get('text') or '').strip()
        if not text:
            continue
        room = str(room or '')
        sender = str(data.get('sender_id') or '')
        if room.startswith('private:') and owners and sender not in owners:
            continue
        name = str(data.get('sender_name') or data.get('sender') or sender or '对方')[:24]
        messages.append({'room': room, 'name': name, 'text': text[:MAX_MESSAGE_CHARS],
                         'private': room.startswith('private:')})
    return messages[-max(1, min(300, int(limit or MAX_MESSAGES))):]


def gather_memories(memory, day=None, limit=MAX_MEMORIES):
    if memory is None:
        return []
    try:
        items = memory.search('', limit=30)
    except Exception:
        return []
    out = []
    seen = set()
    for item in items or []:
        text = str((item or {}).get('content') or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text[:200])
        if len(out) >= limit:
            break
    return out


def _previous_text(bot, day, max_chars=800):
    folder = diary_dir_for(bot)
    try:
        files = sorted(folder.glob('????-??-??.txt'))
    except OSError:
        return ''
    for path in reversed(files):
        if path.stem < day:
            try:
                return path.read_text(encoding='utf-8', errors='ignore')[-max_chars:].strip()
            except OSError:
                return ''
    return ''


def build_prompt(day, persona='', messages=(), memories=(), previous='', max_chars=6000):
    day = normalize_day(day)
    blocks = ['请写 ' + day + ' 的日记。']
    persona = str(persona or '').strip()[:1200]
    if persona:
        blocks.append('【我的人设】\n' + persona)
    lines = []
    for item in list(messages or [])[:MAX_MESSAGES]:
        text = str((item or {}).get('text') or '').strip()
        if not text:
            continue
        kind = '私聊' if (item or {}).get('private') else '群聊'
        name = str((item or {}).get('name') or '对方')[:24]
        lines.append('- ' + kind + ' ' + name + '：' + text)
    blocks.append('【今天的聊天片段】\n' + ('\n'.join(lines) if lines else '（今天没有记录到聊天。）'))
    memory_lines = ['- ' + str(text)[:200] for text in list(memories or [])[:MAX_MEMORIES] if str(text).strip()]
    if memory_lines:
        blocks.append('【我还记得的事】\n' + '\n'.join(memory_lines))
    previous = str(previous or '').strip()[-800:]
    if previous:
        blocks.append('【前一天的日记（只参考语气，不要重复内容）】\n' + previous)
    return '\n\n'.join(blocks)[:max(500, int(max_chars or 6000))]


def clean_text(text):
    text = str(text or '').strip()
    text = re.sub(r'^\x60\x60\x60[a-zA-Z]*\n?', '', text)
    text = re.sub(r'\n?\x60\x60\x60$', '', text).strip()
    lines = text.splitlines()
    if lines and re.match(r'^#+\s*\d{4}-\d{2}-\d{2}\s*$', lines[0].strip()):
        lines = lines[1:]
    return '\n'.join(lines).strip()


async def compose(bot, engine, day=None, timeout=90):
    if engine is None:
        raise ValueError('没有可用的模型配置，无法让 AI 写日记')
    day = normalize_day(day)
    memory = getattr(bot, 'unified_memory', None)
    persona = ''
    style = getattr(bot, 'style', None)
    if style is not None:
        try:
            # 用当前启用的人设；不要传空字符串，full_prompt 期望的是风格对象或 None。
            persona = str(style.full_prompt() or '')
        except Exception:
            persona = ''
    prompt = build_prompt(day, persona=persona, messages=gather_messages(bot, day),
                          memories=gather_memories(memory, day),
                          previous=_previous_text(bot, day))
    try:
        raw = await asyncio.wait_for(engine.chat(
            [{'role': 'user', 'content': prompt}],
            system_prompt=SYSTEM_PROMPT, temperature=0.7, max_tokens=900), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError('模型写日记超时（%d 秒）' % int(timeout)) from None
    if not isinstance(raw, str):
        raw = str(raw or '')
    if not raw.strip():
        # 引擎把 HTTP 403/超时等失败吞成空串，原因在 last_error 里；别误报成“写得太短”。
        reason = str(getattr(engine, 'last_error', '') or '').strip()
        raise RuntimeError('模型没有返回日记内容' + (('：' + reason) if reason else ''))
    text = clean_text(raw)[:MAX_CHARS]
    if len(text) < MIN_CHARS:
        raise ValueError('模型写的日记太短（%d 字），没有保存' % len(text))
    return text


def save(bot, day, text, meta=None):
    day = normalize_day(day)
    text = str(text or '').strip()
    if not text:
        raise ValueError('日记正文不能为空')
    memory = getattr(bot, 'unified_memory', None)
    if memory is None:
        raise ValueError('记忆库未就绪，无法保存日记')
    info = memory.write_diary(day, text, dict(meta or {}, kind='ai'))
    path = diary_file(bot, day)
    return {'day': day, 'path': str(path), 'chars': len(text), 'diary': info}
