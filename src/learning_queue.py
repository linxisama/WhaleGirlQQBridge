"""网络学习队列：只存任务元数据，不预存网页全文；逐条限时读取。"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid


class LearningQueue:
    def __init__(self, db):
        self.db = db
        self.db.execute('''CREATE TABLE IF NOT EXISTS learning_queue(
            id TEXT PRIMARY KEY, kind TEXT, target TEXT, status TEXT,
            attempts INTEGER, created REAL, updated REAL, result TEXT, error TEXT)''')
        self.db.commit()

    def enqueue(self, target, kind='web'):
        target = str(target or '').strip()[:2000]
        if kind not in ('web', 'chat'):
            raise ValueError('学习类型只支持 web/chat')
        if kind == 'web' and not re.match(r'^https?://', target):
            raise ValueError('网页学习只接受 http/https 地址')
        if not target:
            raise ValueError('学习目标不能为空')
        existing = self.db.execute(
            'SELECT id FROM learning_queue WHERE kind=? AND target=? AND status IN (?,?)',
            (kind, target, 'pending', 'processing')).fetchone()
        if existing:
            return {'id': existing[0], 'duplicate': True}
        identifier = uuid.uuid4().hex
        now = time.time()
        self.db.execute('INSERT INTO learning_queue VALUES(?,?,?,?,?,?,?,?,?)',
                        (identifier, kind, target, 'pending', 0, now, now, '', ''))
        self.db.commit()
        return {'id': identifier, 'duplicate': False}

    def next(self):
        row = self.db.execute(
            "SELECT id,kind,target,attempts FROM learning_queue WHERE status='pending' "
            'ORDER BY created LIMIT 1').fetchone()
        if not row:
            return None
        self.db.execute("UPDATE learning_queue SET status='processing',updated=?,attempts=attempts+1 "
                        'WHERE id=?', (time.time(), row[0]))
        self.db.commit()
        return {'id': row[0], 'kind': row[1], 'target': row[2], 'attempts': row[3] + 1}

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

    def list(self, limit=30):
        rows = self.db.execute(
            'SELECT id,kind,target,status,attempts,created,result,error FROM learning_queue '
            'ORDER BY created DESC LIMIT ?', (max(1, min(100, int(limit))),)).fetchall()
        return [{'id': r[0], 'kind': r[1], 'target': r[2], 'status': r[3],
                 'attempts': r[4], 'created': r[5], 'result': r[6], 'error': r[7]}
                for r in rows]

    def stats(self):
        rows = self.db.execute('SELECT status,count(*) FROM learning_queue GROUP BY status').fetchall()
        return {str(k): int(v) for k, v in rows}
