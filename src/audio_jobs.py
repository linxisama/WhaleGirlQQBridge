"""T04：作业持久化、幂等去重、状态机（配合 audio_scheduler.py 做排队/取消/恢复）。

约束（T04 验收）：
- 重复请求只建一个作业（client_request_id + 参数哈希唯一）。
- 取消不会导致“完成后继续自动发送”。
- 重启不重放未核实请求（running/uncertain 先标待核实）。
- 歌曲不占住 QQ 消息处理线程；队列满给明确错误（QUEUE_FULL）。

状态机：queued → preparing → running → postprocessing → ready，
另有 failed / cancel_requested / cancelled / expired / unknown（见 JobState）。

实现补充（2026-10-20）：
- ``audio_jobs`` 只存状态与摘要；完整请求快照另存 ``audio_job_payloads``，
  重启恢复时据此重建请求（不重放“已提交但结果未知”的外部调用）。
- 终态锁定：到终态后不再被迟到的状态写覆盖，避免“取消后又被标 ready”。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from .audio_contracts import AudioJobRequest, ErrorCode, JobState, reject
from .audio_contracts_db import initialize as initialize_schema

TERMINAL_STATES = tuple(s.value for s in JobState if s.terminal)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_job_payloads (
    job_id       TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    provider     TEXT DEFAULT '',
    asset_id     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audio_job_payloads_asset ON audio_job_payloads(asset_id);
"""


class AudioJobStore:
    """audio_jobs 表的读写 + 幂等。真正的领取/执行在 AudioScheduler。"""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        initialize_schema(db)
        db.executescript(_SCHEMA)
        db.commit()

    # ── 入队 ────────────────────────────────────────────────────────────────
    def enqueue(self, request: AudioJobRequest, *, subject_domain: str, principal: str,
                job_id: str, param_hash: str, provider: str = '',
                cost_reserved: float = 0.0, now: float = 0.0) -> dict:
        """入队（幂等）。同键重复请求返回既有作业，不新建。"""
        if not subject_domain:
            reject(ErrorCode.UNAUTHORIZED, '缺少可信主体域，拒绝入队')
        if not job_id or not param_hash:
            reject(ErrorCode.INVALID_INPUT, '缺少作业编号或参数哈希')
        timestamp = float(now) if now else time.time()
        existing = self.find(request.client_request_id, param_hash)
        if existing is not None:
            return {'job_id': existing['job_id'], 'state': existing['state'],
                    'duplicate': True}
        same_id = self.get(job_id)
        if same_id is not None:
            return {'job_id': job_id, 'state': same_id['state'], 'duplicate': True}
        try:
            self.db.execute(
                'INSERT INTO audio_jobs (job_id,client_request_id,subject_domain,principal,mode,'
                'param_hash,state,provider,external_id,phase_started,error_code,cost_reserved,'
                'cost_actual,created_at,updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, request.client_request_id, str(subject_domain), str(principal),
                 request.mode.value, param_hash, JobState.QUEUED.value, str(provider or ''),
                 '', '', '', float(cost_reserved or 0.0), 0.0, timestamp, timestamp))
            self.db.execute(
                'INSERT INTO audio_job_payloads (job_id,request_json,provider) VALUES (?,?,?)',
                (job_id, json.dumps(request.model_dump(mode='json'), ensure_ascii=False,
                                    sort_keys=True), str(provider or '')))
            self.db.commit()
        except sqlite3.IntegrityError:
            # 并发下另一个连接先插入了同键作业：按幂等返回它。
            self.db.rollback()
            existing = self.find(request.client_request_id, param_hash)
            if existing is None:
                reject(ErrorCode.INVALID_INPUT, '作业编号冲突，请重试')
            return {'job_id': existing['job_id'], 'state': existing['state'],
                    'duplicate': True}
        return {'job_id': job_id, 'state': JobState.QUEUED.value, 'duplicate': False}

    # ── 状态 ────────────────────────────────────────────────────────────────
    def mark(self, job_id: str, state: JobState, *, phase_started: str = '',
             error_code: str = '', provider: str = '', now: float = 0.0) -> bool:
        """更新状态；终态锁定，迟到的写入被忽略（取消后不会又变 ready）。"""
        row = self.db.execute('SELECT state FROM audio_jobs WHERE job_id=?', (str(job_id),)).fetchone()
        if row is None:
            return False
        current = str(row[0])
        target = state.value if isinstance(state, JobState) else str(state)
        if current in TERMINAL_STATES and target != current:
            return False
        if current == target and not phase_started and not error_code and not provider:
            return True
        timestamp = float(now) if now else time.time()
        self.db.execute(
            'UPDATE audio_jobs SET state=?, phase_started=COALESCE(NULLIF(?,\'\'),phase_started),'
            ' error_code=COALESCE(NULLIF(?,\'\'),error_code),'
            ' provider=COALESCE(NULLIF(?,\'\'),provider), updated_at=? WHERE job_id=?',
            (target, str(phase_started or ''), str(error_code or ''), str(provider or ''),
             timestamp, str(job_id)))
        self.db.commit()
        return True

    def get(self, job_id: str) -> Optional[dict]:
        """返回已观测阶段，不编造生成百分比。"""
        previous = self.db.row_factory
        self.db.row_factory = sqlite3.Row
        try:
            row = self.db.execute('SELECT * FROM audio_jobs WHERE job_id=?', (str(job_id),)).fetchone()
        finally:
            self.db.row_factory = previous
        if row is None:
            return None
        data = dict(row)
        data['terminal'] = data.get('state') in TERMINAL_STATES
        payload = self.db.execute(
            'SELECT request_json,asset_id FROM audio_job_payloads WHERE job_id=?',
            (str(job_id),)).fetchone()
        if payload:
            data['asset_id'] = payload[1] or ''
            try:
                data['request'] = json.loads(payload[0])
            except (TypeError, ValueError):
                data['request'] = None
        return data

    def find(self, client_request_id: str, param_hash: str) -> Optional[dict]:
        row = self.db.execute(
            'SELECT job_id,state FROM audio_jobs WHERE client_request_id=? AND param_hash=?',
            (str(client_request_id), str(param_hash))).fetchone()
        if row is None:
            return None
        return {'job_id': row[0], 'state': row[1]}

    def set_asset(self, job_id: str, asset_id: str, *, provider: str = '') -> None:
        self.db.execute('UPDATE audio_job_payloads SET asset_id=?, '
                        'provider=COALESCE(NULLIF(?,\'\'),provider) WHERE job_id=?',
                        (str(asset_id or ''), str(provider or ''), str(job_id)))
        self.db.commit()

    # ── 查询 ────────────────────────────────────────────────────────────────
    def list_recent(self, limit: int = 50, *, subject_domain: Optional[str] = None) -> list[dict]:
        sql = ('SELECT job_id,subject_domain,mode,state,provider,phase_started,error_code,'
               'cost_reserved,cost_actual,created_at,updated_at FROM audio_jobs')
        params: list = []
        if subject_domain:
            sql += ' WHERE subject_domain=?'
            params.append(str(subject_domain))
        sql += ' ORDER BY created_at DESC LIMIT ?'
        params.append(int(limit))
        rows = self.db.execute(sql, params).fetchall()
        return [{'job_id': r[0], 'subject_domain': r[1], 'mode': r[2], 'state': r[3],
                 'provider': r[4] or '', 'phase_started': r[5] or '',
                 'error_code': r[6] or '', 'cost_reserved': float(r[7] or 0),
                 'cost_actual': float(r[8] or 0), 'created_at': float(r[9] or 0),
                 'updated_at': float(r[10] or 0)} for r in rows]

    def active_count(self, subject_domain: Optional[str] = None) -> int:
        sql = ("SELECT COUNT(*) FROM audio_jobs WHERE state NOT IN (%s)"
               % ','.join('?' * len(TERMINAL_STATES)))
        params: list = list(TERMINAL_STATES)
        if subject_domain:
            sql += ' AND subject_domain=?'
            params.append(str(subject_domain))
        return int(self.db.execute(sql, params).fetchone()[0])

    def state_counts(self) -> dict:
        return {row[0]: int(row[1]) for row in self.db.execute(
            'SELECT state,COUNT(*) FROM audio_jobs GROUP BY state').fetchall()}

    def queued_requests(self) -> list[tuple[dict, AudioJobRequest]]:
        """重启后需要重新入队的作业（只含 queued，绝不含 unknown）。"""
        rows = self.db.execute(
            "SELECT j.job_id,p.request_json FROM audio_jobs j JOIN audio_job_payloads p "
            "ON p.job_id=j.job_id WHERE j.state='queued' ORDER BY j.created_at").fetchall()
        out = []
        for job_id, raw in rows:
            try:
                request = AudioJobRequest(**json.loads(raw))
            except Exception:
                continue
            out.append(({'job_id': job_id}, request))
        return out

    # ── 重启恢复 ────────────────────────────────────────────────────────────
    def reconcile_restart(self, now: float = 0.0) -> int:
        """重启后把 running/uncertain 标成待核实（不自动重跑、不自动重发）。

        - preparing/running/postprocessing：外部可能已执行 → unknown；
        - cancel_requested：用户已取消 → cancelled；
        - queued 保持不变，由调度器决定是否重新入队（不重放外部调用）。
        返回受影响行数（含把“发送中”的交付标成 uncertain）。
        """
        timestamp = float(now) if now else time.time()
        affected = 0
        for states, target in ((('preparing', 'running', 'postprocessing'), JobState.UNKNOWN),
                               (('cancel_requested',), JobState.CANCELLED)):
            placeholders = ','.join('?' * len(states))
            cursor = self.db.execute(
                f'UPDATE audio_jobs SET state=?, updated_at=? WHERE state IN ({placeholders})',
                (target.value, timestamp, *states))
            affected += int(cursor.rowcount or 0)
        try:
            cursor = self.db.execute(
                "UPDATE audio_deliveries SET state='uncertain', fail_reason=? WHERE state='sending'",
                ('上次发送期间进程中断，结果待核实',))
            affected += int(cursor.rowcount or 0)
        except sqlite3.Error:
            pass
        self.db.commit()
        return affected
