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
