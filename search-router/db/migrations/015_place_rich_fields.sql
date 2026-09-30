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
