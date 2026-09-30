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
