"""统一记忆区：QQ、桌面 Agent、DSH 共用一个可遗忘的分层记忆库。

设计参考鱼缸项目的“鱼脑”记忆插件，但保留本项目的边界：

- 七层：soul（我是谁）/ user（主人是谁）/ project（项目）/ fact（事实）/
  lesson（教训）/ topic（当前话题）/ rules（相处原则）；
- 首轮注入稳定层，之后按关键词取少量相关记忆，而不是每轮塞入整库；
- 群聊不注入 soul/user，避免把主人隐私带进群里；项目记忆按会话锚定；
- 写入自动去重合并，保留置信度、证据、项目、关键词和更新时间；
- 仍有重要度、使用次数、TTL 与维护遗忘，不存图片、密钥、Cookie 或完整聊天记录。
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from pathlib import Path

MAX_ITEMS = 3000
MAX_CONTENT = 2000
SOURCES = {'qq', 'desktop', 'dsh', 'web', 'chat'}
CHANNELS = ('qq', 'console', 'local')
DEFAULT_CHANNEL = 'console'
LAYERS = ('soul', 'user', 'project', 'fact', 'lesson', 'topic', 'rules')
LAYER_LABELS = {
    'soul': '关于我', 'user': '关于你', 'project': '项目', 'fact': '事实',
    'lesson': '教训', 'topic': '话题', 'rules': '相处原则',
}
STATUSES = {'active', 'archived', 'superseded'}
_COLUMNS = ('id,source,content,importance,created,last_used,uses,expires_at,tags,persona,'
            'layer,keywords,project,status,confidence,updated,evidence,goal,channel')
_PLACEHOLDERS = ','.join('?' * len(_COLUMNS.split(',')))
_SPACE = re.compile(r'\s+')
_PUNCT = re.compile(r'[，。！？、；：,.!?;:~～…“”"\'（）()\[\]【】<>《》]+')
_STOP = set('的了是在和与就都而及或一个这那有没我你他她它们要会可以很非常还又把被让给对从到为以于')

# 注入分节预算（字）：稳定层（soul/user/rules）每轮都在、最值得留，相关条目按本轮
# 内容取、最容易把前面挤掉，所以先各自封顶再整体裁 —— 顺序裁会让"最后一条"吃掉全部额度。
SECTION_BUDGET = {'stable': 600, 'project': 300, 'relevant': 600}
# 记忆权重半衰期：140 天没动权重减半（memory_guard.decay 与这里的召回打分共用）
HALF_LIFE_DAYS = 140.0


def _now():
    return time.time()


def _clean(text, limit=MAX_CONTENT):
    return _SPACE.sub(' ', str(text or '')).strip()[:limit]


def _as_list(value, limit=24):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out = []
    for item in value:
        text = _clean(item, 40)
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _clip_lines(lines, budget):
    """按字符预算取前面的行：超预算就停，把额度留给后面的分节。"""
    out, used = [], 0
    budget = max(0, int(budget or 0))
    for line in lines:
        if budget and used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
    return out


def _keywords(text, limit=24):
    """轻量关键词：中文二元组 + 拉丁词，不依赖分词库。

    limit 默认 24 是**存储**用的上限（长记忆只存前 24 个二元组，控制库体积）。
    检索时正文扫描必须传 limit=0 拿全量，否则长记忆尾部的词永远匹配不到 ——
    那正是「明明记得却检索不到」的根因。
    """
    value = str(text or '').lower()
    value = _PUNCT.sub(' ', value)
    words = re.findall(r'[a-z][a-z0-9_+.-]{1,24}', value)
    chinese = re.sub(r'[^\u4e00-\u9fff]+', ' ', value)
    for block in chinese.split():
        if len(block) >= 2:
            words.extend(block[i:i + 2] for i in range(len(block) - 1))
    out = []
    for word in words:
        word = word.strip()
        if len(word) < 2 or word in _STOP or word in out:
            continue
        out.append(word)
    return out[:limit] if limit else out


def _similarity(left, right):
    a, b = _clean(left, 800).lower(), _clean(right, 800).lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = {a[i:i + 2] for i in range(max(1, len(a) - 1))}
    gb = {b[i:i + 2] for i in range(max(1, len(b) - 1))}
    return len(ga & gb) / math.sqrt(len(ga) * len(gb)) if ga and gb else 0.0


def _channel_arg(value, default=''):
    """通道参数校验：去空格转小写；空表示未指定（交给 default），未知通道一律报错。"""
    text = str(value or '').strip().lower()
    if not text:
        return default
    if text not in CHANNELS:
        raise ValueError('记忆通道必须是 ' + '/'.join(CHANNELS))
    return text


def _norm_channel(value):
    """写入通道归一：空表示未指定（= console）；未知通道直接报错，不静默落到默认值。"""
    return _channel_arg(value, DEFAULT_CHANNEL)


def _scope_text(value):
    return _clean(value, 80)


def _scope_conflict(item, channel, persona):
    """旧式软过滤：只在双方都有值且不同时判冲突，未标人设/通道的老行仍然放行。"""
    if persona and item.get('persona') and item.get('persona') != persona:
        return True
    return bool(channel and item.get('channel') and item.get('channel') != channel)


class UnifiedMemory:
    def __init__(self, db, diary_path=None, diary_dir=None):
        self.db = db
        self.diary_path = Path(diary_path) if diary_path else None
        # 旧版把所有日记汇总成一个 Markdown；新版每天一个 txt，二者同时写。
        self.diary_dir = Path(diary_dir) if diary_dir else None
        # 上一次 context_for_prompt 的统计：chars/items/line_chars/max_chars/dropped。
        # 提示词里到底塞了多少、丢了多少，从这里读，不用猜。
        self.last_context_stats = {'chars': 0, 'items': 0, 'line_chars': 0,
                                   'max_chars': 0, 'dropped': 0}
        self._ensure()

    # ---------- schema / migration ----------

    def _ensure(self):
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('''CREATE TABLE IF NOT EXISTS unified_memories(
            id TEXT PRIMARY KEY, source TEXT, content TEXT, importance INTEGER,
            created REAL, last_used REAL, uses INTEGER, expires_at REAL,
            tags TEXT, persona TEXT)''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(unified_memories)')}
        additions = {
            'layer': "TEXT NOT NULL DEFAULT 'fact'",
            'keywords': "TEXT NOT NULL DEFAULT '[]'",
            'project': "TEXT NOT NULL DEFAULT ''",
            'status': "TEXT NOT NULL DEFAULT 'active'",
            'confidence': 'INTEGER NOT NULL DEFAULT 60',
            'updated': 'REAL NOT NULL DEFAULT 0',
            'evidence': "TEXT NOT NULL DEFAULT ''",
            'goal': "TEXT NOT NULL DEFAULT ''",
            'channel': "TEXT NOT NULL DEFAULT 'console'",
            'persona': "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in additions.items():
            if name in columns:
                continue
            try:
                self.db.execute(f'ALTER TABLE unified_memories ADD COLUMN {name} {ddl}')
            except Exception as error:
                # 另一个连接刚好抢先补了同一列时放过这个良性竞态，其它失败一律上报
                if 'duplicate column' not in str(error).lower():
                    raise RuntimeError(
                        f'统一记忆库 scope 迁移失败（ADD COLUMN {name}）：{error}') from error
                columns = {row[1] for row in
                           self.db.execute('PRAGMA table_info(unified_memories)')}
                if name not in columns:
                    raise RuntimeError(
                        f'统一记忆库 scope 迁移失败（ADD COLUMN {name}）：{error}') from error
        self.db.execute("UPDATE unified_memories SET updated=created WHERE updated IS NULL OR updated=0")
        self.db.execute("UPDATE unified_memories SET layer='fact' WHERE layer IS NULL OR layer=''")
        self.db.execute("UPDATE unified_memories SET keywords='[]' WHERE keywords IS NULL OR keywords=''")
        self.db.execute("UPDATE unified_memories SET confidence=importance WHERE confidence IS NULL")
        self.db.execute("UPDATE unified_memories SET channel=? WHERE channel IS NULL OR channel=''",
                        (DEFAULT_CHANNEL,))
        self.db.execute("UPDATE unified_memories SET persona='' WHERE persona IS NULL")
        # W4c 幂等回填：QQ 链路的历史行 source='qq' 但 channel 兜底成了 console；不纠正的话，
        # channel='qq' 的召回会把用户旧记忆当"跨通道"丢掉。WHERE 同时限定 source='qq' 与
        # channel='console'，第二次打开起 0 行受影响，也不会改写后来显式写入的通道。
        self.db.execute("UPDATE unified_memories SET channel='qq' "
                        "WHERE source='qq' AND channel='console'")
        self.db.execute('''CREATE TABLE IF NOT EXISTS diary_entries(
            id TEXT PRIMARY KEY, day TEXT UNIQUE, content TEXT, created REAL, meta TEXT)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS unified_layer ON unified_memories(layer,status,project)')
        self.db.execute('CREATE INDEX IF NOT EXISTS unified_updated ON unified_memories(updated)')
        self.db.execute('CREATE INDEX IF NOT EXISTS unified_scope ON unified_memories(channel,persona)')
        self.db.commit()
        self._sync_legacy()

    def _sync_legacy(self):
        """把旧 self_memories/chat_memories 的已确认内容镜像进统一区（幂等，不改原表）。"""
        for table, source, importance, layer, query in (
            ('self_memories', 'qq', 55, 'soul',
             "SELECT content,persona FROM self_memories WHERE enabled=1"),
            ('chat_memories', 'qq', 45, 'fact',
             "SELECT content,'' FROM chat_memories WHERE status='confirmed'"),
        ):
            try:
                rows = self.db.execute(query).fetchall()
            except Exception:
                continue
            for row in rows:
                text = _clean(row[0])
                persona = _scope_text(row[1]) if len(row) > 1 else ''
                if not text:
                    continue
                # 源表本身就是 QQ 数据：这里显式给 channel='qq'，不走"未指定即 console"的默认值
                exists = self.db.execute(
                    'SELECT 1 FROM unified_memories WHERE content=? AND source=? AND persona=? LIMIT 1',
                    (text, source, persona)).fetchone()
                if exists:
                    continue
                now = _now()
                self.db.execute(
                    'INSERT INTO unified_memories(' + _COLUMNS + ') VALUES(' + _PLACEHOLDERS + ')',
                    (uuid.uuid4().hex, source, text, importance, now, now, 0, 0,
                     '[]', persona, layer,
                     json.dumps(_keywords(text), ensure_ascii=False),
                     '', 'active', importance, now, '', '', 'qq'))
        try:
            self.db.commit()
        except Exception:
            pass

    # ---------- serialization ----------

    def _get_row(self, identifier):
        return self.db.execute(
            'SELECT ' + _COLUMNS + ' FROM unified_memories WHERE id=?', (identifier,)).fetchone()

    @staticmethod
    def _row(row):
        return {
            'id': row[0], 'source': row[1], 'content': row[2], 'importance': row[3],
            'created': row[4], 'last_used': row[5], 'uses': row[6],
            'expires_at': row[7], 'tags': json.loads(row[8] or '[]'), 'persona': row[9],
            'layer': row[10] or 'fact', 'keywords': json.loads(row[11] or '[]'),
            'project': row[12] or '', 'status': row[13] or 'active',
            'confidence': int(row[14] if row[14] is not None else row[3] or 0),
            'updated': row[15] or row[4], 'evidence': row[16] or '', 'goal': row[17] or '',
            'channel': row[18] or DEFAULT_CHANNEL,
        }

    def get(self, identifier):
        row = self._get_row(identifier)
        return self._row(row) if row else None

    # ---------- write / dedupe ----------

    def _similar_row(self, layer, project, text, channel=DEFAULT_CHANNEL, persona=''):
        """只在同 scope（同 channel + 同 persona）内找相似行，避免人设/通道串味。"""
        rows = self.db.execute(
            'SELECT ' + _COLUMNS + ' FROM unified_memories '
            'WHERE layer=? AND project=? AND channel=? AND persona=? AND status="active" '
            'ORDER BY updated DESC LIMIT 400',
            (layer, project, channel, persona)).fetchall()
        for row in rows:
            item = self._row(row)
            if _similarity(item['content'], text) >= .92:
                return item
        return None

    def _merge(self, old, text, importance, confidence, status, tags, keywords,
               evidence, goal, now):
        longer = text if len(text) > len(old['content']) else old['content']
        tags = list(dict.fromkeys(list(old.get('tags') or []) + list(tags)))[:20]
        keywords = list(dict.fromkeys(list(old.get('keywords') or []) + list(keywords)))[:40]
        evidence = '；'.join([x for x in (old.get('evidence'), evidence) if x])[:1000]
        self.db.execute(
            'UPDATE unified_memories SET content=?,importance=?,last_used=?,tags=?,'
            'keywords=?,status=?,confidence=?,updated=?,evidence=?,goal=? WHERE id=?',
            (longer, max(int(old.get('importance') or 0), importance), now,
             json.dumps(tags, ensure_ascii=False), json.dumps(keywords, ensure_ascii=False),
             status, max(int(old.get('confidence') or 0), confidence), now, evidence,
             goal or old.get('goal') or '', old['id']))
        self.db.commit()
        item = self.get(old['id'])
        item['merged'] = True
        return item

    def remember(self, content, source='desktop', importance=50, ttl_days=0,
                 tags=None, persona='', layer='fact', keywords=None, project='',
                 confidence=None, status='active', evidence='', goal='', channel=''):
        text = _clean(content)
        if not text:
            raise ValueError('记忆内容不能为空')
        channel = _norm_channel(channel)
        persona = _scope_text(persona)
        source = source if source in SOURCES else 'desktop'
        layer = str(layer or 'fact').lower()
        if layer not in LAYERS:
            raise ValueError('记忆层必须是 ' + '/'.join(LAYERS))
        status = str(status or 'active').lower()
        if status not in STATUSES:
            raise ValueError('记忆状态不合法')
        importance = max(0, min(100, int(importance)))
        confidence = importance if confidence is None else max(0, min(100, int(confidence)))
        project = _clean(project, 80)
        tags = _as_list(tags, 20)
        keywords = _as_list(keywords, 40) or _keywords(text)
        now = _now()
        old = self._similar_row(layer, project, text, channel, persona)
        if old is not None:
            return self._merge(old, text, importance, confidence, status, tags,
                               keywords, evidence, goal, now)
        expires = now + max(0, int(ttl_days)) * 86400 if ttl_days else 0
        identifier = uuid.uuid4().hex
        self.db.execute(
            'INSERT INTO unified_memories(' + _COLUMNS + ') VALUES(' + _PLACEHOLDERS + ')',
            (identifier, source, text, importance, now, now, 0, expires,
             json.dumps(tags, ensure_ascii=False), persona, layer,
             json.dumps(keywords, ensure_ascii=False), project, status, confidence,
             now, _clean(evidence, 1000), _clean(goal, 300), channel))
        self.db.commit()
        self._trim()
        return self.get(identifier)

    # ---------- search ----------

    def search(self, query='', limit=8, source='', layer='', project='',
               days=0, include_archived=False, channel='', persona='',
               strict_scope=False, track=True):
        """scope 只影响排序加权，默认不把跨 scope 的行过滤掉。

        channel/persona 给定时：同 channel +1 分、同 persona +1 分（都同 = 2 分），
        该分值作为第一排序键（排在 score/updated 之前），原有 score 公式不变。
        strict_scope=True 才会在 SQL 里硬过滤（调用方明确只要某个通道/人设时用）。

        channel：'' 表示不按通道加权/过滤；非空必须是 CHANNELS 之一，否则 raise ValueError
        （与 remember 对齐，早失败好排查；不做"悄悄当 console"的降级）。
        """
        limit = max(1, min(30, int(limit or 8)))
        now = _now()
        channel_want = _channel_arg(channel)
        persona_want = _scope_text(persona)
        where = ['(expires_at=0 OR expires_at>?)']
        params = [now]
        if source:
            where.append('source=?')
            params.append(source)
        if layer:
            where.append('layer=?')
            params.append(layer)
        if project:
            where.append("(project='' OR project=?)")
            params.append(project)
        if days:
            where.append('updated>=?')
            params.append(now - max(1, int(days)) * 86400)
        if not include_archived:
            where.append("status='active'")
        if strict_scope and channel_want:
            where.append('channel=?')
            params.append(channel_want)
        if strict_scope and persona_want:
            where.append('persona=?')
            params.append(persona_want)
        rows = self.db.execute(
            'SELECT ' + _COLUMNS + ' FROM unified_memories WHERE ' + ' AND '.join(where) +
            ' ORDER BY updated DESC LIMIT 2000', params).fetchall()
        items = [self._row(row) for row in rows]
        q = str(query or '').strip()[:200]
        q_norm = _clean(q, 200).lower()
        q_terms = set(_keywords(q))
        chosen = []
        for item in items:
            content = str(item.get('content') or '').lower()
            if q_norm:
                # 关键词只存前 24 个二元组，长记忆尾部的词查不到，所以正文也要扫：
                # 关键词命中算「精确」，正文二元组命中算「相关」，两者取高。
                overlap = q_terms & set(item.get('keywords') or [])
                body_hit = q_norm in content
                body_terms = q_terms & set(_keywords(content, 0))
                if not body_hit and not overlap and not body_terms:
                    continue
                if body_hit:
                    relevance = 1.0
                else:
                    hit = overlap or body_terms
                    relevance = round(min(1.0, len(hit) / max(1, min(len(q_terms), 4))), 4)
                # 正文整串命中给满权重，只有部分词相关时按 relevance 线性给分。
                text_score = .70 * relevance
            else:
                relevance = 0.0
                text_score = 0
            age_days = max(0, (now - float(item.get('updated') or item.get('created') or now)) / 86400)
            # 半衰期衰减：180 天线性归零改成 140 天减半，旧记忆不会突然彻底失权
            recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
            importance = item.get('importance', 0)
            item['relevance'] = relevance
            item['score'] = round(
                text_score + importance / 100 * .15 +
                item.get('confidence', 0) / 100 * .15 + recency * .10 +
                min(1, int(item.get('uses') or 0) / 10) * .05, 4)
            # why 是给人看的：不看公式也能知道这条为什么排在这个位置。
            why = ['正文命中' if q_norm and q_norm in content else
                   ('相关%.2f' % relevance if relevance else '无正文命中'),
                   '重要%d' % int(importance or 0),
                   '新鲜%.2f' % recency]
            if item.get('uses'):
                why.append('用过%d次' % int(item['uses']))
            item['why'] = ' / '.join(why)
            item['scope_score'] = self._scope_score(item, channel_want, persona_want)
            item['scope_mark'] = self._scope_mark(item, channel_want, persona_want)
            chosen.append(item)
        chosen.sort(key=lambda x: (x.get('scope_score') or 0, x['score'],
                                   x.get('updated') or 0), reverse=True)
        chosen = chosen[:limit]
        if track:
            self.touch([item['id'] for item in chosen])
        return chosen

    def touch(self, identifiers):
        """只给真正注入提示的条目累加命中次数，避免被隐私/scope 过滤的条目虚增。"""
        ids = [item for item in dict.fromkeys(identifiers or []) if item]
        if not ids:
            return 0
        now = _now()
        for identifier in ids:
            self.db.execute(
                'UPDATE unified_memories SET last_used=?,uses=uses+1 WHERE id=?',
                (now, identifier))
        self.db.commit()
        return len(ids)

    @staticmethod
    def _scope_score(item, channel, persona):
        """同 scope 加权：同 channel +1、同 persona +1，作为排序键参与，不改 score 语义。"""
        score = 0
        if channel and (item.get('channel') or DEFAULT_CHANNEL) == channel:
            score += 1
        if persona and (item.get('persona') or '') == persona:
            score += 1
        return score

    @staticmethod
    def _scope_mark(item, channel, persona):
        """提示文本里的 scope 标记；未指定 scope 时返回空串，保持旧输出原样。"""
        if not channel and not persona:
            return ''
        parts = []
        if channel:
            parts.append('同通道' if (item.get('channel') or DEFAULT_CHANNEL) == channel
                         else '跨通道')
        if persona:
            own = item.get('persona') or ''
            parts.append('同人设' if own == persona else ('跨人设' if own else '无人设'))
        return '【' + '/'.join(parts) + '】'

    # ---------- prompt context ----------

    def context_for_prompt(self, query='', project='', private=False,
                           persona='', first_turn=False, max_chars=1600,
                           channel='', allow_cross_scope=False):
        """生成给模型的短记忆块；群聊不读 soul/user，避免泄漏主人信息。

        channel/persona 用于召回加权并给每行打上同/跨 scope 标记；默认仍丢掉明确
        属于别的人设/通道的行（提示注入不主动串味），allow_cross_scope=True 才放行。
        channel：'' 表示不按通道过滤/加权；非空必须是 CHANNELS 之一，否则 raise ValueError
        （与 remember、search 一致）。
        """
        channel_want = _channel_arg(channel)
        persona_want = _scope_text(persona)
        scope = {'channel': channel_want, 'persona': persona_want}
        stable, project_lines, relevant = [], [], []
        line_ids = {}
        if first_turn:
            for layer in (('soul', 'user', 'rules') if private else ('rules',)):
                for item in self.search('', limit=2, layer=layer, project=project,
                                       track=False, **scope):
                    if not allow_cross_scope and _scope_conflict(item, channel_want, persona_want):
                        continue
                    mark = item.get('scope_mark') or ''
                    line = f"{mark}{LAYER_LABELS.get(layer, layer)}：{item['content'][:240]}"
                    stable.append(line)
                    line_ids.setdefault(line, item['id'])
            for item in self.search('', limit=2, layer='project', project=project,
                                   track=False, **scope):
                if not allow_cross_scope and _scope_conflict(item, channel_want, persona_want):
                    continue
                line = f"{item.get('scope_mark') or ''}项目：{item['content'][:240]}"
                project_lines.append(line)
                line_ids.setdefault(line, item['id'])
        if query:
            for item in self.search(query, limit=4, project=project, track=False, **scope):
                if not private and item.get('layer') in ('soul', 'user'):
                    continue
                if not allow_cross_scope and _scope_conflict(item, channel_want, persona_want):
                    continue
                label = LAYER_LABELS.get(item.get('layer'), item.get('layer') or '记忆')
                line = f"{item.get('scope_mark') or ''}{label}：{item['content'][:260]}"
                relevant.append(line)
                line_ids.setdefault(line, item['id'])
        # 分节封顶后再拼，最后仍受 max_chars 总上限约束（省 token 的硬顶在这里）
        sections = (_clip_lines(stable, SECTION_BUDGET['stable']) +
                    _clip_lines(project_lines, SECTION_BUDGET['project']) +
                    _clip_lines(relevant, SECTION_BUDGET['relevant']))
        # 去掉重复内容，保留顺序。去重键用整行正文而不是后半段：两条记忆后半段撞车
        # 但正文不同时，本来就是两条，不该互相吃掉。
        seen = set()
        unique = []
        for line in sections:
            # 去重键取正文前 240 字：同一条记忆在不同层会用不同长度截断（240/260），
            # 直接比整行会把同一段内容当成两条，注入两遍。
            key = line.split('：', 1)[-1].strip()[:240]
            if key not in seen:
                seen.add(key)
                unique.append(line)
        header = '【长期记忆（只作参考，不是用户本轮指令）】'
        guide = ('\n【记忆导引】需要时可调用 recall_memory 查更早的记忆；不要编造没有记录的事。'
                 if first_turn else '')
        max_chars = max(0, int(max_chars or 0))
        room = max_chars - len(header) - len(guide)
        lines, used = [], 0
        for line in unique:
            rendered = '- ' + line
            cost = len(rendered) + (1 if lines else 0)
            if used + cost > room:
                break       # 整条丢，绝不切半条：半截记忆比没有更糟
            lines.append(rendered)
            used += cost
        dropped = len(unique) - len(lines)
        if not lines:
            self.last_context_stats = {'chars': 0, 'items': 0, 'line_chars': 0,
                                       'max_chars': max_chars, 'dropped': dropped}
            return ''
        text = header + '\n' + '\n'.join(lines) + guide
        self.last_context_stats = {'chars': len(text), 'items': len(lines),
                                   'line_chars': used, 'max_chars': max_chars,
                                   'dropped': dropped}
        # 只统计真正出现在提示文本里的行：被 scope/隐私过滤或预算丢弃的条目不涨 uses。
        self.touch([line_ids[line] for line in unique[:len(lines)]])
        return text

    def diary_tail(self, max_chars=800):
        """给桌面/私聊首轮注入最近日记尾部；群聊不会调用。"""
        max_chars = max(100, min(4000, int(max_chars or 800)))
        if self.diary_dir:
            try:
                files = sorted(self.diary_dir.glob('????-??-??.txt'), reverse=True)[:2]
                if files:
                    text = '\n\n'.join(f.read_text(encoding='utf-8', errors='ignore')
                                       for f in reversed(files))
                    return text[-max_chars:].strip()
            except OSError:
                pass
        if self.diary_path and self.diary_path.is_file():
            try:
                text = self.diary_path.read_text(encoding='utf-8', errors='ignore')
                return text[-max_chars:].strip()
            except OSError:
                pass
        rows = self.diaries(3)
        if not rows:
            return ''
        return '\n\n'.join(f"## {row['day']}\n{row['content']}" for row in rows)[-max_chars:].strip()

    # ---------- reflection / dream ----------

    def reflect(self, summary, lesson='', project='', source='chat', evidence='',
                channel='', persona=''):
        """记一条教训（layer='lesson'）；channel/persona 原样透传给 remember。

        channel/persona 默认空 = 保持旧行为（remember 自身把空 channel 归一成 'console'、
        persona 归一成空串），由调用方按自己所在的通道/人设显式传入。
        """
        summary = _clean(summary, 800)
        if not summary:
            raise ValueError('反思内容不能为空')
        item = self.remember(summary, source=source, importance=65, confidence=70,
                             layer='lesson', project=project, tags=['reflect'],
                             evidence=evidence, channel=channel, persona=persona)
        if lesson:
            self.remember(lesson, source=source, importance=60, confidence=65,
                          layer='lesson', project=project, tags=['reflect'],
                          evidence=evidence, channel=channel, persona=persona)
        return item

    def dream(self, now=None):
        """低风险整理：合并高度重复的事实/教训，归档过时话题。"""
        now = _now() if now is None else float(now)
        merged = 0
        rows = self.db.execute(
            'SELECT ' + _COLUMNS + ' FROM unified_memories '
            "WHERE status='active' AND layer IN ('fact','lesson','project') "
            'ORDER BY updated DESC LIMIT 800').fetchall()
        by_group = {}
        for row in rows:
            item = self._row(row)
            by_group.setdefault((item['layer'], item['project'],
                                 item.get('channel') or DEFAULT_CHANNEL,
                                 item.get('persona') or ''), []).append(item)
        for group in by_group.values():
            for i in range(len(group)):
                if not group[i]:
                    continue
                for j in range(i + 1, len(group)):
                    if not group[j]:
                        continue
                    if _similarity(group[i]['content'], group[j]['content']) >= .94:
                        self._merge(group[i], group[j]['content'], group[j]['importance'],
                                    group[j]['confidence'], 'active', group[j]['tags'],
                                    group[j]['keywords'], group[j]['evidence'],
                                    group[j]['goal'], now)
                        self.db.execute('DELETE FROM unified_memories WHERE id=?', (group[j]['id'],))
                        group[j] = None
                        merged += 1
        archived = self.db.execute(
            "UPDATE unified_memories SET status='archived' WHERE status='active' "
            "AND layer='topic' AND updated<?", (now - 7 * 86400,)).rowcount
        self.db.commit()
        return {'merged': max(0, merged), 'archived_topics': max(0, archived),
                'remaining': self.count()}

    # ---------- maintenance ----------

    def update(self, identifier, content=None, layer=None, keywords=None,
               project=None, status=None, confidence=None, importance=None,
               evidence=None, goal=None):
        """纠正一条记忆；不改变来源、创建时间或过期时间。"""
        old = self.get(identifier)
        if old is None:
            raise ValueError('记忆不存在')
        text = _clean(content, MAX_CONTENT) if content is not None else old['content']
        if not text:
            raise ValueError('记忆内容不能为空')
        new_layer = str(layer if layer is not None else old['layer']).lower()
        if new_layer not in LAYERS:
            raise ValueError('记忆层不合法')
        new_status = str(status if status is not None else old['status']).lower()
        if new_status not in STATUSES:
            raise ValueError('记忆状态不合法')
        new_keywords = (_as_list(keywords, 40) if keywords is not None
                        else old.get('keywords') or []) or _keywords(text)
        new_project = (_clean(project, 80) if project is not None else old.get('project') or '')
        new_confidence = old.get('confidence', 60) if confidence is None else confidence
        new_importance = old.get('importance', 50) if importance is None else importance
        self.db.execute(
            'UPDATE unified_memories SET content=?,layer=?,keywords=?,project=?,status=?,'
            'confidence=?,importance=?,evidence=?,goal=?,updated=? WHERE id=?',
            (text, new_layer, json.dumps(new_keywords, ensure_ascii=False), new_project,
             new_status, max(0, min(100, int(new_confidence))),
             max(0, min(100, int(new_importance))),
             _clean(evidence, 1000) if evidence is not None else old.get('evidence') or '',
             _clean(goal, 300) if goal is not None else old.get('goal') or '',
             _now(), identifier))
        self.db.commit()
        return self.get(identifier)

    def forget(self, identifier):
        self.db.execute('DELETE FROM unified_memories WHERE id=?', (identifier,))
        self.db.commit()
        return True

    def maintain(self, now=None):
        now = _now() if now is None else float(now)
        dream = self.dream(now)
        expired = self.db.execute('DELETE FROM unified_memories WHERE expires_at>0 AND expires_at<=?',
                                  (now,)).rowcount
        stale = self.db.execute(
            'DELETE FROM unified_memories WHERE importance<25 AND uses=0 AND last_used<?',
            (now - 30 * 86400,)).rowcount
        self.db.commit()
        self._trim()
        return {'expired': max(0, expired), 'stale': max(0, stale),
                'remaining': self.count(), **dream}

    def _trim(self):
        total = self.count()
        if total <= MAX_ITEMS:
            return
        rows = self.db.execute(
            'SELECT id FROM unified_memories ORDER BY importance ASC,last_used ASC LIMIT ?',
            (total - MAX_ITEMS,)).fetchall()
        for (identifier,) in rows:
            self.db.execute('DELETE FROM unified_memories WHERE id=?', (identifier,))
        self.db.commit()

    def count(self):
        return int(self.db.execute('SELECT count(*) FROM unified_memories').fetchone()[0] or 0)

    def layers(self):
        rows = self.db.execute(
            'SELECT layer,count(*) FROM unified_memories WHERE status="active" GROUP BY layer').fetchall()
        counts = {layer: 0 for layer in LAYERS}
        for layer, count in rows:
            if layer in counts:
                counts[layer] = int(count or 0)
        return counts

    # ---------- diary ----------

    def _daily_file(self, day):
        if not self.diary_dir:
            return None
        return self.diary_dir / f'{str(day)[:10]}.txt'

    def _write_daily(self, day, text):
        """每天一个 UTF-8 txt；先写临时文件再替换，避免半个文件。"""
        target = self._daily_file(day)
        if target is None:
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.txt.tmp')
            temp.write_text(str(text).rstrip() + '\n', encoding='utf-8')
            temp.replace(target)
        except OSError:
            return None
        return target

    def export_daily_files(self, limit=365):
        """把库里已有的日记补写成每日 txt（升级后第一次启动用）。"""
        if not self.diary_dir:
            return []
        rows = self.db.execute('SELECT day,content FROM diary_entries ORDER BY day LIMIT ?',
                               (max(1, int(limit)),)).fetchall()
        written = []
        for day, content in rows:
            target = self._write_daily(day, content)
            if target is not None:
                written.append(str(target))
        return written

    def _export_diary(self):
        """把数据库里的日记同步成用户可直接打开的中文 Markdown。"""
        if not self.diary_path:
            return
        try:
            rows = self.db.execute('SELECT day,content FROM diary_entries ORDER BY day DESC').fetchall()
            text = '# 鲸鱼娘日记\n\n> 自动整理，中文可读；数据库仍是唯一事实源。\n\n'
            for day, content in rows:
                text += f'## {day}\n\n{content}\n\n'
            self.diary_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.diary_path.with_suffix('.md.tmp')
            temp.write_text(text, encoding='utf-8')
            temp.replace(self.diary_path)
        except OSError:
            pass

    def write_diary(self, day, content, meta=None):
        day = str(day or '')[:10]
        text = str(content or '').strip()[:12000]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not text:
            raise ValueError('日记需要日期 YYYY-MM-DD 和内容')
        identifier = uuid.uuid4().hex
        self.db.execute('INSERT OR REPLACE INTO diary_entries VALUES(?,?,?,?,?)',
                        (identifier, day, text, _now(), json.dumps(meta or {}, ensure_ascii=False)))
        self.db.commit()
        self._write_daily(day, text)
        self._export_diary()
        return self.diary(day)

    def diary(self, day):
        row = self.db.execute('SELECT id,day,content,created,meta FROM diary_entries WHERE day=?',
                              (str(day)[:10],)).fetchone()
        if not row:
            return None
        return {'id': row[0], 'day': row[1], 'content': row[2], 'created': row[3],
                'meta': json.loads(row[4] or '{}')}

    def diaries(self, limit=30):
        rows = self.db.execute('SELECT id,day,content,created,meta FROM diary_entries '
                               'ORDER BY day DESC LIMIT ?', (max(1, min(365, int(limit))),)).fetchall()
        return [{'id': r[0], 'day': r[1], 'content': r[2], 'created': r[3],
                 'meta': json.loads(r[4] or '{}')} for r in rows]
