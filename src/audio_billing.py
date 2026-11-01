"""T07：云语音预算预留/结算（可单测的纯账本 + 可选 SQLite 持久化）。

规则（1.4.1 / T07 验收）：
- 调用前先预留，超限直接 ``BUDGET_EXCEEDED``，不发请求、不花钱；
- 成功按实际结算；
- “请求已提交但结果未知”（断线/超时）**不释放**预留，也不自动重复提交，
  避免把可能已经发生的计费当成 0；
- 明确未提交（连不上/鉴权失败）才释放预留。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from .audio_contracts import ErrorCode, reject

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_budget_ledger (
    entry_id    TEXT PRIMARY KEY,
    period      TEXT NOT NULL,          -- YYYY-MM-DD（按日统计；月/总额由配置另判）
    request_key TEXT NOT NULL,
    estimated   REAL NOT NULL DEFAULT 0,
    actual      REAL NOT NULL DEFAULT 0,
    state       TEXT NOT NULL,          -- reserved / settled / released / unknown
    note        TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE(period, request_key)
);
"""


@dataclass
class BudgetSnapshot:
    daily_limit: float
    monthly_limit: float
    spent_today: float
    reserved_today: float
    unknown_today: float
    spent_month: float
    remaining_today: float

    def to_dict(self) -> dict:
        return {'daily_limit': round(self.daily_limit, 4),
                'monthly_limit': round(self.monthly_limit, 4),
                'spent_today': round(self.spent_today, 4),
                'reserved_today': round(self.reserved_today, 4),
                'unknown_today': round(self.unknown_today, 4),
                'spent_month': round(self.spent_month, 4),
                'remaining_today': round(self.remaining_today, 4)}


class AudioBilling:
    """按“日/月上限 + 单次上限”记账；金额单位由调用方约定（示例用元）。"""

    def __init__(self, *, daily_limit: float = 5.0, monthly_limit: float = 50.0,
                 per_request_limit: float = 0.5, price_per_1k_chars: float = 0.01,
                 price_per_second: float = 0.0,
                 db: Optional[sqlite3.Connection] = None, now=None):
        self.daily_limit = float(daily_limit)
        self.monthly_limit = float(monthly_limit)
        self.per_request_limit = float(per_request_limit)
        self.price_per_1k_chars = float(price_per_1k_chars)
        self.price_per_second = float(price_per_second)
        self.db = db
        self._clock = now or time.time
        self._memory: list[dict] = []
        if db is not None:
            db.executescript(_SCHEMA)
            db.commit()

    # ── 估价 ────────────────────────────────────────────────────────────────
    def estimate(self, text: str) -> float:
        chars = max(1, len(str(text or '')))
        return round(chars / 1000.0 * self.price_per_1k_chars, 6)

    def estimate_seconds(self, seconds: float) -> float:
        """按秒计价（云歌曲）；未配置每秒单价时回退到按字符估价。"""
        if self.price_per_second <= 0:
            return self.estimate('')
        return round(max(0.0, float(seconds)) * self.price_per_second, 6)

    # ── 预留 ────────────────────────────────────────────────────────────────
    def reserve(self, request_key: str, estimated: float, now: Optional[float] = None) -> dict:
        key = str(request_key or '')
        if not key:
            reject(ErrorCode.INVALID_INPUT, '预算预留缺少请求编号')
        estimated = max(0.0, float(estimated))
        if estimated > self.per_request_limit:
            reject(ErrorCode.BUDGET_EXCEEDED,
                   f'单次预估费用 {estimated:.4f} 超过上限 {self.per_request_limit:.4f}',
                   estimated=estimated)
        timestamp = self._clock() if now is None else float(now)
        existing = self._find(key, timestamp)
        if existing is not None:
            if existing['state'] == 'released':
                # 上一次明确未提交（连不上/鉴权失败）：允许同一请求重试，复用同一条账目。
                snapshot = self.snapshot(now=timestamp)
                if snapshot.spent_today + snapshot.reserved_today + estimated > self.daily_limit:
                    reject(ErrorCode.BUDGET_EXCEEDED, '当日语音预算不足，已停止新的付费任务',
                           estimated=estimated, remaining=snapshot.remaining_today)
                self._update(existing['entry_id'], 'reserved', 0.0, '', timestamp)
                return {'request_key': key, 'state': 'reserved', 'duplicate': False,
                        'estimated': estimated, 'retried': True}
            return {'request_key': key, 'state': existing['state'],
                    'duplicate': True, 'estimated': existing['estimated']}
        snapshot = self.snapshot(now=timestamp)
        if snapshot.spent_today + snapshot.reserved_today + estimated > self.daily_limit:
            reject(ErrorCode.BUDGET_EXCEEDED, '当日语音预算不足，已停止新的付费任务',
                   estimated=estimated, remaining=snapshot.remaining_today)
        month = self._period(timestamp)[:7]
        reserved_month = sum(
            float(entry['estimated']) for entry in self._all()
            if entry['period'][:7] == month and entry['state'] == 'reserved')
        if snapshot.spent_month + reserved_month + estimated > self.monthly_limit:
            reject(ErrorCode.BUDGET_EXCEEDED, '当月语音预算不足（含预留），已停止新的付费任务',
                   estimated=estimated)
        entry = {'entry_id': uuid.uuid4().hex, 'period': self._period(timestamp),
                 'request_key': key, 'estimated': estimated, 'actual': 0.0,
                 'state': 'reserved', 'note': '', 'created_at': timestamp,
                 'updated_at': timestamp}
        self._insert(entry)
        return {'request_key': key, 'state': 'reserved', 'duplicate': False,
                'estimated': estimated}

    # ── 结算 ────────────────────────────────────────────────────────────────
    def settle(self, request_key: str, *, actual: Optional[float] = None,
               success: bool = True, submitted: bool = True,
               note: str = '', now: Optional[float] = None) -> dict:
        """成功→settled；明确未提交→released；已提交但未知→unknown（保留预留）。"""
        timestamp = self._clock() if now is None else float(now)
        entry = self._find(str(request_key or ''), timestamp)
        if entry is None:
            reject(ErrorCode.INVALID_INPUT, '找不到对应的预算预留，拒绝结算')
        if entry['state'] in ('settled', 'released'):
            return {'request_key': entry['request_key'], 'state': entry['state'],
                    'duplicate': True, 'actual': entry['actual']}
        if success:
            state, amount = 'settled', max(0.0, float(actual if actual is not None else entry['estimated']))
        elif not submitted:
            state, amount = 'released', 0.0
        else:
            # 可能已计费：保留预估金额，不释放额度，也不重复提交。
            state, amount = 'unknown', float(entry['estimated'])
        self._update(entry['entry_id'], state, amount, note, timestamp)
        return {'request_key': entry['request_key'], 'state': state, 'actual': amount,
                'duplicate': False}

    # ── 查询 ────────────────────────────────────────────────────────────────
    def snapshot(self, now: Optional[float] = None) -> BudgetSnapshot:
        timestamp = self._clock() if now is None else float(now)
        day = self._period(timestamp)
        month = day[:7]
        spent_today = reserved_today = unknown_today = spent_month = 0.0
        for entry in self._all():
            amount = float(entry['actual'] if entry['state'] == 'settled' else entry['estimated'])
            if entry['period'] == day:
                if entry['state'] == 'settled':
                    spent_today += amount
                elif entry['state'] == 'reserved':
                    reserved_today += amount
                elif entry['state'] == 'unknown':
                    unknown_today += amount
            if entry['period'][:7] == month and entry['state'] in ('settled', 'unknown'):
                spent_month += amount
        remaining = max(0.0, self.daily_limit - spent_today - reserved_today - unknown_today)
        return BudgetSnapshot(self.daily_limit, self.monthly_limit, spent_today,
                              reserved_today, unknown_today, spent_month, remaining)

    def entries(self) -> list[dict]:
        return [dict(item) for item in self._all()]

    def state_for(self, request_key: str, now: Optional[float] = None) -> str:
        """查同一请求的账本状态（供“未知结果不重提”判断）。"""
        timestamp = self._clock() if now is None else float(now)
        entry = self._find(str(request_key or ''), timestamp)
        return entry['state'] if entry else ''

    # ── 存取 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _period(timestamp: float) -> str:
        return time.strftime('%Y-%m-%d', time.localtime(timestamp))

    def _find(self, key: str, timestamp: float) -> Optional[dict]:
        day = self._period(timestamp)
        for entry in self._all():
            if entry['period'] == day and entry['request_key'] == key:
                return entry
        return None

    def _all(self) -> list[dict]:
        if self.db is None:
            return [dict(item) for item in self._memory]
        rows = self.db.execute(
            'SELECT entry_id,period,request_key,estimated,actual,state,note,'
            'created_at,updated_at FROM audio_budget_ledger ORDER BY created_at').fetchall()
        return [{'entry_id': r[0], 'period': r[1], 'request_key': r[2],
                 'estimated': float(r[3]), 'actual': float(r[4]), 'state': r[5],
                 'note': r[6] or '', 'created_at': float(r[7]),
                 'updated_at': float(r[8])} for r in rows]

    def _insert(self, entry: dict) -> None:
        if self.db is None:
            self._memory.append(entry)
            return
        self.db.execute(
            'INSERT INTO audio_budget_ledger (entry_id,period,request_key,estimated,actual,'
            'state,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (entry['entry_id'], entry['period'], entry['request_key'], entry['estimated'],
             entry['actual'], entry['state'], entry['note'], entry['created_at'],
             entry['updated_at']))
        self.db.commit()

    def _update(self, entry_id: str, state: str, amount: float, note: str,
                timestamp: float) -> None:
        if self.db is None:
            for entry in self._memory:
                if entry['entry_id'] == entry_id:
                    entry.update(state=state, actual=amount, note=note,
                                 updated_at=timestamp)
            return
        self.db.execute('UPDATE audio_budget_ledger SET state=?,actual=?,note=?,updated_at=? '
                        'WHERE entry_id=?', (state, amount, note, timestamp, entry_id))
        self.db.commit()
