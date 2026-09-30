"""Unified Source Registry — spec-aware provider registry (Phase 2).

Every provider ships a ``ProviderSpec``: which lanes it serves, priority,
locales, timeout, budget class. Adding a new source is now:

    providers/new_provider.py + one PROVIDER_SPECS entry
    (or a ``HUB_PROVIDERS_CONFIG`` JSON row — no code at all)

and the Source Router decides when to call it, the health monitor when to
drop it, and the executor how to normalize whatever it returns.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol

import providers.arxiv as _arxiv
import providers.code_search as _code_search
import providers.ddgs as _ddgs
import providers.exa as _exa
import providers.hn as _hn
import providers.searxng as _searxng
import providers.vn_gnews as _vn_gnews
import providers.vn_market as _vn_market
import providers.vn_rss as _vn_rss
from models import SearchCategory, SearchResultItem
from providers.base import (
    ProviderResult,
    ProviderSpec,
    SearchContext,
    SourceType,
    to_provider_result,
)

logger = logging.getLogger(__name__)

# SearXNG accepted ``time_range`` literals (anything else → HTTP 400 downstream).
# Shared from the provider so validation stays in one place.
VALID_TIME_RANGES = _searxng.VALID_TIME_RANGES


@dataclass
class ProviderSearchQuery:
    """Legacy provider request, distinct from the v2 ``models.SearchQuery``.

    Providers currently wrap the legacy SearXNG request shape.  Keeping this
    name explicit prevents accidental imports of the semantic v2 query model.
    """

    query: str
    categories: list[SearchCategory] = field(default_factory=lambda: [SearchCategory.general])
    max_results: int = 10
    lang: str = "en"
    safe: bool = False
    time_range: str | None = None

    def __post_init__(self) -> None:
        if self.time_range is not None and self.time_range not in VALID_TIME_RANGES:
            raise ValueError(
                f"invalid time_range: {self.time_range!r} "
                f"(expected one of {sorted(VALID_TIME_RANGES)} or None)"
            )


ProviderSearchResult = ProviderResult

# Federation lane → SearXNG/DDGS category. Lanes without a dedicated
# category (forum, ecommerce, places, government, legal) fold into general.
_LANE_TO_CATEGORY = {
    SourceType.general_web: SearchCategory.general,
    SourceType.news: SearchCategory.news,
    SourceType.image: SearchCategory.images,
    SourceType.video: SearchCategory.videos,
    SourceType.academic: SearchCategory.science,
    SourceType.code: SearchCategory.it,
    SourceType.social: SearchCategory.social_media,
}
_CATEGORY_TO_LANE = {
    "general": SourceType.general_web,
    "text": SourceType.general_web,
    "news": SourceType.news,
    "images": SourceType.image,
    "videos": SourceType.video,
    "science": SourceType.academic,
    "it": SourceType.code,
    "social_media": SourceType.social,
}


def _categories_for(sq: ProviderSearchQuery, ctx: SearchContext | None) -> list[SearchCategory]:
    """Caller-requested categories ∪ lanes the router activated."""
    cats = list(sq.categories or [])
    if ctx is not None:
        for t in ctx.source_types:
            c = _LANE_TO_CATEGORY.get(t)
            if c is not None and c not in cats:
                cats.append(c)
    return cats or [SearchCategory.general]


def _lane_of(item: SearchResultItem, fallback: SourceType) -> str:
    lane = _CATEGORY_TO_LANE.get(getattr(item, "category", "") or "")
    return (lane or fallback).value


def _lane_hint(ctx: SearchContext | None, spec_lane: SourceType) -> SourceType:
    if ctx is not None and ctx.source_types:
        return ctx.source_types[0]
    return spec_lane


class SearchProvider(Protocol):
    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]: ...

    async def health(self) -> bool: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, SearchProvider] = {}
        self._specs: dict[str, ProviderSpec] = {}
        self._lock = RLock()

    def register(
        self, name: str, provider: SearchProvider, spec: ProviderSpec | None = None
    ) -> None:
        if not name:
            raise ValueError("provider name must not be empty")
        with self._lock:
            self._providers[name] = provider
            if spec is not None:
                self._specs[name] = spec

    def get(self, name: str) -> SearchProvider | None:
        with self._lock:
            return self._providers.get(name)

    def spec(self, name: str) -> ProviderSpec | None:
        with self._lock:
            return self._specs.get(name)

    def specs(self) -> dict[str, ProviderSpec]:
        with self._lock:
            return dict(self._specs)

    def all(self) -> list[tuple[str, SearchProvider]]:
        with self._lock:
            return list(self._providers.items())

    async def health(self) -> dict[str, bool]:
        status: dict[str, bool] = {}
        for name, provider in self.all():
            try:
                status[name] = await provider.health()
            except Exception:
                status[name] = False
        return status


# ── provider spec table ──────────────────────────────────────────────────
# The "one entry" per source. ``enabled`` is resolved at load time
# (settings.provider_enabled + PROVIDER_<NAME>_ENABLED env + JSON config).
PROVIDER_SPECS: dict[str, dict[str, Any]] = {
    "opensearch": {
        "source_types": [SourceType.index],
        "priority": 1.0,
        "timeout_s": 8.0,
        "target_latency_ms": 800.0,
        "internal": True,
    },
    "searxng": {
        "source_types": [
            SourceType.general_web,
            SourceType.news,
            SourceType.image,
            SourceType.video,
            SourceType.academic,
            SourceType.code,
            SourceType.social,
        ],
        "priority": 1.0,
        "timeout_s": 30.0,
        "target_latency_ms": 3500.0,
    },
    "ddgs": {
        "source_types": [
            SourceType.general_web,
            SourceType.news,
            SourceType.image,
            SourceType.video,
        ],
        "priority": 0.9,
        "timeout_s": 12.0,
        "target_latency_ms": 2500.0,
    },
    "arxiv": {
        "source_types": [SourceType.academic],
        "priority": 0.85,
        "timeout_s": 20.0,
        "target_latency_ms": 2500.0,
    },
    "hn": {
        "source_types": [SourceType.forum],
        "priority": 0.8,
        "timeout_s": 10.0,
        "target_latency_ms": 1500.0,
    },
    "code_search": {
        "source_types": [SourceType.code],
        "priority": 0.8,
        "timeout_s": 15.0,
        "target_latency_ms": 2500.0,
    },
    "exa": {
        "source_types": [SourceType.general_web],
        "priority": 0.7,
        "timeout_s": 20.0,
        "target_latency_ms": 3500.0,
    },
    # VN RSS sources (P2) — direct feeds, no key, per-source circuit breaking.
    # Priority mirrors the authority table in agent/reranker.py.
    "vnexpress": {
        "source_types": [SourceType.news, SourceType.general_web],
        "priority": 0.8,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    "tuoitre": {
        "source_types": [SourceType.news, SourceType.general_web],
        "priority": 0.8,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    "dantri": {
        "source_types": [SourceType.news, SourceType.general_web],
        "priority": 0.8,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    "thanhnien": {
        "source_types": [SourceType.news, SourceType.general_web],
        "priority": 0.78,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    "nhandan": {
        "source_types": [SourceType.news, SourceType.government],
        "priority": 0.9,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    "vietnamplus": {
        "source_types": [SourceType.news, SourceType.government],
        "priority": 0.9,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    "congthuong": {
        "source_types": [SourceType.news, SourceType.business, SourceType.market],
        "priority": 0.75,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    # Google News metasearch lanes (P3) — queryable VN-locale coverage,
    # site-scoped to official domains for the legal/government lanes.
    "gnews_vn": {
        "source_types": [SourceType.news, SourceType.general_web],
        "priority": 0.7,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2500.0,
    },
    "gnews_vn_gov": {
        "source_types": [
            SourceType.government,
            SourceType.administrative,
            SourceType.news,
        ],
        "priority": 0.85,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2500.0,
    },
    "gnews_vn_legal": {
        "source_types": [
            SourceType.legal,
            SourceType.government,
            SourceType.document,
        ],
        "priority": 0.9,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2500.0,
    },
    # Market quote lanes (P9) — keyless public endpoints, one provider per
    # instrument family so a walled source trips only its own breaker.
    "vn_gold": {
        "source_types": [SourceType.market, SourceType.finance],
        "priority": 0.85,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    "vn_fx": {
        "source_types": [SourceType.market, SourceType.finance],
        "priority": 0.85,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
    "vn_stock": {
        "source_types": [SourceType.market, SourceType.finance],
        "priority": 0.85,
        "countries": ["VN"],
        "languages": ["vi"],
        "timeout_s": 15.0,
        "target_latency_ms": 2000.0,
    },
}


def _spec_from_mapping(name: str, row: dict[str, Any]) -> ProviderSpec:
    spec = ProviderSpec(name=name)
    for key, value in row.items():
        if key == "name":
            continue
        if key == "source_types":
            spec.source_types = [
                t if isinstance(t, SourceType) else SourceType(str(t)) for t in value
            ]
        elif hasattr(spec, key):
            setattr(spec, key, value)
    return spec


def load_provider_specs() -> dict[str, ProviderSpec]:
    """Spec table → JSON overlay (``HUB_PROVIDERS_CONFIG``) → env enabled flags."""
    from config import settings

    specs: dict[str, ProviderSpec] = {}
    for name, row in PROVIDER_SPECS.items():
        specs[name] = _spec_from_mapping(name, row)

    cfg_path = os.getenv("HUB_PROVIDERS_CONFIG") or settings.providers_config_path
    if cfg_path:
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            for name, row in (raw.get("providers") or {}).items():
                base = specs.get(name) or ProviderSpec(name=name)
                merged = _spec_from_mapping(name, {**base.__dict__, **(row or {})})
                specs[name] = merged
        except Exception as exc:
            logger.warning("providers config %r failed to load: %s", cfg_path, exc)

    # Env/Settings enabled flags win last — named providers without a
    # dedicated env var still honour the generic PROVIDER_<NAME>_ENABLED.
    for name, spec in specs.items():
        env = os.getenv(f"PROVIDER_{name.upper()}_ENABLED")
        if env is not None:
            spec.enabled = env.lower() == "true"
        elif name == "opensearch":
            spec.enabled = settings.opensearch_enabled
        elif name == "code_search":
            spec.enabled = settings.provider_enabled.get("github", True)
        elif name in settings.provider_enabled:
            spec.enabled = settings.provider_enabled[name]
        # exa / ddgs default on — they're internally gated (API key /
        # library availability) and health scoring retires them naturally.
    return specs


# ── adapters ─────────────────────────────────────────────────────────────


class DdgsProvider:
    spec_lane = SourceType.general_web

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        sink: list[str] = []
        items = await _ddgs.ddgs_search(
            query=query.query,
            categories=_categories_for(query, ctx),
            max_results=query.max_results,
            lang=query.lang,
            safe=query.safe,
            time_range=query.time_range,
            call_error=sink,
        )
        self.last_call_error = sink[0] if sink else None
        lane = _lane_hint(ctx, self.spec_lane)
        return [
            to_provider_result(it, source="ddgs", source_type=_lane_of(it, lane)) for it in items
        ]

    async def health(self) -> bool:
        return await _ddgs.ddgs_health()


class SearXNGProvider:
    spec_lane = SourceType.general_web

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        signals: dict[str, str] = {}
        items = await _searxng.searxng_search(
            query=query.query,
            categories=_categories_for(query, ctx),
            max_results=query.max_results,
            lang=query.lang,
            safe=query.safe,
            time_range=query.time_range,
            engine_signals=signals,
        )
        self.last_engine_signals = signals
        lane = _lane_hint(ctx, self.spec_lane)
        return [
            to_provider_result(it, source="searxng", source_type=_lane_of(it, lane)) for it in items
        ]

    async def health(self) -> bool:
        return await _searxng.searxng_health()


class ExaProvider:
    """Premium semantic search (Exa) — key-gated, not part of the default path.

    ``health`` reports a ``bool`` like the other providers: ``True`` when a key
    is configured and reachable, ``False`` otherwise.
    """

    spec_lane = SourceType.general_web

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        items = await _exa.exa_search(
            query=query.query,
            max_results=query.max_results,
            time_range=query.time_range,
            category=query.categories[0].value if query.categories else "",
        )
        return [
            to_provider_result(it, source="exa", source_type=_lane_of(it, self.spec_lane))
            for it in items
        ]

    async def health(self) -> bool:
        return await _exa.exa_health()


class ArxivProvider:
    spec_lane = SourceType.academic

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        sink: list[str] = []
        items = await _arxiv.arxiv_search(
            query=query.query, max_results=query.max_results, call_error=sink
        )
        self.last_call_error = sink[0] if sink else None
        return [
            to_provider_result(it, source="arxiv", source_type=_lane_of(it, self.spec_lane))
            for it in items
        ]

    async def health(self) -> bool:
        return await _arxiv.arxiv_health()


class HnProvider:
    spec_lane = SourceType.forum

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        sink: list[str] = []
        items = await _hn.hn_search(
            query=query.query,
            max_results=query.max_results,
            sort_by_date=bool(ctx and ctx.freshness_required),
            call_error=sink,
        )
        self.last_call_error = sink[0] if sink else None
        return [
            to_provider_result(it, source="hn", source_type=_lane_of(it, self.spec_lane))
            for it in items
        ]

    async def health(self) -> bool:
        return await _hn.hn_health()


class CodeSearchProvider:
    """Code lane — GitHub code search + grep.app, merged."""

    spec_lane = SourceType.code

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        sink: list[str] = []
        github_items = await _code_search.github_search(
            query=query.query, max_results=query.max_results, call_error=sink
        )
        grep_items = await _code_search.grep_app_search(
            query=query.query, max_results=query.max_results, call_error=sink
        )
        self.last_call_error = sink[0] if sink else None
        out = [
            to_provider_result(it, source="code_search", source_type=SourceType.code)
            for it in github_items + grep_items
        ]
        return out[: query.max_results]

    async def health(self) -> bool:
        # Public endpoints, no key required — treat as always configured.
        return True


class VnGNewsProvider:
    """Google News metasearch lane — server-side queryable VN coverage."""

    def __init__(self, lane: str) -> None:
        self.lane = lane
        self.spec_lane = SourceType.news
        self.last_call_error: str | None = None

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        sink: list[str] = []
        items = await _vn_gnews.gnews_search(
            self.lane,
            query=query.query,
            max_results=query.max_results,
            call_error=sink,
        )
        self.last_call_error = sink[0] if sink else None
        lane = _lane_hint(ctx, self.spec_lane)
        return [
            to_provider_result(
                it,
                source=self.lane,
                source_type=_lane_of(it, lane),
                language="vi",
                country="VN",
            )
            for it in items
        ]

    async def health(self) -> bool:
        return await _vn_gnews.gnews_health(self.lane)


class VnMarketProvider:
    """Market quote lane — spec name selects the instrument family (P9)."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.spec_lane = SourceType.market

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        sink: list[str] = []
        items = await _vn_market.vn_market_search(
            self.source,
            query=query.query,
            max_results=query.max_results,
            call_error=sink,
        )
        self.last_call_error = sink[0] if sink else None
        lane = _lane_hint(ctx, self.spec_lane)
        return [
            to_provider_result(
                it,
                source=self.source,
                source_type=_lane_of(it, lane),
                language="vi",
                country="VN",
            )
            for it in items
        ]

    async def health(self) -> bool:
        return await _vn_market.vn_market_health(self.source)


class VnRssProvider:
    """One adapter class serves every ``VN_FEEDS`` row — spec name picks the feed."""

    def __init__(self, feed: str) -> None:
        self.feed = feed
        self.spec_lane = SourceType.news

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        sink: list[str] = []
        items = await _vn_rss.vn_feed_search(
            self.feed,
            query=query.query,
            max_results=query.max_results,
            call_error=sink,
        )
        self.last_call_error = sink[0] if sink else None
        lane = _lane_hint(ctx, self.spec_lane)
        return [
            to_provider_result(
                it,
                source=self.feed,
                source_type=_lane_of(it, lane),
                language="vi",
                country="VN",
            )
            for it in items
        ]

    async def health(self) -> bool:
        return await _vn_rss.vn_feed_health(self.feed)


class _OpensearchHealthError(RuntimeError):
    pass


class OpenSearchIndexAdapter:
    """Wraps providers.opensearch_index.OpenSearchIndexProvider to the
    Phase-2 shape — ``search(sq, ctx)`` + empty-means-down reporting."""

    spec_lane = SourceType.index

    def __init__(self, inner: Any | None = None) -> None:
        if inner is None:
            from providers.opensearch_index import OpenSearchIndexProvider

            inner = OpenSearchIndexProvider()
        self._inner = inner

    async def search(
        self, query: ProviderSearchQuery, ctx: SearchContext | None = None
    ) -> list[ProviderSearchResult]:
        items = await self._inner.search(query)
        if items:
            self.last_call_error = None
        else:
            # Empty from the internal lane is only suspicious when the index
            # itself is unhealthy — distinguish "corpus thin" from "index down".
            try:
                healthy = self._inner.client.health()
            except Exception:
                healthy = False
            self.last_call_error = None if healthy else "opensearch unreachable"
        return [
            to_provider_result(it, source="opensearch", source_type=SourceType.index)
            for it in items
        ]

    async def health(self) -> bool:
        try:
            return bool(self._inner.client.health())
        except Exception:
            return False


def create_default_registry() -> ProviderRegistry:
    from config import settings

    specs = load_provider_specs()
    registry = ProviderRegistry()
    # Internal index lane first — cheap (~ms) and canonical per v2.1; live-web
    # lanes supplement it. Provider degrades to [] when OpenSearch is down.
    if settings.opensearch_enabled:
        registry.register("opensearch", OpenSearchIndexAdapter(), specs.get("opensearch"))

    adapters: dict[str, Any] = {
        "searxng": SearXNGProvider,
        "ddgs": DdgsProvider,
        "arxiv": ArxivProvider,
        "hn": HnProvider,
        "code_search": CodeSearchProvider,
        "exa": ExaProvider,
    }
    for name, cls in adapters.items():
        spec = specs.get(name)
        if spec is not None and spec.enabled:
            registry.register(name, cls(), spec)
    for feed in _vn_rss.VN_FEEDS:
        spec = specs.get(feed)
        if spec is not None and spec.enabled:
            registry.register(feed, VnRssProvider(feed), spec)
    for lane in _vn_gnews.GNEWS_LANES:
        spec = specs.get(lane)
        if spec is not None and spec.enabled:
            registry.register(lane, VnGNewsProvider(lane), spec)
    for source in _vn_market.MARKET_SOURCES:
        spec = specs.get(source)
        if spec is not None and spec.enabled:
            registry.register(source, VnMarketProvider(source), spec)
    # JSON-configured providers not in the built-in adapter table surface as
    # specs for health reporting even though nothing calls them yet — the
    # router will pick them up once an adapter class is registered here.
    return registry
