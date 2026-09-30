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
