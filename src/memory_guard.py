"""记忆守卫：写回体检（超长 / 密钥 / 相对时间 / 流水账 / 近似重复 / 关键词偏少）+ 按半衰期降权。

算法参考「神鲸计划」的 memory-gate.mjs 与 memory-decay.mjs，落在本项目单库
unified_memories 上。三条边界必须守住：

1. 默认只报告，apply=True 才动数据（超长截断、密钥打码、近似重复归档）；
2. 相对时间与流水账只报告、不自动改：它们是「这句不该写成长期事实」的信号，
   怎么改写要人判断，机器改了会丢原意；
3. 衰减不删数据：rules / soul / user / project 整层豁免，fact / lesson / topic
   按 140 天半衰期算权重，45 天没动且权重 < 0.8 的归档（status='archived'，
   用 unified_memory.update 就能恢复）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .unified_memory import HALF_LIFE_DAYS, _clean, _keywords, _similarity

LIMITS = {'fact': 200, 'lesson': 300, 'topic': 400, 'project': 1200,
          'rules': 300, 'soul': 300, 'user': 300}
DUP_THRESHOLD = 0.95
STALE_DAYS = 45.0
STALE_WEIGHT = 0.8
IMPORTANCE_FLOOR = 60       # importance >= 60 的记忆永不衰减（参考神鲸 3/5 分制）
USE_BONUS = 0.05            # 每次召回最多加 0.05，最多按 20 次算
EXEMPT_LAYERS = ('rules', 'soul', 'user', 'project')
SMELL_LAYERS = ('fact', 'lesson', 'project', 'soul', 'user')
# 只读体检（参考 memory-audit.mjs）：失效路径 / 疑似冲突 / 冷宫条目
CONFLICT_LAYERS = ('fact', 'lesson', 'topic', 'project', 'rules')
CONFLICT_LOW = 0.5
CONFLICT_LIMIT = 10
COLD_DAYS = 30.0
COLD_LIMIT = 15
DEAD_PATH_LIMIT = 20
# 只读体检：关键词太少的长条目，检索容易打偏（参考 memory-gate 的 weak_keywords）
WEAK_KEYWORD_CHARS = 30
WEAK_KEYWORD_MIN = 3
WEAK_KEYWORD_LIMIT = 20
KEYWORD_FIX_LIMIT = 50       # 整理时最多给 50 条补关键词，避免一次改动过大
BACKUP_KEEP = 10            # 每次整理前留一份库备份，最多保留 10 份
PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'|<>*?，。；：、（）【】「」]+")

SECRET = re.compile(r'(sk-[A-Za-z0-9]{12,})|((api[_-]?key|token|password|secret)\s*[:=]\s*\S{8,})'
                    r'|(密码\s*[:：=])', re.I)
RELATIVE_TIME = re.compile(r'(今天|昨天|前天|刚才|刚刚|今晚|明天|上周)')
DIARY_SMELL = re.compile(r'^(然后|接着|后来|于是|结果|之后)')

_COLUMNS = ('id', 'layer', 'content', 'importance', 'confidence', 'status',
            'updated', 'last_used', 'created', 'project', 'channel', 'persona',
            'keywords', 'uses')
_SELECT = ('SELECT id,layer,content,importance,confidence,status,updated,last_used,'
           'created,project,channel,persona,keywords,uses FROM unified_memories '
           'WHERE status=?')
KINDS = ('too_long', 'secret', 'relative_time', 'diary_smell', 'duplicate')


def _now():
    return time.time()


def _active(memory):
    """只体检还在用的行；archived/superseded 是历史，重复体检没有意义。"""
    try:
        rows = memory.db.execute(_SELECT, ('active',)).fetchall()
    except Exception:
        return []
    return [dict(zip(_COLUMNS, row)) for row in rows]


def _overview(memory):
    """按层/状态统计（只读）：体检报告首页用，缺表时给空表而不是崩。"""
    order = ('soul', 'user', 'project', 'fact', 'lesson', 'topic', 'rules')
    layers = {name: {'total': 0, 'active': 0, 'other': 0} for name in order}
    statuses = {}
    try:
        rows = memory.db.execute(
            'SELECT layer,status,COUNT(*) FROM unified_memories GROUP BY layer,status').fetchall()
    except Exception:
        rows = []
    for layer, status, count in rows:
        layer = str(layer or 'fact')
        status = str(status or 'active')
        row = layers.setdefault(layer, {'total': 0, 'active': 0, 'other': 0})
        amount = int(count or 0)
        row['total'] += amount
        if status == 'active':
            row['active'] += amount
        else:
            row['other'] += amount
        statuses[status] = statuses.get(status, 0) + amount
    return {'layers': [dict(layer=name, **layers[name]) for name in layers],
            'statuses': statuses}


def limit_for(layer):
    return int(LIMITS.get(str(layer or ''), 400))


def mask_secret(text):
    """只留字段名，值一律变成 ***（原文绝不回显到界面或日志）。

    SECRET 只标出「这里有密钥」，它匹配「密码：」这种只有标签的情况时会漏掉后面的值，
    所以打码单独走三步：先按字段名吃掉整段值，再兜一遍 sk- 长串。
    """
    text = str(text or '')
    text = re.sub(r'((?:api[_-]?key|token|password|secret)\s*[:=]\s*)\S+', r'\1***', text, flags=re.I)
    text = re.sub(r'(密码\s*[:：=]\s*)\S+', r'\1***', text)
    return re.sub(r'sk-[A-Za-z0-9]{12,}', 'sk-***', text)


def idle_days(item, now=None):
    """距上次被用到/被改过多少天；两者都缺就按创建时间算。"""
    now = _now() if now is None else float(now)
    stamp = item.get('last_used') or item.get('updated') or item.get('created') or now
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        stamp = now
    return max(0.0, (now - stamp) / 86400.0)


def weight(item, now=None):
    """140 天半衰期的记忆权重：140 天没动 = 0.5，280 天 = 0.25。"""
    return round(0.5 ** (idle_days(item, now) / HALF_LIFE_DAYS), 4)


def decay_score(item, now=None):
    """参考神鲸的退休权重：重要性占 0~1，召回次数给加成，再乘半衰期。"""
    try:
        importance = max(0, min(100, int(item.get('importance') or 0)))
    except (TypeError, ValueError):
        importance = 0
    try:
        uses = max(0, min(20, int(item.get('uses') or 0)))
    except (TypeError, ValueError):
        uses = 0
    return round(importance / 100.0 * weight(item, now) + uses * USE_BONUS, 4)


def scan(memory, now=None):
    """只读体检。findings 里绝不出现密钥原文，密钥只给「已隐去」后的样子。"""
    now = _now() if now is None else float(now)
    items = _active(memory)
    findings = []
    for item in items:
        text = str(item.get('content') or '')
        layer = str(item.get('layer') or 'fact')
        cap = limit_for(layer)
        if len(text) > cap:
            findings.append({'kind': 'too_long', 'id': item['id'], 'layer': layer,
                             'detail': f'超过 {cap} 字（现有 {len(text)} 字）',
                             'before': len(text), 'after': cap})
        if SECRET.search(text):
            findings.append({'kind': 'secret', 'id': item['id'], 'layer': layer,
                             'detail': '含密钥/口令字样，不该写进记忆',
                             'before': '', 'after': mask_secret(text)})
        if layer in SMELL_LAYERS:
            if RELATIVE_TIME.search(text):
                findings.append({'kind': 'relative_time', 'id': item['id'], 'layer': layer,
                                 'detail': '用了相对时间（今天/刚才…），过几天就说不清了',
                                 'before': '', 'after': ''})
            if DIARY_SMELL.search(text):
                findings.append({'kind': 'diary_smell', 'id': item['id'], 'layer': layer,
                                 'detail': '以「然后/后来…」开头，像流水账不像事实',
                                 'before': '', 'after': ''})
    groups = {}
    for item in items:
        key = (str(item.get('layer') or 'fact'), str(item.get('project') or ''),
               str(item.get('channel') or ''), str(item.get('persona') or ''))
        groups.setdefault(key, []).append(item)
    for group in groups.values():
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda x: (float(x.get('importance') or 0),
                                             float(x.get('confidence') or 0),
                                             float(x.get('updated') or 0)), reverse=True)
        for index, keep in enumerate(group):
            for other in group[index + 1:]:
                if _similarity(keep.get('content') or '', other.get('content') or '') >= DUP_THRESHOLD:
                    findings.append({'kind': 'duplicate', 'id': other['id'],
                                     'layer': str(other.get('layer') or 'fact'),
                                     'detail': f"与 {keep['id'][:8]} 近似重复，归档这条",
                                     'before': '', 'after': ''})
                    group = [row for row in group if row['id'] != other['id']]
    counts = {kind: 0 for kind in KINDS}
    for finding in findings:
        counts[finding['kind']] = counts.get(finding['kind'], 0) + 1
    return {'checked': len(items), 'findings': findings, 'counts': counts}


def apply_findings(memory, findings, now=None):
    """按体检结果动数据：超长截断、密钥打码、近似重复归档。相对时间/流水账不改。"""
    now = _now() if now is None else float(now)
    done = {'truncated': 0, 'masked': 0, 'archived_dup': 0}
    for finding in findings or []:
        kind = finding.get('kind')
        identifier = finding.get('id')
        if not identifier:
            continue
        if kind == 'too_long':
            row = memory.get(identifier)
            if not row:
                continue
            text = _clean(row.get('content'), limit_for(row.get('layer')))
            if text and text != row.get('content'):
                memory.db.execute('UPDATE unified_memories SET content=?,updated=? WHERE id=?',
                                  (text, now, identifier))
                done['truncated'] += 1
        elif kind == 'secret':
            row = memory.get(identifier)
            if not row:
                continue
            text = mask_secret(row.get('content'))
            if text != row.get('content'):
                memory.db.execute('UPDATE unified_memories SET content=?,updated=? WHERE id=?',
                                  (text, now, identifier))
                done['masked'] += 1
        elif kind == 'duplicate':
            memory.db.execute("UPDATE unified_memories SET status='archived',updated=? WHERE id=?",
                              (now, identifier))
            done['archived_dup'] += 1
    try:
        memory.db.commit()
    except Exception:
        pass
    return done


def decay(memory, now=None, apply=False):
    """半衰期降权：45 天没动且权重 < 0.8 的 fact/lesson/topic 归档（不删）。"""
    now = _now() if now is None else float(now)
    candidates = []
    for item in _active(memory):
        layer = str(item.get('layer') or 'fact')
        if layer in EXEMPT_LAYERS:
            continue
        try:
            importance = int(item.get('importance') or 0)
        except (TypeError, ValueError):
            importance = 0
        if importance >= IMPORTANCE_FLOOR:
            continue
        idle = idle_days(item, now)
        score = decay_score(item, now)
        if idle >= STALE_DAYS and score < STALE_WEIGHT:
            candidates.append({'id': item['id'], 'layer': layer, 'idle_days': round(idle, 1),
                               'weight': score, 'importance': importance,
                               'uses': int(item.get('uses') or 0)})
    if apply and candidates:
        for candidate in candidates:
            memory.db.execute("UPDATE unified_memories SET status='archived' WHERE id=?",
                              (candidate['id'],))
        try:
            memory.db.commit()
        except Exception:
            pass
    return {'candidates': len(candidates), 'archived': len(candidates) if apply else 0,
            'items': candidates[:20]}


def backup(memory, out_dir, db_path='', keep=BACKUP_KEEP, now=None):
    """在整理前给记忆库做一份一致性备份；只保留最近 keep 份。

    用 sqlite3 的 backup API，而不是直接复制文件：库开着 WAL 时直接复制可能拿到
    半截事务。没有真实库路径（例如测试里的 :memory:）就跳过并说明原因。
    """
    source = str(db_path or '')
    if not source:
        return {'path': '', 'name': '', 'bytes': 0, 'kept': 0, 'removed': 0,
                'skipped': 'no_db_path'}
    if not Path(source).exists():
        return {'path': '', 'name': '', 'bytes': 0, 'kept': 0, 'removed': 0,
                'skipped': 'db_missing'}
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(_now() if now is None else float(now)))
    target = folder / ('memory-' + stamp + '.db')
    index = 1
    while target.exists():
        target = folder / ('memory-' + stamp + '-' + str(index) + '.db')
        index += 1
    target_conn = sqlite3.connect(str(target))
    try:
        memory.db.backup(target_conn)
    except Exception:
        target_conn.close()
        try:
            target.unlink()
        except OSError:
            pass
        raise
    else:
        target_conn.close()
    keep = max(1, int(keep or 1))
    files = sorted(folder.glob('memory-*.db'), key=lambda item: item.stat().st_mtime,
                   reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return {'path': str(target), 'name': target.name, 'bytes': target.stat().st_size,
            'kept': min(len(files), keep), 'removed': removed}


def list_backups(out_dir, limit=20):
    """列出记忆库备份（最新在前）；只读，不创建目录。"""
    folder = Path(out_dir)
    if not folder.is_dir():
        return []
    items = []
    for path in folder.glob('memory-*.db'):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({'name': path.name, 'path': str(path),
                      'bytes': stat.st_size, 'mtime': stat.st_mtime})
    items.sort(key=lambda item: item['mtime'], reverse=True)
    return items[:max(1, int(limit or 20))]


def paths_in(text, limit=8):
    """正文里出现的 Windows 绝对路径（去重保序）；只认盘符开头的路径。"""
    out = []
    for match in PATH_RE.finditer(str(text or '')):
        path = match.group(0).strip().strip('.,;:')
        if path and path not in out:
            out.append(path)
        if len(out) >= limit:
            break
    return out


def path_alive(path):
    """这条路径还指得到东西吗：本身存在就算活；否则上溯到存在的祖先做前缀匹配。

    记忆里的路径经常被截断（写的人只写了半截），所以「父目录里有同名前缀的子项」
    也算活着 —— 宁可漏报，不误报，避免把好记忆标成失效。
    """
    text = str(path or '').strip()
    if len(text) < 4:
        return True
    try:
        target = Path(text)
        if target.exists():
            return True
        current = target
        for _ in range(8):
            parent = current.parent
            if parent == current:
                break
            if parent.exists():
                name = current.name
                if not name:
                    return True
                try:
                    children = os.listdir(parent)
                except OSError:
                    return True
                return any(child == name or child.startswith(name) or name.startswith(child)
                           for child in children)
            current = parent
    except (OSError, ValueError):
        return True
    return False


def find_dead_paths(items, limit=DEAD_PATH_LIMIT):
    """记忆里提到的本地文件/目录已经不存在了 —— 这类记忆照旧注入只会误导模型。"""
    out = []
    for item in items or []:
        for path in paths_in(item.get('content')):
            if path_alive(path):
                continue
            out.append({'kind': 'dead_path', 'id': item.get('id'),
                        'layer': item.get('layer') or 'fact', 'path': path,
                        'snippet': mask_secret(str(item.get('content') or ''))[:120],
                        'detail': '引用的路径已不存在：' + path})
            if len(out) >= limit:
                return out
    return out


def _keywords_of(item):
    raw = item.get('keywords')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or '[]')
        except (TypeError, ValueError):
            return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(word) for word in raw if str(word or '').strip()}
    return set()


def find_weak_keywords(items, limit=WEAK_KEYWORD_LIMIT):
    """长条目却几乎没关键词：向量/关键词检索都会打偏，只报告不改写。

    参考神鲸 memory-gate 的规则：正文超过 30 字、关键词少于 3 个。关键词本身
    可能带密钥，所以只返回数量，不回显关键词列表。
    """
    out = []
    for item in items or []:
        text = str(item.get('content') or '')
        if len(text) <= WEAK_KEYWORD_CHARS:
            continue
        words = _keywords_of(item)
        if len(words) >= WEAK_KEYWORD_MIN:
            continue
        out.append({'kind': 'weak_keywords', 'id': item.get('id'),
                    'layer': item.get('layer') or 'fact', 'keywords': len(words),
                    'snippet': mask_secret(text)[:120],
                    'detail': '关键词只有 %d 个，检索会打偏' % len(words)})
        if len(out) >= limit:
            break
    return out


def fix_weak_keywords(memory, items, limit=KEYWORD_FIX_LIMIT):
    """给关键词偏少的长条目补内部检索词；只改 keywords，不改正文。

    返回实际补上的条数。生成不出至少 3 个词时保持原样、继续在体检里报告，
    避免把一条本来就没法检索的记忆改成看似修好。
    """
    fixed = 0
    for finding in find_weak_keywords(items, limit=limit):
        row = memory.get(finding.get('id'))
        if not row:
            continue
        words = _keywords(str(row.get('content') or ''))
        if len(words) < WEAK_KEYWORD_MIN:
            continue
        try:
            # 只改 keywords，保留 updated/last_used：补检索词不该重置衰减计时。
            memory.db.execute('UPDATE unified_memories SET keywords=? WHERE id=?',
                              (json.dumps(words[:8], ensure_ascii=False), finding['id']))
            memory.db.commit()
        except Exception:
            continue
        fixed += 1
    return fixed


def find_conflicts(items, limit=CONFLICT_LIMIT):
    """同层同项目里「说得像但不一样」的两条：相似度 0.5~0.95，交给人工判断。

    近似重复（>=DUP_THRESHOLD）由 maintain 直接归档，这里不重复报；低于 0.5 的
    大多是无关条目，报了只会淹没真正的问题。
    """
    groups = {}
    for item in items or []:
        layer = str(item.get('layer') or 'fact')
        if layer not in CONFLICT_LAYERS:
            continue
        groups.setdefault((layer, str(item.get('project') or '')), []).append(item)
    out = []
    for (layer, project), rows in groups.items():
        rows = rows[:120]
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                ka, kb = _keywords_of(left), _keywords_of(right)
                if ka and kb and not (ka & kb):
                    continue
                score = _similarity(left.get('content'), right.get('content'))
                if not (CONFLICT_LOW <= score < DUP_THRESHOLD):
                    continue
                out.append({'kind': 'conflict', 'layer': layer, 'project': project,
                            'ids': [left.get('id'), right.get('id')],
                            'similarity': round(score, 3), 'shared': len(ka & kb),
                            'detail': '两条说法相近但不同，建议人工确认：'
                                      + mask_secret(str(left.get('content') or ''))[:40] + ' / '
                                      + mask_secret(str(right.get('content') or ''))[:40]})
                if len(out) >= limit:
                    return out
    return out


def find_cold(items, now=None, days=COLD_DAYS, limit=COLD_LIMIT):
    """冷宫条目：建了 >=30 天却一次都没被召回（uses=0）——该删该改由人定。"""
    now = _now() if now is None else float(now)
    out = []
    for item in items or []:
        try:
            if int(item.get('uses') or 0) > 0:
                continue
            created = float(item.get('created') or 0)
        except (TypeError, ValueError):
            continue
        if not created or (now - created) < float(days) * 86400:
            continue
        out.append({'kind': 'cold', 'id': item.get('id'), 'layer': item.get('layer') or 'fact',
                    'idle_days': round((now - created) / 86400.0, 1),
                    'importance': int(item.get('importance') or 0),
                    'snippet': mask_secret(str(item.get('content') or ''))[:80]})
        if len(out) >= limit:
            break
    return out


def report(memory, now=None):
    """给界面看的一份体检摘要：只读，不动数据。"""
    result = scan(memory, now)
    items = _active(memory)
    result['dead_paths'] = find_dead_paths(items)
    result['conflicts'] = find_conflicts(items)
    result['cold'] = find_cold(items, now)
    result['weak_keywords'] = find_weak_keywords(items)
    result['decay'] = decay(memory, now, apply=False)
    result['overview'] = _overview(memory)
    result['exempt'] = list(EXEMPT_LAYERS)
    result['limits'] = dict(LIMITS)
    return result


def maintain(memory, now=None, apply=True, backup_dir=None, db_path='', keep_backups=BACKUP_KEEP):
    """一键整理：先备份库（apply 时）+ 合并重复（dream）+ 体检 + 修数据 + 衰减归档。"""
    now = _now() if now is None else float(now)
    backup_info = {'path': '', 'name': '', 'bytes': 0, 'kept': 0,
                   'removed': 0, 'skipped': 'not_applied'}
    if apply and backup_dir is not None:
        try:
            backup_info = backup(memory, backup_dir, db_path, keep_backups, now)
        except Exception as exc:
            backup_info = {'path': '', 'name': '', 'bytes': 0, 'kept': 0,
                           'removed': 0, 'skipped': type(exc).__name__}
    dream = {}
    try:
        dream = memory.dream(now) or {}
    except Exception as exc:
        dream = {'error': type(exc).__name__}
    result = scan(memory, now)
    fixed = {'truncated': 0, 'masked': 0, 'archived_dup': 0, 'fixed_keywords': 0}
    if apply:
        fixed.update(apply_findings(memory, result['findings'], now))
    items = _active(memory)
    if apply:
        fixed['fixed_keywords'] = fix_weak_keywords(memory, items)
        if fixed['fixed_keywords']:
            items = _active(memory)
    decayed = decay(memory, now, apply=apply)
    # 三项审计只报告、不参与 apply：失效路径和冲突要靠人判断，冷宫可能只是季节性偏好。
    return {'checked': result['checked'], 'counts': result['counts'],
            **fixed,
            'dead_paths': find_dead_paths(items),
            'conflicts': find_conflicts(items),
            'cold': find_cold(items, now),
            'weak_keywords': find_weak_keywords(items),
            'samples': result['findings'][:10], 'decayed': decayed['archived'],
            'backup': backup_info,
            'decay_candidates': decayed['candidates'], 'applied': bool(apply),
            'merged': int(dream.get('merged') or 0),
            'archived_topics': int(dream.get('archived_topics') or 0),
            'remaining': memory.count()}


def _report_text(data, now=None, db_path=''):
    """把 report() 的结果渲染成可保存的 Markdown；所有正文都先 mask_secret。"""
    stamp = time.strftime('%Y-%m-%d', time.localtime(_now() if now is None else float(now)))
    lines = [f'# 记忆体检报告 {stamp}', '']
    if db_path:
        lines += [f'库：`{db_path}`（只读生成，未做任何修改）', '']
    lines += ['## 一、概览', '', '| 层级 | 总数 | active | 其它状态 |', '|---|---|---|---|']
    overview = data.get('overview') or {}
    rows = overview.get('layers') or []
    for item in rows:
        lines.append('| {0} | {1} | {2} | {3} |'.format(
            item.get('layer', ''), item.get('total', 0),
            item.get('active', 0), item.get('other', 0)))
    total = sum(int(item.get('total') or 0) for item in rows)
    active = sum(int(item.get('active') or 0) for item in rows)
    lines += ['| 合计 | {0} | {1} | {2} |'.format(total, active, total - active), '']
    statuses = overview.get('statuses') or {}
    status_text = '，'.join('{0}={1}'.format(k, v) for k, v in statuses.items()) or '无'
    lines += ['状态分布：' + status_text, '']

    lines += ['## 二、疑似失效（内容里的路径在本机已不存在）', '']
    dead = data.get('dead_paths') or []
    if not dead:
        lines += ['无。', '']
    else:
        for item in dead:
            lines.append('- **{0}/{1}** — 失效路径：`{2}`'.format(
                item.get('layer', ''), item.get('id', ''), item.get('path', '')))
            snippet = str(item.get('snippet') or '').replace('\n', ' ')
            if snippet:
                lines.append('  > ' + snippet)
        lines.append('')

    lines += ['## 三、疑似冲突（同项目、相似或共享关键词）', '']
    conflicts = data.get('conflicts') or []
    if not conflicts:
        lines += ['无。', '']
    else:
        for item in conflicts:
            ids = item.get('ids') or ['', '']
            lines.append('- **{0}/{1}** vs **{0}/{2}**（相似度 {3}，共享关键词 {4}）'.format(
                item.get('layer', ''), ids[0], ids[1],
                item.get('similarity', ''), item.get('shared', 0)))
            detail = str(item.get('detail') or '').replace('\n', ' ')
            if detail:
                lines.append('  - ' + detail)
        lines.append('')

    lines += ['## 四、冷宫候选（创建 >=30 天、从未被召回，最多 15 条）', '']
    cold = data.get('cold') or []
    if not cold:
        lines += ['无。', '']
    else:
        for item in cold:
            snippet = str(item.get('snippet') or '').replace('\n', ' ')
            lines.append('- {0}/{1} [{2} 天] importance={3} — {4}'.format(
                item.get('layer', ''), item.get('id', ''),
                item.get('idle_days', ''), item.get('importance', 0), snippet))
        lines.append('')

    lines += ['## 五、待降权（只报告，不自动处理）', '']
    decay_data = data.get('decay') or {}
    decay_items = decay_data.get('items') or decay_data.get('candidates') or []
    if not decay_items:
        lines += ['无。', '']
    else:
        for item in decay_items[:20]:
            lines.append('- {0}/{1} 已 {2} 天未动，权重 {3}'.format(
                item.get('layer', ''), item.get('id', ''),
                item.get('idle_days', ''), item.get('weight', '')))
        lines.append('')

    lines += ['## 六、接下来', '', '以上只是建议清单，未改动任何条目。', '',
              '1. 失效路径 → 在「对话与记忆」里确认后修正或归档；',
              '2. 冲突 → 保留一条权威，另一条改成指针或归档；',
              '3. 冷宫/待降权 → 点「整理记忆」按半衰期处理，或先观察；',
              '4. 关键词偏少 → 给这条记忆补几个能检索到的词，或改写得更短。', '']

    lines += ['## 附、关键词偏少（检索容易打偏）', '']
    weak = data.get('weak_keywords') or []
    if not weak:
        lines += ['无。', '']
    else:
        for item in weak:
            lines.append('- **{0}/{1}** — {2}'.format(
                item.get('layer', ''), item.get('id', ''), item.get('detail', '')))
            snippet = str(item.get('snippet') or '').replace('\n', ' ')
            if snippet:
                lines.append('  > ' + snippet)
        lines.append('')
    return '\n'.join(lines)


def write_report(memory, now=None, out_dir=None, db_path=''):
    """写一份只读体检报告，返回路径和数量；不改任何记忆行。"""
    data = report(memory, now)
    stamp = time.strftime('%Y%m%d', time.localtime(_now() if now is None else float(now)))
    target_dir = Path(out_dir) if out_dir is not None else Path('记忆')
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ('体检报告-' + stamp + '.md')
    text = _report_text(data, now, db_path)
    target.write_text(text, encoding='utf-8')
    decay_data = data.get('decay') or {}
    return {'path': str(target), 'bytes': len(text.encode('utf-8')),
            'counts': data.get('counts') or {},
            'overview': data.get('overview') or {},
            'dead_paths': len(data.get('dead_paths') or []),
            'conflicts': len(data.get('conflicts') or []),
            'cold': len(data.get('cold') or []),
            'weak_keywords': len(data.get('weak_keywords') or []),
            'decay_candidates': len(decay_data.get('items') or decay_data.get('candidates') or [])}


def _daily_line(guard, report_path=''):
    """每日日志的一行：只写数量与报告路径，不写记忆正文和密钥。"""
    guard = guard or {}
    counts = guard.get('counts') or {}
    parts = ['检查 %d 条' % int(guard.get('checked') or 0),
             '超长 %d' % int(counts.get('too_long') or 0),
             '含密钥 %d' % int(counts.get('secret') or 0),
             '截断 %d' % int(guard.get('truncated') or 0),
             '打码 %d' % int(guard.get('masked') or 0),
             '归档重复 %d' % int(guard.get('archived_dup') or 0),
             '降权归档 %d' % int(guard.get('decayed') or 0),
             '失效路径 %d' % len(guard.get('dead_paths') or []),
             '冲突 %d' % len(guard.get('conflicts') or []),
             '冷宫 %d' % len(guard.get('cold') or []),
             '补关键词 %d' % int(guard.get('fixed_keywords') or 0),
             '关键词偏少 %d' % len(guard.get('weak_keywords') or []),
             '剩余 %d 条' % int(guard.get('remaining') or 0)]
    line = '- ' + '，'.join(parts)
    backup = guard.get('backup') or {}
    if backup.get('name'):
        line += '；备份：' + str(backup.get('name'))
    if report_path:
        line += '；报告：' + str(report_path)
    return line


def daily(memory, now=None, out_dir=None, db_path='', apply=True):
    """每日维护：体检/整理 + 写报告 + 追加一行日志；返回给定时任务和界面。

    与 maintain 一样：apply=False 只读；报告和日志只写数量，不写记忆正文。
    """
    now = _now() if now is None else float(now)
    backup_dir = (Path(out_dir) / '备份') if out_dir is not None else None
    guard = maintain(memory, now, apply=apply, backup_dir=backup_dir, db_path=db_path)
    report_path = ''
    log_path = ''
    if out_dir is not None:
        try:
            written = write_report(memory, now, out_dir=out_dir, db_path=db_path)
            report_path = str(written.get('path') or '')
        except Exception:
            report_path = ''
        try:
            folder = Path(out_dir)
            folder.mkdir(parents=True, exist_ok=True)
            log = folder / '每日日志.md'
            stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
            head = '# 记忆每日日志\n\n' if not log.exists() else ''
            entry = '## {0}（{1}）\n{2}\n\n'.format(
                stamp, '整理' if apply else '只读', _daily_line(guard, report_path))
            with log.open('a', encoding='utf-8') as handle:
                if head:
                    handle.write(head)
                handle.write(entry)
            log_path = str(log)
        except Exception:
            log_path = ''
    return {'guard': guard, 'report': report_path, 'log': log_path,
            'backup': guard.get('backup') or {}, 'applied': bool(apply)}

