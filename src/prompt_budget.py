"""提示词预算：给注入的每一块单独封顶，再按总预算从低优先级开始丢。

算法参考「神鲸计划」的注入硬顶（UPDATE.md）：每块封顶（memory 1200 / diary 900 /
persona 800 / people 800 / stickers 700 / recent 1200）、整段 6000 字封顶，超了
先丢 stickers → people → recent，再丢 memory / diary，并在日志里打「提示构成」。

本项目注入块更少，所以只保留通用实现：compose(blocks, limit) 的 blocks 是
(名字, 文本, 单块上限, 优先级) 四元组，**优先级数字大的先被丢**（0 = 绝不丢），
所以调用方不必把丢弃顺序写死在预算模块里，也不会因为新增一块就漏掉排序。
"""
from __future__ import annotations

DEFAULT_LIMIT = 6000


def clip(text, limit):
    """按字符数截断；不截断 JSON/标记中间，调用方自己保证块是完整句子。"""
    text = str(text or '')
    limit = max(0, int(limit or 0))
    if limit and len(text) > limit:
        return text[:limit]
    return text


def compose(blocks, limit=DEFAULT_LIMIT):
    """把注入块拼成一段文本，返回 (文本, 统计)。

    统计里带每块名字、字符数、是否被丢、是否被截断，以及总字数 —— 日志要能看出
    「这一轮到底注入了什么」，否则省 token 的改动没法验证。
    """
    limit = max(0, int(limit if limit is not None else DEFAULT_LIMIT))
    kept = []
    for name, text, cap, priority in blocks:
        raw = str(text or '').strip()
        if not raw:
            continue
        body = clip(raw, cap) if cap else raw
        kept.append({'name': str(name), 'text': body, 'chars': len(body),
                     'priority': int(priority), 'dropped': False,
                     'truncated': len(body) < len(raw)})
    total = sum(item['chars'] for item in kept)
    for item in sorted(kept, key=lambda x: x['priority'], reverse=True):
        if total <= limit:
            break
        item['dropped'] = True
        total -= item['chars']
    out = '\n'.join(item['text'] for item in kept if not item['dropped'])
    stats = {'total': len(out), 'limit': limit,
             'blocks': [{k: item[k] for k in ('name', 'chars', 'dropped', 'truncated')}
                        for item in kept]}
    return out, stats


def trim_history(messages, budget, protect_last=1):
    """按字符预算从最旧的历史开始删，返回 (保留的消息, 统计)。

    系统提示和历史以前各吃 6000 字，合计能到 12000 字；这里让历史只使用
    「总注入预算 - 系统提示」剩下的额度。protect_last 保底不删当前轮，
    即使当前消息本身就很长也不截断用户原话。
    """
    items = list(messages or [])
    limit = max(0, int(budget or 0))
    protect_last = max(0, int(protect_last or 0))
    before = sum(_message_chars(item) for item in items)
    removed = []
    total = before
    while items and total > limit and len(items) > protect_last:
        item = items.pop(0)
        removed.append(item)
        total -= _message_chars(item)
    stats = {'limit': limit, 'before': before, 'after': total,
             'removed_messages': len(removed),
             'removed_chars': before - total}
    return items, stats


def _message_chars(message):
    """一条对话消息的字符数；兼容多段 content 和测试里的非字符串。"""
    if not isinstance(message, dict):
        return len(str(message or ''))
    content = message.get('content', '')
    if isinstance(content, str):
        return len(content)
    return len(str(content or ''))


def summary(stats):
    """一行中文，给日志/事件用：提示构成：记忆 1180 + 日记 700 = 1880 字（上限 6000）。"""
    stats = stats or {}
    parts = []
    for block in stats.get('blocks') or []:
        if block.get('dropped'):
            parts.append(f"{block.get('name')} 已丢")
            continue
        mark = '（超长已截断）' if block.get('truncated') else ''
        parts.append(f"{block.get('name')} {int(block.get('chars') or 0)}{mark}")
    if not parts:
        return '提示构成：无注入块'
    return ('提示构成：' + ' + '.join(parts) +
            f" = {int(stats.get('total') or 0)} 字（上限 {int(stats.get('limit') or 0)}）")
