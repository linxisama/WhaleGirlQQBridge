"""Agent 多会话存储：每段对话独立保留任务卡片，重启后还在。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

MAX_SESSIONS = 50
MAX_TURNS = 200
MAX_BYTES = 2 * 1024 * 1024


class AgentSessionStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = {'version': 1, 'sessions': []}
        self.load()

    def load(self):
        with self._lock:
            try:
                if self.path.is_file() and self.path.stat().st_size <= MAX_BYTES:
                    data = json.loads(self.path.read_text(encoding='utf-8'))
                    if isinstance(data, dict) and isinstance(data.get('sessions'), list):
                        self._state = {'version': 1, 'sessions': data['sessions']}
            except (OSError, ValueError, RecursionError):
                self._state = {'version': 1, 'sessions': []}
            if not self._state['sessions']:
                self._state['sessions'].append(self._new('新对话'))

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(temp, self.path)

    def _new(self, title='新对话'):
        now = time.time()
        return {'id': uuid.uuid4().hex, 'title': str(title or '新对话')[:60],
                'created': now, 'updated': now, 'turns': []}

    def list(self):
        with self._lock:
            items = sorted(self._state['sessions'], key=lambda s: float(s.get('updated') or 0), reverse=True)
            return [{'id': s['id'], 'title': s.get('title') or '新对话',
                     'created': s.get('created'), 'updated': s.get('updated'),
                     'turns': len(s.get('turns') or [])} for s in items]

    def get(self, identifier):
        with self._lock:
            return next((s for s in self._state['sessions'] if s.get('id') == identifier), None)

    def create(self, title='新对话'):
        with self._lock:
            session = self._new(title)
            self._state['sessions'].insert(0, session)
            self._state['sessions'] = self._state['sessions'][:MAX_SESSIONS]
            self.save()
            return session

    def rename(self, identifier, title):
        with self._lock:
            session = self.get(identifier)
            if session is None:
                raise ValueError('对话不存在')
            session['title'] = str(title or '').strip()[:60] or '新对话'
            session['updated'] = time.time()
            self.save()
            return session

    def delete(self, identifier):
        with self._lock:
            before = len(self._state['sessions'])
            self._state['sessions'] = [s for s in self._state['sessions'] if s.get('id') != identifier]
            if not self._state['sessions']:
                self._state['sessions'].append(self._new())
            self.save()
            return len(self._state['sessions']) != before

    def turns(self, identifier, limit=20):
        with self._lock:
            session = self.get(identifier)
            if session is None:
                return []
            return list(session.get('turns') or [])[-max(1, int(limit)):]

    def add_turn(self, identifier, prompt, event, summary='', job_id=''):
        with self._lock:
            session = self.get(identifier)
            if session is None:
                session = self._new()
                self._state['sessions'].insert(0, session)
            session.setdefault('turns', []).append({
                'time': time.time(), 'prompt': str(prompt or '')[:1200],
                'event': str(event or ''), 'summary': str(summary or '')[:1800],
                'job': str(job_id or '')})
            session['turns'] = session['turns'][-MAX_TURNS:]
            session['updated'] = time.time()
            if not str(session.get('title') or '').strip() or session.get('title') == '新对话':
                session['title'] = (str(prompt or '').strip()[:24] or '新对话')
            self.save()
            return session
