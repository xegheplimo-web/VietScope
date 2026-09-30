"""Tiered reader — URL → clean text for the answer/research lanes.

Chain: cache → HTTP + Trafilatura → Firecrawl → Playwright + Trafilatura.

The extraction quality gate sits *between* fetch and return: a page that
fetches fine but extracts poorly escalates to the next tier instead of
serving boilerplate to the evidence pool. Firecrawl output is already
extracted Markdown, so its tier gates on length only. When extraction is
disabled (``EXTRACTION_ENABLED=false``) the HTTP/Playwright tiers degrade
to the Firecrawl path automatically.

This is the read path — no persistence. The crawl/acquisition lane keeps
its own fetcher (``crawler/fetcher.py``) because it must persist raw
snapshots and honour robots/politeness rules.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from config import settings
from extraction.models import STATUS_SUCCESS, ExtractionResult
from extraction.service import ExtractionService
from models import ScrapeResult

from pipeline.tiered_fetch import FetchResult, TieredFetcher

logger = logging.getLogger(__name__)

_CACHE_MAX = 512


@dataclass
class ReadResult:
    """Result of reading a URL — extracted main text, not raw markup."""

    url: str
    success: bool
    text: str = ""
    title: str = ""
    tier: str = ""  # cache | http | firecrawl | playwright
    quality_score: float = 0.0
    word_count: int = 0
    fetch_time_ms: float = 0.0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_scrape_result(self) -> ScrapeResult:
        """Adapt to the ``ScrapeResult`` shape evidence code consumes."""
        return ScrapeResult(
            url=self.url,
            title=self.title,
            markdown=self.text,
            metadata={
                **self.metadata,
                "reader_tier": self.tier or "reader",
                "quality_score": self.quality_score,
                "word_count": self.word_count,
            },
            error=None if self.success else (self.error or "read failed"),
        )


class ReaderService:
    """Composes TieredFetcher + ExtractionService into a text reader."""

    def __init__(
        self,
        fetcher: TieredFetcher | None = None,
        extractor: ExtractionService | None = None,
        *,
        min_chars: int | None = None,
        min_quality: float | None = None,
    ) -> None:
        self._fetcher = fetcher or TieredFetcher()
        self._extractor = extractor or ExtractionService()
        self._min_chars = min_chars if min_chars is not None else settings.extraction_min_chars
        self._min_quality = (
            min_quality if min_quality is not None else settings.extraction_min_quality
        )
        # url → (stored_at monotonic ts, ReadResult). Entries have no expiry
        # unless the caller passes max_age_s — see freshness classes in
        # pipeline.evidence_fetch for the realtime/news/static policy.
        self._cache: dict[str, tuple[float, ReadResult]] = {}

    async def read(
        self,
        url: str,
        *,
        use_cache: bool = True,
        use_firecrawl: bool = True,
        use_playwright: bool = True,
        timeout: float | None = None,
        max_age_s: float | None = None,
    ) -> ReadResult:
        """Read one URL → extracted text. Never raises.

        ``max_age_s`` bounds how old a cache entry may be to count as a hit
        (seconds); ``None`` keeps the historical never-expire behavior.
        """
        if use_cache and url in self._cache:
            stored_at, cached = self._cache[url]
            fresh = max_age_s is None or (time.monotonic() - stored_at) <= max_age_s
            if cached.success and fresh:
                return ReadResult(
                    url=cached.url,
                    success=True,
                    text=cached.text,
                    title=cached.title,
                    tier="cache",
                    quality_score=cached.quality_score,
                    word_count=cached.word_count,
                    fetch_time_ms=cached.fetch_time_ms,
                    metadata={**cached.metadata, "cached_tier": cached.tier},
                )

        degraded: ReadResult | None = None
        errors: list[str] = []

        # Tier: HTTP → Trafilatura (quality-gated)
        http = await self._fetcher.fetch_http(url)
        if http.success:
            rr = await self._extract(http, tier="http")
            if rr is not None and rr.success:
                self._store(url, rr)
                return rr
            degraded = degraded or rr  # keep first partial result
        else:
            errors.append(http.error or "http fetch failed")

        # Tier: Firecrawl — already-extracted Markdown, gate on length only.
        if use_firecrawl:
            fc = await self._fetcher.fetch_firecrawl(url, timeout=timeout)
            if fc.success and len(fc.content.strip()) >= self._min_chars:
                fc_meta = (fc.metadata.get("firecrawl") or {}).get("metadata", {}) or {}
                rr = ReadResult(
                    url=url,
                    success=True,
                    text=fc.content,
                    title=fc_meta.get("title", ""),
                    tier="firecrawl",
                    word_count=len(fc.content.split()),
                    fetch_time_ms=fc.fetch_time_ms,
                    metadata={
                        "extraction_method": "firecrawl_markdown",
                        "firecrawl_meta": fc_meta,
                    },
                )
                self._store(url, rr)
                return rr
            errors.append(fc.error or "firecrawl insufficient content")

        # Tier: Playwright render → Trafilatura (same quality gate as HTTP).
        if use_playwright:
            pw = await self._fetcher.fetch_playwright(url)
            if pw.success:
                rr = await self._extract(pw, tier="playwright")
                if rr is not None and rr.success:
                    self._store(url, rr)
                    return rr
                degraded = degraded or rr
            else:
                errors.append(pw.error or "playwright render failed")

        # All tiers exhausted — serve the best partial extraction we have
        # rather than nothing (partial results are better than a hard fail).
        if degraded is not None and degraded.text.strip():
            degraded.metadata["degraded"] = True
            degraded.metadata["tier_errors"] = errors
            self._store(url, degraded)
            return degraded
        return ReadResult(
            url=url, success=False, error="; ".join(errors) or "all reader tiers failed"
        )

    async def read_batch(
        self,
        urls: list[str],
        *,
        max_concurrent: int = 5,
        max_age_s: float | None = None,
        **kwargs: Any,
    ) -> list[ReadResult]:
        """Read multiple URLs with a concurrency limit; preserves order."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _one(url: str) -> ReadResult:
            async with semaphore:
                try:
                    return await self.read(url, max_age_s=max_age_s, **kwargs)
                except Exception as exc:  # noqa: BLE001 — reader must not sink the batch
                    return ReadResult(url=url, success=False, error=f"{type(exc).__name__}: {exc}")

        return await asyncio.gather(*[_one(u) for u in urls])

    async def _extract(self, fr: FetchResult, *, tier: str) -> ReadResult | None:
        """Run extraction on a fetched body and apply the quality gate."""
        if not settings.extraction_enabled:
            return None
        try:
            res: ExtractionResult = await self._extractor.extract(
                url=fr.url, mime=fr.content_type, content=fr.content
            )
        except Exception as exc:  # noqa: BLE001 — extractor contract says never, belt+braces
            logger.debug("reader extract raised for %s: %r", fr.url, exc)
            return None
        doc = res.document
        if res.status != STATUS_SUCCESS or doc is None:
            # Below the quality gate — return as a degraded candidate if it
            # produced any usable text at all.
            if doc is not None and doc.text.strip():
                return ReadResult(
                    url=fr.url,
                    success=False,
                    text=doc.text,
                    title=doc.title or "",
                    tier=tier,
                    quality_score=doc.quality_score,
                    word_count=doc.word_count,
                    fetch_time_ms=fr.fetch_time_ms,
                    error=res.error or res.status,
                    metadata={
                        "extraction_status": res.status,
                        "published_at": str(doc.published_at) if doc.published_at else None,
                    },
                )
            return None
        return ReadResult(
            url=fr.url,
            success=True,
            text=doc.text,
            title=doc.title or "",
            tier=tier,
            quality_score=doc.quality_score,
            word_count=doc.word_count,
            fetch_time_ms=fr.fetch_time_ms,
            metadata={
                "extraction_method": doc.extraction_method,
                "quality_score": doc.quality_score,
                "published_at": str(doc.published_at) if doc.published_at else None,
            },
        )

    def _store(self, url: str, result: ReadResult) -> None:
        if len(self._cache) >= _CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))  # evict oldest (FIFO)
        self._cache[url] = (time.monotonic(), result)


_reader: ReaderService | None = None


def get_reader() -> ReaderService:
    """Process-wide reader — shared page cache + fetcher connection reuse."""
    global _reader
    if _reader is None:
        _reader = ReaderService()
    return _reader


async def read_batch(urls: list[str], **kwargs: Any) -> list[ReadResult]:
    """Module-level seam — callers bind this name so tests can patch it."""
    return await get_reader().read_batch(urls, **kwargs)


async def read(url: str, **kwargs: Any) -> ReadResult:
    """Module-level seam for single-URL reads."""
    return await get_reader().read(url, **kwargs)
