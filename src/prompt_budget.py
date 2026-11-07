"""提示词预算：给注入的每一块单独封顶，再按总预算从低优先级开始丢。

算法参考「神鲸计划」的注入硬顶（UPDATE.md）：每块封顶（memory 1200 / diary 900 /
persona 800 / people 800 / stickers 700 / recent 1200）、整段 6000 字封顶，超了
先丢 stickers → people → recent，再丢 memory / diary，并在日志里打「提示构成」。

本项目注入块更少，所以只保留通用实现：compose(blocks, limit) 的 blocks 是
(名字, 文本, 单块上限, 优先级) 四元组，**优先级数字大的先被丢**（0 = 最后才丢），
所以调用方不必把丢弃顺序写死在预算模块里，也不会因为新增一块就漏掉排序。

两个入口：
- compose(blocks, limit)：旧入口，逐块封顶 + 按优先级整块丢，不保底。
- allocate(blocks, limit, minimums)：规划入口，人设/插件/记忆/日记一次全交进来，
  先保证每类的最小保留量，再按优先级丢、按比例缩，并把「历史还能用多少字」算在
  统计的 remaining 里 —— 一次规划取代以前「每块各算一次剩余额度」。
"""
from __future__ import annotations

import json

DEFAULT_LIMIT = 6000

# 每类块的最小保留量：预算再紧也不整块丢，先按比例缩。调用方可用 minimums 覆盖。
DEFAULT_MINIMUMS = {'人设': 1200, '插件': 400, '记忆': 300, '日记': 200}

_SENTENCE_ENDS = '。！？!?；;\n'
_MIN_KEEP_RATIO = 0.5      # 句末太靠前（不到一半）就不值得切，退回硬切


def clip(text, limit, kind=''):
    """按字符上限截断；优先切在句末，JSON 块整块保留或整块丢弃。

    limit=0 表示不封顶（沿用旧语义）。kind='json' 强制按 JSON 处理、'text' 强制
    按文本，默认按内容自动判断。返回空串 = 这块放不下，别注入半截。
    """
    text = str(text or '')
    limit = max(0, int(limit or 0))
    if not limit or len(text) <= limit:
        return text
    if kind == 'json' or (kind != 'text' and _looks_json(text)):
        return ''
    window = text[:limit]
    cut = max(window.rfind(ch) for ch in _SENTENCE_ENDS)
    return text[:cut + 1] if cut >= limit * _MIN_KEEP_RATIO else window


def _looks_json(text):
    """整块就是一个 JSON 对象/数组时返回 True；半截 JSON 解析不过，按文本处理。"""
    if text.lstrip()[:1] not in ('{', '['):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def compose(blocks, limit=DEFAULT_LIMIT):
    """旧入口：逐块封顶后按优先级整块丢，不保底（行为与改动前一致）。"""
    return allocate(blocks, limit, minimums={})


def allocate(blocks, limit=DEFAULT_LIMIT, minimums=None):
    """一次性预算规划：所有注入块一起交进来，统一按优先级 + 最小配额分配。

    blocks 仍是 (名字, 文本, 单块上限, 优先级)，可选第 5 位是该块保底字数。
    minimums 是 {名字: 保底字数}；None 用 DEFAULT_MINIMUMS，{} 表示不保底。
    统计里的 remaining 是留给历史消息的字数，调用方直接喂给 trim_history。
    """
    limit = max(0, int(limit if limit is not None else DEFAULT_LIMIT))
    quotas = DEFAULT_MINIMUMS if minimums is None else dict(minimums or {})
    kept = []
    for block in blocks:
        name, text, cap, priority = block[:4]
        quota = block[4] if len(block) > 4 else quotas.get(str(name), 0)
        raw = str(text or '').strip()
        if not raw:
            continue
        body = clip(raw, cap) if cap else raw
        kept.append({'name': str(name), 'text': body, 'chars': len(body),
                     'priority': int(priority),
                     'min': min(max(0, int(quota or 0)), len(body)),
                     'dropped': not body, 'truncated': len(body) < len(raw),
                     'reason': '' if body else '整块放不下'})
    total = sum(item['chars'] for item in kept)
    # 块之间用 \n 连接，这些分隔符也吃预算，否则 total 会刚好超出 limit 几个字。
    sep = max(0, len(kept) - 1)
    for item in sorted(kept, key=lambda x: x['priority'], reverse=True):
        if total + sep <= limit:
            break
        if item['dropped'] or item['min']:
            continue                      # 有保底的块留给按比例缩，不整块丢
        item['dropped'] = True
        item['reason'] = f"预算不足·优先级 {item['priority']}"
        total -= item['chars']
        sep = max(0, sum(1 for i in kept if not i['dropped']) - 1)
    if total + sep > limit:
        _shrink(kept, max(0, limit - sep), total)
    out = '\n'.join(item['text'] for item in kept if not item['dropped'])
    stats = {'total': len(out), 'limit': limit,
             'remaining': max(0, limit - len(out)),
             'blocks': [{k: item[k] for k in
                         ('name', 'chars', 'dropped', 'truncated', 'reason')}
                        for item in kept]}
    return out, stats


def _shrink(kept, limit, total):
    """仍超预算时按保底缩：先扣「超出保底」的部分，连保底都放不下就按保底比例缩。"""
    live = [item for item in kept if not item['dropped']]
    over = total - limit
    excess = sum(item['chars'] - item['min'] for item in live)
    if excess >= over:
        ratio = (excess - over) / excess
        for item in live:
            _fit(item, item['min'] + int((item['chars'] - item['min']) * ratio),
                 '超预算·按超出部分缩')
    else:
        room = max(0, limit - sum(i['chars'] for i in live if not i['min']))
        base = sum(item['min'] for item in live)
        ratio = room / base if base else 0
        for item in live:
            _fit(item, int(item['min'] * ratio), '总预算不足·按保底比例缩')


def _fit(item, allow, reason):
    """把一块压到 allow 字以内（仍优先切句末）；压到 0 就是整块丢。"""
    allow = max(0, min(int(allow), item['chars']))
    if allow >= item['chars']:
        return
    body = clip(item['text'], allow, 'text') if allow else ''
    if not body:
        item.update(text='', chars=0, dropped=True, reason=reason)
        return
    item.update(text=body, chars=len(body), truncated=True, reason=reason)


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
    """一行中文，给日志/事件用：谁被丢、谁被截、为什么。

    例：提示构成：记忆 1180 + 日记 已丢 = 1180 字（上限 6000）
        ｜丢：日记（预算不足·优先级 3）｜截：记忆 1180
    """
    stats = stats or {}
    parts, dropped, clipped = [], [], []
    for block in stats.get('blocks') or []:
        name = block.get('name')
        if block.get('dropped'):
            parts.append(f"{name} 已丢")
            dropped.append(f"{name}（{block.get('reason') or '预算不足'}）")
            continue
        chars = int(block.get('chars') or 0)
        mark = '（超长已截断）' if block.get('truncated') else ''
        parts.append(f"{name} {chars}{mark}")
        if block.get('truncated'):
            clipped.append(f"{name} {chars}")
    if not parts:
        return '提示构成：无注入块'
    line = ('提示构成：' + ' + '.join(parts) +
            f" = {int(stats.get('total') or 0)} 字（上限 {int(stats.get('limit') or 0)}）")
    if dropped:
        line += '｜丢：' + '、'.join(dropped)
    if clipped:
        line += '｜截：' + '、'.join(clipped)
    return line
