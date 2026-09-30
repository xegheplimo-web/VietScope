"""Opt-in live test: one real crawl through the real stack.

Skipped by default — requires the Docker stack (hub-postgres + MinIO) and
real outbound HTTP. Enable with ``LIVE=1`` and the usual env:

    LIVE=1 HUB_DATABASE_URL=postgresql://searchhub:searchhub@127.0.0.1:5433/searchhub \
    MINIO_ENDPOINT=http://127.0.0.1:9000 MINIO_ACCESS_KEY=minioadmin \
    MINIO_SECRET_KEY=minioadmin \
    python -m pytest tests/test_crawler_live.py -q
"""

from __future__ import annotations

import asyncio
import os

import pytest
from config import settings
from crawler.fetcher import Fetcher
from crawler.pipeline import CrawlPipeline
from crawler.politeness import DomainRateLimiter
from crawler.robots import RobotsCache
from extraction.service import ExtractionService
from storage.object_store import get_object_store
from workers.freshness_worker import FreshnessWorker, RecrawlTask
from workers.indexing_worker import get_indexing_worker

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("LIVE") != "1",
        reason="live crawl test — set LIVE=1 with the stack up",
    ),
]


def test_live_single_url_crawl():
    from storage import pg_client

    if not pg_client.database_url():
        pytest.skip("HUB_DATABASE_URL unset")

    url = os.getenv("LIVE_URL", "https://chinhphu.vn")

    async def main():
        pool = await pg_client.get_pool()
        if pool is None:
            pytest.skip("hub-postgres unreachable")
        frontier = FreshnessWorker()
        extraction = ExtractionService() if settings.extraction_enabled else None
        indexer = None
        if extraction is not None and settings.indexing_enabled and settings.opensearch_enabled:
            indexer = get_indexing_worker().process_one
        pipe = CrawlPipeline(
            frontier=frontier,
            object_store=get_object_store(),
            robots=RobotsCache(),
            limiter=DomainRateLimiter(),
            fetcher=Fetcher(),
            pool=pool,
            extraction=extraction,
            indexer=indexer,
        )
        await frontier.enqueue(
            RecrawlTask(url=url, priority=1.0, scheduled_at=0.0, discovered_from="seed")
        )
        batch = await frontier.pop_batch(1)
        assert batch, "frontier did not return the seeded URL"
        outcome = await pipe.process_one(batch[0])
        assert outcome.outcome in (
            "changed",
            "unchanged",
            "not_modified",
            "skipped_robots",
            "robots_unavailable",
            "oversize",
            "gone",
            "blocked",
        )
        if outcome.outcome == "changed":
            # A 'changed' verdict must mean durable artifacts — not just
            # the status line: MinIO object + document row + snapshot row.
            assert outcome.storage_key, "changed outcome without storage_key"
            assert outcome.doc_id, "changed outcome without doc_id"
            blob = await get_object_store().get_raw(outcome.storage_key)
            assert blob, f"snapshot {outcome.storage_key} not readable from MinIO"
            rows = await pool.fetch(
                "SELECT status FROM documents WHERE doc_id = $1", outcome.doc_id
            )
            assert rows and rows[0]["status"] == "active"
            snaps = await pool.fetch(
                "SELECT storage_key FROM document_snapshots WHERE doc_id = $1",
                outcome.doc_id,
            )
            assert any(r["storage_key"] == outcome.storage_key for r in snaps)
        return outcome

    outcome = asyncio.run(main())
    assert outcome.url == url
