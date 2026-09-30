"""P1 Reader Consolidation — unit tests for pipeline.reader.ReaderService.

Covers the tier chain cache → HTTP+Trafilatura → Firecrawl → Playwright,
the extraction quality gate between tiers, degraded-partial behavior, and
the ReadResult → ScrapeResult adapter used by the research agent.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

from extraction.models import (
    STATUS_LOW_QUALITY,
    STATUS_SUCCESS,
    ExtractedDocument,
    ExtractionResult,
)
from pipeline.reader import ReaderService, ReadResult
from pipeline.tiered_fetch import FetchResult


def _fetch(url, *, ok=True, tier="http", content="<html>body</html>", ctype="text/html", error=""):
    return FetchResult(
        url=url,
        success=ok,
        status_code=200 if ok else 0,
        content=content,
        content_type=ctype,
        tier=tier,
        error=error,
    )


def _doc(text, quality=0.9):
    return ExtractedDocument(
        text=text,
        title="Page Title",
        quality_score=quality,
        word_count=len(text.split()),
        extraction_method="trafilatura",
    )


class _FakeFetcher:
    """Records tier calls; each tier configurable via attrs."""

    def __init__(self, http=None, firecrawl=None, playwright=None):
        self._http = http
        self._fc = firecrawl
        self._pw = playwright
        self.calls = []

    async def fetch_http(self, url):
        self.calls.append("http")
        if callable(self._http):
            return self._http(url)
        return self._http or _fetch(url, ok=False, error="conn refused")

    async def fetch_firecrawl(self, url, *, timeout=None):
        self.calls.append("firecrawl")
        if callable(self._fc):
            return self._fc(url)
        return self._fc or _fetch(url, ok=False, tier="firecrawl", error="fc down")

    async def fetch_playwright(self, url):
        self.calls.append("playwright")
        if callable(self._pw):
            return self._pw(url)
        return self._pw or _fetch(url, ok=False, tier="playwright", error="pw down")


class _FakeExtractor:
    """Returns a canned ExtractionResult per URL."""

    def __init__(self, results=None, default=None):
        self.results = results or {}
        self.default = default
        self.calls = []

    async def extract(self, *, url, mime, content, snapshot_id=None):
        self.calls.append(url)
        fn = self.results.get(url, self.default)
        if callable(fn):
            return fn(url, mime, content)
        return fn


def _ok_extract(url, mime, content):
    return ExtractionResult(status=STATUS_SUCCESS, document=_doc("good extracted text " * 20))


def _lowq_extract(url, mime, content):
    return ExtractionResult(
        status=STATUS_LOW_QUALITY,
        document=_doc("some thin text " * 10, quality=0.1),
        error="quality 0.10 < 0.30",
    )


def _service(http=None, firecrawl=None, playwright=None, extractor=None):
    return ReaderService(
        fetcher=_FakeFetcher(http=http, firecrawl=firecrawl, playwright=playwright),
        extractor=extractor or _FakeExtractor(default=_ok_extract),
        min_chars=100,
        min_quality=0.30,
    )


class TestTierChain:
    def test_http_plus_trafilatura_success(self):
        svc = _service(http=lambda u: _fetch(u))
        res = asyncio.run(svc.read("https://a.example"))
        assert res.success is True
        assert res.tier == "http"
        assert res.text.startswith("good extracted text")
        assert res.title == "Page Title"
        assert res.quality_score == 0.9
        # HTTP was enough — no fallback tiers touched
        assert svc._fetcher.calls == ["http"]

    def test_low_quality_escalates_to_firecrawl(self):
        svc = _service(
            http=lambda u: _fetch(u),
            extractor=_FakeExtractor(default=_lowq_extract),
            firecrawl=lambda u: _fetch(u, tier="firecrawl", content="firecrawl markdown " * 30),
        )
        res = asyncio.run(svc.read("https://a.example"))
        assert res.success is True
        assert res.tier == "firecrawl"
        assert "firecrawl markdown" in res.text
        assert svc._fetcher.calls == ["http", "firecrawl"]

    def test_http_failure_falls_back_to_firecrawl(self):
        svc = _service(
            http=None,  # default: conn refused
            firecrawl=lambda u: _fetch(u, tier="firecrawl", content="fc body " * 30),
        )
        res = asyncio.run(svc.read("https://a.example"))
        assert res.success is True
        assert res.tier == "firecrawl"
        assert svc._fetcher.calls == ["http", "firecrawl"]

    def test_firecrawl_short_content_escalates_to_playwright(self):
        svc = _service(
            http=None,
            firecrawl=lambda u: _fetch(u, tier="firecrawl", content="tiny"),
            playwright=lambda u: _fetch(u, tier="playwright"),
        )
        res = asyncio.run(svc.read("https://a.example"))
        assert res.success is True
        assert res.tier == "playwright"
        assert svc._fetcher.calls == ["http", "firecrawl", "playwright"]

    def test_all_tiers_fail_returns_error(self):
        svc = _service()  # every tier fails, no partial
        res = asyncio.run(svc.read("https://a.example"))
        assert res.success is False
        assert res.text == ""
        assert "conn refused" in res.error
        assert svc._fetcher.calls == ["http", "firecrawl", "playwright"]

    def test_degraded_partial_returned_when_fallbacks_fail(self):
        """Low-quality extraction beats a hard fail — returned degraded."""
        svc = _service(
            http=lambda u: _fetch(u),
            extractor=_FakeExtractor(default=_lowq_extract),
        )
        res = asyncio.run(svc.read("https://a.example"))
        assert res.success is False  # not a clean read
        assert res.text.startswith("some thin text")
        assert res.metadata["degraded"] is True
        assert res.metadata["extraction_status"] == STATUS_LOW_QUALITY

    def test_cache_hit_returns_cached_tier(self):
        svc = _service(http=lambda u: _fetch(u))
        first = asyncio.run(svc.read("https://a.example"))
        assert first.tier == "http"
        second = asyncio.run(svc.read("https://a.example"))
        assert second.success is True
        assert second.tier == "cache"
        assert second.metadata["cached_tier"] == "http"
        assert second.text == first.text
        assert svc._fetcher.calls == ["http"]  # only one real fetch

    def test_firecrawl_tier_gate_on_min_chars(self):
        svc = _service(
            http=None,
            firecrawl=lambda u: _fetch(u, tier="firecrawl", content="x" * 50),
        )
        res = asyncio.run(svc.read("https://a.example"))
        # 50 chars < min_chars=100 → escalate to playwright → fails → hard fail
        assert res.success is False
        assert svc._fetcher.calls == ["http", "firecrawl", "playwright"]


class TestCacheTTL:
    """P1.2 — the page cache honors a caller-supplied max_age_s TTL so
    fresh-content queries ("giá vàng hôm nay") never serve a stale
    snapshot; None keeps the historical never-expire behavior."""

    def _seed(self, svc, url, age_s):
        svc._cache[url] = (
            time.monotonic() - age_s,
            ReadResult(url=url, success=True, text="stale body", tier="http"),
        )

    def test_no_max_age_serves_any_age(self):
        svc = _service(http=lambda u: _fetch(u))
        self._seed(svc, "https://a.example", age_s=99999)
        res = asyncio.run(svc.read("https://a.example"))
        assert res.tier == "cache"
        assert svc._fetcher.calls == []

    def test_stale_entry_refetched_when_max_age_exceeded(self):
        svc = _service(http=lambda u: _fetch(u))
        self._seed(svc, "https://a.example", age_s=100)
        res = asyncio.run(svc.read("https://a.example", max_age_s=30))
        assert res.tier == "http"
        assert res.text.startswith("good extracted text")
        assert svc._fetcher.calls == ["http"]

    def test_fresh_entry_served_within_max_age(self):
        svc = _service(http=lambda u: _fetch(u))
        self._seed(svc, "https://a.example", age_s=1)
        res = asyncio.run(svc.read("https://a.example", max_age_s=30))
        assert res.tier == "cache"
        assert res.metadata["cached_tier"] == "http"
        assert svc._fetcher.calls == []

    def test_use_cache_false_bypasses_entirely(self):
        svc = _service(http=lambda u: _fetch(u))
        self._seed(svc, "https://a.example", age_s=1)
        res = asyncio.run(svc.read("https://a.example", use_cache=False))
        assert res.tier == "http"
        assert svc._fetcher.calls == ["http"]

    def test_read_batch_forwards_max_age(self):
        svc = _service(http=lambda u: _fetch(u))
        self._seed(svc, "https://a.example", age_s=100)
        results = asyncio.run(svc.read_batch(["https://a.example"], max_age_s=30))
        assert results[0].tier == "http"
        assert svc._fetcher.calls == ["http"]


class TestBatchAndAdapter:
    def test_read_batch_preserves_order_and_isolates_failures(self):
        svc = _service(
            http=lambda u: _fetch(u) if "ok" in u else _fetch(u, ok=False, error="down"),
        )
        urls = ["https://ok-1.example", "https://bad.example", "https://ok-2.example"]
        results = asyncio.run(svc.read_batch(urls, max_concurrent=2))
        assert [r.url for r in results] == urls
        assert results[0].success and results[2].success
        assert results[1].success is False

    def test_read_batch_never_raises(self):
        class _Boom(_FakeFetcher):
            async def fetch_http(self, url):
                raise RuntimeError("kaboom")

        svc = ReaderService(fetcher=_Boom(), extractor=_FakeExtractor(default=_ok_extract))
        results = asyncio.run(svc.read_batch(["https://a.example"]))
        assert results[0].success is False
        assert "RuntimeError" in results[0].error

    def test_to_scrape_result_shape(self):
        rr = ReadResult(
            url="https://a.example",
            success=True,
            text="body",
            title="T",
            tier="http",
            quality_score=0.8,
            word_count=1,
        )
        sc = rr.to_scrape_result()
        assert sc.url == "https://a.example"
        assert sc.markdown == "body"
        assert sc.title == "T"
        assert sc.error is None
        assert sc.metadata["reader_tier"] == "http"
        # failure case
        bad = ReadResult(url="https://b.example", success=False, error="nope")
        assert bad.to_scrape_result().error == "nope"


class TestResearchAgentSeam:
    """DoD: research path must not call providers.firecrawl directly."""

    def test_agent_orchestrator_has_no_firecrawl_scrape(self):
        import agent.orchestrator as orch

        assert not hasattr(orch, "firecrawl_scrape_batch")
        assert callable(orch.read_batch)

    def test_module_read_batch_delegates_to_singleton(self):
        import pipeline.reader as pr

        fake_result = [ReadResult(url="https://a", success=True, text="x", tier="http")]
        with patch.object(
            ReaderService, "read_batch", new=AsyncMock(return_value=fake_result)
        ) as m:
            out = asyncio.run(pr.read_batch(["https://a"]))
        assert out == fake_result
        m.assert_awaited_once()
