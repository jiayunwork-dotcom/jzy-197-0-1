-- 选矿数据校正服务的 PostgreSQL 16 schema
CREATE TABLE IF NOT EXISTS circuits (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS circuit_versions (
    circuit_code TEXT NOT NULL REFERENCES circuits(code) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    topology     JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (circuit_code, version)
);

CREATE TABLE IF NOT EXISTS surveys (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circuit_code      TEXT NOT NULL REFERENCES circuits(code),
    topology_version  INTEGER NOT NULL,
    month             DATE NOT NULL,                 -- 考查所属月份（取每月1日）
    name              TEXT NOT NULL DEFAULT '',
    current_version   INTEGER NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (circuit_code, month)
);

CREATE TABLE IF NOT EXISTS survey_versions (
    survey_id         UUID NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    version           INTEGER NOT NULL,
    topology_version  INTEGER NOT NULL,
    data              JSONB NOT NULL,
    note              TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (survey_id, version)
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    survey_id         UUID NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    survey_version    INTEGER NOT NULL,
    topology_version  INTEGER NOT NULL,
    settings_hash     CHAR(32) NOT NULL,
    settings          JSONB NOT NULL,
    status            TEXT NOT NULL,                  -- converged | not_converged | unobservable
    result            JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (survey_id, survey_version, topology_version, settings_hash)
);

CREATE INDEX IF NOT EXISTS idx_runs_survey ON reconciliation_runs(survey_id);
