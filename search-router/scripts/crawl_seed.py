#!/usr/bin/env python3
"""Seed + drive the crawl engine from the command line.

Enqueue a single URL or the curated VN seed set into ``crawl_frontier``,
then optionally run N pipeline rounds (each pops a batch and processes it
with bounded concurrency) — enough for E2E verification without a
long-lived worker.

    python search-router/scripts/crawl_seed.py --url https://www.chinhphu.vn --run 1
    python search-router/scripts/crawl_seed.py --seeds --run 2
    python search-router/scripts/crawl_seed.py --seeds            # enqueue only

Env: ``HUB_DATABASE_URL`` (default 127.0.0.1:5433 via manage_keys) and
``MINIO_*`` — the repo-root ``.env`` is read for both, with sensible
localhost fallbacks matching the compose stack.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[1]
if str(_SEARCH_ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_SEARCH_ROUTER_DIR))

_REPO_ENV = _SEARCH_ROUTER_DIR.parent / ".env"

from manage_keys import _load_dotenv, _resolve_dsn  # noqa: E402


def _bootstrap_env() -> None:
    """Resolve DB + MinIO env BEFORE anything imports ``config``."""
    _load_dotenv()  # HUB_* from repo .env
    os.environ.setdefault("HUB_DATABASE_URL", _resolve_dsn(None))
    if _REPO_ENV.exists():
        for line in _REPO_ENV.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("MINIO_") and value.strip():
                os.environ.setdefault(key, value.strip().strip('"'))
    os.environ.setdefault("MINIO_ENDPOINT", f"http://127.0.0.1:{os.getenv('MINIO_PORT', '9000')}")
    # Container env names (MINIO_ROOT_*) → the names ObjectStore reads.
    if not os.getenv("MINIO_ACCESS_KEY"):
        os.environ["MINIO_ACCESS_KEY"] = os.getenv("MINIO_ROOT_USER", "minioadmin")
    if not os.getenv("MINIO_SECRET_KEY"):
        os.environ["MINIO_SECRET_KEY"] = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin")


async def _amain(args: argparse.Namespace) -> int:
    _bootstrap_env()

    from config import settings
    from crawler.fetcher import Fetcher
    from crawler.pipeline import CrawlPipeline
    from crawler.politeness import DomainRateLimiter
    from crawler.robots import RobotsCache
    from crawler.seeds import load_seeds
    from crawler.worker import CrawlWorker
    from extraction.service import ExtractionService
    from storage.object_store import get_object_store
    from workers.freshness_worker import FreshnessWorker, RecrawlTask
    from workers.indexing_worker import get_indexing_worker

    extraction = ExtractionService() if settings.extraction_enabled else None
    indexer = None
    if extraction is not None and settings.indexing_enabled and settings.opensearch_enabled:
        indexer = get_indexing_worker().process_one

    frontier = FreshnessWorker()
    pipeline = CrawlPipeline(
        frontier=frontier,
        object_store=get_object_store(),
        robots=RobotsCache(),
        limiter=DomainRateLimiter(),
        fetcher=Fetcher(),
        extraction=extraction,
        indexer=indexer,
    )
    worker = CrawlWorker(pipeline, batch_size=args.batch_size, concurrency=args.concurrency)

    for url in args.url or []:
        where = await frontier.enqueue(
            RecrawlTask(url=url, priority=1.0, scheduled_at=0.0, discovered_from="cli")
        )
        print(f"enqueued ({where}) {url}")
    if args.seeds:
        robots = RobotsCache() if args.expand else None
        count = await load_seeds(frontier, expand=args.expand, robots=robots)
        print(f"enqueued {count} VN seeds")

    for round_no in range(1, args.run + 1):
        outcomes = await worker.run_once()
        if not outcomes:
            print(f"round {round_no}: frontier empty")
            break
        print(f"round {round_no}: {len(outcomes)} task(s)")
        for o in outcomes:
            extra = f" doc={o.doc_id}" if o.doc_id else ""
            extra += f" links={o.links_found}" if o.links_found else ""
            extra += f" error={o.error}" if o.error else ""
            print(f"  {o.outcome:14} status={o.status:<3} {o.elapsed_ms:7.0f}ms {o.url}{extra}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url",
        action="append",
        help="enqueue one URL (repeatable for multiple seeds)",
    )
    parser.add_argument("--seeds", action="store_true", help="enqueue the VN seed set")
    parser.add_argument(
        "--expand",
        action="store_true",
        help="with --seeds: deep-seed each site from its sitemaps/feeds",
    )
    parser.add_argument(
        "--run",
        type=int,
        default=0,
        metavar="N",
        help="run N pipeline rounds after enqueuing",
    )
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args(argv)

    if not args.url and not args.seeds and not args.run:
        parser.error("nothing to do — pass --url, --seeds, and/or --run N")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
