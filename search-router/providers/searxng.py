"""SearXNG search provider — metasearch aggregator."""

import httpx
from canonical.url import canonical_url
from config import settings
from models import (
    RetrievalObservation,
    SearchCategory,
    SearchResultItem,
    _utc_now_iso,
    result_fingerprint,
)
from pipeline.freshness import detect_live_numeric, rephrase_for_widening

MIN_RESULTS_BEFORE_WIDENING = 5

# SearXNG accepted ``time_range`` literals (anything else → HTTP 400 downstream).
# Source of truth shared with ``core.provider_registry``.
VALID_TIME_RANGES = frozenset({"day", "week", "month", "year"})


def _parse_results(data: dict) -> list[SearchResultItem]:
    results: list[SearchResultItem] = []
    retrieved_at = _utc_now_iso()
    for rank, r in enumerate(data.get("results", []), start=1):
        url = r.get("url") or ""
        title = r.get("title") or ""
        description = r.get("content") or ""
        engines = r.get("engines", []) or []
        engine = ", ".join(engines) if engines else "searxng"
        c_url = canonical_url(url)
        fingerprint = result_fingerprint(c_url, title, description)
        results.append(
            SearchResultItem(
                url=url,
                canonical_url=c_url,
                title=title,
                description=description,
                fingerprint=fingerprint,
                score=r.get("score", 0.0) or 0.0,
                engine=engine,
                category=r.get("category") or "",
                published_date=r.get("publishedDate"),
                # Image-category engines report the picture itself as img_src
                # (thumbnail/thumbnail_src are just previews); prefer it so the
                # image lane can hand back a usable visual URL.
                thumbnail=r.get("img_src") or r.get("thumbnail") or r.get("thumbnail_src") or "",
                retrieval_observations=[
                    RetrievalObservation(
                        provider="searxng",
                        engine=engine,
                        rank=rank,
                        retrieved_at=retrieved_at,
                    )
                ],
            )
        )
    return results


def _dedupe(results: list[SearchResultItem]) -> list[SearchResultItem]:
    seen: set[str] = set()
    out: list[SearchResultItem] = []
    for r in results:
        if not r.url:
            continue
        if r.url in seen:
            continue
        seen.add(r.url)
        out.append(r)
    return out


def _unresponsive_engines(data: dict) -> dict[str, str]:
    """SearXNG reports dead/blocked engines as ``unresponsive_engines``:
    a list of ``[engine, reason]`` pairs (or a dict on newer versions)."""
    raw = data.get("unresponsive_engines") or []
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for entry in raw:
        if isinstance(entry, (list, tuple)) and entry:
            out[str(entry[0])] = str(entry[1]) if len(entry) > 1 else ""
    return out


async def _searxng_fetch(
    client: httpx.AsyncClient,
    query: str,
    categories: list[SearchCategory],
    lang: str,
    safe: bool,
    page: int,
    time_range: str | None,
) -> tuple[list[SearchResultItem], dict[str, str]]:
    cats = ",".join(c.value for c in categories)
    params: dict[str, str | int] = {
        "q": query,
        "format": "json",
        "categories": cats,
        "language": lang,
        "pageno": page,
    }
    if safe:
        params["safesearch"] = 2
    if time_range:
        params["time_range"] = time_range

    resp = await client.get(f"{settings.searxng_url}/search", params=params)
    resp.raise_for_status()
    data = resp.json()
    return _parse_results(data), _unresponsive_engines(data)


async def searxng_search(
    query: str,
    categories: list[SearchCategory] | None = None,
    max_results: int = 10,
    lang: str = "en",
    safe: bool = False,
    page: int = 1,
    time_range: str | None = None,
    widen_on_thin: bool = True,
    engine_signals: dict[str, str] | None = None,
) -> list[SearchResultItem]:
    """Search via SearXNG JSON API.

    Wave-8D: live / numeric / time-sensitive queries are auto-routed to a
    recent ``time_range`` and widened into news categories; thin results are
    retried with a rephrased supplemental query. Non time-sensitive queries
    follow the exact legacy path (no ``time_range``, no extra categories).
    """
    if time_range is not None and time_range not in VALID_TIME_RANGES:
        raise ValueError(
            f"invalid time_range: {time_range!r} "
            f"(expected one of {sorted(VALID_TIME_RANGES)} or None)"
        )

    signal = detect_live_numeric(query)
    cats = list(categories or [SearchCategory.general])

    effective_time_range = time_range or (signal.time_range if signal.time_sensitive else None)
    time_sensitive = effective_time_range is not None

    if time_sensitive and SearchCategory.news not in cats:
        cats.append(SearchCategory.news)

    async with httpx.AsyncClient(timeout=30) as client:
        results, signals = await _searxng_fetch(
            client, query, cats, lang, safe, page, effective_time_range
        )
        if engine_signals is not None:
            engine_signals.update(signals)
        if widen_on_thin and time_sensitive and len(results) < MIN_RESULTS_BEFORE_WIDENING:
            widened = rephrase_for_widening(query, signal)
            if widened:
                extra, signals = await _searxng_fetch(
                    client, widened, cats, lang, safe, page, effective_time_range
                )
                if engine_signals is not None:
                    engine_signals.update(signals)
                results = _dedupe(results + extra)

        # Safety net: if the time_range-restricted pass (plus widening) is still
        # near-zero, retry the ORIGINAL query once WITHOUT time_range — never
        # re-issue the same failing constraint. Bounded to a single fallback.
        if widen_on_thin and time_sensitive and len(results) < MIN_RESULTS_BEFORE_WIDENING:
            fallback, signals = await _searxng_fetch(client, query, cats, lang, safe, page, None)
            if engine_signals is not None:
                engine_signals.update(signals)
            results = _dedupe(results + fallback)

    return results[:max_results]


async def searxng_health() -> bool:
    """Check if SearXNG is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.searxng_url}/healthz")
            return resp.status_code in (200, 404)  # 404 still means server is up
    except Exception:
        return False
