"""多账号：账号簿（第一个切片）

目标（用户第 1 条）：一个程序实例里管多个机器人账号，**每个账号独立配置**
（连接方式、API/模型、人设、插件设置、聊天记录）。本切片先把"账号从哪来、配置存哪"
落地，切换与并发运行在后续切片接上。

设计要点：
- 账号簿 `data/qq-only/accounts.json`：`{active, items:[{id,label,config,db,created}]}`；
- **首次运行自动迁移**：已有 `config.qq.yaml` + `data/qq-only/messages.sqlite3` 直接登记为
  `默认账号`，**不移动、不复制**用户数据（零风险）；
- 新账号：`data/accounts/<id>/{config.qq.yaml,messages.sqlite3}`，配置从当前默认账号**复制**一份
  （含 API Key 等，保持"能用"，但不共享聊天记录）；
- 删除账号只从账号簿移除，**目录保留**（避免误删聊天记录）；最后一个账号不能删。
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

__all__ = ['AccountBook', 'DEFAULT_ACCOUNT_ID']


DEFAULT_ACCOUNT_ID = 'default'


class AccountBook:
    def __init__(self, root):
        self.root = Path(root)
        self.data_dir = self.root / 'data' / 'qq-only'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / 'accounts.json'
        self._data = self._load()

    # ── 读写 ────────────────────────────────────────────────────────
    def _load(self) -> dict:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(data, dict) and isinstance(data.get('items'), list):
                    return data
            except (OSError, ValueError):
                pass
        return self._migrate()

    def _migrate(self) -> dict:
        """把现有的单账号数据登记为"默认账号"（不移动任何文件）。"""
        data = {'active': DEFAULT_ACCOUNT_ID, 'items': [{
            'id': DEFAULT_ACCOUNT_ID,
            'label': '默认账号',
            'config': 'config.qq.yaml',
            'db': 'data/qq-only/messages.sqlite3',
            'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        }]}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return data

    def _save(self):
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                             encoding='utf-8')

    # ── 查询 ────────────────────────────────────────────────────────
    def items(self) -> list:
        return [dict(item) for item in self._data['items']]

    def labels(self) -> list:
        return [(item['id'], item.get('label') or item['id']) for item in self._data['items']]

    def active(self) -> str:
        active = str(self._data.get('active') or '')
        if active not in {item['id'] for item in self._data['items']}:
            active = self._data['items'][0]['id']
            self._data['active'] = active
            self._save()
        return active

    def get(self, account_id: str) -> dict:
        for item in self._data['items']:
            if item['id'] == account_id:
                return dict(item)
        raise KeyError(account_id)

    def paths(self, account_id: str | None = None) -> tuple[Path, Path]:
        """返回该账号的 (配置文件, 数据库) 绝对路径。"""
        item = self.get(account_id or self.active())
        return self.root / item['config'], self.root / item['db']

    # ── 增删选 ──────────────────────────────────────────────────────
    def select(self, account_id: str) -> dict:
        self.get(account_id)          # 不存在就抛
        self._data['active'] = account_id
        self._save()
        return self.get(account_id)

    def add(self, label: str = '', *, clone_from: str | None = None) -> dict:
        label = (label or '').strip()[:40] or '新账号'
        account_id = uuid.uuid4().hex[:8]
        folder = self.root / 'data' / 'accounts' / account_id
        (folder / 'data' / 'qq-only').mkdir(parents=True, exist_ok=True)
        source_config, _source_db = self.paths(clone_from)
        target_config = folder / 'config.qq.yaml'
        if source_config.is_file():
            shutil.copy2(source_config, target_config)
        item = {
            'id': account_id,
            'label': label,
            'config': f'data/accounts/{account_id}/config.qq.yaml',
            'db': f'data/accounts/{account_id}/data/qq-only/messages.sqlite3',
            'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        self._data['items'].append(item)
        self._data['active'] = account_id
        self._save()
        return dict(item)

    def rename(self, account_id: str, label: str) -> dict:
        item = self.get(account_id)
        item['label'] = (label or '').strip()[:40] or item['label']
        for stored in self._data['items']:
            if stored['id'] == account_id:
                stored['label'] = item['label']
        self._save()
        return item

    def remove(self, account_id: str) -> bool:
        if len(self._data['items']) <= 1:
            raise ValueError('最后一个账号不能删除')
        self.get(account_id)
        self._data['items'] = [item for item in self._data['items'] if item['id'] != account_id]
        if self._data.get('active') == account_id:
            self._data['active'] = self._data['items'][0]['id']
        self._save()
        return True
