-- 007_place_staging — P15 raw source-ingestion staging (canonical DDL).
--
-- Boundary: raw provider records land HERE and only here — never in
-- `businesses` (P16 owns canonical LegalEntity/Business/Place). Every
-- record keeps its source-native identity, verbatim payload, observation
-- timestamps, P14 administrative anchor and ingest-run lineage so runs
-- are auditable, resumable and idempotent.

-- One row per ingestion job execution (a scrape run, a PBF import, a
-- corpus backfill). Checkpoints make runs resumable.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider        TEXT NOT NULL,           -- google_maps|osm|web_corpus|...
    status          TEXT NOT NULL DEFAULT 'running', -- running|done|failed|aborted
    parameters      JSONB NOT NULL DEFAULT '{}',     -- province, category, grid cell...
    source_version  TEXT,                    -- upstream dataset version / adapter version
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    records_seen    INTEGER NOT NULL DEFAULT 0,
    records_new     INTEGER NOT NULL DEFAULT 0,
    records_changed INTEGER NOT NULL DEFAULT 0,
    records_unchanged INTEGER NOT NULL DEFAULT 0,
    records_invalid INTEGER NOT NULL DEFAULT 0,
    records_failed  INTEGER NOT NULL DEFAULT 0,
    cursor          TEXT,                    -- provider-native resume token
    checkpoint      JSONB NOT NULL DEFAULT '{}', -- arbitrary adapter progress state
    error_summary   JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_provider
    ON ingestion_runs (provider, started_at DESC);

-- Raw staging: one row per (provider, external_id) observation of a place.
-- Records without a usable external id stay unique-by-nothing (plain
-- UNIQUE treats NULLs as distinct) and rely on content_hash + P16.
CREATE TABLE IF NOT EXISTS place_source_records (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider         TEXT NOT NULL,
    external_id      TEXT,
    external_id_type TEXT,               -- google_place_id|google_cid|osm_node|doc_id|tax_code

    source_url       TEXT,
    raw_name         TEXT,
    raw_address      TEXT,
    raw_phone        TEXT,
    normalized_phone TEXT,
    raw_website      TEXT,
    canonical_website TEXT,
    raw_category     TEXT,
    raw_hours        JSONB,

    lat              DOUBLE PRECISION,
    lon              DOUBLE PRECISION,
    location         GEOMETRY(Point, 4326),

    raw_payload      JSONB,              -- verbatim provider record (small)
    raw_payload_ref  TEXT,               -- MinIO key when payload exceeds inline cap

    observed_at      TIMESTAMPTZ NOT NULL,
    fetched_at       TIMESTAMPTZ,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    ingestion_run_id BIGINT REFERENCES ingestion_runs(run_id),

    -- P14 administrative anchor: never a name key — the internal unit id.
    admin_unit_id       BIGINT REFERENCES administrative_units(unit_id),
    resolver_version    TEXT,
    resolution_confidence REAL,

    content_hash     TEXT,               -- sha256 of canonical normalized fields
    record_status    TEXT NOT NULL DEFAULT 'valid', -- valid|quarantined|superseded

    UNIQUE (provider, external_id)
);

CREATE INDEX IF NOT EXISTS idx_place_source_records_location
    ON place_source_records USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_place_source_records_admin
    ON place_source_records (admin_unit_id);
CREATE INDEX IF NOT EXISTS idx_place_source_records_run
    ON place_source_records (ingestion_run_id);
CREATE INDEX IF NOT EXISTS idx_place_source_records_status
    ON place_source_records (record_status);
CREATE INDEX IF NOT EXISTS idx_place_source_records_name
    ON place_source_records (raw_name);

-- Idempotency for id-less records (external_id is optional per source):
-- same re-imported payload collapses on (provider, content_hash).
CREATE UNIQUE INDEX IF NOT EXISTS idx_psr_hash_noid
    ON place_source_records (provider, content_hash)
    WHERE external_id IS NULL;

-- Dead letter queue: one bad record must never kill a 500k-record import.
CREATE TABLE IF NOT EXISTS place_source_errors (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id BIGINT REFERENCES ingestion_runs(run_id),
    provider         TEXT NOT NULL,
    reason           TEXT NOT NULL,      -- validation/parse failure class
    detail           TEXT,
    payload          JSONB,              -- rejected record, verbatim when parseable
    retryable        BOOLEAN NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_place_source_errors_run
    ON place_source_errors (ingestion_run_id);

-- Per-source metadata: authority hints + storage/refresh policy. Schema
-- prepared now so P16 confidence resolution needs no re-migration; P15
-- does not score with it.
CREATE TABLE IF NOT EXISTS source_policies (
    provider        TEXT PRIMARY KEY,
    kind            TEXT NOT NULL DEFAULT 'discovery', -- discovery|authority|enrichment
    storage_policy  TEXT NOT NULL DEFAULT 'raw_staging',
    authority       JSONB NOT NULL DEFAULT '{}',  -- field → weight hints
    refresh         JSONB NOT NULL DEFAULT '{}',  -- default_days etc.
    usage           JSONB NOT NULL DEFAULT '{}',  -- realtime/batch flags
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO source_policies (provider, kind, authority, refresh, usage)
VALUES
    ('google_maps', 'discovery',
     '{"location":0.90,"opening_hours":0.80,"legal_status":0.20}',
     '{"default_days":30}',
     '{"realtime":false,"batch":true}'),
    ('osm', 'discovery',
     '{"location":0.90,"opening_hours":0.60,"legal_status":0.10}',
     '{"default_days":90}',
     '{"realtime":false,"batch":true}'),
    ('web_corpus', 'enrichment',
     '{"location":0.50,"opening_hours":0.40,"legal_status":0.40}',
     '{"default_days":60}',
     '{"realtime":false,"batch":true}')
ON CONFLICT (provider) DO NOTHING;
