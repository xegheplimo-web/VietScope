"""Integration test: full crawl pipeline over fixture files.

Two consecutive crawls of the same fixture page exercise the real pipeline
flow end to end — frontier row → robots → limiter → fetch → MinIO snapshot
→ documents/snapshot rows → change detection → link discovery → recrawl
scheduling — with only the transport boundaries (HTTP, Postgres, MinIO)
faked in memory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from crawler.fetcher import FetchResult
from crawler.pipeline import CrawlPipeline
from crawler.seeds import parse_feed, parse_sitemap
from test_crawler_pipeline import (
    FakeFetcher,
    FakeFrontier,
    FakeLimiter,
    FakeRobots,
    FakeStore,
    PipePool,
    _task,
)

FIXTURES = Path(__file__).parent / "fixtures" / "crawler"


def _run(coro):
    return asyncio.run(coro)


def _pipeline_for(url: str, html: bytes):
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = FetchResult(
        ok=True,
        status=200,
        headers={"etag": '"v1"'},
        content=html,
        mime="text/html",
        final_url=url,
    )
    pipe = CrawlPipeline(
        frontier=FakeFrontier(),
        object_store=FakeStore(),
        robots=FakeRobots(),
        limiter=FakeLimiter(),
        fetcher=FakeFetcher(result),
        pool=pool,
        allowed_domains={"chinhphu.vn"},
    )
    return pipe, pool


def test_fixture_parsers():
    urls = parse_sitemap((FIXTURES / "sitemap.xml").read_bytes())
    assert [u.loc for u in urls] == [
        "https://chinhphu.vn/tin-tuc/bai-1",
        "https://chinhphu.vn/chinh-sach/bai-2",
    ]
    feed = parse_feed((FIXTURES / "feed.xml").read_bytes())
    assert feed[0].loc == "https://baochinhphu.vn/phien-hop-thuong-ky"


def test_full_crawl_cycle_changed_then_unchanged():
    url = "https://chinhphu.vn/"
    html = (FIXTURES / "page.html").read_bytes()
    pipe, pool = _pipeline_for(url, html)
    store = pipe._object_store

    # ── First crawl: new document → changed, snapshot, discovery ──
    outcome1 = _run(pipe.process_one(_task(url)))
    assert outcome1.outcome == "changed"
    assert outcome1.doc_id
    assert len(store.objects) == 1
    assert len(pool.snapshots) == 1
    assert pool.documents["https://chinhphu.vn"]["language"] == "vi"
    assert pool.documents["https://chinhphu.vn"]["title"] == (
        "Chính phủ họp phiên thường kỳ tháng 9 - Báo Chính Phủ"
    )
    discovered = [d[0] for d in pool.discovered]
    assert "https://chinhphu.vn/tin-tuc" in discovered
    assert "https://vanban.chinhphu.vn/van-ban-moi" in discovered
    assert not any("example.org" in u for u in discovered)

    # ── Second crawl, same bytes → unchanged, history still grows ──
    outcome2 = _run(pipe.process_one(_task(url)))
    assert outcome2.outcome == "unchanged"
    assert len(store.objects) == 2  # immutable snapshot per fetch
    assert len(pool.snapshots) == 2

    # ── Third crawl, edited body → changed again ──
    edited = html.replace("tháng 9".encode(), "tháng 10".encode())
    pipe._fetcher.result = FetchResult(
        ok=True, status=200, headers={}, content=edited, mime="text/html", final_url=url
    )
    outcome3 = _run(pipe.process_one(_task(url)))
    assert outcome3.outcome == "changed"
    assert len(pool.snapshots) == 3


def test_pipeline_works_without_pool_degraded():
    # No Postgres: fetch + snapshot still happen; DB writes + discovery skip.
    url = "https://chinhphu.vn/"
    html = (FIXTURES / "page.html").read_bytes()
    result = FetchResult(
        ok=True, status=200, headers={}, content=html, mime="text/html", final_url=url
    )
    frontier = FakeFrontier()
    store = FakeStore()
    pipe = CrawlPipeline(
        frontier=frontier,
        object_store=store,
        robots=FakeRobots(),
        limiter=FakeLimiter(),
        fetcher=FakeFetcher(result),
        pool=None,
        allowed_domains={"chinhphu.vn"},
    )
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome in ("changed", "unchanged")
    assert len(store.objects) == 1
    assert frontier.completions[0]["next_crawl_at"] is not None
