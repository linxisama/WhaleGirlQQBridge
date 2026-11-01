"""夜间固化（做梦）：把近几天日记与记忆里的同类痕迹按概率沉进 soul 层。

算法参考鱼缸桥接的 dream-consolidate.js，落在本项目的单库记忆上：

- 素材：日记.md 按天分段、同一天再按空行分段，超长段落再按句末标点切到 MAX_TEXT 以内；
  统一记忆近几天条目同样切分；排除 soul 与自画像，避免自反馈；
- 聚类：difflib 相似度或最长公共连续子串任一命中即同簇，簇内去重条数 = 这件事被提过几次；
- 每簇过一道 rate 概率门，一晚最多 max_clusters 簇；
- 账本 dream-seen.json 记住每晚看过的素材，避免同一句重复计数；
- 只新增/更新 layer='soul' 的 dream 条目，不碰 fact/lesson/project/topic。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULTS = {'enabled': False, 'lookback_days': 3, 'max_clusters': 3,
            'similarity': 0.3, 'lcs': 4, 'rate': 0.35}
LEDGER_MAX = 4000
MIN_TEXT = 4
MAX_TEXT = 300
CMP_MAX = 240
DIARY_NAME = '日记.md'
_DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
_DAY_HEAD = re.compile(r'^##\s+(\d{4}-\d{2}-\d{2})')
_SPACE = re.compile(r'\s+')
_BLANK = re.compile(r'\n[ \t]*\n')
_SENTENCE = re.compile(r'(?<=[。！？；…])')
_PUNCT = re.compile('[，。！？、；：,.!?;:~～…“”’‘\u0027\u0022（）()\\[\\]【】<>《》·]+')
_TRUE = {'1', 'true', 'yes', 'on', '是', '开', '开启', '打开'}
_FALSE = {'0', 'false', 'no', 'off', '否', '关', '关闭', '不'}


# ---------- 配置 ----------

def _fetch(config, name, default=None):
    """读 ai.<name>：兼容 None、嵌套 dict、扁平点号键和带 .get 的配置对象。"""
    if config is None:
        return default
    getter = getattr(config, 'get', None)
    if callable(getter):
        try:
            value = getter('ai.' + name)
        except Exception:
            value = None
        if value is not None:
            return value
    section = config if isinstance(config, dict) else getattr(config, 'config', None)
    if isinstance(section, dict):
        inner = section.get('ai')
        if isinstance(inner, dict) and inner.get(name) is not None:
            return inner[name]
    return default


def _flag(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return default


def _int_in(value, low, high, default):
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _rate_in(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(1.0, number))


def settings(config) -> dict:
    """读 ai.dream_* 配置；缺失或非法一律回落默认值，数值夹取到安全区间。"""
    return {
        'enabled': _flag(_fetch(config, 'dream_enabled'), DEFAULTS['enabled']),
        'lookback_days': _int_in(_fetch(config, 'dream_lookback_days'),
                                 1, 30, DEFAULTS['lookback_days']),
        'max_clusters': _int_in(_fetch(config, 'dream_max_clusters'),
                                1, 10, DEFAULTS['max_clusters']),
        'similarity': DEFAULTS['similarity'],
        'lcs': DEFAULTS['lcs'],
        'rate': _rate_in(_fetch(config, 'dream_rate'), DEFAULTS['rate']),
    }


def ledger_path(data_dir) -> Path:
    """账本固定在 qq-only 数据目录下，跟消息库放同一处。"""
    return Path(data_dir) / 'qq-only' / 'dream-seen.json'


# ---------- 文本工具 ----------

def _clean(text):
    return _SPACE.sub(' ', str(text or '')).strip()[:MAX_TEXT]


def _chunk(text, limit=MAX_TEXT):
    """超长文本按句末标点（。！？；…）切成不超过 limit 字的片段；单句本身超长就硬切。"""
    text = _SPACE.sub(' ', str(text or '')).strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    pieces, buffer = [], ''
    for sentence in _SENTENCE.split(text):
        while len(sentence) > limit:
            if buffer:
                pieces.append(buffer)
                buffer = ''
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if buffer and len(buffer) + len(sentence) > limit:
            pieces.append(buffer)
            buffer = ''
        buffer += sentence
    if buffer:
        pieces.append(buffer)
    return [piece.strip() for piece in pieces if piece.strip()]


def _paragraphs(text, limit=MAX_TEXT):
    """先按空行分段，再把每段按句末标点切到 limit 以内：一段是一条素材，不拼回一句。"""
    pieces = []
    for block in _BLANK.split(str(text or '')):
        pieces.extend(_chunk(block, limit))
    return pieces


def _norm(text):
    """去空白与标点并转小写：判重、相似度、LCS 都用这一份。"""
    return _PUNCT.sub('', _SPACE.sub('', str(text or ''))).lower()


def _ratio(left, right):
    a, b = _norm(left)[:CMP_MAX], _norm(right)[:CMP_MAX]
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _lcs_at_least(left, right, need):
    """是否有 >= need 字的连续公共片段；等价于两组 need-gram 有交集，O(n)。"""
    need = max(1, int(need))
    if len(left) < need or len(right) < need:
        return False
    grams = {left[i:i + need] for i in range(len(left) - need + 1)}
    return any(right[i:i + need] in grams for i in range(len(right) - need + 1))


def _lcs_len(left, right):
    """最长公共连续子串长度；只在测试/排查时用，正常判定走 _lcs_at_least。"""
    a, b = _norm(left)[:CMP_MAX], _norm(right)[:CMP_MAX]
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        char = a[i - 1]
        for j in range(1, len(b) + 1):
            if char == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _same_thing(left, right,
                similarity=DEFAULTS['similarity'], lcs=DEFAULTS['lcs']):
    """同一件事：相似度够，或有一段 >= lcs 字的连续相同片段。"""
    return _same_norm(_norm(left)[:CMP_MAX], _norm(right)[:CMP_MAX], similarity, lcs)


def _same_norm(left, right, similarity, lcs):
    if not left or not right:
        return False
    if difflib.SequenceMatcher(None, left, right).ratio() >= similarity:
        return True
    return _lcs_at_least(left, right, lcs)


def _day_of(stamp):
    return datetime.fromtimestamp(float(stamp)).strftime('%Y-%m-%d')


def _cutoff_day(now, days):
    return (datetime.fromtimestamp(float(now)) - timedelta(days=max(1, days) - 1)).strftime('%Y-%m-%d')


def _day_ord(day):
    try:
        return date.fromisoformat(str(day or '')[:10]).toordinal()
    except ValueError:
        return 0


# ---------- 素材 ----------

def _make_item(text, origin, day):
    text = _clean(text)
    if len(text) < MIN_TEXT:
        return None
    return {'text': text, 'from': origin, 'date': str(day or '')[:10]}


def _diary_blocks(data_dir):
    """读 <data_dir>/日记.md，按 ## YYYY-MM-DD 分天、天内按空行分段；读不到就返回空列表。"""
    try:
        raw = (Path(data_dir) / DIARY_NAME).read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return []
    blocks, day, lines = [], '', []

    def flush():
        if day and lines:
            for piece in _paragraphs('\n'.join(lines)):
                blocks.append((day, piece))
        lines.clear()

    for line in raw.splitlines():
        head = _DAY_HEAD.match(line.strip())
        if head:
            flush()
            day = head.group(1)
            continue
        if not day:
            continue
        text = line.strip()
        if text.startswith('#') or text.startswith('>'):
            continue
        lines.append(text)
    flush()
    blocks.sort(key=lambda block: block[0])
    return blocks


def _diary_rows(memory):
    """没有日记.md 时退回归一记忆里的日记行；memory 取不到就跳过。"""
    try:
        rows = memory.diaries(30) or []
    except Exception:
        return []
    blocks = []
    for row in rows:
        day = str(row.get('day') or '')[:10]
        if not _DAY.fullmatch(day):
            continue
        for piece in _paragraphs(row.get('content')):
            blocks.append((day, piece))
    blocks.sort(key=lambda block: block[0])
    return blocks


def collect(memory, data_dir, *, lookback_days=3, now=None) -> list[dict]:
    """收集近几天可固化的素材；每项 {'text','from','date'}，from 是 diary/memory。"""
    now = time.time() if now is None else float(now)
    days = _int_in(lookback_days, 1, 30, DEFAULTS['lookback_days'])
    cutoff = _cutoff_day(now, days)
    items, seen = [], set()
    for day, text in (_diary_blocks(data_dir) or _diary_rows(memory)):
        if day < cutoff:
            continue
        item = _make_item(text, 'diary', day)
        if item is not None and _norm(item['text']) not in seen:
            seen.add(_norm(item['text']))
            items.append(item)
    try:
        rows = memory.search(days=days, limit=30) or []
    except Exception:
        rows = []
    for row in rows:
        if str(row.get('layer') or '') == 'soul' or str(row.get('source') or '') == 'portrait':
            continue
        item = _make_item(row.get('content'), 'memory',
                          _day_of(row.get('updated') or row.get('created') or now))
        if item is not None and _norm(item['text']) not in seen:
            seen.add(_norm(item['text']))
            items.append(item)
    return items


# ---------- 聚类 ----------

def _representative(cluster):
    """簇里最长的那句当正文（信息量最大）；一样长取先出现的。"""
    return max(cluster, key=lambda member: len(str(member.get('text') or '')))


def _cluster(items, similarity=DEFAULTS['similarity'], lcs=DEFAULTS['lcs']):
    """并查集聚类；两两比较顺序固定，结果与跑多少次无关。"""
    size = len(items)
    parent = list(range(size))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    norms = [_norm(item.get('text'))[:CMP_MAX] for item in items]
    for i in range(size):
        for j in range(i + 1, size):
            if find(i) == find(j):
                continue
            if _same_norm(norms[i], norms[j], similarity, lcs):
                union(i, j)
    groups = {}
    for index in range(size):
        groups.setdefault(find(index), []).append(items[index])
    clusters = list(groups.values())
    clusters.sort(key=lambda group: (-len(group), -_newest(group),
                                     _representative(group)['text']))
    return clusters


def _newest(group):
    return max((_day_ord(member.get('date')) for member in group), default=0)


# ---------- 账本 ----------

def _key(item):
    """账本键：同一天同一句话只固化一次。"""
    raw = '%s|%s' % (item.get('date') or '', _norm(item.get('text')))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def _read_ledger(path):
    """键列表；文件不存在或损坏都从空开始，不抛。"""
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    keys = payload.get('keys') if isinstance(payload, dict) else None
    if not isinstance(keys, list):
        return []
    return [key for key in keys if isinstance(key, str) and key][-LEDGER_MAX:]


def _write_ledger(path, keys):
    """写回账本；失败只让这次去重失效，不影响已经写下的记忆。"""
    payload = {'version': 1, 'keys': list(keys)[-LEDGER_MAX:]}
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
        temp.replace(path)
    except OSError:
        pass


# ---------- 固化 ----------

def _find_dream(memory, text, similarity, lcs):
    """库里同主题的 soul 条目：source='dream' 或带 dream 标签的才算自己写过的。"""
    try:
        rows = memory.search(layer='soul', limit=30) or []
    except Exception:
        return None
    best, score = None, -1.0
    for row in rows:
        if str(row.get('layer') or '') != 'soul':
            continue
        marks = {str(tag) for tag in (row.get('tags') or [])}
        if str(row.get('source') or '') != 'dream' and 'dream' not in marks:
            continue
        content = str(row.get('content') or '')
        ratio = _ratio(content, text)
        if ratio > score and _same_thing(content, text, similarity, lcs):
            best, score = row, ratio
    return best


def _write_cluster(memory, cluster, representative, similarity, lcs):
    """一簇 → 一条 soul 记忆：命中同主题就更新，否则新建。"""
    text = str(representative.get('text') or '')
    mentions = len({_norm(member.get('text')) for member in cluster})
    importance = min(100, 40 + 15 * mentions)
    existing = _find_dream(memory, text, similarity, lcs)
    if existing is not None:
        older = str(existing.get('content') or '')
        longer = text if len(text) > len(older) else older
        before = int(existing.get('importance') or 0)
        saved = memory.update(existing['id'], content=longer,
                              importance=max(before, importance)) or existing
        return {'id': saved['id'], 'text': longer, 'mentions': mentions,
                'importance': max(before, importance), 'updated': True}
    saved = memory.remember(text, source='dream', layer='soul', importance=importance,
                            channel='', persona='', tags=['dream'])
    entry = {'id': saved['id'], 'text': text, 'mentions': mentions,
             'importance': importance}
    if saved.get('merged'):
        entry['updated'] = True
    else:
        entry['created'] = True
    return entry


def _result(ok, reason, *, clusters=0, written=0, items=None, detail=''):
    out = {'ok': bool(ok), 'reason': reason, 'clusters': int(clusters),
           'written': int(written), 'items': list(items or [])}
    if detail:
        out['detail'] = str(detail)
    return out


def consolidate(memory, data_dir, *, config=None, now=None, force=False, rng=None) -> dict:
    """跑一次夜间固化；任何异常都收成 reason='error'，不冒泡。"""
    try:
        conf = settings(config)
        if not conf['enabled'] and not force:
            return _result(False, 'disabled')
        now = time.time() if now is None else float(now)
        items = collect(memory, data_dir, lookback_days=conf['lookback_days'], now=now)
        if not items:
            return _result(False, 'no_items')
        path = ledger_path(data_dir)
        known = _read_ledger(path)
        seen = set(known)
        fresh = [item for item in items if _key(item) not in seen]
        if not fresh:
            return _result(False, 'no_items')
        clusters = _cluster(fresh, conf['similarity'], conf['lcs'])
        roll = rng.random if rng is not None else random.random
        passed = [cluster for cluster in clusters if force or roll() < conf['rate']]
        if not passed:
            return _result(False, 'rate_skipped', clusters=len(clusters))
        entries, keys = [], []
        for cluster in passed[:conf['max_clusters']]:
            representative = _representative(cluster)
            entries.append(_write_cluster(memory, cluster, representative,
                                          conf['similarity'], conf['lcs']))
            keys.append(_key(representative))
            _write_ledger(path, known + keys)
        return _result(True, 'ok', clusters=len(clusters),
                       written=len(entries), items=entries)
    except Exception as error:
        return _result(False, 'error', detail='%s: %s' % (type(error).__name__, error))
