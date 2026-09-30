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
