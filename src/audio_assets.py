"""T03：受管理音频资产（母版/衍生、哈希、来源、授权元数据、有效期清理）。

与旧 voice_service.store 的区别：这里要“授权引用 + 母版/衍生分离 + 按所有者隔离”，
旧接口仍保留（新旧共存，1.5.1）。

约束（T03 验收）：
- 拒绝伪扩展名、超限、损坏文件及越权引用（path 必须落在数据目录内）。
- 同内容可去重存储，但每个引用仍单独鉴权——不能靠“猜到哈希”读别家会话素材。
- 清理不删除“被作业引用”或“用户保留”的母版。
- 本地素材默认不出网；云处理另按用途/供应商授权（1.8）。

实现补充（2026-10-20）：
- 内容表 ``audio_assets`` 仍以内容哈希为主键；单独新增 ``audio_asset_refs``
  记录“谁引用过这份内容”。同内容去重只共享文件，不共享读取权。
- 写文件走临时文件 + ``os.replace``，半截文件不会进入资产库。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import wave
from pathlib import Path
from typing import Optional

from .audio_contracts import ErrorCode, reject
from .audio_contracts_db import initialize as initialize_schema
from .audio_postprocess import Transcoder, read_wav

MAX_AUDIO_BYTES = 8 * 1024 * 1024
_HEX64 = re.compile(r'^[a-f0-9]{64}$')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_asset_refs (
    audio_id       TEXT NOT NULL,
    owner_domain   TEXT NOT NULL,
    source_job_id  TEXT DEFAULT '',
    retained       INTEGER NOT NULL DEFAULT 0,   -- 1 = 用户明确保留，不自动清理
    retain_until   REAL NOT NULL DEFAULT 0,      -- 0 = 不设到期
    licensed       INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    PRIMARY KEY (audio_id, owner_domain)
);
CREATE INDEX IF NOT EXISTS idx_audio_asset_refs_owner ON audio_asset_refs(owner_domain);
"""


def _now(now: float = 0.0) -> float:
    return float(now) if now else time.time()


def validate_wav(audio: bytes) -> float:
    """复用旧接口口径：PCM WAV、≤8MB、≤120 秒、单/双声道。返回时长。"""
    return read_wav(audio)['duration']


class AudioAssetStore:
    def __init__(self, root: Path, db: sqlite3.Connection,
                 transcoder: Optional[Transcoder] = None):
        self.root = Path(root)
        self.directory = self.root / 'data' / 'voice' / 'assets'
        self.db = db
        self.transcoder = transcoder or Transcoder()
        initialize_schema(db)
        db.executescript(_SCHEMA)
        db.commit()

    # ── 入库 ────────────────────────────────────────────────────────────────
    def store(self, audio: bytes, *, owner_domain: str, source_job_id: str = '',
              media_type: str = 'audio/wav', licensed: bool = False,
              master_of: str = '', now: float = 0.0, retained: bool = False,
              retain_seconds: float = 0.0) -> dict:
        """校验 + 哈希去重 + 入库元数据。返回 {audio_id, duration, size, deduped}。

        去重只共享内容文件；调用方每次都会得到自己的引用记录，跨主体读不到。
        """
        if not isinstance(owner_domain, str) or not owner_domain.strip():
            reject(ErrorCode.UNAUTHORIZED, '缺少资产所有者域，拒绝入库')
        if not isinstance(audio, (bytes, bytearray)) or not audio:
            reject(ErrorCode.ASSET_INVALID, '音频为空')
        if len(audio) > MAX_AUDIO_BYTES:
            reject(ErrorCode.ASSET_INVALID, '音频超过 8MB 上限')
        if media_type not in ('audio/wav',):
            reject(ErrorCode.ASSET_INVALID, '资产库只接收 PCM WAV（其它格式先转码）')
        audio = bytes(audio)
        info = read_wav(audio)
        duration = info['duration']
        if master_of and not _HEX64.match(str(master_of)):
            reject(ErrorCode.ASSET_INVALID, '母版编号无效')
        if master_of == hashlib.sha256(audio).hexdigest():
            reject(ErrorCode.ASSET_INVALID, '衍生文件不能与母版内容相同')
        audio_id = hashlib.sha256(audio).hexdigest()
        timestamp = _now(now)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (audio_id + '.wav')

        row = self.db.execute(
            'SELECT audio_id,size_bytes FROM audio_assets WHERE audio_id=?', (audio_id,)).fetchone()
        deduped = row is not None
        if deduped:
            if not self._file_intact(path, audio_id):
                self._write_atomic(path, audio)
        else:
            self._write_atomic(path, audio)
            self.db.execute(
                'INSERT INTO audio_assets (audio_id,content_hash,owner_domain,source_job_id,mime,'
                'sample_rate,bits_per_sample,channels,duration_seconds,size_bytes,master_of,'
                'licensed,retain_until,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (audio_id, audio_id, str(owner_domain), str(source_job_id or ''), 'audio/wav',
                 info['sample_rate'], info['bits'], info['channels'], float(duration),
                 len(audio), str(master_of or ''), 1 if licensed else 0, 0.0, timestamp))
        retain_until = timestamp + float(retain_seconds) if retain_seconds and retain_seconds > 0 else 0.0
        self.db.execute(
            'INSERT INTO audio_asset_refs (audio_id,owner_domain,source_job_id,retained,'
            'retain_until,licensed,created_at) VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(audio_id,owner_domain) DO UPDATE SET '
            'source_job_id=excluded.source_job_id, retained=MAX(retained,excluded.retained), '
            'retain_until=CASE WHEN excluded.retain_until>0 THEN excluded.retain_until '
            'ELSE retain_until END, licensed=MAX(licensed,excluded.licensed)',
            (audio_id, str(owner_domain), str(source_job_id or ''),
             1 if retained else 0, retain_until, 1 if licensed else 0, timestamp))
        self.db.commit()
        meta = self._probe(path, audio_id)
        return {'audio_id': audio_id, 'media_type': 'audio/wav', 'bytes': len(audio),
                'duration_seconds': round(duration, 3), 'deduped': deduped,
                'content_hash': audio_id, 'master_of': str(master_of or ''),
                'sample_rate': meta.get('sample_rate', 0),
                'channels': meta.get('channels', 1)}

    # ── 读取 ────────────────────────────────────────────────────────────────
    def path(self, audio_id: str, *, owner_domain: str) -> Path:
        """取受管理文件路径；越权引用/越目录/不存在都要明确拒绝（T03 验收）。"""
        audio_id = str(audio_id or '')
        if not _HEX64.match(audio_id):
            reject(ErrorCode.ASSET_INVALID, '资产编号须为 64 位十六进制内容哈希')
        if not owner_domain:
            reject(ErrorCode.UNAUTHORIZED, '缺少资产所有者域，拒绝读取')
        row = self.db.execute(
            'SELECT 1 FROM audio_asset_refs WHERE audio_id=? AND owner_domain=?',
            (audio_id, str(owner_domain))).fetchone()
        if not row:
            reject(ErrorCode.POLICY_DENIED, '无权读取该资产（不属于当前主体）')
        directory = self.directory.resolve()
        path = (self.directory / (audio_id + '.wav')).resolve()
        if not path.is_relative_to(directory):
            reject(ErrorCode.ASSET_INVALID, '资产路径越出数据目录')
        if not path.is_file():
            reject(ErrorCode.ASSET_INVALID, '资产文件不存在或已被清理')
        if not self._file_intact(path, audio_id):
            reject(ErrorCode.ASSET_INVALID, '资产文件内容与登记哈希不一致')
        return path

    def meta(self, audio_id: str, *, owner_domain: str) -> dict:
        """给 API 的鉴权元数据；先校验引用，再读内容表，不泄露别家主体。"""
        path = self.path(audio_id, owner_domain=owner_domain)
        row = self.db.execute(
            'SELECT mime,sample_rate,channels,duration_seconds,size_bytes,master_of,licensed '
            'FROM audio_assets WHERE audio_id=?', (audio_id,)).fetchone()
        if not row:
            reject(ErrorCode.ASSET_INVALID, '资产登记不存在')
        return {'audio_id': audio_id, 'media_type': row[0] or 'audio/wav',
                'sample_rate': int(row[1] or 0), 'channels': int(row[2] or 1),
                'duration_seconds': round(float(row[3] or 0.0), 3),
                'size_bytes': int(row[4] or path.stat().st_size),
                'content_hash': audio_id, 'owner_domain': str(owner_domain),
                'master_of': row[5] or '', 'licensed': bool(row[6]),
                'path': str(path)}

    def list_for_owner(self, owner_domain: str, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            'SELECT r.audio_id, a.mime, a.duration_seconds, a.size_bytes, a.master_of, r.retained '
            'FROM audio_asset_refs r JOIN audio_assets a ON a.audio_id=r.audio_id '
            'WHERE r.owner_domain=? ORDER BY r.created_at DESC LIMIT ?',
            (str(owner_domain), int(limit))).fetchall()
        return [{'audio_id': r[0], 'media_type': r[1] or 'audio/wav',
                 'duration_seconds': round(float(r[2] or 0.0), 3),
                 'size_bytes': int(r[3] or 0), 'master_of': r[4] or '',
                 'retained': bool(r[5])} for r in rows]

    # ── 衍生 ────────────────────────────────────────────────────────────────
    def derived(self, master_id: str, *, owner_domain: str, spec: dict,
                now: float = 0.0) -> dict:
        """由母版生成独立衍生文件（如 QQ 单声道 24k/16bit），不覆盖母版（1.3.4）。"""
        master = self.path(master_id, owner_domain=owner_domain)
        spec = dict(spec or {})
        sample_rate = int(spec.get('sample_rate', 24000))
        channels = int(spec.get('channels', 1))
        bits = int(spec.get('bits_per_sample', 16))
        media_type = str(spec.get('media_type', 'audio/wav'))
        if media_type != 'audio/wav':
            reject(ErrorCode.TRANSCODE_FAILED, '衍生资产目前必须是 PCM WAV')
        temp = self.directory / ('.derived-' + uuid.uuid4().hex + '.wav')
        try:
            self.transcoder.transcode(master, temp, sample_rate=sample_rate,
                                      channels=channels, bits=bits, media_type='wav')
            audio = temp.read_bytes()
            result = self.store(audio, owner_domain=owner_domain,
                                source_job_id=spec.get('source_job_id', ''),
                                master_of=master_id, now=now)
            result['spec'] = {'sample_rate': sample_rate, 'channels': channels,
                              'bits_per_sample': bits, 'media_type': 'audio/wav'}
            return result
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    # ── 清理 ────────────────────────────────────────────────────────────────
    def cleanup(self, now: float = 0.0, min_referenced_jobs: int = 0) -> int:
        """清理到期且无引用的资产；被作业引用或用户保留的母版不删（T03 验收）。

        - ``retain_until=0``（默认）与 ``retained=1`` 永不自动删除；
        - 仍被衍生文件、发送计划或作业引用的内容不删；
        - 只删除“所有引用都已到期”的内容，删记录与文件。
        """
        timestamp = _now(now)
        expired = self.db.execute(
            'SELECT audio_id, owner_domain FROM audio_asset_refs '
            'WHERE retained=0 AND retain_until>0 AND retain_until<=?', (timestamp,)).fetchall()
        removed = 0
        for audio_id, owner_domain in expired:
            # 被作业/衍生/发送计划引用的内容整体保留（连引用一起保留），避免“文件在但读不到”。
            if self._referenced(audio_id):
                continue
            # 到期的是“这条引用”，先删引用；内容是否删除看是否还有别的有效引用。
            self.db.execute('DELETE FROM audio_asset_refs WHERE audio_id=? AND owner_domain=?',
                            (audio_id, owner_domain))
            self.db.commit()
            still = self.db.execute('SELECT COUNT(*) FROM audio_asset_refs WHERE audio_id=?',
                                    (audio_id,)).fetchone()[0]
            if still:
                continue
            if self._referenced(audio_id):
                continue
            self.db.execute('DELETE FROM audio_assets WHERE audio_id=?', (audio_id,))
            self.db.commit()
            try:
                (self.directory / (audio_id + '.wav')).unlink(missing_ok=True)
            except OSError:
                pass
            removed += 1
        return removed

    def _referenced(self, audio_id: str) -> bool:
        """母版/衍生/发送计划/作业任一引用都算“不能删”。"""
        if self.db.execute('SELECT 1 FROM audio_assets WHERE master_of=? LIMIT 1',
                           (audio_id,)).fetchone():
            return True
        if self.db.execute("SELECT 1 FROM audio_assets WHERE audio_id=? AND source_job_id<>'' "
                           "AND source_job_id IN (SELECT job_id FROM audio_jobs) LIMIT 1",
                           (audio_id,)).fetchone():
            return True
        try:
            rows = self.db.execute('SELECT audio_asset_ids FROM audio_deliveries').fetchall()
        except sqlite3.Error:
            return False
        for (raw,) in rows:
            try:
                ids = json.loads(raw or '[]')
            except (TypeError, ValueError):
                continue
            if isinstance(ids, list) and audio_id in ids:
                return True
        return False

    # ── 内部 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _write_atomic(path: Path, audio: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(audio)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _file_intact(path: Path, audio_id: str) -> bool:
        try:
            if not path.is_file() or path.stat().st_size > MAX_AUDIO_BYTES:
                return False
            return hashlib.sha256(path.read_bytes()).hexdigest() == audio_id
        except OSError:
            return False

    @staticmethod
    def _probe(path: Path, audio_id: str) -> dict:
        try:
            info = read_wav(path.read_bytes())
            return {'sample_rate': info['sample_rate'], 'channels': info['channels'],
                    'duration': info['duration']}
        except Exception:
            return {}
