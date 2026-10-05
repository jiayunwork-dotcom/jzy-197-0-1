-- 数据校正服务 PostgreSQL 16 schema
-- 所有拓扑/数据版本均为只追加（immutable）：修订总是插入新版本行，
-- 旧考查引用的旧版本结果永远可复现。

CREATE TABLE IF NOT EXISTS circuits (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS topology_versions (
    id              TEXT PRIMARY KEY,
    circuit_id      TEXT NOT NULL REFERENCES circuits(id),
    version_no      INTEGER NOT NULL,
    definition      JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (circuit_id, version_no)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id                      TEXT PRIMARY KEY,
    circuit_id              TEXT NOT NULL REFERENCES circuits(id),
    name                    TEXT NOT NULL,
    period                  TEXT,                       -- 月份标签，如 2026-09
    current_topology_id     TEXT NOT NULL REFERENCES topology_versions(id),
    current_version_id      TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_campaigns_circuit_period ON campaigns(circuit_id, period);

CREATE TABLE IF NOT EXISTS campaign_versions (
    id              TEXT PRIMARY KEY,
    campaign_id     TEXT NOT NULL REFERENCES campaigns(id),
    version_no      INTEGER NOT NULL,
    parent_id       TEXT REFERENCES campaign_versions(id),  -- 修订所基于的版本
    dataset         JSONB NOT NULL,
    note            TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, version_no)
);

-- 校正结果绑定 (数据版本, 设定指纹)。同版本同设定重复提交直接返回已有结果。
CREATE TABLE IF NOT EXISTS recon_runs (
    id                  TEXT PRIMARY KEY,
    campaign_version_id TEXT NOT NULL REFERENCES campaign_versions(id),
    settings_hash       TEXT NOT NULL,
    settings_json       JSONB NOT NULL,
    input_hash          TEXT NOT NULL,
    result              JSONB NOT NULL,
    converged           BOOLEAN NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_version_id, settings_hash)
);
CREATE INDEX IF NOT EXISTS idx_runs_version ON recon_runs(campaign_version_id);
