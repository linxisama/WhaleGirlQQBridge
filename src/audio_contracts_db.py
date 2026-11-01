"""音频网关 SQLite 数据模型（规划书 1.5.2 的落地）。

只做“建表”这一件可独立验收的事：表结构与约束先定下来，各功能任务
（T03 资产 / T04 队列 / T15 Agent）往里读写，不用再各自发明 schema。

- 全部 `CREATE TABLE IF NOT EXISTS`，幂等、可重复调用，不影响现有 `voice_sends`。
- 音频二进制放文件系统（data/voice/…），这里只存元数据与必要摘要。
- 语音表与“机器人管理”旧表（pc_backend）完全分开，不复活旧宿主控制通道。
"""
from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_profiles (
    profile_id       TEXT PRIMARY KEY,
    persona_id       TEXT NOT NULL,
    provider_map     TEXT NOT NULL,          -- JSON：local/cloud 各引擎的音色映射
    model_version    TEXT DEFAULT '',
    weight_version   TEXT DEFAULT '',
    ref_asset_id     TEXT DEFAULT '',        -- 参考素材的内容哈希（见 audio_assets）
    ref_asset_hash   TEXT DEFAULT '',
    language         TEXT DEFAULT 'zh',
    licensed_use     TEXT DEFAULT '',
    allow_cloud      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audio_jobs (
    job_id          TEXT PRIMARY KEY,
    client_request_id TEXT NOT NULL,
    subject_domain  TEXT NOT NULL,           -- 可信主体域，非模型输出
    principal       TEXT NOT NULL,
    mode            TEXT NOT NULL,
    param_hash      TEXT NOT NULL,
    state           TEXT NOT NULL,
    provider        TEXT DEFAULT '',
    external_id     TEXT DEFAULT '',
    phase_started   TEXT DEFAULT '',        -- 最近观测到的阶段，不编造百分比
    error_code      TEXT DEFAULT '',
    cost_reserved   REAL DEFAULT 0,
    cost_actual     REAL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE(client_request_id, param_hash)
);
CREATE INDEX IF NOT EXISTS idx_audio_jobs_state ON audio_jobs(state, subject_domain);

CREATE TABLE IF NOT EXISTS audio_assets (
    audio_id         TEXT PRIMARY KEY,       -- 内容哈希（sha256）
    content_hash     TEXT NOT NULL,
    owner_domain     TEXT NOT NULL,
    source_job_id    TEXT DEFAULT '',
    mime             TEXT DEFAULT 'audio/wav',
    sample_rate      INTEGER DEFAULT 0,
    bits_per_sample  INTEGER DEFAULT 16,
    channels         INTEGER DEFAULT 1,
    duration_seconds REAL DEFAULT 0,
    size_bytes       INTEGER DEFAULT 0,
    master_of        TEXT DEFAULT '',        -- 母版/衍生关联
    licensed         INTEGER NOT NULL DEFAULT 0,
    retain_until     REAL DEFAULT 0,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audio_assets_owner ON audio_assets(owner_domain);

CREATE TABLE IF NOT EXISTS audio_deliveries (
    delivery_id       TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    audio_asset_ids   TEXT NOT NULL,         -- JSON 数组
    account_id        TEXT NOT NULL,
    kind              TEXT NOT NULL,         -- group / private
    target            TEXT NOT NULL,
    segment_index     INTEGER NOT NULL DEFAULT 0,
    request_id        TEXT NOT NULL,          -- 每段独立幂等键（1.6.2）
    state             TEXT NOT NULL,
    platform_message_id TEXT DEFAULT '',
    fail_reason       TEXT DEFAULT '',
    UNIQUE(request_id)
);

-- 本地 Agent 独立权限域（1.7）：与机器人管理表分开，不复用旧宿主口令。
CREATE TABLE IF NOT EXISTS agent_devices (
    device_id     TEXT PRIMARY KEY,
    public_key    TEXT NOT NULL,
    paired        INTEGER NOT NULL DEFAULT 0,
    capability_ver INTEGER NOT NULL DEFAULT 0,
    policy_ver    INTEGER NOT NULL DEFAULT 0,
    bound_account TEXT DEFAULT '',           -- 配对的机器人账号 + 主人私聊
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id          TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    action          TEXT NOT NULL,
    args_hash       TEXT NOT NULL,           -- 动作参数摘要（1.7.2 审批绑定）
    policy_version  INTEGER NOT NULL DEFAULT 1,
    nonce          TEXT NOT NULL,
    state           TEXT NOT NULL,
    result_status   TEXT DEFAULT '',
    created_at      REAL NOT NULL,
    UNIQUE(device_id, nonce)
);

CREATE TABLE IF NOT EXISTS agent_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    event       TEXT NOT NULL,
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_audit_job ON agent_audit(job_id, at);
"""


def initialize(db: sqlite3.Connection) -> None:
    """幂等地建立全部音频网关与本地 Agent 表。调用方负责 commit。"""
    db.executescript(_SCHEMA)
