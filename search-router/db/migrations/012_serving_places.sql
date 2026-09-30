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
