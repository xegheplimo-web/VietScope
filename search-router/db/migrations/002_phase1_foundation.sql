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
