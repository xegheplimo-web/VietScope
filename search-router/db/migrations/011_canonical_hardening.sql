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
