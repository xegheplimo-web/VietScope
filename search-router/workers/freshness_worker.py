"""L17 Freshness Worker — recrawl priority queue on ``crawl_frontier``.

Computes recrawl priority for URLs based on:
- Query popularity (7d)
- Source authority
- Observed change rate
- Freshness requirement
- RSS activity
- Time since last crawl

Persistence (Phase 1/T2): the canonical queue is the ``crawl_frontier`` table
in hub-postgres — enqueue/pop survive restarts and are shared across router
replicas (``FOR UPDATE SKIP LOCKED`` makes concurrent pops safe). Claims
carry a lease: a row stuck in ``fetching`` past ``claim_lease_seconds`` is
reclaimed by the next pop, and ``claim_token`` ownership keeps a late
completion from a crashed worker from clobbering the newer claim. When
``HUB_DATABASE_URL`` is unset or the DB is unreachable every async method
falls back to the in-memory ``_queue`` — same degrade-by-design contract as
``storage.pg_client`` — and the RAM queue is flushed back into Postgres once
the pool comes back.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# How long a 'fetching' claim may go unanswered before another pop reclaims
# it. Worker crash → row becomes eligible again after the lease instead of
# being stuck forever.
DEFAULT_CLAIM_LEASE_SECONDS = float(os.getenv("FRONTIER_CLAIM_LEASE_SECONDS", "900"))


@dataclass
class RecrawlTask:
    """A single recrawl task."""

    url: str
    priority: float
    scheduled_at: float
    last_crawl: float | None = None
    fail_count: int = 0
    discovered_from: str = ""
    change_rate_estimate: float = 0.0
    # Ownership token assigned by pop_batch — pass back to complete()/fail()
    # so a stale claim can't overwrite a newer one.
    claim_token: str = ""


# enqueue: dedup by url — refresh priority and earliest next_crawl_at without
# resurrecting rows a pop_batch has in flight unless they finished/failed.
_ENQUEUE_SQL = """
INSERT INTO crawl_frontier
    (url, domain, status, priority, scheduled_at, next_crawl_at,
     discovered_from, change_rate)
VALUES ($1, $2, 'queued', $3, now(), $4, $5, $6)
ON CONFLICT (url) DO UPDATE SET
    priority = GREATEST(crawl_frontier.priority, EXCLUDED.priority),
    next_crawl_at = LEAST(crawl_frontier.next_crawl_at, EXCLUDED.next_crawl_at),
    discovered_from =
        COALESCE(crawl_frontier.discovered_from, EXCLUDED.discovered_from),
    change_rate = COALESCE(EXCLUDED.change_rate, crawl_frontier.change_rate),
    status = CASE
        WHEN crawl_frontier.status IN ('done', 'failed') THEN 'queued'
        ELSE crawl_frontier.status
    END,
    claimed_at = CASE
        WHEN crawl_frontier.status IN ('done', 'failed') THEN NULL
        ELSE crawl_frontier.claimed_at
    END,
    claim_token = CASE
        WHEN crawl_frontier.status IN ('done', 'failed') THEN NULL
        ELSE crawl_frontier.claim_token
    END
"""

# pop: claim up to $1 due tasks — CTE row locks + SKIP LOCKED so concurrent
# workers never grab the same URL; marking 'fetching' hides them until
# complete()/fail() moves them on. Rows stuck in 'fetching' longer than the
# claim lease ($2 seconds) are reclaimed; a fresh claim_token per pop makes
# late completions from dead workers a no-op.
_POP_SQL = """
WITH next AS (
    SELECT url FROM crawl_frontier
    WHERE (
        status = 'queued'
        AND (next_crawl_at IS NULL OR next_crawl_at <= now())
    ) OR (
        status = 'fetching'
        AND (claimed_at IS NULL
             OR claimed_at < now() - make_interval(secs => $2::float8))
    )
    ORDER BY priority DESC, scheduled_at ASC
    LIMIT $1
    FOR UPDATE SKIP LOCKED
)
UPDATE crawl_frontier f
SET status = 'fetching',
    last_crawled_at = now(),
    claimed_at = now(),
    claim_token = gen_random_uuid()::text
FROM next
WHERE f.url = next.url
RETURNING f.url, f.priority, f.next_crawl_at, f.last_crawled_at,
          f.failure_count, f.discovered_from, f.change_rate, f.claim_token
"""

# complete: 'queued' + next_crawl_at when the caller schedules a recrawl,
# 'done' otherwise — a scheduled recrawl must stay pop-eligible. The
# claim_token guard keeps a late completion from overwriting a newer claim.
_COMPLETE_SQL = """
UPDATE crawl_frontier SET
    status = CASE WHEN $2::timestamptz IS NULL THEN 'done' ELSE 'queued' END,
    fetched_at = now(),
    last_crawled_at = now(),
    failure_count = 0,
    next_crawl_at = $2,
    etag = COALESCE($3, etag),
    last_modified = COALESCE($4, last_modified),
    change_rate = COALESCE($5, change_rate),
    claimed_at = NULL,
    claim_token = NULL
WHERE url = $1
  AND claim_token IS NOT DISTINCT FROM $6
"""

# fail: requeue with linear backoff (10 min per consecutive failure, capped
# at 12 h) — a frontier that parks failed URLs forever stops being useful.
_FAIL_SQL = """
UPDATE crawl_frontier SET
    status = 'queued',
    fetched_at = now(),
    failure_count = failure_count + 1,
    next_crawl_at = now() + make_interval(mins => LEAST(10 * (failure_count + 1), 720)),
    claimed_at = NULL,
    claim_token = NULL
WHERE url = $1
  AND claim_token IS NOT DISTINCT FROM $2
"""

_CHANGE_RATE_SQL = """
UPDATE crawl_frontier SET change_rate = $2 WHERE url = $1
"""


class FreshnessWorker:
    """Computes recrawl priority and owns the persistent recrawl frontier."""

    def __init__(self, claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS):
        self._queue: list[RecrawlTask] = []
        self._claim_lease_seconds = claim_lease_seconds

    def compute_priority(
        self,
        url: str,
        query_popularity_7d: float = 0.0,
        source_authority: float = 0.5,
        observed_change_rate: float = 0.0,
        freshness_requirement: str = "medium",
        rss_activity: float = 0.0,
        time_since_last_crawl: float = 0.0,
    ) -> float:
        """Compute recrawl priority score."""
        # Weights
        w_popularity = 0.30
        w_authority = 0.20
        w_change_rate = 0.20
        w_freshness = 0.15
        w_rss = 0.10
        w_time = 0.05

        # Freshness multiplier
        freshness_mult = {
            "realtime": 2.0,
            "high": 1.5,
            "medium": 1.0,
            "slow": 0.5,
            "static": 0.1,
        }.get(freshness_requirement, 1.0)

        priority = (
            w_popularity * query_popularity_7d
            + w_authority * source_authority
            + w_change_rate * observed_change_rate
            + w_freshness * freshness_mult
            + w_rss * rss_activity
            + w_time * min(time_since_last_crawl / 86400, 1.0)
        )

        return priority

    # ── In-memory queue (sync dev path / DB-down fallback) ───────────────

    def add_task(self, task: RecrawlTask) -> None:
        """Add task to the in-memory recrawl queue (fallback path)."""
        self._queue.append(task)
        # Sort by priority descending
        self._queue.sort(key=lambda t: t.priority, reverse=True)

    def get_next_batch(self, batch_size: int = 10) -> list[RecrawlTask]:
        """Get next batch of highest-priority tasks from the RAM queue."""
        batch = self._queue[:batch_size]
        self._queue = self._queue[batch_size:]
        return batch

    def update_change_rate(self, url: str, change_rate: float) -> None:
        """Update observed change rate for URL in the RAM queue."""
        for task in self._queue:
            if task.url == url:
                task.change_rate_estimate = change_rate
                break

    # ── Persistent frontier (crawl_frontier via storage.pg_client) ────────

    @staticmethod
    def _enqueue_params(task: RecrawlTask) -> tuple:
        return (
            task.url,
            urlparse(task.url).netloc.lower() or None,
            task.priority,
            _to_dt(task.scheduled_at),
            task.discovered_from or None,
            task.change_rate_estimate or None,
        )

    async def _flush_memory_queue(self, pool) -> None:
        """Drain RAM-fallback tasks into Postgres once the DB is back.

        Dedup and due-time are preserved by ``_ENQUEUE_SQL`` (ON CONFLICT
        merge + earliest next_crawl_at). Tasks whose insert fails stay in
        ``_queue`` for the next flush attempt; a cancelled flush re-queues
        the in-flight task plus everything not yet attempted, so no recovery
        work is lost.
        """
        if not self._queue:
            return
        pending, self._queue = self._queue, []
        for idx, task in enumerate(pending):
            try:
                await pool.execute(_ENQUEUE_SQL, *self._enqueue_params(task))
            except asyncio.CancelledError:
                self._queue.extend(pending[idx:])
                self._queue.sort(key=lambda t: t.priority, reverse=True)
                raise
            except Exception as exc:
                logger.warning("frontier flush failed for %s: %s", task.url, exc)
                self._queue.append(task)
        if self._queue:
            self._queue.sort(key=lambda t: t.priority, reverse=True)

    async def enqueue(self, task: RecrawlTask) -> str:
        """Insert/refresh a URL in the frontier. Returns 'db' or 'memory'."""
        from storage import pg_client

        pool = await pg_client.get_pool()
        if pool is None:
            self.add_task(task)
            return "memory"
        try:
            await self._flush_memory_queue(pool)
            await pool.execute(_ENQUEUE_SQL, *self._enqueue_params(task))
            return "db"
        except Exception as exc:
            logger.warning("frontier enqueue failed for %s: %s", task.url, exc)
            self.add_task(task)
            return "memory"

    async def pop_batch(self, batch_size: int = 10) -> list[RecrawlTask]:
        """Claim the next due batch — marks rows 'fetching' atomically."""
        from storage import pg_client

        pool = await pg_client.get_pool()
        if pool is None:
            return self.get_next_batch(batch_size)
        try:
            await self._flush_memory_queue(pool)
            rows = await pool.fetch(_POP_SQL, batch_size, self._claim_lease_seconds)
            return [self._row_to_task(row) for row in rows]
        except Exception as exc:
            logger.warning("frontier pop_batch failed: %s", exc)
            return self.get_next_batch(batch_size)

    async def complete(
        self,
        url: str,
        *,
        next_crawl_at: float | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        change_rate: float | None = None,
        claim_token: str | None = None,
    ) -> bool:
        """Mark a fetched URL done; schedule the next crawl if given.

        ``next_crawl_at`` set → row goes back to 'queued' and becomes
        pop-eligible when due; unset → 'done'. ``claim_token`` should be the
        token returned by pop_batch — a mismatch (stale claim) updates zero
        rows and returns False.
        """
        from storage import pg_client

        pool = await pg_client.get_pool()
        if pool is None:
            return False
        try:
            result = await pool.execute(
                _COMPLETE_SQL,
                url,
                _to_dt_or_none(next_crawl_at),
                etag,
                last_modified,
                change_rate,
                claim_token,
            )
            return result.split()[-1] != "0"
        except Exception as exc:
            logger.warning("frontier complete failed for %s: %s", url, exc)
            return False

    async def fail(self, url: str, claim_token: str | None = None) -> bool:
        """Record a fetch failure: failure_count+1 and requeue with backoff."""
        from storage import pg_client

        pool = await pg_client.get_pool()
        if pool is None:
            return False
        try:
            result = await pool.execute(_FAIL_SQL, url, claim_token)
            return result.split()[-1] != "0"
        except Exception as exc:
            logger.warning("frontier fail failed for %s: %s", url, exc)
            return False

    async def set_change_rate(self, url: str, change_rate: float) -> bool:
        """Persist observed change rate (DB first, RAM fallback)."""
        from storage import pg_client

        pool = await pg_client.get_pool()
        if pool is None:
            self.update_change_rate(url, change_rate)
            return False
        try:
            await pool.execute(_CHANGE_RATE_SQL, url, change_rate)
            return True
        except Exception as exc:
            logger.warning("frontier set_change_rate failed for %s: %s", url, exc)
            self.update_change_rate(url, change_rate)
            return False

    @staticmethod
    def _row_to_task(row) -> RecrawlTask:
        next_crawl = row["next_crawl_at"]
        last_crawled = row["last_crawled_at"]
        return RecrawlTask(
            url=row["url"],
            priority=float(row["priority"]),
            scheduled_at=next_crawl.timestamp() if next_crawl else 0.0,
            last_crawl=last_crawled.timestamp() if last_crawled else None,
            fail_count=int(row["failure_count"] or 0),
            discovered_from=row["discovered_from"] or "",
            change_rate_estimate=float(row["change_rate"] or 0.0),
            claim_token=row["claim_token"] or "",
        )


def _to_dt(epoch: float | None) -> datetime | None:
    """Epoch seconds → aware datetime; 0/None → now() (due immediately)."""
    if not epoch:
        return datetime.now(UTC)
    return datetime.fromtimestamp(epoch, UTC)


def _to_dt_or_none(epoch: float | None) -> datetime | None:
    """Epoch seconds → aware datetime; only None → NULL (no recrawl scheduled).

    ``0.0`` is a real epoch (due in the past → immediately eligible), not a
    sentinel — every finite value converts.
    """
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC)
