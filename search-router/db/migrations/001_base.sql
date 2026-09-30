-- 001_base — Search-Hub Postgres base schema (P10) + PostGIS.
--
-- Mirrors db/init.sql (docker-entrypoint-initdb.d) so the migration runner
-- converges pre-existing volumes (where init.sql already ran) and fresh
-- volumes to the same baseline. All statements are idempotent.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    tier         TEXT NOT NULL DEFAULT 'free',   -- free | pro | internal
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT PRIMARY KEY,               -- uuid
    key_prefix   TEXT NOT NULL,                  -- e.g. dsa_live_Ab3x (lookup aid only)
    key_hash     TEXT NOT NULL UNIQUE,           -- sha256(full key) — never store plaintext
    tenant_id    TEXT NOT NULL REFERENCES tenants(tenant_id),
    name         TEXT NOT NULL DEFAULT '',
    scopes       TEXT[] NOT NULL DEFAULT '{}',   -- search:read, answer:use, research:use, ...
    rpm_limit    INTEGER NOT NULL DEFAULT 60,    -- requests/minute
    daily_quota  INTEGER NOT NULL DEFAULT 1000,  -- requests/day, -1 = unlimited
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys (tenant_id);

CREATE TABLE IF NOT EXISTS query_logs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id    TEXT,
    key_id       TEXT,
    endpoint     TEXT NOT NULL,
    mode         TEXT,
    status_code  INTEGER NOT NULL,
    latency_ms   INTEGER,
    query_hash   TEXT,                           -- sha256(query) — never store raw query
    result_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_query_logs_ts ON query_logs (ts);
CREATE INDEX IF NOT EXISTS idx_query_logs_tenant ON query_logs (tenant_id, ts);

CREATE TABLE IF NOT EXISTS usage_daily (
    day          DATE NOT NULL,
    tenant_id    TEXT NOT NULL,
    key_id       TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    requests     INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, tenant_id, key_id, endpoint)
);

CREATE TABLE IF NOT EXISTS domain_profiles (
    domain       TEXT PRIMARY KEY,
    trust_score  REAL NOT NULL DEFAULT 0.5,
    notes        TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crawl_frontier (
    url          TEXT PRIMARY KEY,
    domain       TEXT,
    status       TEXT NOT NULL DEFAULT 'queued', -- queued | fetching | done | failed
    priority     INTEGER NOT NULL DEFAULT 0,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    fetched_at   TIMESTAMPTZ
);
