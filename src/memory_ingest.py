"""零 token 记忆提取：从主人私聊里按规则挑出「值得记长期」的句子。

参考「神鲸计划」的 memory-ingest.mjs：不调模型、不花 token，只做三件事——

1. 命中说过头的话（以后/记住/必须/不准/改成…）才算候选，闲聊不进来；
2. 含密钥口令的直接跳过，绝不写库；
3. 纯链接/纯数字/纯标点跳过，和库里已有 fact/topic 相似度 >0.8 的也算重复跳过。

默认只报告（preview），apply=True 才写 fact 层；写进去的条目带证据
「主人私聊 YYYY-MM-DD」，以后要追溯能知道来源。
"""
from __future__ import annotations

import json
import re
import time

from .unified_memory import _clean, _similarity

DECISION = re.compile(r'(以后|今后|一律|统一|记住|记下|必须|不准|不许|别把|别再|不要再|'
                      r'只发|只给|只用|改成|换成|定死|说好|约好|提醒我|每次)')
SECRET = re.compile(r'(sk-[A-Za-z0-9]{8,}|api[_-]?key|token|password|密码\s*[:：]|'
                    r'Bearer\s+[A-Za-z0-9])', re.I)
MAX_FACT = 200
MIN_FACT = 6
DUP_THRESHOLD = 0.8
STATES = ('sent', 'partial', 'pending', 'sending', 'verified', 'handled', 'done')


def is_noise(text):
    """纯链接 / 纯数字 / 纯标点：没有任何可记的内容。"""
    value = str(text or '').strip()
    if not value:
        return True
    if re.match(r'^https?://', value, re.I):
        return True
    if re.fullmatch(r'[0-9]+', value):
        return True
    return not re.sub(r'[\W_]+', '', value, flags=re.U)


def candidate_text(text):
    """候选正文：压空白、截到 fact 上限；太短的句子不当长期记忆。"""
    value = _clean(text, MAX_FACT)
    return value


def collect(messages, existing=(), limit=20):
    """从消息列表里挑候选（纯函数，不碰数据库，方便测试）。

    messages: [{'text','ts','room'}]；existing: 已有 fact/topic 正文列表。
    返回 {'checked','candidates':[{'text','keywords','ts','room'}],'skipped':{...}}。
    """
    existing = [str(item or '') for item in (existing or []) if str(item or '').strip()][:2000]
    result = {'checked': 0, 'candidates': [], 'skipped': {'short': 0, 'secret': 0,
                                                          'noise': 0, 'quiet': 0, 'duplicate': 0}}
    limit = max(1, int(limit or 20))
    for message in messages or []:
        result['checked'] += 1
        raw = str((message or {}).get('text') or '')
        text = candidate_text(raw)
        if len(text) < MIN_FACT:
            result['skipped']['short'] += 1
            continue
        if SECRET.search(text):
            result['skipped']['secret'] += 1
            continue
        if is_noise(text):
            result['skipped']['noise'] += 1
            continue
        if not DECISION.search(text):
            result['skipped']['quiet'] += 1
            continue
        if any(_similarity(text, item) > DUP_THRESHOLD for item in existing):
            result['skipped']['duplicate'] += 1
            continue
        if any(_similarity(text, item['text']) > 0.9 for item in result['candidates']):
            result['skipped']['duplicate'] += 1
            continue
        result['candidates'].append({'text': text, 'keywords': keywords_of(text),
                                     'ts': float((message or {}).get('ts') or 0),
                                     'room': str((message or {}).get('room') or '')})
        existing.append(text)
        if len(result['candidates']) >= limit:
            break
    return result


def keywords_of(text):
    """关键词直接用统一记忆那套二元组，不再维护第二份分词规则。"""
    from .unified_memory import _keywords
    return _keywords(text)[:8]


def existing_texts(memory):
    """库里已有的 fact/topic 正文，用来判重。"""
    try:
        rows = memory.db.execute("SELECT content FROM unified_memories "
                                 "WHERE status='active' AND layer IN ('fact','topic') "
                                 "ORDER BY updated DESC LIMIT 2000").fetchall()
    except Exception:
        return []
    return [str(row[0] or '') for row in rows]


def inbox_messages(bot, days=7, limit=200, owners=None):
    """主人最近 N 天的私聊正文（只读 inbox，不碰群聊和别人的私聊）。"""
    try:
        owners = {str(item).strip() for item in (owners if owners is not None else bot.owner_ids())
                  if str(item or '').strip()}
    except Exception:
        return []
    if not owners:
        return []
    since = time.time() - max(0.0, float(days or 0)) * 86400
    try:
        rows = bot.db.execute(
            "SELECT payload,created,room FROM inbox WHERE mode=? AND account=? "
            "AND room LIKE 'private:%' AND created>=? AND state IN ("
            + ','.join('?' * len(STATES)) + ') ORDER BY created DESC LIMIT ?',
            (bot.mode, bot.account(), since, *STATES, max(1, int(limit or 200)))).fetchall()
    except Exception:
        return []
    out = []
    for payload, created, room in rows:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get('sender_id') or '') not in owners:
            continue
        text = str(data.get('text') or '').strip()
        if not text:
            continue
        out.append({'text': text, 'ts': float(created or 0), 'room': str(room or '')})
    return out


def run(memory, messages, apply=False, limit=20, persona='', project='', channel='qq'):
    """预览或落库；只写 fact 层，默认不改任何东西。"""
    result = collect(messages, existing_texts(memory), limit=limit)
    added = 0
    if apply:
        for candidate in result['candidates']:
            stamp = candidate.get('ts') or time.time()
            evidence = '主人私聊 ' + time.strftime('%Y-%m-%d', time.localtime(stamp))
            try:
                memory.remember(candidate['text'], source='chat', importance=60,
                                layer='fact', keywords=candidate['keywords'],
                                persona=persona, project=project, channel=channel,
                                evidence=evidence)
                added += 1
            except (ValueError, TypeError):
                continue
    result['added'] = added
    result['applied'] = bool(apply)
    return result
