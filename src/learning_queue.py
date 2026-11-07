"""网络学习队列：只存任务元数据，不预存网页全文；逐条限时读取。"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid

DEFAULT_PERSONA = ''


def _persona(persona) -> str:
    return str(persona or '').strip()[:40]


class LearningQueue:
    def __init__(self, db):
        self.db = db
        self.db.execute('''CREATE TABLE IF NOT EXISTS learning_queue(
            id TEXT PRIMARY KEY, kind TEXT, target TEXT, status TEXT,
            attempts INTEGER, created REAL, updated REAL, result TEXT, error TEXT,
            persona TEXT DEFAULT '')''')
        # 老库没有 persona 列：补一列，老记录落到默认人格，不重建表也就不会丢数据。
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(learning_queue)')}
        if 'persona' not in columns:
            self.db.execute("ALTER TABLE learning_queue ADD COLUMN persona TEXT DEFAULT ''")
        self.db.commit()

    def enqueue(self, target, kind='web', persona=DEFAULT_PERSONA):
        target = str(target or '').strip()[:2000]
        persona = _persona(persona)
        if kind not in ('web', 'chat'):
            raise ValueError('学习类型只支持 web/chat')
        if kind == 'web' and not re.match(r'^https?://', target):
            raise ValueError('网页学习只接受 http/https 地址')
        if not target:
            raise ValueError('学习目标不能为空')
        # 查重也按人格：别人格学过的同一目标，这个人格还得自己学一遍。
        existing = self.db.execute(
            'SELECT id FROM learning_queue WHERE kind=? AND target=? AND persona=? '
            'AND status IN (?,?)', (kind, target, persona, 'pending', 'processing')).fetchone()
        if existing:
            return {'id': existing[0], 'duplicate': True}
        identifier = uuid.uuid4().hex
        now = time.time()
        self.db.execute(
            'INSERT INTO learning_queue(id,kind,target,status,attempts,created,updated,result,error,persona) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (identifier, kind, target, 'pending', 0, now, now, '', '', persona))
        self.db.commit()
        return {'id': identifier, 'duplicate': False}

    def next_any(self):
        """取任意人设的最早一条待学任务；返回里带 persona，消费者按它选学习库。

        队列按人设分，但消费者只有一个：只按当前人设取，切走人设后别人格排进去的
        任务就永远不跑了。"""
        row = self.db.execute(
            "SELECT id,kind,target,attempts,persona FROM learning_queue WHERE status='pending' "
            'ORDER BY created LIMIT 1').fetchone()
        if not row:
            return None
        self.db.execute("UPDATE learning_queue SET status='processing',updated=?,attempts=attempts+1 "
                        'WHERE id=?', (time.time(), row[0]))
        self.db.commit()
        return {'id': row[0], 'kind': row[1], 'target': row[2], 'attempts': row[3] + 1,
                'persona': str(row[4] or '')}

    def next(self, persona=DEFAULT_PERSONA):
        persona = _persona(persona)
        row = self.db.execute(
            "SELECT id,kind,target,attempts FROM learning_queue WHERE status='pending' AND persona=? "
            'ORDER BY created LIMIT 1', (persona,)).fetchone()
        if not row:
            return None
        self.db.execute("UPDATE learning_queue SET status='processing',updated=?,attempts=attempts+1 "
                        'WHERE id=?', (time.time(), row[0]))
        self.db.commit()
        return {'id': row[0], 'kind': row[1], 'target': row[2], 'attempts': row[3] + 1,
                'persona': persona}

    def finish(self, identifier, result=''):
        self.db.execute("UPDATE learning_queue SET status='done',updated=?,result=? WHERE id=?",
                        (time.time(), str(result)[:1000], identifier))
        self.db.commit()

    def fail(self, identifier, error=''):
        self.db.execute("UPDATE learning_queue SET status='failed',updated=?,error=? WHERE id=?",
                        (time.time(), str(error)[:500], identifier))
        self.db.commit()

    def retry(self, identifier):
        self.db.execute("UPDATE learning_queue SET status='pending',updated=?,error='' WHERE id=?",
                        (time.time(), identifier))
        self.db.commit()

    def list(self, limit=30, persona=None):
        """persona=None 时列全部（控制台总览用），给字符串时只看这个人格。"""
        limit = max(1, min(100, int(limit)))
        if persona is None:
            rows = self.db.execute(
                'SELECT id,kind,target,status,attempts,created,result,error,persona FROM learning_queue '
                'ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        else:
            rows = self.db.execute(
                'SELECT id,kind,target,status,attempts,created,result,error,persona FROM learning_queue '
                'WHERE persona=? ORDER BY created DESC LIMIT ?', (_persona(persona), limit)).fetchall()
        return [{'id': r[0], 'kind': r[1], 'target': r[2], 'status': r[3],
                 'attempts': r[4], 'created': r[5], 'result': r[6], 'error': r[7],
                 'persona': r[8] or ''} for r in rows]

    def stats(self, persona=None):
        if persona is None:
            rows = self.db.execute('SELECT status,count(*) FROM learning_queue GROUP BY status').fetchall()
        else:
            rows = self.db.execute('SELECT status,count(*) FROM learning_queue WHERE persona=? '
                                   'GROUP BY status', (_persona(persona),)).fetchall()
        return {str(k): int(v) for k, v in rows}
