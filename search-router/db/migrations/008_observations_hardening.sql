-- 008_observations_hardening — P15.1 ingestion hardening.
--
-- Splits the two jobs `content_hash` was doing: `identity_hash` keys
-- id-less dedup (stable across mutable-field drift), `observation_hash`
-- detects change (covers every tracked field incl. opening hours).
-- Adds an append-only observation log so re-ingestion never destroys
-- history or run lineage (place_source_records stays CURRENT STATE),
-- and records adapter vs upstream dataset versioning per run.

ALTER TABLE place_source_records
    ADD COLUMN IF NOT EXISTS identity_hash TEXT,
    ADD COLUMN IF NOT EXISTS observation_hash TEXT;

-- Existing rows: the old content_hash played both roles.
UPDATE place_source_records
SET identity_hash    = COALESCE(identity_hash, content_hash),
    observation_hash = COALESCE(observation_hash, content_hash);

-- Id-less dedup now keys on identity_hash (not the full-content hash).
DROP INDEX IF EXISTS idx_psr_hash_noid;
CREATE UNIQUE INDEX IF NOT EXISTS idx_psr_identity_noid
    ON place_source_records (provider, identity_hash)
    WHERE external_id IS NULL;

-- Run lineage hardening: separate adapter version from upstream dataset
-- identity (sha256/size/snapshot timestamp) and record resume ancestry.
ALTER TABLE ingestion_runs
    ADD COLUMN IF NOT EXISTS adapter_version TEXT,
    ADD COLUMN IF NOT EXISTS source_dataset JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS resume_of BIGINT;

-- Append-only observation history — one row per (provider, source object,
-- run) sighting. Current state lives in place_source_records; this log
-- is the change-detection / freshness / audit foundation for P16/P18.
CREATE TABLE IF NOT EXISTS place_source_observations (
    observation_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider         TEXT NOT NULL,
    external_id      TEXT,
    external_id_type TEXT,
    identity_hash    TEXT,
    observation_hash TEXT NOT NULL,
    ingestion_run_id BIGINT REFERENCES ingestion_runs(run_id),
    observed_at      TIMESTAMPTZ NOT NULL,
    fetched_at       TIMESTAMPTZ,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_type      TEXT NOT NULL,      -- new|changed|unchanged
    admin_unit_id    BIGINT REFERENCES administrative_units(unit_id),
    raw_payload      JSONB,
    raw_payload_ref  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pso_identity
    ON place_source_observations (provider, external_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_pso_run
    ON place_source_observations (ingestion_run_id);
CREATE INDEX IF NOT EXISTS idx_pso_time
    ON place_source_observations (observed_at);
