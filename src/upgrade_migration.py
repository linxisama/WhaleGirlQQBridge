"""安装包覆盖升级：版本记录、配置备份、幂等数据库补丁。

只做**增量**：
- 不改用户配置值，只备份；
- 不删数据表/消息/插件；
- 每个版本只备份一次配置；
- 数据库补丁使用 IF NOT EXISTS。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 2


class UpgradeManager:
    def __init__(self, app_version, db, config_path, data_dir):
        self.app_version = str(app_version or '')
        self.db = db
        self.config_path = Path(config_path) if config_path else None
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / 'upgrade_state.json'

    def _read_state(self):
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _backup_config(self, previous):
        if not self.config_path or not self.config_path.is_file():
            return ''
        folder = self.data_dir / 'backups'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        safe_old = str(previous or 'unknown').replace('/', '_').replace('\\', '_')
        target = folder / f'config-before-{safe_old}-{stamp}.yaml'
        try:
            shutil.copy2(self.config_path, target)
            return str(target)
        except OSError:
            return ''

    def run(self):
        state = self._read_state()
        previous = str(state.get('app_version') or '')
        changed = previous != self.app_version
        backup = ''
        if changed:
            backup = self._backup_config(previous)
        # 幂等补丁：老库没有统一记忆/日记表时补上；已有数据不动。
        self.db.execute('''CREATE TABLE IF NOT EXISTS unified_memories(
            id TEXT PRIMARY KEY, source TEXT, content TEXT, importance INTEGER,
            created REAL, last_used REAL, uses INTEGER, expires_at REAL,
            tags TEXT, persona TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS diary_entries(
            id TEXT PRIMARY KEY, day TEXT UNIQUE, content TEXT, created REAL, meta TEXT)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS unified_created ON unified_memories(created)')
        self.db.execute('CREATE INDEX IF NOT EXISTS diary_day ON diary_entries(day)')
        self.db.execute('CREATE TABLE IF NOT EXISTS schema_migrations('
                        'version INTEGER PRIMARY KEY, applied REAL, note TEXT)')
        self.db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?,?,?)',
                        (SCHEMA_VERSION, time.time(), f'upgrade to {self.app_version}'))
        self.db.commit()
        if changed:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {'app_version': self.app_version, 'previous': previous,
                       'schema_version': SCHEMA_VERSION, 'migrated_at': time.time(),
                       'config_backup': backup}
            temp = self.state_path.with_suffix('.tmp')
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
            temp.replace(self.state_path)
        return {'changed': changed, 'previous': previous, 'backup': backup,
                'schema_version': SCHEMA_VERSION}
