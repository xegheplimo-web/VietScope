-- init.sql
-- Search-Hub Postgres schema (P10) — API keys, tenants, usage, query logs.
-- Mounted into hub-postgres:/docker-entrypoint-initdb.d/ (runs once on
-- first volume init). All objects are additive/idempotent.
--
-- Phase 1 (T2): hub-postgres runs postgis/postgis:16-3.5-alpine. These
-- extensions are created here for fresh volumes; existing volumes get them
-- via db/migrations/001_base.sql + the migrate runner (db/migrate.py).

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

-- Crawl frontier + domain profiles land in later phases; tables reserved
-- here so migrations stay additive.
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

-- 001_base.sql
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

-- 002_phase1_foundation.sql
-- 002_phase1_foundation — canonical storage foundation (Phase 1/T2).
--
-- Provenance + history model: sources own domains, documents are canonical
-- pages, document_snapshots are immutable raw captures (bytes live in MinIO,
-- keyed by storage_key). crawl_frontier gains recrawl scheduling columns.
-- administrative_units is the VN gazetteer base (34 provinces post-2025
-- merge, with aliases carrying historical names).
-- Additive only: no P10 table is altered destructively.

-- Domain-level provenance / trust registry.
CREATE TABLE IF NOT EXISTS sources (
    domain       TEXT PRIMARY KEY,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    trust_score  REAL NOT NULL DEFAULT 0.5,
    notes        TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Canonical crawled documents (one row per canonical URL).
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,            -- doc_<sha256(canonical_url)[:12]>
    canonical_url TEXT NOT NULL UNIQUE,
    domain        TEXT REFERENCES sources(domain),
    title         TEXT,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash  TEXT,                        -- sha256 of latest normalized content
    status        TEXT NOT NULL DEFAULT 'active', -- active | gone | blocked
    language      TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents (domain);
CREATE INDEX IF NOT EXISTS idx_documents_last_seen ON documents (last_seen);

-- Immutable per-fetch history; raw bytes are in MinIO under storage_key.
CREATE TABLE IF NOT EXISTS document_snapshots (
    snapshot_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id       TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    storage_key  TEXT NOT NULL,                -- MinIO object key in bucket raw
    content_hash TEXT,                         -- sha256 of raw bytes
    http_status  INTEGER,
    mime         TEXT,
    headers      JSONB NOT NULL DEFAULT '{}'::jsonb,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_snapshots_doc
    ON document_snapshots (doc_id, retrieved_at DESC);

-- Upgrade the reserved crawl_frontier table into a real recrawl frontier.
-- priority becomes REAL: FreshnessWorker.compute_priority returns 0..1
-- floats — an INTEGER column would truncate every score to 0.
ALTER TABLE crawl_frontier ALTER COLUMN priority TYPE REAL;
ALTER TABLE crawl_frontier
    ADD COLUMN IF NOT EXISTS canonical_url TEXT,
    ADD COLUMN IF NOT EXISTS discovered_from TEXT,
    ADD COLUMN IF NOT EXISTS depth INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS robots_allowed BOOLEAN,
    ADD COLUMN IF NOT EXISTS last_crawled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_crawl_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS change_rate REAL,
    ADD COLUMN IF NOT EXISTS etag TEXT,
    ADD COLUMN IF NOT EXISTS last_modified TEXT,
    ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_frontier_next_crawl
    ON crawl_frontier (next_crawl_at) WHERE status = 'queued';

-- Vietnam gazetteer base: administrative units + historical aliases.
CREATE TABLE IF NOT EXISTS administrative_units (
    unit_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,           -- official unit code
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,                  -- province | municipality | ward | ...
    parent_id  BIGINT REFERENCES administrative_units (unit_id),
    valid_from DATE,
    valid_to   DATE,                           -- NULL = currently valid
    geometry   geometry(Geometry, 4326)
);
CREATE INDEX IF NOT EXISTS idx_admin_units_parent ON administrative_units (parent_id);
CREATE INDEX IF NOT EXISTS idx_admin_units_geom
    ON administrative_units USING GIST (geometry);

CREATE TABLE IF NOT EXISTS administrative_aliases (
    alias_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    unit_id    BIGINT NOT NULL REFERENCES administrative_units (unit_id)
               ON DELETE CASCADE,
    alias      TEXT NOT NULL,
    valid_from DATE,
    valid_to   DATE
);
CREATE INDEX IF NOT EXISTS idx_admin_aliases_unit ON administrative_aliases (unit_id);
CREATE INDEX IF NOT EXISTS idx_admin_aliases_alias ON administrative_aliases (alias);

-- 003_businesses.sql
-- 003_businesses — local-business geo table (canonical DDL).
--
-- storage/business_store.py reads/writes this table via storage.pg_client.
-- Previously the DDL only existed in BusinessStore.ensure_schema(), which no
-- production path calls — a freshly provisioned hub-postgres had no table
-- and every query degraded to []. This migration is the single source of
-- truth; ensure_schema() was removed.
--
-- Data note: any DB where ensure_schema() ran creates the identical table —
-- IF NOT EXISTS makes this a no-op there. coords are DOUBLE PRECISION and
-- location is a PostGIS geography-capable GEOMETRY(Point,4326) (T2 types).

CREATE TABLE IF NOT EXISTS businesses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    category TEXT,
    address TEXT,
    phone TEXT,
    hours TEXT,
    rating REAL,
    price_level TEXT,
    website TEXT,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    location GEOMETRY(Point, 4326),
    description TEXT,
    source_url TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(lat, lon, name)
);

CREATE INDEX IF NOT EXISTS idx_businesses_location
    ON businesses USING GIST(location);

-- 004_frontier_lease.sql
-- 004_frontier_lease — claim lease columns on crawl_frontier.
--
-- FreshnessWorker.pop_batch stamps claimed_at + claim_token (one fresh
-- token per claim). A row stuck in 'fetching' past the lease — worker
-- crash, lost completion — becomes pop-eligible again, and the token lets
-- complete()/fail() ignore late writes from a superseded claim.
-- claimed_at NULL on legacy 'fetching' rows = already expired (reclaimed
-- on the next pop instead of being stuck forever).

ALTER TABLE crawl_frontier
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_token TEXT;

CREATE INDEX IF NOT EXISTS idx_frontier_stale_claims
    ON crawl_frontier (claimed_at) WHERE status = 'fetching';

-- 005_extraction.sql
-- 005_extraction — extraction + indexing lifecycle on documents (Phase 3/T3).
--
-- The crawler now extracts main text (Trafilatura) and feeds the index.
-- Fields the search/filter path queries directly become real columns;
-- the raw extractor payload and per-field provenance stay in metadata.
-- Additive only: existing rows get NULL extraction_status, which the
-- pipeline treats as "never extracted" → one extraction pass on the
-- next recrawl, no re-crawl required.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS main_text TEXT,
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS author TEXT,
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS site_name TEXT,
    ADD COLUMN IF NOT EXISTS word_count INTEGER,
    ADD COLUMN IF NOT EXISTS quality_score REAL,
    ADD COLUMN IF NOT EXISTS extraction_method TEXT,
    ADD COLUMN IF NOT EXISTS extraction_version TEXT,
    -- success | low_content | low_quality | empty | skipped_mime | error
    ADD COLUMN IF NOT EXISTS extraction_status TEXT,
    -- success | failed | skipped
    ADD COLUMN IF NOT EXISTS indexing_status TEXT,
    -- success | failed | skipped
    ADD COLUMN IF NOT EXISTS embedding_status TEXT,
    ADD COLUMN IF NOT EXISTS current_snapshot_id BIGINT
        REFERENCES document_snapshots (snapshot_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Retry/backfill scans: "which docs still need extraction/indexing".
CREATE INDEX IF NOT EXISTS idx_documents_extraction
    ON documents (extraction_status);
CREATE INDEX IF NOT EXISTS idx_documents_domain_lang
    ON documents (domain, language);

-- 006_admin_graph.sql
-- 006_admin_graph.sql — P14A: temporal Vietnamese administrative graph.
--
-- Turns the 002 gazetteer stubs into the canonical administrative layer:
-- versioned units (valid_from/valid_to/status), typed aliases, and an
-- old→new transition graph so historical addresses ("Yên Dũng, Bắc Giang",
-- "Quận Hoàn Kiếm, Hà Nội") resolve to the current post-2025 two-level
-- model (province → commune). Historical units are never deleted.
--
-- Code uniqueness moves from global to per-era: the 2025 reorganization
-- reassigned official codes (new Bắc Ninh = 24 = old Bắc Giang's code;
-- Lào Cai = 15, Quảng Trị = 44, Gia Lai = 52, Tây Ninh = 80,
-- Đồng Tháp = 82, An Giang = 91), so a global UNIQUE(code) collides.
--
-- Additive only: existing columns are extended, never narrowed.

ALTER TABLE administrative_units
    ADD COLUMN IF NOT EXISTS normalized_name TEXT,
    ADD COLUMN IF NOT EXISTS admin_level SMALLINT,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'current',
    ADD COLUMN IF NOT EXISTS source TEXT,
    ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
-- normalized_name: accent-folded short name without the type prefix
--   ("Phường Ba Đình" -> "ba dinh") — the resolver's primary index key.
-- admin_level: 1 province | 2 district | 3 commune (district exists only
--   in historical rows; the current model is two-level).
-- status: current | historical | proposed.

-- Drop the 002 global UNIQUE(code); enforce uniqueness per era instead.
-- valid_from may be NULL (units whose origin predates our snapshots), so
-- the unique index coalesces NULL to the epoch sentinel.
ALTER TABLE administrative_units
    DROP CONSTRAINT IF EXISTS administrative_units_code_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_units_code_era
    ON administrative_units (code, COALESCE(valid_from, DATE '0001-01-01'));
CREATE INDEX IF NOT EXISTS idx_admin_units_norm
    ON administrative_units (normalized_name);
CREATE INDEX IF NOT EXISTS idx_admin_units_level_status
    ON administrative_units (admin_level, status);

ALTER TABLE administrative_aliases
    ADD COLUMN IF NOT EXISTS normalized_alias TEXT,
    ADD COLUMN IF NOT EXISTS alias_type TEXT NOT NULL DEFAULT 'alternate';
-- normalized_alias: accent-folded alias for lookup without diacritics.
-- alias_type: official | historical | abbreviation | alternate | english.
CREATE INDEX IF NOT EXISTS idx_admin_aliases_norm
    ON administrative_aliases (normalized_alias);
-- Idempotent seeding: one row per (unit, folded alias, era start).
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_aliases_dedupe
    ON administrative_aliases
    (unit_id, COALESCE(normalized_alias, ''),
     COALESCE(valid_from, DATE '0001-01-01'));

-- Old→new transition graph. One row per (from, to, type) edge; partial
-- merges and splits are many-edge relationships.
CREATE TABLE IF NOT EXISTS administrative_relations (
    relation_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_unit_id   BIGINT NOT NULL REFERENCES administrative_units (unit_id),
    to_unit_id     BIGINT NOT NULL REFERENCES administrative_units (unit_id),
    relation_type  TEXT NOT NULL,
    effective_date DATE,
    source         TEXT,
    UNIQUE (from_unit_id, to_unit_id, relation_type)
);
-- relation_type: renamed_to | merged_into | split_into | replaced_by |
--                boundary_changed (partial contribution).
-- District-level edges are derived: a historical district's constituent
-- communes map forward, so the district inherits split_into edges to the
-- new communes its territory became.
CREATE INDEX IF NOT EXISTS idx_admin_rel_from
    ON administrative_relations (from_unit_id);
CREATE INDEX IF NOT EXISTS idx_admin_rel_to
    ON administrative_relations (to_unit_id);

-- Businesses reference a canonical admin id instead of treating address
-- name strings as authoritative geography.
ALTER TABLE businesses
    ADD COLUMN IF NOT EXISTS admin_unit_id BIGINT
        REFERENCES administrative_units (unit_id);
CREATE INDEX IF NOT EXISTS idx_businesses_admin
    ON businesses (admin_unit_id);

-- 007_place_staging.sql
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

-- 008_observations_hardening.sql
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

-- 009_identity_rehash.sql
-- 009_identity_rehash — recompute identity_hash with the P15.1 algorithm.
--
-- 008 backfilled identity_hash = content_hash, but the v2 identity canon
-- differs: {"e","p"} for rows with external_id, {"a","lat","lon","n","p"}
-- for id-less rows (vs the old all-fields content hash). Migrated rows
-- would therefore never conflict on (provider, identity_hash) and would
-- duplicate instead of updating on the next ingest. Recompute in SQL,
-- mirroring RawPlaceRecord.identity_hash():
--   sha256( json.dumps(canon, sort_keys=True, ensure_ascii=False) )
-- → keys sorted, separators ", " and ": ", floats via shortest
-- round-trip with a trailing ".0" for integral values.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION _p151_json_flt(v FLOAT8)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN v IS NULL THEN 'null'
        ELSE regexp_replace(round(v::numeric, 6)::float8::text, '^(-?\d+)$', '\1.0')
    END
$$;

CREATE OR REPLACE FUNCTION _p151_identity_canon(
    provider TEXT, external_id TEXT, raw_name TEXT, raw_address TEXT,
    lat FLOAT8, lon FLOAT8
) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN external_id IS NOT NULL THEN
            format('{"e": %s, "p": %s}', to_json(external_id::text), to_json(provider))
        ELSE
            format(
                '{"a": %s, "lat": %s, "lon": %s, "n": %s, "p": %s}',
                to_json(COALESCE(btrim(lower(raw_address)), '')),
                _p151_json_flt(lat),
                _p151_json_flt(lon),
                to_json(COALESCE(btrim(lower(raw_name)), '')),
                to_json(provider)
            )
    END
$$;

UPDATE place_source_records
SET identity_hash = encode(
    digest(
        _p151_identity_canon(provider, external_id, raw_name, raw_address, lat, lon),
        'sha256'
    ),
    'hex'
);

DROP FUNCTION _p151_identity_canon(TEXT, TEXT, TEXT, TEXT, FLOAT8, FLOAT8);
DROP FUNCTION _p151_json_flt(FLOAT8);

-- 010_canonical_graph.sql
-- 010_canonical_graph — P16 entity graph over the P15 raw staging layer.
--
-- place_source_records/observations stay the immutable RAW side. Resolution
-- produces the canonical side: LegalEntity → Business → Place, with
-- per-field provenance back to contributing source records. The legacy
-- `businesses` table (003) is untouched — it remains the query-time
-- compatibility layer until P17 serving migrates.

-- ── legal entities (gov-registry backed; empty until gov adapters land) ──
CREATE TABLE IF NOT EXISTS legal_entities (
    entity_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legal_name      TEXT NOT NULL,
    normalized_name TEXT,
    tax_code        TEXT,                 -- MST — the strong legal key
    registered_address TEXT,
    status          TEXT NOT NULL DEFAULT 'active',  -- active|dissolved|suspended
    source_record_id BIGINT,              -- place_source_records.id that introduced it
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_legal_entities_tax
    ON legal_entities (tax_code) WHERE tax_code IS NOT NULL;

-- ── canonical businesses (the operating entity a place belongs to) ────────
CREATE TABLE IF NOT EXISTS canonical_businesses (
    business_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    display_name    TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    legal_entity_id BIGINT REFERENCES legal_entities(entity_id),
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cbiz_name ON canonical_businesses (normalized_name);

-- ── canonical places (physical locations — the merge target) ──────────────
CREATE TABLE IF NOT EXISTS canonical_places (
    place_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    business_id     BIGINT REFERENCES canonical_businesses(business_id),

    canonical_name  TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    canonical_category TEXT,              -- ontology bucket, not provider vocab
    address         TEXT,
    normalized_address TEXT,
    phone           TEXT,
    website         TEXT,
    opening_hours   JSONB,

    lat             DOUBLE PRECISION,
    lon             DOUBLE PRECISION,
    location        GEOMETRY(Point, 4326),

    admin_unit_id   BIGINT REFERENCES administrative_units(unit_id),
    status          TEXT NOT NULL DEFAULT 'open',   -- open|closed|unknown
    confidence      REAL NOT NULL DEFAULT 0,        -- field-weighted resolution confidence

    source_count    INT NOT NULL DEFAULT 1,         -- distinct providers contributing
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolution_run_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cplaces_location
    ON canonical_places USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_cplaces_admin ON canonical_places (admin_unit_id);
CREATE INDEX IF NOT EXISTS idx_cplaces_name ON canonical_places (normalized_name);
CREATE INDEX IF NOT EXISTS idx_cplaces_business ON canonical_places (business_id);
CREATE INDEX IF NOT EXISTS idx_cplaces_category ON canonical_places (canonical_category);

-- ── place ↔ source-record lineage (which sightings compose this place) ────
CREATE TABLE IF NOT EXISTS place_sources (
    place_id        BIGINT NOT NULL REFERENCES canonical_places(place_id),
    source_record_id BIGINT NOT NULL REFERENCES place_source_records(id),
    provider        TEXT NOT NULL,
    external_id     TEXT,
    linked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolution_run_id BIGINT,
    PRIMARY KEY (place_id, source_record_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_place_sources_ext
    ON place_sources (provider, external_id) WHERE external_id IS NOT NULL;
-- a staged record belongs to exactly one canonical place
CREATE UNIQUE INDEX IF NOT EXISTS idx_place_sources_record
    ON place_sources (source_record_id);

-- ── field-level provenance (per field: candidate values + chosen winner) ──
CREATE TABLE IF NOT EXISTS place_field_provenance (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    place_id        BIGINT NOT NULL REFERENCES canonical_places(place_id),
    field           TEXT NOT NULL,        -- name|phone|website|category|hours|address|location
    source_record_id BIGINT NOT NULL REFERENCES place_source_records(id),
    provider        TEXT NOT NULL,
    value           JSONB NOT NULL,
    weight          REAL NOT NULL DEFAULT 0, -- authority × recency × corroboration
    observed_at     TIMESTAMPTZ,
    chosen          BOOLEAN NOT NULL DEFAULT false,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (place_id, field, source_record_id)
);
CREATE INDEX IF NOT EXISTS idx_pfp_place_field
    ON place_field_provenance (place_id, field);
CREATE INDEX IF NOT EXISTS idx_pfp_chosen
    ON place_field_provenance (place_id, field) WHERE chosen;

-- ── resolution batch runs (mirror ingestion_runs bookkeeping) ─────────────
CREATE TABLE IF NOT EXISTS resolution_runs (
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'running',  -- running|done|failed|aborted
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    parameters      JSONB NOT NULL DEFAULT '{}',
    resolver_version TEXT,
    records_scanned INT NOT NULL DEFAULT 0,
    pairs_scored    INT NOT NULL DEFAULT 0,
    places_created  INT NOT NULL DEFAULT 0,
    places_merged   INT NOT NULL DEFAULT 0,
    fields_written  INT NOT NULL DEFAULT 0,
    cursor          BIGINT,               -- last place_source_records.id processed
    checkpoint      JSONB NOT NULL DEFAULT '{}',
    error_summary   JSONB NOT NULL DEFAULT '{}',
    resume_of       BIGINT
);
CREATE INDEX IF NOT EXISTS idx_resolution_runs_status ON resolution_runs (status);

-- 011_canonical_hardening.sql
-- 011_canonical_hardening — P16.1 canonical graph quality hardening.
--
-- Additive only: operational status on staged records + canonical domain
-- column for candidate blocking. No table drops, no renames.

-- ── staged records: provider-reported operational status ─────────────────
ALTER TABLE place_source_records
    ADD COLUMN IF NOT EXISTS raw_status TEXT;
-- 'open'|'temporarily_closed'|'permanently_closed' — verbatim provider value

-- ── canonical places: domain column replaces LIKE '%domain%' blocking ────
ALTER TABLE canonical_places
    ADD COLUMN IF NOT EXISTS website_domain TEXT;
CREATE INDEX IF NOT EXISTS idx_places_website_domain
    ON canonical_places (website_domain);

-- backfill from website (lowercase host, www. stripped)
UPDATE canonical_places
SET website_domain = regexp_replace(
        lower(website), '^https?://(www\.)?([^/]+).*$', '\2')
WHERE website_domain IS NULL AND website IS NOT NULL;

-- ── business dedup index for brand-level merge lookup ────────────────────
CREATE INDEX IF NOT EXISTS idx_cbiz_norm_name
    ON canonical_businesses (normalized_name);

-- 012_serving_places.sql
-- 012_serving_places — P17 serving-layer index-sync ledger.
--
-- The canonical graph (010/011) stays the source of truth; this migration
-- owns ONLY the serving side: a durable checkpoint ledger so the places
-- OpenSearch index can be rebuilt / incrementally synced / reconciled
-- without corrupting or depending on resolver internals. Additive only.

-- ── incremental scan support ──────────────────────────────────────────────
-- (updated_at, place_id) is the resumable cursor for delta indexing.
CREATE INDEX IF NOT EXISTS idx_cplaces_updated_id
    ON canonical_places (updated_at, place_id);

-- ── index-sync ledger (one row per logical index) ─────────────────────────
-- Checkpoint semantics: cursor advances only after the batch that produced
-- it is durably indexed, so a crash mid-batch re-projects the same rows
-- (upserts by place_id are idempotent). ``generation`` counts full rebuilds
-- and names the concrete index behind the ``places`` alias.
CREATE TABLE IF NOT EXISTS serving_index_state (
    index_name        TEXT PRIMARY KEY,        -- logical index / alias name
    doc_version       INT NOT NULL,            -- PlaceDocumentV1 version indexed
    generation        BIGINT NOT NULL DEFAULT 0, -- full-rebuild generation
    concrete_index    TEXT,                    -- current concrete index name
    cursor_updated_at TIMESTAMPTZ,             -- last checkpointed updated_at
    cursor_place_id   BIGINT NOT NULL DEFAULT 0, -- tiebreak within updated_at
    docs_indexed      BIGINT NOT NULL DEFAULT 0,
    last_run_at       TIMESTAMPTZ,
    last_error        TEXT,
    checkpoint        JSONB NOT NULL DEFAULT '{}'
);

-- 013_resolution_telemetry.sql
-- 013_resolution_telemetry — P16.2 decision metadata + post-run merge audit.
--
-- Motivation: the pilot showed a matcher bug can materialize wrong links
-- into place_sources, and the relink short-circuit then preserves them
-- forever. Recording matcher_version + score + reason per link makes every
-- decision auditable and lets a future matcher upgrade identify (and
-- rebuild) only the links an older matcher wrote.
-- Additive only — existing links keep NULL metadata (matcher unknown).

-- ── per-link decision record ──────────────────────────────────────────────
ALTER TABLE place_sources
    ADD COLUMN IF NOT EXISTS matcher_version TEXT,   -- RESOLVER_VERSION at link time
    ADD COLUMN IF NOT EXISTS resolution_score REAL,  -- winning pairwise score (NULL: relink/create)
    ADD COLUMN IF NOT EXISTS resolution_reason TEXT, -- existing_link|external_id|force:*|weighted|create
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

-- ── post-run audit flag on the canonical place ────────────────────────────
-- A merged place whose linked sources disagree beyond plausibility is
-- reviewable, not silently trusted. Reset+recomputed for places touched
-- by each resolution run.
ALTER TABLE canonical_places
    ADD COLUMN IF NOT EXISTS suspicious_merge BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS suspicious_reason TEXT;  -- e.g. "geo_span:1740m; source_count:9"
CREATE INDEX IF NOT EXISTS idx_cplaces_suspicious
    ON canonical_places (place_id) WHERE suspicious_merge;

-- ── per-run audit summary ─────────────────────────────────────────────────
ALTER TABLE resolution_runs
    ADD COLUMN IF NOT EXISTS audit_summary JSONB NOT NULL DEFAULT '{}';

-- 014_source_category_mappings.sql
-- 014_source_category_mappings — P16.3 runtime category vocabulary.
--
-- Motivation: provider category labels arrive in open-ended vocabularies
-- (Google -lang vi returns Vietnamese free text, OSM uses tags). The pilot
-- needed ~37 label additions in code — that does not scale. Mappings now
-- live here so a new label is a row insert, not a resolver deploy.
--
-- Lookup semantics (resolution/normalize.py CategoryResolver):
--   provider + category_key → canonical_category wins over provider '*',
--   which wins over the built-in static map (the fallback when this table
--   is empty or unreadable).
--   category_key is the label folded+lowercased exactly like matching does
--   (fold_diacritics(lower(raw))) — e.g. 'Hiệu thuốc' → 'hieu thuoc'.
--   raw_sample keeps one original label for readability.

CREATE TABLE IF NOT EXISTS source_category_mappings (
    provider            TEXT NOT NULL,             -- e.g. 'google_maps'; '*' = all providers
    category_key        TEXT NOT NULL,             -- folded+lowercased source label
    canonical_category  TEXT NOT NULL,             -- bucket: food|retail|health|...
    raw_sample          TEXT,                      -- label as first observed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, category_key)
);

-- Labels nothing could map (neither table nor built-ins), counted per run.
-- Ops workflow after a crawl: read top seen_count rows, insert mappings
-- above, next resolution run picks them up.
CREATE TABLE IF NOT EXISTS unknown_source_categories (
    provider        TEXT NOT NULL,
    category_key    TEXT NOT NULL,
    raw_sample      TEXT,
    seen_count      BIGINT NOT NULL DEFAULT 0,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, category_key)
);

-- 015_place_rich_fields.sql
-- 015_place_rich_fields — P2.0 rich place-card columns on canonical_places.
--
-- rating / review_count / price_level / images are promoted from each
-- contributing source record's verbatim ``raw_payload`` (007 staging) by
-- the resolver's existing field-provenance machinery — no network fetch,
-- no scraping. NULL means "no source reported it"; the serving layer
-- projects them through verbatim and derives open_now/map_url at request
-- time. Additive only — no backfill, no column changes.

ALTER TABLE canonical_places
    ADD COLUMN IF NOT EXISTS rating            REAL,
    ADD COLUMN IF NOT EXISTS review_count      INTEGER,
    ADD COLUMN IF NOT EXISTS price_level       TEXT,
    ADD COLUMN IF NOT EXISTS primary_image_url TEXT,
    ADD COLUMN IF NOT EXISTS images            JSONB;

-- 016_unwrap_hours_jsonb.sql
-- 016_unwrap_hours_jsonb — P2.0.3 repair for double-encoded opening_hours.
--
-- Rows written while the resolver re-serialized staged jsonb text hold a
-- JSON *string* containing the hours object (jsonb_typeof = 'string'), and
-- serving projects them to NULL — every such place lost open_now. Unwrap
-- each string row whose inner text parses to a jsonb object.
--
-- Idempotent: once unwrapped a row is jsonb_typeof 'object' and is never
-- re-selected. Per-row safe: a string whose inner text is not valid jsonb
-- fails the cast inside the sub-block and is left untouched instead of
-- aborting the migration.
--
-- Repaired rows bump updated_at so the incremental index cursor —
-- (updated_at, place_id) in serving_index_state — re-scans them; without
-- the bump a repair would stay invisible to sync() until a manual full
-- rebuild.

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT place_id, opening_hours #>> '{}' AS inner_text
        FROM canonical_places
        WHERE jsonb_typeof(opening_hours) = 'string'
          AND (opening_hours #>> '{}') LIKE '{%'
    LOOP
        BEGIN
            IF jsonb_typeof(r.inner_text::jsonb) = 'object' THEN
                UPDATE canonical_places
                SET opening_hours = r.inner_text::jsonb,
                    updated_at = now()
                WHERE place_id = r.place_id;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
    END LOOP;
END $$;
