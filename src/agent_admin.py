"""Agent 的单一管理员绑定：只存一个 QQ 号，不存密码/令牌；可解绑。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


class AgentAdmin:
    def __init__(self, path):
        self.path = Path(path)
        self._state = {'admin': ''}
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                self._state = {'admin': str(data.get('admin') or '')}
        except (OSError, ValueError, RecursionError):
            self._state = {'admin': ''}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(temp, self.path)

    def status(self):
        admin = self._state.get('admin') or ''
        return {'admin': admin, 'bound': bool(admin),
                'masked': (admin[:2] + '****' + admin[-2:]) if len(admin) >= 5 else admin}

    def bind(self, qq):
        value = str(qq or '').strip()
        if not re.fullmatch(r'\d{5,15}', value):
            raise ValueError('管理员 QQ 必须是 5～15 位数字')
        self._state = {'admin': value, 'bound_at': time.time()}
        self.save()
        return self.status()

    def unbind(self):
        self._state = {'admin': ''}
        self.save()
        return self.status()

    def is_admin(self, qq):
        return bool(self._state.get('admin')) and str(qq) == self._state['admin']
