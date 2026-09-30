"""L7 Tiered Fetch — page cache → HTTP → Firecrawl → Playwright.

Each tier has its own timeout and fallback.  Partial results are OK.

SSRF policy (shared with the crawl lane): the HTTP tier runs through
``crawler.netguard.guarded_get`` — every redirect hop is re-validated and
the dial itself is pinned to DNS answers that resolved public at connect
time, so a search-result URL cannot bounce the reader into internal
space.  Firecrawl/Playwright render server-side inside their own
sandbox, so our per-hop guard cannot apply there; the requested URL is
still netguard-checked before dispatch, and a Firecrawl result that
landed on a different host than requested is dropped.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from config import settings
from crawler.netguard import NetGuard, guarded_client, guarded_get

logger = logging.getLogger(__name__)

_MAX_REDIRECTS = 10
_MAX_BYTES = 10 * 1024 * 1024


@dataclass
class FetchResult:
    """Result of fetching a URL."""

    url: str
    success: bool
    status_code: int = 0
    content: str = ""
    content_type: str = ""
    content_hash: str = ""
    etag: str = ""
    last_modified: str = ""
    fetch_time_ms: float = 0.0
    tier: str = ""  # cache | http | firecrawl | playwright
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _same_host(a: str, b: str) -> bool:
    """Case-insensitive host equality; malformed input never matches."""
    try:
        return (urlsplit(a).hostname or "").lower() == (urlsplit(b).hostname or "").lower()
    except ValueError:
        return False


# Mirrors crawler/pipeline.py:_decode — the declared Content-Type charset
# wins over the UTF-8 default, exactly like the old ``resp.text`` path.
_CHARSET_RE = re.compile(r"charset=([a-zA-Z0-9_-]+)")


def _decode(content: bytes, headers: dict[str, str]) -> str:
    charset = "utf-8"
    match = _CHARSET_RE.search(headers.get("content-type", ""))
    if match:
        charset = match.group(1)
    try:
        return content.decode(charset, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


class TieredFetcher:
    """Tiered URL fetcher with fallback.

    Per-tier methods (``fetch_http``/``fetch_firecrawl``/``fetch_playwright``)
    are public so ``pipeline.reader`` can interpose extraction quality gates
    between tiers; ``fetch()`` keeps the simple first-success chain.
    """

    def __init__(
        self,
        firecrawl_url: str | None = None,
        playwright_url: str | None = None,
        timeout_http: float = 5.0,
        timeout_firecrawl: float = 15.0,
        timeout_playwright: float = 30.0,
        *,
        netguard: NetGuard | None = None,
        max_redirects: int = _MAX_REDIRECTS,
        max_bytes: int = _MAX_BYTES,
    ):
        self.firecrawl_url = firecrawl_url or settings.firecrawl_url
        self.playwright_url = playwright_url or settings.playwright_url
        self.timeout_http = timeout_http
        self.timeout_firecrawl = timeout_firecrawl
        self.timeout_playwright = timeout_playwright
        self._netguard = netguard or NetGuard()
        self._max_redirects = max_redirects
        self._max_bytes = max_bytes
        self._page_cache: dict[str, FetchResult] = {}

    async def fetch(
        self,
        url: str,
        *,
        use_cache: bool = True,
        use_firecrawl: bool = True,
        use_playwright: bool = True,
    ) -> FetchResult:
        """Fetch URL with tiered fallback."""
        # Tier 1: Page cache
        if use_cache and url in self._page_cache:
            cached = self._page_cache[url]
            if cached.success:
                return cached

        # Tier 2: HTTP static
        result = await self.fetch_http(url)
        if result.success:
            self._page_cache[url] = result
            return result

        # Tier 3: Firecrawl API
        if use_firecrawl:
            result = await self.fetch_firecrawl(url)
            if result.success:
                self._page_cache[url] = result
                return result

        # Tier 4: Playwright (JS-heavy pages)
        if use_playwright:
            result = await self.fetch_playwright(url)
            if result.success:
                self._page_cache[url] = result
                return result

        # All tiers failed
        return FetchResult(
            url=url,
            success=False,
            error="All fetch tiers failed",
        )

    async def fetch_batch(
        self,
        urls: list[str],
        *,
        max_concurrent: int = 5,
        **kwargs: Any,
    ) -> list[FetchResult]:
        """Fetch multiple URLs with concurrency limit."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _fetch_one(url: str) -> FetchResult:
            async with semaphore:
                return await self.fetch(url, **kwargs)

        tasks = [_fetch_one(url) for url in urls]
        return await asyncio.gather(*tasks)

    async def fetch_http(self, url: str) -> FetchResult:
        """Tier 2: Direct HTTP fetch — every redirect hop SSRF-validated."""
        start = time.monotonic()
        try:
            async with guarded_client(self._netguard, timeout=self.timeout_http) as client:
                resp = await guarded_get(
                    client,
                    url,
                    headers={"User-Agent": "SearchHub/2.0"},
                    timeout=self.timeout_http,
                    max_redirects=self._max_redirects,
                    max_bytes=self._max_bytes,
                    netguard=self._netguard,
                )
            elapsed = (time.monotonic() - start) * 1000

            if resp.error is not None:
                return FetchResult(
                    url=url,
                    success=False,
                    status_code=resp.status,
                    fetch_time_ms=elapsed,
                    tier="http",
                    error=resp.error,
                )
            if resp.oversize:
                # A capped body is not a document (netguard contract) —
                # surface it as a failure so the next tier can try.
                return FetchResult(
                    url=url,
                    success=False,
                    status_code=resp.status,
                    fetch_time_ms=elapsed,
                    tier="http",
                    error=f"oversize body: capped at {self._max_bytes} bytes",
                )
            if resp.status == 200:
                content = _decode(resp.body, resp.headers)
                return FetchResult(
                    url=resp.final_url or url,
                    success=True,
                    status_code=resp.status,
                    content=content,
                    content_type=resp.headers.get("content-type", ""),
                    content_hash=hashlib.sha256(content.encode()).hexdigest()[:16],
                    etag=resp.headers.get("etag", ""),
                    last_modified=resp.headers.get("last-modified", ""),
                    fetch_time_ms=elapsed,
                    tier="http",
                    metadata={
                        "final_url": resp.final_url,
                        "redirects": list(resp.redirects),
                        "oversize": resp.oversize,
                    },
                )
            return FetchResult(
                url=url,
                success=False,
                status_code=resp.status,
                fetch_time_ms=elapsed,
                tier="http",
                error=f"HTTP {resp.status}",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return FetchResult(
                url=url,
                success=False,
                fetch_time_ms=elapsed,
                tier="http",
                error=str(exc),
            )

    async def _egress_check(self, url: str, tier: str) -> FetchResult | None:
        """NetGuard the URL before handing it to a server-side renderer.

        Firecrawl/Playwright fetch inside their own sandbox — per-hop
        validation is impossible there, so the requested URL must itself
        resolve public before dispatch. Returns the failure result to
        send back, or ``None`` when the URL passes.
        """
        try:
            await self._netguard.check(url)
        except Exception as exc:  # noqa: BLE001 — refuse, don't fetch
            return FetchResult(
                url=url,
                success=False,
                tier=tier,
                error=f"SSRF blocked: {exc}",
            )
        return None

    async def fetch_firecrawl(self, url: str, *, timeout: float | None = None) -> FetchResult:
        """Tier 3: Firecrawl API."""
        blocked = await self._egress_check(url, "firecrawl")
        if blocked is not None:
            return blocked
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout_firecrawl) as client:
                resp = await client.post(
                    f"{self.firecrawl_url}/v2/scrape",
                    json={"url": url, "formats": ["markdown"]},
                )
                elapsed = (time.monotonic() - start) * 1000

                if resp.status_code == 200:
                    data = resp.json()
                    if not data.get("success"):
                        return FetchResult(
                            url=url,
                            success=False,
                            status_code=200,
                            fetch_time_ms=elapsed,
                            tier="firecrawl",
                            error=data.get("error", "scrape failed"),
                        )
                    # /v2/scrape wraps the document: {success, data:{markdown,
                    # metadata}} — same envelope providers/firecrawl.py unwraps.
                    payload = data.get("data") or {}
                    # Firecrawl follows redirects inside its own sandbox —
                    # a final URL on a different host was never vetted.
                    final_url = (payload.get("metadata") or {}).get("sourceURL") or url
                    if not _same_host(final_url, url):
                        return FetchResult(
                            url=url,
                            success=False,
                            fetch_time_ms=elapsed,
                            tier="firecrawl",
                            error=f"firecrawl landed off-host {final_url}",
                        )
                    content = payload.get("markdown", payload.get("content", ""))
                    return FetchResult(
                        url=url,
                        success=True,
                        status_code=200,
                        content=content,
                        content_type="text/markdown",
                        content_hash=hashlib.sha256(content.encode()).hexdigest()[:16],
                        fetch_time_ms=elapsed,
                        tier="firecrawl",
                        metadata={"firecrawl": payload},
                    )
                else:
                    return FetchResult(
                        url=url,
                        success=False,
                        status_code=resp.status_code,
                        fetch_time_ms=elapsed,
                        tier="firecrawl",
                        error=f"Firecrawl {resp.status_code}",
                    )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return FetchResult(
                url=url,
                success=False,
                fetch_time_ms=elapsed,
                tier="firecrawl",
                error=str(exc),
            )

    async def fetch_playwright(self, url: str) -> FetchResult:
        """Tier 4: Playwright (JS-heavy pages)."""
        blocked = await self._egress_check(url, "playwright")
        if blocked is not None:
            return blocked
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_playwright) as client:
                resp = await client.post(
                    f"{self.playwright_url}/render",
                    json={"url": url, "waitUntil": "networkidle"},
                )
                elapsed = (time.monotonic() - start) * 1000

                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("html", data.get("content", ""))
                    return FetchResult(
                        url=url,
                        success=True,
                        status_code=200,
                        content=content,
                        content_type="text/html",
                        content_hash=hashlib.sha256(content.encode()).hexdigest()[:16],
                        fetch_time_ms=elapsed,
                        tier="playwright",
                        metadata={"playwright": data},
                    )
                else:
                    return FetchResult(
                        url=url,
                        success=False,
                        status_code=resp.status_code,
                        fetch_time_ms=elapsed,
                        tier="playwright",
                        error=f"Playwright {resp.status_code}",
                    )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return FetchResult(
                url=url,
                success=False,
                fetch_time_ms=elapsed,
                tier="playwright",
                error=str(exc),
            )
