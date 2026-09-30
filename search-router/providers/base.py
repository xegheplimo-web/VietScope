"""Source federation contract — one schema every provider speaks (Phase 2).

The federation layer sits between the Source Router and the provider
adapters:

- ``ProviderResult`` is the common result schema. Every adapter — SearXNG,
  DDGS, arXiv, HN, code search, the internal index, and any future source —
  returns ``list[ProviderResult]`` so the orchestrator never changes when a
  provider is added. Provenance fields produced by Phase 1
  (``canonical_url``/``fingerprint``/``retrieval_observations``) ride along
  untouched.
- ``ProviderSpec`` is the registry descriptor (name, source types, priority,
  countries, languages, enabled, timeout). Adding a source is a new adapter
  file plus one entry in ``core.provider_registry.PROVIDER_SPECS``.
- ``SearchContext`` carries the router's per-query decisions (which lanes
  fired, language, freshness) to the adapters.
- ``CallOutcome`` / ``ProviderCallReport`` feed ``core.provider_health`` so
  the circuit breaker sees honest outcomes instead of "empty means fine".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from models import RetrievalObservation, SearchResultItem, _utc_now_iso
from pydantic import BaseModel, Field


class SourceType(StrEnum):
    """The federation lanes a provider can serve (Source Router taxonomy)."""

    general_web = "general_web"
    news = "news"
    government = "government"
    legal = "legal"
    places = "places"
    ecommerce = "ecommerce"
    forum = "forum"
    social = "social"
    academic = "academic"
    code = "code"
    video = "video"
    image = "image"
    index = "index"  # own corpus (OpenSearch/Qdrant) — the "own index" lane
    # VN taxonomy lanes (P1) — providers subscribe in later phases
    business = "business"
    company = "company"
    finance = "finance"
    market = "market"
    administrative = "administrative"
    product = "product"
    medical = "medical"
    document = "document"


class CallOutcome(StrEnum):
    """Outcome of a single provider call — the circuit breaker's raw signal."""

    success = "success"  # returned >= 1 result without error
    empty = "empty"  # answered cleanly, zero results
    timeout = "timeout"
    captcha = "captcha"  # bot-wall / CAPTCHA challenge
    rate_limited = "rate_limited"  # HTTP 429 / explicit throttling
    error = "error"  # any other failure
    disabled = "disabled"  # skipped: unconfigured or turned off


class ProviderResult(BaseModel):
    """Common search-result schema every provider returns.

    Mirrors the Phase-2 contract: ``url``/``title``/``snippet`` identify the
    hit, ``source``/``source_type`` say who found it and on which lane, and
    the rest is uniform metadata. Phase-1 provenance fields stay first-class
    so dedup/fingerprint pipelines keep working.
    """

    url: str
    title: str = ""
    snippet: str | None = None

    source: str = ""  # provider name, e.g. "searxng"
    source_type: str = ""  # SourceType value of the lane that produced it

    published_at: str | None = None
    crawled_at: str | None = None  # when the provider returned it (retrieval ts)

    language: str | None = None
    country: str | None = None

    score: float | None = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ── Phase-1 provenance, carried for dedup/fusion (not part of the minimal
    # contract but required by the existing normalize/dedup pipeline).
    canonical_url: str = ""
    fingerprint: str = ""
    engine: str = ""  # upstream engine inside a metasearch provider
    thumbnail: str | None = ""
    retrieval_observations: list[RetrievalObservation] = []

    @property
    def usable(self) -> bool:
        """A result counts as usable when it has a URL plus human context."""
        return bool(self.url and (self.title or self.snippet))


@dataclass
class ProviderSpec:
    """Registry descriptor — the "one entry" needed to add a source."""

    name: str
    source_types: list[SourceType] = field(default_factory=lambda: [SourceType.general_web])
    priority: float = 1.0  # 0..1 static weight inside its lanes
    countries: list[str] = field(default_factory=list)  # ISO codes, empty = all
    languages: list[str] = field(default_factory=list)  # ISO 639-1, empty = all
    enabled: bool = True
    timeout_s: float = 20.0  # per-call deadline
    internal: bool = False  # own-index lane: cheap, never consumes query budget
    target_latency_ms: float = 3000.0  # health-score latency reference
    max_results_bias: float = 1.0  # reserved for per-provider recall tuning

    def supports(self, language: str | None = None, country: str | None = None) -> bool:
        if self.languages and language and language not in self.languages:
            return False
        return not (self.countries and country and country not in self.countries)


@dataclass
class SearchContext:
    """Per-query routing context handed to each selected provider."""

    source_types: list[SourceType] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)  # provider-side category hints
    language: str = "en"
    country: str | None = None
    freshness_required: bool = False
    time_range: str | None = None
    mode: str = "normal"
    intent: str = ""
    probe: bool = False  # this call is a circuit-breaker half-open probe


@dataclass
class ProviderCallReport:
    """What the health monitor learns from one provider call."""

    outcome: CallOutcome
    latency_ms: float = 0.0
    error: str = ""
    result_count: int = 0
    unique_result_count: int = 0
    usable_result_count: int = 0
    probe: bool = False
    # Metasearch-level signals, e.g. SearXNG ``unresponsive_engines``:
    # {"qwant": "captcha", "bing": "ok"} — feeds the engine health layer.
    engine_signals: dict[str, str] = field(default_factory=dict)


class ProviderSearchError(Exception):
    """A provider call that failed with a classified outcome.

    Adapters raise this (or let httpx/TimeoutError propagate — the executor
    classifies both) so the health monitor records the *kind* of failure
    instead of a generic error.
    """

    def __init__(self, message: str, outcome: CallOutcome = CallOutcome.error):
        super().__init__(message)
        self.outcome = outcome


_TIMEOUT_TYPES: tuple[type[BaseException], ...] = ()
_STATUS_TYPES: tuple[type[Exception], ...] = ()
try:  # httpx is a hard dep; the try block only narrows types for linters.
    import httpx

    _TIMEOUT_TYPES = (
        httpx.TimeoutException,
        TimeoutError,
    )
    _STATUS_TYPES = (httpx.HTTPStatusError,)
except Exception:  # pragma: no cover
    _TIMEOUT_TYPES = (TimeoutError,)

_CAPTCHA_RE = re.compile(r"captcha|solvemedia|are you a robot|unusual traffic", re.I)
_RATE_LIMIT_RE = re.compile(r"rate.?limit|too many requests|429", re.I)
_TIMEOUT_TEXT_RE = re.compile(r"time.?out|timed out|deadline exceeded", re.I)


def classify_exception(exc: BaseException) -> CallOutcome:
    """Map an exception to a CallOutcome for the health monitor."""
    if isinstance(exc, ProviderSearchError):
        return exc.outcome
    if isinstance(exc, TimeoutError) or (_TIMEOUT_TYPES and isinstance(exc, _TIMEOUT_TYPES)):
        # asyncio.TimeoutError is a TimeoutError alias on 3.12 (asyncio.wait_for);
        # httpx.TimeoutException is its own hierarchy — both mean "timeout".
        return CallOutcome.timeout
    if _STATUS_TYPES and isinstance(exc, _STATUS_TYPES):
        code = exc.response.status_code
        if code == 429:
            return CallOutcome.rate_limited
        if code in (401, 402, 403):
            # Bot walls surface as 403 more often than as an explicit captcha page.
            body = getattr(exc.response, "text", "") or ""
            return CallOutcome.captcha if _CAPTCHA_RE.search(body) else CallOutcome.error
        return CallOutcome.error
    if _TIMEOUT_TYPES and isinstance(exc, _TIMEOUT_TYPES):
        return CallOutcome.timeout
    msg = str(exc)
    if _CAPTCHA_RE.search(msg):
        return CallOutcome.captcha
    if _RATE_LIMIT_RE.search(msg):
        return CallOutcome.rate_limited
    if _TIMEOUT_TEXT_RE.search(msg):
        return CallOutcome.timeout
    return CallOutcome.error


def classify_error_text(msg: str) -> CallOutcome:
    """Classify a provider error *message* (providers that swallow errors)."""
    m = msg or ""
    if _CAPTCHA_RE.search(m):
        return CallOutcome.captcha
    if _RATE_LIMIT_RE.search(m):
        return CallOutcome.rate_limited
    if _TIMEOUT_TEXT_RE.search(m):
        return CallOutcome.timeout
    return CallOutcome.error


def classify_engine_signal(reason: str) -> str:
    """Classify a SearXNG ``unresponsive_engines`` reason into a coarse kind."""
    r = reason or ""
    if _CAPTCHA_RE.search(r):
        return "captcha"
    if _RATE_LIMIT_RE.search(r):
        return "rate_limited"
    if re.search(r"timeout|timed out|deadline", r, re.I):
        return "timeout"
    return "error"


def to_provider_result(
    item: SearchResultItem | ProviderResult | dict,
    *,
    source: str,
    source_type: SourceType | str = SourceType.general_web,
    language: str | None = None,
    country: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ProviderResult:
    """Normalize any provider payload onto the common schema.

    Accepts a ``ProviderResult`` (pass-through with lane fields filled), the
    legacy ``SearchResultItem`` wire item, or a plain dict (code-search style
    providers that return raw mappings).
    """
    st = source_type.value if isinstance(source_type, SourceType) else str(source_type or "")
    if isinstance(item, ProviderResult):
        item.source = item.source or source
        item.source_type = item.source_type or st
        item.language = item.language or language
        item.country = item.country or country
        if metadata:
            item.metadata.update(metadata)
        return item
    if isinstance(item, SearchResultItem):
        return ProviderResult(
            url=item.url,
            title=item.title or "",
            snippet=item.description,
            source=source,
            source_type=st,
            published_at=item.published_date,
            crawled_at=_utc_now_iso(),
            language=language,
            country=country,
            score=item.score,
            # Per-item extras (e.g. gnews publisher identity) win over the
            # provider-level lane metadata on key collisions.
            metadata={**(metadata or {}), **(item.metadata or {})},
            canonical_url=item.canonical_url,
            fingerprint=item.fingerprint,
            engine=item.engine,
            thumbnail=item.thumbnail,
            retrieval_observations=list(item.retrieval_observations or []),
        )
    # Raw mapping providers (code_search et al.).
    url = str(item.get("url") or "")
    return ProviderResult(
        url=url,
        title=str(item.get("title") or ""),
        snippet=item.get("description") or item.get("body") or item.get("snippet"),
        source=source,
        source_type=st,
        published_at=item.get("published_date") or item.get("published_at"),
        crawled_at=_utc_now_iso(),
        language=language,
        country=country,
        score=float(item.get("score") or 0.0),
        metadata={**(metadata or {}), **{k: v for k, v in item.items() if k not in _CONSUMED_KEYS}},
    )


def result_domain(url: str, metadata: Mapping[str, Any] | None = None) -> str:
    """Domain to score/display for a result.

    Metasearch wrappers (Google News) put the true publisher in
    ``metadata["publisher_domain"]`` — it wins over the wrapper's netloc
    so authority/trust see the real outlet instead of ``news.google.com``.
    Everything else keeps the URL's own host (lower-cased, ``www.`` stripped).
    """
    if metadata:
        publisher = metadata.get("publisher_domain")
        if publisher:
            return str(publisher)
    return urlparse(url).netloc.lower().removeprefix("www.")


_CONSUMED_KEYS = frozenset(
    {
        "url",
        "title",
        "description",
        "body",
        "snippet",
        "published_date",
        "published_at",
        "score",
    }
)
