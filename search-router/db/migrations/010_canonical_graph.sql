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
