"""HTTP fetcher for the crawl pipeline.

Plain httpx GET first — through ``netguard.guarded_get`` so every redirect
hop is SSRF-validated and the body is capped at ``max_bytes`` without
silent truncation (``oversize=True`` past the cap; conditional-GET
headers supported). An HTML response whose visible text is implausibly
thin — a JS-shell page — falls back to Firecrawl's rendered scrape
(``providers.firecrawl.firecrawl_scrape``) *only when* the operator opted
in via ``CRAWLER_FIRECRAWL_FALLBACK=true`` (G4) — and only on *positive*
JS evidence: thin text plus a ``<script>``/``<noscript>``/app-root
marker (R2). With the fallback off — the default — a marker-bearing,
link-less thin page is reported as ``js_required`` and the pipeline
parks the URL; the stub body is never indexed. A thin page that still
carries discoverable ``<a href>`` targets is a normal document — it is
persisted and its links are discovered. With the fallback on, a
Firecrawl failure still returns the original httpx result; only a
missing httpx result (network error, non-2xx, unresolved redirect)
propagates as ``ok=False``.

Never raises: network/timeout/policy failures return ``FetchResult(ok=False)``
with ``error`` set — the pipeline maps those to frontier backoff.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from crawler.netguard import NetGuard, guarded_client, guarded_get
from crawler.robots import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 30.0
_MAX_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 10
# Below this many bytes of visible text an HTML page is treated as a
# JS shell and retried through Firecrawl's renderer.
_JS_TEXT_THRESHOLD = 200


def _firecrawl_fallback_enabled() -> bool:
    """``CRAWLER_FIRECRAWL_FALLBACK`` — opt-in renderer lane (G4).

    Off by default: Firecrawl renders server-side and follows redirects
    inside its own sandbox, so per-hop robots/politeness/SSRF enforcement
    cannot apply — only post-fetch checks. JS-shell pages therefore
    surface as ``js_required`` unless the operator explicitly enables it.
    """
    return os.getenv("CRAWLER_FIRECRAWL_FALLBACK", "false").lower() == "true"


# Positive JS evidence (R2): a script tag, a noscript wrapper, or a
# conventional SPA mount point. Checked on the RAW markup — the markers
# are exactly what the structural scan skips over.
_JS_SHELL_MARKERS_RE = re.compile(
    r"<script[\s>]|<noscript[\s>]|<app-root[\s>]|\sdata-reactroot"
    r"|\sid\s*=\s*[\"']?(?:root|app|__next|__nuxt|__svelte)\b",
    re.IGNORECASE,
)

# Element bodies that are not visible text and whose markup must not be
# scanned for anchors: script/style carry code that only *looks* like
# markup ("<!--" in a JS string, "<a href=" in a bundle), while noscript/
# template content is inert fallback never rendered as links (R5/R6).
_SKIP_CONTENT_TAGS = frozenset({"script", "style", "noscript", "template"})


class _HTMLScan(HTMLParser):
    """Single structural pass over a document: visible text + anchors.

    The parser tokenizes for real, so bytes that merely look like markup
    inside a script/style body or a comment node can neither fake an
    anchor nor swallow a genuine one — the failure mode of the regex
    stack this replaced (R6).

    ``scripting=True`` gives <noscript> raw-text semantics: its body is
    scanned straight to the literal ``</noscript>`` like script/style,
    so a malformed close (an end tag arriving while inner markup is
    unclosed, or a skip element opened inside) can never hold the scan
    open and swallow the markup behind it (R7).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True, scripting=True)
        self.text_parts: list[str] = []
        self.has_anchor = False
        self._skip: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip.append(tag)
            return
        if self._skip:
            return
        if tag == "a" and not self.has_anchor:
            for name, value in attrs:
                if name == "href":
                    # Discoverable = href resolves to an http(s) target —
                    # absolute http(s):// or "/"-relative (R5).
                    if value is not None and value.lstrip().lower().startswith(
                        ("http://", "https://", "/")
                    ):
                        self.has_anchor = True
                    break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            # "<tag/>" does not self-close a non-void element in
            # text/html — the slash is ignored and the element stays
            # open, so only the start-tag half applies (R7). Raw-text
            # elements entered this way still get raw-text mode, the
            # same as parse_starttag's normal path.
            self.handle_starttag(tag, attrs)
            if tag in self.CDATA_CONTENT_ELEMENTS or (self.scripting and tag == "noscript"):
                self.set_cdata_mode(tag, escapable=False)
            return
        super().handle_startendtag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._skip and tag == self._skip[-1]:
            self._skip.pop()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.text_parts.append(data)

    @property
    def visible_text_len(self) -> int:
        return len(" ".join("".join(self.text_parts).split()))


def _scan_html(html_text: str) -> _HTMLScan:
    """Parse once: collect visible text length + discoverable-anchor flag."""
    parser = _HTMLScan()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 — a scan failure must never park a page
        parser.text_parts = ["x" * (_JS_TEXT_THRESHOLD + 1)]
        parser.has_anchor = True
    return parser


@dataclass
class FetchResult:
    """Outcome of a single URL fetch."""

    ok: bool
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    mime: str = ""
    final_url: str = ""
    elapsed_ms: float = 0.0
    via: str = "http"  # http | firecrawl
    not_modified: bool = False
    # Body exceeded max_bytes — partial content, never durable material.
    oversize: bool = False
    # Redirect hop chain (validated per hop by netguard).
    redirects: list[str] = field(default_factory=list)
    error: str | None = None
    # HTML JS-shell (positive evidence: thin text + JS markers + no
    # discoverable anchor) while the renderer fallback is disabled (G4) —
    # the pipeline parks the URL with outcome ``js_required``; no snapshot.
    js_required: bool = False


def _has_js_marker(html_text: str) -> bool:
    """Positive JS-shell evidence in the raw markup (R2)."""
    return _JS_SHELL_MARKERS_RE.search(html_text) is not None


class Fetcher:
    """httpx-first fetcher with conditional GET + opt-in Firecrawl JS fallback."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        max_bytes: int = _MAX_BYTES,
        firecrawl=None,
        js_text_threshold: int = _JS_TEXT_THRESHOLD,
        netguard: NetGuard | None = None,
        max_redirects: int = _MAX_REDIRECTS,
        firecrawl_fallback: bool | None = None,
    ) -> None:
        self._client = client
        self._ua = user_agent
        self._max_bytes = max_bytes
        self._firecrawl = firecrawl
        self._js_threshold = js_text_threshold
        self._netguard = netguard or NetGuard()
        self._max_redirects = max_redirects
        self._firecrawl_fallback = (
            _firecrawl_fallback_enabled() if firecrawl_fallback is None else firecrawl_fallback
        )

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        policy_check=None,
    ) -> FetchResult:
        """Fetch ``url``; conditional GET when validators are supplied.

        ``policy_check`` is an ``async (url) -> str | None`` run before
        every redirect hop's request — a non-None return aborts the fetch
        with ``error`` set to the refusal reason (robots/politeness are
        enforced *before* the destination is contacted, not after).
        """
        started = time.monotonic()
        headers = {"User-Agent": self._ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            resp = await self._get(url, headers, timeout, policy_check)
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            return FetchResult(
                ok=False,
                final_url=url,
                elapsed_ms=_elapsed(started),
                error=f"{type(exc).__name__}: {exc}",
            )

        if resp.error:
            return FetchResult(
                ok=False,
                status=resp.status,
                headers=resp.headers,
                final_url=resp.final_url or url,
                redirects=resp.redirects,
                elapsed_ms=_elapsed(started),
                error=resp.error,
            )
        if resp.status == 304:
            return FetchResult(
                ok=True,
                status=304,
                headers=resp.headers,
                final_url=resp.final_url,
                redirects=resp.redirects,
                elapsed_ms=_elapsed(started),
                not_modified=True,
            )
        if resp.status >= 400:
            return FetchResult(
                ok=False,
                status=resp.status,
                headers=resp.headers,
                content=resp.body,
                mime=_mime(resp.headers),
                final_url=resp.final_url,
                redirects=resp.redirects,
                elapsed_ms=_elapsed(started),
                error=f"HTTP {resp.status}",
            )
        if resp.status >= 300:
            # Non-followable 3xx (e.g. 300/305): not a valid document.
            return FetchResult(
                ok=False,
                status=resp.status,
                headers=resp.headers,
                final_url=resp.final_url,
                redirects=resp.redirects,
                elapsed_ms=_elapsed(started),
                error=f"unhandled HTTP {resp.status}",
            )

        result = FetchResult(
            ok=True,
            status=resp.status,
            headers=resp.headers,
            content=resp.body,
            mime=_mime(resp.headers),
            final_url=resp.final_url,
            redirects=resp.redirects,
            oversize=resp.oversize,
            elapsed_ms=_elapsed(started),
        )

        # JS-shell detection: thin visible text on an HTML page PLUS
        # positive JS evidence — a script/noscript/app-root marker (R2).
        # A thin static page is a normal document: parking it as
        # js_required would lose persistence and link discovery for ~30 d.
        # Skipped on oversize bodies — a truncated page's text density is
        # meaningless and the cap must apply to the rendered copy too.
        if not result.oversize and result.mime == "text/html":
            html_text = resp.body.decode("utf-8", errors="replace")
            scan = _scan_html(html_text)
            if scan.visible_text_len < self._js_threshold and _has_js_marker(html_text):
                if not self._firecrawl_fallback:
                    # Renderer lane disabled (G4) — park only when the
                    # page is a pure shell: JS markers AND no discoverable
                    # anchor. A link-bearing thin page flows through as a
                    # normal fetch (R2); the stub body is never indexed.
                    if not scan.has_anchor:
                        result.js_required = True
                    return result
                rendered = await self._via_firecrawl(resp.final_url or url, started)
                if rendered is not None:
                    return rendered
        return result

    # ── Transport ────────────────────────────────────────────────────────

    async def _get(self, url: str, headers: dict[str, str], timeout: float, policy_check):
        if self._client is not None:
            return await guarded_get(
                self._client,
                url,
                headers=headers,
                timeout=timeout,
                max_redirects=self._max_redirects,
                max_bytes=self._max_bytes,
                netguard=self._netguard,
                policy_check=policy_check,
            )
        # Self-created clients get the SSRF-validating transport so the
        # connect-time resolution is vetted too — not just the URL check.
        async with guarded_client(self._netguard, timeout=timeout) as client:
            return await guarded_get(
                client,
                url,
                headers=headers,
                timeout=timeout,
                max_redirects=self._max_redirects,
                max_bytes=self._max_bytes,
                netguard=self._netguard,
                policy_check=policy_check,
            )

    # ── Firecrawl fallback (opt-in: CRAWLER_FIRECRAWL_FALLBACK) ──────────

    async def render(self, url: str) -> FetchResult | None:
        """Render ``url`` through the opt-in Firecrawl lane — public wrapper.

        Called by the extraction retry ladder: a static HTML body that
        yielded nothing indexable gets one rendered re-fetch. Returns
        ``None`` when the lane is disabled (the default) or the render
        failed — callers must not treat None as a fetched document.
        """
        if not self._firecrawl_fallback:
            return None
        return await self._via_firecrawl(url, time.monotonic())

    async def _via_firecrawl(self, url: str, started: float) -> FetchResult | None:
        """Render ``url`` through Firecrawl — opt-in only (G4).

        WARNING: Firecrawl renders server-side and follows redirects
        inside its own sandbox — our per-hop robots/politeness/SSRF
        enforcement cannot apply there. Validation is post-fetch only
        (netguard on the requested URL + the same-host check on
        ``metadata.sourceURL`` below), which is exactly why this lane is
        off by default.
        """
        # Firecrawl renders server-side; validate the URL we hand it even
        # though the httpx hop already passed the guard.
        try:
            await self._netguard.check(url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("netguard blocked firecrawl target %s: %r", url, exc)
            return None
        scrape = self._firecrawl
        if scrape is None:
            from providers.firecrawl import firecrawl_scrape

            scrape = firecrawl_scrape
        try:
            result = await scrape(url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("firecrawl fallback failed for %s: %r", url, exc)
            return None
        if getattr(result, "error", None) or not getattr(result, "markdown", ""):
            return None
        metadata = getattr(result, "metadata", {}) or {}
        final = metadata.get("sourceURL") or url
        # Firecrawl renders server-side — its redirect chain bypasses our
        # per-hop validation. A different final host was never vetted, so
        # the result is dropped rather than indexed under it.
        if not _same_host(final, url):
            logger.debug("firecrawl landed off-host %s (asked %s) — dropping", final, url)
            return None
        content = result.markdown.encode("utf-8")
        if len(content) > self._max_bytes:
            return FetchResult(
                ok=True,
                status=200,
                mime="text/markdown",
                final_url=final,
                oversize=True,
                via="firecrawl",
                elapsed_ms=_elapsed(started),
            )
        return FetchResult(
            ok=True,
            status=200,
            headers={},
            content=content,
            mime="text/markdown",
            final_url=final,
            elapsed_ms=_elapsed(started),
            via="firecrawl",
        )


def _same_host(a: str, b: str) -> bool:
    """Case/port-insensitive host equality; malformed input never matches."""
    try:
        return (urlsplit(a).hostname or "").lower() == (urlsplit(b).hostname or "").lower()
    except ValueError:
        return False


def _mime(headers: dict[str, str]) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _elapsed(started: float) -> float:
    return (time.monotonic() - started) * 1000.0
