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
